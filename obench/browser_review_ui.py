"""Trusted review shell; submitted markup/scripts never enter this origin."""

HTML = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="data:,"><title>OpenBench interactive review</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#eef1f5;color:#152337;font:15px system-ui,sans-serif}header{padding:22px 28px;background:white;border-bottom:1px solid #d3dbe6}h1{font-size:23px;margin:0 0 6px}p{margin:6px 0;color:#4b5b70}main{padding:20px;max-width:1600px;margin:auto}.bar{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-bottom:14px}label{display:flex;flex-direction:column;gap:5px;font-size:13px}button,select,input{font:inherit;padding:9px;border:1px solid #aab7c7;border-radius:6px;background:white;color:inherit}button{cursor:pointer}button:focus-visible,select:focus-visible,input:focus-visible,#screen:focus-visible{outline:3px solid #176ddb;outline-offset:2px}button:disabled{opacity:.5;cursor:wait}#stage{overflow:auto;background:#d9e0e9;padding:12px;border-radius:8px}#screen{display:block;max-width:100%;height:auto;cursor:crosshair;border:1px solid #aab7c7;background:white}#message{min-height:24px;white-space:pre-wrap}#meta{font-size:13px}details{margin-top:16px;background:white;border:1px solid #d3dbe6;border-radius:8px;padding:14px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:450px;overflow:auto}#controls{display:grid;gap:10px;margin-top:12px}.control{display:flex;flex-wrap:wrap;align-items:center;gap:10px;border-bottom:1px solid #edf0f4;padding-bottom:10px}.name{flex:1;min-width:180px;overflow-wrap:anywhere}.control input{max-width:100%;flex:1}code{overflow-wrap:anywhere} .hint{font-size:13px}
</style><header><h1>Interactive interface review</h1><p>Explore a frozen submission. Everyday content is the default; stress content is a separate view. These observations do not change benchmark scores.</p></header>
<main><div class="bar"><label>Candidate<select id="candidate"></select></label><label>Content<select id="fixture"></select></label><label>Viewport<select id="viewport"><option value="1440,900">Desktop · 1440 × 900</option><option value="768,900">Tablet · 768 × 900</option><option value="360,800">Mobile · 360 × 800</option></select></label><button id="reset">Load / reset</button><button id="refresh">Refresh observation</button></div>
<div class="bar"><label>Text to enter at current focus<input id="text" autocomplete="off"></label><button id="type">Type into interface</button><label>Key<select id="key"><option>Tab</option><option>Shift+Tab</option><option>Enter</option><option>Space</option><option>Escape</option><option>Backspace</option><option>ArrowDown</option><option>ArrowUp</option><option>PageDown</option><option>PageUp</option></select></label><button id="press">Send key</button><button id="up">Scroll up</button><button id="down">Scroll down</button></div>
<p class="hint">Click the rendered interface to interact. Wheel scrolling and supported keys also work when the image is focused. Semantic controls below support direct fill/select.</p><p id="message" role="status"></p><p id="meta"></p><div id="stage"><img id="screen" alt="Current candidate interface" tabindex="0"></div>
<details><summary>Accessible controls</summary><div id="controls"></div></details><details><summary>Accessibility snapshot and browser diagnostics</summary><pre id="accessibility"></pre></details><details><summary>Agent interface</summary><p>The same actions are available through <code>obench review inspect SESSION</code> and <code>obench review act SESSION --request FILE</code>. Inspect returns the latest sequence number, control references, accessibility snapshot and evidence paths. Each action is recorded separately.</p></details></main><script src="/app.js"></script></html>'''

SCRIPT = r'''
const $=id=>document.getElementById(id);
const token=location.hash.slice(1);
window.addEventListener('hashchange',()=>location.reload());
let seq=0,busy=false,blob,lastViewport={width:1440,height:900};
async function api(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});const v=await r.json();if(!r.ok)throw Error(v.error||'Review request failed');return v;}
function option(value,label){const o=document.createElement('option');o.value=value;o.textContent=label;return o;}
async function render(record){
 seq=record.seq;$('candidate').value=record.selection.candidate;$('fixture').value=record.selection.fixture;lastViewport=record.selection.viewport;
 $('viewport').value=[lastViewport.width,lastViewport.height].join(',');
 if(!record.result.ok){$('message').textContent=record.result.error;return;}
 const v=record.result.value;
 const response=await fetch('/'+v.screenshot,{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw Error('Screenshot unavailable');
 if(blob)URL.revokeObjectURL(blob);blob=URL.createObjectURL(await response.blob());$('screen').src=blob;
 $('screen').width=v.viewport.width;$('screen').height=v.viewport.height;
 $('meta').textContent='Candidate '+record.selection.candidate+' · '+record.selection.fixture+' · observation '+seq+' · '+v.viewport.width+' × '+v.viewport.height;
 $('accessibility').textContent=v.accessibility+'\n\nBrowser errors: '+JSON.stringify(v.errors)+'\nBlocked requests: '+JSON.stringify(v.blocked);
 $('controls').replaceChildren();
 for(const c of v.controls){
  const row=document.createElement('div');row.className='control';const label=document.createElement('span');label.className='name';label.textContent=c.tag+' · '+(c.name||'(unnamed)')+(c.focused?' · focused':'');row.append(label);
  if(c.tag==='select'){
   const select=document.createElement('select');select.setAttribute('aria-label',c.name);for(const o of c.options||[])select.append(option(o.value,o.label));select.value=c.value;select.disabled=c.disabled;
   select.onchange=()=>act({action:'select',ref:c.ref,value:select.value});row.append(select);
  }else if(['input','textarea'].includes(c.tag)&&!['checkbox','radio','button','submit','file','range'].includes(c.type)){
   const input=document.createElement('input');input.value=c.value;input.setAttribute('aria-label',c.name);input.disabled=c.disabled;row.append(input);const b=document.createElement('button');b.textContent='Fill';b.disabled=c.disabled;b.onclick=()=>act({action:'fill',ref:c.ref,text:input.value});row.append(b);
  }else{const b=document.createElement('button');b.textContent='Activate';b.setAttribute('aria-label','Activate '+c.name);b.disabled=c.disabled;b.onclick=()=>act({action:'click',ref:c.ref});row.append(b);}
  $('controls').append(row);
 }
}
function lockControls(){for(const el of document.querySelectorAll('button,select,input')){el.dataset.reviewDisabled=String(el.disabled);el.disabled=true;}$('screen').style.pointerEvents='none';}
function unlockControls(){for(const el of document.querySelectorAll('[data-review-disabled]')){el.disabled=el.dataset.reviewDisabled==='true';delete el.dataset.reviewDisabled;}$('screen').style.pointerEvents='';}
async function act(action){if(busy)return;busy=true;lockControls();$('message').textContent='Updating…';try{const record=await api('/act',{...action,seq});await render(record);if(record.result.ok)$('message').textContent='Observation saved.';}catch(e){$('message').textContent=e.message;try{const s=await api('/status');if(s.last)await render(s.last);}catch{}}finally{busy=false;unlockControls();}}
$('reset').onclick=()=>{const [width,height]=$('viewport').value.split(',').map(Number);act({action:'reset',candidate:$('candidate').value,fixture:$('fixture').value,viewport:{width,height}});};
$('refresh').onclick=()=>act({action:'snapshot'});
$('type').onclick=()=>act({action:'type',text:$('text').value});
$('press').onclick=()=>act({action:'key',key:$('key').value});
function scroll(delta){act({action:'scroll',x:Math.floor(lastViewport.width/2),y:Math.floor(lastViewport.height/2),delta});}
$('up').onclick=()=>scroll(-500);$('down').onclick=()=>scroll(500);
function coords(e){const r=$('screen').getBoundingClientRect();return {x:Math.min(lastViewport.width-1,Math.max(0,Math.floor((e.clientX-r.left)*lastViewport.width/r.width))),y:Math.min(lastViewport.height-1,Math.max(0,Math.floor((e.clientY-r.top)*lastViewport.height/r.height)))};}
$('screen').onclick=e=>act({action:'click',...coords(e)});
$('screen').onwheel=e=>{e.preventDefault();act({action:'scroll',...coords(e),delta:Math.max(-1200,Math.min(1200,Math.round(e.deltaY)))});};
$('screen').onkeydown=e=>{let key=e.key===' '?'Space':e.key;if(e.shiftKey&&key==='Tab')key='Shift+Tab';if(['Tab','Shift+Tab','Enter','Space','Escape','Backspace','ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End','PageDown','PageUp'].includes(key)){e.preventDefault();act({action:'key',key});}else if(key.length===1&&!e.metaKey&&!e.ctrlKey&&!e.altKey){e.preventDefault();act({action:'type',text:key});}};
(async()=>{try{const s=await api('/status');for(const c of s.candidates)$('candidate').append(option(c,c));for(const f of s.fixtures)$('fixture').append(option(f,f));await render(s.last);$('message').textContent='Ready.';}catch(e){$('message').textContent=e.message+' Open the session URL containing its access fragment.';}})();
'''
