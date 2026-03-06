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
full_category_list = sorted(list(set(DEFAULT_CATEGORIES + list(mapping_dict.values()))))

uploaded_file = st.file_uploader("엑셀 파일을 업로드하세요", type=["xlsx"])

if uploaded_file:
    df = pd.read_excel(uploaded_file)
    
    cols = list(df.columns)
    desc_col = st.selectbox("내역(설명)이 적힌 컬럼을 확인하세요", cols, index=0)
    
    if desc_col:
        # 매핑 적용
        df['카테고리'] = df[desc_col].map(mapping_dict).fillna("미분류")
        
        st.subheader("📊 정산 결과 미리보기 (카테고리 칸을 클릭해 수정 가능)")
        
        # --- 핵심: 수정 가능한 표(Data Editor) 사용 ---
        edited_df = st.data_editor(
            df,
            column_config={
                "카테고리": st.column_config.SelectboxColumn(
                    "카테고리",
                    help="분류를 선택하세요",
                    options=full_category_list,
                    required=True,
                )
            },
            use_container_width=True,
            key="data_editor"
        )

        # --- 표에서 수정한 내용 실시간 DB 저장 ---
        # 사용자가 표에서 카테고리를 바꿨는지 체크합니다.
        if st.session_state.get("data_editor"):
            edits = st.session_state["data_editor"]["edited_rows"]
            if edits:
                for row_idx, changes in edits.items():
                    if "카테고리" in changes:
                        new_cat = changes["카테고리"]
                        desc_val = df.iloc[int(row_idx)][desc_col]
                        save_mapping(desc_val, new_cat)
                
                # 저장이 완료되면 화면을 다시 불러와서 '미분류' 경고를 업데이트합니다.
                st.toast("변경사항이 DB에 저장되어 학습되었습니다! ✅")
                st.rerun()

        # --- 3. 하단 미분류 내역 처리 섹션 ---
        unclassified = edited_df[edited_df['카테고리'] == "미분류"][desc_col].unique()
        
        if len(unclassified) > 0:
            st.divider()
            st.warning(f"🔎 아직 분류되지 않은 내역이 {len(unclassified)}건 있습니다. 위 표에서 직접 선택하거나 아래에서 지정하세요.")
            # (이전의 일괄 학습 섹션은 그대로 유지하거나 표 수정을 주력으로 사용하시면 됩니다.)