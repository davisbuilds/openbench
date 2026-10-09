"""Closed browser-only container policy; no host networking or extra caps."""
import hashlib
import json
from pathlib import Path

POLICY = Path(__file__).with_name('browser-seccomp.json')


def security_options():
    return ['no-new-privileges:true', 'seccomp=' + str(POLICY)]


def verify_options(options):
    values = list(options or [])
    privileges = [s for s in values if s in ('no-new-privileges', 'no-new-privileges:true')]
    profiles = [s for s in values if s.startswith('seccomp=')]
    if len(values) != 2 or len(privileges) != 1 or len(profiles) != 1:
        return False
    # Docker inspect embeds the loaded JSON, not the host policy pathname.
    try:
        return json.loads(profiles[0].split('=', 1)[1]) == json.loads(POLICY.read_text())
    except (ValueError, OSError):
        return False


def identity():
    return hashlib.sha256(POLICY.read_bytes()).hexdigest()
