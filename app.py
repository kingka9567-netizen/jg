import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px # 그래프 시각화를 위해 추가

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
st.set_page_config(page_title="HuckJun의 스마트 정산기", layout="wide")
st.title("📊 사업 수익 분석 대시보드")

init_db()
mapping_dict = load_mappings()
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

uploaded_file = st.file_uploader("정산할 엑셀 파일을 업로드하세요", type=["xlsx"])

if uploaded_file:
    df = pd.read_excel(uploaded_file)
    cols = list(df.columns)
    
    # 1. 컬럼 매핑 설정 (자동 추측 포함)
    col_c1, col_c2, col_c3 = st.columns(3)
    with col_c1:
        desc_col = st.selectbox("내역(설명) 컬럼", cols, index=0)
    with col_c2:
        # '이용금액' 또는 '금액' 포함 컬럼 찾기
        exp_idx = next((i for i, c in enumerate(cols) if "금액" in str(c) and "입금" not in str(c)), 0)
        expense_col = st.selectbox("지출 금액 컬럼", cols, index=exp_idx)
    with col_c3:
        # '입금' 포함 컬럼 찾기
        inc_idx = next((i for i, c in enumerate(cols) if "입금" in str(c)), 0)
        income_col = st.selectbox("입금 금액 컬럼", cols, index=inc_idx)

    if desc_col and expense_col and income_col:
        # 데이터 정리: 숫자가 아닌 값은 0으로 처리
        df[expense_col] = pd.to_numeric(df[expense_col], errors='coerce').fillna(0)
        df[income_col] = pd.to_numeric(df[income_col], errors='coerce').fillna(0)
        df['카테고리'] = df[desc_col].map(mapping_dict).fillna("미분류")
        
        # --- [신규] 1단계: 핵심 지표 요약 (KPI) ---
        # 사용자가 표에서 수정한 금액을 반영하기 위해 아래 edited_df 이후에 계산하지만, 
        # 화면 구성을 위해 미리 공간을 만듭니다.
        kpi_area = st.container()
        
        st.divider()
        
        # --- 2단계: 데이터 편집기 (금액 및 카테고리 수정) ---
        st.subheader("📝 상세 내역 및 금액 수정")
        st.info("💡 택배비 등 금액 조정이 필요한 경우 '지출 금액' 칸을 직접 수정하세요.")
        
        edited_df = st.data_editor(
            df,
            column_config={
                "카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list, required=True),
                expense_col: st.column_config.NumberColumn("지출 금액", format="%d원"),
                income_col: st.column_config.NumberColumn("입금 금액", format="%d원")
            },
            use_container_width=True,
            key="main_editor"
        )

        # 실시간 DB 저장 (카테고리 매핑)
        if st.session_state.get("main_editor"):
            edits = st.session_state["main_editor"]["edited_rows"]
            if edits:
                for row_idx, changes in edits.items():
                    if "카테고리" in changes:
                        save_mapping(df.iloc[int(row_idx)][desc_col], changes["카테고리"])
                st.toast("변경사항이 기록되었습니다. ✅")
                st.rerun()

        # --- 3단계: 수익 분석 및 그래프 ---
        # 수정된 데이터(edited_df)를 기준으로 합계 계산
        total_income = edited_df[income_col].sum()
        total_expense = edited_df[expense_col].sum()
        net_profit = total_income - total_expense
        profit_margin = (net_profit / total_income * 100) if total_income > 0 else 0

        # 상단 KPI 영역 채우기
        with kpi_area:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("총 입금액", f"{total_income:,.0f}원")
            c2.metric("총 지출액", f"-{total_expense:,.0f}원")
            c3.metric("최종 순이익", f"{net_profit:,.0f}원", delta_color="normal")
            c4.metric("수익률", f"{profit_margin:.1f}%")

        # 그래프 섹션
        st.subheader("📊 지출 비중 분석")
        g1, g2 = st.columns(2)
        
        # 지출만 필터링 (입금 제외)
        exp_only = edited_df[edited_df["카테고리"] != "입금"]
        cat_summary = exp_only.groupby("카테고리")[expense_col].sum().reset_index()
        
        with g1:
            fig_pie = px.pie(cat_summary, values=expense_col, names='카테고리', title="카테고리별 지출 비중")
            st.plotly_chart(fig_pie, use_container_width=True)
            
        with g2:
            fig_bar = px.bar(cat_summary.sort_values(expense_col, ascending=False), 
                             x='카테고리', y=expense_col, title="지출 금액 순위")
            st.plotly_chart(fig_bar, use_container_width=True)

        # --- 4단계: 집중 분류 섹션 ---
        unclassified = edited_df[edited_df['카테고리'] == "미분류"][desc_col].unique()
        if len(unclassified) > 0:
            st.divider()
            st.warning(f"🔎 분류가 필요한 내역이 {len(unclassified)}건 남았습니다.")
            target = unclassified[0]
            with st.container(border=True):
                st.write(f"항목: **{target}**")
                sc1, sc2, sc3 = st.columns([2, 2, 1])
                with sc1:
                    sel = st.selectbox("카테고리 선택", full_category_list + ["(직접 입력)"], key="bot_sel")
                with sc2:
                    val = st.text_input("새 카테고리", key="bot_in") if sel == "(직접 입력)" else ""
                with sc3:
                    st.write("")
                    if st.button("학습하기", type="primary"):
                        final = val if sel == "(직접 입력)" else sel
                        if final:
                            save_mapping(target, final)
                            st.rerun()