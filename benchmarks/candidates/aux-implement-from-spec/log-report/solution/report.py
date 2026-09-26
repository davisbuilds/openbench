"""Reference solution: access-log report CLI."""
import sys
from collections import Counter


def parse(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ts, method, path_, status, nbytes, ms = line.split()
            rows.append({
                "ts": ts, "method": method, "path": path_,
                "status": int(status), "bytes": int(nbytes), "ms": int(ms),
            })
    return rows


def main(argv):
    logfile, command, rest = argv[0], argv[1], argv[2:]
    rows = parse(logfile)

    if command == "top-paths":
        n = int(rest[0])
        counts = Counter(r["path"] for r in rows)
        for path_, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]:
            print(path_, count)
    elif command == "status-counts":
        counts = Counter(r["status"] for r in rows)
        for status in sorted(counts):
            print(status, counts[status])
    elif command == "error-rate":
        errors = sum(1 for r in rows if r["status"] >= 400)
        pct = (errors / len(rows) * 100) if rows else 0.0
        print("{:.1f}%".format(pct))
    elif command == "bytes-total":
        print(sum(r["bytes"] for r in rows))
    elif command == "slowest":
        n = int(rest[0])
        for r in sorted(rows, key=lambda r: (-r["ms"], r["path"]))[:n]:
            print(r["path"], r["ms"])


if __name__ == "__main__":
    main(sys.argv[1:])
