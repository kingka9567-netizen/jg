import streamlit as st
import pandas as pd
import sqlite3

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
st.set_page_config(page_title="스마트 정산기", layout="wide")
st.title("학습하는 정산 시스템 🧠")

init_db()
mapping_dict = load_mappings()
# 현재 DB에 있는 카테고리와 기본 목록 합치기
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

uploaded_file = st.file_uploader("엑셀 파일을 업로드하세요", type=["xlsx"])

if uploaded_file:
    df = pd.read_excel(uploaded_file)
    
    # 내역 컬럼 자동 찾기
    cols = list(df.columns)
    default_desc_idx = 0
    for i, col in enumerate(cols):
        if any(k in str(col) for k in ["내역", "내용", "사용처", "가맹점", "적요"]):
            default_desc_idx = i
            break
    
    desc_col = st.selectbox("내역(설명)이 적힌 컬럼을 확인하세요", cols, index=default_desc_idx)
    
    if desc_col:
        # 1차 분류 적용
        df['카테고리'] = df[desc_col].map(mapping_dict).fillna("미분류")
        
        st.subheader("📊 정산 결과 미리보기 (카테고리 칸 클릭 수정 가능)")
        
        # --- 방법 A: 표에서 직접 수정 기능 ---
        edited_df = st.data_editor(
            df,
            column_config={
                "카테고리": st.column_config.SelectboxColumn(
                    "카테고리",
                    options=full_category_list,
                    required=True,
                )
            },
            use_container_width=True,
            key="main_editor"
        )

        # 표 수정 실시간 반영 로직
        if st.session_state.get("main_editor"):
            edits = st.session_state["main_editor"]["edited_rows"]
            if edits:
                for row_idx, changes in edits.items():
                    if "카테고리" in changes:
                        new_cat = changes["카테고리"]
                        desc_val = df.iloc[int(row_idx)][desc_col]
                        save_mapping(desc_val, new_cat)
                st.toast("표 수정 내용이 저장되었습니다! ✅")
                st.rerun()

        # --- 방법 B: 하단 집중 분류 섹션 ---
        unclassified = edited_df[edited_df['카테고리'] == "미분류"][desc_col].unique()
        
        st.divider()
        if len(unclassified) > 0:
            st.warning(f"🔎 아직 분류되지 않은 내역이 **{len(unclassified)}**건 있습니다.")
            
            # 분류할 대상 (가장 첫 번째 미분류 항목)
            target = unclassified[0]
            
            with st.container(border=True):
                st.write(f"현재 분류 중인 항목: **{target}**")
                
                col1, col2, col3 = st.columns([2, 2, 1])
                
                with col1:
                    selected_cat = st.selectbox(
                        "목록에서 선택", 
                        full_category_list + ["(직접 입력)"],
                        key="select_box"
                    )
                
                with col2:
                    if selected_cat == "(직접 입력)":
                        user_input_cat = st.text_input("새 카테고리 입력", key="text_input")
                    else:
                        user_input_cat = ""
                
                with col3:
                    st.write("") # 간격 맞추기용
                    if st.button("이 내역 기억하기", type="primary", use_container_width=True):
                        final_cat = user_input_cat if selected_cat == "(직접 입력)" else selected_cat
                        if final_cat:
                            save_mapping(target, final_cat)
                            st.success(f"'{target}'을(를) '{final_cat}'(으)로 학습했습니다.")
                            st.rerun()
        else:
            st.success("✅ 모든 내역의 분류가 완료되었습니다!")