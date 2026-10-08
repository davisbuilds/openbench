"""Browser observations only. Authoritative comparisons stay on the host.

Candidate files are static assets, never imported as Node/Python modules. The
browser retains Chromium's sandbox inside a confined network-free container.
"""
import json

DRIVER = r'''
const fs=require('node:fs'), path=require('node:path'), http=require('node:http');
const {chromium}=require('/opt/browser-deps/node_modules/playwright');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const root='/tmp/submission/web';
let current;
async function visibleTitles(page,data) {
 const records=[],handles=[];
 try {
  for(const item of data) {
   const matches=await page.getByRole('button',{name:item.title,exact:true}).elementHandles();
   handles.push(...matches);
   for(const element of matches) if(await element.isVisible()) records.push({title:item.title,element});
  }
  // Preserve actual DOM order and duplicates, including metadata-bearing cards.
  return await page.evaluate(records=>records.sort((a,b)=>
   a.element.compareDocumentPosition(b.element)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1
  ).map(r=>r.title),records);
 } finally {await Promise.all(handles.map(handle=>handle.dispose()));}
}
async function settleFilters(page,test) {
 // A wait hint derived from public inputs, never a verdict. Read fresh browser
 // observations afterward; the host independently checks their correctness.
 const titles=test.data.filter(r=>
  (r.title+' '+r.project).toLowerCase().includes((test.query||'').toLowerCase()) &&
  (!test.status || r.status===test.status.toLowerCase())).map(r=>r.title);
 const deadline=Date.now()+2000;
 do {
  const observed=await visibleTitles(page,test.data);
  const empty=await page.getByText('No activities found',{exact:true}).isVisible();
  if(JSON.stringify(observed)===JSON.stringify(titles) && empty===(titles.length===0))return;
  await page.waitForTimeout(40);
 } while(Date.now()<deadline);
}
const server=http.createServer((req,res)=>{
  let name;
  try { name=decodeURIComponent(new URL(req.url,'http://localhost').pathname); }
  catch {res.writeHead(400);return res.end();}
  if(name==='/api/activities') {
    const count=current.requests++;
    const failed=current.test.mode==='retry' && count===0;
    return setTimeout(()=>{res.writeHead(failed?503:200,{'Content-Type':'application/json'});res.end(JSON.stringify({activities:current.test.data}));},current.test.mode==='loading'?600:0);
  }
  const file=path.resolve(root,'.'+(name==='/'?'/index.html':name));
  if(!file.startsWith(root+path.sep)){res.writeHead(403);return res.end();}
  fs.readFile(file,(err,bytes)=>{if(err){res.writeHead(404);return res.end();}
    res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');res.end(bytes);});
});
const results=[];
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({chromiumSandbox:true});
 try {
  for(const test of payload.cases){
   current={test,requests:0};
   const context=await browser.newContext({viewport:test.viewport||{width:1280,height:800},locale:'en-US',timezoneId:'UTC',deviceScaleFactor:1,serviceWorkers:'block',reducedMotion:'reduce'});
   const page=await context.newPage();page.setDefaultTimeout(2000);
   // Only the disposable static server can be reached. No file navigation or
   // external URLs; the Docker network also independently denies egress.
   await context.route('**/*',r=>r.request().url().startsWith(origin+'/')?r.continue():r.abort());
   try {
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    let value={};
    if(test.mode==='loading') {
      await page.getByText('Loading activities',{exact:true}).waitFor();
      value.loading=await page.getByText('Loading activities',{exact:true}).isVisible();
    }
    if(test.mode==='retry') {
      await page.getByText('Could not load activities',{exact:true}).waitFor();
      await page.getByRole('button',{name:'Retry',exact:true}).click();
    }
    if(test.data.length) await page.getByRole('button',{name:test.data[0].title,exact:true}).waitFor();
    else await page.getByText('No activities found',{exact:true}).waitFor();
    if(test.query!==undefined) await page.getByLabel('Search activities',{exact:true}).fill(test.query);
    if(test.status) await page.getByRole('combobox',{name:'Status',exact:true}).selectOption({label:test.status});
    if(test.query!==undefined || test.status) await settleFilters(page,test);
    if(test.mode==='detail') {
      await page.getByRole('button',{name:test.data[0].title,exact:true}).click();
      const detail=page.getByRole('region',{name:'Activity details',exact:true});
      await detail.waitFor();value.detail=await detail.innerText();
      value.detailReadable=await detail.evaluate(el=>{
        const rect=el.getBoundingClientRect();
        if(rect.left<0||rect.right>innerWidth+1||el.scrollWidth>el.clientWidth+2)return false;
        for(const p of [el,...el.querySelectorAll('*')]){
          const s=getComputedStyle(p);
          if(Number(s.opacity)<0.9||s.visibility!=='visible'||s.color==='rgba(0, 0, 0, 0)'||s.color==='transparent')return false;
          if(['hidden','clip'].includes(s.overflowY)&&p.scrollHeight>p.clientHeight+2)return false;
          if(['hidden','clip'].includes(s.overflowX)&&p.scrollWidth>p.clientWidth+2)return false;
        }
        return true;
      });
      value.screenshot=(await page.screenshot({type:'png'})).toString('base64');
      await page.getByRole('button',{name:'Close details',exact:true}).click();
      value.closed=!(await detail.isVisible());
    }
    if(test.mode==='keyboard') {
      const search=page.getByLabel('Search activities',{exact:true});
      await search.focus(); await page.keyboard.type(test.data[0].title);
      value.statusFocused=false;
      for(let i=0;i<20;i++){await page.keyboard.press('Tab');if(await page.getByRole('combobox',{name:'Status',exact:true}).evaluate(el=>el===document.activeElement)){value.statusFocused=true;break;}}
      const button=page.getByRole('button',{name:test.data[0].title,exact:true});
      value.recordFocused=false;
      for(let i=0;i<20;i++){await page.keyboard.press('Tab');if(await button.evaluate(el=>el===document.activeElement)){value.recordFocused=true;break;}}
      await page.keyboard.press('Enter');
      value.detailOpened=await page.getByRole('region',{name:'Activity details',exact:true}).isVisible();
    }
    value.titles=await visibleTitles(page,test.data);
    value.empty=await page.getByText('No activities found',{exact:true}).isVisible();
    value.error=await page.getByText('Could not load activities',{exact:true}).isVisible();
    value.requests=current.requests;
    if(test.mode==='layout') {
      if(test.largeText) await page.evaluate(()=>{
        const sizes=[...document.querySelectorAll('body,body *')].map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
        for(const [el,size] of sizes)el.style.setProperty('font-size',size*1.5+'px','important');
      });
      value.documentWidth=await page.evaluate(()=>document.documentElement.scrollWidth);
      value.viewportWidth=page.viewportSize().width;
      value.items=[];
      for(const item of test.data) {
        const button=page.getByRole('button',{name:item.title,exact:true});
        await button.scrollIntoViewIfNeeded();
        const box=await button.boundingBox();
        const observation=await button.evaluate(el=>{
          const style=getComputedStyle(el),rect=el.getBoundingClientRect();
          let clipped=false,nestedScroll=false;
          for(let p=el;p && p!==document.body && p!==document.documentElement;p=p.parentElement){
            const s=getComputedStyle(p),r=p.getBoundingClientRect();
            if(['auto','scroll'].includes(s.overflowY) && p.scrollHeight>p.clientHeight+2) nestedScroll=true;
            if(['hidden','clip'].includes(s.overflowX) && p.scrollWidth>p.clientWidth+2) clipped=true;
            if(['hidden','clip'].includes(s.overflowY) && p.scrollHeight>p.clientHeight+2) clipped=true;
            if(Number(s.opacity)<0.9 || s.visibility!=='visible'||s.color==='rgba(0, 0, 0, 0)'||s.color==='transparent') clipped=true;
          }
          const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let node;
          while(node=walker.nextNode()){if(!node.textContent.trim())continue;const range=document.createRange();range.selectNodeContents(node);
            for(const r of range.getClientRects()) if(r.left<rect.left-2 || r.right>rect.right+2 || r.top<rect.top-2 || r.bottom>rect.bottom+2) clipped=true;}
          const center=document.elementFromPoint(rect.left+rect.width/2,Math.max(0,Math.min(innerHeight-1,rect.top+rect.height/2)));
          return {clipped,nestedScroll,fontSize:parseFloat(style.fontSize),text:el.innerText,hit:!!center && (center===el||el.contains(center))};
        });
        value.items.push({box,...observation});
      }
      await page.evaluate(()=>scrollTo(0,0));
      value.screenshot=(await page.screenshot({type:'png'})).toString('base64');
    }
    results.push({ok:true,value});
   }catch(error){results.push({ok:false,error:{type:String(error.name).slice(0,128),message:String(error.message).slice(0,1024),operation:test.mode.slice(0,128)}});}
   finally{await context.close();}
  }
 } finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
 process.stdout.write(JSON.stringify({schema:1,results}));
})().catch(e=>{console.error(String(e.message).slice(0,1024));process.exitCode=1;server.close();});
'''


def program():
    return r'''
import base64,io,json,os,subprocess,sys,tarfile
from pathlib import Path
p=json.load(sys.stdin)
root=Path('/tmp/submission');root.mkdir()
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(p['source']))) as tar:
 for member in tar:
  if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts:
   raise ValueError('unsafe browser source archive')
  dest=root/member.name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(tar.extractfile(member).read())
driver=Path('/tmp/driver.cjs');driver.write_text(''' + repr(DRIVER) + r''')
result=subprocess.run(['node',str(driver)],input=json.dumps({'cases':p['cases']}).encode(),stdout=sys.stdout.buffer,stderr=sys.stderr.buffer)
sys.exit(result.returncode)
'''
