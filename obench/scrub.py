#!/usr/bin/env python3
"""PII scrubber for LOCAL-ONLY benchmark transcripts.

Transcripts written by ``run.py`` are the raw, unscrubbed output of a coding
harness. They can contain the operator's absolute home paths, username,
hostname, email addresses, and -- worst case -- secrets the agent echoed
(API keys, tokens, long hex/base64 blobs). This module rewrites those into
stable placeholders so a transcript can be read/shared without leaking them.

HARD RULE (user directive): transcripts are LOCAL-ONLY. Publishing one requires
a MANUAL review first -- run ``scrub.py --check <path>`` to see every potential
PII hit, eyeball it, then ``scrub.py <path> --out <dir>`` to emit scrubbed
copies. This tool builds NO publishing machinery: it only reads originals and
writes scrubbed copies elsewhere. It NEVER modifies the originals.

Design guarantees:
  * originals are never mutated (scrubbed copies go to a parallel --out dir);
  * idempotent -- scrubbing already-scrubbed text is a no-op (placeholders match
    no rule), so re-runs are safe;
  * --check only REPORTS (exit 1 if anything was found, 0 if clean) so it can
    gate a manual review; it writes nothing.

Detected PII classes -> placeholder:
  email address .............. <EMAIL>
  api key (sk-/pk-/rk-) ...... <REDACTED_KEY>
  github token ............... <REDACTED_KEY>
  slack token ................ <REDACTED_KEY>
  aws access key id .......... <REDACTED_KEY>
  home path (/Users/<u> ...) . <HOME>
  local username ............. <USER>
  hostname ................... <HOST>
  long hex blob (>=32) ....... <REDACTED_HEX>
  long base64 blob (>=40) .... <REDACTED_B64>

The username / home / hostname literals are discovered from the environment at
runtime (overridable for testing). Over-redaction is deliberate: for a secret
scrubber a false positive is cheap, a false negative is a leak.

Python3 stdlib only.
"""

import argparse
import getpass
import os
import platform
import re
import socket
import sys


# --- static (environment-independent) detectors -----------------------------
# Order matters: higher-signal / more-specific rules run before the greedy
# hex/base64 blob catch-alls so a key keeps its labeled placeholder.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_API_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")
_GITHUB_RE = re.compile(r"\b(?:gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SLACK_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_AWS_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")

_STATIC_RULES = [
    ("email", _EMAIL_RE, "<EMAIL>"),
    ("api-key", _API_KEY_RE, "<REDACTED_KEY>"),
    ("github-token", _GITHUB_RE, "<REDACTED_KEY>"),
    ("slack-token", _SLACK_RE, "<REDACTED_KEY>"),
    ("aws-key", _AWS_RE, "<REDACTED_KEY>"),
]
# hex/base64 run LAST (after the dynamic host/home/user rules) so labeled data
# wins over the generic blob catch-alls.
_BLOB_RULES = [
    ("hex-blob", _HEX_RE, "<REDACTED_HEX>"),
    ("base64-blob", _B64_RE, "<REDACTED_B64>"),
]


def build_context(user=None, home=None, hostnames=None):
    """Discover (or accept overrides for) the local identity to redact.

    Returns a dict of the literal strings to scrub. Tests pass explicit values
    so results don't depend on the machine running them.
    """
    user = user or getpass.getuser()
    home = home or os.path.expanduser("~")
    if hostnames is None:
        hostnames = [socket.gethostname(), platform.node()]
    # De-dupe, keep the short (pre-dot) hostname form too, drop empties/dupes.
    hosts = set()
    for h in hostnames:
        if h:
            hosts.add(h)
            hosts.add(h.split(".")[0])
    return {"user": user, "home": home, "hostnames": sorted(hosts)}


def _dynamic_rules(ctx):
    """Build the identity-derived rules (home paths, username, hostnames).

    Home-path variants are matched longest-first so the fullest prefix wins.
    Only tokens of length >= 4 are used, so a trivially short username or
    hostname can't carpet-bomb ordinary words.
    """
    user = ctx["user"]
    rules = []

    home_paths = [ctx["home"], f"/Users/{user}", f"/home/{user}"]
    # De-dupe preserving longest-first order.
    seen = set()
    for p in sorted({p for p in home_paths if p}, key=len, reverse=True):
        if p not in seen:
            seen.add(p)
            rules.append(("home-path", re.compile(re.escape(p)), "<HOME>"))

    if user and len(user) >= 3:
        rules.append(("username", re.compile(r"\b" + re.escape(user) + r"\b"), "<USER>"))

    for h in sorted((h for h in ctx["hostnames"] if len(h) >= 4), key=len, reverse=True):
        rules.append(("hostname", re.compile(re.escape(h)), "<HOST>"))

    return rules


def _rules(ctx):
    """Full ordered rule list: static -> identity -> greedy blob catch-alls."""
    return _STATIC_RULES + _dynamic_rules(ctx) + _BLOB_RULES


def scrub_text(text, ctx):
    """Return ``text`` with every PII class replaced by its placeholder.

    Idempotent: placeholders match no rule, so ``scrub_text(scrub_text(x)) ==
    scrub_text(x)``.
    """
    for _category, pattern, placeholder in _rules(ctx):
        text = pattern.sub(placeholder, text)
    return text


def find_pii(text, ctx):
    """Report (category, line_no, matched_snippet) for every hit, no mutation.

    Powers ``--check``. Snippets are truncated so a huge blob doesn't flood the
    report, but are otherwise shown verbatim -- the whole point of --check is to
    let a human eyeball exactly what would be redacted before sharing.
    """
    findings = []
    rules = _rules(ctx)
    for lineno, line in enumerate(text.splitlines(), start=1):
        for category, pattern, _placeholder in rules:
            for m in pattern.finditer(line):
                snippet = m.group(0)
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                findings.append((category, lineno, snippet))
    return findings


# --- file / tree helpers ----------------------------------------------------

def _read_text(path):
    """Read a file as text, tolerating undecodable bytes (transcripts are text)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _iter_files(src):
    """Yield (abs_path, rel_path) for a file or every file under a directory."""
    src = os.path.abspath(src)
    if os.path.isfile(src):
        yield src, os.path.basename(src)
        return
    for root, _dirs, files in os.walk(src):
        for name in sorted(files):
            full = os.path.join(root, name)
            yield full, os.path.relpath(full, src)


def scrub_tree(src, out_dir, ctx):
    """Write scrubbed copies of every file under ``src`` into ``out_dir``.

    Mirrors the source layout under ``out_dir``. NEVER writes back into ``src``
    (refuses if ``out_dir`` is inside ``src`` or vice versa). Returns the list of
    output paths written.
    """
    src = os.path.abspath(src)
    out_dir = os.path.abspath(out_dir)
    if out_dir == src or out_dir.startswith(src + os.sep) or src.startswith(out_dir + os.sep):
        raise ValueError("refusing to scrub in place: --out must be a separate directory")

    written = []
    for full, rel in _iter_files(src):
        dest = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(scrub_text(_read_text(full), ctx))
        written.append(dest)
    return written


def check_tree(src, ctx):
    """Return {rel_path: [findings]} for every file under ``src`` with PII."""
    report = {}
    for full, rel in _iter_files(src):
        hits = find_pii(_read_text(full), ctx)
        if hits:
            report[rel] = hits
    return report


def _print_check_report(src, report):
    """Human-readable --check summary. Returns total number of hits."""
    total = 0
    if not report:
        print(f"CLEAN: no potential PII found under {src}")
        return 0
    print(f"POTENTIAL PII under {src} -- review before sharing:\n")
    for rel in sorted(report):
        hits = report[rel]
        total += len(hits)
        by_cat = {}
        for category, lineno, snippet in hits:
            by_cat.setdefault(category, []).append((lineno, snippet))
        print(f"  {rel}  ({len(hits)} hit{'s' if len(hits) != 1 else ''})")
        for category in sorted(by_cat):
            examples = by_cat[category]
            shown = examples[:5]
            for lineno, snippet in shown:
                print(f"    line {lineno:>4}  [{category}]  {snippet}")
            if len(examples) > len(shown):
                print(f"    ... +{len(examples) - len(shown)} more [{category}]")
        print()
    print(f"TOTAL: {total} potential hit(s) across {len(report)} file(s).")
    print("This is a REPORT only; nothing was written. Scrub with: "
          "scrub.py <src> --out <dir>")
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrub PII from LOCAL-ONLY transcripts (never modifies "
                    "originals; --check only reports).")
    parser.add_argument("src", help="transcript file or directory to scrub/check")
    parser.add_argument("--out", default=None,
                        help="destination directory for scrubbed copies "
                             "(required unless --check)")
    parser.add_argument("--check", action="store_true",
                        help="REPORT potential PII only; write nothing. Exit 1 "
                             "if any is found (for a manual review gate).")
    parser.add_argument("--user", default=None,
                        help="override the local username to redact")
    parser.add_argument("--home", default=None,
                        help="override the home path to redact")
    parser.add_argument("--hostname", action="append", default=None,
                        help="hostname to redact (repeatable); "
                             "defaults to this machine's")
    args = parser.parse_args(argv)

    if not os.path.exists(args.src):
        parser.error(f"src does not exist: {args.src}")

    ctx = build_context(user=args.user, home=args.home, hostnames=args.hostname)

    if args.check:
        report = check_tree(args.src, ctx)
        total = _print_check_report(args.src, report)
        return 1 if total else 0

    if not args.out:
        parser.error("--out is required unless --check is given")
    written = scrub_tree(args.src, args.out, ctx)
    print(f"Wrote {len(written)} scrubbed file(s) to {os.path.abspath(args.out)}")
    print("Originals untouched. Re-run scrub.py --check on the OUTPUT to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
