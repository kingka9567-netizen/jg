# --- 2. 데이터 처리 함수 (에러 방지 강화 버전) ---
def load_mappings():
    ws = get_worksheet("mapping")
    if not ws: return {}
    
    # 시트의 모든 데이터를 가져옴
    data = ws.get_all_records()
    
    # [수정] 데이터가 아예 없거나 헤더가 없을 경우 빈 사전 반환
    if not data:
        # 헤더가 없는 경우를 대비해 제목 강제 입력
        ws.update('A1:B1', [['description', 'category']])
        return {}
        
    df = pd.DataFrame(data)
    
    # [수정] 컬럼 이름이 있는지 한 번 더 확인
    if 'description' in df.columns and 'category' in df.columns:
        return dict(zip(df['description'], df['category']))
    else:
        # 컬럼 이름이 틀렸을 경우 제목 수정
        ws.update('A1:B1', [['description', 'category']])
        return {}

def load_history():
    ws = get_worksheet("history")
    if not ws: return pd.DataFrame()
    
    data = ws.get_all_records()
    if not data:
        # 헤더가 없는 경우 제목 강제 입력
        ws.update('A1:E1', [['report_name', 'date', 'category', 'income', 'expense']])
        return pd.DataFrame()
        
    return pd.DataFrame(data)