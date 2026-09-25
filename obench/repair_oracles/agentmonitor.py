"""Corrected AgentMonitor #106 oracle. Expectations stay in this trusted module."""
import json

# Public observation protocol. Inputs describe operations, never assertions or
# expected outputs. Both this program and all candidate modules run confined.
NODE_PROTOCOL = r'''
import fs from 'node:fs';
import path from 'node:path';
console.log=(...args)=>console.error(...args);
const root=process.cwd();
const database=path.join(root,'fixture.db');
process.env.AGENTMONITOR_DB_PATH=database;
const connection=await import('./src/db/connection.ts');
const schema=await import('./src/db/schema.ts');
const queries=await import('./src/db/queries.ts');
const benchmark=await import('./src/import/benchmark.ts');
const v2=await import('./src/db/v2-queries.ts');
const requests=JSON.parse(fs.readFileSync(0,'utf8'));
const results=[];
let serial=0;
for (const request of requests) {
  try {
    connection.closeDb();
    for(const suffix of ['', '-wal', '-shm']) fs.rmSync(database+suffix,{force:true});
    const values=[];
    for (const step of request.steps) {
      let value=null;
      switch(step.op) {
        case 'init': schema.initSchema(); break;
        case 'close': connection.closeDb(); break;
        case 'sql': connection.getDb().exec(step.sql); break;
        case 'query': value=connection.getDb().prepare(step.sql).all(...(step.params||[])); break;
        case 'seed': connection.getDb().prepare(step.sql).run(...step.params); break;
        case 'insert': queries.insertEvent(step.event); break;
        case 'import': {
          const directory=path.join(root,step.directory||'inputs');
          fs.mkdirSync(path.join(directory,'nested'),{recursive:true});
          const file=path.join(directory,step.file||`cells-${serial++}.jsonl`);
          fs.writeFileSync(file,step.rows.map(row=>JSON.stringify(row)).join('\n')+'\n');
          let argument=file;
          if(step.path==='bare') {process.chdir(directory);argument=path.basename(file);}
          if(step.path==='parent') {process.chdir(path.join(directory,'nested'));argument='../'+path.basename(file);}
          try {value=benchmark.importBenchmarkResults(argument,step.options||{});}
          finally {process.chdir(root);}
          break;
        }
        case 'studies': value=v2.getBenchmarkStudies(); break;
        case 'study': value=v2.getBenchmarkStudy(step.study); break;
        case 'usage': value=v2.getUsageSummary(step.options||{}); break;
        default: throw new Error('unknown operation');
      }
      values.push(value===undefined?null:value);
    }
    results.push({ok:true,value:values});
  } catch (_) {results.push({ok:false});}
  finally {process.chdir(root);connection.closeDb();}
}
process.stdout.write(JSON.stringify({schema:1,results}));
'''


def worker_program():
    # The dependency image contains only public packages and this request-time
    # protocol; no oracle or solution is installed. No candidate runs on host.
    return '''import base64,io,json,subprocess,sys,tarfile,tempfile
from pathlib import Path
request=json.loads(sys.stdin.buffer.read(4194305))
with tempfile.TemporaryDirectory(prefix='candidate-') as directory:
    root=Path(directory)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['source']))) as archive:
        archive.extractall(directory,filter='data')
    (root/'package.json').write_text('{"type":"module"}')
    (root/'node_modules').symlink_to('/opt/repair-deps/node_modules',target_is_directory=True)
    (root/'observe.mjs').write_text('''+repr(NODE_PROTOCOL)+''')
    result=subprocess.run(['node','--import','/opt/repair-deps/node_modules/tsx/dist/loader.mjs','observe.mjs'],
        input=json.dumps(request['cases']).encode(),cwd=root,check=False)
    sys.exit(result.returncode)
'''


LEGACY_SCHEMA = "\n    CREATE TABLE events (\n      id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE,\n      schema_version INTEGER NOT NULL DEFAULT 1, session_id TEXT NOT NULL,\n      agent_type TEXT NOT NULL, event_type TEXT NOT NULL, tool_name TEXT,\n      status TEXT NOT NULL DEFAULT 'success', tokens_in INTEGER DEFAULT 0,\n      tokens_out INTEGER DEFAULT 0, branch TEXT, project TEXT, duration_ms INTEGER,\n      created_at TEXT NOT NULL DEFAULT (datetime('now')), client_timestamp TEXT,\n      metadata TEXT DEFAULT '{}', payload_truncated INTEGER NOT NULL DEFAULT 0,\n      model TEXT, cost_usd REAL, cache_read_tokens INTEGER DEFAULT 0,\n      cache_write_tokens INTEGER DEFAULT 0, source TEXT DEFAULT 'api',\n      study_id TEXT, study TEXT);\n    PRAGMA user_version = 4;\n  "
RESTRICTIVE_SCHEMA = "CREATE TABLE events (\n      id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, schema_version INTEGER NOT NULL DEFAULT 1,\n      session_id TEXT NOT NULL, agent_type TEXT NOT NULL,\n      event_type TEXT NOT NULL CHECK (event_type IN ('tool_use', 'response', 'error')),\n      tool_name TEXT, status TEXT NOT NULL DEFAULT 'success', tokens_in INTEGER DEFAULT 0,\n      tokens_out INTEGER DEFAULT 0, branch TEXT, project TEXT, duration_ms INTEGER,\n      created_at TEXT NOT NULL DEFAULT (datetime('now')), metadata TEXT DEFAULT '{}',\n      source TEXT DEFAULT 'api', study_id TEXT, study TEXT);\n    INSERT INTO events (event_id,session_id,agent_type,event_type,source,study_id,study,tokens_in)\n    VALUES ('retained','retained','codex','response','benchmark','existing-study','existing-slug',99);\n  "

ROW = dict(run_id='codex:parser:gold-model:trial1', harness='codex', model='gold-model',
           canonical_model='gold-model', task='parser', trial=1, score=.75, success=True,
           cost_usd=.25, cost_source='captured', tokens_input_uncached=100,
           tokens_output=20, study='shared-slug', study_sha256='study-A')
BENCH_QUERY = "SELECT event_id,session_id,study_id,study,tokens_in,cost_usd,metadata FROM events WHERE source='benchmark' ORDER BY study_id,event_id"


def step(op, **kwargs): return dict(op=op, **kwargs)
def row(**kwargs): return {**ROW, **kwargs}
def imported(rows=None, **kwargs): return step('import', rows=rows or [row()], **kwargs)
def bench(): return step('query',sql=BENCH_QUERY)
def usage(**options): return step('usage',options=options)


def cell(study, model, task, trial, score):
    return step('insert',event=dict(event_id=f'{study}-{model}-{task}-{trial}',session_id=f'{study}-{model}',
        agent_type='codex',event_type='llm_response',status='success',source='benchmark',study_id=study,
        study=study,project=task,model=model,cost_usd=.25,tokens_in=100,tokens_out=20,
        metadata=dict(canonical_model=model,task=task,trial=trial,score=score,success=score is not None,
                      cost_source='captured',is_open_model=True)))


def cases():
    cases=[]
    def add(name,bucket,steps): cases.append((name,bucket,{'steps':steps}))
    add('ID1','identity',[step('init'),imported(),imported([row(study_sha256='study-B',cost_usd=.5)]),
                        imported(),imported([row(study_sha256='study-B',cost_usd=.5)]),bench(),step('studies')])
    legacy={k:v for k,v in row().items() if k not in ('study','study_sha256')}
    add('ID2','identity',[step('init'),*[imported([legacy],directory='nightly-study',file='cells.jsonl',path=style)
                        for style in ('absolute','bare','parent')],bench()])
    add('ID3','identity',[step('init'),*[imported([legacy],directory=name,file='results.jsonl',path='bare')
                        for name in ('march-run','april-run')],bench()])
    base=row(model='unpriced-obench-fictitious',canonical_model='unpriced-obench-fictitious',cost_usd=None)
    add('ID4','identity',[step('init'),imported([base]),imported([{**base,'study_sha256':'study-B','cost_usd':2}]),
                        imported([{**base,'cost_usd':.75}]),imported([{**base,'cost_usd':9}]),bench()])
    seed_sql="INSERT INTO events (event_id,session_id,agent_type,event_type,source,study_id,study,tokens_in,tokens_out,cost_usd,metadata) VALUES (?,?,'codex','llm_response',?,?,?,?,?,?,?)"
    seeds=[['live-control','live-control','api',None,None,7,3,2,'{}'],
           ['modern-cell','modern-session','benchmark','modern-study','modern-study',100,20,.25,
            json.dumps(dict(task='parser',trial=1,score=1,canonical_model='modern'))],
           [ROW['run_id'],ROW['run_id'],'benchmark',None,None,100,20,.25,json.dumps(dict(run_id=ROW['run_id']))]]
    add('MIG1','migration',[step('sql',sql=LEGACY_SCHEMA),*[step('seed',sql=seed_sql,params=seed) for seed in seeds],
                          step('init'),imported(),imported(),bench(),usage(),usage(include_benchmark=True),
                          step('close'),step('init'),bench(),imported(),usage(include_benchmark=True)])
    new_event=dict(event_id='new-type',session_id='new-type',agent_type='codex',event_type='session_update',
                   status='success',source='api',metadata={})
    add('MIG2','migration',[step('sql',sql=RESTRICTIVE_SCHEMA),step('init'),
        step('query',sql="SELECT study_id,study,tokens_in FROM events WHERE event_id='retained'"),
        step('insert',event=new_event),step('init'),step('query',sql='SELECT COUNT(*) n FROM events')])
    grid=[cell('grid','complete',task,trial,1) for task in ('parser','database') for trial in (1,2)]
    grid += [cell('grid','partial','parser',1,.8),cell('grid','partial','parser',2,None),cell('grid','partial','database',1,.6)]
    add('GRID1','coverage',[step('init'),*grid,step('study',study='grid')])
    add('GRID2','coverage',[step('init'),cell('single','failed','parser',1,0),cell('single','failed','parser',2,None),step('study',study='single')])
    live=dict(event_id='live-control',session_id='live-control',agent_type='codex',event_type='llm_response',
              status='success',source='api',tokens_in=7,tokens_out=3,cost_usd=2,metadata={})
    add('GUARD1','guards',[step('init'),step('insert',event=live),imported([row(),row(run_id='another-cell',trial=2)]),
                         usage(),usage(include_benchmark=True),step('studies')])
    manual=[row(reasoning_effort='high',is_open_model=True)]
    add('GUARD2','guards',[step('init'),imported(manual,options={'study':'manual'}),
                         imported(manual,options={'study':'manual'}),bench()])
    return cases


def subset(value, expected):
    if isinstance(expected,dict):
        return isinstance(value,dict) and all(key in value and subset(value[key],item) for key,item in expected.items())
    if isinstance(expected,list):
        return isinstance(value,list) and len(value)==len(expected) and all(subset(a,b) for a,b in zip(value,expected))
    if isinstance(expected,(int,float)) and not isinstance(expected,bool):
        return type(value) in (int,float) and value==expected
    return type(value) is type(expected) and value==expected


def compare(name, v):
    """Representation-neutral observations; no expected values enter a worker."""
    try:
        if name=='ID1':
            return (all(subset(v[i],{'eventsImported':1}) for i in (1,2))
                    and all(subset(v[i],{'duplicates':1}) for i in (3,4))
                    and subset(v[5],[{'study_id':'study-A'},{'study_id':'study-B'}])
                    and all(json.loads(r['metadata'])['run_id']==ROW['run_id'] for r in v[5]) and len(v[6])==2)
        if name=='ID2':
            return (subset(v[1],{'eventsImported':1}) and all(subset(v[i],{'duplicates':1}) for i in (2,3))
                    and len(v[4])==1 and isinstance(v[4][0]['study_id'],str) and bool(v[4][0]['study_id']))
        if name=='ID3':
            return (all(subset(v[i],{'eventsImported':1}) for i in (1,2)) and len(v[3])==2
                    and all(isinstance(r['study_id'],str) and r['study_id'] for r in v[3])
                    and len({r['study_id'] for r in v[3]})==2)
        if name=='ID4':
            return (subset(v[3],{'costsBackfilled':1}) and subset(v[4],{'costsBackfilled':0})
                    and subset(v[5],[{'study_id':'study-A','cost_usd':.75},{'study_id':'study-B','cost_usd':2}]))
        if name=='MIG1':
            return (subset(v[5],{'eventsImported':1}) and subset(v[6],{'duplicates':1})
                    and len(v[7])==2 and sum(r['cost_usd'] for r in v[7])==.5
                    and subset(v[8],{'total_cost_usd':2}) and subset(v[9],{'total_cost_usd':2.5})
                    and len(v[12])==2 and subset(v[13],{'duplicates':1}) and subset(v[14],{'total_cost_usd':2.5}))
        if name=='MIG2':
            return subset(v[2],[{'study_id':'existing-study','study':'existing-slug','tokens_in':99}]) and subset(v[5],[{'n':2}])
        if name=='GRID1':
            arms={a['canonical_model']:a for a in v[-1]['arms']}
            return (subset(arms['partial'],{'n':2,'mean_score':.7,'excluded_trials':2})
                    and subset(arms['complete'],{'n':4,'mean_score':1,'excluded_trials':0}))
        if name=='GRID2':
            return subset(v[-1]['arms'],[{'n':1,'mean_score':0,'excluded_trials':1}])
        if name=='GUARD1':
            return (subset(v[3],{'total_usage_events':1,'total_cost_usd':2})
                    and subset(v[4],{'total_usage_events':3,'total_cost_usd':2.5}) and len(v[5])==1)
        if name=='GUARD2':
            return (subset(v[1],{'eventsImported':1}) and subset(v[2],{'duplicates':1})
                    and subset(v[3],[{'study_id':'manual','study':'manual'}])
                    and subset(json.loads(v[3][0]['metadata']),{'canonical_model':'gold-model','reasoning_effort':'high','is_open_model':True}))
    except (KeyError,TypeError,ValueError,IndexError):
        return False
    return False


def grade(results):
    fixtures=cases()
    buckets={name:True for name in ('identity','migration','coverage','guards')}
    checks=[]
    if len(results)!=len(fixtures):
        raise ValueError('observation count differs from oracle cases')
    for (name,bucket,_),result in zip(fixtures,results):
        passed=result.get('ok') is True and compare(name,result.get('value'))
        buckets[bucket] &= passed
        checks.append({'case':name,'bucket':bucket,'pass':passed})
    score=round(sum(buckets[k] for k in ('identity','migration','coverage'))/3,4) if buckets['guards'] else 0.0
    return {'score':score,'buckets':buckets,'checks':checks,'oracle_version':2}
