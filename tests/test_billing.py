import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from settlement.core import (parse_rows,guess_columns,metrics,classify,validate_rows,billing_month,installment_info)
from settlement.checks import upload_summary,coverage_checks,duplicate_comparisons
from settlement.store import Store,restore_to_new_sqlite

def bill(month='2026-09',number='3/3',charge=100000,fee=1000,included=False,file='september',card='신한 사업카드'):
    f=pd.DataFrame({'이용일자':['2026-07-10'],'내역':['촬영장비'],'청구금액':[charge],
                    '총구매금액':[300000],'할부회차':[number],'할부수수료':[fee]})
    rows,errors=parse_rows(f,guess_columns(f.columns,card=True),'카드',card,file,'청구서',0,'입금/출금 별도',
                          card_kind='청구내역',statement_month=month,fee_included=included)
    if errors:
        raise ValueError(str(errors))
    return rows[0]

def payment(card='신한 사업카드'):
    f=pd.DataFrame({'거래일':['2026-09-25'],'내역':['카드대금'],'출금액':[101000]})
    rows,_=parse_rows(f,guess_columns(f.columns),'은행','국민 사업통장','bank','거래',0,'입금/출금 별도')
    rows[0].update(category='기타',treatment='카드대금 납부',reviewed=True,card_account=card)
    return rows[0]

def confirm(row):
    return dict(row,category='사입',reviewed=True)

class BillingTests(unittest.TestCase):
    def test_july_purchase_is_september_charge_not_whole_purchase(self):
        r=confirm(bill());validate_rows([r])
        self.assertEqual(r['date'],'2026-07-10');self.assertEqual(r['month'],'2026-09')
        self.assertEqual(r['purchase_total'],300000)
        self.assertEqual((r['installment_number'],r['installment_total']),(3,3))
        self.assertEqual(metrics([r,payment()])['expense'],101000)

    def test_each_month_uses_actual_charge_instead_of_division(self):
        rows=[bill('2026-07','1/3',100001,0,file='july'),bill('2026-08','2/3',100000,0,file='august'),bill('2026-09','3/3',99999,0)]
        classified=classify([rows[2]],[],rows[:2])
        self.assertFalse(classified[0]['duplicate'])
        self.assertEqual(sum(r['outflow'] for r in rows),300000)
        self.assertEqual({r['month'] for r in rows},{'2026-07','2026-08','2026-09'})

    def test_reupload_same_charge_is_candidate_but_different_month_or_round_is_not(self):
        first=bill()
        self.assertTrue(classify([bill(file='new-name-content')],[],[first])[0]['duplicate'])
        self.assertFalse(classify([bill('2026-08','2/3',file='aug')],[],[first])[0]['duplicate'])
        self.assertFalse(classify([bill(number='2/3',file='same-month-other-round')],[],[first])[0]['duplicate'])

    def test_fee_included_does_not_double_count(self):
        r=bill(charge=101000,fee=1000,included=True)
        self.assertEqual(r['outflow'],101000)
        self.assertEqual(bill(charge=100000,fee=1000,included=False)['outflow'],101000)

    def test_refund_in_statement_month(self):
        r=confirm(bill(charge=-100000,fee=0))
        validate_rows([r]);self.assertEqual(metrics([r])['expense'],-100000)
        self.assertEqual(r['month'],'2026-09')

    def test_row_billing_month_overrides_file_month(self):
        f=pd.DataFrame({'이용일자':['2026-07-10'],'내역':['상품'],'청구금액':[100000],'청구월':['2026년 9월']})
        rows,errors=parse_rows(f,guess_columns(f.columns,card=True),'카드','카드','file','s',0,'입금/출금 별도',card_kind='청구내역',statement_month='2026-08')
        self.assertFalse(errors);self.assertEqual(rows[0]['month'],'2026-09')

    def test_approval_cannot_be_counted_even_when_reviewed(self):
        f=pd.DataFrame({'날짜':['2026-07-10'],'내역':['상품'],'승인금액':[300000]})
        rows,_=parse_rows(f,guess_columns(f.columns),'카드','카드','file','s',0,'양수=출금 (음수=환불)')
        self.assertFalse(rows[0]['include'])
        row=confirm(rows[0]);row['include']=True
        self.assertEqual(metrics([row])['expense'],0)
        with self.assertRaisesRegex(ValueError,'승인내역'):validate_rows([row])

    def test_zero_billing_is_preserved_as_excluded_review_item(self):
        r=bill(charge=0,fee=0)
        self.assertFalse(r['include']);self.assertEqual(r['purchase_total'],300000)
        classified=classify([r],[])[0]
        self.assertFalse(classified['include']);self.assertFalse(classified['reviewed'])
        validate_rows([dict(classified,reviewed=True)])

    def test_missing_month_amount_and_invalid_round_are_errors(self):
        f=pd.DataFrame({'날짜':['2026-07-10'],'내역':['상품'],'총구매금액':[300000]})
        with self.assertRaises(ValueError):
            parse_rows(f,guess_columns(f.columns,True),'카드','카드','f','s',0,'입금/출금 별도',card_kind='청구내역',statement_month='2026-09')
        with self.assertRaises(ValueError):bill(month='')
        with self.assertRaises(ValueError):installment_info('4/3',None)
        with self.assertRaises(ValueError):installment_info('2/3',6)
        self.assertEqual(billing_month(202609),'2026-09')

    def test_summary_counts_and_purchase_period_distinct_from_month(self):
        r=bill()
        batch=dict(id=r['batch_id'],filename='9월 카드.xlsx',source='카드',account=r['account'],card_kind='청구내역',source_rows=4,error_count=2,blank_rows=1)
        summary=upload_summary([batch],[r])[0]
        self.assertIn('2026-07-10',summary['구매/거래 기간']);self.assertEqual(summary['정산월'],'2026-09')
        self.assertEqual(summary['원본 데이터 행'],summary['정상 거래']+summary['빈 행']+summary['오류 행'])
        self.assertEqual(summary['출금/당월 청구 합계'],101000)

    def test_duplicate_comparison_keeps_source_and_installment_evidence(self):
        old=bill();new=bill(file='new');new['duplicate']=True
        result=duplicate_comparisons([new],[old],[{'id':old['batch_id'],'filename':'old.xlsx'},{'id':new['batch_id'],'filename':'new.xlsx'}])[0]
        self.assertEqual(result['new']['파일'],'new.xlsx');self.assertEqual(result['matches'][0]['파일'],'old.xlsx')
        self.assertEqual(result['new']['회차'],3)

class CoverageTests(unittest.TestCase):
    def test_payment_without_statement_blocks(self):
        checks=coverage_checks([payment()])
        self.assertTrue(any(c['blocking'] for c in checks))

    def test_statement_already_stored_satisfies_presence(self):
        checks=coverage_checks([payment()],[bill()])
        self.assertFalse(any(c['blocking'] for c in checks));self.assertEqual(checks[0]['status'],'카드자료 있음')

    def test_two_cards_same_bank_one_missing_is_detected(self):
        cards=[dict(account='신한 사업카드',bank_account='국민 사업통장'),dict(account='다른 사업카드',bank_account='국민 사업통장')]
        checks=coverage_checks([payment(),bill()],cards=cards)
        self.assertTrue(any(c['account']=='다른 사업카드' and c['blocking'] for c in checks))

    def test_unlinked_payment_does_not_assume_any_uploaded_card(self):
        checks=coverage_checks([payment(''),bill()])
        self.assertTrue(any(c['status']=='연결 필요' for c in checks))

    def test_single_registered_link_and_no_charge_reason(self):
        cards=[dict(account='신한 사업카드',bank_account='국민 사업통장')]
        checks=coverage_checks([payment(''),bill()],cards=cards)
        self.assertFalse(any(c['blocking'] for c in checks))
        exemptions=[dict(month='2026-09',account='신한 사업카드',reason='미사용, 이월 할부 없음',confirmed=True)]
        self.assertFalse(any(c['blocking'] for c in coverage_checks([],cards=cards,months=['2026-09'],no_billing=exemptions)))
        exemptions[0]['reason']=''
        self.assertTrue(any(c['blocking'] for c in coverage_checks([],cards=cards,months=['2026-09'],no_billing=exemptions)))

    def test_old_purchase_month_is_not_presence_evidence(self):
        old=dict(bill(),settlement_basis='구버전',month='2026-07')
        self.assertTrue(any(c['blocking'] for c in coverage_checks([payment()],[old])))

    def test_legacy_same_month_and_new_statement_cannot_double_count(self):
        old=dict(bill(file='legacy'),settlement_basis='구버전',card_kind='구버전')
        checks=coverage_checks([bill()],[old])
        self.assertTrue(any(c['status']=='구버전 자료 중복 위험' and c['blocking'] for c in checks))
        old['include']=False
        self.assertFalse(any(c['blocking'] for c in coverage_checks([bill()],[old])))

    def test_inactive_and_not_yet_active_cards(self):
        cards=[dict(account='비활성',enabled=False),dict(account='미래',start_month='2026-10')]
        self.assertFalse(coverage_checks([],cards=cards,months=['2026-09']))

class BillingStorageTests(unittest.TestCase):
    def test_store_enforces_coverage_and_billing_backup_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store('sqlite:///'+str(Path(tmp)/'db.sqlite3'))
            p=payment();r=confirm(bill())
            batches=[{'id':p['batch_id']},{'id':r['batch_id']}]
            with self.assertRaisesRegex(ValueError,'누락'):
                store.import_rows(batches[:1],[p],coverage_confirmed=True)
            with self.assertRaisesRegex(ValueError,'완전성'):
                store.import_rows(batches,[p,r])
            self.assertFalse(store.rows())
            store.import_rows(batches,[p,r],coverage_confirmed=True)
            self.assertEqual(len(store.rows('2026-09')),2);self.assertFalse(store.rows('2026-07'))
            store.save_setting('card:1',dict(account=r['account'],bank_account=p['account']))
            restored=restore_to_new_sqlite(store.export(),Path(tmp)/'restored.sqlite3')
            self.assertEqual(restored.rows(),store.rows())
            self.assertEqual(restored.settings('card:'),store.settings('card:'))
            self.assertEqual(metrics(restored.rows())['expense'],101000)

if __name__=='__main__':unittest.main()
