"""Synthetic structural-row fixtures; no customer files or records."""
import io
import unittest
from unittest.mock import patch,MagicMock
import pandas as pd
from settlement.core import guess_columns,parse_rows,money,transaction_date
from settlement.layout import inspect_layout
from settlement.originals import read_original


COLUMNS=['이용일','이용카드','이용가맹점','이용금액','할부/회차','적립/할인율(%)','예상적립/할인','결제원금','결제후잔액','수수료(이자)']
def modern_hyundai(totals=False,mismatch=False,extra=()):
    rows=[['2026년 11월 이용대금명세서'],[],COLUMNS,
          [20260402,'사업','오래된 장비',700000,'10/7','','',70000,0,0],
          [20261002,'사업','새 장비',300000,'3/1','','',100000,0,0],
          [20261005,'사업','소계상점',50000,'-','','',0,50000,0],
          [20261006,'사업','반품',-5000,'-','','',0,-5000,0]]
    rows.extend(extra)
    if totals:
        rows.extend([['','','일시불 합계',45000,'','','',0,'',0],
                     ['총 합계','','',1045001 if mismatch else 1045000,'','','',170000,'',0],
                     ['안내: 다음 달도 같은 형식입니다.']])
    output=io.BytesIO()
    pd.DataFrame(rows).to_excel(output,index=False,header=False,sheet_name='hyundaicard_export')
    return output.getvalue()


class LayoutTests(unittest.TestCase):
    def test_modern_xlsx_without_total_is_valid_and_has_correct_rounds(self):
        result=read_original(modern_hyundai(),'download.xlsx','2026-10')
        self.assertIsNotNone(result);self.assertFalse(result['errors'])
        self.assertEqual(len(result['rows']),4)
        self.assertTrue(all(r['include'] for r in result['rows']))
        old,new,revolving,refund=result['rows']
        self.assertEqual((old['installment_total'],old['installment_number'],old['outflow']),(10,7,70000))
        self.assertEqual((new['installment_total'],new['installment_number'],new['outflow']),(3,1,100000))
        self.assertEqual(revolving['outflow'],50000);self.assertEqual(refund['inflow'],5000)
        self.assertEqual(sum(r['outflow']-r['inflow'] for r in result['rows']),215000)
        self.assertEqual(result['reconciliation'],'원본 합계 없음');self.assertFalse(result['controls'])
        self.assertEqual(old['row_number'],4)

    def test_excel_numeric_serial_dates_keep_their_type(self):
        result=read_original(modern_hyundai(extra=[[46300.0,'사업','일련번호 날짜',100,'-','','',0,0,0]]),'original.xlsx','2026-10')
        self.assertFalse(result['errors']);self.assertEqual(len(result['rows']),5)
        self.assertEqual(result['rows'][-1]['date'],transaction_date(46300.0))

    def test_total_subtotal_and_notice_never_become_transactions(self):
        result=read_original(modern_hyundai(totals=True),'original.xlsx','2026-10')
        self.assertFalse(result['errors']);self.assertEqual(len(result['rows']),4)
        self.assertEqual(result['reconciliation'],'일치')
        self.assertTrue(all(c['passed'] for c in result['controls']))
        self.assertEqual(sum(b['ignored_rows'] for b in result['batches']),3)
        self.assertEqual(sum(b['source_rows'] for b in result['batches']),7)
        # Compare the file's gross purchase total with gross, not installment expense.
        gross=next(c for c in result['controls'] if c['check'].startswith('이용금액'))
        self.assertEqual(gross['actual'],1045000)
        self.assertEqual(sum(r['outflow']-r['inflow'] for r in result['rows']),215000)

    def test_total_mismatch_and_invalid_total_amount_block(self):
        result=read_original(modern_hyundai(totals=True,mismatch=True),'original.xlsx','2026-10')
        self.assertTrue(result['errors']);self.assertEqual(result['reconciliation'],'불일치 · 확인 필요')
        check=next(c for c in result['controls'] if c['check'].startswith('이용금액'))
        self.assertEqual(check['difference'],-1)
        result=read_original(modern_hyundai(extra=[['총 합계','','','불명']]),'original.xlsx','2026-10')
        self.assertTrue(result['errors']);self.assertTrue(any(not c['passed'] for c in result['controls']))

    def test_bad_date_or_amount_is_reported_not_silently_ignored(self):
        for bad in ([20261301,'사업','추가',123,'-','','',0,0,0],
                    ['', '사업','날짜 없는 실제 거래',100,'-','','',0,0,0],
                    [20261008,'사업','실제 거래','불명','-','','',0,0,0]):
            result=read_original(modern_hyundai(extra=[bad]),'original.xlsx','2026-10')
            self.assertEqual(len(result['rows']),4)
            self.assertEqual(len(result['errors']),1);self.assertEqual(result['errors'][0]['row'],8)
            self.assertEqual(result['batches'][0]['error_count'],1)

    def test_summary_word_in_dated_merchant_is_a_real_transaction(self):
        result=read_original(modern_hyundai(extra=[[20261008,'사업','총 합계',100,'-','','',0,0,0]]),'original.xlsx','2026-10')
        self.assertFalse(result['errors']);self.assertEqual(len(result['rows']),5)
        self.assertEqual(result['rows'][-1]['outflow'],100)

    def test_missing_installment_principal_is_not_guessed(self):
        result=read_original(modern_hyundai(extra=[[20261008,'사업','불명 할부',90000,'3/1','','','','',0]]),'original.xlsx','2026-10')
        self.assertTrue(any('회차의 결제원금' in e['error'] for e in result['errors']))

    def test_bank_totals_notices_and_repeated_headers_keep_original_line_numbers(self):
        frame=pd.DataFrame([['2026-10-01','포장상',0,100],['2026-10-02','매출',1000,0],
                            ['','','',''],['거래일','내역','입금액','출금액'],
                            ['소계','',1000,100],['총 합계','',1000,100],['안내: 보관해 주세요','','','']],
                           columns=['거래일','내역','입금액','출금액'])
        mapping=guess_columns(frame.columns)
        rows,errors=parse_rows(frame,mapping,'은행','사업','hash','sheet',2,'입금/출금 별도')
        self.assertFalse(errors);self.assertEqual([r['row_number'] for r in rows],[4,5])
        layout=inspect_layout(frame,mapping,transaction_date,money)
        self.assertEqual(layout['blank_rows'],1);self.assertEqual(layout['ignored_rows'],4)
        self.assertEqual(layout['status'],'일치')

    def test_generic_signed_refund_total_is_checked_in_same_source_column(self):
        frame=pd.DataFrame({'날짜':['2026-10-01','2026-10-02','총 합계'],
                            '가맹점':['구매','반품',''],'이용금액':[100,-20,80]})
        rows,errors=parse_rows(frame,guess_columns(frame.columns),'카드','사업카드','hash','sheet',0,'양수=출금 (음수=환불)')
        self.assertFalse(errors);self.assertEqual(len(rows),2);self.assertEqual(rows[1]['inflow'],20)
        frame.loc[2,'이용금액']=81
        rows,errors=parse_rows(frame,guess_columns(frame.columns),'카드','사업카드','hash','sheet',0,'양수=출금 (음수=환불)')
        self.assertEqual(len(rows),2);self.assertTrue(errors[0]['검산'])

    def test_unknown_bad_row_and_malformed_date_with_total_word_remain_errors(self):
        frame=pd.DataFrame({'거래일':['2026-13-01','알수없음',None],
                            '내역':['총 합계','상점','실제 거래'],'출금':[100,100,100]})
        rows,errors=parse_rows(frame,guess_columns(frame.columns),'은행','사업','f','s',0,'입금/출금 별도')
        self.assertFalse(rows);self.assertEqual(len(errors),3)

    def test_subtotals_without_overall_scope_do_not_claim_a_match(self):
        frame=pd.DataFrame({'거래일':['2026-10-01','소계'],'내역':['상점',''],'출금':[100,100]})
        layout=inspect_layout(frame,guess_columns(frame.columns),transaction_date,money)
        self.assertFalse(layout['controls']);self.assertIn('전체 대조 안 함',layout['status'])

    def test_headerless_bank_still_recognizes_footer_and_validates_balance(self):
        raw=pd.DataFrame([['2026-10-02',100,0,900,'포장상']+['']*7,
                          ['2026-10-01',0,1000,1000,'입금']+['']*7,
                          ['총 합계',100,1000,'','']+['']*7,
                          ['안내: 원본 보관']+['']*11])
        book=MagicMock();book.sheet_names=['거래내역'];book.__enter__.return_value=book
        with patch('settlement.originals.pd.ExcelFile',return_value=book),patch('settlement.originals.pd.read_excel',return_value=raw):
            result=read_original(bytes.fromhex('d0cf11e0a1b11ae1')+b'fixture','bank.xls','2026-10')
        self.assertFalse(result['errors']);self.assertEqual(len(result['rows']),2)
        self.assertEqual(result['reconciliation'],'일치');self.assertEqual(result['batches'][0]['ignored_rows'],2)
