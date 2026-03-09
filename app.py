import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go

# --- 1. DB 및 설정 ---
DEFAULT_CATEGORIES = ["사입", "부자재", "세금", "택배비", "광고비", "생활 및 기타", "식비", "입금", "기타"]
INVESTMENT_CATS = ["사입", "부자재", "광고비"] # 매출을 일으키는 투자성 지출

def init_db():
    conn = sqlite3.connect('mapping.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS category_map (description TEXT PRIMARY KEY, category TEXT)''')
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

# --- 2. 페이지 설정 ---
st.set_page_config(page_title="HuckJun's 비즈니스 대시보드", layout="wide")
init_db()
mapping_dict = load_mappings()
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

# --- 3. 사이드바 제어 (필터 및 세금 설정) ---
with st.sidebar:
    st.header("⚙️ 정산 설정")
    # 정산에서 제외할 카테고리 선택 (요청 사항 2)
    exclude_cats = st.multiselect(
        "정산에서 제외할 카테고리",
        full_category_list,
        default=["기타", "생활 및 기타"]
    )
    
    st.divider()
    st.header("🏦 세금 예비비 설정")
    st.caption("국세청 권장 비율을 참고하여 설정하세요.")
    vat_rate = st.slider("부가세 예비비 (%)", 0, 10, 7, help="매출의 약 10%이나 매입세액공제를 고려해 보통 5~8%를 권장합니다.")
    income_tax_rate = st.slider("소득세 예비비 (%)", 0, 45, 15, help="누진세율 구간(6~45%)에 맞춰 설정하세요.")

# --- 4. 파일 업로드 및 데이터 통합 ---
st.title("🚀 사업 수익 정밀 분석 시스템")
uploaded_files = st.file_uploader("정산 엑셀 파일들을 선택하세요 (현대/삼성/기업 등)", type=["xlsx"], accept_multiple_files=True)

all_dfs = []
if uploaded_files:
    for i, file in enumerate(uploaded_files):
        with st.expander(f"📄 {file.name} 컬럼 매핑", expanded=(i==0)):
            df_temp = pd.read_excel(file)
            options = ["없음"] + list(df_temp.columns)
            c1, c2, c3 = st.columns(3)
            d_col = c1.selectbox(f"내역 ({file.name})", options, key=f"d_{i}", index=1)
            e_col = c2.selectbox(f"지출 ({file.name})", options, key=f"e_{i}", index=0)
            i_col = c3.selectbox(f"입금 ({file.name})", options, key=f"i_{i}", index=0)
            
            sub_df = pd.DataFrame()
            sub_df["내역"] = df_temp[d_col].astype(str) if d_col != "없음" else ["내역 없음"]*len(df_temp)
            sub_df["지출금액"] = pd.to_numeric(df_temp[e_col], errors='coerce').fillna(0) if e_col != "없음" else 0
            sub_df["입금금액"] = pd.to_numeric(df_temp[i_col], errors='coerce').fillna(0) if i_col != "없음" else 0
            sub_df["출처"] = file.name
            all_dfs.append(sub_df)

    if all_dfs:
        combined_df = pd.concat(all_dfs, ignore_index=True)
        combined_df['카테고리'] = combined_df["내역"].map(mapping_dict).fillna("미분류")
        
        # --- 5. 데이터 필터링 및 계산 ---
        # 사용자가 선택한 제외 카테고리 적용
        filtered_df = combined_df[~combined_df['카테고리'].isin(exclude_cats)].copy()
        
        # 상세 편집기 (수정 가능)
        st.subheader("📝 통합 데이터 수정")
        edited_df = st.data_editor(
            filtered_df,
            column_config={
                "카테고리": st.column_config.SelectboxColumn("카테고리", options=full_category_list),
                "지출금액": st.column_config.NumberColumn("지출", format="%d원"),
                "입금금액": st.column_config.NumberColumn("입금", format="%d원")
            },
            use_container_width=True,
            key="main_editor"
        )

        # 수익 계산 로직
        total_in = edited_df["입금금액"].sum()
        total_out = edited_df["지출금액"].sum()
        raw_profit = total_in - total_out
        
        # 세금 계산 (제안 A 적용)
        vat_reserve = total_in * (vat_rate / 100)
        income_tax_reserve = max(0, raw_profit * (income_tax_rate / 100))
        final_take_home = raw_profit - vat_reserve - income_tax_reserve
        profit_margin = (final_take_home / total_in * 100) if total_in > 0 else 0

        # --- 6. 대시보드 시각화 ---
        # KPI 카드
        st.divider()
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("총 매출(입금)", f"{total_in:,.0f}원")
        k2.metric("사업 지출", f"-{total_out:,.0f}원")
        k3.metric("세금 예비비", f"-{vat_reserve + income_tax_reserve:,.0f}원", help="부가세+소득세 합계")
        k4.metric("실제 수령액", f"{final_take_home:,.0f}원", delta=f"{profit_margin:.1f}% (수익률)")
        
        # 수익률 게이지 차트 (요청 사항 1)
        with k5:
            fig_gauge = go.Figure(go.Indicator(
                mode = "gauge+number",
                value = profit_margin,
                title = {'text': "최종 수익률 (%)"},
                gauge = {'axis': {'range': [None, 50]},
                         'bar': {'color': "darkblue"},
                         'steps': [
                             {'range': [0, 10], 'color': "red"},
                             {'range': [10, 20], 'color': "yellow"},
                             {'range': [20, 50], 'color': "green"}]}))
            fig_gauge.update_layout(height=250, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_gauge, use_container_width=True)

        # 지출 분석 그래프 (제안 C 적용)
        st.subheader("🔍 지출 성격 분석 (투자 vs 소비)")
        g1, g2 = st.columns(2)
        
        exp_df = edited_df[edited_df["카테고리"] != "입금"].copy()
        exp_df["지출성격"] = exp_df["카테고리"].apply(lambda x: "투자 (성장)" if x in INVESTMENT_CATS else "소비 (운영)")
        
        with g1:
            fig_type = px.pie(exp_df, values="지출금액", names="지출성격", color="지출성격",
                             color_discrete_map={"투자 (성장)":"#00CC96", "소비 (운영)":"#EF553B"},
                             title="투자 vs 소비 비중")
            st.plotly_chart(fig_type, use_container_width=True)
        
        with g2:
            fig_cat = px.bar(exp_df.groupby("카테고리")["지출금액"].sum().reset_index().sort_values("지출금액"),
                            x="지출금액", y="카테고리", orientation='h', title="카테고리별 지출 순위")
            st.plotly_chart(fig_cat, use_container_width=True)

        # 미분류 처리 섹션
        uncl = edited_df[edited_df['카테고리'] == "미분류"]["내역"].unique()
        if len(uncl) > 0:
            st.divider()
            st.info(f"💡 아직 학습이 필요한 내역이 {len(uncl)}건 있습니다.")
            target = uncl[0]
            with st.container(border=True):
                st.write(f"미분류 항목: **{target}**")
                c1, c2, c3 = st.columns([2, 2, 1])
                sel = c1.selectbox("카테고리", full_category_list + ["(직접 입력)"], key="bot_sel")
                val = c2.text_input("새 카테고리") if sel == "(직접 입력)" else ""
                if c3.button("학습 및 저장", type="primary", use_container_width=True):
                    save_mapping(target, val if sel == "(직접 입력)" else sel)
                    st.rerun()