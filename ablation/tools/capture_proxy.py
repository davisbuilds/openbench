#!/usr/bin/env python3
"""Small local capture proxy for ablation probes.

Logs scrubbed inbound JSON request bodies, forwards to the OpenBench LiteLLM
bridge, and writes one JSON artifact per request. No API key values are stored.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REDACT = "[REDACTED]"


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = k.lower()
            if any(s in lk for s in ("key", "token", "authorization", "api_key", "secret")):
                out[k] = REDACT
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and ("sk-" in value or "Bearer " in value):
        return REDACT
    return value


def short_meta(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    return {
        "model": body.get("model"),
        "has_instructions": isinstance(body.get("instructions"), str),
        "instructions_chars": len(body.get("instructions") or ""),
        "tools_count": len(body.get("tools") or []) if isinstance(body.get("tools"), list) else 0,
        "input_items": len(body.get("input") or []) if isinstance(body.get("input"), list) else None,
        "messages_count": len(body.get("messages") or []) if isinstance(body.get("messages"), list) else None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ablation-capture-proxy/0.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {"_raw_non_json": raw.decode("utf-8", "replace")}
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        idx = self.server.next_index()  # type: ignore[attr-defined]
        artifact = {
            "timestamp_utc": ts,
            "request_index": idx,
            "method": "POST",
            "path": self.path,
            "headers": scrub(dict(self.headers)),
            "body": scrub(parsed),
            "meta": short_meta(parsed),
        }
        out_path = Path(self.server.capture_dir) / f"{idx:03d}-{ts}.json"  # type: ignore[attr-defined]
        out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"captured {out_path} {artifact['meta']}", flush=True)

        target = urlsplit(self.server.target)  # type: ignore[attr-defined]
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(target.hostname, target.port or (443 if target.scheme == "https" else 80), timeout=300)
        forward_path = self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length"}}
        headers["Host"] = target.netloc
        try:
            conn.request("POST", forward_path, body=raw, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() in {"transfer-encoding", "connection", "content-encoding"}:
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            msg = json.dumps({"error": f"capture proxy forward failed: {exc.__class__.__name__}"}).encode()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        print("proxy:", fmt % args, flush=True)


class Server(ThreadingHTTPServer):
    def __init__(self, addr, handler, capture_dir: Path, target: str):
        super().__init__(addr, handler)
        self.capture_dir = str(capture_dir)
        self.target = target.rstrip("/")
        self._idx = 0

    def next_index(self) -> int:
        self._idx += 1
        return self._idx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=4142)
    ap.add_argument("--target", default="http://127.0.0.1:4141")
    ap.add_argument("--capture-dir", required=True)
    args = ap.parse_args()
    capture_dir = Path(args.capture_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    server = Server((args.listen_host, args.listen_port), Handler, capture_dir, args.target)
    print(f"capture proxy on {args.listen_host}:{args.listen_port} -> {args.target}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
