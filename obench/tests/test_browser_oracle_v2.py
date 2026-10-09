"""V2 contracts; real attack controls run in the browser Docker CI lane."""
import os
from pathlib import Path
import unittest

from obench.repair_oracles import activity_explorer as v1, activity_explorer_v2 as v2
from obench.repair_validation import inspect_task, assess_controls

ROOT = Path(__file__).resolve().parents[2]


class BrowserV2Contracts(unittest.TestCase):
    def test_cosmetic_casing_keeps_full_content_requirement(self):
        request = next(r for name, _, r in v2.cases() if name == 'detail')
        row = request['data'][0]
        value = {'observer': 'cdp-isolated-world-v2', 'titles': [r['title'] for r in request['data']],
                 'empty': False, 'error': False, 'closed': True, 'detailReadable': True,
                 'detail': '\n'.join(row[k].upper() for k in ('title','project','status','description'))}
        self.assertTrue(v2.compare('detail', value, request))
        self.assertFalse(v1.compare('detail', value, request))
        value['detail'] = '\n'.join(row[k].upper() for k in ('title','project','status'))
        self.assertIn('missing-detail-description', v2.reasons('detail', value, request))
        value['detailReadable'] = False
        self.assertIn('details-not-readable', v2.reasons('detail', value, request))

    def test_unprotected_or_incomplete_observations_cannot_pass(self):
        self.assertEqual(v2.reasons('list', {}, {}), ['unprotected-observation'])
        report = v2.grade([{'ok': False} for _ in v2.cases()])
        self.assertEqual(report['score'], 0)
        self.assertEqual(report['check_pass_fraction'], 0)
        self.assertFalse(report['all_checks_passed'])
        self.assertEqual(len(report['failure_groups']['browser-operation-failed']), len(v2.cases()))
        with self.assertRaises(ValueError):
            v2.grade([])

    def test_keyboard_requires_open_and_close_not_just_initial_focus(self):
        value = dict(observer='cdp-isolated-world-v2', statusFocused=True, recordFocused=True,
                     detailOpened=True, closeFocused=True, detailClosed=True)
        self.assertTrue(v2.compare('keyboard', value, {}))
        value['detailClosed'] = False
        self.assertEqual(v2.reasons('keyboard', value, {}), ['details-not-keyboard-closable'])

    def test_historical_replay_is_inspectable_but_cannot_admit_campaigns(self):
        old = inspect_task(ROOT/'benchmarks/harbor/local/activity-explorer-c1-o1')
        new = inspect_task(ROOT/'benchmarks/harbor/local/activity-explorer-c2-o2')
        self.assertFalse(old['quality_eligible'])
        self.assertTrue(new['quality_eligible'])
        self.assertNotEqual(old['revision']['case_sha256'], new['revision']['case_sha256'])
        self.assertTrue(any('historical browser oracle is quarantined' in f for f in assess_controls([], old)))


@unittest.skipUnless(os.environ.get('OBENCH_BROWSER_IMAGE'), 'requires pinned browser Docker image')
class ObserverFailureTests(unittest.TestCase):
    def test_destroyed_world_cannot_fall_back_to_candidate_world(self):
        from obench.browser_worker_v2 import OBSERVER
        from obench.repair_worker import run_worker
        code = "const {chromium}=require('/opt/browser-deps/node_modules/playwright');\n" + OBSERVER + r'''
(async()=>{
 const browser=await chromium.launch({chromiumSandbox:true});
 try{
  const page=await browser.newPage();await page.setContent('<button>Positive</button>');
  const observer=await Observer.create(page), node=await observer.one('button','Positive');
  if(await observer.measure(node,el=>el.innerText)!=='Positive')throw Error('positive detector failed');
  await page.goto('data:text/html,<button>Replacement</button>');
  let refused=false;try{await observer.measure(node,el=>el.innerText);}catch{refused=true;}
  console.log(JSON.stringify({schema:1,results:[{ok:true,value:{refused}}]}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        program = 'import subprocess,sys;sys.exit(subprocess.call(' + repr(['node','-e',code]) + '))'
        results, _ = run_worker(os.environ['OBENCH_BROWSER_IMAGE'], b'', [{}], program=program, timeout=30, browser=True)
        self.assertEqual(results, [{'ok':True,'value':{'refused':True}}])


if __name__ == '__main__': unittest.main()
