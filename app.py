import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

# --- 1. DB 고도화 (프로젝트 관리용) ---
def init_db():
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS category_map (description TEXT PRIMARY KEY, category TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settlement_history 
                 (report_name TEXT, date_label TEXT, category TEXT, amount REAL, type TEXT)''')
    conn.commit()
    conn.close()

def delete_project(name):
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute("DELETE FROM settlement_history WHERE report_name = ?", (name,))
    conn.commit()
    conn.close()

def rename_project(old_name, new_name):
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute("UPDATE settlement_history SET report_name = ? WHERE report_name = ?", (new_name, old_name))
    conn.commit()
    conn.close()

# (중략: 기존 save/load 함수들 유지)

# --- 2. 웹 화면 구성 ---
st.set_page_config(page_title="HuckJun's 통합 관리 시스템", layout="wide")
init_db()
current_month = datetime.now().strftime("%Y-%m")

# --- 3. 사이드바: 프로젝트 리스트 관리 ---
with st.sidebar:
    st.header("📂 정산 프로젝트 관리")
    mode = st.radio("작업 선택", ["새 정산하기", "보관함 관리 (이름 변경/삭제)"])
    
    conn = sqlite3.connect('mapping.db')
    history_list = pd.read_sql_query("SELECT DISTINCT report_name FROM settlement_history", conn)['report_name'].tolist()
    conn.close()

    if mode == "보관함 관리 (이름 변경/삭제)" and history_list:
        target_project = st.selectbox("관리할 프로젝트 선택", history_list)
        
        # 이름 바꾸기
        new_name = st.text_input("새 이름 입력", value=target_project)
        if st.button("이름 바꾸기"):
            rename_project(target_project, new_name)
            st.success("이름이 변경되었습니다!")
            st.rerun()
        
        st.divider()
        # 삭제하기
        if st.button("🚨 프로젝트 삭제", use_container_width=True):
            st.warning(f"'{target_project}' 내역을 정말 삭제하시겠습니까?")
            if st.button("정말 삭제함"):
                delete_project(target_project)
                st.success("삭제 완료")
                st.rerun()

# --- 4. 메인 화면 ---
if mode == "새 정산하기":
    # 제목을 사용자가 직접 수정 가능하게 함 (요청 사항)
    project_title = st.text_input("📝 이번 정산 프로젝트 제목", value=f"{current_month} 신규 정산")
    st.title(f"🚀 {project_title}")
    
    # [파일 업로드 및 정산 로직...]
    # (이전 코드와 동일하되, 저장 시 project_title을 사용)

# (이하 생략: 과거 내역 확인 탭에서도 수정된 제목으로 리스트 출력)