"""V2 browser observations through CDP and an isolated execution world. Authoritative comparisons stay on the host.

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
// CDP-owned node identity plus an isolated world. Never fall back to page code.
class Observer {
 static async create(page) {
  const observer=new Observer();
  observer.cdp=await page.context().newCDPSession(page);
  const {frameTree}=await observer.cdp.send('Page.getFrameTree');
  observer.frameId=frameTree.frame.id;
  const world=await observer.cdp.send('Page.createIsolatedWorld',{frameId:observer.frameId,worldName:'openbench-observer-v2'});
  observer.contextId=world.executionContextId;
  const doc=await observer.cdp.send('Runtime.evaluate',{expression:'document',contextId:observer.contextId,objectGroup:'obench-observer'});
  if(doc.exceptionDetails||!doc.result.objectId)throw Error('isolated observer unavailable');
  observer.document=doc.result.objectId;
  return observer;
 }
 async nodes(role,name,fold=false) {
  const {nodes}=await this.cdp.send('Accessibility.getFullAXTree',{frameId:this.frameId});
  return nodes.filter(n=>!n.ignored && n.backendDOMNodeId && n.role?.value===role &&
   (name===undefined || (fold ? n.name?.value?.toLowerCase()===name.toLowerCase() : n.name?.value===name)));
 }
 async one(role,name,fold=false) {
  const nodes=await this.nodes(role,name,fold);
  if(nodes.length!==1)throw Error('expected one accessible '+role+': '+name);
  return nodes[0];
 }
 async object(node) {
  if(!node)return this.document;
  const {object}=await this.cdp.send('DOM.resolveNode',{backendNodeId:node.backendDOMNodeId,executionContextId:this.contextId,objectGroup:'obench-observer'});
  if(!object.objectId)throw Error('observer node unavailable');
  return object.objectId;
 }
 async measure(node,fn,arg=null) {
  const result=await this.cdp.send('Runtime.callFunctionOn',{
   objectId:await this.object(node),functionDeclaration:'function(arg){return ('+fn.toString()+')(this,arg)}',
   arguments:[{value:arg}],returnByValue:true,awaitPromise:true});
  if(result.exceptionDetails)throw Error('isolated observation failed: '+(result.exceptionDetails.text||'exception'));
  if(!Object.hasOwn(result.result,'value'))throw Error('missing isolated observation');
  return result.result.value;
 }
 async titles(data) {
  const wanted=new Map(data.map(r=>[r.title.toLowerCase(),r.title]));
  const nodes=(await this.nodes('button')).filter(n=>wanted.has(n.name?.value?.toLowerCase()));
  const args=await Promise.all(nodes.map(async n=>({objectId:await this.object(n)})));
  const result=await this.cdp.send('Runtime.callFunctionOn',{objectId:this.document,
   functionDeclaration:'function(...nodes){return nodes.map((el,i)=>({el,i})).sort((a,b)=>a.el.compareDocumentPosition(b.el)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1).map(r=>r.i)}',
   arguments:args,returnByValue:true});
  if(result.exceptionDetails||!Array.isArray(result.result.value))throw Error('isolated ordering failed');
  return result.result.value.map(i=>wanted.get(nodes[i].name.value.toLowerCase()));
 }
 async detail() {
  const regions=await this.nodes('region','Activity details');
  if(regions.length===1)return regions[0];
  if(regions.length>1)throw Error('ambiguous detail regions');
  return this.one('dialog','Activity details');
 }
 async detailOpen() {
  return (await this.nodes('region','Activity details')).length>0 || (await this.nodes('dialog','Activity details')).length>0;
 }
}
function recordButton(page,title) {
 return page.getByRole('button',{name:new RegExp('^'+title.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'$','i')});
}
async function visibleTitles(observer,data) { return observer.titles(data); }
async function settleFilters(page,observer,test) {
 // A wait hint derived from public inputs, never a verdict. Read fresh browser
 // observations afterward; the host independently checks their correctness.
 const titles=test.data.filter(r=>
  (r.title+' '+r.project).toLowerCase().includes((test.query||'').toLowerCase()) &&
  (!test.status || r.status===test.status.toLowerCase())).map(r=>r.title);
 const deadline=Date.now()+2000;
 do {
  const observed=await visibleTitles(observer,test.data);
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
   await context.routeWebSocket(/.*/,ws=>ws.close());
   page.on('dialog',d=>d.dismiss().catch(()=>{}));
   context.on('page',p=>{if(p!==page)p.close().catch(()=>{});});
   try {
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    const observer=await Observer.create(page);
    let value={observer:'cdp-isolated-world-v2'};
    if(test.mode==='loading') {
      await page.getByText('Loading activities',{exact:true}).waitFor();
      value.loading=await page.getByText('Loading activities',{exact:true}).isVisible();
    }
    if(test.mode==='retry') {
      await page.getByText('Could not load activities',{exact:true}).waitFor();
      await page.getByRole('button',{name:'Retry',exact:true}).click();
    }
    if(test.data.length) await recordButton(page,test.data[0].title).waitFor();
    else await page.getByText('No activities found',{exact:true}).waitFor();
    if(test.query!==undefined) await page.getByLabel('Search activities',{exact:true}).fill(test.query);
    if(test.status) await page.getByRole('combobox',{name:'Status',exact:true}).selectOption({label:test.status});
    if(test.query!==undefined || test.status) await settleFilters(page,observer,test);
    if(test.mode==='detail') {
      await recordButton(page,test.data[0].title).click();
      await page.getByRole('region',{name:'Activity details',exact:true}).or(page.getByRole('dialog',{name:'Activity details',exact:true})).first().waitFor();
      const detail=await observer.detail();
      value.detail=await observer.measure(detail,el=>el.innerText);
      value.detailRole=detail.role.value;
      value.detailReadable=await observer.measure(detail,el=>{
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
      value.closed=!(await observer.detailOpen());
    }
    if(test.mode==='keyboard') {
      const search=page.getByLabel('Search activities',{exact:true});
      await search.focus(); await page.keyboard.type(test.data[0].title);
      value.statusFocused=false;
      for(let i=0;i<20;i++){await page.keyboard.press('Tab');if(await observer.measure(await observer.one('combobox','Status'),el=>el===document.activeElement)){value.statusFocused=true;break;}}
      value.recordFocused=false;
      for(let i=0;i<20;i++){await page.keyboard.press('Tab');if(await observer.measure(await observer.one('button',test.data[0].title,true),el=>el===document.activeElement)){value.recordFocused=true;break;}}
      await page.keyboard.press('Enter');
      await page.getByRole('button',{name:'Close details',exact:true}).waitFor();
      value.detailOpened=await observer.detailOpen();
      value.closeFocused=false;
      for(let i=0;i<20;i++){
       if(await observer.measure(await observer.one('button','Close details'),el=>el===document.activeElement)){value.closeFocused=true;break;}
       await page.keyboard.press('Tab');
      }
      await page.keyboard.press('Enter');
      value.detailClosed=!(await observer.detailOpen());
    }
    value.titles=await visibleTitles(observer,test.data);
    value.empty=await page.getByText('No activities found',{exact:true}).isVisible();
    value.error=await page.getByText('Could not load activities',{exact:true}).isVisible();
    value.requests=current.requests;
    if(test.mode==='layout') {
      if(test.largeText) await observer.measure(null,()=>{
        const sizes=[...document.querySelectorAll('body,body *')].map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
        for(const [el,size] of sizes)el.style.setProperty('font-size',size*1.5+'px','important');
        return true;
      });
      value.documentWidth=await observer.measure(null,()=>document.documentElement.scrollWidth);
      value.viewportWidth=page.viewportSize().width;
      value.items=[];
      for(const item of test.data) {
        const button=recordButton(page,item.title);
        await button.scrollIntoViewIfNeeded();
        const node=await observer.one('button',item.title,true);
        const box=await observer.measure(node,el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
        const observation=await observer.measure(node,el=>{
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
      await observer.measure(null,()=>{scrollTo(0,0);return true;});
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


OBSERVER = DRIVER[DRIVER.index('class Observer'):DRIVER.index('async function visibleTitles')]

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
