"""Local suggestions and explicit merchant-group review. No API or network calls."""
import hashlib
import json
import re
from collections import defaultdict


def normalized(value):
    text=str(value).casefold()
    for company in ('주식회사','(주)','（주）','㈜'):
        text=text.replace(company,'')
    return re.sub(r'[\s()（）._-]+','',text)


def suggest(row):
    text=normalized(row['description'])
    direction='출금' if row['outflow'] else '입금'
    # Receipts identify a settlement channel only on the bank's incoming side.
    if row['source']=='은행' and direction=='입금':
        channels=('스마트스토어정','쿠팡정산','쿠팡페이','11st','11번가','지마켓','옥션','에이블리셀러스','카카오스타')
        if any(text.startswith(c) for c in channels):
            return '판매대금 입금','사업','판매채널 정산 입금 추천: 최초 확인 필요'
    if row['source']=='은행':
        for words,treatment in [(['카드대금','카드결제대금'],'카드대금 납부'),(['대출','차입'],'대출'),(['계좌이체','자금이체'],'계좌이체')]:
            if any(w in text for w in words):
                return '기타',treatment,'거래명 추천: 처리 구분 확인 필요'
    if direction=='출금':
        patterns=[
          (('관세','국세','지방세','국세청','관세청'),'세금','세금 관련 거래'),
          (('cj대한통운','롯데택배','한진택배'),'택배비','택배사 결제'),
          (('메타광고','facebookads','googleads','구글애즈'),'광고비','광고 결제'),
          (('sk브로드밴드','세븐모바일','구독료','adobe.com','adobecom'),'구독/통신','구독·통신 결제'),
          (('배민클럽',),'구독/통신','멤버십 결제'),
          (('우아한형제들','배달의민족'),'식비','배달 서비스 결제'),
          (('alibaba.com','alibabacom','지앤지커머스'),'사입','도매 구매 후보'),
        ]
        for words,category,reason in patterns:
            if any(w in text for w in words):
                return category,'사업',reason+': 사업·개인 및 용도 확인 필요'
        if text.startswith('한진') and any(c in text for c in ('택배','－','-')):
            return '택배비','사업','택배사명 후보: 확인 필요'
    return None


def needs_purpose(description):
    text=normalized(description)
    if any(w in text for w in ('네이버페이','네이버파이낸셜','카카오페이','토스페이','kcp','이니시스')):
        return '결제대행명: 같은 이름도 용도가 다를 수 있습니다.'
    if any(w in text for w in ('카페24','에이블리코퍼레이션','apple','지마켓','쿠팡','11번가')):
        return '플랫폼 결제: 상품·광고·서비스 등 실제 용도 확인 필요'
    return '기존 분류표에 없거나 판단 근거 부족: 거래처별로 확인'


def review_groups(rows):
    """Never merge different sources, accounts, directions, or current decisions."""
    grouped=defaultdict(list)
    for row in rows:
        if row['reviewed'] or row.get('duplicate') or row.get('outside_period') or not row['include']:
            continue
        key=(row['source'],row['account'],'출금' if row['outflow'] else '입금',row['description'],row['category'],row['treatment'])
        grouped[key].append(row)
    result=[]
    for key,items in grouped.items():
        amounts=[r['outflow'] or r['inflow'] for r in items]
        result.append(dict(group_id=hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest(),
            selected=False,source=key[0],account=key[1],direction=key[2],description=key[3],
            count=len(items),total=sum(amounts),minimum=min(amounts),maximum=max(amounts),
            category=key[4],treatment=key[5],ambiguous=any(r.get('purpose_ambiguous') for r in items),ids=[r['id'] for r in items]))
    return sorted(result,key=lambda g:(g['category']=='미분류',-g['count'],g['description']))


def apply_groups(rows,groups):
    """Validate all choices first; no partial updates or implicit inclusion changes."""
    chosen=[g for g in groups if g['selected']]
    if not chosen:
        raise ValueError('확인할 거래처를 선택하세요.')
    by_id={r['id']:r for r in rows}
    decisions={}
    for group in chosen:
        if not str(group['category']).strip() or group['category']=='미분류':
            raise ValueError('선택한 거래처의 카테고리를 지정하세요.')
        for ident in group['ids']:
            row=by_id[ident]
            if row.get('duplicate') or row.get('outside_period') or not row['include']:
                raise ValueError('중복·기간 밖·제외 거래는 별도로 검토하세요.')
            if ident in decisions:
                raise ValueError('같은 거래에 여러 분류를 적용할 수 없습니다.')
            decisions[ident]=(group['category'],group['treatment'])
    result=[]
    for original in rows:
        row=dict(original)
        if row['id'] in decisions:
            row.update(category=decisions[row['id']][0],treatment=decisions[row['id']][1],
                       reviewed=True,reason='거래처별 묶음 검토로 확인')
        result.append(row)
    return result,len(decisions)


def read_legacy_mapping(data,filename):
    """Only description/category are imported; history/raw_data are never migrated."""
    import io
    import pandas as pd
    if filename.casefold().endswith('.csv'):
        try:
            frame=pd.read_csv(io.BytesIO(data),encoding='utf-8-sig',dtype=str)
        except UnicodeDecodeError:
            frame=pd.read_csv(io.BytesIO(data),encoding='cp949',dtype=str)
    else:
        with pd.ExcelFile(io.BytesIO(data)) as book:
            sheet='mapping' if 'mapping' in book.sheet_names else book.sheet_names[0]
            frame=pd.read_excel(book,sheet_name=sheet,dtype=str)
    if not {'description','category'}<=set(frame.columns):
        raise ValueError('description·category 열이 있는 기존 mapping 시트가 필요합니다.')
    records=[]
    for _,row in frame.fillna('').iterrows():
        description,category=str(row['description']).strip(),str(row['category']).strip()
        if not description and not category:
            continue
        if not description or not category:
            raise ValueError('거래명 또는 카테고리가 빈 분류 행이 있습니다.')
        records.append(dict(description=description,category=category))
    if not records:
        raise ValueError('분류표가 비어 있습니다.')
    return records
