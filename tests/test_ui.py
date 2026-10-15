import os
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

APP=Path(__file__).resolve().parents[1]/'settlement_v2.py'

class UITests(unittest.TestCase):
    def test_demo_import_review_save_reload(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run(timeout=15)
            self.assertFalse(at.exception)
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            self.assertFalse(at.exception)
            self.assertEqual([m.value for m in at.metric][:3],['5,000,000원','500,000원','4,500,000원'])
            # Streamlit editor events: ensure the first edit immediately changes metrics,
            # then a second edit survives rather than reverting on the next rerun.
            table_key=next(k for k in at.session_state.filtered_state if k.endswith('_editor'))
            at.session_state[table_key]={'edited_rows':{0:{'treatment':'개인/제외','reviewed':True}},'added_rows':[],'deleted_rows':[]}
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(at.metric[0].value,'0원')
            at.session_state[table_key]={'edited_rows':{0:{'treatment':'개인/제외','reviewed':True},1:{'reviewed':True}},'added_rows':[],'deleted_rows':[]}
            at.run()
            self.assertEqual(at.metric[0].value,'0원')
            # Explicitly confirm the remainder in the same immutable editor snapshot.
            at.session_state[table_key]={'edited_rows':{i:{'reviewed':True} for i in range(4)},'added_rows':[],'deleted_rows':[]}
            at.run()
            next(c for c in at.checkbox if '검토했습니다' in c.label).check().run()
            next(b for b in at.button if b.label=='현재 검색 결과 확인 처리 (분류 유지)').click().run()
            next(b for b in at.button if b.label=='검토한 정산 저장').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('저장했습니다' in s.value for s in at.success))
            next(r for r in at.radio if r.label=='작업').set_value('저장한 월정산').run()
            self.assertEqual(at.selectbox[0].value,'2026-09')
            self.assertFalse(at.exception)

    def test_cloud_fail_closed_before_storage(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'cloud','SETTLEMENT_PASSWORD':''}):
            at=AppTest.from_file(str(APP)).run()
            self.assertFalse(at.exception)
            self.assertTrue(any('16자' in e.value for e in at.error))

    def test_future_menu_placeholder(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(r for r in at.radio if r.label=='대메뉴').set_value('월별 사업 분석 보고서').run()
            self.assertFalse(at.exception)
            self.assertTrue(any('추후' in i.value for i in at.info))

if __name__=='__main__': unittest.main()
