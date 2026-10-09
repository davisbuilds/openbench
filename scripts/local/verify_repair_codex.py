#!/usr/bin/env python3
"""Offline pinned Linux Codex tool-loop proof with an explicitly fake provider.

Use the repository's pinned optional Harbor interpreter. This diagnostic injects
only the provider transport into a broker in the real RepairSandbox. It uses no
real credentials and makes no external model requests. Production broker startup
and shutdown are covered separately by verify_repair_sandbox.py.
"""
from __future__ import annotations

import argparse
import ast
import base64
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import types
import tomllib
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from obench.harbor_agents.sandbox_codex import CLI_VERSION, SandboxCodex
from obench.codex_models import REPAIR_MODEL_PAIRS
from obench.harbor_sandbox import RepairSandbox, docker_bytes, validate_image

MARKER = "OPENBENCH_OFFLINE_CODEX_TOOL_PROBE"
FINAL = "Offline tool transport verified."
FAKE = r'''
import base64,http.server,http.client,json,os,re,signal,threading,sys
from pathlib import Path
from obench.sandbox_gateway import BrokerServer,GatewayConfig,AuthCredentials,UpstreamStream
import obench.sandbox_gateway as gateway
original_validate=gateway.validate_body
def observe_validate(raw,config):
 with Path('/run/private/ingress.jsonl').open('ab') as output: output.write(raw+b'\n')
 return original_validate(raw,config)
gateway.validate_body=observe_validate
class Provider(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  body=self.rfile.read(int(self.headers['Content-Length']))
  with Path('/run/private/requests.jsonl').open('ab') as output: output.write(body+b'\n')
  request=json.loads(body)
  outputs=[item for item in request['input'] if item.get('type') in ('custom_tool_call_output','function_call_output')]
  has_output=bool(outputs)
  image_received=any(isinstance(i,dict) and i.get('type')=='input_image' for block in request['input'] for i in (block.get('content',[]) if isinstance(block.get('content'),list) else []) ) or any(isinstance(i,dict) and i.get('type')=='input_image' for block in outputs for i in (block.get('output',[]) if isinstance(block.get('output'),list) else []))
  pending=re.search(r'Script running with cell ID ([^\s]+)', str(outputs[-1].get('output',''))) if outputs else None
  if pending:
   serial=str(len(outputs))
   item={'id':'wait_'+serial,'type':'function_call','call_id':'call_wait_'+serial,'name':'wait','arguments':json.dumps({'cell_id':pending[1],'yield_time_ms':60000}), 'status':'completed'}
  elif has_output and len(sys.argv)>4 and sys.argv[4]=='browser' and not image_received:
   code='for (const path of ["/logs/agent/browser-before.png", "/logs/agent/browser.png"]) { const result = await tools.view_image({path}); image(result.image_url); }'
   item={'id':'image_offline','type':'custom_tool_call','call_id':'call_image_offline','name':'exec','input':code,'status':'completed'}
  elif has_output:
   item={'id':'msg_offline','type':'message','role':'assistant','content':[{'type':'output_text','text':'Offline tool transport verified.'}]}
  else:
   namespaces=[tool for block in request['input'] if block.get('type')=='additional_tools' for tool in block.get('tools',[])]
   assert any(tool.get('name')=='functions' and any(child.get('name')=='exec' for child in tool.get('tools',[])) for tool in namespaces)
   command=base64.b64decode(sys.argv[3]).decode()
   # A yielded shell session is still running. Drain it inside the real tool
   # invocation before the fake provider emits its final response; otherwise
   # a slower CI host seals the sandbox in the middle of the checks.
   code='// @exec: {"yield_time_ms": 60000}\nlet r = await tools.exec_command('+json.dumps({'cmd':command,'yield_time_ms':1000})+');\n'
   code+='while (r.session_id) { r = await tools.write_stdin({session_id:r.session_id,chars:"",yield_time_ms:1000}); }\ntext(r);'
   # The real pinned provider returns the short tool name without namespace.
   item={'id':'tool_offline','type':'custom_tool_call','call_id':'call_offline','name':'exec','input':code,'status':'completed'}
  response={'id':'resp_offline_'+('final' if has_output else 'tool'),'object':'response','status':'completed','output':[item],'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}
  events=[('response.created',{'type':'response.created','response':{**response,'status':'in_progress','output':[]}}),('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':item}),('response.completed',{'type':'response.completed','response':response})]
  data=''.join('event: '+event+'\ndata: '+json.dumps(value)+'\n\n' for event,value in events).encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
provider=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
threading.Thread(target=provider.serve_forever,daemon=True).start()
def transport(body,headers,timeout):
 connection=http.client.HTTPConnection(*provider.server_address,timeout=timeout)
 connection.request('POST','/fake-provider',body,headers)
 return UpstreamStream(connection,connection.getresponse())
broker=BrokerServer('/run/openbench-model/gateway.sock',GatewayConfig(sys.argv[1],sys.argv[2],8,8*1024*1024,30),AuthCredentials('offline-fake-token','offline-fake-account'),transport=transport)
Path('/run/private/fake.pid').write_text(str(os.getpid()))
def stop(*_): threading.Thread(target=broker.stop,daemon=True).start()
signal.signal(signal.SIGTERM,stop)
print(json.dumps({'event':'ready','role':'offline-fake-broker'}),flush=True)
try: broker.serve_forever(poll_interval=.1)
finally:
 broker._stop_complete.wait(3)
 broker.server_close()
 print(json.dumps({'event':'stopped','role':'offline-fake-broker','clean':not (broker._receipt_failed or broker._drain_incomplete)}),flush=True)
 Path('/run/private/fake.done').touch()
'''


async def run(args):
    model, effort = REPAIR_MODEL_PAIRS[args.model]
    output = args.output_dir.resolve()
    if not output.is_relative_to((REPO / "results").resolve()):
        raise ValueError("--output-dir must be under the repository's ignored results directory")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("--output-dir must be empty to preserve previous evidence")
    image = validate_image(args.runtime_image)
    receipt = {"schema": 1, "scope": "actual pinned Linux Codex adapter, tool loop, relay, gateway and environment; explicitly injected fake provider transport",
               "live_inference": False, "real_credentials": False, "production_gateway_lifecycle": False,
               "runtime_image": image, "codex_version": CLI_VERSION,
               "model_alias": args.model, "model": model, "effort": effort,
               "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    from obench.repair_validation import inspect_task
    from obench.repair_workflow import workflow_command, identity
    receipt['workflow_sha256'] = identity()
    inspected = inspect_task(args.task)
    receipt.update(task=str(args.task.resolve()), task_binding=inspected['task_binding'])
    app = args.task / 'environment/app'
    browser = inspected['revision']['oracle'] == 'activity-explorer'
    registered = 'web/app.js' if browser else 'src/import/benchmark.ts' if (app / 'src').is_dir() else None
    target = registered or 'scripts/profiles/__init__.py'
    prefix = '//' if registered else '#'
    command = "set -eu\ntest -n \"${BASH_VERSION:-}\"\ntest \"$(printf '%s ' {alpha,beta})\" = 'alpha beta '\npython3 -m obench.repair_devtools check > /logs/agent/developer-workflow.json\n"
    command += "test -z \"$(git status --porcelain)\"\ntest \"$(git rev-list --count HEAD)\" = 1\ntest -z \"$(git remote)\"\n"
    project_check = getattr(args, 'project_check', None)
    if not project_check and (app / 'tests').is_dir():
        project_check = 'pnpm run typecheck && pnpm test' if registered else 'python3 -m pytest -q'
    receipt['project_check'] = project_check
    if project_check:
        command += workflow_command(project_check) + '\n'
    if browser:
        command += 'node scripts/browser-check.cjs\n'
    command += "printf '\\n" + prefix + " " + MARKER + "\\n' >> " + shlex.quote('/app/' + target) + "\n"
    command += "git diff -- " + shlex.quote(target) + " | rg " + shlex.quote(MARKER) + "\n"
    command += "mkdir /tmp/codex-cleanup-check\nprintf disposable > /tmp/codex-cleanup-check/file\nrm -r /tmp/codex-cleanup-check\ntest ! -e /tmp/codex-cleanup-check\n"
    captured = None
    if getattr(args, 'context_archive', None):
        from obench.frozen_context import load_archive
        captured = load_archive(args.context_archive, args.context_sha256, kind='context')
        # Exercise real reads of every staged file through the harness, including
        # resources and executable helpers, not just discovery metadata.
        checks = []
        for name, data in captured.files.items():
            prefix, relative = name.split('/', 1)
            dest = ('/tmp/codex-home/' if prefix == 'codex' else '/home/solver/context/resources/') + relative
            checks.append((dest, hashlib.sha256(data).hexdigest()))
        program = 'import hashlib,pathlib\n'
        program += 'for p,h in ' + repr(checks) + ':\n assert hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h,p\n'
        command += 'python3 -c ' + shlex.quote(program) + '\n'
    encoded_command = base64.b64encode(command.encode()).decode()
    oracle = tomllib.loads((args.task / 'task.toml').read_text()).get('metadata', {}).get('openbench_oracle')
    env = None
    temporary = tempfile.TemporaryDirectory(prefix="obench-codex-offline-")
    try:
        root = Path(temporary.name)
        auth = root / "auth.json"
        auth.write_text("{}")
        auth.chmod(0o600)
        paths = TrialPaths(root / "trial")
        env = RepairSandbox(environment_dir=(args.task / "environment").resolve(),
            environment_name="offline-codex", session_id="offline-codex-" + uuid.uuid4().hex[:12],
            trial_paths=paths, task_env_config=EnvironmentConfig(network_mode="no-network", cpus=1, memory_mb=1024),
            network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), runtime_image=image, oracle_id=oracle,
            **({'context_archive':str(args.context_archive.resolve()),'context_sha256':args.context_sha256} if captured else {}))

        async def fake_start(self, actual_model, actual_effort, *_):
            if (actual_model, actual_effort) != (model, effort):
                raise RuntimeError("adapter passed a different model/effort to the broker")
            # Verify the actual loaded broker bytes before injecting the
            # test-only transport. Never call production start_gateway.
            expected = hashlib.sha256((REPO / "obench/sandbox_gateway.py").read_bytes()).hexdigest()
            observed = (await docker_bytes("exec", self._containers["broker"], "python3", "-c",
                "import hashlib,importlib.util; p=importlib.util.find_spec('obench.sandbox_gateway').origin; print(hashlib.sha256(open(p,'rb').read()).hexdigest())")).decode().strip()
            if expected != observed:
                raise RuntimeError("runtime gateway differs from current source")
            receipt["gateway_module_sha256"] = observed
            # Even the broker loses external network access in this probe.
            await docker_bytes("network", "disconnect", self._network, self._containers["broker"])
            networks = (await self._inspect("broker"))["NetworkSettings"]["Networks"]
            if networks:
                raise RuntimeError("offline fake broker retained an external network")
            receipt["broker_external_networks"] = networks
            await docker_bytes("exec", "-i", self._containers["broker"], "python3", "-c",
                "import sys;open('/run/private/fake.py','wb').write(sys.stdin.buffer.read())", data=FAKE.encode())
            await docker_bytes("exec", "--detach", self._containers["broker"], "sh", "-c",
                "exec python3 /run/private/fake.py " + shlex.quote(model) + " " + shlex.quote(effort) + " " + shlex.quote(encoded_command) + (' browser' if browser else '') + " > /run/private/fake.log 2>&1")
            for _ in range(100):
                ready = await docker_bytes("exec", self._containers["broker"], "python3", "-c",
                    "import os;print(os.path.exists('/run/openbench-model/gateway.sock'))")
                if ready.strip() == b"True":
                    break
                await asyncio.sleep(.05)
            else:
                raise RuntimeError("fake broker failed to start")
            await docker_bytes("exec", "--detach", "--user", "10001:10001", self._containers["main"],
                "sh", "-c", "exec python3 -m obench.sandbox_gateway relay --socket /run/openbench-model/gateway.sock --port 8765 > /logs/agent/relay.log 2>&1")
            for _ in range(100):
                ready = await self.exec("python3 -c " + shlex.quote("import socket; s=socket.socket(); s.settimeout(.1); print(s.connect_ex(('127.0.0.1',8765)))"))
                if ready.return_code == 0 and ready.stdout.strip() == "0":
                    return
                await asyncio.sleep(.05)
            raise RuntimeError("relay failed to start")

        env.start_gateway = types.MethodType(fake_start, env)
        original_seal = env.seal

        async def observed_seal():
            if not env._sealed:
                # Stop and drain the injected broker before its private
                # tmpfs disappears. Production seal remains responsible for
                # stopping every process in both containers.
                await docker_bytes("exec", env._containers["broker"], "python3", "-c",
                    "import os,signal;from pathlib import Path;p=Path('/run/private/fake.pid');os.kill(int(p.read_text()),signal.SIGTERM) if p.exists() else None")
                for _ in range(100):
                    ready = await docker_bytes("exec", env._containers["broker"], "python3", "-c",
                        "from pathlib import Path;print(Path('/run/private/fake.done').exists() or not Path('/run/private/fake.pid').exists())")
                    if ready.strip() == b"True":
                        break
                    await asyncio.sleep(.05)
                for source, target in (("requests.jsonl", "requests.jsonl"), ("ingress.jsonl", "ingress.jsonl"), ("fake.log", "gateway.jsonl")):
                    data = await docker_bytes("exec", env._containers["broker"], "python3", "-c",
                        "import sys;from pathlib import Path;p=Path('/run/private')/sys.argv[1];sys.stdout.buffer.write(p.read_bytes() if p.exists() else b'')", source)
                    (output / target).write_bytes(data)
            # Export invokes seal itself. Keep this hook limited to shutdown;
            # the caller exports logs after the agent finishes below.
            return await original_seal()

        env.seal = observed_seal
        agent = SandboxCodex(logs_dir=paths.agent_dir, model_name=model, version=CLI_VERSION,
            reasoning_effort=effort, extra_env={"CODEX_AUTH_JSON_PATH": str(auth),
                "OPENBENCH_CODEX_AUTH_RETURN_PATH": str(root / "return.json")})
        await env.start()
        await agent.setup(env)
        await asyncio.wait_for(agent.run("Run the requested developer workflow checks, add the harmless probe comment, then confirm completion.", env, AgentContext()), 240)
        await env.download_dir("/logs/agent", output / "agent")
        receipt["frozen_source"] = await env.freeze_source(output / "source")
        text = (output / "agent/codex.txt").read_text()
        source = (output / "source" / target).read_text()
        requests = [json.loads(line) for line in (output / "requests.jsonl").read_text().splitlines()]
        if browser:
            images=[]
            for request in requests:
                for block in request['input']:
                    for key in ('content','output'):
                        for item in block.get(key,[]) if isinstance(block.get(key),list) else []:
                            if isinstance(item,dict) and item.get('type')=='input_image': images.append(item)
            if not images or not (output/'agent/browser.png').is_file():
                raise RuntimeError('actual Codex screenshot observation did not reach the provider')
            transported={hashlib.sha256(base64.b64decode(i['image_url'].split(',',1)[1],validate=True)).hexdigest() for i in images}
            screenshots={name:hashlib.sha256((output/'agent'/name).read_bytes()).hexdigest()
                         for name in ('browser-before.png','browser.png')}
            if len(set(screenshots.values()))!=2 or not set(screenshots.values())<=transported:
                raise RuntimeError('distinct before/after screenshots did not reach the provider unchanged')
            receipt['browser_image_received']=True
            receipt['browser_screenshot_sha256']=screenshots['browser.png']
        if FINAL not in text or MARKER not in source:
            raise RuntimeError("actual Codex tool execution or final response was not observed")
        if not registered:
            ast.parse(source)
        developer = json.loads((output / 'agent/developer-workflow.json').read_text())
        required = {'git-baseline-diff', 'search', 'python-tests', 'typescript-check-run', 'typescript-lint', 'native-build', 'json', 'processes', 'cleanup'}
        if set(developer['workflows']) != required:
            raise RuntimeError('developer workflow checks incomplete')
        # Completion of the single command is independently observed in Codex's
        # emitted command event, not inferred from the fake provider's final.
        events = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get('item'), dict):
                item = event['item']
                if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
                    events.append(item)
        if (len(events) != 1 or events[0].get('exit_code') != 0
                or events[0].get('status') != 'completed'
                or MARKER not in events[0].get('aggregated_output', '')):
            raise RuntimeError('developer workflow command did not finish successfully through Codex')
        receipt['developer_workflows_passed'] = True
        receipt['developer_environment'] = developer
        if f"Model metadata for `{model}` not found" in text:
            raise RuntimeError("pinned CLI lacks the selected model metadata")
        if not 2 <= len(requests) <= 8 or not any(item.get("type") == "custom_tool_call_output" for item in requests[-1]["input"]):
            raise RuntimeError("expected the actual completed tool loop")
        sessions = list((output / 'agent/sessions').rglob('*.jsonl'))
        if len(sessions) != 1:
            raise RuntimeError('expected one raw Codex session')
        metas = [event['payload'] for line in sessions[0].read_text().splitlines()
                 if (event := json.loads(line)).get('type') == 'session_meta']
        instructions = metas[0].get('base_instructions', {}).get('text') if len(metas) == 1 else None
        observed = '\n'.join(part['text'] for item in requests[0]['input']
                             if item.get('type') == 'message' and item.get('role') in ('user', 'developer', 'system')
                             for part in item.get('content', []) if isinstance(part.get('text'), str))
        if not isinstance(instructions, str) or not instructions.strip() or instructions.strip() not in observed:
            raise RuntimeError('native session base instructions differ from the actual provider request')
        receipt['base_instructions_sha256'] = hashlib.sha256(instructions.encode()).hexdigest()
        if captured:
            from obench.frozen_context import verify_instruction_context, verify_session_context
            # Request capture, not an agent's assertion, proves instruction load.
            context_evidence = verify_instruction_context(requests[0]['input'], captured)
            project = app / 'AGENTS.md'
            if project.is_file() and project.read_text().strip() not in observed:
                raise RuntimeError('project guidance was not loaded by Codex')
            receipt['context'] = {**context_evidence,'file_reads_verified':len(checks),
                                  'project_loaded':project.is_file()}
            verify_session_context(output / 'agent/sessions', captured)
        if any(request.get("model") != model or request.get("reasoning", {}).get("effort") != effort for request in requests):
            raise RuntimeError("provider request changed the selected model/effort")
        ledger = [json.loads(line) for line in (output / "gateway.jsonl").read_text().splitlines()]
        if ledger[-1] != {"event": "stopped", "role": "offline-fake-broker", "clean": True}:
            raise RuntimeError("injected broker did not drain cleanly")
        receipt.update(status="passed", actual_tool_mutation=True, tool_result_returned=True, final_response_present=True,
                       request_count=len(requests), gateway_ledger_sha256=hashlib.sha256((output / "gateway.jsonl").read_bytes()).hexdigest())
        receipt['evidence_sha256'] = {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                                     for name in ('agent/codex.txt', 'agent/developer-workflow.json',
                                                  'requests.jsonl', 'gateway.jsonl')}
        if browser:
            receipt['evidence_sha256'].update({'agent/'+name:digest for name,digest in screenshots.items()})
    except BaseException as error:
        receipt.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        try:
            if env is not None:
                await env.stop()
            receipt["cleanup_confirmed"] = True
        except BaseException as error:
            receipt.update(status="failed", cleanup_error_type=type(error).__name__)
            raise
        finally:
            temporary.cleanup()
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "passed", "receipt": str(output / "receipt.json"), "live_inference": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--task", type=Path, default=REPO / "benchmarks/harbor/local/dojo-evidence-pr60-v3")
    parser.add_argument("--model", choices=sorted(REPAIR_MODEL_PAIRS), default="gpt-5.6-terra-xhigh")
    parser.add_argument('--context-archive', type=Path)
    parser.add_argument('--context-sha256')
    parser.add_argument('--project-check', help='Explicit command for a passing pre-fix public-test subset')
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
