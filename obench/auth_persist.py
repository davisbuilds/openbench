"""Safely persist rotating CLI credentials from disposable auth copies."""

import contextlib
import json
import os
import re
import sys
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - OpenBench runners are Unix hosts
    fcntl = None


# (host-HOME relative master, container-HOME relative writable copy).
# This allowlist is the complete set of credentials that may be returned.
AUTH_PERSIST = {
    "codex": [(".codex/auth.json", ".codex/auth.json")],
    "codex_v1": [(".codex/auth.json", ".codex/auth.json")],
    "codex_v2": [(".codex/auth.json", ".codex/auth.json")],
    "pi": [(".pi/agent/auth.json", ".pi/agent/auth.json")],
    "opencode": [
        (".local/share/opencode/auth.json", ".local/share/opencode/auth.json"),
        (".opencode/data/auth.json", ".opencode/data/auth.json"),
    ],
    "grokbuild": [
        (".openbench/grok-container-auth/auth.json", ".grok/auth.json"),
        (".grok/auth.json", ".grok/auth.json"),
    ],
}


@contextlib.contextmanager
def _lock(path):
    """Serialize updates for one auth file across concurrent runner processes."""
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _mutable_auth_key(key):
    """Fields subscription CLIs are expected to rotate in-place."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return ("token" in normalized or normalized.startswith("access")
            or normalized.startswith("refresh")
            or normalized.startswith("expires")
            or normalized.startswith("expiry")
            or normalized in {"lastrefresh", "lastrefreshed", "refreshedat",
                              "updatedat"})


def _validate_rotation(current, updated, path=()):
    """Require identical schema and immutable provider/account metadata."""
    if isinstance(current, dict) and isinstance(updated, dict):
        if current.keys() != updated.keys():
            raise ValueError(f"auth schema changed at {'.'.join(path) or '<root>'}")
        for key in current:
            old_value, new_value = current[key], updated[key]
            # Containers such as Codex's `tokens` include immutable account_id,
            # so only scalar token/expiry leaves are mutable.
            if (_mutable_auth_key(key)
                    and not isinstance(old_value, (dict, list))
                    and not isinstance(new_value, (dict, list))):
                continue
            _validate_rotation(old_value, new_value, (*path, str(key)))
        return
    if isinstance(current, list) and isinstance(updated, list):
        if len(current) != len(updated):
            raise ValueError(f"auth list changed at {'.'.join(path)}")
        for index, (old_value, new_value) in enumerate(zip(current, updated)):
            _validate_rotation(old_value, new_value, (*path, str(index)))
        return
    if current != updated:
        raise ValueError(f"immutable auth identity changed at {'.'.join(path)}")


def _validated_auth_bytes(current_bytes, updated_bytes):
    try:
        current = json.loads(current_bytes)
        updated = json.loads(updated_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("refusing to persist malformed auth JSON") from exc
    if not isinstance(current, dict) or not isinstance(updated, dict):
        raise ValueError("refusing to persist non-object auth JSON")
    _validate_rotation(current, updated)


def persist_auth_file(copy_path, master_path):
    """Atomically replace *master_path* when *copy_path* has different bytes.

    Symlinked masters are resolved once so the atomic replacement updates their
    target without destroying the user's link. Only the named file is read and
    returned. The lock prevents two local cells
    from interleaving compare/replace; across hosts or non-cooperating writers,
    the last completed cell still wins.
    """
    if not copy_path or not master_path or not os.path.isfile(copy_path):
        return False
    master_path = os.path.realpath(master_path)

    with open(copy_path, "rb") as fh:
        updated = fh.read()

    # Avoid creating a lock file (or requiring directory write access) for the
    # overwhelmingly common no-rotation case. The locked comparison below is
    # still authoritative if another runner updates the master meanwhile.
    try:
        with open(master_path, "rb") as fh:
            current = fh.read()
    except FileNotFoundError:
        raise ValueError("refusing to create a missing auth master")
    if current == updated:
        return False
    _validated_auth_bytes(current, updated)

    parent = os.path.dirname(os.path.abspath(master_path))
    with _lock(master_path):
        with open(master_path, "rb") as fh:
            current = fh.read()
        if current == updated:
            return False
        _validated_auth_bytes(current, updated)

        fd, temp_path = tempfile.mkstemp(prefix=".auth-persist-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(updated)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, master_path)
            temp_path = None
            os.chmod(master_path, 0o600)
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
    return True


def try_persist_auth_file(copy_path, master_path):
    """Best-effort adapter wrapper that never masks the cell's real result."""
    try:
        return persist_auth_file(copy_path, master_path)
    except (OSError, ValueError) as exc:
        # Do not include credential contents (or exception text, which can be
        # supplied by a filesystem implementation) in runner output.
        print(f"WARN auth persist-back failed ({type(exc).__name__})",
              file=sys.stderr)
        return False
