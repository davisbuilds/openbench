"""Synthetic correct and defective UI controls; never supplied to solvers."""
from pathlib import Path

HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Activity explorer</title><link rel="stylesheet" href="/style.css"></head><body>
<main><header><p class="eyebrow">WORKSPACE / ACTIVITY</p><h1>Your work, in view.</h1><p>Find an activity and inspect its latest state.</p></header>
<div class="filters"><label>Search activities<input type="search" aria-label="Search activities"></label><label>Status<select aria-label="Status"><option>All</option><option>Running</option><option>Completed</option><option>Failed</option></select></label></div>
<p id="state" role="status"></p><button id="retry" hidden>Retry</button><div id="list"></div>
<section aria-label="Activity details" hidden><button id="close">Close details</button><h2></h2><p class="metadata"></p><p class="description"></p></section></main><script src="/app.js"></script></body></html>'''
JS = '''const list=document.querySelector('#list'),state=document.querySelector('#state'),retry=document.querySelector('#retry'),search=document.querySelector('input'),status=document.querySelector('select'),detail=document.querySelector('section');
let rows=[];
function render(){list.replaceChildren();const query=search.value.toLowerCase();
const filtered=rows.filter(r=>(r.title+' '+r.project).toLowerCase().includes(query)&&(status.value==='All'||r.status===status.value.toLowerCase()));
state.textContent=filtered.length?'':'No activities found';
for(const r of filtered){const card=document.createElement('article');const button=document.createElement('button');button.className='activity';button.textContent=r.title;button.onclick=()=>{detail.hidden=false;detail.querySelector('h2').textContent=r.title;detail.querySelector('.metadata').textContent=r.project+' · '+r.status;detail.querySelector('.description').textContent=r.description;detail.scrollIntoView({block:'start'});};const meta=document.createElement('p');meta.textContent=r.project+' · '+r.status;card.append(button,meta);list.append(card);}}
async function load(){list.replaceChildren();retry.hidden=true;state.textContent='Loading activities';try{const response=await fetch('/api/activities');if(!response.ok)throw Error('load');rows=(await response.json()).activities;render();}catch{state.textContent='Could not load activities';retry.hidden=false;}}
search.oninput=render;status.onchange=render;retry.onclick=load;document.querySelector('#close').onclick=()=>{detail.hidden=true;};load();'''
CSS = '''*{box-sizing:border-box}body{margin:0;background:#f5f4ef;color:#202b30;font:16px/1.55 system-ui,sans-serif}main{max-width:1000px;margin:auto;padding:clamp(16px,4vw,48px)}header{margin-bottom:28px}h1{font-size:clamp(1.8rem,5vw,3rem);line-height:1.12;margin:8px 0}h2{font-size:1.5rem}.eyebrow{font-size:.875rem;font-weight:700;letter-spacing:.14em;color:#38624c}.filters{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}.filters label{display:flex;flex-direction:column;gap:6px;flex:1;min-width:0}input,select,button{font:inherit;max-width:100%;min-width:0}input,select{padding:10px;border:1px solid #65766d;border-radius:6px;background:white;width:100%}button{cursor:pointer;padding:12px;border:1px solid #667a70;background:white;border-radius:6px;color:inherit}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #0064cc;outline-offset:3px}article{padding:18px 0;border-bottom:1px solid #c6cec7}article p{margin:8px 0;color:#44554c}.activity{border:0;background:transparent;text-align:left;padding:0;font-weight:600;display:block;width:100%;overflow-wrap:anywhere;min-height:28px}section{margin-top:32px;border-top:3px solid #38624c;padding-top:24px;overflow-wrap:anywhere}.description{white-space:pre-wrap}h2,p{overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:480px){.filters{flex-direction:column}}'''
CARDS = '''\nbody{background:#111b24;color:#e1eaf0}.eyebrow,article p{color:#add1c2}#list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px}article{border:1px solid #51606b;padding:20px;border-radius:12px;background:#1b2935}input,select,button{background:#1b2935;color:#e1eaf0}section{padding:24px;border:1px solid #51606b;border-radius:12px}'''
CARD_JS = (JS.replace('button.textContent=r.title;',
    "button.setAttribute('aria-label',r.title);const title=document.createElement('span');title.textContent=r.title;button.append(title);")
    .replace('card.append(button,meta);', 'button.append(meta);card.append(button);')
    .replace("r.project+' · '+r.status", "r.project+' · '+r.status[0].toUpperCase()+r.status.slice(1)")
    .replace("detail.querySelector('.description').textContent=r.description;",
        "detail.querySelector('.description').replaceChildren(...r.description.split('\\n').map(line=>{const p=document.createElement('p');p.textContent=line;return p;}));"))
CARD_JS = (CARD_JS.replace('search.oninput=render;status.onchange=render;',
    'let timer;const update=()=>{clearTimeout(timer);timer=setTimeout(render,300);};search.oninput=update;status.onchange=update;')
    .removesuffix('load();')+'setTimeout(load,200);')
CARD_HTML = HTML.replace('<p class="description"></p>', '<div class="description"></div>')


def prepare(root):
    root=Path(root)
    variants={
        'valid-list':(HTML,JS,CSS),
        'valid-cards':(CARD_HTML,CARD_JS,CSS+CARDS),
        'missing-visible-title':(CARD_HTML,CARD_JS.replace('title.textContent=r.title','title.textContent=""'),CSS+CARDS),
        'truncated-description':(HTML,JS.replace('textContent=r.description','textContent=r.description.slice(0,30)'),CSS),
        'broken-filter':(HTML,JS.replace("&&(status.value==='All'||r.status===status.value.toLowerCase())",''),CSS),
        'clipped-title':(HTML,JS,CSS+'\n.activity{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'),
        'nested-scroll':(HTML,JS,CSS+'\n#list{height:120px;overflow-y:auto}'),
        'hidden-text':(HTML,JS,CSS+'\n.activity{color:transparent;font-size:1px;height:28px}'),
        'broken-retry':(HTML,JS.replace('retry.onclick=load','retry.onclick=()=>{}'),CSS),
        'broken-keyboard':(HTML,JS.replace("button.onclick=()=>", "button.tabIndex=-1;button.onkeydown=e=>e.preventDefault();button.onclick=()=>"),CSS),
        'reversed-list':(HTML,JS.replace('for(const r of filtered)', 'for(const r of filtered.reverse())'),CSS),
        'duplicate-record':(HTML,JS.replace('for(const r of filtered)', 'for(const r of [...filtered,...filtered])'),CSS),
        'wide-detail':(HTML,JS,CSS+'\nsection{min-width:900px}'),
    }
    for name,(html,js,css) in variants.items():
        web=root/name/'web';web.mkdir(parents=True,exist_ok=False)
        for file,value in [('index.html',html),('app.js',js),('style.css',css)]:
            (web/file).write_text(value)
    return root
