import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

# --- 1. 데이터베이스 기능 (분류 규칙 + 정산 기록) ---
def init_db():
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS category_map (description TEXT PRIMARY KEY, category TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settlement_history 
                 (report_name TEXT, date_label TEXT, category TEXT, amount_in REAL, amount_out REAL)''')
    conn.commit()
    conn.close()

def load_mappings():
    conn = sqlite3.connect('mapping.db')
    df = pd.read_sql_query("SELECT * FROM category_map", conn)
    conn.close()
    return dict(zip(df['description'], df['category']))

def save_mapping(desc, cat):
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO category_map VALUES (?, ?)", (desc, cat))
    conn.commit()
    conn.close()

def save_settlement_report(name, df):
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute("DELETE FROM settlement_history WHERE report_name = ?", (name,))
    date_now = datetime.now().strftime("%Y-%m-%d")
    summary = df.groupby('카테고리').agg({'입금금액':'sum', '지출금액':'sum'}).reset_index()
    for _, row in summary.iterrows():
        c.execute("INSERT INTO settlement_history VALUES (?, ?, ?, ?, ?)",
                  (name, date_now, row['카테고리'], row['입금금액'], row['지출금액']))
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

# --- 2. 페이지 설정 및 사이드바 ---
st.set_page_config(page_title="HuckJun's 비즈니스 대시보드", layout="wide")
init_db()
mapping_dict = load_mappings()
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
INVESTMENT_CATS = ["사입", "부자재", "광고비"]
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

with st.sidebar:
    st.header("📂 프로젝트 관리")
    mode = st.radio("작업 선택", ["새 정산하기", "과거 내역 확인", "보관함 편집"])
    
    st.divider()
    st.header("⚙️ 분석 필터")
    exclude_cats = st.multiselect("정산 제외 항목", full_category_list, default=["기타", "생활 및 기타"])
    
    st.divider()
    st.header("🏦 세금 설정")
    vat_rate = st.slider("부가세 (%)", 0, 10, 7)
    income_tax_rate = st.slider("소득세 (%)", 0, 45, 15)

# --- 3. [모드 1] 새 정산하기 (메인 기능) ---
if mode == "새 정산하기":
    project_title = st.text_input("📝 이번 정산 제목 설정", value=f"{datetime.now().strftime('%Y-%m')} 정산")
    st.title(f"🚀 {project_title}")

    uploaded_files = st.file_uploader("엑셀 파일들을 선택하세요", type=["xlsx"], accept_multiple_files=True)
    all_dfs = []

    if uploaded_files:
        st.subheader("⚙️ 파일별 컬럼 설정")
        for i, file in enumerate(uploaded_files):
            with st.expander(f"📄 {file.name} 매핑", expanded=(i==0)):
                df_temp = pd.read_excel(file)
                options = ["없음"] + list(df_temp.columns)
                c1, c2, c3 = st.columns(3)
                d_col = c1.selectbox(f"내역 ({file.name})", options, key=f"d_{i}", index=1 if len(options)>1 else 0)
                e_col = c2.selectbox(f"지출 ({file.name})", options, key=f"e_{i}", index=2 if len(options)>2 else 0)
                i_col = c3.selectbox(f"입금 ({file.name})", options, key=f"i_{i}", index=0)
                
                sub_df = pd.DataFrame()
                sub_df["내역"] = df_temp[d_col].astype(str) if d_col != "없음" else ["없음"]*len(df_temp)
                sub_df["지출금액"] = pd.to_numeric(df_temp[e_col], errors='coerce').fillna(0) if e_col != "없음" else 0
                sub_df["입금금액"] = pd.to_numeric(df_temp[i_col], errors='coerce').fillna(0) if i_col != "없음" else 0
                sub_df["출처"] = file.name
                all_dfs.append(sub_df)

        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
            
            # --- 핵심 지표 계산 ---
            filtered_df = combined_df[~combined_df['카테고리'].isin(exclude_cats)].copy()
            total_in = filtered_df["입금금액"].sum()
            total_out = filtered_df["지출금액"].sum()
            raw_profit = total_in - total_out
            tax_val = (total_in * vat_rate/100) + (max(0, raw_profit) * income_tax_rate/100)
            net_val = raw_profit - tax_val
            margin = (net_val / total_in * 100) if total_in > 0 else 0

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("총 매출", f"{total_in:,.0f}원")
            k2.metric("사업 지출", f"-{total_out:,.0f}원")
            k3.metric("세금 예비비", f"-{tax_val:,.0f}원")
            k4.metric("최종 순이익", f"{net_val:,.0f}원", f"{margin:.1f}%")

            st.divider()
            # --- 데이터 편집 및 저장 ---
            st.subheader("📝 상세 내역 수정 및 보관")
            edited_df = st.data_editor(combined_df, column_config={
                "카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list)
            }, use_container_width=True, key="main_editor")
            
            if st.button("💾 이 정산 내역을 보관함에 저장", type="primary"):
                save_settlement_report(project_title, edited_df)
                st.success(f"'{project_title}' 저장 완료!")

            # --- [부활] 미분류 집중 처리 섹션 ---
            uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
            if len(uncl) > 0:
                st.divider()
                st.warning(f"🔎 미분류 내역 {len(uncl)}건이 남았습니다.")
                target = uncl[0]
                with st.container(border=True):
                    st.write(f"현재 분류 항목: **{target}**")
                    sc1, sc2, sc3 = st.columns([2, 2, 1])
                    with sc1:
                        sel = st.selectbox("카테고리 선택", full_category_list + ["(직접 입력)"], key="bot_sel")
                    with sc2:
                        val = st.text_input("새 카테고리") if sel == "(직접 입력)" else ""
                    with sc3:
                        st.write("")
                        if st.button("학습 및 저장", use_container_width=True):
                            save_mapping(target, val if sel == "(직접 입력)" else sel)
                            st.rerun()

            # --- 시각화 그래프 ---
            st.subheader("📊 지출 및 수익 분석")
            g1, g2 = st.columns(2)
            exp_only = edited_df[~edited_df['카테고리'].isin(["입금"] + exclude_cats)]
            with g1:
                st.plotly_chart(px.pie(exp_only, values="지출금액", names="카테고리", title="지출 비중"), use_container_width=True)
            with g2:
                fig_gauge = go.Figure(go.Indicator(mode="gauge+number", value=margin, title={'text': "수익률 (%)"},
                                                 gauge={'axis':{'range':[None, 50]}, 'bar':{'color':"darkblue"}}))
                st.plotly_chart(fig_gauge, use_container_width=True)

# --- 4. [모드 2 & 3] 내역 확인 및 보관함 편집 ---
elif mode == "과거 내역 확인":
    st.title("📜 과거 정산 보고서")
    conn = sqlite3.connect('mapping.db')
    reports = pd.read_sql_query("SELECT DISTINCT report_name FROM settlement_history", conn)['report_name'].tolist()
    conn.close()
    if reports:
        selected = st.selectbox("보고서 선택", reports)
        st.write(f"📂 '{selected}' 데이터를 분석합니다.")
    else:
        st.info("저장된 정산 내역이 없습니다.")

elif mode == "보관함 편집":
    st.title("🛠️ 보관함 프로젝트 관리")
    conn = sqlite3.connect('mapping.db')
    reports = pd.read_sql_query("SELECT DISTINCT report_name FROM settlement_history", conn)['report_name'].tolist()
    conn.close()
    if reports:
        target = st.selectbox("관리할 프로젝트", reports)
        c1, c2 = st.columns(2)
        new_name = c1.text_input("변경할 이름", value=target)
        if c1.button("이름 바꾸기"):
            rename_project(target, new_name)
            st.rerun()
        if c2.button("🚨 프로젝트 삭제", use_container_width=True):
            delete_project(target)
            st.rerun()