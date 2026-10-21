"""Pure settlement logic. No network calls and no AI transmission."""
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
import pandas as pd

CATEGORIES = ['미분류', '판매대금 입금', '사입', '부자재', '세금', '택배비', '광고비',
              '식비', '구독/통신', '생활 및 기타', '기타']
TREATMENTS = ['사업', '카드대금 납부', '계좌이체', '대출', '개인/제외']
ALIASES = {
    'date': ['거래일시', '거래일자', '거래일', '이용일자', '이용일', '승인일시', '승인일자', '날짜'],
    'description': ['적요', '거래내역', '내역', '가맹점명', '가맹점', '내용', '거래처명'],
    'outflow': ['출금액', '출금금액', '지출금액', '찾으신금액', '지급금액'],
    'inflow': ['입금액', '입금금액', '맡기신금액'],
    'amount': ['이용금액', '승인금액', '거래금액', '금액'],
    'external_id': ['승인번호', '거래번호'],
}
CARD_ALIASES = {
    'billing_month': ['청구월', '청구년월', '결제월', '결제예정일', '청구일자'],
    'billing_amount': ['이번달청구금액', '당월청구금액', '청구금액', '청구원금'],
    'purchase_amount': ['이용금액', '승인금액', '거래금액'],
    'purchase_total': ['총구매금액', '원거래금액', '전체승인금액'],
    'installment_number': ['할부회차', '청구회차', '현재회차'],
    'installment_total': ['할부개월수', '할부개월', '총회차'],
    'fee': ['당월할부수수료', '할부수수료', '수수료'],
}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()

def norm(value):
    return re.sub(r'\s+', '', str(value)).casefold()

def guess_columns(columns, card=False):
    return {field: next((str(c) for a in aliases for c in columns if norm(c) == norm(a)), '')
            for field, aliases in (dict(ALIASES, **CARD_ALIASES) if card else ALIASES).items()}

def guess_header(raw):
    scores = []
    for i in range(min(30, len(raw))):
        mapped = guess_columns(raw.iloc[i].dropna().tolist())
        scores.append((sum(bool(x) for x in mapped.values()), -i))
    return -max(scores)[1] if scores else 0

def money(value):
    if pd.isna(value) or str(value).strip() in ('', '-', '—'):
        return 0
    s = str(value).strip().replace(',', '').replace('원', '').replace('₩', '').strip()
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        n = Decimal(s)
        if not n.is_finite() or n != n.to_integral_value():
            raise ValueError('원 단위 정수가 아닙니다')
        if abs(n) > 10**15:
            raise ValueError('금액 범위 초과')
        return int(n)
    except InvalidOperation as e:
        raise ValueError('금액을 해석할 수 없습니다') from e

def transaction_date(value):
    if pd.isna(value):
        raise ValueError('거래일 누락')
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()[:10]
    s = str(int(value)) if isinstance(value,(int,float)) and float(value).is_integer() else str(value).strip()
    if re.fullmatch(r'\d{8}', s):
        return datetime.strptime(s, '%Y%m%d').date().isoformat()
    if isinstance(value, (int, float)) and 20000 <= value <= 90000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    if not re.match(r'^\d{4}[-./년]', s):
        raise ValueError('연도가 포함된 거래일이 필요합니다')
    s = s.replace('년', '-').replace('월', '-').replace('일', '')
    return pd.Timestamp(s).date().isoformat()

def billing_month(value):
    if pd.isna(value) or not str(value).strip():
        raise ValueError('청구월 누락')
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()[:7]
    s = str(int(value)) if isinstance(value, (int,float)) and float(value).is_integer() else str(value).strip()
    match = re.fullmatch(r'(\d{4})[-./년]?[ ]?(\d{1,2})월?', s)
    if match:
        return date(int(match[1]), int(match[2]), 1).isoformat()[:7]
    return transaction_date(value)[:7]

def installment_info(number, total):
    def count(v):
        if pd.isna(v) or str(v).strip() in ('', '-', '일시불', '0', '0.0'):
            return None
        s = str(v).strip()
        if isinstance(v,(int,float)) and float(v).is_integer():
            return int(v)
        m = re.fullmatch(r'(\d+)\s*(?:회차|회|개월)?', s)
        if not m:
            raise ValueError('할부 회차/개월을 해석할 수 없습니다')
        return int(m[1])
    if isinstance(number,str) and '/' in number:
        parts=number.split('/')
        if len(parts)!=2:
            raise ValueError('할부 회차 형식 오류')
        n,t=count(parts[0]),count(parts[1])
        explicit=count(total)
        if explicit is not None and explicit!=t:
            raise ValueError('할부 총회차가 서로 다릅니다')
    else:
        n,t=count(number),count(total)
    if (n is not None and n<1) or (t is not None and t<1) or (n is not None and t is not None and n>t):
        raise ValueError('할부 회차 범위를 확인하세요')
    return n,t

def parse_rows(frame, mapping, source, account, file_hash, sheet, header, amount_mode,
               card_kind='승인내역', statement_month='', fee_included=True):
    if source not in ('은행', '카드') or not account.strip():
        raise ValueError('은행/카드 종류와 계좌·카드 별칭이 필요합니다')
    is_bill=source=='카드' and card_kind=='청구내역'
    if source=='카드' and card_kind not in ('승인내역','청구내역'):
        raise ValueError('카드 자료 종류를 확인하세요')
    required = ['date', 'description'] + (['billing_amount'] if is_bill else (['amount'] if amount_mode != '입금/출금 별도' else []))
    if any(not mapping.get(k) for k in required):
        raise ValueError('거래일, 내역, 금액 열을 지정하세요')
    if not is_bill and amount_mode == '입금/출금 별도' and not (mapping.get('inflow') or mapping.get('outflow')):
        raise ValueError('입금 또는 출금 열이 필요합니다')
    selected = [v for v in mapping.values() if v]
    if len(selected) != len(set(selected)):
        raise ValueError('서로 다른 항목에 같은 열을 지정할 수 없습니다')
    rows, errors = [], []
    batch_id = digest([file_hash, source, account.strip(), sheet])
    for pos, (_, raw) in enumerate(frame.iterrows()):
        number = pos + header + 2
        def cell(k):
            return raw[mapping[k]] if mapping.get(k) else None
        # Ignore truly blank rows, never silently drop totals or malformed transactions.
        if all(pd.isna(cell(k)) for k in mapping if mapping.get(k)):
            continue
        try:
            d = transaction_date(cell('date'))
            desc = '' if pd.isna(cell('description')) else str(cell('description')).strip()
            if not desc:
                raise ValueError('내역 누락')
            if is_bill:
                charge=money(cell('billing_amount'))
                fee=money(cell('fee'))
                net_charge=charge if fee_included else charge+fee
                incoming,outgoing=max(-net_charge,0),max(net_charge,0)
            elif amount_mode == '입금/출금 별도':
                incoming, outgoing = money(cell('inflow')), money(cell('outflow'))
                if incoming < 0 or outgoing < 0:
                    raise ValueError('음수는 부호 있는 금액 방식으로 지정하세요')
            else:
                amount = money(cell('amount'))
                if amount_mode == '양수=출금 (음수=환불)':
                    incoming, outgoing = max(-amount, 0), max(amount, 0)
                else:
                    incoming, outgoing = max(amount, 0), max(-amount, 0)
            if incoming and outgoing:
                raise ValueError('입금과 출금이 동시에 존재합니다')
            if not incoming and not outgoing and not is_bill:
                raise ValueError('금액 0원: 합계/취소 행인지 확인하세요')
            ext = '' if pd.isna(cell('external_id')) else str(cell('external_id')).strip()
            row = dict(id=digest([batch_id, pos]), batch_id=batch_id, date=d,
                       month=d[:7], description=desc, source=source, account=account.strip(),
                       inflow=incoming, outflow=outgoing, external_id=ext, row_number=number,
                       category='미분류', treatment='사업', reviewed=False, include=True,
                       reason='분류 필요', duplicate=False, version=0)
            if source=='카드':
                n,t=installment_info(cell('installment_number'),cell('installment_total'))
                row.update(purchase_date=d, purchase_amount=money(cell('purchase_amount')) if mapping.get('purchase_amount') else None, card_kind=card_kind, installment_number=n,
                           installment_total=t, purchase_total=money(cell('purchase_total')) if mapping.get('purchase_total') else None,
                           settlement_basis='청구월' if is_bill else '승인내역(정산 제외)')
                if is_bill:
                    m=billing_month(cell('billing_month')) if mapping.get('billing_month') else billing_month(statement_month)
                    if m < d[:7]:
                        raise ValueError('청구월이 최초 구매월보다 빠릅니다')
                    row.update(month=m,billing_month=m,billing_amount=charge,fee=fee,fee_included=fee_included)
                    if net_charge==0:
                        row.update(include=False,reason='당월 청구 0원: 정산 제외·내용 확인')
                else:
                    row.update(include=False,reason='승인내역: 월별 청구금액이 없어 정산 제외')
            row['fingerprint'] = transaction_fingerprint(row)
            rows.append(row)
        except (ValueError, TypeError, OverflowError) as e:
            errors.append({'엑셀 행': number, '오류': str(e)})
    return rows, errors

def transaction_fingerprint(row):
    base=[row['source'],row['account'],row['date'],norm(row['description']),row['inflow'],row['outflow'],row.get('external_id','')]
    if row.get('settlement_basis')=='청구월':
        base += ['청구월',row['month'],row.get('installment_number'),row.get('installment_total')]
    elif row.get('settlement_basis')=='사용월' and row.get('installment_total',0) and row['installment_total']>1:
        base += ['할부',row['month'],row.get('installment_number'),row.get('installment_total')]
    return digest(base)

def use_in_month(original, month):
    """Assign a statement's installment round to the chosen operating month.

    One-time purchases use their actual usage month, independently of when due.
    Never use a zero revolving payment to erase actual card usage.
    """
    row=dict(original)
    if row['source']!='카드':
        return row
    month=billing_month(month)
    total=row.get('installment_total') or 0
    installment=total>1 or (row.get('installment_number') or 0)>1
    if installment:
        if row.get('billing_amount') is None:
            raise ValueError('할부는 해당 회차의 원금이 필요합니다. 전체 구매금액을 자동 분할하지 않습니다.')
        net=money(row['billing_amount'])+(0 if row.get('fee_included',True) else money(row.get('fee',0)))
        expense_basis='할부 회차 원금·수수료'
    else:
        amount=row.get('purchase_amount')
        if amount is None:
            amount=row.get('purchase_total')
        if amount is None and row.get('card_kind')=='청구내역':
            raise ValueError('일시불 실제 사용금액 열이 필요합니다. 납부 원금을 사용액으로 추정하지 않습니다.')
        if amount is None:
            amount=row['outflow']-row['inflow']
        net=money(amount)+money(row.get('discount_amount',0))
        expense_basis='일시불 사용금액'
    outside=not installment and row['date'][:7]!=month
    if installment and row['date'][:7]>month:
        raise ValueError('할부 정산월이 최초 구매월보다 빠릅니다.')
    row.update(month=month,settlement_month=month,settlement_basis='사용월',
               usage_month=month if installment else row['date'][:7],card_kind='사용내역',
               recognized_amount=net,expense_basis=expense_basis,outside_period=outside,
               outflow=max(net,0),inflow=max(-net,0),include=not outside,
               reviewed=False,reason='대상월 밖의 일시불 사용: 제외' if outside else '사용월 기준 분류 필요')
    if net==0:
        row.update(include=False,reason='당월 반영액 0원: 확인 필요')
    row['fingerprint']=transaction_fingerprint(row)
    return row

def rule_matches(rule, row):
    return (rule.get('enabled', True)
            and rule['source'] == row['source'] and rule['account'] == row['account']
            and (rule.get('direction', '출금') == ('출금' if row['outflow'] else '입금'))
            and (norm(rule['description']) == norm(row['description']) if rule.get('match', '정확히') == '정확히'
                 else norm(rule['description']) in norm(row['description']))
            and (rule.get('amount') is None or rule['amount'] == (row['outflow'] or row['inflow'])))

def classify(rows, rules, existing=(), advisor=None):
    """advisor is an optional local callable returning suggestions; never confirms a row."""
    seen = {r['fingerprint'] for r in existing}
    counts = Counter(r['fingerprint'] for r in rows)
    exact, partial = defaultdict(list), []
    for rule in rules:
        if rule.get('match','정확히') == '정확히':
            exact[(rule['source'],rule['account'],rule.get('direction','출금'),norm(rule['description']))].append(rule)
        else:
            partial.append(rule)
    result = []
    for original in rows:
        r = dict(original)
        r.update(category='미분류', treatment='사업', reviewed=False, reason='분류 필요')
        candidates=exact[(r['source'],r['account'],'출금' if r['outflow'] else '입금',norm(r['description']))]+partial
        hits = [rule for rule in candidates if rule_matches(rule, r)]
        decisions = {(h['category'], h['treatment']) for h in hits}
        if len(decisions) == 1:
            r['category'], r['treatment'] = next(iter(decisions))
            r.update(reviewed=True, reason='확인한 분류 규칙')
            if any(p in norm(r['description']) for p in ['네이버페이','naverpay','카카오페이','토스페이']):
                r.update(reviewed=False, reason='공용 결제명: 같은 금액도 용도가 다를 수 있어 확인 필요')
        elif len(decisions) > 1:
            r.update(reason='분류 규칙 충돌: 확인 필요')
        else:
            text = norm(r['description'])
            for words, cat, treatment in [
                (['카드대금', '카드결제대금'], '기타', '카드대금 납부'),
                (['대출', '차입'], '기타', '대출'),
                (['계좌이체', '자금이체'], '기타', '계좌이체'),
                (['cj대한통운', '롯데택배', '한진택배'], '택배비', '사업'),
                (['메타광고', 'facebookads', 'googleads'], '광고비', '사업'),
                (['스마트스토어정산', '쿠팡정산'], '판매대금 입금', '사업'),
            ]:
                if any(w in text for w in words):
                    r.update(category=cat, treatment=treatment, reason='거래명 추천: 첫 확인 필요')
                    break
            if advisor and r['category'] == '미분류':
                suggestion = advisor(dict(r))
                if suggestion in CATEGORIES:
                    r.update(category=suggestion, reason='AI 제안: 확인 필요')
        if r['fingerprint'] in seen or counts[r['fingerprint']] > 1:
            r.update(duplicate=True, include=False, reviewed=False, reason='중복 후보: 동일 거래인지 확인')
        if r.get('card_kind')=='승인내역':
            r.update(include=False,reviewed=False,reason='승인내역: 월별 청구금액이 없어 정산 제외')
        elif r.get('settlement_basis')=='청구월' and not r['inflow'] and not r['outflow']:
            r.update(include=False,reviewed=False,reason='당월 청구 0원: 정산 제외·내용 확인')
        if r.get('outside_period'):
            r.update(include=False,reviewed=False,reason='대상월 밖의 사용: 제외·기간 확인')
        result.append(r)
    return result

def learn_rules(rows, amount_scoped=True):
    rules = {}
    for r in rows:
        if not r['reviewed'] or not r['include'] or r['category'] == '미분류':
            continue
        rule = {k: r[k] for k in ['source', 'account', 'description', 'category', 'treatment']}
        rule.update(direction='출금' if r['outflow'] else '입금', match='정확히',
                    amount=(r['outflow'] or r['inflow']) if amount_scoped else None, enabled=True)
        rule['id'] = digest(rule)
        rules[rule['id']] = rule
    return list(rules.values())

def metrics(rows):
    business = [r for r in rows if r['include'] and r['treatment'] == '사업' and r.get('card_kind')!='승인내역']
    income = sum(r['inflow'] for r in business if r['source'] == '은행')
    expense = sum(r['outflow'] - (r['inflow'] if r['source'] == '카드' else 0) for r in business)
    bank_net = sum(r['inflow'] - r['outflow'] for r in rows if r['include'] and r['source'] == '은행')
    pending = sum(not r['reviewed'] or (r['include'] and r['category'] == '미분류') for r in rows)
    return dict(income=income, expense=expense, residual=income-expense,
                bank_net=bank_net, pending=pending)

def validate_rows(rows):
    if not rows:
        raise ValueError('거래가 없습니다')
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('거래 ID 중복')
    for r in rows:
        if r['treatment'] not in TREATMENTS or not str(r['category']).strip():
            raise ValueError('분류값 오류')
        basis=r.get('settlement_basis')
        expected_month=(billing_month(r['billing_month']) if basis=='청구월' else
                        billing_month(r['settlement_month']) if basis=='사용월' else transaction_date(r['date'])[:7])
        if expected_month != r['month']:
            raise ValueError('거래월 불일치')
        if r.get('settlement_basis')=='청구월':
            if r['source']!='카드' or r['month'] < transaction_date(r['date'])[:7]:
                raise ValueError('카드 청구월 오류')
            net=r['billing_amount']+(0 if r.get('fee_included',True) else r.get('fee',0))
            if r['outflow']-r['inflow'] != net:
                raise ValueError('청구금액과 집계금액 불일치')
        if basis=='사용월':
            if r['source']!='카드' or r['outflow']-r['inflow']!=r['recognized_amount']:
                raise ValueError('사용월 집계금액 불일치')
            if r.get('outside_period') and r['include']:
                raise ValueError('대상월 밖의 일시불 사용은 포함할 수 없습니다.')
        if r['source']=='카드' and r.get('card_kind')=='승인내역' and r['include']:
            raise ValueError('승인내역은 월정산에 포함할 수 없습니다. 월별 청구내역을 올리세요.')
        if not r['reviewed'] or (r['include'] and r['category'] == '미분류'):
            raise ValueError('확인 필요 거래를 모두 검토하세요. 제외 거래도 확인 체크가 필요합니다.')
        if r['source'] == '카드' and r['treatment'] == '카드대금 납부':
            raise ValueError('카드 사용내역은 카드대금 납부로 지정할 수 없습니다')

def csv_safe(frame):
    result = frame.copy()
    for c in result.select_dtypes(include=['object', 'string']).columns:
        result[c] = result[c].map(lambda x: "'"+x if isinstance(x, str) and x.startswith(('=', '+', '-', '@', '\t', '\r')) else x)
    return result.to_csv(index=False).encode('utf-8-sig')
