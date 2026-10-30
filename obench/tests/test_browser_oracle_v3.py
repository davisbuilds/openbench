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
    def test_uppercase_labels_work_through_full_interaction_driver(self):
        from scripts.ci.browser_quality_fixtures import HTML, JS, CSS
        from obench.harbor_sandbox import pack_files
        from obench.repair_worker import run_worker
        from obench.repair_oracles.activity_explorer_v2 import cases, reasons
        html = HTML
        for label in ('Activity details', 'Close details', 'Search activities', 'Status', 'Retry'):
            html = html.replace(label, label.upper())
        # Unlike exact string labels, Playwright regex labels preserve surrounding
        # whitespace from ordinary indented, nested HTML. Exercise that path.
        html = html.replace('<label>SEARCH ACTIVITIES<input type="search" aria-label="SEARCH ACTIVITIES">',
                            '<label>\n  <span>SEARCH ACTIVITIES</span>\n  <input type="search">\n')
        selected = [c for c in cases() if c[0] in ('detail', 'keyboard', 'retry', 'combined-filter')]
        self.assertEqual(len(selected), 4)
        files = {'web/index.html': html.encode(), 'web/app.js': JS.encode(), 'web/style.css': CSS.encode()}
        rows, _ = run_worker(os.environ['OBENCH_BROWSER_IMAGE'], pack_files(files),
                             [c[2] for c in selected], program=v3.program(), timeout=90, browser=True)
        for (name, _, request), row in zip(selected, rows, strict=True):
            with self.subTest(check=name):
                self.assertTrue(row['ok'], row.get('error'))
                self.assertEqual(reasons(name, row['value'], request), [])

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
