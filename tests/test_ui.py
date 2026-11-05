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
    def setUp(self):
        fixed_month=patch.dict(os.environ,{"SETTLEMENT_PREVIEW_MONTH":"2026-09"})
        fixed_month.start();self.addCleanup(fixed_month.stop)

    def test_demo_import_review_save_reload(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run(timeout=15)
            self.assertFalse(at.exception)
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            self.assertFalse(at.exception)
            self.assertEqual([m.value for m in at.metric][:3],['5,000,000원','500,000원','4,500,000원'])
            # Committed edits create a fresh immutable editor snapshot, persist,
            # and immediately update totals; unrelated reruns must not undo them.
            table_key=next(k for k in at.session_state.filtered_state if k.endswith('_details'))
            at.session_state[table_key]={'edited_rows':{0:{'treatment':'개인/제외','reviewed':True}},'added_rows':[],'deleted_rows':[]}
            at.run();self.assertFalse(at.exception)
            self.assertEqual(at.metric[0].value,'0원')
            next(b for b in at.button if b.label=='전체 선택').click().run()
            next(b for b in at.button if b.label=='선택한 거래 확인').click().run()
            at.run();self.assertEqual(at.metric[0].value,'0원')
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
            env={'SETTLEMENT_MODE':'demo','SETTLEMENT_PREVIEW_FILES':json.dumps([str(p)]),'SETTLEMENT_PREVIEW_MONTH':'2026-09','SETTLEMENT_PREVIEW_DATABASE_URL':'sqlite:///'+str(Path(tmp)/'preview.sqlite3')}
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
            table_key=next(k for k in at.session_state.filtered_state if k.endswith('_details'))
            at.session_state[table_key]={'edited_rows':{0:{'category':'세금'}},'added_rows':[],'deleted_rows':[]}
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
            at.run()
            self.assertFalse(at.exception)
            draft=next(v for k,v in at.session_state.filtered_state.items() if k.startswith('new_') and isinstance(v,list) and v and isinstance(v[0],dict) and 'id' in v[0])
            self.assertTrue(all(r['reviewed'] for r in draft))

    def test_autosave_resumes_after_new_browser_session_without_source_files(self):
        from test_originals import hyundai_file
        with tempfile.TemporaryDirectory() as tmp:
            f=io.BytesIO(hyundai_file());f.name='original.xls'
            url='sqlite:///'+str(Path(tmp)/'persistent.sqlite3')
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'test-password-long-enough','SETTLEMENT_DATABASE_URL':url}
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[f]):
                at=AppTest.from_file(str(APP)).run();at.session_state['authenticated']=True;at.run()
                drafts=Store(url).drafts();self.assertEqual(len(drafts),1)
                self.assertEqual(len(drafts[0]['rows']),3)
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[]):
                fresh=AppTest.from_file(str(APP)).run();fresh.session_state['authenticated']=True;fresh.run()
                next(r for r in fresh.radio if r.label=='작업').set_value('임시 저장 이어하기').run()
                self.assertFalse(fresh.exception)
                self.assertEqual(fresh.metric[1].value,'151,000원')
                self.assertTrue(any('자동 임시 저장 완료' in c.value for c in fresh.caption))

    def test_finalization_reopen_and_excel_ui_flow(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            next(c for c in at.checkbox if c.label.startswith('추천된 ')).check().run()
            next(b for b in at.button if b.label=='추천 분류 한 번에 확인').click().run()
            at.run()
            next(c for c in at.checkbox if c.label=='카드별 사용월·전체 건수·합계를 확인했습니다').check().run()
            next(c for c in at.checkbox if c.label=='업로드 확인표의 파일별 기간·건수·금액을 원본과 대조했습니다').check().run()
            next(b for b in at.button if b.label=='검토한 정산 저장').click().run()
            next(r for r in at.radio if r.label=='작업').set_value('저장한 월정산').run()
            self.assertFalse(at.exception)
            next(c for c in at.checkbox if c.label.startswith('이 월의 전체 파일')).check().run()
            next(b for b in at.button if b.label=='월정산 확정').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('1차 확정' in s.value for s in at.success))
            self.assertFalse(any(b.label=='수정 저장' for b in at.button))
            next(t for t in at.text_input if t.label=='확정 해제 사유').set_value('카테고리 재확인').run()
            next(b for b in at.button if b.label=='사유를 남기고 확정 해제').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any(b.label=='수정 저장' for b in at.button))

    def test_cloud_fail_closed_before_storage(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'cloud','SETTLEMENT_PASSWORD':''}):
            at=AppTest.from_file(str(APP)).run()
            self.assertFalse(at.exception)
            self.assertTrue(any('숫자 4자리' in e.value for e in at.error))

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
                # Recognized charge heading is mapped without another required checkbox.
                self.assertEqual(at.metric[1].value,'101,000원')
                next(b for b in at.button if b.label=='전체 선택').click().run()
                next(s for s in at.selectbox if s.label=='선택 거래 카테고리').set_value('사입').run()
                next(b for b in at.button if b.label=='선택한 거래 확인').click().run()
                at.run()
                next(c for c in at.checkbox if c.label=='카드별 사용월·전체 건수·합계를 확인했습니다').check().run()
                next(c for c in at.checkbox if c.label=='업로드 확인표의 파일별 기간·건수·금액을 원본과 대조했습니다').check().run()
                next(b for b in at.button if b.label=='검토한 정산 저장').click().run()
                self.assertFalse(at.exception)
                self.assertTrue(any('저장했습니다' in s.value for s in at.success))
                saved=store.rows('2026-09')[0]
                self.assertEqual(saved['date'],'2026-07-10');self.assertEqual(saved['outflow'],101000)
                self.assertEqual(saved['installment_number'],3)
                self.assertEqual(store.settings('profile:')[0]['card_kind'],'청구내역')

    def test_select_all_is_scoped_to_search_and_clear_preserves_transactions(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            next(t for t in at.text_input if t.label=='거래명 검색').set_value('CJ').run()
            next(b for b in at.button if b.label=='전체 선택').click().run()
            self.assertTrue(any('선택 1건 / 검색 결과 1건' in c.value for c in at.caption))
            next(b for b in at.button if b.label=='선택 해제').click().run()
            self.assertTrue(next(b for b in at.button if b.label=='선택한 거래 확인').disabled)
            next(b for b in at.button if b.label=='전체 선택').click().run()
            next(s for s in at.selectbox if s.label=='선택 거래 카테고리').set_value('부자재').run()
            next(b for b in at.button if b.label=='선택한 거래 확인').click().run()
            at.run()
            draft=next(v for k,v in at.session_state.filtered_state.items() if k.startswith('new_') and isinstance(v,list) and v and isinstance(v[0],dict) and 'id' in v[0])
            self.assertEqual(sum(r['reviewed'] for r in draft),1)
            self.assertEqual(next(r for r in draft if r['description']=='CJ대한통운')['category'],'부자재')
            next(t for t in at.text_input if t.label=='거래명 검색').set_value('').run()
            self.assertTrue(any('선택 0건' in c.value for c in at.caption))
            next(b for b in at.button if b.label=='전체 선택').click().run()
            self.assertTrue(any('선택 3건 / 검색 결과 3건' in c.value for c in at.caption))
            self.assertFalse(at.exception)

    def test_merchant_select_all_and_clear_keep_edited_category(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            next(t for t in at.text_input if t.label=='거래처 검색').set_value('CJ').run()
            group_key=next(k for k in at.session_state.filtered_state if '_groups_' in k and isinstance(at.session_state[k],dict))
            at.session_state[group_key]={'edited_rows':{0:{'category':'부자재'}},'added_rows':[],'deleted_rows':[]};at.run()
            next(b for b in at.button if b.label=='거래처 전체 선택').click().run()
            self.assertTrue(any('선택 1묶음 / 검색 결과 1묶음' in c.value for c in at.caption))
            next(b for b in at.button if b.label=='거래처 선택 해제').click().run()
            self.assertTrue(any('선택 0묶음' in c.value for c in at.caption))
            next(b for b in at.button if b.label=='거래처 전체 선택').click().run()
            next(b for b in at.button if b.label=='선택한 거래처 분류 적용').click().run()
            at.run();self.assertFalse(at.exception)
            draft=next(v for k,v in at.session_state.filtered_state.items() if k.startswith('new_') and isinstance(v,list) and v and isinstance(v[0],dict) and 'id' in v[0])
            cj=next(r for r in draft if r['description']=='CJ대한통운')
            self.assertTrue(cj['reviewed']);self.assertEqual(cj['category'],'부자재')
            self.assertEqual(sum(r['reviewed'] for r in draft),1)

    def test_basic_table_has_only_selection_and_four_business_columns(self):
        with patch.dict(os.environ,{'SETTLEMENT_MODE':'demo'}):
            at=AppTest.from_file(str(APP)).run()
            next(b for b in at.button if b.label=='가상 은행·카드 내역 불러오기').click().run()
            table=next(d.value for d in at.dataframe if 'selected' in d.value.columns and 'date' in d.value.columns)
            self.assertEqual(list(table.columns),['selected','id','date','description','inflow','outflow'])
            self.assertNotIn('installment_number',table)
            details=next(e for e in at.expander if e.label=='제외·이체·카드 연결 등 상세 설정')
            self.assertFalse(details.proto.expanded)

    def test_four_digit_pin_login_and_persistent_lockout(self):
        with tempfile.TemporaryDirectory() as tmp:
            url='sqlite:///'+str(Path(tmp)/'pin.sqlite3')
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'0042','SETTLEMENT_DATABASE_URL':url}
            with patch.dict(os.environ,env):
                for _ in range(5):
                    bad=AppTest.from_file(str(APP)).run()
                    next(t for t in bad.text_input if t.label=='비밀번호').set_value('wrong').run()
                    next(b for b in bad.button if b.label=='로그인').click().run()
                fresh=AppTest.from_file(str(APP)).run()
                next(t for t in fresh.text_input if t.label=='비밀번호').set_value('0042').run()
                next(b for b in fresh.button if b.label=='로그인').click().run()
                self.assertFalse(fresh.exception)
                self.assertTrue(any('시도 제한' in e.value for e in fresh.error))
                self.assertFalse(any(r.label=='작업' for r in fresh.radio))
            # Separate new store: a correct PIN logs in without an administrative override.
            env['SETTLEMENT_DATABASE_URL']='sqlite:///'+str(Path(tmp)/'correct.sqlite3')
            with patch.dict(os.environ,env):
                good=AppTest.from_file(str(APP)).run()
                next(t for t in good.text_input if t.label=='비밀번호').set_value('0042').run()
                next(b for b in good.button if b.label=='로그인').click().run()
                self.assertFalse(good.exception);self.assertTrue(any(r.label=='작업' for r in good.radio))

    def test_modern_hyundai_has_no_column_mapping_and_total_does_not_error(self):
        from test_layout import modern_hyundai
        with tempfile.TemporaryDirectory() as tmp:
            f=io.BytesIO(modern_hyundai(totals=True));f.name='raw.xlsx'
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'test-password-long-enough','SETTLEMENT_DATABASE_URL':'sqlite:///'+str(Path(tmp)/'native.sqlite3'),'SETTLEMENT_PREVIEW_MONTH':'2026-10'}
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[f]):
                at=AppTest.from_file(str(APP)).run();at.session_state['authenticated']=True;at.run()
                self.assertFalse(at.exception);self.assertFalse(at.error)
                self.assertEqual(at.metric[1].value,'215,000원')
                self.assertFalse(any(s.label=='최초 구매일 / 은행 거래일' for s in at.selectbox))
                self.assertFalse(any(e.label.startswith('파일 읽기 설정') for e in at.expander))
                self.assertTrue(any('거래 4건 읽음' in c.value and '3건 제외' in c.value for c in at.caption))
                self.assertTrue(any(c.value=='원본 합계 대조: 일치' for c in at.caption))
                draft=Store(env['SETTLEMENT_DATABASE_URL']).drafts()[0]
                self.assertEqual(len(draft['rows']),4)
                self.assertEqual(draft['context']['batches'][0]['source_rows'],7)
                self.assertEqual(draft['context']['batches'][0]['ignored_rows'],3)

    def test_native_total_mismatch_blocks_save_with_no_manual_mapping(self):
        from test_layout import modern_hyundai
        with tempfile.TemporaryDirectory() as tmp:
            f=io.BytesIO(modern_hyundai(totals=True,mismatch=True));f.name='raw.xlsx'
            env={'SETTLEMENT_MODE':'local','SETTLEMENT_PASSWORD':'test-password-long-enough','SETTLEMENT_DATABASE_URL':'sqlite:///'+str(Path(tmp)/'native.sqlite3'),'SETTLEMENT_PREVIEW_MONTH':'2026-10'}
            with patch.dict(os.environ,env),patch('streamlit.file_uploader',return_value=[f]):
                at=AppTest.from_file(str(APP)).run();at.session_state['authenticated']=True;at.run()
                self.assertFalse(at.exception)
                self.assertTrue(any('검증 오류' in e.value for e in at.error))
                self.assertTrue(next(b for b in at.button if b.label=='검토한 정산 저장').disabled)
                self.assertFalse(any(e.label.startswith('파일 읽기 설정') for e in at.expander))

if __name__=='__main__': unittest.main()
