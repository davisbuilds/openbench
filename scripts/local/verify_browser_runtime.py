#!/usr/bin/env python3
"""Check upstream full/headless/Chrome and default CLI paths inside the browser boundary."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench.repair_worker import run_worker


DRIVER = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const provenance = JSON.parse(fs.readFileSync('/opt/browser-deps/browser-provenance.json', 'utf8'));
const expectedFor = pkg => {
  const core = require.resolve('playwright-core/package.json', {paths:[pkg]});
  return JSON.parse(fs.readFileSync(core.replace(/package.json$/, 'browsers.json'), 'utf8'))
    .browsers.find(browser => browser.name === 'chromium').browserVersion;
};
const chromeVersion = expectedFor('/opt/browser-deps/node_modules/playwright');
for (const browser of provenance.browsers) {
  const version = browser.declared.chromium.browserVersion.split('.').map(Number);
  const floor = [156,0,8078,79];
  const comparison = version.map((part, index) => Math.sign(part-floor[index])).find(value => value !== 0) || 0;
  assert.ok(comparison >= 0, 'Upstream browser is below the security floor');
  const crypto = require('node:crypto');
  const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
  assert.equal(digest(browser.launcher.vendor_executable), browser.launcher.vendor_executable_sha256);
  assert.equal(digest(browser.installed_executable), browser.launcher.launcher_sha256);
}
(async () => {
  const records = [];
  for (const pkg of ['/opt/browser-deps/node_modules/playwright', '/opt/browser-deps/node_modules/playwright-core']) {
    const {chromium} = require(pkg);
    for (const mode of ['default-headless', 'full', 'chrome']) {
      const browser = await chromium.launch({chromiumSandbox:true, args:['--enable-automation'],
        ...(mode === 'full' ? {channel:'chromium'} : mode === 'chrome' ? {channel:'chrome'} : {})});
      try {
        const expected = mode === 'chrome' ? chromeVersion : expectedFor(pkg);
        assert.equal(browser.version(), expected);
        const page = await browser.newPage();
        const cdp = await page.context().newCDPSession(page);
        const {arguments:argv} = await cdp.send('Browser.getBrowserCommandLine');
        assert.ok(!argv.includes('--no-sandbox'));
        await page.setContent('<button onclick="this.textContent=\'after\'">before</button>');
        await page.getByRole('button', {name:'before', exact:true}).click();
        assert.equal(await page.getByRole('button').innerText(), 'after');
        records.push({pkg, mode, version:browser.version(), executable:argv[0], sandbox:true, interaction:true});
      } finally { await browser.close(); }
    }
  }
  const directory = '/tmp/upstream-browser-cli';
  fs.mkdirSync(directory);
  const cli = (...args) => execFileSync('playwright-cli', ['-s=upstream-browser-qualification', ...args], {
    cwd:directory, env:{...process.env, CI:'1', NO_UPDATE_NOTIFIER:'1'}, encoding:'utf8', timeout:30000,
  });
  try {
    cli('open', 'data:text/html,' + encodeURIComponent('<button onclick="this.textContent=\'after\'">before</button>'));
    const result = cli('run-code', `async page => {
      await page.getByRole('button', {name:'before', exact:true}).click();
      const cdp = await page.context().newCDPSession(page);
      return {product:(await cdp.send('Browser.getVersion')).product, text:await page.getByRole('button').innerText()};
    }`);
    assert.ok(result.includes('Chrome/' + chromeVersion), result);
    assert.ok(cli('snapshot').includes('after'));
    const browserProcesses = fs.readdirSync('/proc').filter(name => /^[0-9]+$/.test(name)).flatMap(pid => {
      try {
        const argv = fs.readFileSync('/proc/' + pid + '/cmdline', 'utf8').split('\0').filter(Boolean);
        return argv[0]?.startsWith('/opt/browser-cache/chromium-') && argv.includes('--remote-debugging-pipe')
          && !argv.some(arg => arg.startsWith('--type=')) ? [argv] : [];
      } catch { return []; }
    });
    assert.ok(browserProcesses.length > 0, 'CLI browser process was not observed');
    assert.ok(browserProcesses.every(argv => !argv.includes('--no-sandbox')));
    records.push({mode:'default-cli', version:chromeVersion, sandbox:true, interaction:true, snapshot:true});
  } finally { cli('close'); }
  console.log(JSON.stringify({schema:1, results:[{ok:true, value:{records, provenance}}]}));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; preserve prior qualification evidence')
    driver = DRIVER
    program = 'import subprocess,sys;sys.exit(subprocess.call(' + repr(['node', '-e', driver]) + '))'
    rows, boundary = run_worker(args.image, b'', [{}], program=program, timeout=180, browser=True)
    if not rows[0]['ok'] or len(rows[0]['value']['records']) != 7:
        raise RuntimeError('upstream browser consumer controls incomplete')
    report = {'status':'passed', 'scope':'offline browser consumer qualification; no campaign admission',
              'image':args.image, 'boundary':boundary, 'results':rows[0]['value']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
