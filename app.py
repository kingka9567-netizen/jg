import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 구글 시트 연결 및 보안 설정 ---
def get_gspread_client():
    try:
        # Streamlit Secrets에 저장한 JSON 데이터를 불러옵니다.
        creds_dict = st.secrets["gcp_service_account"]
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"⚠️ Secrets 설정 오류: {e}")
        return None

def get_worksheet(sheet_name):
    client = get_gspread_client()
    if not client: return None
    try:
        # 구글 시트 이름이 'settlement_db'와 정확히 일치해야 합니다.
        sh = client.open("settlement_db")
        try:
            ws = sh.worksheet(sheet_name)
            return ws
        except gspread.exceptions.WorksheetNotFound:
            # 시트가 없으면 자동으로 제목(헤더)과 함께 생성합니다.
            if sheet_name == "mapping":
                new_ws = sh.add_worksheet(title="mapping", rows="100", cols="2")
                new_ws.append_row(["description", "category"])
                return new_ws
            else:
                new_ws = sh.add_worksheet(title="history", rows="100", cols="5")
                new_ws.append_row(["report_name", "date", "category", "income", "expense"])
                return new_ws
    except Exception as e:
        st.error(f"⚠️ 구글 시트 접속 실패: 'settlement_db' 시트를 생성하고 서비스 계정 이메일을 초대했는지 확인하세요.")
        return None

# --- 2. 데이터 처리 및 학습 로직 (KeyError 방어형) ---
def load_mappings():
    ws = get_worksheet("mapping")
    if not ws: return {}
    data = ws.get_all_records()
    if not data: return {}
    
    df = pd.DataFrame(data)
    # 컬럼명이 정확히 있는지 확인하여 에러를 방지합니다.
    if 'description' in df.columns and 'category' in df.columns:
        return dict(zip(df['description'], df['category']))
    return {}

def save_mapping(desc, cat):
    ws = get_worksheet("mapping")
    if not ws: return
    try:
        cell = ws.find(desc)
        if cell:
            ws.update_cell(cell.row, 2, cat)
        else:
            ws.append_row([desc, cat])
    except:
        ws.append_row([desc, cat])

def save_report_to_gsheet(name, df):
    ws = get_worksheet("history")
    if not ws: return
    date_now = datetime.now().strftime("%Y-%m-%d")
    summary = df.groupby('카테고리').agg({'입금금액':'sum', '지출금액':'sum'}).reset_index()
    
    # 기존에 같은 이름으로 저장된 리포트가 있다면 삭제(업데이트 개념)
    try:
        cells = ws.findall(name)
        rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
        for r in rows_to_delete:
            ws.delete_rows(r)
    except:
        pass
        
    for _, row in summary.iterrows():
        ws.append_row([name, date_now, row['카테고리'], row['입금금액'], row['지출금액']])

# --- 3. 앱 메인 화면 구성 ---
st.set_page_config(page_title="HuckJun's 비즈니스 대시보드", layout="wide")
mapping_dict = load_mappings()
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
INVESTMENT_CATS = ["사입", "부자재", "광고비"]
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 프로젝트 관리")
    mode = st.radio("작업 선택", ["새 정산하기", "과거 내역 확인", "보관함 관리"])
    st.divider()
    st.header("⚙️ 분석 필터")
    exclude_cats = st.multiselect("정산 제외 항목", full_category_list, default=["기타", "생활 및 기타"])
    vat_rate = st.slider("부가세 예비비 (%)", 0, 10, 7)
    income_tax_rate = st.slider("소득세 예비비 (%)", 0, 45, 15)

# [모드 1] 새 정산하기
if mode == "새 정산하기":
    project_title = st.text_input("📝 이번 정산 프로젝트 제목", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    st.title(f"🚀 {project_title}")

    uploaded_files = st.file_uploader("엑셀 파일들을 선택하세요", type=["xlsx"], accept_multiple_files=True)
    all_dfs = []

    if uploaded_files:
        for i, file in enumerate(uploaded_files):
            with st.expander(f"📄 {file.name} 컬럼 설정", expanded=(i==0)):
                df_temp = pd.read_excel(file)
                options = ["없음"] + list(df_temp.columns)
                c1, c2, c3 = st.columns(3)
                d_col = c1.selectbox(f"내역 열 ({file.name})", options, key=f"d_{i}", index=1 if len(options)>1 else 0)
                e_col = c2.selectbox(f"지출 금액 ({file.name})", options, key=f"e_{i}", index=0)
                i_col = c3.selectbox(f"입금 금액 ({file.name})", options, key=f"i_{i}", index=0)
                
                # 데이터 가공 (KeyError 방지 로직)
                sub_df = pd.DataFrame()
                sub_df["내역"] = df_temp[d_col].astype(str) if d_col != "없음" else ["내역 없음"]*len(df_temp)
                sub_df["지출금액"] = pd.to_numeric(df_temp[e_col], errors='coerce').fillna(0) if e_col != "없음" else 0.0
                sub_df["입금금액"] = pd.to_numeric(df_temp[i_col], errors='coerce').fillna(0) if i_col != "없음" else 0.0
                all_dfs.append(sub_df)

        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
            
            # --- 필터링 및 계산 ---
            filtered_df = combined_df[~combined_df['카테고리'].isin(exclude_cats)].copy()
            total_in = filtered_df["입금금액"].sum()
            total_out = filtered_df["지출금액"].sum()
            raw_profit = total_in - total_out
            tax_val = (total_in * vat_rate/100) + (max(0, raw_profit) * income_tax_rate/100)
            net_profit = raw_profit - tax_val
            margin = (net_profit / total_in * 100) if total_in > 0 else 0

            # KPI 표시
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("총 매출", f"{total_in:,.0f}원")
            k2.metric("사업 지출", f"-{total_out:,.0f}원")
            k3.metric("세금 예비비", f"-{tax_val:,.0f}원")
            k4.metric("최종 순이익", f"{net_profit:,.0f}원", f"{margin:.1f}%")

            st.divider()
            
            # 데이터 편집기
            st.subheader("📝 상세 내역 수정")
            edited_df = st.data_editor(
                combined_df, 
                column_config={"카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list)},
                use_container_width=True,
                key="main_editor"
            )
            
            if st.button("💾 구글 시트에 이 정산 내역 보관", type="primary"):
                save_report_to_gsheet(project_title, edited_df)
                st.success(f"'{project_title}' 내역이 구글 시트에 안전하게 보관되었습니다!")

            # 분류 학습 섹션
            uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
            if len(uncl) > 0:
                st.divider()
                st.warning(f"🔎 미분류 내역 {len(uncl)}건 남음")
                target = uncl[0]
                with st.container(border=True):
                    st.write(f"현재 분류 학습 항목: **{target}**")
                    sc1, sc2, sc3 = st.columns([2, 2, 1])
                    sel = sc1.selectbox("카테고리 선택", full_category_list + ["(직접 입력)"], key="bot_sel")
                    val = sc2.text_input("새 카테고리 이름") if sel == "(직접 입력)" else ""
                    if sc3.button("학습 및 기억하기", use_container_width=True):
                        save_mapping(target, val if sel == "(직접 입력)" else sel)
                        st.rerun()

            # 시각화
            st.subheader("📊 지출 분석 요약")
            g1, g2 = st.columns(2)
            exp_only = edited_df[~edited_df['카테고리'].isin(["입금"] + exclude_cats)]
            exp_only["지출성격"] = exp_only["카테고리"].apply(lambda x: "투자 (성장)" if x in INVESTMENT_CATS else "소비 (운영)")
            
            with g1:
                st.plotly_chart(px.pie(exp_only, values="지출금액", names="지출성격", title="투자 vs 소비 비중"), use_container_width=True)
            with g2:
                fig_gauge = go.Figure(go.Indicator(mode="gauge+number", value=margin, title={'text': "최종 수익률 (%)"},
                                                 gauge={'axis':{'range':[None, 50]}, 'bar':{'color':"darkblue"}}))
                st.plotly_chart(fig_gauge, use_container_width=True)

# [모드 2] 과거 내역 확인
elif mode == "과거 내역 확인":
    st.title("📜 보관된 정산 보고서")
    ws = get_worksheet("history")
    if ws:
        data = ws.get_all_records()
        if data:
            df_h = pd.DataFrame(data)
            report_names = df_h['report_name'].unique()
            selected = st.selectbox("불러올 보고서 선택", report_names)
            
            report_df = df_h[df_h['report_name'] == selected]
            st.table(report_df[['category', 'income', 'expense']])
        else:
            st.info("보관함이 비어 있습니다.")

# [모드 3] 보관함 관리
elif mode == "보관함 관리":
    st.title("🛠️ 정산 프로젝트 관리 (삭제/수정)")
    ws = get_worksheet("history")
    if ws:
        data = ws.get_all_records()
        if data:
            df_h = pd.DataFrame(data)
            report_names = df_h['report_name'].unique()
            target = st.selectbox("삭제할 프로젝트 선택", report_names)
            if st.button("🚨 이 프로젝트 영구 삭제", use_container_width=True):
                cells = ws.findall(target)
                rows_to_delete = sorted(list(set([c.row for c in cells])), reverse=True)
                for r in rows_to_delete: ws.delete_rows(r)
                st.success("삭제 완료")
                st.rerun()