#!/usr/bin/env python3
"""Stdlib checker for extract-elf."""
import hashlib
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

TASK_DIR = pathlib.Path(os.environ["TASK_DIR"])
DATA = TASK_DIR / "checker_data"
CWD = pathlib.Path.cwd()


SHT_SYMTAB = 2
LOAD_SECTION_NAMES = {".text", ".data", ".rodata"}


def fail(message):
    print(f"FAIL: {message}")
    return 1


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def c_string(buf, offset):
    if offset < 0 or offset >= len(buf):
        return ""
    end = offset
    while end < len(buf) and buf[end] != 0:
        end += 1
    return buf[offset:end].decode("utf-8", errors="replace")


def expected_memory_from_elf(path):
    """Return the upstream-reference memory map for selected ELF sections.

    This is a small stdlib port of the upstream reference extractor. It derives
    the checker oracle from the checker-owned probe binary at check time instead
    of storing an expected-map JSON file that a submitted extract.js could read.
    """
    data = pathlib.Path(path).read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError("probe is not an ELF file")

    elf_class = data[4]
    elf_data = data[5]
    if elf_class not in (1, 2):
        raise ValueError(f"unsupported ELF class {elf_class}")
    if elf_data not in (1, 2):
        raise ValueError(f"unsupported ELF byte order {elf_data}")
    endian = "<" if elf_data == 1 else ">"

    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise ValueError("ELF header is truncated")
        return struct.unpack_from(fmt, data, offset)[0]

    if elf_class == 1:
        shoff = unpack(endian + "I", 32)
        shentsize = unpack(endian + "H", 46)
        shnum = unpack(endian + "H", 48)
        shstrndx = unpack(endian + "H", 50)
    else:
        shoff = unpack(endian + "Q", 40)
        shentsize = unpack(endian + "H", 58)
        shnum = unpack(endian + "H", 60)
        shstrndx = unpack(endian + "H", 62)

    sections = []
    for i in range(shnum):
        off = shoff + i * shentsize
        if off + shentsize > len(data):
            raise ValueError("section header table is truncated")
        if elf_class == 1:
            name, typ, flags, addr, file_off, size, link, info, addralign, entsize = struct.unpack_from(
                endian + "IIIIIIIIII", data, off
            )
        else:
            name, typ, flags, addr, file_off, size, link, info, addralign, entsize = struct.unpack_from(
                endian + "IIQQQQIIQQ", data, off
            )
        sections.append({
            "name": name,
            "type": typ,
            "addr": addr,
            "offset": file_off,
            "size": size,
            "link": link,
            "entsize": entsize,
        })

    if shstrndx >= len(sections):
        raise ValueError("invalid section-name string table index")
    shstr = sections[shstrndx]
    shstr_data = data[shstr["offset"]:shstr["offset"] + shstr["size"]]

    memory = {}
    for section in sections:
        name = c_string(shstr_data, section["name"])
        if name not in LOAD_SECTION_NAMES:
            continue
        payload = data[section["offset"]:section["offset"] + section["size"]]
        for rel in range(0, len(payload) - 3, 4):
            # Match upstream REF: section words are interpreted as little-endian
            # uint32 values regardless of the ELF header's declared byte order.
            memory[str(section["addr"] + rel)] = struct.unpack_from("<I", payload, rel)[0]

    # Exercise symbol-table parsing enough to catch malformed reference probes,
    # matching the upstream reference's dependence on SHT_SYMTAB without adding
    # symbol data to the required output.
    for section in sections:
        if section["type"] != SHT_SYMTAB or not section["entsize"]:
            continue
        if section["link"] >= len(sections):
            raise ValueError("symbol table links to an invalid string table")
        symtab = data[section["offset"]:section["offset"] + section["size"]]
        for rel in range(0, len(symtab), section["entsize"]):
            if rel + section["entsize"] > len(symtab):
                raise ValueError("truncated symbol table entry")

    return memory


def main():
    extract = CWD / "extract.js"
    if not extract.is_file():
        return fail("extract.js does not exist")

    expected_hashes = json.loads((DATA / "input_hashes.json").read_text())
    visible_binary = CWD / "a.out"
    if not visible_binary.is_file():
        return fail("a.out is missing")
    if sha256(visible_binary) != expected_hashes["a.out"]:
        return fail("a.out hash does not match the original task binary")
    if sha256(DATA / "probe.out") != expected_hashes["probe.out"]:
        return fail("checker-owned probe binary hash mismatch")

    # Do not expose TASK_DIR or checker_data paths to untrusted extract.js. The
    # probe is copied to an opaque temporary path, and the child receives only a
    # minimal environment needed to find node and harmless process defaults.
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(CWD),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="elf-probe-") as tmpdir:
            staged_probe = pathlib.Path(tmpdir) / "input.bin"
            shutil.copy2(DATA / "probe.out", staged_probe)
            proc = subprocess.run(
                [
                    "node",
                    "--permission",
                    f"--allow-fs-read={extract}",
                    f"--allow-fs-read={staged_probe}",
                    str(extract),
                    str(staged_probe),
                ],
                cwd=CWD,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
    except FileNotFoundError:
        return fail("node executable is not available")
    except subprocess.TimeoutExpired:
        return fail("extract.js did not finish within the 60s safety timeout")
    if proc.returncode != 0:
        return fail(f"extract.js exited {proc.returncode}: {proc.stderr.strip()}")

    try:
        actual = json.loads(proc.stdout)
    except Exception as exc:
        return fail(f"extract.js output is not valid JSON: {exc}")
    if not isinstance(actual, dict):
        return fail("extract.js output must be a JSON object")

    normalized = {}
    for key, value in actual.items():
        if not isinstance(key, str) or not key.isdigit():
            return fail(f"address key is not a decimal string: {key!r}")
        if not isinstance(value, int) or isinstance(value, bool):
            return fail(f"value for address {key} is not an integer")
        normalized[key] = value

    try:
        expected = expected_memory_from_elf(DATA / "probe.out")
    except Exception as exc:
        return fail(f"checker could not derive reference map from probe ELF: {exc}")

    unknown = sorted(k for k in normalized if k not in expected)
    if unknown:
        return fail(f"included addresses outside the checker-owned reference map: {unknown[:5]}")

    wrong = [k for k, v in normalized.items() if v != expected[k]]
    if wrong:
        return fail(
            f"included {len(wrong)} address(es) with incorrect values; "
            f"sample addresses: {wrong[:5]}"
        )

    found = sum(1 for key in expected if key in normalized)
    coverage = found / len(expected) if expected else 0.0
    if coverage < 0.75:
        return fail(f"coverage {coverage:.2%} is below required 75% ({found}/{len(expected)})")

    print(f"PASS: extracted {found}/{len(expected)} checker-owned memory words ({coverage:.2%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
