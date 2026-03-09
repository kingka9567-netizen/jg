import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 구글 시트 연결 설정 ---
def get_gspread_client():
    # Streamlit Secrets에서 보안 열쇠 로드
    creds_dict = st.secrets["gcp_service_account"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_worksheet(sheet_name):
    client = get_gspread_client()
    # 구글 시트 이름 (아까 만드신 이름과 정확히 일치해야 함)
    sh = client.open("settlement_db") 
    try:
        return sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        # 시트가 없으면 생성
        if sheet_name == "mapping":
            return sh.add_worksheet(title="mapping", rows="100", cols="2")
        else:
            return sh.add_worksheet(title="history", rows="100", cols="5")

# --- 2. 데이터 처리 함수 (구글 시트 버전) ---
def load_mappings():
    ws = get_worksheet("mapping")
    data = ws.get_all_records()
    if not data:
        return {}
    df = pd.DataFrame(data)
    return dict(zip(df['description'], df['category']))

def save_mapping(desc, cat):
    ws = get_worksheet("mapping")
    cell = ws.find(desc)
    if cell:
        ws.update_cell(cell.row, 2, cat)
    else:
        ws.append_row([desc, cat])

def save_report(name, df):
    ws = get_worksheet("history")
    date_now = datetime.now().strftime("%Y-%m-%d")
    summary = df.groupby('카테고리').agg({'입금금액':'sum', '지출금액':'sum'}).reset_index()
    
    # 기존 같은 이름 보고서 삭제 후 추가
    cells = ws.findall(name)
    rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
    for r in rows_to_delete:
        ws.delete_rows(r)
        
    for _, row in summary.iterrows():
        ws.append_row([name, date_now, row['카테고리'], row['입금금액'], row['지출금액']])

def delete_project(name):
    ws = get_worksheet("history")
    cells = ws.findall(name)
    rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
    for r in rows_to_delete:
        ws.delete_rows(r)

# --- 3. 앱 화면 구성 (기존 로직 유지) ---
st.set_page_config(page_title="HuckJun's 클라우드 정산기", layout="wide")
mapping_dict = load_mappings()
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 프로젝트 관리")
    mode = st.radio("작업 선택", ["새 정산하기", "과거 내역 확인", "보관함 편집"])
    st.divider()
    exclude_cats = st.multiselect("정산 제외 항목", full_category_list, default=["기타", "생활 및 기타"])
    vat_rate = st.slider("부가세 (%)", 0, 10, 7)
    income_tax_rate = st.slider("소득세 (%)", 0, 45, 15)

if mode == "새 정산하기":
    project_title = st.text_input("📝 정산 제목", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    st.title(f"🚀 {project_title}")
    
    uploaded_files = st.file_uploader("엑셀 파일 선택", type=["xlsx"], accept_multiple_files=True)
    if uploaded_files:
        all_dfs = []
        for i, file in enumerate(uploaded_files):
            with st.expander(f"📄 {file.name}"):
                df_temp = pd.read_excel(file)
                options = ["없음"] + list(df_temp.columns)
                c1, c2, c3 = st.columns(3)
                d_col = c1.selectbox(f"내역", options, key=f"d_{i}", index=1)
                e_col = c2.selectbox(f"지출", options, key=f"e_{i}", index=0)
                i_col = c3.selectbox(f"입금", options, key=f"i_{i}", index=0)
                
                sub_df = pd.DataFrame()
                sub_df["내역"] = df_temp[d_col].astype(str) if d_col != "없음" else ["없음"]*len(df_temp)
                sub_df["지출금액"] = pd.to_numeric(df_temp[e_col], errors='coerce').fillna(0)
                sub_df["입금금액"] = pd.to_numeric(df_temp[i_col], errors='coerce').fillna(0)
                all_dfs.append(sub_df)

        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
            
            # 지표 계산 및 시각화 (기존 코드와 동일)
            # ... [상단 KPI 및 그래프 로직] ...
            
            st.subheader("📝 상세 내역 수정")
            edited_df = st.data_editor(combined_df, use_container_width=True)
            
            if st.button("💾 구글 시트에 정산 저장", type="primary"):
                save_report(project_title, edited_df)
                st.success("구글 시트 저장 완료!")

            # 미분류 처리
            uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
            if len(uncl) > 0:
                target = uncl[0]
                with st.container(border=True):
                    st.write(f"분류 학습: **{target}**")
                    sc1, sc2, sc3 = st.columns([2, 2, 1])
                    sel = sc1.selectbox("카테고리", full_category_list + ["(직접 입력)"], key="learn_sel")
                    val = sc2.text_input("새 분류") if sel == "(직접 입력)" else ""
                    if sc3.button("학습하기"):
                        save_mapping(target, val if sel == "(직접 입력)" else sel)
                        st.rerun()

elif mode == "보관함 편집":
    st.title("🛠️ 구글 시트 데이터 관리")
    ws = get_worksheet("history")
    history_data = ws.get_all_records()
    if history_data:
        reports = list(set([r['report_name'] for r in history_data]))
        target = st.selectbox("삭제할 프로젝트 선택", reports)
        if st.button("🚨 프로젝트 삭제"):
            delete_project(target)
            st.rerun()