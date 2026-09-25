"""Real filesystem, process-lock and tmux controls for campaign supervision."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from obench import campaign, init, suite_run


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='obench campaign ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'run'
        self.directory.mkdir()
        (self.directory / 'execution.lock').touch()
        self.session = 'obench-test-' + uuid.uuid4().hex[:12]
        self.launch = {'schema': 1, 'host': socket.gethostname(), 'session': self.session,
                       'source': {'root': str(self.root), 'commit': 'test'},
                       'manifest_sha256': 'a'*64, 'jobs': [str(self.root/'jobs')]}
        campaign.write_record(self.directory/'launch.json', self.launch)

    def test_atomic_private_receipt(self):
        self.assertEqual(campaign.read_record(self.directory/'launch.json'), self.launch)
        self.assertEqual((self.directory/'launch.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.directory/'launch.tmp').exists())

    def test_missing_completion_is_unknown_not_success(self):
        with patch.object(campaign, 'session_exists', return_value=False):
            status = campaign.campaign_status(self.directory)
        self.assertEqual(status['state'], 'interrupted_or_not_started')
        self.assertIsNone(status['completion'])

    def test_status_uses_a_real_cross_process_lock(self):
        code = "import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
        proc = subprocess.Popen([sys.executable,'-c',code,str(self.directory/'execution.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            with patch.object(campaign,'session_exists',return_value=False):
                self.assertEqual(campaign.campaign_status(self.directory)['state'],'running')
            with self.assertRaisesRegex(campaign.CampaignError,'active'):
                with campaign.execution_lock(self.directory):
                    self.fail('second lock holder entered')
        finally:
            proc.communicate('stop',timeout=10)
        with campaign.execution_lock(self.directory):
            pass

    def test_status_preserves_timeout_and_incomplete_evidence(self):
        trial = self.root/'jobs/trial'
        (trial/'verifier').mkdir(parents=True)
        (trial/'result.json').write_text(json.dumps({'exception_info':{'exception_type':'AgentTimeoutError'},'verifier_result':{'rewards':{'reward':1}}}))
        (trial/'verifier/sandbox-gateway.jsonl').write_text('{"event":"request","outcome":"revoked"}\nbroken\n')
        with patch.object(campaign,'session_exists',return_value=False):
            status=campaign.campaign_status(self.directory)
        self.assertEqual(status['trial_results'][0]['exception_type'],'AgentTimeoutError')
        self.assertEqual(status['trial_results'][0]['reward'],{'reward':1})
        self.assertEqual(status['transport_outcomes'],{'revoked':1})
        self.assertEqual(len(status['unreadable_evidence']),1)
        self.assertNotEqual(status['state'],'completed')

    def test_remote_host_receipt_is_not_a_local_process_claim(self):
        campaign.write_record(self.directory/'launch.json',{**self.launch,'host':'other-host'})
        with self.assertRaisesRegex(campaign.CampaignError,'execution host'):
            campaign.campaign_status(self.directory)

    def test_tmux_environment_does_not_inherit_api_keys(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-secret','ANTHROPIC_API_KEY':'test-secret','DOCKER_HOST':'unix:///test.sock'}):
            env=campaign._environment()
        self.assertNotIn('OPENAI_API_KEY',env)
        self.assertNotIn('ANTHROPIC_API_KEY',env)
        self.assertEqual(env['DOCKER_HOST'],'unix:///test.sock')

    @unittest.skipUnless(shutil.which('tmux'),'tmux not installed')
    def test_real_tmux_launch_with_spaces_and_duplicate_refusal(self):
        # Stand-in work proves the supervisor launch/lock/environment seam;
        # real Harbor execution is covered by the integration control.
        code = "from pathlib import Path; import os,sys,time; from obench.campaign import execution_lock; d=Path(sys.argv[1]);\nwith execution_lock(d,blocking=True):\n (d/'alive').write_text(str('OPENAI_API_KEY' in os.environ)); time.sleep(30)"
        command=[sys.executable,'-c',code,str(self.directory)]
        env={'PATH':os.environ['PATH'],'HOME':os.environ['HOME'],'PYTHONPATH':str(campaign.ROOT)}
        try:
            campaign.spawn_supervisor(self.directory,self.launch,command,env)
            for _ in range(100):
                if (self.directory/'alive').exists(): break
                time.sleep(.05)
            self.assertEqual((self.directory/'alive').read_text(),'False')
            self.assertTrue(campaign.session_exists(self.session))
            with self.assertRaisesRegex(campaign.CampaignError,'active|already exists'):
                campaign.spawn_supervisor(self.directory,self.launch,command,env)
        finally:
            subprocess.run(['tmux','kill-session','-t','='+self.session],capture_output=True)

    def test_launch_intent_refuses_reuse_before_spawning(self):
        init.init_scaffold(self.root)
        suite=self.root/'.openbench/suites/default.toml'
        compiled=suite_run.compile_suite(suite)
        directory=Path(compiled.config.results_dir)/'campaigns'/compiled.manifest_sha256
        directory.mkdir(parents=True)
        with patch.object(campaign,'git_identity',return_value=self.launch['source']), patch.object(campaign.shutil,'which',return_value=sys.executable):
            with self.assertRaisesRegex(campaign.CampaignError,'already has launch intent'):
                campaign.launch_campaign(str(suite))

    def test_worker_rejects_drift_before_executor(self):
        # Execute in a child because it intentionally redirects process stdout.
        path=self.directory/'launch.json'
        value={**self.launch,'suite':str(self.root/'missing-suite.toml'),'harbor_binary':'not-run'}
        campaign.write_record(path,value)
        env={**os.environ,'PYTHONPATH':str(campaign.ROOT)}
        proc=subprocess.run([sys.executable,'-m','obench.campaign','_execute',str(self.directory)],env=env,capture_output=True,timeout=15)
        self.assertNotEqual(proc.returncode,0)
        finished=campaign.read_record(self.directory/'finished.json')
        self.assertEqual(finished['state'],'failed')
        self.assertEqual(finished['error_type'],'CampaignError')
        self.assertIn('source',(self.directory/'campaign.log').read_text())
