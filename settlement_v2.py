"""Run: python -m streamlit run settlement_v2.py"""
import hashlib
import hmac
import io
import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd
import streamlit as st
from settlement.core import (CATEGORIES, TREATMENTS, ALIASES, CARD_ALIASES, digest, guess_columns,
    guess_header, parse_rows, classify, learn_rules, metrics, csv_safe, billing_month)
from settlement.checks import upload_summary, coverage_checks, duplicate_comparisons
from settlement.store import Store, ConflictError

st.set_page_config(page_title='월별 정산 내역', page_icon='📒', layout='wide')

def config(key, default=''):
    if key in os.environ:
        return os.environ[key]
    try:
        return str(st.secrets.get(key, default))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default

MODE = config('SETTLEMENT_MODE', 'demo')
if MODE not in ('demo', 'local', 'cloud'):
    st.error('SETTLEMENT_MODE는 demo, local, cloud 중 하나여야 합니다.'); st.stop()
if MODE != 'demo':
    password = config('SETTLEMENT_PASSWORD')
    if len(password) < 16:
        st.error('운영 데이터 접근 전에 Secrets에 16자 이상의 SETTLEMENT_PASSWORD를 설정하세요.'); st.stop()
    if not st.session_state.get('authenticated'):
        st.title('월정산 로그인')
        with st.form('login'):
            supplied = st.text_input('비밀번호', type='password')
            login = st.form_submit_button('로그인')
        if login:
            if hmac.compare_digest(supplied.encode(), password.encode()):
                st.session_state.authenticated = True
                st.rerun()
            st.error('비밀번호를 확인하세요.')
        st.stop()

if MODE == 'demo':
    if 'demo_path' not in st.session_state:
        st.session_state.demo_path = str(Path(tempfile.mkdtemp(prefix='settlement-demo-'))/'demo.sqlite3')
    database_url = 'sqlite:///' + st.session_state.demo_path
else:
    database_url = config('SETTLEMENT_DATABASE_URL', 'sqlite:///data/settlement.sqlite3' if MODE == 'local' else '')
    if MODE == 'cloud' and not database_url.startswith(('postgresql://', 'postgres://')):
        st.error('클라우드 운영은 영속 PostgreSQL 연결이 필요합니다. 로컬 SQLite로 대체 저장하지 않습니다.'); st.stop()
    if MODE == 'cloud' and 'sslmode=verify-full' not in database_url and 'sslmode=require' not in database_url:
        st.error('클라우드 DB URL에 sslmode=require 또는 verify-full을 설정하세요.'); st.stop()
@st.cache_resource
def open_store(url):
    return Store(url)

try:
    store = open_store(database_url)
except Exception:
    st.error('저장소 연결에 실패했습니다. 관리자에게 DB 연결·접근 권한을 확인하세요.'); st.stop()

custom_categories = store.settings('category:')
CATEGORIES = sorted(set(CATEGORIES + [c['name'] for c in custom_categories]))
registered_cards=store.settings('card:')

with st.sidebar:
    st.title('📒 월정산')
    menu = st.radio('대메뉴', ['월별 정산 내역', '월별 사업 분석 보고서'])
    if MODE == 'demo':
        st.warning('체험 모드 · 가상 자료\n실제 자료는 운영 모드에서 업로드하세요.')
    else:
        st.caption('운영 모드 · '+ ('PostgreSQL' if store.pg else '로컬 SQLite'))
        if st.button('로그아웃'):
            st.session_state.clear(); st.rerun()
    with st.expander('카테고리 추가'):
        new_category=st.text_input('새 카테고리 이름')
        if st.button('추가') and new_category.strip():
            store.save_setting('category:'+digest(new_category.strip()), {'name':new_category.strip()})
            st.rerun()
    with st.expander('정산 대상 카드 설정'):
        st.caption('카드별 별칭과 대금이 빠지는 통장을 등록하면 누락을 점검합니다. 여러 장이면 카드마다 등록하세요.')
        if registered_cards:
            st.dataframe(pd.DataFrame(registered_cards),hide_index=True)
        with st.form('card_registry'):
            card_name=st.text_input('카드 별칭 (파일 설정과 동일)')
            bank_name=st.text_input('카드대금 출금 통장 별칭')
            card_start=st.text_input('점검 시작월',value=(pd.Timestamp.now(tz='Asia/Seoul')-pd.DateOffset(months=1)).strftime('%Y-%m'))
            card_enabled=st.checkbox('매월 청구자료 점검',value=True)
            if st.form_submit_button('카드 설정 저장'):
                try:
                    if not card_name.strip() or not bank_name.strip():
                        raise ValueError('카드와 통장 별칭을 입력하세요.')
                    store.save_setting('card:'+digest(card_name.strip()),dict(account=card_name.strip(),bank_account=bank_name.strip(),start_month=billing_month(card_start),enabled=card_enabled))
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

if menu == '월별 사업 분석 보고서':
    st.title('월별 사업 분석 보고서')
    st.info('추후 월별 판매·광고 자료를 도입할 때 구현합니다. 현재는 월별 정산 내역을 이용하세요.')
    st.stop()

st.title('월별 정산 내역')
st.caption('파일 업로드 → 확인이 필요한 거래 검토 → 월정산 저장')
view = st.radio('작업', ['새 파일 정산', '저장한 월정산', '분류 규칙', '백업'], horizontal=True)

def show_metrics(rows, target):
    m = metrics(rows)
    with target.container():
        cols = st.columns(4)
        cols[0].metric('월정산 수입', f"{m['income']:,}원")
        cols[1].metric('사업 지출 (환불 차감)', f"{m['expense']:,}원")
        cols[2].metric('최종 순수익 (월정산 기준)', f"{m['residual']:,}원")
        cols[3].metric('확인 필요', f"{m['pending']}건")
        st.caption('최종 순수익은 수수료 차감 후 사업 입금 − 사업 지출입니다. 재고·미수금·감가상각 등을 반영하는 회계상 순이익과 다릅니다. 세금 예비비를 자동 차감하지 않습니다.')
        st.caption(f"은행 순현금 증감: {m['bank_net']:,}원 · 포함된 은행 거래의 모든 입출금(이체·대출·카드대금 포함) 기준. 새 카드 자료는 구매일과 별도로 청구월의 실제 청구액을 집계합니다. 월정산 잔여금과 통장 잔액은 다를 수 있습니다.")
        if m['pending']:
            st.warning('검토 전 예상 합계입니다. 미확인 사업 거래도 포함하며, 중복 후보는 기본 제외합니다.')

def card_review(rows,key,months=()):
    checks=coverage_checks(rows,store.rows(),registered_cards,months,store.settings('no_billing:'))
    has_card=bool(checks) or any(r['source']=='카드' for r in rows)
    if not has_card:
        return True
    st.subheader('카드 청구자료 누락 점검')
    if checks:
        table=pd.DataFrame(checks).rename(columns={'month':'청구월','account':'카드','status':'상태','message':'확인 내용'})
        st.dataframe(table.drop(columns='blocking'),hide_index=True,width='stretch')
    missing=[c for c in checks if c['blocking']]
    if missing:
        st.error('누락 또는 미연결 카드가 있습니다. 카드 청구내역 업로드, 대상 카드 연결, 청구 없음 확인 중 필요한 조치를 완료하세요.')
        absent=[c for c in missing if c['status']=='청구자료 누락']
        if absent:
            with st.expander('실제 청구가 없는 달 기록'):
                selected=st.selectbox('청구 없는 카드·월',list(range(len(absent))),format_func=lambda i:absent[i]['month']+' · '+absent[i]['account'],key=key+'_absent')
                reason=st.text_input('청구 없음 사유',key=key+'_absent_reason',placeholder='예: 이번 달 미사용이며 이월 할부도 없음')
                acknowledged=st.checkbox('청구서에서 이번 달 청구가 없음을 확인했습니다',key=key+'_absent_confirm')
                if st.button('청구 없음 기록',key=key+'_absent_save',disabled=not acknowledged or not reason.strip()):
                    item=absent[selected]
                    store.save_setting('no_billing:'+digest([item['month'],item['account']]),dict(month=item['month'],account=item['account'],reason=reason.strip(),confirmed=True))
                    st.rerun()
    if any(r['source']=='카드' and r.get('settlement_basis')!='청구월' for r in rows):
        st.warning('승인내역 또는 이전 버전의 거래일 기준 카드 자료가 있습니다. 기존 자료는 자동 변환하지 않습니다. 새 승인내역은 정산에 포함할 수 없습니다.')
    complete=st.checkbox('카드별 청구월·전체 건수·합계를 확인했습니다',key=key+'_card_complete')
    st.caption('청구자료 있음은 파일의 존재 확인입니다. 완전성이나 은행 납부액과의 대사 완료를 뜻하지 않습니다. 등록하지 않은 카드도 직접 확인하세요.')
    return complete and not missing

def compare_duplicates(draft,key,batches=()):
    if not any(r.get('duplicate') for r in draft):
        return
    comparisons=duplicate_comparisons(draft,store.rows(),store.batches()+list(batches))
    if not comparisons:
        return
    st.subheader('중복 후보 비교')
    selected=st.selectbox('비교할 이번 업로드 거래',list(range(len(comparisons))),
        format_func=lambda i:f"{comparisons[i]['new']['정산월']} · {comparisons[i]['new']['내역']} · {comparisons[i]['new']['당월 출금/청구']:,}원 · {comparisons[i]['new']['거래 ID'][:6]}",key=key+'_duplicate_selected')
    item=comparisons[selected]
    left,right=st.columns(2)
    left.write('이번 거래');left.dataframe(pd.DataFrame([item['new']]).T.astype(str),width='stretch')
    right.write('일치하는 기존/같이 올린 거래')
    right.dataframe(pd.DataFrame(item['matches']).T.astype(str),width='stretch')
    decision=st.radio('비교 결과',['이미 저장한 거래 또는 같은 파일의 반복 행 → 이번 행 제외','실제 별개 결제·청구 → 이번 행 포함'],key=key+'_duplicate_decision_'+item['id'])
    if st.button('비교 결과 적용',key=key+'_duplicate_apply'):
        row=next(r for r in draft if r['id']==item['id'])
        row.update(include=decision.startswith('실제'),reviewed=True,duplicate_resolution=decision)
        st.session_state[key+'_revision']+=1
        st.rerun()
    st.caption('동일 파일 안의 반복 후보는 최소 한 건을 남기거나, 모두 별개 거래임을 확인해 포함하세요. 다른 달의 정상 할부 청구는 중복으로 처리하지 않습니다.')

def editor(rows, key, batches=()):
    """Always merge visible edits into full session draft, then calculate metrics."""
    slot = st.empty()
    if key not in st.session_state:
        st.session_state[key] = [dict(r,card_account=r.get('card_account','')) for r in rows]
        st.session_state[key+'_revision'] = 0
    draft = st.session_state[key]
    months=sorted({r['month'] for r in draft},reverse=True)
    metric_month=st.selectbox('지표 기준월',months,key=key+'_metric_month')
    if len(months)>1:
        st.info('여러 달이 포함된 파일입니다. 상단 지표는 선택한 월만, 아래 검토 표와 저장은 모든 거래를 대상으로 합니다.')
    q = st.text_input('거래명 검색', key=key+'_search')
    a, b = st.columns(2)
    review_only = a.checkbox('확인 필요만 보기', value=True, key=key+'_pending')
    cats = sorted(set(CATEGORIES + [r['category'] for r in draft]))
    selected_cats = b.multiselect('카테고리 필터', cats, key=key+'_categories')
    active_filter=digest([q,review_only,selected_cats])
    if st.session_state.get(key+'_active_filter') != active_filter:
        st.session_state[key+'_revision'] += 1
        st.session_state[key+'_active_filter']=active_filter
    visible = [r for r in draft if (not q or q.casefold() in r['description'].casefold())
               and (not review_only or not r['reviewed'] or (r['include'] and r['category']=='미분류'))
               and (not selected_cats or r['category'] in selected_cats)]
    rev = st.session_state[key+'_revision']
    display_fields = ['id','date','month','source','account','description','inflow','outflow',
                      'category','treatment','card_account','include','reviewed','reason','duplicate']
    labels = dict(id='거래 ID',date='구매/거래일',month='정산월',source='자료 종류',account='계좌/카드',description='내역',
                  inflow='입금/환불',outflow='출금/사용',category='카테고리',treatment='처리 구분',
                  card_account='납부 대상 카드',include='포함',reviewed='확인',reason='분류 근거',duplicate='중복 후보')
    # Keep the editor's input immutable for this filter/revision. Changing its input
    # on every rerun can reset Streamlit's pending edits or drop newly reviewed rows.
    snapshot_key=f'{key}_snapshot_{rev}_'+digest([q,review_only,selected_cats])[:12]
    if snapshot_key not in st.session_state:
        st.session_state[snapshot_key]=pd.DataFrame(visible)[display_fields] if visible else pd.DataFrame()
    frame=st.session_state[snapshot_key]
    if not frame.empty:
        visible_ids=set(frame['id'])
        visible=[r for r in draft if r['id'] in visible_ids]
        edited = st.data_editor(frame, key=snapshot_key+'_editor', hide_index=True,
            disabled=[c for c in display_fields if c not in ['category','treatment','include','reviewed','card_account']],
            column_config={
                **{c: st.column_config.Column(label) for c,label in labels.items()},
                'id': None,
                'category': st.column_config.SelectboxColumn('카테고리', options=cats, required=True),
                'treatment': st.column_config.SelectboxColumn('처리 구분', options=TREATMENTS, required=True),
                'card_account': st.column_config.SelectboxColumn('납부 대상 카드',options=sorted({''} | {c['account'] for c in registered_cards} | {r['account'] for r in draft if r['source']=='카드'} | {r.get('card_account','') for r in draft})),
            }, width='stretch')
        updates = {r['id']:r for r in edited.to_dict('records')}
        for r in draft:
            if r['id'] in updates:
                r.update({c: updates[r['id']][c] for c in ['category','treatment','include','reviewed','card_account']})
                r['card_account']=r.get('card_account') or ''
        st.caption('카드대금 납부·계좌이체·대출·개인/제외는 월정산 수입과 사업 지출에서 제외됩니다. 중복 후보는 실제 별개 거래라면 포함을 체크하세요.')
        with st.expander('현재 검색 결과 일괄 수정'):
            category = st.selectbox('일괄 카테고리', cats, key=key+'_bulk_category')
            treatment = st.selectbox('일괄 처리 구분', TREATMENTS, key=key+'_bulk_treatment')
            inclusion = st.selectbox('포함 여부', ['현재 값 유지','모두 포함','모두 제외'], key=key+'_bulk_include')
            confirm = st.checkbox(f'현재 표시된 {len(visible)}건을 검토했습니다', key=key+'_bulk_confirm')
            if st.button('현재 검색 결과 확인 처리 (분류 유지)', key=key+'_review', disabled=not confirm):
                for r in draft:
                    if r['id'] in visible_ids:
                        r['reviewed']=True
                st.session_state[key+'_revision'] += 1
                st.rerun()
            if st.button('현재 검색 결과에 적용', key=key+'_bulk', disabled=not confirm):
                ids = {r['id'] for r in visible}
                for r in draft:
                    if r['id'] in ids:
                        r.update(category=category,treatment=treatment,reviewed=True)
                        if inclusion != '현재 값 유지':
                            r['include'] = inclusion == '모두 포함'
                st.session_state[key+'_revision'] += 1
                st.rerun()
    else:
        st.info('현재 조건에 해당하는 거래가 없습니다. 전체 내역은 확인 필요 필터를 해제하세요.')
    compare_duplicates(draft,key,batches)
    installments=[r for r in draft if r['source']=='카드' and r.get('settlement_basis')=='청구월']
    if installments:
        with st.expander('카드 구매일·청구월·할부 정보'):
            st.dataframe(pd.DataFrame([{'구매일':r['date'],'청구월':r['month'],'카드':r['account'],'내역':r['description'],
                '전체 구매금액(참고)':r.get('purchase_total'),'당월 청구금액':r.get('billing_amount'),'수수료':r.get('fee',0),
                '수수료 포함':r.get('fee_included',True),'이번 회차':r.get('installment_number'),'총 회차':r.get('installment_total'),
                '집계 지출':r['outflow']-r['inflow']} for r in installments]),hide_index=True,width='stretch')
    show_metrics([r for r in draft if r['month']==metric_month], slot)
    st.caption(f'전체 업로드 확인 필요: {metrics(draft)["pending"]}건 · 저장 전 모든 월의 검토를 완료하세요.')
    counts = [r for r in draft if r['month']==metric_month and r['include'] and r['treatment']=='사업' and r.get('card_kind')!='승인내역']
    if counts:
        expenses = pd.DataFrame(counts)
        expenses['지출'] = expenses['outflow'] - expenses['inflow'].where(expenses['source']=='카드', 0)
        st.bar_chart(expenses.groupby('category')['지출'].sum())
    st.download_button('현재 내역 CSV', csv_safe(pd.DataFrame(draft)), '월정산.csv', key=key+'_csv')
    return draft

def rule_options(key):
    remember = st.checkbox('이번에 확인한 포함 거래의 분류를 다음 업로드에 기억', value=True, key=key+'_remember')
    scope = st.radio('기억 범위', ['동일 거래명·계좌·방향·금액', '동일 거래명·계좌·방향 (금액 무관)'], key=key+'_scope', horizontal=True)
    st.caption('동일 이름의 용도가 다르면 금액 조건을 유지하세요. 서로 다른 분류가 겹치면 다음 업로드에서 다시 확인합니다.')
    return remember, scope.startswith('동일 거래명·계좌·방향·금액')

if view == '새 파일 정산':
    staged_rows, batches, profiles, problems, file_checks, parsed_for_summary = [], [], [], [], [], []
    last_month=(pd.Timestamp.now(tz='Asia/Seoul')-pd.DateOffset(months=1)).strftime('%Y-%m')
    target_input=st.text_input('정산 대상월',value=last_month)
    try:
        target_month=billing_month(target_input)
    except ValueError:
        st.error('정산 대상월은 YYYY-MM 형식으로 입력하세요.');st.stop()
    if MODE == 'demo':
        st.info('기본 가상 자료 또는 7월 구매·9월 3/3회차 청구 예시를 불러올 수 있습니다.')
        basic=st.button('가상 은행·카드 내역 불러오기')
        installment=st.button('7월 구매·9월 할부 청구 예시 불러오기')
        if basic or installment:
            frame=pd.DataFrame({'거래일':['2026-09-01','2026-09-02','2026-09-25'],'내역':['스마트스토어정산','CJ대한통운','카드대금'],
                                '입금액':[5000000,0,0],'출금액':[0,300000,101000 if installment else 200000]})
            card=pd.DataFrame({'이용일자':['2026-07-10' if installment else '2026-09-02'],
                               '내역':['촬영 장비' if installment else '메타광고'],
                               '청구금액':[100000 if installment else 200000],
                               '할부수수료':[1000 if installment else 0], '할부회차':['3/3' if installment else '일시불'],
                               '총구매금액':[300000 if installment else 200000]})
            demo_rows,demo_batches=[],[]
            for src,account,f in [('은행','사업통장',frame),('카드','사업카드',card)]:
                mapping=guess_columns(f.columns,card=src=='카드')
                rs,errors=parse_rows(f,mapping,src,account,digest([src,installment]),'예시',0,'입금/출금 별도',
                                    card_kind='청구내역',statement_month='2026-09',fee_included=False)
                for row in rs:
                    if row['treatment']=='카드대금 납부' or row['description']=='카드대금':
                        row['card_account']='사업카드'
                demo_rows.extend(rs)
                demo_batches.append(dict(id=rs[0]['batch_id'],filename=src+' 가상 예시',source=src,account=account,source_rows=len(f),error_count=len(errors),card_kind='청구내역' if src=='카드' else '은행 거래'))
            classified=classify(demo_rows,store.settings('rule:'),store.rows())
            st.session_state.demo_import=(classified,demo_batches)
        staged_rows,batches=st.session_state.get('demo_import',([],[]))
        file_checks=batches
        parsed_for_summary=staged_rows
    else:
        files=st.file_uploader('은행·카드 엑셀 파일',type=['xlsx','xls','csv'],accept_multiple_files=True)
        known_profiles=store.settings('profile:')
        saved_batch_ids=store.batch_ids()
        st.caption('계좌/카드 별칭을 매달 동일하게 사용하세요. 카드 청구내역은 당월 청구금액으로 집계하며 승인내역은 정산에서 제외합니다.')
        for index,file in enumerate(files or []):
            data=file.getvalue();file_hash=hashlib.sha256(data).hexdigest()
            key=f'upload_{index}_{file_hash[:12]}'
            with st.expander(file.name,expanded=True):
                src=st.selectbox('자료 종류',['은행','카드'],key=key+'_source')
                accounts=sorted({p['account'] for p in known_profiles if p['source']==src} | ({c['account'] for c in registered_cards} if src=='카드' else set()))
                choice=st.selectbox('저장한 계좌/카드',accounts+['새로 등록'],key=key+'_account_choice')
                account=st.text_input('계좌/카드 별칭',key=key+'_account') if choice=='새로 등록' else choice
                meta=dict(id=digest([file_hash,src,account,index]),filename=file.name,source=src,account=account,source_rows=0,error_count=0)
                try:
                    sheets=[]
                    if file.name.lower().endswith('.csv'):
                        encoding=st.selectbox('CSV 인코딩',['utf-8-sig','cp949'],key=key+'_encoding')
                        raw=pd.read_csv(io.BytesIO(data),header=None,encoding=encoding);sheet='CSV'
                    else:
                        book=pd.ExcelFile(io.BytesIO(data));sheets=book.sheet_names
                        prior=next((p for p in known_profiles if p['source']==src and p['account']==account and p.get('sheets')==sheets),None)
                        default_sheet=prior['sheet'] if prior and prior['sheet'] in sheets else sheets[0]
                        sheet=st.selectbox('시트',sheets,index=sheets.index(default_sheet),key=key+'_sheet')
                        raw=pd.read_excel(book,sheet_name=sheet,header=None)
                    raw=raw.dropna(axis=1,how='all')
                    prior=next((p for p in known_profiles if p['source']==src and p['account']==account and p['sheet']==sheet
                        and p['header']<len(raw) and p['id']==digest([src,account,sheet,[str(v).strip() for v in raw.iloc[p['header']].tolist()]])),None)
                    header=int(st.number_input('제목 행 번호 (엑셀 기준)',1,max(1,len(raw)),(prior['header'] if prior else guess_header(raw))+1,key=key+'_header'))-1
                    columns=[str(v).strip() for v in raw.iloc[header].tolist()]
                    if len(set(columns))!=len(columns) or any(c in ('nan','') for c in columns):
                        raise ValueError('빈 제목 또는 중복 열 이름을 확인하세요.')
                    frame=raw.iloc[header+1:].copy();frame.columns=columns
                    meta['source_rows']=len(frame)
                    signature=digest([src,account,sheet,columns])
                    saved=next((p for p in known_profiles if p['id']==signature),None)
                    default_map=guess_columns(columns,card=src=='카드')
                    if saved:
                        default_map.update(saved['mapping'])
                    card_kind=''
                    month=''
                    fee_included=True
                    if src=='카드':
                        kinds=['청구내역','승인내역']
                        card_kind=st.radio('카드 파일 종류',kinds,index=kinds.index(saved.get('card_kind','청구내역')) if saved and saved.get('card_kind') in kinds else 0,key=key+'_kind',horizontal=True)
                        if card_kind=='청구내역':
                            month=st.text_input('파일 청구월 (청구월 열이 없을 때)',value=target_month,key=key+'_billmonth')
                            fee_included=st.checkbox('청구금액에 당월 수수료가 이미 포함됨',value=saved.get('fee_included',True) if saved else True,key=key+'_fee_included')
                            st.caption('구매일은 7월이어도 청구월이 9월이면 9월 지출입니다. 전체 구매금액을 할부개월로 자동 나누지 않습니다.')
                        else:
                            st.warning('승인내역에는 당월 청구액이 없습니다. 참고용으로만 저장하며 월정산에 포함할 수 없습니다.')
                    is_bill=src=='카드' and card_kind=='청구내역'
                    modes=['입금/출금 별도','양수=출금 (음수=환불)','양수=입금 (음수=출금)']
                    if is_bill:
                        amount_mode=modes[0]
                    else:
                        default_mode=saved.get('amount_mode',modes[0]) if saved else (modes[1] if src=='카드' and not default_map.get('outflow') else modes[0])
                        amount_mode=st.selectbox('금액 방식',modes,index=modes.index(default_mode),key=key+'_amount_mode')
                    mapping={}
                    labels={'date':'최초 구매일 / 은행 거래일','description':'내역/거래처','outflow':'출금','inflow':'입금','amount':'부호 있는 금액',
                            'external_id':'승인/거래번호 (선택)','billing_month':'청구월 열 (없으면 파일 청구월 사용)',
                            'billing_amount':'이번 달 청구금액 (환불은 음수)','purchase_total':'전체 구매금액 (참고용·선택)',
                            'installment_number':'이번 할부 회차 또는 3/3 (선택)','installment_total':'전체 할부 개월/회차 (선택)',
                            'fee':'이번 달 할부 수수료 (선택)'}
                    fields=list(ALIASES)+list(CARD_ALIASES) if src=='카드' else list(ALIASES)
                    for field in fields:
                        used=True
                        if is_bill and field in ('amount','outflow','inflow'): used=False
                        if not is_bill and field in ('billing_month','billing_amount','fee'): used=False
                        if not is_bill and field=='amount' and amount_mode==modes[0]: used=False
                        if not is_bill and field in ('outflow','inflow') and amount_mode!=modes[0]: used=False
                        if not used:
                            mapping[field]='';continue
                        opts=['']+columns;value=default_map.get(field,'')
                        mapping[field]=st.selectbox(labels[field],opts,index=opts.index(value) if value in opts else 0,key=key+'_'+field,format_func=lambda x:x or '선택 안 함')
                    if is_bill:
                        charge_confirm=st.checkbox('청구금액 열이 전체 구매금액이 아닌 이번 달 청구액임을 확인했습니다',key=key+'_charge_confirm')
                        if not charge_confirm:
                            problems.append({'파일':file.name,'오류':'당월 청구금액 열 확인 필요'})
                    st.caption('저장한 형식 적용' if saved else '처음 보는 형식 · 정상 저장 시 기억합니다')
                    st.dataframe(frame.head(5),hide_index=True)
                    parsed,errors=parse_rows(frame,mapping,src,account,file_hash,sheet,header,amount_mode,card_kind=card_kind or '승인내역',statement_month=month,fee_included=fee_included)
                    bid=digest([file_hash,src,account.strip(),sheet])
                    meta.update(id=bid,source_rows=len(frame),error_count=len(errors),blank_rows=len(frame)-len(parsed)-len(errors),card_kind=card_kind or '은행 거래')
                    parsed_for_summary.extend(parsed)
                    if errors:
                        problems.extend(errors);st.error('오류 행을 수정하기 전에는 저장할 수 없습니다.');st.dataframe(errors)
                    if not parsed and not errors:
                        st.warning('정상 거래가 없습니다. 실제 청구 없는 달이면 카드 누락 점검에서 사유를 기록하세요.')
                    if parsed:
                        if bid in saved_batch_ids:
                            st.warning('이미 저장한 파일·시트입니다. 이번 저장에서 제외합니다.')
                            meta['status']='이미 저장한 파일'
                        elif bid not in {b['id'] for b in batches}:
                            staged_rows.extend(parsed)
                            batches.append(dict(meta,hash=file_hash,sheet=sheet,header=header,mapping=mapping,statement_month=month,fee_included=fee_included))
                            profiles.append(dict(id=signature,source=src,account=account,sheet=sheet,sheets=sheets,header=header,mapping=mapping,amount_mode=amount_mode,card_kind=card_kind,fee_included=fee_included))
                        else:
                            meta['status']='이번 업로드 중복 파일'
                except Exception as e:
                    meta.update(error_count=meta['source_rows'],status='설정 오류 · 저장 차단')
                    problems.append({'파일':file.name,'오류':str(e)})
                    st.error(f'파일 설정을 확인하세요: {e}')
                file_checks.append(meta)
        if staged_rows:
            staged_rows=classify(staged_rows,store.settings('rule:'),store.rows())
    if file_checks:
        st.subheader('업로드 확인표')
        overrides={r['id']:r for r in staged_rows}
        summary_rows=[overrides.get(r['id'],r) for r in parsed_for_summary]
        st.dataframe(pd.DataFrame(upload_summary(file_checks,summary_rows)),hide_index=True,width='stretch')
        st.caption('원본 데이터 행 = 정상 거래 + 빈 행 + 오류 행. 거래 없는 날짜가 곧 누락을 뜻하지는 않습니다. 원본 파일 전체 건수·합계와 대조하세요.')
        other_months=sorted({r['month'] for r in summary_rows if r['month']!=target_month})
        if other_months:
            st.warning('정산 대상월 밖의 거래가 있습니다: '+', '.join(other_months)+' · 의도한 기간인지 확인하세요.')
    if staged_rows:
        key='new_'+digest([staged_rows,profiles])[:16]
        draft=editor(staged_rows,key,batches)
        remember,scoped=rule_options(key)
        coverage_ok=card_review(draft,key,sorted({target_month} | {r['month'] for r in draft}))
        upload_ok=st.checkbox('업로드 확인표의 파일별 기간·건수·금액을 원본과 대조했습니다',key=key+'_upload_checked')
        if st.button('검토한 정산 저장',type='primary',disabled=bool(problems) or not coverage_ok or not upload_ok):
            try:
                store.import_rows(batches,draft,learn_rules(draft,scoped) if remember else [],profiles,coverage_confirmed=coverage_ok)
                st.session_state.pop('demo_import',None)
                st.success('저장했습니다. 저장한 월정산에서 월별로 확인할 수 있습니다.')
            except ValueError as e:
                st.error(str(e))
            except Exception:
                st.error('저장에 실패해 작업을 취소했습니다. DB 연결을 확인하세요.')

elif view == '저장한 월정산':
    months = store.months()
    if not months:
        st.info('아직 저장한 월정산이 없습니다.')
    else:
        month = st.selectbox('거래월',months)
        key = 'saved_'+month
        if st.button('저장된 자료 새로 불러오기 (미저장 수정 초기화)'):
            st.session_state.pop(key,None)
            st.session_state[key+'_revision'] = st.session_state.get(key+'_revision',0)+1
            st.rerun()
        original = store.rows(month)
        draft=editor(original,key)
        remember,scoped=rule_options(key)
        coverage_ok=card_review(draft,key)
        if st.button('수정 저장',type='primary',disabled=not coverage_ok):
            try:
                store.edit_rows(draft,learn_rules(draft,scoped) if remember else [],coverage_confirmed=coverage_ok)
                st.session_state.pop(key,None)
                st.session_state[key+'_revision'] += 1
                st.session_state['saved_notice'] = True
                st.rerun()
            except ValueError as e:
                st.error(str(e))
            except Exception:
                st.error('저장 실패. DB 연결을 확인하세요. 변경은 취소했습니다.')
        if st.session_state.pop('saved_notice',False):
            st.success('수정 내용을 저장했습니다.')

elif view == '분류 규칙':
    st.write('확인한 거래를 바탕으로 기억한 규칙입니다. 잘못된 규칙은 비활성화하세요.')
    rules=store.settings('rule:')
    if rules:
        frame=pd.DataFrame(rules)
        edited=st.data_editor(frame,hide_index=True,disabled=[c for c in frame.columns if c!='enabled'],width='stretch')
        if st.button('규칙 상태 저장'):
            for rule in edited.to_dict('records'):
                if pd.isna(rule.get('amount')):
                    rule['amount']=None
                store.save_setting('rule:'+rule['id'],rule)
            st.success('규칙 상태를 저장했습니다.')
    else:
        st.info('정산 검토 후 저장하면 규칙이 만들어집니다.')

elif view == '백업':
    st.write('거래·파일 식별값·분류 규칙·열 매핑·수정 이력을 JSON으로 내려받습니다. 원본 엑셀은 별도 보관하세요.')
    if st.button('최신 백업 생성'):
        st.session_state.backup=store.export()
    if 'backup' in st.session_state:
        st.download_button('전체 백업 다운로드',st.session_state.backup,
            'settlement-backup-'+datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d-%H%M%S')+'.json',mime='application/json')
    st.caption('기존 Google Sheets 자료는 자동으로 가져오거나 덮어쓰지 않습니다. 이관 전 원본 전체 백업과 거래일 재확인이 필요합니다.')
