"""Safely persist rotating CLI credentials from disposable auth copies."""

import contextlib
import os
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


def persist_auth_file(copy_path, master_path):
    """Atomically replace *master_path* when *copy_path* has different bytes.

    Only the named file is read and returned. The lock prevents two local cells
    from interleaving compare/replace; across hosts or non-cooperating writers,
    the last completed cell still wins.
    """
    if not copy_path or not master_path or not os.path.isfile(copy_path):
        return False

    with open(copy_path, "rb") as fh:
        updated = fh.read()

    parent = os.path.dirname(os.path.abspath(master_path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    with _lock(master_path):
        try:
            with open(master_path, "rb") as fh:
                current = fh.read()
        except FileNotFoundError:
            current = None
        if current == updated:
            return False

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
