"""Copy an approved snapshot into an EMPTY target only. Never overwrite a DB."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from settlement.store import Store
from settlement.core import validate_rows


def copy_to_empty_store(store,data,source_sha):
    if data.get('schema_version')!=2:raise ValueError('지원하는 이관 형식이 아닙니다.')
    if data.get('transactions'):validate_rows(data['transactions'])
    counts={name:len(data.get(name,[])) for name in ('transactions','drafts','settings')}
    with store.connection(write=True) as c:
        store._write_lock(c)
        for name in ('batches','transactions','drafts','draft_versions','settings','periods','audit','backups'):
            if store.execute(c,'SELECT COUNT(*) FROM settlement_'+name).fetchone()[0]:
                raise ValueError('대상 DB가 비어 있지 않아 이관을 중단했습니다.')
        for b in data.get('batches',[]):
            store.execute(c,'INSERT INTO settlement_batches(id,payload) VALUES (?,?)',(b['id'],json.dumps(b,ensure_ascii=False)))
        for r in data.get('transactions',[]):
            store.execute(c,'INSERT INTO settlement_transactions(id,month,version,payload) VALUES (?,?,?,?)',(r['id'],r['month'],r['version'],json.dumps(r,ensure_ascii=False)))
        for name,keys in [('drafts',('id','version','updated_at')),('draft_versions',('id','draft_id','version')),('periods',('month',))]:
            for value in data.get(name,[]):
                values=[value[k] for k in keys]
                store.execute(c,'INSERT INTO settlement_'+name+'('+','.join(keys)+',payload) VALUES ('+','.join('?' for _ in range(len(keys)+1))+')',(*values,json.dumps(value,ensure_ascii=False)))
        for setting in data.get('settings',[]):store._setting(c,setting['id'],setting['payload'])
        for event in data.get('audit',[]):
            store.execute(c,'INSERT INTO settlement_audit(id,created_at,payload) VALUES (?,?,?)',(event['id'],event['created_at'],json.dumps(event['payload'],ensure_ascii=False)))
        store._audit(c,dict(action='approved_empty_migration',source_sha256=source_sha,counts=counts))
        for name in ('transactions','drafts','settings'):
            actual=store.execute(c,'SELECT COUNT(*) FROM settlement_'+name).fetchone()[0]
            if actual!=counts[name]:raise ValueError('이관 건수 검증 실패')
        store._checkpoint(c,'운영 전환 직후 자동 복구본')
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--connection-file',type=Path,required=True)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--approved',action='store_true',help='실제 자료 복사에 대한 사용자 확인 후에만 지정')
    args=parser.parse_args();payload=args.snapshot.read_bytes();sha=hashlib.sha256(payload).hexdigest()
    if sha!=args.expected_sha256:raise ValueError('검토한 백업과 파일이 다릅니다.')
    data=json.loads(payload)
    if not args.approved:
        print('검증만 수행했습니다. 실제 자료 복사는 사용자 확인 후 --approved로 실행합니다.');return
    url=args.connection_file.read_text().strip()
    if not url.startswith(('postgresql://','postgres://')) or 'sslmode=require' not in url and 'sslmode=verify-full' not in url:
        raise ValueError('TLS PostgreSQL 연결이 필요합니다.')
    store=Store(url)
    print('검증한 이관 건수:',copy_to_empty_store(store,data,sha))
    if store._pool:store._pool.close()

if __name__=='__main__':main()
