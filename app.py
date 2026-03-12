import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 구글 시트 연결 설정 ---
def get_gspread_client():
    try:
        creds_dict = st.secrets["gcp_service_account"]
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"⚠️ Secrets 설정 오류: {e}")
        return None

def get_worksheet(sheet_name):
    client = get_gspread_client()
    if not client: return None
    try:
        sh = client.open("settlement_db")
        try:
            return sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            if sheet_name == "mapping":
                ws = sh.add_worksheet(title="mapping", rows="100", cols="2")
                ws.append_row(["description", "category"])
                return ws
            else:
                ws = sh.add_worksheet(title="history", rows="1000", cols="5")
                ws.append_row(["report_name", "date", "category", "income", "expense"])
                return ws
    except Exception as e:
        st.error(f"⚠️ 구글 시트 'settlement_db' 접속 실패. 공유 설정을 확인하세요.")
        return None

# --- 2. 데이터 처리 함수 ---
def load_mappings():
    ws = get_worksheet("mapping")
    if not ws: return {}
    data = ws.get_all_records()
    if not data: return {}
    df = pd.DataFrame(data)
    return dict(zip(df['description'], df['category'])) if 'description' in df.columns else {}

def save_mapping(desc, cat):
    ws = get_worksheet("mapping")
    if not ws: return
    try:
        cell = ws.find(desc)
        if cell: ws.update_cell(cell.row, 2, cat)
        else: ws.append_row([desc, cat])
    except: ws.append_row([desc, cat])

def save_report(name, df):
    ws = get_worksheet("history")
    if not ws: return
    date_now = datetime.now().strftime("%Y-%m-%d")
    summary = df.groupby('카테고리').agg({'입금금액':'sum', '지출금액':'sum'}).reset_index()
    try:
        cells = ws.findall(name)
        rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
        for r in rows_to_delete: ws.delete_rows(r)
    except: pass
    for _, row in summary.iterrows():
        ws.append_row([name, date_now, row['카테고리'], row['입금금액'], row['지출금액']])

# --- 3. 시각화 모듈 (공통 사용) ---
def display_dashboard(df, v_rate, i_rate, exclude):
    f_df = df[~df['카테고리'].isin(exclude)].copy()
    t_in = f_df["입금금액"].sum()
    t_out = f_df["지출금액"].sum()
    r_profit = t_in - t_out
    tax = (t_in * v_rate/100) + (max(0, r_profit) * i_rate/100)
    n_profit = r_profit - tax
    margin = (n_profit / t_in * 100) if t_in > 0 else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("총 매출", f"{t_in:,.0f}원")
    k2.metric("사업 지출", f"-{t_out:,.0f}원")
    k3.metric("세금 예비비", f"-{tax:,.0f}원")
    k4.metric("최종 순이익", f"{n_profit:,.0f}원", f"{margin:.1f}%")

    st.divider()
    
    st.subheader("📊 지출 카테고리별 상세 분석")
    col1, col2 = st.columns(2)
    
    exp_df = f_df[f_df['지출금액'] > 0].groupby('카테고리')['지출금액'].sum().reset_index().sort_values('지출금액', ascending=False)
    
    with col1:
        fig_bar = px.bar(exp_df, x='지출금액', y='카테고리', orientation='h', 
                         title="카테고리별 지출 금액 (비교)", text_auto=',.0f',
                         color='지출금액', color_continuous_scale='Reds')
        st.plotly_chart(fig_bar, use_container_width=True)
        
    with col2:
        fig_pie = px.pie(exp_df, values='지출금액', names='카테고리', title="지출 비중 (%)", hole=0.4)
        st.plotly_chart(fig_pie, use_container_width=True)

# --- 4. 앱 화면 구성 ---
st.set_page_config(page_title="HuckJun's 비즈니스 인텔리전스", layout="wide")
mapping_dict = load_mappings()
DEFAULT_CATS = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
full_cat_list = sorted(list(set(DEFAULT_CATS + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 메뉴")
    mode = st.radio("이동", ["새 정산하기", "과거 내역 및 추세"])
    st.divider()
    exclude_cats = st.multiselect("분석 제외", full_cat_list, default=["기타", "생활 및 기타"])
    v_rate = st.slider("부가세 (%)", 0, 10, 7)
    i_rate = st.slider("소득세 (%)", 0, 45, 15)

if mode == "새 정산하기":
    title = st.text_input("📝 정산 제목", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    files = st.file_uploader("엑셀 파일 선택", type=["xlsx"], accept_multiple_files=True)
    
    if files:
        all_data = []
        for i, f in enumerate(files):
            with st.expander(f"📄 {f.name} 매핑"):
                df_t = pd.read_excel(f)
                opts = ["없음"] + list(df_t.columns)
                c1, c2, c3 = st.columns(3)
                d_c = c1.selectbox(f"내역", opts, key=f"d_{i}", index=1)
                e_c = c2.selectbox(f"지출", opts, key=f"e_{i}", index=0)
                i_c = c3.selectbox(f"입금", opts, key=f"i_{i}", index=0)
                
                tmp = pd.DataFrame()
                tmp["내역"] = df_t[d_c].astype(str) if d_c != "없음" else ["없음"]*len(df_t)
                tmp["지출금액"] = pd.to_numeric(df_t[e_c], errors='coerce').fillna(0) if e_c != "없음" else 0.0
                tmp["입금금액"] = pd.to_numeric(df_t[i_c], errors='coerce').fillna(0) if i_c != "없음" else 0.0
                all_data.append(tmp)

        if all_data:
            df_main = pd.concat(all_data, ignore_index=True)
            df_main['카테고리'] = df_main["내역"].map(mapping_dict).fillna("미분류")
            
            display_dashboard(df_main, v_rate, i_rate, exclude_cats)
            
            st.subheader("📝 상세 수정 및 저장")
            edited = st.data_editor(df_main, column_config={"카테고리": st.column_config.SelectboxColumn("카테고리", options=full_cat_list)}, use_container_width=True)
            
            if st.button("💾 구글 시트에 최종 저장", type="primary"):
                save_report(title, edited)
                st.success("데이터 보관 완료!")

            uncl = edited[edited['카테고리'] == "미분류"]["내역"].unique()
            if len(uncl) > 0:
                with st.container(border=True):
                    st.write(f"미분류 학습: **{uncl[0]}**")
                    sc1, sc2, sc3 = st.columns([2, 2, 1])
                    sel = sc1.selectbox("카테고리", full_cat_list + ["(직접 입력)"], key="l_sel")
                    val = sc2.text_input("직접 입력") if sel == "(직접 입력)" else ""
                    if sc3.button("기억하기"):
                        save_mapping(uncl[0], val if sel == "(직접 입력)" else sel)
                        st.rerun()

elif mode == "과거 내역 및 추세":
    st.title("📜 과거 리포트 및 지출 추세 분석")
    ws = get_worksheet("history")
    if ws:
        h_data = ws.get_all_records()
        if h_data:
            df_h = pd.DataFrame(h_data)
            reports = sorted(df_h['report_name'].unique(), reverse=True)
            
            tab1, tab2 = st.tabs(["📄 리포트 상세 보기", "📈 전월 대비 추세"])
            
            with tab1:
                selected = st.selectbox("보고서 선택", reports)
                r_df = df_h[df_h['report_name'] == selected].rename(columns={'income':'입금금액', 'expense':'지출금액', 'category':'카테고리'})
                display_dashboard(r_df, v_rate, i_rate, exclude_cats)
                with st.expander("원본 요약 데이터 보기"):
                    st.dataframe(r_df, use_container_width=True)

            with tab2:
                if len(reports) >= 2:
                    st.subheader(f"🔄 {reports[0]} vs {reports[1]} 비교")
                    curr = df_h[df_h['report_name'] == reports[0]].groupby('category')['expense'].sum()
                    prev = df_h[df_h['report_name'] == reports[1]].groupby('category')['expense'].sum()
                    
                    comp_df = pd.DataFrame({'이번달': curr, '지난달': prev}).fillna(0)
                    comp_df['차이'] = comp_df['이번달'] - comp_df['지난달']
                    comp_df['증감률(%)'] = (comp_df['차이'] / comp_df['지난달'] * 100).replace([float('inf'), -float('inf')], 0).fillna(0)
                    
                    st.table(comp_df.style.format("{:,.0f}").background_gradient(subset=['차이'], cmap='RdYlGn_r'))
                    
                    fig_trend = go.Figure()
                    fig_trend.add_trace(go.Bar(name='지난달', x=comp_df.index, y=comp_df['지난달']))
                    fig_trend.add_trace(go.Bar(name='이번달', x=comp_df.index, y=comp_df['이번달']))
                    st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    st.info("비교할 과거 데이터가 부족합니다. 최소 2개월 이상의 데이터를 저장해 주세요.")