"""Identify structural rows conservatively and compare comparable grand totals.

A dated transaction always wins over a summary-looking merchant name. Unknown
or malformed rows remain transactions so the caller reports an error, not loss.
"""
import re
import pandas as pd


def text(value):
    return '' if pd.isna(value) else str(value).strip()


def summary_label(value):
    value=re.sub(r'\s+', '', text(value)).rstrip(':：')
    pattern=r'(?:총|전체|총이용금액|거래|입금|출금|일시불|할부|해외매출|원금|결제원금|수수료|청구|청구금액|이용금액|일부결제금액이월약정)?(?:합계|소계)'
    return value if re.fullmatch(pattern,value) else ''


def inspect_layout(frame,mapping,date_parser,money_parser):
    columns=list(frame.columns)
    date_col=mapping.get('date');desc_col=mapping.get('description')
    numeric_fields=[k for k in ('inflow','outflow','amount','billing_amount','purchase_amount','purchase_total','fee') if mapping.get(k)]
    kinds=[];labels={}
    for pos,(_,row) in enumerate(frame.iterrows()):
        def cell(col):return row[col] if col in columns else None
        values=[text(v) for v in row]
        if not any(values):kinds.append('blank');continue
        day=text(cell(date_col))
        try:
            date_parser(cell(date_col));kinds.append('transaction');continue
        except (ValueError,TypeError,OverflowError):pass
        # A malformed, nonempty date cannot be ignored as a summary or notice.
        first=next((v for v in values if v),'')
        label=next((summary_label(v) for v in (cell(desc_col),cell(date_col),first) if summary_label(v)),'')
        if label and (day in ('','-','—') or summary_label(day)):
            kinds.append('summary');labels[pos]=label;continue
        if day==str(date_col) and text(cell(desc_col))==str(desc_col):
            kinds.append('header');continue
        amount_present=any(text(cell(mapping[k])) not in ('','-','—') for k in numeric_fields)
        note=any(re.match(r'^(?:※|안내(?:사항)?\s*[:：]|유의사항(?:\s|[:：])|고객센터\s*[:：]|문의\s*[:：])',v) for v in values if v)
        if note and not amount_present and (day in ('','-','—') or day==first):
            kinds.append('notice');continue
        kinds.append('transaction')
    grand=[p for p,l in labels.items() if l.startswith(('총','전체','거래')) and '소계' not in l]
    # A lone trailing '합계' is an overall total; multiple subtotals have no
    # reliably inferable scope and are excluded without claiming reconciliation.
    plain=[p for p,l in labels.items() if l=='합계']
    transactions=[i for i,k in enumerate(kinds) if k=='transaction']
    if len(plain)==1 and (not transactions or plain[0]>max(transactions)):grand+=plain
    controls=[]
    for pos in sorted(set(grand)):
        row=frame.iloc[pos];compared=set()
        for field in numeric_fields:
            col=mapping[field]
            if col in compared or text(row[col]) in ('','-','—'):continue
            compared.add(col)
            try:
                expected=money_parser(row[col])
                actual=sum(money_parser(frame.iloc[i][col]) for i in transactions)
                controls.append(dict(row=pos,check=str(col)+' 원본 합계 대조',expected=expected,actual=actual,difference=actual-expected,passed=actual==expected))
            except (ValueError,TypeError,OverflowError):
                controls.append(dict(row=pos,check=str(col)+' 원본 합계 대조',passed=False,error='거래 또는 합계의 금액을 읽을 수 없습니다.'))
    if any(not c['passed'] for c in controls):status='불일치 · 확인 필요'
    elif controls:status='일치'
    elif labels:status='부분 합계 또는 비교 금액 없음 · 전체 대조 안 함'
    else:status='원본 합계 없음'
    return dict(kinds=kinds,controls=controls,status=status,blank_rows=kinds.count('blank'),
                ignored_rows=sum(k in ('summary','header','notice') for k in kinds),
                summary_rows=kinds.count('summary'),notice_rows=sum(k in ('header','notice') for k in kinds))
