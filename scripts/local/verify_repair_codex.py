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
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import types
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from obench.harbor_agents.sandbox_codex import CLI_VERSION, SandboxCodex
from obench.harbor_sandbox import RepairSandbox, docker_bytes, validate_image

MARKER = "OPENBENCH_OFFLINE_CODEX_TOOL_PROBE"
FINAL = "Offline tool transport verified."
FAKE = r'''
import http.server,http.client,json,os,signal,threading
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
  has_output=any(item.get('type') in ('custom_tool_call_output','function_call_output') for item in request['input'])
  if has_output:
   item={'id':'msg_offline','type':'message','role':'assistant','content':[{'type':'output_text','text':'Offline tool transport verified.'}]}
  else:
   namespaces=[tool for block in request['input'] if block.get('type')=='additional_tools' for tool in block.get('tools',[])]
   assert any(tool.get('name')=='functions' and any(child.get('name')=='exec' for child in tool.get('tools',[])) for tool in namespaces)
   command="printf '\\n# OPENBENCH_OFFLINE_CODEX_TOOL_PROBE\\n' >> /app/scripts/profiles/__init__.py"
   code='text(await tools.exec_command('+json.dumps({'cmd':command})+'));'
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
broker=BrokerServer('/run/openbench-model/gateway.sock',GatewayConfig('gpt-5.6-terra','xhigh',3,8*1024*1024,30),AuthCredentials('offline-fake-token','offline-fake-account'),transport=transport)
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
               "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
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
            network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), runtime_image=image)

        async def fake_start(self, *_):
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
                "exec python3 /run/private/fake.py > /run/private/fake.log 2>&1")
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
            boundary = await original_seal()
            await env.download_dir("/logs/agent", output / "agent")
            return boundary

        env.seal = observed_seal
        agent = SandboxCodex(logs_dir=paths.agent_dir, model_name="gpt-5.6-terra", version=CLI_VERSION,
            reasoning_effort="xhigh", extra_env={"CODEX_AUTH_JSON_PATH": str(auth),
                "OPENBENCH_CODEX_AUTH_RETURN_PATH": str(root / "return.json")})
        await env.start()
        await agent.setup(env)
        await asyncio.wait_for(agent.run("Add the requested harmless probe comment to scripts/profiles/__init__.py, then confirm completion.", env, AgentContext()), 90)
        await env.download_dir("/logs/agent", output / "agent")
        receipt["frozen_source"] = await env.freeze_source(output / "source")
        text = (output / "agent/codex.txt").read_text()
        source = (output / "source/scripts/profiles/__init__.py").read_text()
        requests = [json.loads(line) for line in (output / "requests.jsonl").read_text().splitlines()]
        if FINAL not in text or MARKER not in source:
            raise RuntimeError("actual Codex tool execution or final response was not observed")
        ast.parse(source)
        if len(requests) != 2 or not any(item.get("type") == "custom_tool_call_output" for item in requests[-1]["input"]):
            raise RuntimeError("expected the actual two-request tool loop")
        ledger = [json.loads(line) for line in (output / "gateway.jsonl").read_text().splitlines()]
        if ledger[-1] != {"event": "stopped", "role": "offline-fake-broker", "clean": True}:
            raise RuntimeError("injected broker did not drain cleanly")
        receipt.update(status="passed", actual_tool_mutation=True, tool_result_returned=True, final_response_present=True,
                       request_count=len(requests), gateway_ledger_sha256=hashlib.sha256((output / "gateway.jsonl").read_bytes()).hexdigest())
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
    parser.add_argument("--task", type=Path, default=REPO / "harbor-tasks-local/dojo-evidence-pr60-v3")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
