"""Read-only Google Sheets backup; no create/update/clear/delete operations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime,timezone

def main():
    import gspread
    from google.oauth2.service_account import Credentials
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,required=True,help='기존 서비스 계정 JSON 경로. 내용을 출력하지 않습니다.')
    parser.add_argument('--sheet-id',required=True,help='settlement_db 문서 URL의 ID')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    # readonly scope plus only get calls; names, headers, blank rows retained as strings.
    credentials=Credentials.from_service_account_file(str(args.credentials),scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    book=gspread.authorize(credentials).open_by_key(args.sheet_id)
    data={'format':'legacy-sheet-snapshot-v1','exported_at':datetime.now(timezone.utc).isoformat(),
          'spreadsheet_id':args.sheet_id,'worksheets':[]}
    for ws in book.worksheets():
        data['worksheets'].append({'title':ws.title,'id':ws.id,'values':ws.get_all_values()})
    payload=json.dumps(data,ensure_ascii=False,indent=2).encode()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create; refuse to replace an earlier backup.
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f: f.write(payload)
    print(f'백업 완료: {len(data["worksheets"])}개 시트. SHA256 {hashlib.sha256(payload).hexdigest()}')
    print('원본 정산 입력을 잠시 멈춘 상태에서 백업하고, 건수·합계를 원본과 대조하세요.')

if __name__=='__main__': main()
