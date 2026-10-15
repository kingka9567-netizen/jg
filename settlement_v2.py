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
from settlement.core import (CATEGORIES, TREATMENTS, ALIASES, digest, guess_columns,
    guess_header, parse_rows, classify, learn_rules, metrics, csv_safe)
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
        st.caption(f"은행 순현금 증감: {m['bank_net']:,}원 · 포함된 은행 거래의 모든 입출금(이체·대출·카드대금 포함) 기준. 카드 사용 지출은 사용일에 집계하므로 월정산 잔여금과 실제 통장 잔액은 다를 수 있습니다.")
        if m['pending']:
            st.warning('검토 전 예상 합계입니다. 미확인 사업 거래도 포함하며, 중복 후보는 기본 제외합니다.')

def editor(rows, key):
    """Always merge visible edits into full session draft, then calculate metrics."""
    slot = st.empty()
    if key not in st.session_state:
        st.session_state[key] = [dict(r) for r in rows]
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
    display_fields = ['id','date','source','account','description','inflow','outflow',
                      'category','treatment','include','reviewed','reason','duplicate']
    labels = dict(id='거래 ID',date='거래일',source='자료 종류',account='계좌/카드',description='내역',
                  inflow='입금/환불',outflow='출금/사용',category='카테고리',treatment='처리 구분',
                  include='포함',reviewed='확인',reason='분류 근거',duplicate='중복 후보')
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
            disabled=[c for c in display_fields if c not in ['category','treatment','include','reviewed']],
            column_config={
                **{c: st.column_config.Column(label) for c,label in labels.items()},
                'id': None,
                'category': st.column_config.SelectboxColumn('카테고리', options=cats, required=True),
                'treatment': st.column_config.SelectboxColumn('처리 구분', options=TREATMENTS, required=True),
            }, width='stretch')
        updates = {r['id']:r for r in edited.to_dict('records')}
        for r in draft:
            if r['id'] in updates:
                r.update({c: updates[r['id']][c] for c in ['category','treatment','include','reviewed']})
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
    show_metrics([r for r in draft if r['month']==metric_month], slot)
    st.caption(f'전체 업로드 확인 필요: {metrics(draft)["pending"]}건 · 저장 전 모든 월의 검토를 완료하세요.')
    counts = [r for r in draft if r['month']==metric_month and r['include'] and r['treatment']=='사업']
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
    staged_rows, batches, profiles, problems = [], [], [], []
    if MODE == 'demo':
        st.info('가상 예시: 판매대금 500만원, 택배비 30만원, 광고비 20만원, 카드대금 납부 20만원입니다.')
        if st.button('가상 은행·카드 내역 불러오기'):
            frames = [
                ('은행','사업통장',pd.DataFrame({'거래일':['2026-09-01','2026-09-02','2026-09-25'], '내역':['스마트스토어정산','CJ대한통운','카드대금'], '입금액':[5000000,0,0],'출금액':[0,300000,200000]})),
                ('카드','사업카드',pd.DataFrame({'거래일':['2026-09-02'],'내역':['메타광고'],'입금액':[0],'출금액':[200000]}))]
            demo_rows, demo_batches = [], []
            for src, account, frame in frames:
                rs, _ = parse_rows(frame,guess_columns(frame.columns),src,account,digest(src),'예시',0,'입금/출금 별도')
                demo_rows.extend(rs); demo_batches.append({'id':rs[0]['batch_id'],'filename':src+' 가상 예시'})
            st.session_state.demo_import = (classify(demo_rows,store.settings('rule:'),store.rows()),demo_batches)
        staged_rows, batches = st.session_state.get('demo_import', ([],[]))
    else:
        files = st.file_uploader('은행·카드 엑셀 파일', type=['xlsx','xls','csv'], accept_multiple_files=True)
        known_profiles = store.settings('profile:')
        st.caption('계좌/카드 별칭을 매달 동일하게 사용하세요. 파일 내용은 파싱 후 저장하지 않으므로 원본도 별도로 보관하세요.')
        for index, file in enumerate(files or []):
            data = file.getvalue()
            file_hash = hashlib.sha256(data).hexdigest()
            key = f'upload_{index}_{file_hash[:12]}'
            with st.expander(file.name, expanded=True):
                src = st.selectbox('자료 종류', ['은행','카드'], key=key+'_source')
                accounts=sorted({p['account'] for p in known_profiles if p['source']==src})
                account_choice=st.selectbox('저장한 계좌/카드',accounts+['새로 등록'],key=key+'_account_choice')
                account = st.text_input('계좌/카드 별칭', key=key+'_account', placeholder='예: 국민 사업통장 / 신한 사업카드') if account_choice=='새로 등록' else account_choice
                try:
                    if file.name.lower().endswith('.csv'):
                        encoding = st.selectbox('CSV 인코딩', ['utf-8-sig','cp949'], key=key+'_encoding')
                        raw = pd.read_csv(io.BytesIO(data), header=None, encoding=encoding)
                        sheet = 'CSV'
                    else:
                        book = pd.ExcelFile(io.BytesIO(data))
                        # Remember sheet choice when a known account has this workbook layout.
                        prior = next((p for p in known_profiles if p['source']==src and p['account']==account and p.get('sheets')==book.sheet_names),None)
                        default_sheet = prior['sheet'] if prior and prior['sheet'] in book.sheet_names else book.sheet_names[0]
                        sheet = st.selectbox('시트',book.sheet_names,index=book.sheet_names.index(default_sheet),key=key+'_sheet')
                        raw = pd.read_excel(book, sheet_name=sheet, header=None)
                    raw=raw.dropna(axis=1,how='all')
                    previous_profile=next((p for p in known_profiles if p['source']==src and p['account']==account and p['sheet']==sheet
                        and p['header'] < len(raw) and p['id']==digest([src,account,sheet,[str(v).strip() for v in raw.iloc[p['header']].tolist()]])),None)
                    default_header=previous_profile['header'] if previous_profile else guess_header(raw)
                    header = int(st.number_input('제목 행 번호 (엑셀 기준)',1,max(1,len(raw)),default_header+1,key=key+'_header'))-1
                    columns = [str(v).strip() for v in raw.iloc[header].tolist()]
                    if len(set(columns)) != len(columns) or any(c in ('nan','') for c in columns):
                        raise ValueError('제목 행에 빈 열 또는 중복 열 이름이 있습니다. 원본에서 제거하거나 제목 행을 확인하세요.')
                    frame = raw.iloc[header+1:].copy(); frame.columns = columns
                    signature = digest([src,account,sheet,columns])
                    saved = next((p for p in known_profiles if p['id']==signature),None)
                    default_map = saved['mapping'] if saved else guess_columns(columns)
                    modes = ['입금/출금 별도','양수=출금 (음수=환불)','양수=입금 (음수=출금)']
                    default_mode = saved['amount_mode'] if saved else ('양수=출금 (음수=환불)' if src=='카드' and not default_map.get('outflow') else modes[0])
                    amount_mode = st.selectbox('금액 방식',modes,index=modes.index(default_mode),key=key+'_amount_mode')
                    mapping = {}
                    field_labels = {'date':'거래일','description':'내역/거래처','outflow':'출금','inflow':'입금','amount':'부호 있는 금액','external_id':'승인/거래번호 (선택)'}
                    for field in ALIASES:
                        if field=='amount' and amount_mode==modes[0]:
                            mapping[field]=''; continue
                        if field in ('outflow','inflow') and amount_mode!=modes[0]:
                            mapping[field]=''; continue
                        opts = ['']+columns
                        value = default_map.get(field,'')
                        mapping[field] = st.selectbox(field_labels[field],opts,index=opts.index(value) if value in opts else 0,key=key+'_'+field,format_func=lambda x:x or '선택 안 함')
                    st.caption('저장한 형식 자동 적용' if saved else '처음 보는 형식 · 정산 저장 시 기억합니다')
                    st.dataframe(frame.head(5),hide_index=True)
                    parsed, errors = parse_rows(frame,mapping,src,account,file_hash,sheet,header,amount_mode)
                    if errors:
                        problems.extend(errors); st.error('오류 행이 있어 이 파일은 저장할 수 없습니다. 원본이나 열 설정을 수정하세요.'); st.dataframe(errors)
                    if parsed:
                        bid=parsed[0]['batch_id']
                        if bid in store.batch_ids():
                            st.warning('이미 저장한 파일·시트입니다. 이번 저장에서 제외합니다.')
                        elif bid not in {b['id'] for b in batches}:
                            staged_rows.extend(parsed)
                            batches.append(dict(id=bid,filename=file.name,hash=file_hash,source=src,account=account,sheet=sheet,header=header,mapping=mapping))
                            profiles.append(dict(id=signature,source=src,account=account,sheet=sheet,sheets=book.sheet_names if sheet!='CSV' else [],header=header,mapping=mapping,amount_mode=amount_mode))
                except Exception as e:
                    problems.append({'파일':file.name,'오류':str(e)})
                    st.error(f'파일 설정을 확인하세요: {e}')
        if staged_rows:
            staged_rows=classify(staged_rows,store.settings('rule:'),store.rows())
    if staged_rows:
        fingerprint = digest([staged_rows,profiles])[:16]
        key = 'new_'+fingerprint
        draft = editor(staged_rows,key)
        remember, scoped = rule_options(key)
        st.info('카드대금 납부로 제외한 금액에 해당하는 카드 사용내역이 누락되지 않았는지 확인하세요. 이체·대출·환불과 중복 후보도 함께 검토하세요.')
        if st.button('검토한 정산 저장',type='primary',disabled=bool(problems)):
            try:
                store.import_rows(batches,draft,learn_rules(draft,scoped) if remember else [],profiles)
                st.session_state.pop('demo_import',None)
                st.success('저장했습니다. 저장한 월정산에서 월별로 확인할 수 있습니다.')
            except (ValueError,ConflictError) as e:
                st.error(str(e))
            except Exception:
                st.error('저장에 실패해 작업을 취소했습니다. DB 연결을 확인하고 다시 시도하세요.')

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
        if st.button('수정 저장',type='primary'):
            try:
                store.edit_rows(draft,learn_rules(draft,scoped) if remember else [])
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
