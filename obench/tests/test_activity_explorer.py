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

    def test_detail_allows_status_presentation_and_paragraph_spacing(self):
        request=next(r for name,_,r in oracle.cases() if name=='detail')
        row=request['data'][0]
        value={'titles':[r['title'] for r in request['data']], 'empty':False,'error':False,
               'closed':True,'detailReadable':True,
               'detail':'\n\n'.join((row['title'],row['project'],row['status'].capitalize(),
                                     row['description'].replace('\n','\n\n')))}
        self.assertTrue(oracle.compare('detail',value,request))
        for field in ('title','project','status','description'):
            wrong=dict(value,detail='\n'.join(row[k] for k in ('title','project','status','description') if k!=field))
            self.assertFalse(oracle.compare('detail',wrong,request),field)

    def test_activity_button_can_include_visible_metadata(self):
        request=next(r for name,_,r in oracle.cases() if name=='layout-360')
        value={'titles':[r['title'] for r in request['data']],'empty':False,'error':False,'documentWidth':360,
               'items':[{'text':r['title']+'\n'+r['project']+' · '+r['status'],
                         'clipped':False,'nestedScroll':False,'hit':True,'fontSize':16,
                         'box':{'width':300,'height':64}} for r in request['data']]}
        self.assertTrue(oracle.compare('layout-360',value,request))
        value['items'][0]['text']='Metadata only, missing the visible title'
        self.assertFalse(oracle.compare('layout-360',value,request))


if __name__=='__main__': unittest.main()
