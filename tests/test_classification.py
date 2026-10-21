"""Synthetic merchants and categories; customer mappings stay local."""
import unittest
from settlement.classification import suggest, review_groups, apply_groups, read_legacy_mapping
from settlement.core import classify
from test_settlement import sample

class ClassificationTests(unittest.TestCase):
    def test_channel_receipt_does_not_classify_card_purchase_as_sales(self):
        income=sample(description='주식회사지마켓',incoming=1000,outgoing=0)
        payment=sample(description='주식회사 지마켓',source='카드',outgoing=1000)
        self.assertEqual(classify([income],[])[0]['category'],'판매대금 입금')
        self.assertEqual(classify([payment],[])[0]['category'],'미분류')

    def test_legacy_exact_mapping_and_previous_exclusion(self):
        row=sample(description='테스트 정산입금',incoming=100,outgoing=0)
        personal=sample(description='테스트 기타',file='personal')
        mapping=[{'description':'테스트 정산입금','category':'입금'},{'description':'테스트 기타','category':'생활 및 기타'}]
        a,b=classify([row,personal],[],legacy_mapping=mapping,legacy_excluded=['생활 및 기타'])
        self.assertEqual(a['category'],'입금');self.assertFalse(a['reviewed'])
        self.assertEqual(b['treatment'],'개인/제외')

    def test_legacy_conflict_and_common_payment_need_confirmation(self):
        row=sample(description='네이버페이')
        mapping=[{'description':'네이버페이','category':'사입'}]
        r=classify([row],[],legacy_mapping=mapping)[0]
        self.assertEqual(r['category'],'사입');self.assertTrue(r['purpose_ambiguous']);self.assertFalse(r['reviewed'])
        mapping.append({'description':'네이버페이','category':'광고비'})
        r=classify([row],[],legacy_mapping=mapping)[0]
        self.assertEqual(r['category'],'미분류');self.assertIn('충돌',r['reason'])

    def test_group_applies_many_rows_but_keeps_accounts_and_direction_separate(self):
        rows=[sample(description='테스트 거래처',outgoing=100),sample(description='테스트 거래처',outgoing=200,file='b'),
              sample(description='테스트 거래처',account='다른통장',file='c'),
              sample(description='테스트 거래처',incoming=100,outgoing=0,file='d')]
        groups=review_groups(rows);self.assertEqual(len(groups),3)
        g=next(g for g in groups if g['count']==2)
        self.assertEqual((g['minimum'],g['maximum']),(100,200))
        g.update(selected=True,category='사입')
        updated,n=apply_groups(rows,[g])
        self.assertEqual(n,2);self.assertEqual(sum(r['reviewed'] for r in updated),2)
        self.assertTrue(all(not r['reviewed'] for r in rows))

    def test_group_rejects_unclassified_or_duplicate_without_partial_mutation(self):
        row=sample();g=review_groups([row])[0];g['selected']=True
        with self.assertRaises(ValueError):apply_groups([row],[g])
        self.assertFalse(row['reviewed'])
        g['category']='사입';row['duplicate']=True
        with self.assertRaisesRegex(ValueError,'별도로'):apply_groups([row],[g])
        self.assertFalse(review_groups([row]))

    def test_mapping_only_reader_rejects_invalid_and_preserves_conflicts(self):
        records=read_legacy_mapping('description,category\n테스트,사입\n테스트,광고비\n'.encode(),'mapping.csv')
        self.assertEqual(len(records),2)
        with self.assertRaises(ValueError):read_legacy_mapping(b'unknown,column\na,b','mapping.csv')

if __name__=='__main__':unittest.main()
