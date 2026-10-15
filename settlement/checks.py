"""Review evidence for uploaded files, card coverage and duplicate decisions."""
from datetime import date
from collections import defaultdict

def upload_summary(batches, rows):
    result=[]
    grouped=defaultdict(dict)
    for row in rows:
        grouped[row['batch_id']][row['id']]=row
    for batch in batches:
        items=list(grouped[batch['id']].values())
        dates=sorted({r['date'] for r in items})
        gaps=[(date.fromisoformat(b)-date.fromisoformat(a)).days-1 for a,b in zip(dates,dates[1:])]
        result.append({
            '파일':batch.get('filename','알 수 없는 파일'),'자료':batch.get('source',items[0]['source'] if items else ''),
            '계좌/카드':batch.get('account',items[0]['account'] if items else ''),
            '파일 종류':batch.get('card_kind','은행 거래'),
            '처리 상태':batch.get('status','검토 대상'),
            '원본 데이터 행':batch.get('source_rows',len(items)), '정상 거래':len(items),
            '빈 행':batch.get('blank_rows',0),'오류 행':batch.get('error_count',0),
            '구매/거래 기간':f'{dates[0]} ~ {dates[-1]}' if dates else '없음',
            '정산월':', '.join(sorted({r['month'] for r in items})),
            '입금/환불 합계':sum(r['inflow'] for r in items),
            '출금/당월 청구 합계':sum(r['outflow'] for r in items),
            '중복 후보':sum(bool(r.get('duplicate')) for r in items),
            '최장 거래 없는 일수':max(gaps,default=0),
        })
    return result

def coverage_checks(rows, existing=(), cards=(), months=(), no_billing=()):
    """Any statement is presence evidence, not proof of completeness or reconciliation."""
    combined={r['id']:r for r in existing}
    combined.update({r['id']:r for r in rows})
    all_rows=list(combined.values())
    months=set(months) or {r['month'] for r in rows}
    exempt={(e['month'],e['account']) for e in no_billing if e.get('confirmed') and e.get('reason','').strip()}
    active=[c for c in cards if c.get('enabled',True)]
    evidence={(r['month'],r['account']) for r in all_rows if r['source']=='카드' and r.get('settlement_basis')=='청구월'}
    checks=[]
    def add(month,account,status,message,blocking=False):
        checks.append(dict(month=month,account=account,status=status,message=message,blocking=blocking))
    mixed={(r['month'],r['account']) for r in all_rows if r['source']=='카드' and r['include']
           and r.get('settlement_basis')!='청구월' and r['month'] in months and (r['month'],r['account']) in evidence}
    for month,account in sorted(mixed):
        add(month,account,'구버전 자료 중복 위험','같은 카드·월에 거래일 기준 자료와 청구월 기준 자료가 함께 있습니다. 이전 자료를 검토해 제외한 후 저장하세요.',True)
    needed={(month,c['account']) for month in months for c in active
            if not c.get('start_month') or month>=c['start_month']}
    for r in all_rows:
        if r['month'] not in months or r['source']!='은행' or r['treatment']!='카드대금 납부' or not r['include']:
            continue
        account=r.get('card_account','')
        linked=[c['account'] for c in active if c.get('bank_account')==r['account']]
        if not account and len(linked)==1:
            account=linked[0]
        if not account:
            add(r['month'],'미지정','연결 필요',f"{r['account']} 카드대금의 대상 카드를 지정하세요. 거래: {r['description']}",True)
        else:
            needed.add((r['month'],account))
    for month,account in sorted(needed):
        if (month,account) in evidence:
            add(month,account,'청구자료 있음','청구내역을 찾았습니다. 파일 전체 행·합계와 청구기간의 완전성은 별도 확인이 필요합니다.')
        elif (month,account) in exempt:
            add(month,account,'청구 없음 확인','사용자가 사유와 함께 이번 달 청구 없음으로 확인했습니다.')
        else:
            add(month,account,'청구자료 누락','월별 청구내역을 올리거나 실제 청구가 없는 달이면 사유를 기록하세요.',True)
    return checks

def duplicate_comparisons(rows, existing=(), batches=()):
    names={b['id']:b.get('filename','알 수 없는 파일') for b in batches}
    grouped=defaultdict(dict)
    for r in list(existing)+list(rows):
        grouped[r['fingerprint']][r['id']]=r
    result=[]
    for row in rows:
        if not row.get('duplicate'):
            continue
        matches=[r for ident,r in grouped[row['fingerprint']].items() if ident!=row['id']]
        def detail(r):
            return {'거래 ID':r['id'],'파일':names.get(r['batch_id'],'기존 저장 자료'),
                    '원본 행':r.get('row_number'),'구매/거래일':r['date'],'정산월':r['month'],
                    '계좌/카드':r['account'],'내역':r['description'],
                    '당월 출금/청구':r['outflow'],'입금/환불':r['inflow'],
                    '회차':r.get('installment_number'),'총회차':r.get('installment_total'),
                    '카테고리':r['category'],'포함':r['include']}
        result.append({'id':row['id'],'new':detail(row),'matches':[detail(r) for r in matches]})
    return result
