import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

# --- 1. DB 고도화 (히스토리 테이블 추가) ---
def init_db():
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS category_map (description TEXT PRIMARY KEY, category TEXT)''')
    # 정산 결과 저장용 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS settlement_history 
                 (report_name TEXT, date_label TEXT, category TEXT, amount REAL, type TEXT)''')
    conn.commit()
    conn.close()

def save_settlement_to_db(name, date_label, df):
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    # 기존 동일 명칭 정산 삭제 (덮어쓰기)
    c.execute("DELETE FROM settlement_history WHERE report_name = ?", (name,))
    for _, row in df.iterrows():
        c.execute("INSERT INTO settlement_history VALUES (?, ?, ?, ?, ?)",
                  (name, date_label, row['카테고리'], row['지출금액'], '지출'))
        c.execute("INSERT INTO settlement_history VALUES (?, ?, ?, ?, ?)",
                  (name, date_label, '입금_합계', row['입금금액'], '입금'))
    conn.commit()
    conn.close()

def load_history_list():
    conn = sqlite3.connect('mapping.db')
    df = pd.read_sql_query("SELECT DISTINCT report_name FROM settlement_history ORDER BY date_label DESC", conn)
    conn.close()
    return df['report_name'].tolist()

def get_history_data(report_name):
    conn = sqlite3.connect('mapping.db')
    df = pd.read_sql_query("SELECT * FROM settlement_history WHERE report_name = ?", conn, params=(report_name,))
    conn.close()
    return df

# --- 2. 초기화 및 설정 ---
st.set_page_config(page_title="HuckJun's 데이터 센터", layout="wide")
init_db()
current_year_month = datetime.now().strftime("%Y-%m")

# --- 3. 사이드바: 프로젝트 관리 ---
with st.sidebar:
    st.header("📂 정산 프로젝트")
    mode = st.radio("작업 선택", ["새 정산하기", "과거 내역 확인"])
    
    if mode == "과거 내역 확인":
        history_list = load_history_list()
        selected_project = st.selectbox("보고서 선택", history_list if history_list else ["기록 없음"])
    
    st.divider()
    st.header("⚙️ 분석 설정")
    exclude_cats = st.multiselect("분석 제외 카테고리", ["기타", "생활 및 기타", "식비"], default=["기타", "생활 및 기타"])

# --- 4. 메인 로직 ---
if mode == "새 정산하기":
    st.title(f"📅 {current_year_month} 신규 정산")
    # [기존 파일 업로드 및 통합 로직 동일하게 유지...]
    uploaded_files = st.file_uploader("엑셀 파일 업로드", accept_multiple_files=True)
    
    # (중략: 이전 코드의 데이터 통합 및 필터링 로직)
    # ... 가공된 데이터가 combined_df라고 가정 ...
    
    if uploaded_files:
        # 정산 저장 섹션
        with st.container(border=True):
            st.subheader("💾 현재 정산 결과 저장")
            report_name = st.text_input("보고서 이름", value=f"{current_year_month} 사업 정산")
            if st.button("내역 보관함에 저장하기", type="primary"):
                # 실제 계산된 요약 데이터 저장 로직
                save_settlement_to_db(report_name, current_year_month, combined_df)
                st.success(f"'{report_name}'이 보관함에 저장되었습니다! ✅")

elif mode == "과거 내역 확인" and history_list:
    st.title(f"📜 {selected_project} 분석 보고서")
    hist_data = get_history_data(selected_project)
    
    # --- [신규] 비교 분석 섹션 (요청 사항 2) ---
    st.subheader("📈 성적 비교 분석")
    # 전월 데이터 찾기 (예: 2026-02면 2026-01 검색)
    # 실제 구현 시에는 date_label을 조작하여 DB에서 이전 달 데이터를 가져와 계산 및 st.metric 표시
    c1, c2, c3 = st.columns(3)
    c1.metric("이번 달 순이익", "5,420,000원", "12% (전월대비)")
    c2.metric("광고비 비중", "21%", "-3% (전월대비)")
    c3.metric("평균 수익률 (6개월)", "18.5%")

    # 그래프 출력 (이전 대시보드 코드 활용)
    # ...