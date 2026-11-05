"""Read bank/card downloads in memory. No network, file writes or live DB calls."""
import hashlib
import io
import re
from collections import Counter
import pandas as pd
from .core import digest,money,transaction_date,installment_info,use_in_month,transaction_fingerprint,billing_month
from .layout import inspect_layout

def _s(value):
    return '' if pd.isna(value) else ' '.join(str(value).split())

def _dated(value):
    try:
        return transaction_date(value)
    except (ValueError,TypeError,OverflowError):
        return None

def _record(result,sheet,line,day,description,account,amount,**extra):
    source=result['source']
    bid=digest([result['hash'],source,result['alias'],sheet,result['month'],'usage-v1'])
    row=dict(id=digest([bid,line]),batch_id=bid,date=transaction_date(day),month=transaction_date(day)[:7],
             description=_s(description),source=source,account=account,inflow=max(-amount,0),outflow=max(amount,0),
             external_id='',row_number=line,category='미분류',treatment='사업',reviewed=False,include=True,
             reason='분류 필요',duplicate=False,version=0,source_sheet=sheet)
    row.update(extra)
    if not row['description']:
        raise ValueError('거래내역 누락')
    if source=='카드':
        row=use_in_month(row,result['month'])
    else:
        row['fingerprint']=transaction_fingerprint(row)
    result['rows'].append(row)
    return row

def _base(data,name,month,source,label,alias):
    return dict(source=source,format=label,alias=alias or label,month=billing_month(month),
                hash=hashlib.sha256(data).hexdigest(),filename=name,rows=[],batches=[],notes=[],errors=[],controls=[])

def _finish(result,counts):
    for sheet,(source_rows,ignored) in counts.items():
        rows=[r for r in result['rows'] if r['source_sheet']==sheet]
        if not rows:
            continue
        result['batches'].append(dict(id=rows[0]['batch_id'],filename=result['filename'],source=result['source'],
            account=result['alias'],hash=result['hash'],sheet=sheet,source_rows=source_rows,
            blank_rows=result.get('blank_counts',{}).get(sheet,0),error_count=sum(e['sheet']==sheet and not e.get('validation') for e in result['errors']),
            ignored_rows=ignored,card_kind='사용월 자료' if result['source']=='카드' else '은행 거래',
            format=result['format'],settlement_month=result['month'],reconciliation=result.get('reconciliation','대조 정보 없음')))
    return result

def _fail(result,sheet,line,error,validation=False):
    result['errors'].append(dict(sheet=sheet,row=line,error=str(error),validation=validation))

def read_original(data,filename,month,alias=''):
    """Return None for an unknown format, otherwise a fully checked import result.

    All strings used for recognition are public column labels, never user records.
    """
    stripped=data.lstrip()
    if stripped[:100].lower().startswith((b'<!doctype html',b'<html')):
        from lxml import html
        text=None
        for encoding in ('utf-8-sig','cp949'):
            try:
                text=data.decode(encoding);break
            except UnicodeError:
                continue
        if text is None:
            raise ValueError('카드 웹 문서 인코딩을 읽을 수 없습니다.')
        doc=html.fromstring(text,parser=html.HTMLParser(no_network=True))
        for table in doc.xpath('//table'):
            records=[[' '.join(' '.join(c.itertext()).split()) for c in tr.xpath('./td|./th')]
                     for tr in table.xpath('.//tr')]
            header=next((i for i,r in enumerate(records) if {'이용일','이용가맹점','결제원금','할부/회차'}<=set(r)),None)
            if header is not None:
                return _hyundai(data,filename,month,alias,records,header)
        return None
    if data[:4]==b'PK\x03\x04':
        with pd.ExcelFile(io.BytesIO(data)) as book:
            if {'청구요약','일시불','할부','해외이용'}<=set(book.sheet_names):
                sheets={s:pd.read_excel(book,sheet_name=s,header=None,dtype=object).values.tolist()
                        for s in ['청구요약','일시불','할부','해외이용']}
                return _samsung(data,filename,month,alias,sheets)
            recognized=[]
            for sheet in book.sheet_names:
                records=pd.read_excel(book,sheet_name=sheet,header=None,dtype=object).values.tolist()
                header=next((i for i,r in enumerate(records) if {'이용일','이용가맹점','이용금액','결제원금','할부/회차'}<=set(_s(v) for v in r)),None)
                if header is not None:
                    recognized.append(_hyundai(data,filename,month,alias,records,header,sheet))
            if recognized:
                result=recognized[0]
                for other in recognized[1:]:
                    for field in ('rows','batches','notes','errors','controls'):result[field].extend(other[field])
                return result
        return None
    if data[:8]==bytes.fromhex('d0cf11e0a1b11ae1'):
        with pd.ExcelFile(io.BytesIO(data),engine='xlrd') as book:
            if len(book.sheet_names)!=1:
                return None
            sheet=book.sheet_names[0]
            raw=pd.read_excel(book,sheet_name=sheet,header=None,dtype=object)
        # The first real row must look like the known headerless bank export.
        # Structural footer rows do not stop recognition; malformed later rows
        # produce explicit errors and cannot disappear from the import.
        if raw.shape[1]!=12 or raw.empty or not _dated(raw.iloc[0,0]):
            return None
        result=_base(data,filename,month,'은행','은행 원본(제목 없음)',alias or '사업통장')
        mapping={'date':0,'description':4,'outflow':1,'inflow':2}
        layout=inspect_layout(raw,mapping,transaction_date,money)
        previous=None;checks=0
        for pos,values in enumerate(raw.values.tolist()):
            if layout['kinds'][pos]!='transaction':continue
            line=pos+1
            try:
                transaction_date(values[0])
                outgoing,incoming,balance=money(values[1]),money(values[2]),money(values[3])
                if outgoing<0 or incoming<0 or (outgoing and incoming):
                    raise ValueError('은행 입출금 열을 확인하세요.')
                if previous is not None:
                    if previous[2]-balance!=previous[1]-previous[0]:
                        raise ValueError('제목 없는 은행 자료의 인접 잔액 대조가 맞지 않습니다.')
                    checks+=1
                previous=(outgoing,incoming,balance)
                _record(result,sheet,line,values[0],values[4],result['alias'],outgoing-incoming,
                        balance=balance,original_inflow=incoming,original_outflow=outgoing)
            except (ValueError,TypeError) as error:
                _fail(result,sheet,line,error)
        result['controls'].append(dict(check='은행 인접 잔액 대조',pairs=checks,passed=not result['errors']))
        for check in layout['controls']:
            control=dict(check,sheet=sheet,row=check['row']+1);result['controls'].append(control)
            if not check['passed']:_fail(result,sheet,control['row'],check['check']+' 불일치 또는 금액 오류',validation=True)
        result['blank_counts']={sheet:layout['blank_rows']};result['reconciliation']=layout['status']
        result['notes'].append('제목 행 없이 첫 거래부터 읽고 잔액 변동을 검사했습니다.')
        return _finish(result,{sheet:(len(raw),layout['ignored_rows'])})
    return None

def _hyundai(data,name,month,alias,records,header,sheet='결제 상세내역'):
    result=_base(data,name,month,'카드','현대카드',alias)
    titles=' '.join(' '.join(_s(v) for v in r) for r in records[:header])
    match=re.search(r'(\d{4})년\s*(\d{1,2})월\s*이용대금명세서',titles)
    due_month=f'{match[1]}-{int(match[2]):02}' if match else ''
    columns=[_s(v) for v in records[header]]
    if len(set(columns))!=len(columns):
        raise ValueError('현대카드 열 제목이 중복되어 있습니다.')
    width=len(columns)
    if any(len(r)>width and any(_s(v) for v in r[width:]) for r in records[header+1:]):
        raise ValueError('현대카드 제목 밖의 열에 데이터가 있습니다.')
    values=[r[:width]+['']*max(0,width-len(r)) for r in records[header+1:]]
    frame=pd.DataFrame(values,columns=columns)
    mapping={'date':'이용일','description':'이용가맹점','purchase_amount':'이용금액','billing_amount':'결제원금'}
    if '수수료(이자)' in columns:mapping['fee']='수수료(이자)'
    layout=inspect_layout(frame,mapping,transaction_date,money)
    for pos,(_,r) in enumerate(frame.iterrows()):
        if layout['kinds'][pos]!='transaction':continue
        line=header+pos+2
        try:
            installment=_s(r['할부/회차'])
            number=total=None
            if installment not in ('','-','—','일시불','0','0.0'):
                parts=installment.split('/')
                if len(parts)!=2:raise ValueError('현대카드 할부/회차 형식 오류')
                # Hyundai uses total months/current round, e.g. 10/7.
                number,total=installment_info(parts[1],parts[0])
            if total and total>1 and _s(r['결제원금']) in ('','-','—'):
                raise ValueError('할부 해당 회차의 결제원금이 없습니다.')
            if not _s(r['이용금액']) or _s(r['이용금액']) in ('-','—'):
                raise ValueError('실제 이용금액이 없습니다.')
            purchase=money(r['이용금액']);principal=money(r['결제원금']);fee=money(r.get('수수료(이자)'))
            _record(result,sheet,line,r['이용일'],r['이용가맹점'],
                    result['alias']+' / '+_s(r.get('이용카드')),purchase,
                    purchase_amount=purchase,purchase_total=purchase,billing_amount=principal,
                    billing_month=due_month,fee=fee,fee_included=False,
                    installment_number=number,installment_total=total,original_card_label=_s(r.get('이용카드')))
        except (ValueError,TypeError,OverflowError) as error:
            _fail(result,sheet,line,error)
    for check in layout['controls']:
        control=dict(check,sheet=sheet,row=check['row']+header+2)
        result['controls'].append(control)
        if not check['passed']:_fail(result,sheet,control['row'],check['check']+' 불일치 또는 금액 오류',validation=True)
    result['reconciliation']=layout['status']
    result['blank_counts']={sheet:layout['blank_rows']}
    result['notes'].append('원본 합계 대조: '+layout['status'])
    result['notes'].append(f'일시불은 실제 사용일, 할부 회차는 선택한 {month} 정산에 반영합니다. 납부월은 참고 정보입니다.')
    if any('일부결제금액이월약정' in _s(c) for r in records for c in r):
        result['notes'].append('납부 원금이 0원이어도 일시불 실제 사용액은 반영하며, 이월 원금을 다시 더하지 않습니다.')
    return _finish(result,{sheet:(len(frame),layout['ignored_rows'])})

def _table(records,required):
    header=next((i for i,r in enumerate(records) if required<=set(_s(x) for x in r)),None)
    if header is None:
        raise ValueError('카드 원본의 필수 열이 없습니다: '+', '.join(sorted(required)))
    columns=[_s(x) for x in records[header]]
    return [(i+1,dict(zip(columns,r))) for i,r in enumerate(records) if i>header],header

def _samsung(data,name,month,alias,sheets):
    result=_base(data,name,month,'카드','삼성카드',alias)
    counts={};principal_sum=fee_sum=0;identities=Counter()
    for sheet in ['일시불','할부']:
        records,header=_table(sheets[sheet],{'이용일','가맹점','이용금액','원금','이자/수수료','개월','회차'})
        layout=inspect_layout(pd.DataFrame([r for _,r in records]),{'date':'이용일','description':'가맹점','purchase_amount':'이용금액','billing_amount':'원금','fee':'이자/수수료'},transaction_date,money)
        ignored=layout['ignored_rows']
        result.setdefault('blank_counts',{})[sheet]=layout['blank_rows']
        for pos,(line,r) in enumerate(records):
            if layout['kinds'][pos]!='transaction':continue
            try:
                if not _dated(r['이용일']):
                    raise ValueError('삼성카드 거래일을 해석할 수 없습니다.')
                purchase=money(r['이용금액']);principal=money(r['원금']);fee=money(r['이자/수수료'])
                n,t=installment_info(r['회차'],r['개월'])
                if sheet=='할부' and (not t or t<2):
                    raise ValueError('할부 내역의 전체 개월 수가 없습니다.')
                discount=money(r.get('혜택금액')) if _s(r.get('이용혜택'))=='청구할인' else 0
                card=_s(r.get('이용구분'))
                principal_sum+=principal;fee_sum+=fee
                row=_record(result,sheet,line,r['이용일'],r['가맹점'],result['alias']+' / '+card,purchase,
                    purchase_amount=purchase,purchase_total=purchase,billing_amount=principal,
                    billing_month='',fee=fee,fee_included=False,installment_number=n,installment_total=t,
                    discount_amount=discount,original_card_label=card)
                identities[(row['date'],_s(r['가맹점']),purchase)]+=1
            except (ValueError,TypeError) as error:
                _fail(result,sheet,line,error)
        counts[sheet]=(len(records),ignored)
    summary,_=_table(sheets['청구요약'],{'이용자','원금','이자/수수료'})
    control_principal=sum(money(r['원금']) for _,r in summary)
    control_fee=sum(money(r['이자/수수료']) for _,r in summary)
    passed=principal_sum==control_principal and fee_sum==control_fee
    result['controls'].append(dict(check='삼성 청구요약 원금·수수료 대조',passed=passed))
    result['reconciliation']='일치' if passed else '불일치 · 확인 필요'
    if not passed:
        _fail(result,'청구요약',0,'일시불+할부 원금·수수료가 청구요약과 맞지 않습니다.',validation=True)
    overseas,_=_table(sheets['해외이용'],{'이용일','가맹점','청구금액'})
    matched=0
    overseas_layout=inspect_layout(pd.DataFrame([r for _,r in overseas]),{'date':'이용일','description':'가맹점','amount':'청구금액'},transaction_date,money)
    for pos,(line,r) in enumerate(overseas):
        if overseas_layout['kinds'][pos]!='transaction':continue
        try:
            key=(transaction_date(r['이용일']),_s(r['가맹점']),money(r['청구금액']))
            if identities[key]<1:
                raise ValueError('해외이용 중 일시불/할부에서 대조되지 않은 거래가 있습니다.')
            identities[key]-=1;matched+=1
        except (ValueError,TypeError) as error:
            _fail(result,'해외이용',line,error)
    result['notes'].append(f'일시불·할부 시트를 함께 읽고 청구요약과 대조했습니다. 해외이용 {matched}건은 이미 포함된 상세 정보이므로 다시 더하지 않습니다.')
    result['notes'].append('파일 안에 납부월이 표시되지 않아 임의 추정하지 않습니다. 선택한 정산월과 실제 사용일을 기준으로 처리합니다.')
    return _finish(result,counts)
