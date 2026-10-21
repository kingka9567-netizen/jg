import os
import json
import streamlit as st
import io
import tempfile
import hashlib
import pandas as pd
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from settlement.store import Store

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
            next(c for c in at.checkbox if c.label=='카드별 사용월·전체 건수·합계를 확인했습니다').check().run()
            next(c for c in at.checkbox if c.label=='업로드 확인표의 파일별 기간·건수·금액을 원본과 대조했습니다').check().run()
            next(b for b in at.button if b.label=='검토한 정산 저장').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('저장했습니다' in s.value for s in at.success))
            next(r for r in at.radio if r.label=='작업').set_value('저장한 월정산').run()
            self.assertEqual(at.selectbox[0].value,'2026-09')
            self.assertFalse(at.exception)

    def test_native_download_upload_keeps_usage_month_and_skips_mapping(self):
        from test_originals import hyundai_file
        with tempfile.TemporaryDirectory() as tmp:
            f=io.BytesIO(hyundai_file());f.name='original.xls'
            url='sqlite:///'+str(Path(tmp)/'native.sqlite3')
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'test-password-long-enough','SETTLEMENT_DATABASE_URL':url}
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[f]):
                at=AppTest.from_file(str(APP)).run()
                at.session_state['authenticated']=True;at.run()
                next(t for t in at.text_input if t.label=='정산 대상월').set_value('2026-09').run()
                self.assertFalse(at.exception);self.assertFalse(at.error)
                self.assertTrue(any('원본 형식 자동 인식' in s.value for s in at.success))
                self.assertEqual(at.metric[1].value,'151,000원')
                self.assertFalse(any(s.label=='자료 종류' for s in at.selectbox))
                summary=next(d.value for d in at.dataframe if '정상 거래' in d.value.columns)
                self.assertEqual(summary['정상 거래'].sum(),3)
                self.assertEqual(summary['대상월 밖 제외'].sum(),1)
                self.assertTrue(next(b for b in at.button if b.label=='검토한 정산 저장').disabled)

    def test_local_preview_loads_supplied_original_without_upload(self):
        from test_originals import hyundai_file
        original_get_option=st.get_option
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'original.xls';p.write_bytes(hyundai_file())
            env={'SETTLEMENT_MODE':'demo','SETTLEMENT_PREVIEW_FILES':json.dumps([str(p)]),'SETTLEMENT_PREVIEW_MONTH':'2026-09'}
            with patch.dict(os.environ,env),patch('streamlit.get_option',side_effect=lambda key: '127.0.0.1' if key=='server.address' else original_get_option(key)):
                at=AppTest.from_file(str(APP)).run()
                self.assertFalse(at.exception);self.assertFalse(at.error)
                self.assertEqual(at.metric[1].value,'151,000원')
                self.assertFalse(any('가상' in b.label for b in at.button))
                self.assertTrue(any('실제 자료 로컬 검토' in i.value for i in at.info))
                self.assertEqual(p.read_bytes(),hyundai_file())

    def test_private_preview_refuses_public_bind(self):
        original_get_option=st.get_option
        env={'SETTLEMENT_MODE':'demo','SETTLEMENT_PREVIEW_FILES':'["never-read.xls"]'}
        with patch.dict(os.environ,env),patch('streamlit.get_option',side_effect=lambda key: '0.0.0.0' if key=='server.address' else original_get_option(key)):
            at=AppTest.from_file(str(APP)).run()
            self.assertFalse(at.exception)
            self.assertTrue(any('로컬 주소에만' in e.value for e in at.error))
            self.assertFalse(at.metric)

    def test_legacy_mapping_review_reuses_draft_and_preserves_user_edit(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            draft_key=next(k for k,v in at.session_state.filtered_state.items() if k.startswith('new_') and isinstance(v,list) and v and isinstance(v[0],dict) and 'id' in v[0])
            draft=at.session_state[draft_key]
            row_id=draft[0]['id']
            table_key=next(k for k in at.session_state.filtered_state if k.endswith('_editor'))
            at.session_state[table_key]={'edited_rows':{0:{'category':'세금','treatment':'개인/제외','reviewed':True}},'added_rows':[],'deleted_rows':[]}
            at.run()
            store=Store('sqlite:///'+at.session_state['demo_path'])
            store.save_setting('legacy_mapping:current',{'records':[{'description':'CJ대한통운','category':'기존택배'}],'excluded':[]})
            at.run()
            self.assertFalse(at.exception)
            current=at.session_state[draft_key]
            self.assertEqual(next(r for r in current if r['id']==row_id)['category'],'세금')
            self.assertEqual(next(r for r in current if r['description']=='CJ대한통운')['category'],'기존택배')
            self.assertTrue(any(h.value=='거래처별 빠른 분류' for h in at.subheader))

    def test_confirm_recommendations_only_leaves_uncertain_for_review(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            next(c for c in at.checkbox if c.label.startswith('추천된 ')).check().run()
            next(b for b in at.button if b.label=='추천 분류 한 번에 확인').click().run()
            self.assertFalse(at.exception)
            draft=next(v for k,v in at.session_state.filtered_state.items() if k.startswith('new_') and isinstance(v,list) and v and isinstance(v[0],dict) and 'id' in v[0])
            self.assertTrue(all(r['reviewed'] for r in draft))

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

    def test_installment_demo_shows_actual_statement_cost(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='7월 구매·9월 할부 청구 예시 불러오기').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.metric[1].value,'401,000원')
            summary=at.dataframe[0].value
            card=summary[summary['자료']=='카드'].iloc[0]
            self.assertEqual(card['정산월'],'2026-09')
            self.assertIn('2026-07-10',card['구매/거래 기간'])
            self.assertTrue(next(b for b in at.button if b.label=='검토한 정산 저장').disabled)

    def test_missing_second_card_blocks_and_no_charge_requires_reason(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            store=Store('sqlite:///'+at.session_state['demo_path'])
            store.save_setting('card:other',{'account':'누락된 카드','bank_account':'사업통장','start_month':'2026-09'})
            at.run()
            self.assertTrue(any('누락 또는 미연결' in e.value for e in at.error))
            self.assertTrue(next(b for b in at.button if b.label=='검토한 정산 저장').disabled)
            next(t for t in at.text_input if t.label=='청구 없음 사유').set_value('미사용이며 이월 할부도 없음').run()
            next(c for c in at.checkbox if c.label=='청구서에서 이번 달 청구가 없음을 확인했습니다').check().run()
            next(b for b in at.button if b.label=='청구 없음 기록').click().run()
            self.assertFalse(at.exception)
            self.assertFalse(any('누락 또는 미연결' in e.value for e in at.error))
            self.assertEqual(store.settings('no_billing:')[0]['account'],'누락된 카드')

    def test_duplicate_side_by_side_and_explicit_include(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            staged,_=at.session_state['demo_import']
            original=dict(staged[0],id='old-transaction',batch_id='old-file',reviewed=True)
            store=Store('sqlite:///'+at.session_state['demo_path'])
            store.import_rows([{'id':'old-file','filename':'기존.xlsx'}],[original])
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.metric[0].value,'0원')
            self.assertTrue(any('중복 후보 비교' in h.value for h in at.subheader))
            decision=next(r for r in at.radio if r.label=='비교 결과')
            decision.set_value('실제 별개 결제·청구 → 이번 행 포함').run()
            next(b for b in at.button if b.label=='비교 결과 적용').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.metric[0].value,'5,000,000원')

    def test_real_xlsx_upload_mapping_review_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            url='sqlite:///'+str(Path(tmp)/'test.sqlite3')
            store=Store(url)
            store.save_setting('card:test',{'account':'시험카드','bank_account':'시험통장','start_month':'2026-09'})
            f=io.BytesIO()
            pd.DataFrame({'이용일자':['2026-07-10'],'내역':['촬영장비'],'청구금액':[100000],
                          '할부수수료':[1000],'할부회차':['3/3'],'총구매금액':[300000]}).to_excel(f,index=False)
            f.name='가상-9월청구.xlsx'
            upload_key='upload_0_'+hashlib.sha256(f.getvalue()).hexdigest()[:12]
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'test-password-long-enough','SETTLEMENT_DATABASE_URL':url}
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[f]):
                at=AppTest.from_file(str(APP)).run()
                at.session_state['authenticated']=True
                at.run()
                next(s for s in at.selectbox if s.key==upload_key+'_source').set_value('카드').run()
                next(s for s in at.selectbox if s.label=='저장한 계좌/카드').set_value('시험카드').run()
                self.assertFalse(at.exception)
                next(t for t in at.text_input if t.label=='정산 대상월').set_value('2026-09').run()
                next(t for t in at.text_input if t.label=='파일 청구월 (청구월 열이 없을 때)').set_value('2026-09').run()
                next(c for c in at.checkbox if c.key==upload_key+'_fee_included').uncheck().run()
                next(c for c in at.checkbox if c.key==upload_key+'_charge_confirm').check().run()
                self.assertEqual(at.metric[1].value,'101,000원')
                next(s for s in at.selectbox if s.label=='일괄 카테고리').set_value('사입').run()
                next(c for c in at.checkbox if '검토했습니다' in c.label).check().run()
                next(b for b in at.button if b.label=='현재 검색 결과에 적용').click().run()
                next(c for c in at.checkbox if c.label=='카드별 사용월·전체 건수·합계를 확인했습니다').check().run()
                next(c for c in at.checkbox if c.label=='업로드 확인표의 파일별 기간·건수·금액을 원본과 대조했습니다').check().run()
                next(b for b in at.button if b.label=='검토한 정산 저장').click().run()
                self.assertFalse(at.exception)
                self.assertTrue(any('저장했습니다' in s.value for s in at.success))
                saved=store.rows('2026-09')[0]
                self.assertEqual(saved['date'],'2026-07-10');self.assertEqual(saved['outflow'],101000)
                self.assertEqual(saved['installment_number'],3)
                self.assertEqual(store.settings('profile:')[0]['card_kind'],'청구내역')

if __name__=='__main__': unittest.main()
