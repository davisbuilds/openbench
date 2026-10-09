"""Interactive observation driver. Candidate JS runs only in sandboxed Chromium."""

DRIVER = r'''
const http=require('node:http'), readline=require('node:readline');
const {chromium}=require('/opt/browser-deps/node_modules/playwright');
let browser,context,page,server,origin,files={},fixture={},requests=0,targets=[];
let errors=[],blocked=[];
const bounded=(s,n=2048)=>String(s).slice(0,n);
function record(list,value){if(list.length<30)list.push(bounded(value));}
async function boot(){
 server=http.createServer((req,res)=>{
  let name;try{name=decodeURIComponent(new URL(req.url,origin).pathname);}catch{res.writeHead(400);return res.end();}
  if(name==='/api/activities'){
   const failed=fixture.mode==='retry' && requests++===0;
   return setTimeout(()=>{res.writeHead(failed?503:200,{'Content-Type':'application/json'});res.end(JSON.stringify({activities:fixture.data}));},fixture.mode==='loading'?1500:0);
  }
  const key='web/'+(name==='/'?'index.html':name.slice(1));
  if(!Object.hasOwn(files,key)){res.writeHead(404);return res.end();}
  const ext=key.split('.').pop();
  res.writeHead(200,{'Content-Type':({html:'text/html',js:'text/javascript',css:'text/css',json:'application/json',svg:'image/svg+xml',png:'image/png',woff2:'font/woff2'})[ext]||'application/octet-stream'});
  res.end(Buffer.from(files[key],'base64'));
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({chromiumSandbox:true});
 if(require('/opt/browser-deps/node_modules/playwright/package.json').version!=='1.64.0')throw Error('review requires Playwright 1.64.0');
}
async function reset(c){
 if(context)await context.close();
 files=c.files;fixture=c.fixture;requests=0;errors=[];blocked=[];
 context=await browser.newContext({viewport:c.viewport,locale:'en-US',timezoneId:'UTC',deviceScaleFactor:1,serviceWorkers:'block',reducedMotion:'reduce',acceptDownloads:false});
 await context.route('**/*',r=>{
  if(r.request().url().startsWith(origin+'/'))return r.continue();
  record(blocked,r.request().url());return r.abort();
 });
 await context.routeWebSocket(/.*/,ws=>{record(blocked,ws.url());ws.close();});
 page=await context.newPage();page.setDefaultTimeout(2000);page.setDefaultNavigationTimeout(5000);
 context.on('page',p=>{if(p!==page)p.close().catch(()=>{});});
 page.on('pageerror',e=>record(errors,e.message));
 page.on('dialog',d=>d.dismiss().catch(()=>{}));
 await page.goto(origin,{waitUntil:'domcontentloaded'});
}
async function snapshot(){
 for(const t of targets)await t.dispose().catch(()=>{});
 targets=[];
 const all=await page.locator('button,input,select,textarea,a[href],[tabindex],[role="button"],[contenteditable="true"]').all();
 const controls=[];
 for(const locator of all){
  const el=await locator.elementHandle();
  if(!el)continue;
  if(targets.length>=150 || !(await el.isVisible())){await el.dispose();continue;}
  const info=await el.evaluate(el=>({tag:el.tagName.toLowerCase(),type:el.type||'',
    name:(el.getAttribute('aria-label')||((el.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.textContent||'').join(' ').trim())||[...(el.labels||[])].map(l=>l.innerText).join(' ')||el.innerText||el.getAttribute('placeholder')||'').slice(0,500),
    value:String(el.value||'').slice(0,2000),disabled:!!el.disabled,focused:el===document.activeElement,
    options:el.tagName==='SELECT'?[...el.options].slice(0,100).map(o=>({value:o.value,label:o.label})):undefined}));
  const semantic=await locator.ariaSnapshot();
  const match=semantic.match(/^-[ \t]+[^ \t]+[ \t]+("(?:[^"\\]|\\.)*")/);
  if(match){try{info.name=JSON.parse(match[1]).slice(0,500);}catch{}}
  info.accessibility=bounded(semantic,2000);
  controls.push({ref:targets.length,...info});targets.push(el);
 }
 return {viewport:page.viewportSize(),url:bounded(page.url()),
  text:bounded(await page.locator('body').innerText(),24000),
  accessibility:bounded(await page.locator('body').ariaSnapshot(),30000),controls,
  screenshot:(await page.screenshot({type:'png',timeout:5000})).toString('base64'),
  errors:[...errors],blocked:[...blocked],runtime:{playwright:'1.64.0',chromium:browser.version(),chromium_sandbox:true}};
}
async function act(c){
 if(!browser)await boot();
 if(c.action==='reset')await reset(c);
 else {
  if(!page)throw Error('reset first');
  let el;if(c.ref!==undefined){el=targets[c.ref];if(!el)throw Error('unknown control ref');}
  switch(c.action){
   case 'snapshot':break;
   case 'click':el?await el.click():await page.mouse.click(c.x,c.y);break;
   case 'fill':await el.fill(c.text);break;
   case 'select':await el.selectOption(c.value);break;
   case 'type':await page.keyboard.insertText(c.text);break;
   case 'key':await page.keyboard.press(c.key);break;
   case 'scroll':await page.mouse.move(c.x,c.y);await page.mouse.wheel(0,c.delta);break;
   case 'viewport':await page.setViewportSize(c.viewport);break;
   case 'wait':await page.waitForTimeout(c.ms);break;
   default:throw Error('unknown review action');
  }
 }
 await page.waitForTimeout(120);
 return snapshot();
}
let queue=Promise.resolve();
readline.createInterface({input:process.stdin}).on('line',line=>{
 queue=queue.then(async()=>{
  try{const value=await act(JSON.parse(line));process.stdout.write(JSON.stringify({ok:true,value})+'\n');}
  catch(e){process.stdout.write(JSON.stringify({ok:false,error:bounded(e.message)})+'\n');}
 });
}).on('close',()=>queue.finally(async()=>{if(browser)await browser.close();if(server)server.close();}));
'''
