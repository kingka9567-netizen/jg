import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 구글 시트 연결 설정 (안전장치 강화) ---
def get_gspread_client():
    try:
        creds_dict = st.secrets["gcp_service_account"]
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"❌ 구글 보안 열쇠(Secrets) 설정에 문제가 있습니다: {e}")
        return None

def get_worksheet(sheet_name):
    client = get_gspread_client()
    if not client: return None
    try:
        # ⚠️ 중요: 구글 시트 이름이 'settlement_db'여야 합니다.
        sh = client.open("settlement_db") 
        try:
            return sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            if sheet_name == "mapping":
                ws = sh.add_worksheet(title="mapping", rows="100", cols="2")
                ws.append_row(["description", "category"]) # 헤더 추가
                return ws
            else:
                ws = sh.add_worksheet(title="history", rows="100", cols="5")
                ws.append_row(["report_name", "date", "category", "income", "expense"]) # 헤더 추가
                return ws
    except Exception as e:
        st.error(f"❌ 구글 시트 'settlement_db'를 찾을 수 없습니다. 시트 이름을 확인하고 서비스 계정을 초대했는지 확인하세요.")
        return None

# --- 2. 데이터 처리 함수 ---
def load_mappings():
    ws = get_worksheet("mapping")
    if not ws: return {}
    data = ws.get_all_records()
    if not data: return {}
    df = pd.DataFrame(data)
    return dict(zip(df['description'], df['category']))

def save_mapping(desc, cat):
    ws = get_worksheet("mapping")
    if not ws: return
    cell = ws.find(desc)
    if cell:
        ws.update_cell(cell.row, 2, cat)
    else:
        ws.append_row([desc, cat])

def save_report(name, df):
    ws = get_worksheet("history")
    if not ws: return
    date_now = datetime.now().strftime("%Y-%m-%d")
    summary = df.groupby('카테고리').agg({'입금금액':'sum', '지출금액':'sum'}).reset_index()
    
    # 기존 데이터 삭제 후 갱신
    cells = ws.findall(name)
    rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
    for r in rows_to_delete: ws.delete_rows(r)
        
    for _, row in summary.iterrows():
        ws.append_row([name, date_now, row['카테고리'], row['입금금액'], row['지출금액']])

# --- 3. 앱 화면 구성 ---
st.set_page_config(page_title="HuckJun's 클라우드 정산기", layout="wide")
mapping_dict = load_mappings()
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 프로젝트 관리")
    mode = st.radio("작업 선택", ["새 정산하기", "보관함 관리"])
    st.divider()
    exclude_cats = st.multiselect("정산 제외 항목", full_category_list, default=["기타", "생활 및 기타"])
    vat_rate = st.slider("부가세 (%)", 0, 10, 7)
    income_tax_rate = st.slider("소득세 (%)", 0, 45, 15)

if mode == "새 정산하기":
    project_title = st.text_input("📝 정산 제목", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    uploaded_files = st.file_uploader("엑셀 파일 선택", type=["xlsx"], accept_multiple_files=True)
    
    if uploaded_files:
        all_dfs = []
        for i, file in enumerate(uploaded_files):
            with st.expander(f"📄 {file.name}"):
                df_temp = pd.read_excel(file)
                options = ["없음"] + list(df_temp.columns)
                c1, c2, c3 = st.columns(3)
                d_col = c1.selectbox(f"내역 열", options, key=f"d_{i}", index=1)
                e_col = c2.selectbox(f"지출 열", options, key=f"e_{i}", index=0)
                i_col = c3.selectbox(f"입금 열", options, key=f"i_{i}", index=0)
                
                # [오류 수정 핵심 로직]
                sub_df = pd.DataFrame()
                sub_df["내역"] = df_temp[d_col].astype(str) if d_col != "없음" else ["없음"]*len(df_temp)
                # 컬럼명이 '없음'일 경우 pd.to_numeric을 건너뛰고 0으로 채움
                sub_df["지출금액"] = pd.to_numeric(df_temp[e_col], errors='coerce').fillna(0) if e_col != "없음" else 0.0
                sub_df["입금금액"] = pd.to_numeric(df_temp[i_col], errors='coerce').fillna(0) if i_col != "없음" else 0.0
                all_dfs.append(sub_df)

        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
            
            # --- 분석 및 요약 ---
            filtered_df = combined_df[~combined_df['카테고리'].isin(exclude_cats)].copy()
            total_in = filtered_df["입금금액"].sum()
            total_out = filtered_df["지출금액"].sum()
            raw_profit = total_in - total_out
            tax_val = (total_in * vat_rate/100) + (max(0, raw_profit) * income_tax_rate/100)
            net_profit = raw_profit - tax_val
            margin = (net_profit / total_in * 100) if total_in > 0 else 0

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("총 매출", f"{total_in:,.0f}원")
            k2.metric("사업 지출", f"-{total_out:,.0f}원")
            k3.metric("세금 예비비", f"-{tax_val:,.0f}원")
            k4.metric("최종 순이익", f"{net_profit:,.0f}원", f"{margin:.1f}%")

            st.divider()
            # 데이터 수정 및 저장
            edited_df = st.data_editor(combined_df, column_config={"카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list)}, use_container_width=True)
            
            if st.button("💾 구글 시트에 이 정산 내역 저장", type="primary"):
                save_report(project_title, edited_df)
                st.success("구글 시트에 성공적으로 보관되었습니다! ✅")

            # 분류 학습
            uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
            if len(uncl) > 0:
                st.info(f"🔎 미분류 내역 {len(uncl)}건 남음")
                target = uncl[0]
                with st.container(border=True):
                    st.write(f"분류 학습: **{target}**")
                    sc1, sc2, sc3 = st.columns([2, 2, 1])
                    sel = sc1.selectbox("카테고리", full_category_list + ["(직접 입력)"], key="learn_sel")
                    val = sc2.text_input("새 분류") if sel == "(직접 입력)" else ""
                    if sc3.button("기억하기"):
                        save_mapping(target, val if sel == "(직접 입력)" else sel)
                        st.rerun()

elif mode == "보관함 관리":
    st.title("🛠️ 정산 보관함 관리")
    ws = get_worksheet("history")
    if ws:
        data = ws.get_all_records()
        if data:
            df_hist = pd.DataFrame(data)
            reports = df_hist['report_name'].unique()
            target = st.selectbox("관리할 프로젝트 선택", reports)
            if st.button("🚨 이 프로젝트 삭제", use_container_width=True):
                # 삭제 로직 (sh.findall 사용)
                cells = ws.findall(target)
                rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
                for r in rows_to_delete: ws.delete_rows(r)
                st.success("삭제되었습니다.")
                st.rerun()