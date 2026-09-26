#!/usr/bin/env python3
"""Measure fixed harness context in ablation capture payloads.

Token estimate: tries tiktoken cl100k_base when installed; otherwise uses a
simple GPT-style regex split approximation. This repo environment used the
fallback unless noted in the generated JSON.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT / "captures"
TOKEN_METHOD = "regex_approx_words_punct"
try:
    import tiktoken  # type: ignore
    _enc = tiktoken.get_encoding("cl100k_base")
    TOKEN_METHOD = "tiktoken_cl100k_base"
except Exception:  # noqa: BLE001
    _enc = None


def tokens(text: str) -> int:
    if not text:
        return 0
    if _enc is not None:
        return len(_enc.encode(text))
    # Rough tiktoken-style fallback: words, numbers, and punctuation chunks.
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def words(text: str) -> int:
    return len(re.findall(r"\b\S+\b", text))


def as_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_body(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["body"]


def first_json_capture(name: str) -> Path:
    paths = sorted((CAP / name).glob("[0-9]*.json"))
    if not paths:
        raise FileNotFoundError(name)
    return paths[0]


def codex_components(capture_name: str) -> dict[str, str]:
    body = read_body(first_json_capture(capture_name))
    comps = {
        "base_instructions": body.get("instructions") or "",
        "tool_schemas": as_text(body.get("tools") or []),
        "extra_blocks": "",
        "project_docs": "",
    }
    extra_parts: list[str] = []
    project_parts: list[str] = []
    for item in body.get("input") or []:
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or ""
            if not text:
                continue
            if text.startswith("# AGENTS.md instructions"):
                project_parts.append(text)
            elif text.startswith("<permissions instructions>") or text.startswith("<skills_instructions>") or text.startswith("<environment_context>") or "<apps_instructions>" in text or "<collaboration" in text:
                extra_parts.append(text)
    comps["extra_blocks"] = "\n\n".join(extra_parts)
    comps["project_docs"] = "\n\n".join(project_parts)
    return comps


def pi_components(capture_name: str = "pi") -> dict[str, str]:
    body = read_body(first_json_capture(capture_name))
    messages = body.get("messages") or []
    system = ""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            system = content if isinstance(content, str) else as_text(content)
            break
    return {
        "base_instructions": system,
        "tool_schemas": as_text(body.get("tools") or []),
        "extra_blocks": "",
        "project_docs": "",
    }


def count_components(comps: dict[str, str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, text in comps.items():
        out[name] = {"words": words(text), "tokens": tokens(text), "chars": len(text)}
    out["total"] = {
        "words": sum(v["words"] for k, v in out.items() if k != "total"),
        "tokens": sum(v["tokens"] for k, v in out.items() if k != "total"),
        "chars": sum(v["chars"] for k, v in out.items() if k != "total"),
    }
    return out


def marker_info(capture_name: str) -> dict[str, Any]:
    body = read_body(first_json_capture(capture_name))
    joined = as_text(body)
    return {
        "instructions_chars": len(body.get("instructions") or ""),
        "tools_count": len(body.get("tools") or []) if isinstance(body.get("tools"), list) else 0,
        "input_items": len(body.get("input") or []) if isinstance(body.get("input"), list) else None,
        "has_permissions": "<permissions instructions>" in joined,
        "has_environment": "<environment_context>" in joined,
        "has_skills": "<skills_instructions>" in joined,
        "has_project_doc_start": "PROJECT_DOC_START" in joined,
        "has_project_doc_end": "PROJECT_DOC_END" in joined,
    }


def main() -> None:
    variants = {
        "V0": codex_components("v0"),
        "V1": codex_components("v1"),
        "V2": codex_components("v2"),
        "pi": pi_components("pi"),
    }
    measured = {name: count_components(comps) for name, comps in variants.items()}
    probes = {
        "V0_project_docs": count_components(codex_components("project-docs-v0")),
        "V2_project_docs": count_components(codex_components("project-docs-v2")),
        "markers": {
            "v0": marker_info("v0"),
            "v1": marker_info("v1"),
            "v2": marker_info("v2"),
            "project-docs-v0": marker_info("project-docs-v0"),
            "project-docs-v2": marker_info("project-docs-v2"),
            "unknown-key": marker_info("unknown-key"),
        },
    }
    result = {"token_method": TOKEN_METHOD, "variants": measured, "probes": probes}
    out = ROOT / "measurement.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
