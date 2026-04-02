import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 구글 시트 연결 및 설정 ---
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
            elif sheet_name == "raw_data": # 기존 history 대신 raw_data 탭 생성
                ws = sh.add_worksheet(title="raw_data", rows="1000", cols="6")
                ws.append_row(["report_name", "date", "내역", "지출금액", "입금금액", "카테고리"])
                return ws
    except Exception as e:
        st.error(f"⚠️ 구글 시트 접속 실패. 공유 설정 및 시트 이름을 확인하세요.")
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

def load_all_raw_data():
    ws = get_worksheet("raw_data")
    if not ws: return pd.DataFrame()
    data = ws.get_all_records()
    return pd.DataFrame(data)

def save_raw_report(name, df):
    ws = get_worksheet("raw_data")
    if not ws: return
    date_now = datetime.now().strftime("%Y-%m-%d")
    
    # 저장할 데이터 프레임 구조화
    df_to_save = df[['내역', '지출금액', '입금금액', '카테고리']].copy()
    df_to_save.insert(0, 'date', date_now)
    df_to_save.insert(0, 'report_name', name)
    
    # 기존 데이터를 불러와서 현재 덮어쓰는 리포트 이름만 제외하고 합침 (Bulk Update 방식)
    existing_data = ws.get_all_records()
    if existing_data:
        existing_df = pd.DataFrame(existing_data)
        existing_df = existing_df[existing_df['report_name'] != name] # 기존 동일 이름 삭제 효과
        final_df = pd.concat([existing_df, df_to_save], ignore_index=True)
    else:
        final_df = df_to_save
        
    # 시트 전체 갱신 (API 속도 최적화)
    ws.clear()
    ws.update([final_df.columns.values.tolist()] + final_df.values.tolist())

# --- 3. 공통 UI 및 계산 로직 ---
def calculate_metrics(df, v_rate, i_rate, exclude):
    f_df = df[~df['카테고리'].isin(exclude)].copy()
    t_in = f_df["입금금액"].sum()
    t_out = f_df["지출금액"].sum()
    r_profit = t_in - t_out
    tax = (t_in * v_rate/100) + (max(0, r_profit) * i_rate/100)
    n_profit = r_profit - tax
    margin = (n_profit / t_in * 100) if t_in > 0 else 0
    return t_in, t_out, tax, n_profit, margin, f_df

def display_live_editor(df_main, title, v_rate, i_rate, exclude_cats, full_cat_list):
    t_in, t_out, tax, n_profit, margin, f_df = calculate_metrics(df_main, v_rate, i_rate, exclude_cats)
    
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("총 매출", f"{t_in:,.0f}원")
    k2.metric("사업 지출", f"-{t_out:,.0f}원")
    k3.metric("세금 예비비", f"-{tax:,.0f}원")
    k4.metric("최종 순이익", f"{n_profit:,.0f}원", f"{margin:.1f}%")

    st.divider()
    st.subheader("📊 지출 분석 요약")
    col1, col2 = st.columns(2)
    exp_df = f_df[f_df['지출금액'] > 0].groupby('카테고리')['지출금액'].sum().reset_index().sort_values('지출금액', ascending=False)
    with col1:
        if not exp_df.empty:
            st.plotly_chart(px.bar(exp_df, x='지출금액', y='카테고리', orientation='h', title="카테고리별 지출", text_auto=',.0f'), use_container_width=True)
    with col2:
        if not exp_df.empty:
            st.plotly_chart(px.pie(exp_df, values='지출금액', names='카테고리', title="지출 비중 (%)", hole=0.4), use_container_width=True)

    st.subheader("📝 상세 내역 라이브 수정")
    # 라이브 에디터
    edited = st.data_editor(df_main, column_config={"카테고리": st.column_config.SelectboxColumn("카테고리", options=full_cat_list)}, use_container_width=True)
    
    if st.button("💾 데이터 구글 시트에 덮어쓰기/저장", type="primary"):
        save_raw_report(title, edited)
        st.success(f"'{title}' 데이터가 구글 시트에 안전하게 보존되었습니다!")

    uncl = edited[edited['카테고리'] == "미분류"]["내역"].unique()
    if len(uncl) > 0:
        with st.container(border=True):
            st.write(f"미분류 학습: **{uncl[0]}**")
            sc1, sc2, sc3 = st.columns([2, 2, 1])
            sel = sc1.selectbox("카테고리", full_cat_list + ["(직접 입력)"], key=f"l_sel_{title}")
            val = sc2.text_input("직접 입력", key=f"l_val_{title}") if sel == "(직접 입력)" else ""
            if sc3.button("기억하기", key=f"l_btn_{title}"):
                save_mapping(uncl[0], val if sel == "(직접 입력)" else sel)
                st.rerun()

# --- 4. 앱 메인 화면 구성 ---
st.set_page_config(page_title="HuckJun's 비즈니스 대시보드", layout="wide")
mapping_dict = load_mappings()
DEFAULT_CATS = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
full_cat_list = sorted(list(set(DEFAULT_CATS + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 메뉴")
    mode = st.radio("이동", ["새 정산하기", "과거 내역 열람 및 수정", "지출 추세 및 원인 분석"])
    st.divider()
    exclude_cats = st.multiselect("분석 제외 카테고리", full_cat_list, default=["기타", "생활 및 기타"])
    v_rate = st.slider("부가세 예비비 (%)", 0, 10, 7)
    i_rate = st.slider("소득세 예비비 (%)", 0, 45, 15)

if mode == "새 정산하기":
    title = st.text_input("📝 새 정산 제목 설정", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    files = st.file_uploader("엑셀 파일 업로드", type=["xlsx"], accept_multiple_files=True)
    
    if files:
        all_data = []
        for i, f in enumerate(files):
            with st.expander(f"📄 {f.name} 설정"):
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
            display_live_editor(df_main, title, v_rate, i_rate, exclude_cats, full_cat_list)

elif mode == "과거 내역 열람 및 수정":
    st.title("📂 보관된 정산 내역 관리")
    df_raw = load_all_raw_data()
    
    if not df_raw.empty:
        reports = sorted(df_raw['report_name'].unique(), reverse=True)
        selected_report = st.selectbox("불러올 정산 리포트 선택", reports)
        
        st.info(f"💡 '{selected_report}'의 원본 데이터를 불러왔습니다. 아래에서 카테고리를 수정하고 저장하면 지표가 업데이트됩니다.")
        
        # 선택된 리포트의 데이터만 추출
        df_selected = df_raw[df_raw['report_name'] == selected_report].copy()
        display_live_editor(df_selected, selected_report, v_rate, i_rate, exclude_cats, full_cat_list)
        
        st.divider()
        if st.button("🚨 이 정산 리포트 전체 삭제", type="secondary"):
            ws = get_worksheet("raw_data")
            existing_df = pd.DataFrame(ws.get_all_records())
            filtered_df = existing_df[existing_df['report_name'] != selected_report]
            ws.clear()
            if not filtered_df.empty:
                ws.update([filtered_df.columns.values.tolist()] + filtered_df.values.tolist())
            else:
                ws.append_row(["report_name", "date", "내역", "지출금액", "입금금액", "카테고리"])
            st.success("삭제되었습니다.")
            st.rerun()
    else:
        st.warning("저장된 내역이 없습니다. '새 정산하기'에서 엑셀을 업로드하고 데이터를 저장해 주세요.")

elif mode == "지출 추세 및 원인 분석":
    st.title("📈 기간별 대조 및 원인 분석")
    df_raw = load_all_raw_data()
    
    if not df_raw.empty:
        reports = sorted(df_raw['report_name'].unique(), reverse=True)
        if len(reports) < 2:
            st.warning("비교를 위해 최소 2개 이상의 정산 리포트가 필요합니다.")
        else:
            c1, c2 = st.columns(2)
            target_a = c1.selectbox("기준 데이터 (과거)", reports, index=min(1, len(reports)-1))
            target_b = c2.selectbox("대조 데이터 (최신)", reports, index=0)
            
            df_a = df_raw[df_raw['report_name'] == target_a]
            df_b = df_raw[df_raw['report_name'] == target_b]
            
            m_a = calculate_metrics(df_a, v_rate, i_rate, exclude_cats)
            m_b = calculate_metrics(df_b, v_rate, i_rate, exclude_cats)
            
            st.subheader("1️⃣ 주요 지표 증감")
            k1, k2, k3 = st.columns(3)
            k1.metric("총 매출 변화", f"{m_b[0]:,.0f}원", f"{m_b[0] - m_a[0]:+,.0f}원")
            k2.metric("총 지출 변화", f"-{m_b[1]:,.0f}원", f"{(m_b[1] - m_a[1])*-1:+,.0f}원", delta_color="inverse")
            k3.metric("최종 순이익 변화", f"{m_b[3]:,.0f}원", f"{m_b[3] - m_a[3]:+,.0f}원")
            
            st.divider()
            
            st.subheader("2️⃣ 카테고리별 지출 증감 대조")
            exp_a = m_a[5][m_a[5]['지출금액'] > 0].groupby('카테고리')['지출금액'].sum()
            exp_b = m_b[5][m_b[5]['지출금액'] > 0].groupby('카테고리')['지출금액'].sum()
            
            comp_df = pd.DataFrame({target_a: exp_a, target_b: exp_b}).fillna(0)
            comp_df['차이'] = comp_df[target_b] - comp_df[target_a]
            
            fig_comp = go.Figure()
            fig_comp.add_trace(go.Bar(name=target_a, x=comp_df.index, y=comp_df[target_a]))
            fig_comp.add_trace(go.Bar(name=target_b, x=comp_df.index, y=comp_df[target_b]))
            fig_comp.update_layout(barmode='group')
            st.plotly_chart(fig_comp, use_container_width=True)
            
            st.divider()
            
            # Drill-down: 원인 분석
            st.subheader("3️⃣ 항목별 원인 분석 (상세 내역 확인)")
            st.write("특정 카테고리에서 지출 차이가 발생한 원인을 원본 내역을 통해 확인합니다.")
            
            drill_cat = st.selectbox("상세 내역을 확인할 카테고리 선택", comp_df.index)
            
            d1, d2 = st.columns(2)
            with d1:
                st.markdown(f"**{target_a} ({drill_cat}) 내역**")
                raw_a = df_a[(df_a['카테고리'] == drill_cat) & (df_a['지출금액'] > 0)][['내역', '지출금액']]
                st.dataframe(raw_a, use_container_width=True)
            with d2:
                st.markdown(f"**{target_b} ({drill_cat}) 내역**")
                raw_b = df_b[(df_b['카테고리'] == drill_cat) & (df_b['지출금액'] > 0)][['내역', '지출금액']]
                st.dataframe(raw_b, use_container_width=True)

    else:
        st.info("데이터가 없습니다. 먼저 정산을 진행해 주세요.")