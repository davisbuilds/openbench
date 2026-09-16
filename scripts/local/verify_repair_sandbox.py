#!/usr/bin/env python3
"""Offline effective-runtime probe for the Harbor repair environment.

Requires the pinned optional Harbor interpreter and a prebuilt runtime image.
Only invalid model requests are sent; dummy credentials cannot contact a model.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from obench.harbor_sandbox import RepairSandbox, SandboxArtifactError, docker_bytes
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths


SERVER = r'''
import json, socket, threading, time
def serve(family, kind, port):
    s=socket.socket(family,kind)
    if family == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
    s.bind(('::' if family == socket.AF_INET6 else '0.0.0.0',port))
    if kind == socket.SOCK_STREAM: s.listen()
    while True:
        if kind == socket.SOCK_STREAM:
            c,_=s.accept(); data=c.recv(1024); c.sendall(data); c.close()
        else:
            data,peer=s.recvfrom(1024); s.sendto(data,peer)
        print(json.dumps({'family':family,'kind':kind,'port':port,'challenge':data.decode()}),flush=True)
for family in (socket.AF_INET,socket.AF_INET6):
    for kind,port in ((socket.SOCK_STREAM,443),(socket.SOCK_STREAM,22),(socket.SOCK_DGRAM,53)):
        threading.Thread(target=serve,args=(family,kind,port),daemon=True).start()
print('READY',flush=True)
while True: time.sleep(1)
'''

CLIENT = r'''
import json,socket,sys
addresses=json.loads(sys.argv[1]); challenge=sys.argv[2].encode(); allowed=sys.argv[3]=='allowed'
results=[]
for family,address in ((socket.AF_INET,addresses['v4']),(socket.AF_INET6,addresses['v6'])):
 for kind,port in ((socket.SOCK_STREAM,443),(socket.SOCK_STREAM,22),(socket.SOCK_DGRAM,53)):
  s=socket.socket(family,kind); s.settimeout(.5)
  try:
   s.connect((address,port)); s.send(challenge); returned=s.recv(1024)
   reachable=returned==challenge; outcome='echo' if reachable else 'wrong-response'
  except OSError as error: reachable=False; outcome=type(error).__name__+':'+str(error.errno)
  finally: s.close()
  assert reachable==allowed,(family,kind,port,outcome)
  results.append({'family':family,'kind':kind,'port':port,'outcome':outcome})
print(json.dumps(results))
'''


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--task", type=Path, default=Path("harbor-tasks-local/dojo-evidence-pr60-v3"))
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    token = uuid.uuid4().hex[:12]
    network, server = "obench-probe-" + token, "obench-canary-" + token
    receipt = {"schema": 1, "scope": "Harbor environment, offline gateway rejection, and source lifecycle; no model inference",
               "runtime_image": args.runtime_image,
               "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    env = None
    with tempfile.TemporaryDirectory(prefix="repair-boundary-") as temporary:
        root = Path(temporary)
        auth = root / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "offline-not-a-credential", "account_id": "offline"}}))
        auth.chmod(0o600)
        canary = root / "host-only.txt"
        canary.write_text("host-canary-" + token)
        before = canary.read_bytes()
        try:
            await docker_bytes("network", "create", "--internal", "--ipv6", "--subnet",
                               "fd42:" + token[:4] + ":" + token[4:8] + "::/64", network)
            await docker_bytes("run", "--detach", "--name", server, "--network", network,
                "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE", "--read-only",
                "--entrypoint", "python3", args.runtime_image, "-u", "-c", SERVER)
            info = json.loads(await docker_bytes("inspect", server))[0]
            addresses = {"v4": info["NetworkSettings"]["Networks"][network]["IPAddress"],
                         "v6": info["NetworkSettings"]["Networks"][network]["GlobalIPv6Address"]}
            if not all(addresses.values()):
                raise RuntimeError("controlled dual-stack canary did not receive both addresses")
            # The server is observed externally before any refusal is interpreted.
            for _ in range(100):
                if b"READY" in await docker_bytes("logs", server):
                    break
                await asyncio.sleep(.1)
            positive = await docker_bytes("run", "--rm", "--network", network, "--cap-drop", "ALL",
                "--user", "10001:10001", "--entrypoint", "python3", args.runtime_image, "-c",
                CLIENT, json.dumps(addresses), token, "allowed")
            receipt["network_positive"] = json.loads(positive)
            env = RepairSandbox(environment_dir=(args.task / "environment").resolve(),
                environment_name="repair-boundary", session_id="repair-boundary-" + token,
                trial_paths=TrialPaths(root / "trial"),
                task_env_config=EnvironmentConfig(network_mode="no-network", cpus=1, memory_mb=512),
                network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), runtime_image=args.runtime_image)
            await env.start()
            receipt["solver_inspect"] = await env._inspect("main")
            with env.scoped_exec_env({"CODEX_AUTH_JSON_PATH": "/host/private/not-for-solver",
                                      "SSH_AUTH_SOCK": "/host/agent-socket"}):
                leaked = await env.exec("python3 -c " + shlex.quote(
                    "import os; assert 'CODEX_AUTH_JSON_PATH' not in os.environ; "
                    "assert 'SSH_AUTH_SOCK' not in os.environ; print('scoped-host-env-excluded')"))
            if leaked.return_code or leaked.stdout.strip() != "scoped-host-env-excluded":
                raise RuntimeError("Harbor host environment overlay leaked")
            receipt["host_scoped_environment_excluded"] = True
            denied = await env.exec("python3 -c " + shlex.quote(CLIENT) + " " +
                shlex.quote(json.dumps(addresses)) + " " + shlex.quote("denied-" + token) + " denied")
            if denied.return_code:
                raise RuntimeError("solver network denial probe failed")
            receipt["network_denied"] = json.loads(denied.stdout)
            fs_code = r'''
import json,os,pathlib,socket,subprocess,sys
root=pathlib.Path('/app'); f=root/'allowed.txt'; f.write_text('allowed')
assert subprocess.check_output([sys.executable,'-c','from pathlib import Path; print(Path("/app/allowed.txt").read_text())'],text=True).strip()=='allowed'
assert os.getuid()==10001
status=pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status and 'CapBnd:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
paths=[sys.argv[1],'/var/run/docker.sock','/run/host-services/ssh-auth.sock','/root/.ssh/id_ed25519','/tests','/solution']
for p in paths:
 try: exists=pathlib.Path(p).exists()
 except PermissionError: exists=False
 assert not exists,p
link=root/'canary-link'; link.symlink_to(sys.argv[1])
try: link.read_bytes(); raise AssertionError('host symlink readable')
except FileNotFoundError: pass
link.unlink()
assert subprocess.run([sys.executable,'-c','from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()',sys.argv[1]],capture_output=True).returncode != 0
try: socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP); raise AssertionError('raw socket created')
except PermissionError: pass
try: pathlib.Path('/run/openbench-model/replace').write_text('bad'); raise AssertionError('socket volume writable')
except OSError: pass
print(json.dumps({'source_read_write':True,'host_paths_denied':True,'symlink_subprocess_denied':True,'raw_socket_denied':True,'socket_volume_readonly':True}))
'''
            fs = await env.exec("python3 -c " + shlex.quote(fs_code) + " " + shlex.quote(str(canary)))
            if fs.return_code:
                raise RuntimeError("filesystem/capability probe failed: " + (fs.stdout or "") + (fs.stderr or ""))
            receipt["filesystem"] = json.loads(fs.stdout)
            await env.start_gateway("gpt-5.6-terra", "xhigh", auth)
            invalid = await env.exec("python3 -c " + shlex.quote(
                "import http.client,json; c=http.client.HTTPConnection('127.0.0.1',8765); "
                "c.request('GET','/forbidden-answer-retrieval'); r=c.getresponse(); "
                "assert 400<=r.status<500,r.status; print(json.dumps({'status':r.status})); r.read(); c.close()"))
            if invalid.return_code:
                raise RuntimeError("gateway invalid-route rejection failed")
            receipt["gateway_invalid_route"] = json.loads(invalid.stdout)
            watcher = (
                "from pathlib import Path; import sys,time; "
                "Path('/app/'+sys.argv[1]+'.ready').touch(); "
                "\nwhile not Path('/app/'+sys.argv[1]).exists(): time.sleep(.01)\n"
                "with Path('/app/scripts/profiles/__init__.py').open('a') as f: f.write('\\n# '+sys.argv[2]+'\\n')"
            )
            for trigger, marker in (("positive-trigger", "POSITIVE-WATCHER"), ("after-stop-trigger", "FORBIDDEN-WATCHER")):
                await docker_bytes("exec", "--detach", "--user", "10001:10001", env._containers["main"],
                                   "python3", "-c", watcher, trigger, marker)
                for _ in range(100):
                    ready = await env.exec("test -f /app/" + trigger + ".ready")
                    if ready.return_code == 0:
                        break
                    await asyncio.sleep(.02)
                else:
                    raise RuntimeError("watcher failed to initialize")
                if trigger == "positive-trigger":
                    await env.exec("touch /app/positive-trigger")
                    for _ in range(100):
                        observed = await env.exec("grep -q POSITIVE-WATCHER /app/scripts/profiles/__init__.py")
                        if observed.return_code == 0:
                            break
                        await asyncio.sleep(.02)
                    else:
                        raise RuntimeError("watcher positive control did not fire")
            await env.seal()
            await docker_bytes("run", "--rm", "--network", "none", "--user", "10001:10001", "--cap-drop", "ALL",
                "--mount", "type=volume,source=" + env._volumes["source"] + ",target=/app",
                "--entrypoint", "python3", args.runtime_image, "-c",
                "from pathlib import Path; Path('/app/after-stop-trigger').touch()")
            frozen = root / "frozen"
            receipt["frozen_source"] = await env.freeze_source(frozen)
            ledger = (env.trial_paths.verifier_dir / "sandbox-gateway.jsonl").read_bytes()
            assert hashlib.sha256(ledger).hexdigest() == receipt["frozen_source"]["gateway_ledger_sha256"]
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.with_suffix(".gateway.jsonl").write_bytes(ledger)
            source = (frozen / "scripts/profiles/__init__.py").read_text()
            assert "POSITIVE-WATCHER" in source and "FORBIDDEN-WATCHER" not in source
            receipt["descendant_control"] = {"active_watcher_fired": True, "stopped_watcher_did_not_fire": True}
            assert canary.read_bytes() == before
            server_logs = (await docker_bytes("logs", server)).decode()
            assert "denied-" + token not in server_logs
            assert server_logs.count('"challenge": "' + token + '"') == 6
            receipt["server_observations"] = [json.loads(line) for line in server_logs.splitlines() if line.startswith("{")]
            await env.stop()
            env = RepairSandbox(environment_dir=(args.task / "environment").resolve(),
                environment_name="repair-artifact", session_id="repair-artifact-" + token,
                trial_paths=TrialPaths(root / "artifact-trial"),
                task_env_config=EnvironmentConfig(network_mode="no-network", cpus=1, memory_mb=512),
                network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), runtime_image=args.runtime_image)
            await env.start()
            changed = await env.exec("rm /app/scripts/profiles/__init__.py && ln -s /etc/passwd /app/scripts/profiles/__init__.py")
            assert changed.return_code == 0
            try:
                await env.freeze_source(root / "rejected-artifact")
                raise AssertionError("candidate symlink was exported")
            except SandboxArtifactError as error:
                assert error.receipt["solver_stopped"] and error.receipt["broker_revoked"]
                assert error.receipt["gateway_ledger_sha256"] == hashlib.sha256(b"").hexdigest()
                receipt["candidate_artifact_rejected"] = error.receipt
            await env.stop()  # A handled candidate failure must not fail cleanup.
        finally:
            if env is not None:
                await env.stop()
            try:
                await docker_bytes("rm", "--force", server)
            finally:
                await docker_bytes("network", "rm", network)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "passed", "receipt": str(args.receipt), "live_inference": False}))


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("probe requires assertions; do not use -O")
    asyncio.run(main())
