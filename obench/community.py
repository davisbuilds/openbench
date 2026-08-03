#!/usr/bin/env python3
"""Community publish-bundle submissions for the public OpenBench site.

Third parties land a verified ``obench publish`` bundle under
``data/community/<submitter>-<slug>/`` with a small ``submission.toml``.
CI re-runs ``obench verify`` on every bundle; maintainers sync accepted
submissions onto the GitHub Pages site (``docs/community.json`` + copied
cards under ``docs/community/``) so ``docs/index.html`` lists them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib

from . import publish
from . import report_page
from .paths import default_tasks_dir, resolve_tasks_dir

BUNDLE_FILES = ("index.html", "results.jsonl", "provenance.json", "README.md")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?$")


class CommunityError(ValueError):
    """User-facing community submission failure."""


def _validated_date(value):
    return report_page._validated_date(value)


def load_submission_toml(path):
    """Parse and validate a community ``submission.toml``.

    Required keys: ``submitter`` (GitHub handle), ``date`` (YYYY-MM-DD),
    ``claim`` (short summary). Optional: ``title``, ``link``.
    """
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise CommunityError(f"missing submission.toml: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CommunityError(f"invalid submission.toml: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CommunityError(f"submission.toml must be a table: {path}")

    submitter = raw.get("submitter")
    if not isinstance(submitter, str) or not _HANDLE_RE.fullmatch(submitter):
        raise CommunityError(
            f"{path}: submitter must be a GitHub-style handle "
            "(letters, digits, single hyphens; max 39 chars)"
        )

    date = raw.get("date")
    if not isinstance(date, str):
        raise CommunityError(f"{path}: date must be a YYYY-MM-DD string")
    try:
        date = _validated_date(date)
    except ValueError as exc:
        raise CommunityError(f"{path}: {exc}") from exc

    claim = raw.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise CommunityError(f"{path}: claim must be a non-empty string")
    claim = claim.strip()

    title = raw.get("title")
    if title is None:
        title = claim
    elif not isinstance(title, str) or not title.strip():
        raise CommunityError(f"{path}: title must be a non-empty string when set")
    else:
        title = title.strip()

    link = raw.get("link")
    if link is not None:
        if not isinstance(link, str) or not link.strip():
            raise CommunityError(f"{path}: link must be a non-empty string when set")
        link = link.strip()
        if not link.startswith(("https://", "http://")):
            raise CommunityError(f"{path}: link must be an http(s) URL")

    return {
        "submitter": submitter,
        "date": date,
        "claim": claim,
        "title": title,
        "link": link,
    }


def _submission_id_from_dirname(dirname):
    if not _ID_RE.fullmatch(dirname):
        raise CommunityError(
            f"submission directory name must be path-safe "
            f"([A-Za-z0-9][A-Za-z0-9._-]*): {dirname!r}"
        )
    return dirname


def validate_bundle_dir(bundle_dir):
    """Ensure a submission directory contains the publish-bundle files."""
    missing = [
        name for name in BUNDLE_FILES
        if not os.path.isfile(os.path.join(bundle_dir, name))
    ]
    if missing:
        raise CommunityError(
            f"{bundle_dir}: missing publish-bundle file(s): {', '.join(missing)}"
        )


def load_submission(submission_dir):
    """Load one community submission (toml + bundle presence check)."""
    submission_dir = os.path.abspath(submission_dir)
    if not os.path.isdir(submission_dir):
        raise CommunityError(f"not a directory: {submission_dir}")
    sid = _submission_id_from_dirname(os.path.basename(submission_dir))
    meta = load_submission_toml(os.path.join(submission_dir, "submission.toml"))
    validate_bundle_dir(submission_dir)
    return {
        "id": sid,
        "dir": submission_dir,
        **meta,
    }


def discover_submissions(community_dir):
    """Return validated submissions under ``community_dir``, sorted by id."""
    community_dir = os.path.abspath(community_dir)
    if not os.path.isdir(community_dir):
        return []
    found = []
    for name in sorted(os.listdir(community_dir)):
        if name.startswith(".") or name == "README.md":
            continue
        path = os.path.join(community_dir, name)
        if not os.path.isdir(path):
            continue
        found.append(load_submission(path))
    return found


def submission_to_manifest_entry(submission):
    """Map a loaded submission to a ``community.json`` / site-index entry."""
    entry = {
        "id": submission["id"],
        "submitter": submission["submitter"],
        "date": submission["date"],
        "claim": submission["claim"],
        "title": submission["title"],
        "path": f"community/{submission['id']}/index.html",
    }
    if submission.get("link"):
        entry["link"] = submission["link"]
    return entry


def build_community_manifest(submissions):
    """Build the sorted list written to ``docs/community.json``."""
    return [submission_to_manifest_entry(s) for s in submissions]


def verify_submission(submission, tasks_dirs=None):
    """Run publish.verify_bundle on one submission; return (checks, exit_code)."""
    checks = publish.verify_bundle(submission["dir"], tasks_dirs=tasks_dirs)
    failed = sum(1 for item in checks if item["status"] != "PASS")
    return checks, (1 if failed else 0)


def verify_all(community_dir, tasks_dirs=None):
    """Verify every submission under ``community_dir``.

    Returns ``(results, exit_code)`` where ``results`` is a list of
    ``{id, checks, exit_code}`` and ``exit_code`` is non-zero if any
    submission is invalid or has a FAIL verify verdict.
    """
    try:
        submissions = discover_submissions(community_dir)
    except CommunityError as exc:
        print(f"community: {exc}", file=sys.stderr)
        return [], 2

    if not submissions:
        print(f"community: no submissions under {community_dir}")
        return [], 0

    results = []
    worst = 0
    for submission in submissions:
        print(f"== verify {submission['id']} ==")
        checks, code = verify_submission(submission, tasks_dirs=tasks_dirs)
        publish.print_verify_report(checks)
        results.append({
            "id": submission["id"],
            "checks": checks,
            "exit_code": code,
        })
        if code != 0:
            worst = max(worst, code)
    return results, worst


def _copy_bundle_to_site(submission, site_community_dir):
    """Copy publish-bundle files into ``docs/community/<id>/``."""
    dest = os.path.join(site_community_dir, submission["id"])
    os.makedirs(dest, exist_ok=True)
    for name in BUNDLE_FILES:
        src = os.path.join(submission["dir"], name)
        shutil.copy2(src, os.path.join(dest, name))
    # Keep submission.toml next to the served card for provenance on the site.
    shutil.copy2(
        os.path.join(submission["dir"], "submission.toml"),
        os.path.join(dest, "submission.toml"),
    )
    return dest


def load_community_manifest(site_dir):
    """Read ``community.json`` from a site dir; missing → empty list."""
    path = os.path.join(site_dir, "community.json")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise CommunityError("community.json must contain a JSON list")
    return data


def load_releases_manifest(site_dir):
    """Read ``releases.json`` from a site dir; missing → empty list."""
    path = os.path.join(site_dir, "releases.json")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise CommunityError("releases.json must contain a JSON list")
    return data


def load_packs_manifest(site_dir):
    """Read ``packs.json`` from a site dir; missing → empty list."""
    from .packs import load_packs_index
    return load_packs_index(site_dir)


def write_site_index(site_dir, releases=None, community=None, packs=None):
    """Regenerate the site landing page (``docs/index.html``).

    The landing page *is* the leaderboard, so this rebuilds the whole board.
    Callers write their manifest JSON before calling, and the board reads those
    files back, which is why the manifest arguments are accepted for
    compatibility but no longer consulted.
    """
    del releases, community, packs
    from . import leaderboard, site
    # Use the same default scan roots as `obench site build`, so a rebuild
    # triggered by a publish produces the same page as one run by hand.
    info = site.write_board(
        site_dir, community_dir=leaderboard._default_community_dir())
    return info["html_path"]


def sync_community_to_site(community_dir, site_dir, tasks_dirs=None):
    """Copy accepted bundles to the Pages site and refresh manifests/index.

    Writes ``site_dir/community.json``, copies each bundle under
    ``site_dir/community/<id>/``, and regenerates ``site_dir/index.html``
    using the existing ``releases.json`` plus the new community list.
    """
    community_dir = os.path.abspath(community_dir)
    site_dir = os.path.abspath(site_dir)
    submissions = discover_submissions(community_dir)
    failures = []
    for submission in submissions:
        checks, code = verify_submission(submission, tasks_dirs=tasks_dirs)
        if code:
            failed = ", ".join(
                item["name"] for item in checks if item["status"] != "PASS"
            )
            failures.append(f"{submission['id']}: {failed}")
    if failures:
        raise CommunityError(
            "refusing to sync unverified submission(s): " + "; ".join(failures)
        )
    manifest = build_community_manifest(submissions)

    site_community = os.path.join(site_dir, "community")
    os.makedirs(site_community, exist_ok=True)

    # Remove stale site copies whose ids are no longer in data/community/.
    keep = {s["id"] for s in submissions}
    if os.path.isdir(site_community):
        for name in os.listdir(site_community):
            path = os.path.join(site_community, name)
            if os.path.isdir(path) and name not in keep:
                shutil.rmtree(path)

    for submission in submissions:
        _copy_bundle_to_site(submission, site_community)

    manifest_path = os.path.join(site_dir, "community.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    index_path = write_site_index(site_dir, community=manifest)
    return {
        "manifest_path": manifest_path,
        "index_path": index_path,
        "count": len(submissions),
        "ids": [s["id"] for s in submissions],
    }


def _default_community_dir():
    return os.path.join(os.getcwd(), "data", "community")


def _default_site_dir():
    return os.path.join(os.getcwd(), "docs")


def _resolve_tasks_dirs(explicit):
    if explicit:
        return explicit
    try:
        return [resolve_tasks_dir()]
    except Exception:  # noqa: BLE001 - verify can proceed without tasks
        discovered = default_tasks_dir()
        return [discovered] if discovered else []


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench community",
        description="Manage community publish-bundle submissions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser(
        "verify",
        help="run obench verify on every bundle under data/community/",
    )
    p_verify.add_argument(
        "--community-dir", default=None,
        help="community root (default: ./data/community)",
    )
    p_verify.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for content digests (repeatable)",
    )

    p_sync = sub.add_parser(
        "sync",
        help="copy accepted bundles to docs/ and regenerate community.json + index",
    )
    p_sync.add_argument(
        "--community-dir", default=None,
        help="community root (default: ./data/community)",
    )
    p_sync.add_argument(
        "--site-dir", default=None,
        help="GitHub Pages site directory (default: ./docs)",
    )
    p_sync.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for content digests (repeatable)",
    )

    p_list = sub.add_parser(
        "list",
        help="list validated submissions (JSON to stdout)",
    )
    p_list.add_argument(
        "--community-dir", default=None,
        help="community root (default: ./data/community)",
    )

    args = parser.parse_args(argv)
    community_dir = args.community_dir or _default_community_dir()

    if args.command == "verify":
        tasks_dirs = _resolve_tasks_dirs(args.tasks_dir)
        _results, code = verify_all(community_dir, tasks_dirs=tasks_dirs)
        return code

    if args.command == "sync":
        site_dir = args.site_dir or _default_site_dir()
        tasks_dirs = _resolve_tasks_dirs(args.tasks_dir)
        try:
            info = sync_community_to_site(
                community_dir, site_dir, tasks_dirs=tasks_dirs)
        except CommunityError as exc:
            print(f"community: {exc}", file=sys.stderr)
            return 2
        print(
            f"Synced {info['count']} community submission(s) → {site_dir}"
        )
        for sid in info["ids"]:
            print(f"  {sid}")
        print(f"  community.json  {info['manifest_path']}")
        print(f"  index.html      {info['index_path']}")
        return 0

    if args.command == "list":
        try:
            submissions = discover_submissions(community_dir)
        except CommunityError as exc:
            print(f"community: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(build_community_manifest(submissions), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
