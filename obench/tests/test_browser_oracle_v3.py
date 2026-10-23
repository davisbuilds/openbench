"""Real AX casing regression: synthetic controls, no private benchmark assets."""
import json
import os
import unittest

from obench import browser_worker_v2 as v2, browser_worker_v3 as v3
from obench.evaluation_package import BACKENDS


class BrowserV3Contracts(unittest.TestCase):
    def test_backend_keeps_historical_worker_separate(self):
        self.assertNotEqual(BACKENDS['activity-explorer-v2'].worker_program(),
                            BACKENDS['activity-explorer-v3'].worker_program())
        self.assertEqual(BACKENDS['activity-explorer-v2'].worker_program(), v2.program())


@unittest.skipUnless(os.environ.get('OBENCH_BROWSER_IMAGE'), 'requires pinned browser Docker image')
class AccessibleNameTests(unittest.TestCase):
    def test_css_casing_accepts_valid_controls_but_rejects_missing_and_ambiguous(self):
        from obench.repair_worker import run_worker
        for version, module in [('v2', v2), ('v3', v3)]:
            with self.subTest(version=version):
                code = "const {chromium}=require('/opt/browser-deps/node_modules/playwright');\n" + module.OBSERVER + r'''
(async()=>{
 const browser=await chromium.launch({chromiumSandbox:true});
 try{
  const page=await browser.newPage();
  await page.setContent(`<style>.caps{text-transform:uppercase}</style>
   <dialog aria-labelledby="heading"><p id="heading" class="caps">Activity details</p>
   <button class="caps">Close details</button></dialog>`);
  await page.locator('dialog').evaluate(el=>el.showModal());
  const observer=await Observer.create(page);
  const names=(await observer.nodes('dialog')).map(n=>n.name.value);
  let dialog=false, close=false, missingRejected=false, ambiguousRejected=false;
  try{await observer.detail();dialog=true;}catch{}
  try{await observer.one('button','Close details');close=true;}catch{}
  try{await observer.one('button','Missing');}catch{missingRejected=true;}
  await page.locator('dialog').evaluate(el=>{
   const button=document.createElement('button');button.textContent='Close details';
   el.append(button);
  });
  try{await observer.one('button','Close details');}catch{ambiguousRejected=true;}
  console.log(JSON.stringify({schema:1,results:[{ok:true,value:{names,dialog,close,missingRejected,ambiguousRejected}}]}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
                program = 'import subprocess,sys;sys.exit(subprocess.call(' + repr(['node','-e',code]) + '))'
                results, _ = run_worker(os.environ['OBENCH_BROWSER_IMAGE'], b'', [{}],
                                        program=program, timeout=30, browser=True)
                value = results[0]['value']
                self.assertEqual(value['names'], ['ACTIVITY DETAILS'])
                self.assertEqual(value['dialog'], version == 'v3')
                self.assertEqual(value['close'], version == 'v3')
                self.assertTrue(value['missingRejected'])
                if version == 'v3':
                    self.assertTrue(value['ambiguousRejected'])
