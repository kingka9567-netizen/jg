import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
import pandas as pd
from settlement.core import (money, transaction_date, parse_rows, guess_columns, guess_header,
    classify, learn_rules, metrics, validate_rows, csv_safe)
from settlement.store import Store, ConflictError, restore_to_new_sqlite

def sample(source='은행', description='상점', incoming=0, outgoing=100, file='file', account='사업'):
    frame = pd.DataFrame({'거래일':['2026-09-01'],'내역':[description],'입금액':[incoming],'출금액':[outgoing]})
    if source=='카드':
        frame=frame.drop(columns=['입금액','출금액']);frame['청구금액']=outgoing-incoming
    rows, errors = parse_rows(frame,guess_columns(frame.columns,card=source=='카드'),source,account,file,'내역',0,'입금/출금 별도',card_kind='청구내역',statement_month='2026-09')
    assert not errors
    return rows[0]

def confirmed(**kwargs):
    r=sample(**kwargs); r.update(reviewed=True,category='기타'); return r

class CoreTests(unittest.TestCase):
    def test_currency_and_date(self):
        self.assertEqual(money('1,230원'),1230)
        self.assertEqual(money('(300)'),-300)
        for bad in ['abc','NaN','1.5','Infinity']:
            with self.assertRaises(ValueError): money(bad)
        self.assertEqual(transaction_date(20260901),'2026-09-01')
        self.assertEqual(transaction_date(20260901.0),'2026-09-01')
        self.assertEqual(transaction_date(datetime(2026,9,1)),'2026-09-01')
        self.assertEqual(transaction_date(46266),'2026-09-01')
        with self.assertRaises(ValueError): transaction_date('09/01')

    def test_signed_refund(self):
        frame=pd.DataFrame({'날짜':['2026-09-01'],'가맹점명':['상점'],'청구금액':['-1,000']})
        rows, errors=parse_rows(frame,guess_columns(frame.columns,card=True),'카드','카드','f','s',0,'양수=출금 (음수=환불)',card_kind='청구내역',statement_month='2026-09')
        self.assertFalse(errors)
        rows[0].update(category='사입',reviewed=True)
        self.assertEqual(metrics(rows)['expense'],-1000)

    def test_bad_rows_not_silently_zeroed(self):
        frame=pd.DataFrame({'날짜':['2026-09-01','총합',None],'내역':['상점','합계',None],'출금액':['불명','100',None]})
        rows,errors=parse_rows(frame,guess_columns(frame.columns),'은행','사업','f','s',0,'입금/출금 별도')
        self.assertFalse(rows); self.assertEqual(len(errors),2)

    def test_xlsx_roundtrip_header_and_transaction_date(self):
        data=io.BytesIO()
        frame=pd.DataFrame({'거래일시':[datetime(2026,9,1)],'적요':['상점'],'출금액':['1,000']})
        frame.to_excel(data,index=False,startrow=3)
        raw=pd.read_excel(io.BytesIO(data.getvalue()),header=None)
        self.assertEqual(guess_header(raw),3)
        frame=raw.iloc[4:].copy(); frame.columns=raw.iloc[3]
        rows,errors=parse_rows(frame,guess_columns(frame.columns),'은행','통장','f','Sheet1',3,'입금/출금 별도')
        self.assertFalse(errors); self.assertEqual(rows[0]['date'],'2026-09-01')
        self.assertEqual(rows[0]['row_number'],5)

    def test_conflicting_name_and_amount_patterns(self):
        a=confirmed(description='네이버페이',outgoing=100); a['category']='사입'
        b=confirmed(description='네이버페이',outgoing=200); b['category']='광고비'
        rules=learn_rules([a,b])
        self.assertEqual(classify([a],rules)[0]['category'],'사입')
        self.assertEqual(classify([b],rules)[0]['category'],'광고비')
        broad=learn_rules([a,b],False)
        self.assertFalse(classify([a],broad)[0]['reviewed'])
        unknown=sample(description='네이버페이',outgoing=500)
        self.assertFalse(classify([unknown],rules)[0]['reviewed'])
        other=sample(description='네이버페이',outgoing=100,account='다른 통장')
        self.assertFalse(classify([other],rules)[0]['reviewed'])

    def test_rule_direction_and_disabled(self):
        a=confirmed(); rule=learn_rules([a])[0]
        self.assertFalse(classify([sample(incoming=100,outgoing=0)],[rule])[0]['reviewed'])
        rule['enabled']=False
        self.assertFalse(classify([a],[rule])[0]['reviewed'])

    def test_ambiguous_ai_never_confirms(self):
        row=sample(description='새로운 거래')
        r=classify([row],[],advisor=lambda _: '광고비')[0]
        self.assertEqual(r['category'],'광고비'); self.assertFalse(r['reviewed'])

    def test_duplicate_candidate_and_legitimate_repetitions(self):
        a=sample(); b=sample(file='another')
        r=classify([b],[],[a])[0]
        self.assertTrue(r['duplicate']); self.assertFalse(r['include'])
        duplicated=classify([a,b],[])
        self.assertEqual(len(duplicated),2)
        self.assertTrue(all(r['duplicate'] for r in duplicated))
        # A reviewer can retain both legitimate equal-value transactions.
        for r in duplicated: r.update(include=True,reviewed=True,category='사입')
        validate_rows(duplicated)
        self.assertEqual(metrics(duplicated)['expense'],200)

    def test_exclude_card_payment_transfer_loan_but_bank_net_keeps_them(self):
        rows=[confirmed(incoming=5000,outgoing=0),confirmed(file='card',source='카드',outgoing=200),
              confirmed(file='payment',outgoing=200),confirmed(file='loan',incoming=1000,outgoing=0),
              confirmed(file='transfer',outgoing=500)]
        rows[2]['treatment']='카드대금 납부'; rows[3]['treatment']='대출'; rows[4]['treatment']='계좌이체'
        m=metrics(rows)
        self.assertEqual(m['income'],5000); self.assertEqual(m['expense'],200)
        self.assertEqual(m['residual'],4800); self.assertEqual(m['bank_net'],5300)

    def test_validation_and_csv_injection(self):
        with self.assertRaises(ValueError): validate_rows([sample()])
        data=csv_safe(pd.DataFrame({'내역':['=HYPERLINK("a")','안전']})).decode('utf-8-sig')
        self.assertIn("'=HYPERLINK",data)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.url='sqlite:///'+str(Path(self.tmp.name)/'db.sqlite3')
        self.store=Store(self.url)
        self.row=confirmed()
        self.batch={'id':self.row['batch_id'],'filename':'test.xlsx'}
    def tearDown(self): self.tmp.cleanup()
    def save(self): self.store.import_rows([self.batch],[self.row],learn_rules([self.row]))

    def test_persistent_idempotent_import(self):
        self.save()
        self.assertEqual(len(Store(self.url).rows()),1)
        with self.assertRaises(ConflictError): self.save()
        self.assertEqual(len(self.store.rows()),1)
        self.assertEqual(self.store.months(),['2026-09'])

    def test_rollback_incomplete_batch(self):
        self.save()
        other=confirmed(file='new')
        with self.assertRaises(ConflictError):
            self.store.import_rows([{'id':other['batch_id']},self.batch],[other,self.row])
        self.assertEqual(len(self.store.rows()),1)
        self.assertNotIn(other['batch_id'],self.store.batch_ids())

    def test_overlapping_import_after_review_requires_recheck(self):
        self.save()
        other=confirmed(file='different-file')
        with self.assertRaises(ConflictError):
            self.store.import_rows([{'id':other['batch_id']}],[other])
        self.assertEqual(len(self.store.rows()),1)
        other.update(duplicate=True,include=True)
        self.store.import_rows([{'id':other['batch_id']}],[other])
        self.assertEqual(len(self.store.rows()),2)

    def test_optimistic_conflict_and_atomic_edits(self):
        self.save()
        old=self.store.rows()[0]; current=dict(old,category='광고비')
        self.store.edit_rows([current])
        with self.assertRaises(ConflictError): self.store.edit_rows([dict(old,category='사입')])
        saved=self.store.rows()[0]
        self.assertEqual(saved['category'],'광고비'); self.assertEqual(saved['version'],2)

    def test_metrics_from_edited_rows(self):
        self.save(); row=self.store.rows()[0]
        row['treatment']='계좌이체'
        self.assertEqual(metrics([row])['expense'],0)
        self.store.edit_rows([row]); self.assertEqual(metrics(self.store.rows())['expense'],0)

    def test_immutable_identity(self):
        self.save(); row=self.store.rows()[0]
        row['outflow']=999999
        self.store.edit_rows([row])
        self.assertEqual(self.store.rows()[0]['outflow'],100)

    def test_backup_snapshot_contains_rules_profiles_audit(self):
        self.save(); self.store.save_setting('profile:p',{'id':'p','header':3})
        backup=json.loads(self.store.export())
        self.assertEqual(len(backup['transactions']),1)
        self.assertEqual(len(backup['audit']),1)
        self.assertEqual(len(backup['settings']),2)
        self.assertNotIn('url',backup)

    def test_backup_restore_roundtrip_refuses_overwrite(self):
        self.save()
        self.store.save_setting('category:custom',{'name':'새 분류'})
        target=Path(self.tmp.name)/'restored.sqlite3'
        restored=restore_to_new_sqlite(self.store.export(),target)
        self.assertEqual(restored.rows(),self.store.rows())
        self.assertEqual(restored.settings('rule:'),self.store.settings('rule:'))
        self.assertEqual(restored.settings('category:'),[{'name':'새 분류'}])
        self.assertEqual(json.loads(restored.export())['audit'],json.loads(self.store.export())['audit'])
        with self.assertRaises(ValueError): restore_to_new_sqlite(self.store.export(),target)

if __name__=='__main__': unittest.main()
