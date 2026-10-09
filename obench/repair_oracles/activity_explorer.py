"""Activity explorer v1: host-owned predicates over confined browser observations."""
import re

from ..browser_worker import program as worker_program


def data(count=9):
    return [{'id':f'item-{i}', 'title': ('Investigate '+('LongUnbrokenIdentifier' * 5) if i==0 else f'Inspect operation {i}'),
             'project':['Observatory','Ledger','Workspace'][i%3],
             'status':['running','completed','failed'][(i//3)%3],
             'description':(f'Activity {i} details.\n'+('Preserve every line.\n'*30)+('UnbrokenDescription'*100))[:2000]} for i in range(count)]


def cases():
    rows=data()
    definitions=[('list','function',{}),('search','function',{'query':'OPERATION 1'}),
                 ('combined-filter','function',{'query':'ledger','status':'Completed'}),
                 ('empty-search','function',{'query':'nothing-matches-this'}),
                 ('empty-data','states',{'data':[]}),('loading','states',{'mode':'loading'}),
                 ('retry','states',{'mode':'retry'}),('detail','function',{'mode':'detail'}),
                 ('detail-mobile','layout',{'mode':'detail','viewport':{'width':360,'height':640}}),
                 ('keyboard','keyboard',{'mode':'keyboard'})]
    for width in (360,768,1440):
        definitions.append((f'layout-{width}','layout',{'mode':'layout','viewport':{'width':width,'height':800}}))
    dense=data(30)
    dense[-1]['project']='LongProjectName'*5+'ABCDE'
    dense[-1]['title']='LongTitle'*17+'abcdefg'
    definitions.append(('dense-mobile','layout',{'mode':'layout','data':dense,'viewport':{'width':360,'height':640}}))
    definitions.append(('large-text','layout',{'mode':'layout','largeText':True,'viewport':{'width':360,'height':800}}))
    return [(name,bucket,{'mode':'list','data':rows,**args}) for name,bucket,args in definitions]


def contains_text(text, expected, *, casefold=False):
    """Compare complete displayed content without prescribing its formatting."""
    if not isinstance(text,str): return False
    text,expected=' '.join(text.split()),' '.join(expected.split())
    if casefold: text,expected=text.casefold(),expected.casefold()
    return bool(re.search(r'(?<!\w)'+re.escape(expected)+r'(?!\w)',text))


def compare(name, value, request):
    expected=[r['title'] for r in request['data'] if
              request.get('query','').lower() in (r['title']+' '+r['project']).lower() and
              (not request.get('status') or r['status']==request['status'].lower())]
    if name=='keyboard':
        return value.get('statusFocused') is True and value.get('recordFocused') is True and value.get('detailOpened') is True
    if value.get('titles')!=expected or value.get('empty')!=(not expected) or value.get('error') is not False:
        return False
    if request['mode']=='detail':
        row=request['data'][0]
        return (value.get('closed') is True and value.get('detailReadable') is True
                and all(contains_text(value.get('detail'),row[k],casefold=k=='status')
                        for k in ('title','project','status','description')))
    if name=='loading': return value.get('loading') is True
    if name=='retry': return value.get('requests')==2
    if request['mode']=='layout':
        items=value.get('items',[])
        return (value.get('documentWidth',10**6)<=request['viewport']['width']+1
                and len(items)==len(expected) and all(
                    contains_text(item.get('text'),title) and item.get('clipped') is False and item.get('nestedScroll') is False
                    and item.get('hit') is True and item.get('fontSize',0)>=14
                    and item.get('box') and item['box']['width']>0 and item['box']['height']>=24
                    for item,title in zip(items,expected)))
    return True


def grade(results):
    checks=[]
    for (name,bucket,request),result in zip(cases(),results,strict=True):
        try: passed=result.get('ok') is True and compare(name,result['value'],request)
        except (KeyError,TypeError,ValueError): passed=False
        checks.append({'case':name,'bucket':bucket,'pass':bool(passed)})
    buckets={bucket:all(c['pass'] for c in checks if c['bucket']==bucket) for _,bucket,_ in cases()}
    return {'score':sum(buckets.values())/len(buckets),'checks':checks,'buckets':buckets,'oracle_version':1,
            'scope':'declared functional/layout checks; design preference is unscored'}


def validate_runtime(image):
    # Launch before loading candidate assets, under the same confinement used
    # for grading. A missing/broken Chromium is an infrastructure error.
    from ..repair_worker import run_worker
    from ..sandbox_grading import CandidateFailure, GradingError
    code="""const pw=require('/opt/browser-deps/node_modules/playwright');
    (async()=>{const b=await pw.chromium.launch({chromiumSandbox:true});
    const value={playwright:require('/opt/browser-deps/node_modules/playwright/package.json').version,chromium:b.version(),sandbox:true};
    await b.close();console.log(JSON.stringify({schema:1,results:[{ok:true,value}]}));})().catch(e=>{console.error(e.message);process.exitCode=1});"""
    probe='import subprocess,sys;sys.exit(subprocess.call('+repr(['node','-e',code])+'))'
    try:
        results,_=run_worker(image,b'', [{}],program=probe,timeout=30,browser=True)
    except CandidateFailure as exc:
        raise GradingError('sandboxed Chromium preflight failed') from exc
    value=results[0].get('value',{})
    if value.get('playwright')!='1.64.0' or value.get('sandbox') is not True or not value.get('chromium'):
        raise GradingError('browser runtime dependency mismatch')
    return value
