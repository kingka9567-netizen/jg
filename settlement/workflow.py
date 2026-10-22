"""Durable drafts, atomic recovery points, and explicitly closed monthly reports."""
import base64
import json
import zlib
from datetime import datetime, timezone
from .core import digest, validate_rows, metrics, billing_month


class ClosedPeriodError(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def row_digest(rows):
    return digest(sorted(rows,key=lambda r:r['id']))


class Workflow:
    def _write_lock(self,c):
        if self.pg:
            self.execute(c,'SELECT pg_advisory_xact_lock(716294018)')

    def _period(self,c,month):
        found=self.execute(c,'SELECT payload FROM settlement_periods WHERE month=?',(month,)).fetchone()
        return json.loads(found[0]) if found else dict(month=month,status='review',version=0,revision=0)

    def period(self,month):
        with self.connection() as c:
            return self._period(c,billing_month(month))

    def _assert_open(self,c,months):
        for month in sorted(set(months)):
            if self._period(c,month)['status']=='finalized':
                raise ClosedPeriodError(f'{month} 정산은 확정되어 있습니다. 수정 사유를 기록하고 확정을 해제하세요.')

    def _snapshot(self,c):
        data=dict(schema_version=2,exported_at=stamp())
        for name in ('batches','transactions','drafts','draft_versions','periods'):
            data[name]=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_'+name).fetchall()]
        data['settings']=[dict(id=k,payload=json.loads(p)) for k,p in self.execute(c,'SELECT id,payload FROM settlement_settings').fetchall()]
        data['audit']=[dict(id=k,created_at=t,payload=json.loads(p)) for k,t,p in self.execute(c,'SELECT id,created_at,payload FROM settlement_audit').fetchall()]
        return data

    def _checkpoint(self,c,reason,daily=False):
        now=stamp()
        if daily and self.execute(c,'SELECT id FROM settlement_backups WHERE kind=?',('daily:'+now[:10],)).fetchone():
            return
        data=json.dumps(self._snapshot(c),ensure_ascii=False,sort_keys=True).encode()
        value=dict(id=digest([now,reason,digest(data.hex())]),created_at=now,reason=reason,
                   codec='zlib-base64',sha256=__import__('hashlib').sha256(data).hexdigest(),
                   data=base64.b64encode(zlib.compress(data)).decode())
        self.execute(c,'INSERT INTO settlement_backups(id,created_at,kind,payload) VALUES (?,?,?,?)',
                     (value['id'],now,'daily:'+now[:10] if daily else 'change',json.dumps(value,ensure_ascii=False)))
        return value['id']

    def backups(self):
        with self.connection() as c:
            return [{k:v for k,v in json.loads(p).items() if k!='data'} for (p,) in self.execute(c,'SELECT payload FROM settlement_backups ORDER BY created_at DESC').fetchall()]

    def backup_bytes(self,ident):
        with self.connection() as c:
            found=self.execute(c,'SELECT payload FROM settlement_backups WHERE id=?',(ident,)).fetchone()
        if not found:
            raise ValueError('복구본을 찾을 수 없습니다.')
        value=json.loads(found[0]);data=zlib.decompress(base64.b64decode(value['data']))
        if __import__('hashlib').sha256(data).hexdigest()!=value['sha256']:
            raise ValueError('복구본 무결성 검사 실패')
        return data

    def checkpoint(self,reason='수동 복구본'):
        with self.connection(write=True) as c:
            self._write_lock(c)
            return self._checkpoint(c,reason)

    def draft(self,ident):
        with self.connection() as c:
            found=self.execute(c,'SELECT payload FROM settlement_drafts WHERE id=?',(ident,)).fetchone()
            return json.loads(found[0]) if found else None

    def drafts(self,active_only=True):
        with self.connection() as c:
            values=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_drafts ORDER BY updated_at DESC').fetchall()]
            return [d for d in values if not active_only or d['status']=='active']

    def save_draft(self,ident,rows,context,expected_version=0):
        if not rows or len({r['id'] for r in rows})!=len(rows):
            raise ValueError('임시 저장할 거래 또는 거래 ID가 잘못되었습니다.')
        # Pending classification is deliberately allowed; provenance is still required.
        known={b['id'] for b in context.get('batches',[])}
        if known and any(r['batch_id'] not in known for r in rows):
            raise ValueError('임시 저장 원본 파일 연결 오류')
        for r in rows:
            billing_month(r['month'])
        payload=json.loads(json.dumps(dict(rows=rows,context=context),ensure_ascii=False,allow_nan=False))
        with self.connection(write=True) as c:
            self._write_lock(c)
            self._assert_open(c,[r['month'] for r in rows])
            found=self.execute(c,'SELECT payload FROM settlement_drafts WHERE id=?',(ident,)).fetchone()
            old=json.loads(found[0]) if found else None
            if old and old['status']=='saved':
                raise ValueError('이미 저장 완료하거나 보관 종료한 작업입니다.')
            if (old['version'] if old else 0)!=expected_version:
                from .store import ConflictError
                raise ConflictError('다른 창의 임시 저장이 있습니다. 최신 작업을 다시 불러오세요.')
            if old and old['content_hash']==digest(payload):
                return old
            version=expected_version+1;now=stamp()
            value=dict(id=ident,version=version,status='active',updated_at=now,content_hash=digest(payload),**payload)
            self.execute(c,'INSERT INTO settlement_drafts(id,version,updated_at,payload) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at,payload=excluded.payload',
                         (ident,version,now,json.dumps(value,ensure_ascii=False)))
            history=dict(id=digest([ident,version]),draft_id=ident,version=version,updated_at=now,
                         data=base64.b64encode(zlib.compress(json.dumps(value,ensure_ascii=False).encode())).decode())
            self.execute(c,'INSERT INTO settlement_draft_versions(id,draft_id,version,payload) VALUES (?,?,?,?)',
                         (history['id'],ident,version,json.dumps(history,ensure_ascii=False)))
            self._audit(c,dict(action='draft_save',draft_id=ident,version=version,row_count=len(rows),months=sorted({r['month'] for r in rows})))
            self._checkpoint(c,'일일 자동 복구본',daily=True)
            return value

    def draft_versions(self,ident):
        with self.connection() as c:
            return [{k:v for k,v in json.loads(p).items() if k!='data'} for (p,) in self.execute(c,'SELECT payload FROM settlement_draft_versions WHERE draft_id=? ORDER BY version DESC',(ident,)).fetchall()]

    def restore_draft_version(self,ident,version,expected_version):
        with self.connection() as c:
            found=self.execute(c,'SELECT payload FROM settlement_draft_versions WHERE draft_id=? AND version=?',(ident,version)).fetchone()
        if not found:
            raise ValueError('이전 작업을 찾을 수 없습니다.')
        old=json.loads(zlib.decompress(base64.b64decode(json.loads(found[0])['data'])))
        self.checkpoint('임시 저장 복원 전')
        result=self.save_draft(ident,old['rows'],old['context'],expected_version)
        return result

    def _finish_draft(self,c,ident,version,status='saved',reason='정산 저장 완료'):
        if not ident:
            return
        found=self.execute(c,'SELECT payload FROM settlement_drafts WHERE id=?',(ident,)).fetchone()
        if not found:
            raise ValueError('임시 저장을 찾을 수 없습니다.')
        value=json.loads(found[0])
        if value['version']!=version or value['status']!='active':
            from .store import ConflictError
            raise ConflictError('임시 저장이 변경되었습니다. 최신 작업을 불러오세요.')
        value.update(status=status,updated_at=stamp(),completion_reason=reason)
        self.execute(c,'UPDATE settlement_drafts SET payload=?,updated_at=? WHERE id=?',(json.dumps(value,ensure_ascii=False),value['updated_at'],ident))

    def archive_draft(self,ident,version,reason):
        if not reason.strip():
            raise ValueError('보관 종료 사유를 입력하세요.')
        with self.connection(write=True) as c:
            self._write_lock(c);self._checkpoint(c,'작업 보관 종료 전')
            self._finish_draft(c,ident,version,'archived',reason.strip())
            self._audit(c,dict(action='draft_archive',draft_id=ident,reason=reason.strip()))

    def month_history(self,month):
        with self.connection() as c:
            events=[dict(id=i,created_at=t,**json.loads(p)) for i,t,p in self.execute(c,'SELECT id,created_at,payload FROM settlement_audit ORDER BY created_at DESC').fetchall()]
        return [e for e in events if e.get('month')==month or any(r.get('month')==month for r in e.get('after',[])) or month in e.get('months',[])]

    def finalize_month(self,month,expected_digest,coverage_confirmed=False,upload_confirmed=False):
        month=billing_month(month)
        with self.connection(write=True) as c:
            self._write_lock(c);self._assert_open(c,[month])
            all_rows=[json.loads(p) for (p,) in self.execute(c,'SELECT payload FROM settlement_transactions').fetchall()]
            rows=[r for r in all_rows if r['month']==month]
            if row_digest(rows)!=expected_digest:
                from .store import ConflictError
                raise ConflictError('검토 후 거래가 변경되었습니다. 최신 합계로 다시 확인하세요.')
            validate_rows(rows)
            if not upload_confirmed:
                raise ValueError('월 전체 원본 파일·기간·건수·합계를 확인하세요.')
            self._check_coverage(c,rows,all_rows,coverage_confirmed)
            relevant={r['batch_id'] for r in rows}
            for (p,) in self.execute(c,'SELECT payload FROM settlement_batches').fetchall():
                batch=json.loads(p)
                if batch['id'] not in relevant:continue
                if batch.get('error_count',0):raise ValueError('오류 행이 남아 있는 원본 파일은 확정할 수 없습니다.')
                if 'source_rows' in batch:
                    count=sum(r['batch_id']==batch['id'] for r in all_rows)
                    if count+batch.get('blank_rows',0)+batch.get('ignored_rows',0)!=batch['source_rows']:
                        raise ValueError('원본 행 수와 저장 거래 수를 다시 대조하세요.')
            for (p,) in self.execute(c,'SELECT payload FROM settlement_drafts').fetchall():
                d=json.loads(p)
                if d['status']=='active' and any(r['month']==month for r in d['rows']):
                    raise ValueError('같은 월의 임시 작업을 먼저 정산 저장하거나 보관 종료하세요.')
            for r in rows:
                if r.get('duplicate') and r['include'] and not r.get('duplicate_resolution','').startswith('실제'):
                    raise ValueError('포함한 중복 후보의 비교 결과를 기록하세요.')
            self._checkpoint(c,'월정산 확정 전')
            previous=self._period(c,month)
            period=dict(month=month,status='finalized',version=previous['version']+1,revision=previous['revision']+1,
                        finalized_at=stamp(),digest=row_digest(rows),metrics=metrics(rows),coverage_confirmed=coverage_confirmed,upload_confirmed=True)
            self.execute(c,'INSERT INTO settlement_periods(month,payload) VALUES (?,?) ON CONFLICT(month) DO UPDATE SET payload=excluded.payload',(month,json.dumps(period,ensure_ascii=False)))
            self._audit(c,dict(action='finalize',month=month,period=period,after=rows))
            self._checkpoint(c,'월정산 확정 완료')
            return period

    def reopen_month(self,month,reason,expected_version):
        if not reason.strip():
            raise ValueError('확정 해제 사유를 입력하세요.')
        with self.connection(write=True) as c:
            self._write_lock(c);old=self._period(c,billing_month(month))
            if old['status']!='finalized' or old['version']!=expected_version:
                from .store import ConflictError
                raise ConflictError('월정산 상태가 변경되었습니다. 새로 불러오세요.')
            self._checkpoint(c,'확정 해제 전')
            period=dict(old,status='review',version=old['version']+1,reopened_at=stamp(),reopen_reason=reason.strip())
            self.execute(c,'UPDATE settlement_periods SET payload=? WHERE month=?',(json.dumps(period,ensure_ascii=False),month))
            self._audit(c,dict(action='reopen',month=month,reason=reason.strip(),previous=old))
            return period
