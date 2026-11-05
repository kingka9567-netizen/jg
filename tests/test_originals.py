"""Synthetic download layouts only; no customer transaction fixtures."""
import io
import unittest
from unittest.mock import patch
import pandas as pd
from settlement.originals import read_original
from settlement.core import classify, use_in_month, validate_rows


def hyundai_file():
    columns=['이용일','이용카드','이용가맹점','이용금액','할부/회차','결제원금','수수료(이자)']
    rows=[['2026년 10월 이용대금명세서'],columns,
          ['2026-09-15','사업용','재료상',50000,'',0,0],
          ['2026-07-03','사업용','장비상',300000,'3/3',100000,1000],
          ['2026-08-31','사업용','포장상',20000,'',20000,0],
          ['총 합계','','',370000,'',120000,1000],
          ['일부결제금액이월약정 소계']]
    return ('<html><table>'+''.join('<tr>'+''.join('<td>'+str(c)+'</td>' for c in r)+'</tr>' for r in rows)+'</table></html>').encode()


class OriginalTests(unittest.TestCase):
    def test_october_title_september_usage_revolving_installment_outside(self):
        result=read_original(hyundai_file(),'download.xls','2026-09')
        self.assertFalse(result['errors']);self.assertTrue(all(c['passed'] for c in result['controls']))
        a,b,c=result['rows']
        self.assertEqual((a['month'],a['billing_month'],a['outflow']),('2026-09','2026-10',50000))
        self.assertEqual((b['installment_number'],b['installment_total'],b['outflow']),(3,3,101000))
        self.assertFalse(c['include']);self.assertTrue(c['outside_period'])
        self.assertEqual(sum(r['outflow']-r['inflow'] for r in result['rows'] if r['include']),151000)
        self.assertFalse(classify(result['rows'],[])[2]['include'])
        c.update(reviewed=True,category='기타',include=True)
        with self.assertRaisesRegex(ValueError,'대상월 밖'):
            validate_rows([c])

    def test_bank_no_header_first_transaction_preserved_and_balance_checked(self):
        raw=pd.DataFrame([['2026-09-02',100,0,900,'포장상']+['']*7,
                          ['2026-09-01',0,1000,1000,'판매입금']+['']*7])
        book=unittest.mock.MagicMock();book.sheet_names=['거래내역']
        book.__enter__.return_value=book
        with patch('settlement.originals.pd.ExcelFile',return_value=book),patch('settlement.originals.pd.read_excel',return_value=raw):
            result=read_original(bytes.fromhex('d0cf11e0a1b11ae1')+b'fake','bank.xls','2026-09')
        self.assertEqual(len(result['rows']),2);self.assertFalse(result['errors'])
        self.assertEqual(result['rows'][0]['row_number'],1)
        raw.iloc[0,3]=899
        with patch('settlement.originals.pd.ExcelFile',return_value=book),patch('settlement.originals.pd.read_excel',return_value=raw):
            result=read_original(bytes.fromhex('d0cf11e0a1b11ae1')+b'fake','bank.xls','2026-09')
        self.assertTrue(result['errors']);self.assertFalse(result['controls'][0]['passed'])

    def test_samsung_multi_sheet_discount_foreign_reference_and_round(self):
        columns=['이용일','이용구분','가맹점','이용금액','원금','이자/수수료','개월','회차','이용혜택','혜택금액']
        frames={
          '일시불':[['제목'],[],columns,['2026-09-12','사업용','해외도구',10000,9000,0,0,0,'청구할인',-1000],
                  ['2026-08-31','사업용','포장상',2000,2000,0,0,0,'',0],['','','일시불합계']],
          '할부':[['제목'],[],columns,['2026-07-02','사업용','장비',300000,100000,500,3,3,'',0],['','','할부합계']],
          '청구요약':[['제목'],[],['이용자','원금','이자/수수료'],['사업용',111000,500]],
          '해외이용':[['제목'],[],['이용일','가맹점','청구금액'],['2026-09-12','해외도구',10000],['','해외매출합계','']]}
        buffer=io.BytesIO()
        with pd.ExcelWriter(buffer,engine='openpyxl') as writer:
            for name,rows in frames.items():
                pd.DataFrame(rows).to_excel(writer,sheet_name=name,index=False,header=False)
        result=read_original(buffer.getvalue(),'card.xlsx','2026-09')
        self.assertFalse(result['errors']);self.assertEqual(len(result['rows']),3)
        self.assertEqual(sum(r['outflow'] for r in result['rows'] if r['include']),109500)
        self.assertEqual(sum(b['ignored_rows'] for b in result['batches']),2)
        self.assertTrue(any('해외이용 1건' in s for s in result['notes']))

    def test_installment_requires_actual_round_not_division(self):
        row=dict(source='카드',date='2026-07-01',installment_total=3,installment_number=3,
                 inflow=0,outflow=300000,purchase_total=300000)
        with self.assertRaisesRegex(ValueError,'회차의 원금'):
            use_in_month(row,'2026-09')

if __name__=='__main__': unittest.main()
