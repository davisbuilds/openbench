"""Task predicates and real-browser controls use independent expected behavior."""
import unittest
from obench.repair_oracles import activity_explorer as oracle
from obench.browser_policy import POLICY, verify_options


class ActivityTests(unittest.TestCase):
    def test_combined_filter_requires_both_conditions(self):
        request=next(r for name,_,r in oracle.cases() if name=='combined-filter')
        good={'titles':[r['title'] for r in request['data'] if r['project']=='Ledger' and r['status']=='completed'], 'empty':False,'error':False}
        self.assertTrue(oracle.compare('combined-filter',good,request))
        good['titles'] += [request['data'][0]['title']]
        self.assertFalse(oracle.compare('combined-filter',good,request))

    def test_missing_or_failed_observation_never_passes(self):
        grade=oracle.grade([{'ok':False} for _ in oracle.cases()])
        self.assertEqual(grade['score'],0)
        self.assertTrue(all(not c['pass'] for c in grade['checks']))
        with self.assertRaises(ValueError): oracle.grade([])

    def test_hidden_content_cannot_satisfy_layout(self):
        request=next(r for name,_,r in oracle.cases() if name=='layout-360')
        value={'titles':[r['title'] for r in request['data']],'empty':False,'error':False,'documentWidth':360,
               'items':[{'text':r['title'],'clipped':False,'nestedScroll':False,'hit':True,'fontSize':16,'box':{'width':300,'height':32}} for r in request['data']]}
        self.assertTrue(oracle.compare('layout-360',value,request))
        for mutation in ({'fontSize':1},{'clipped':True},{'nestedScroll':True},{'hit':False},{'text':''}):
            original=dict(value['items'][0]);value['items'][0].update(mutation)
            self.assertFalse(oracle.compare('layout-360',value,request),mutation)
            value['items'][0]=original

    def test_loaded_policy_must_match_exactly(self):
        self.assertTrue(verify_options(['no-new-privileges','seccomp='+POLICY.read_text()]))
        self.assertFalse(verify_options(['no-new-privileges','seccomp=unconfined']))
        self.assertFalse(verify_options(['no-new-privileges','seccomp='+POLICY.read_text(),'label=disable']))


if __name__=='__main__': unittest.main()
