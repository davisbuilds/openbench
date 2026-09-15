"""Offline end-to-end candidate -> supervised adapter -> checker -> evidence path."""
import json
import os
from pathlib import Path
import sys
import shlex
import tempfile
import unittest
from unittest.mock import patch

from obench import candidates, run


class CapturedTrialPathTests(unittest.TestCase):
    def test_both_harnesses_receive_frozen_treatment_and_retain_actual_tool_events(self):
        for harness, model in [('codex','gpt-6-astra-max'), ('claude','claude-opus-4-8')]:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
                root=Path(td)
                capture=root/'capture'; capture.mkdir()
                (capture/'settings').write_text('' if harness=='codex' else '{}')
                (capture/'skill').write_text('captured sentinel')
                task=root/'tasks/task'; (task/'workspace').mkdir(parents=True)
                (task/'instruction.md').write_text('Read the staged skill and create answer.json.')
                checker=task/'checker.sh'
                checker.write_text('#!/bin/sh\n' + shlex.quote(sys.executable) + " - <<'CHECK'\n" + '''import json
from pathlib import Path
answer=json.loads(Path('answer.json').read_text())
assert answer['skill']=='captured sentinel'
assert 'DAILY_SECRET' not in answer['env']
assert answer['env']['CAPTURE_CONTROL']=='declared'
print('SCORE: 0.5')
raise SystemExit(1)
CHECK
''')
                checker.chmod(0o755)
                binary=root/'bin';binary.mkdir()
                exe=binary/harness
                exe.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('offline-fixture 1.0'); raise SystemExit(0)
harness=Path(sys.argv[0]).name
home=Path.home()
skill=next(home.glob('.agents/skills/*/SKILL.md')).read_text()
Path('answer.json').write_text(json.dumps({'skill':skill,'env':dict(os.environ)}))
if harness=='codex':
    events=[{'type':'item.completed','item':{'type':'command_execution','id':'read','command':'read captured SKILL.md','exit_code':0,'aggregated_output':skill}},
            {'type':'item.completed','item':{'type':'agent_message','text':'finished','phase':'final_answer'}},
            {'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}]
else:
    events=[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'read','name':'Read','input':{'file_path':'SKILL.md'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'read','content':skill}]}},
            {'type':'result','is_error':False,'result':'finished','num_turns':1,'usage':{'input_tokens':2,'output_tokens':1}}]
for event in events: print(json.dumps(event))
''')
                exe.chmod(0o755)
                key='CODEX_HOME' if harness=='codex' else 'CLAUDE_CONFIG_DIR'
                config_name='config.toml' if harness=='codex' else 'settings.json'
                spec=root/'candidate.toml'
                spec.write_text(f'''kind="config-variant"
name="captured-{harness}"
base_adapter="{harness}"
captured_context=true
config_dir="capture"
config_files=[{{source="settings",destination="config/{config_name}"}},
              {{source="skill",destination="home/.agents/skills/probe/SKILL.md"}}]
pass_env=["CLAUDE_CODE_OAUTH_TOKEN"]
[env]
{key}="{{config_dir}}/config"
CAPTURE_CONTROL="declared"
''' + ('OPENBENCH_CLAUDE_AUTH_MODE="subscription"\n' if harness=='claude' else ''))
                candidate=candidates.load_candidate(spec,run.DEFAULT_ADAPTERS_DIR)
                (capture/'skill').write_text('later source drift')
                with patch.dict(os.environ, {'PATH':str(binary)+os.pathsep+os.environ['PATH'],
                                'DAILY_SECRET':'dummy', 'CLAUDE_CODE_OAUTH_TOKEN':'fixture-lane-token'}):
                    before=dict(os.environ)
                    row=run.run_cell(candidate.name,'task',model,1,10,str(task.parent),
                                     run.DEFAULT_ADAPTERS_DIR,10,candidate=candidate,
                                     transcripts_dir=str(root/'transcripts'),results_stem='offline')
                    self.assertEqual(dict(os.environ),before)
                self.assertEqual(row['checker_exit'],1,row.get('checker_stderr'))
                self.assertEqual(row['score'],0.5,(row.get('checker_stdout'),row.get('checker_stderr'),row.get('error')))
                self.assertFalse(row['success'])
                self.assertEqual(row['evidence_status'],'complete',row.get('error'))
                bundles=list((root/'transcripts/offline').glob('*.evidence.json'))
                self.assertEqual(len(bundles),1)
                bundle=json.loads(bundles[0].read_text())
                self.assertEqual(bundle['run_id'],row['run_id'])
                self.assertEqual(bundle['final_message'],'finished')
                self.assertTrue(bundle['tool_events'])
                self.assertNotIn('later source drift',bundle['full_output'])


if __name__=='__main__':
    unittest.main()
