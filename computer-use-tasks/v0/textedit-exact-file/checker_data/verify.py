#!/usr/bin/env python3
import json
import os
import pathlib
import sys


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def contained(root, path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main():
    try:
        root = pathlib.Path(sys.argv[1]).resolve(strict=True)
        context_path = root / "run-context.json"
        context = json.loads(
            context_path.read_text(encoding="utf-8"), object_pairs_hook=unique
        )
        if type(context) is not dict or set(context) != {"output_path", "schema_version"}:
            raise ValueError("run-context.json has an inexact schema")
        if context["schema_version"] != "openbench.computer-use.run-context.v1":
            raise ValueError("unsupported run context schema")
        relative = context["output_path"]
        if type(relative) is not str or not relative or os.path.isabs(relative):
            raise ValueError("output_path must be a non-empty relative path")
        if pathlib.PurePosixPath(relative).as_posix() != relative or ".." in pathlib.PurePosixPath(relative).parts:
            raise ValueError("output_path must be normalized and cannot escape the workspace")

        declared = root / relative
        env_path = sys.argv[2]
        if env_path:
            env_declared = pathlib.Path(env_path)
            if not env_declared.is_absolute():
                raise ValueError("OPENBENCH_NATIVE_OUTPUT_PATH must be absolute")
            if env_declared.resolve(strict=False) != declared.resolve(strict=False):
                raise ValueError("provided environment path disagrees with run context")

        if declared.is_symlink() or not declared.is_file():
            raise ValueError("provided output path is not a regular non-symlink file")
        resolved = declared.resolve(strict=True)
        if not contained(root, resolved):
            raise ValueError("provided output path escapes the run-scoped workspace")

        expected = (pathlib.Path(os.environ["TASK_DIR"]) / "checker_data/expected.txt").read_bytes()
        if declared.read_bytes() != expected:
            raise ValueError("output bytes do not exactly match the UTF-8 fixture")

        artifacts = root / "artifacts"
        entries = sorted(path.relative_to(root).as_posix() for path in artifacts.rglob("*"))
        if entries != [relative]:
            raise ValueError("artifacts contains missing or additional entries")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, IndexError, KeyError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: TextEdit output path and UTF-8 bytes are exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
