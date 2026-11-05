"""DB-API storage: append imports, targeted optimistic edits, atomic audit log."""
import json
import sqlite3
import threading
import atexit
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from .core import validate_rows, digest
from .checks import coverage_checks
from .workflow import Workflow

class ConflictError(ValueError):
    pass

class Store(Workflow):
    def __init__(self, url):
        self.url = url
        self._pool=None
        self._pool_lock=threading.Lock()
        self.pg = url.startswith(('postgresql://', 'postgres://'))
        if not self.pg and not url.startswith('sqlite:///'):
            raise ValueError('지원 저장소: sqlite:/// 또는 postgresql://')
        with self.connection() as c:
            for statement in [
                'CREATE TABLE IF NOT EXISTS settlement_batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS settlement_transactions (id TEXT PRIMARY KEY, month TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL)',
                'CREATE INDEX IF NOT EXISTS settlement_month ON settlement_transactions(month)',
                'CREATE TABLE IF NOT EXISTS settlement_settings (id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS settlement_audit (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS settlement_drafts (id TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS settlement_draft_versions (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(draft_id,version))',
                'CREATE TABLE IF NOT EXISTS settlement_periods (month TEXT PRIMARY KEY, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS settlement_backups (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)',
            ]:
                self.execute(c, statement)

    @contextmanager
    def connection(self, write=False):
        if self.pg:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                import psycopg
                c=psycopg.connect(self.url,connect_timeout=10)
            else:
                with self._pool_lock:
                    if self._pool is None:
                        self._pool=ConnectionPool(self.url,min_size=0,max_size=3,max_idle=120,timeout=20,
                            kwargs={'connect_timeout':10},check=ConnectionPool.check_connection,open=True)
                        atexit.register(self._pool.close)
                with self._pool.connection() as pooled:
                    yield pooled
                return
        else:
            path = self.url[len('sqlite:///'):]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(path, timeout=15)
            c.execute('PRAGMA busy_timeout=15000')
            c.execute('PRAGMA journal_mode=WAL')
            if write:
                c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def execute(self, c, sql, params=()):
        cursor = c.cursor()
        cursor.execute(sql.replace('?', '%s') if self.pg else sql, params)
        return cursor

    def rows(self, month=None):
        with self.connection() as c:
            query = 'SELECT payload,version FROM settlement_transactions'
            results = self.execute(c, query + (' WHERE month=?' if month else '') + ' ORDER BY id',
                                   (month,) if month else ()).fetchall()
            return [dict(json.loads(p), version=v) for p, v in results]

    def months(self):
        with self.connection() as c:
            return [r[0] for r in self.execute(c, 'SELECT DISTINCT month FROM settlement_transactions ORDER BY month DESC').fetchall()]

    def settings(self, prefix):
        with self.connection() as c:
            return [json.loads(p) for (p,) in self.execute(c, 'SELECT payload FROM settlement_settings WHERE id LIKE ?', (prefix+'%',)).fetchall()]

    def _setting(self, c, key, value):
        self.execute(c, 'INSERT INTO settlement_settings(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                     (key, json.dumps(value, ensure_ascii=False)))

    def save_setting(self, key, value):
        with self.connection(write=True) as c:
            self._write_lock(c)
            self._checkpoint(c,'설정 변경 전')
            self._setting(c, key, value)

    def _audit(self, c, payload):
        now = datetime.now(timezone.utc).isoformat()
        self.execute(c, 'INSERT INTO settlement_audit(id,created_at,payload) VALUES (?,?,?)',
                     (digest([now, payload]), now, json.dumps(payload, ensure_ascii=False)))

    def import_rows(self, batches, rows, rules=(), profiles=(), coverage_confirmed=False, draft_id=None, draft_version=None):
        validate_rows(rows)
        batch_ids = {b['id'] for b in batches}
        if {r['batch_id'] for r in rows} != batch_ids:
            raise ValueError('업로드 파일과 거래 연결 불일치')
        try:
            with self.connection(write=True) as c:
                if self.pg:
                    self.execute(c, 'SELECT pg_advisory_xact_lock(716294018)')
                self._assert_open(c,[r['month'] for r in rows])
                self._checkpoint(c,'정산 자료 저장 전')
                current_rows=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_transactions').fetchall()]
                self._check_coverage(c,rows,current_rows,coverage_confirmed)
                current_fingerprints={r['fingerprint'] for r in current_rows}
                if any(r['fingerprint'] in current_fingerprints and not r.get('duplicate') for r in rows):
                    raise ConflictError('검토 후 다른 업로드에서 같은 거래가 저장되었습니다. 새로 불러와 중복 후보를 확인하세요.')
                for b in batches:
                    self.execute(c, 'INSERT INTO settlement_batches(id,payload) VALUES (?,?)',
                                 (b['id'], json.dumps(b, ensure_ascii=False)))
                for r in rows:
                    value = dict(r, version=1)
                    self.execute(c, 'INSERT INTO settlement_transactions(id,month,version,payload) VALUES (?,?,?,?)',
                                 (r['id'], r['month'], 1, json.dumps(value, ensure_ascii=False)))
                for rule in rules:
                    self._setting(c, 'rule:'+rule['id'], rule)
                for profile in profiles:
                    self._setting(c, 'profile:'+profile['id'], profile)
                self._finish_draft(c,draft_id,draft_version)
                self._audit(c, {'action': 'import', 'batches': batches, 'after': rows,'coverage_confirmed':coverage_confirmed})
        except Exception as e:
            # Never conceal connection/credential errors behind a duplicate message.
            if isinstance(e, sqlite3.IntegrityError) or getattr(e, 'sqlstate', None) == '23505':
                raise ConflictError('이미 저장한 파일 또는 거래입니다. 전체 작업을 취소했습니다.') from e
            raise

    def edit_rows(self, rows, rules=(), coverage_confirmed=False, draft_id=None, draft_version=None):
        validate_rows(rows)
        with self.connection(write=True) as c:
            if self.pg:
                self.execute(c, 'SELECT pg_advisory_xact_lock(716294018)')
            self._assert_open(c,[r['month'] for r in rows])
            self._checkpoint(c,'정산 내역 수정 전')
            current_rows=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_transactions').fetchall()]
            self._check_coverage(c,rows,current_rows,coverage_confirmed)
            before = []
            for r in rows:
                old = self.execute(c, 'SELECT payload FROM settlement_transactions WHERE id=?', (r['id'],)).fetchone()
                if not old:
                    raise ConflictError('거래를 찾을 수 없습니다')
                previous = json.loads(old[0])
                # Edits are classification only; identity, dates and amounts remain source-backed.
                value = dict(previous)
                value.update({k: r[k] for k in ['category','treatment','include','reviewed']})
                value['card_account']=r.get('card_account','')
                if 'duplicate_resolution' in r:
                    value['duplicate_resolution']=r['duplicate_resolution']
                value['version'] = r['version'] + 1
                cursor = self.execute(c, 'UPDATE settlement_transactions SET payload=?,version=? WHERE id=? AND version=?',
                                      (json.dumps(value, ensure_ascii=False), value['version'], r['id'], r['version']))
                if cursor.rowcount != 1:
                    raise ConflictError('다른 창에서 수정했습니다. 새로 불러온 뒤 다시 저장하세요.')
                before.append(previous)
            for rule in rules:
                self._setting(c, 'rule:'+rule['id'], rule)
            self._finish_draft(c,draft_id,draft_version)
            self._audit(c, {'action': 'edit', 'before': before, 'after': rows,'coverage_confirmed':coverage_confirmed})

    def batch_ids(self):
        with self.connection() as c:
            return {r[0] for r in self.execute(c, 'SELECT id FROM settlement_batches').fetchall()}

    def batches(self):
        with self.connection() as c:
            return [json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_batches').fetchall()]

    def _check_coverage(self,c,rows,existing,confirmed):
        settings=[(k,json.loads(p)) for k,p in self.execute(c,'SELECT id,payload FROM settlement_settings').fetchall()]
        cards=[v for k,v in settings if k.startswith('card:')]
        exemptions=[v for k,v in settings if k.startswith('no_billing:')]
        checks=coverage_checks(rows,existing,cards,no_billing=exemptions)
        if any(check['blocking'] for check in checks):
            raise ValueError('카드 청구자료 누락 또는 카드대금 연결을 먼저 해결하세요.')
        relevant=bool(checks) or any(r['source']=='카드' for r in rows)
        if relevant and not confirmed:
            raise ValueError('카드별 청구월·전체 건수·합계의 완전성을 확인하세요.')

    def export(self):
        # One consistent database snapshot, including import profiles, rules and edit history.
        with self.connection() as c:
            if self.pg:
                self.execute(c, 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            else:
                c.execute('BEGIN')
            result=self._snapshot(c)
            result['backups']=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_backups').fetchall()]
            return json.dumps(result, ensure_ascii=False, indent=2).encode()

def restore_to_new_sqlite(backup, target):
    """Offline restore only, to a NEW local database; never overwrite a live DB."""
    import os
    import tempfile
    target = Path(target)
    if target.exists():
        raise ValueError('복원 대상이 이미 존재합니다. 새 경로를 지정하세요.')
    data = json.loads(backup)
    if data.get('schema_version') not in (1,2):
        raise ValueError('지원하지 않는 백업 버전')
    if data['transactions']:
        validate_rows(data['transactions'])
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        temp_path = Path(tmp)/'restored.sqlite3'
        restored = Store('sqlite:///'+str(temp_path))
        with restored.connection(write=True) as c:
            for batch in data['batches']:
                restored.execute(c,'INSERT INTO settlement_batches(id,payload) VALUES (?,?)',
                                 (batch['id'],json.dumps(batch,ensure_ascii=False)))
            known_batches={b['id'] for b in data['batches']}
            for row in data['transactions']:
                if row['batch_id'] not in known_batches:
                    raise ValueError('백업 거래의 원본 파일 연결 누락')
                restored.execute(c,'INSERT INTO settlement_transactions(id,month,version,payload) VALUES (?,?,?,?)',
                                 (row['id'],row['month'],row['version'],json.dumps(row,ensure_ascii=False)))
            for setting in data['settings']:
                restored._setting(c,setting['id'],setting['payload'])
            for name,keys in [('drafts',('id','version','updated_at')),('draft_versions',('id','draft_id','version')),('periods',('month',)),('backups',('id','created_at','kind'))]:
                for value in data.get(name,[]):
                    if name=='backups':
                        values=[value['id'],value['created_at'],'restored']
                        cols='id,created_at,kind'
                    else:
                        values=[value[k] for k in keys];cols=','.join(keys)
                    restored.execute(c,'INSERT INTO settlement_'+name+'('+cols+',payload) VALUES ('+','.join('?' for _ in range(len(values)+1))+')',(*values,json.dumps(value,ensure_ascii=False)))
            for event in data['audit']:
                restored.execute(c,'INSERT INTO settlement_audit(id,created_at,payload) VALUES (?,?,?)',
                                 (event['id'],event['created_at'],json.dumps(event['payload'],ensure_ascii=False)))
        # Fail if another process created the target meanwhile. No overwrite operation.
        os.link(temp_path,target)
    return Store('sqlite:///'+str(target))
