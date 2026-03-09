import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px

# --- 1. 설정 및 데이터베이스 창고 관리 ---
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금"]

def init_db():
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS category_map 
                 (description TEXT PRIMARY KEY, category TEXT)''')
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

# --- 2. 웹 화면 구성 ---
st.set_page_config(page_title="통합 사업 정산기", layout="wide")
st.title("📈 통합 사업 수익 분석 대시보드")

init_db()
mapping_dict = load_mappings()
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

# [수정] 여러 파일 업로드 가능하게 변경
uploaded_files = st.file_uploader("정산할 엑셀 파일들을 모두 선택하세요 (최대 3개 이상 가능)", type=["xlsx"], accept_multiple_files=True)

all_dfs = []

if uploaded_files:
    st.subheader("⚙️ 파일별 컬럼 설정")
    
    # 각 파일별로 컬럼을 맞추는 과정
    for i, file in enumerate(uploaded_files):
        with st.expander(f"파일 {i+1}: {file.name} 설정", expanded=True):
            df_temp = pd.read_excel(file)
            cols = list(df_temp.columns)
            
            c1, c2, c3 = st.columns(3)
            with c1:
                d_col = st.selectbox(f"내역 컬럼 ({file.name})", cols, key=f"d_{i}")
            with c2:
                e_col = st.selectbox(f"지출 금액 ({file.name})", cols, key=f"e_{i}")
            with c3:
                i_col = st.selectbox(f"입금 금액 ({file.name})", cols, key=f"i_{i}")
            
            # 필요한 컬럼만 추출하여 표준화
            if d_col and e_col and i_col:
                sub_df = df_temp[[d_col, e_col, i_col]].copy()
                sub_df.columns = ["내역", "지출금액", "입금금액"]
                sub_df["지출금액"] = pd.to_numeric(sub_df["지출금액"], errors='coerce').fillna(0)
                sub_df["입금금액"] = pd.to_numeric(sub_df["입금금액"], errors='coerce').fillna(0)
                sub_df["출처"] = file.name # 파일 이름 기록
                all_dfs.append(sub_df)

    if all_dfs:
        # 모든 파일 합치기
        combined_df = pd.concat(all_dfs, ignore_index=True)
        combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
        
        # --- 1단계: 상단 요약 (KPI) ---
        kpi_placeholder = st.empty()
        st.divider()
        
        # --- 2단계: 통합 데이터 편집기 (금액/카테고리 수정) ---
        st.subheader("📝 통합 내역 및 금액 수정")
        st.caption("💡 특정 파일의 택배비 등을 여기서 직접 수정하면 실시간으로 정산에 반영됩니다.")
        
        # 표에서 수정 가능하게 함
        edited_df = st.data_editor(
            combined_df,
            column_config={
                "카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list, required=True),
                "지출금액": st.column_config.NumberColumn("지출 금액(수정 가능)", format="%d원"),
                "입금금액": st.column_config.NumberColumn("입금 금액(수정 가능)", format="%d원"),
                "출처": st.column_config.TextColumn("파일 출처", disabled=True)
            },
            use_container_width=True,
            key="total_editor"
        )

        # 실시간 DB 저장
        if st.session_state.get("total_editor"):
            edits = st.session_state["total_editor"]["edited_rows"]
            if edits:
                for row_idx, changes in edits.items():
                    if "카테고리" in changes:
                        save_mapping(combined_df.iloc[int(row_idx)]["내역"], changes["카테고리"])
                st.toast("변경사항이 기록되었습니다. ✅")
                st.rerun()

        # --- 3단계: 분석 및 대시보드 ---
        income_sum = edited_df["입금금액"].sum()
        expense_sum = edited_df["지출금액"].sum()
        profit = income_sum - expense_sum
        margin = (profit / income_sum * 100) if income_sum > 0 else 0

        with kpi_placeholder.container():
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("총 입금 (전체)", f"{income_sum:,.0f}원")
            k2.metric("총 지출 (전체)", f"-{expense_sum:,.0f}원")
            k3.metric("최종 순이익", f"{profit:,.0f}원")
            k4.metric("수익률", f"{margin:.1f}%")

        # 그래프
        st.subheader("📊 통합 지출 비중")
        g1, g2 = st.columns(2)
        exp_summary = edited_df[edited_df["카테고리"] != "입금"].groupby("카테고리")["지출금액"].sum().reset_index()
        
        with g1:
            st.plotly_chart(px.pie(exp_summary, values="지출금액", names="카테고리", title="카테고리별 비중"), use_container_width=True)
        with g2:
            st.plotly_chart(px.bar(exp_summary.sort_values("지출금액", ascending=False), x="카테고리", y="지출금액", title="지출 순위"), use_container_width=True)

        # --- 4단계: 미분류 집중 처리 ---
        uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
        if len(uncl) > 0:
            st.divider()
            st.warning(f"🔎 미분류 내역 {len(uncl)}건 남음")
            target = uncl[0]
            with st.container(border=True):
                st.write(f"미분류 항목: **{target}**")
                sc1, sc2, sc3 = st.columns([2, 2, 1])
                with sc1:
                    s = st.selectbox("분류 선택", full_category_list + ["(직접 입력)"], key="bot_s")
                with sc2:
                    v = st.text_input("새 분류", key="bot_v") if s == "(직접 입력)" else ""
                with sc3:
                    st.write("")
                    if st.button("기억하기", type="primary", use_container_width=True):
                        final = v if s == "(직접 입력)" else s
                        if final:
                            save_mapping(target, final)
                            st.rerun()