// Public workflow smoke, not the hidden task acceptance tests.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
(async()=>{
  const server=spawn(process.execPath,['scripts/server.cjs'],{stdio:'ignore'});
  let browser;
  try {
    browser=await chromium.launch({chromiumSandbox:true});
    const page=await browser.newPage({viewport:{width:900,height:700}});
    for(let i=0;i<40;i++) {
      try {await page.goto('http://127.0.0.1:4173');break;}
      catch(error){if(i===39)throw error;await new Promise(r=>setTimeout(r,100));}
    }
    await page.getByRole('heading',{name:'Activity explorer'}).waitFor();
    fs.mkdirSync('/logs/agent',{recursive:true});
    await page.screenshot({path:'/logs/agent/browser-before.png'});
    fs.appendFileSync('web/style.css','\nbody {background:rgb(12,34,56)}\n');
    await page.reload();
    const color=await page.locator('body').evaluate(el=>getComputedStyle(el).backgroundColor);
    if(color!=='rgb(12, 34, 56)')throw Error('Edited CSS did not reach the rendered app');
    await page.screenshot({path:'/logs/agent/browser.png'});
    if(fs.readFileSync('/logs/agent/browser-before.png').equals(fs.readFileSync('/logs/agent/browser.png')))throw Error('Visible edit did not change screenshot');
    console.log('BROWSER_RENDER_OK');
  } finally {if(browser)await browser.close();server.kill();await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
