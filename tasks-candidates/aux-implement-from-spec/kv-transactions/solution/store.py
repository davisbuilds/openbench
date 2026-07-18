"""Reference solution: transactional in-memory key/value store."""
import sys


def main():
    base = {}
    stack = []  # each open transaction is a layer: key -> value, or key -> None (deleted)
    out = []

    def lookup(key):
        for layer in reversed(stack):
            if key in layer:
                return layer[key]  # may be None when deleted in this layer
        return base.get(key)

    def view():
        merged = dict(base)
        for layer in stack:
            for k, v in layer.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
        return merged

    def write(key, value):
        (stack[-1] if stack else base)[key] = value

    for raw in sys.stdin:
        parts = raw.split()
        if not parts:
            continue
        cmd = parts[0].upper()

        if cmd == "SET":
            write(parts[1], parts[2])
        elif cmd == "DELETE":
            if stack:
                stack[-1][parts[1]] = None
            else:
                base.pop(parts[1], None)
        elif cmd == "GET":
            value = lookup(parts[1])
            out.append(value if value is not None else "NULL")
        elif cmd == "COUNT":
            out.append(str(sum(1 for v in view().values() if v == parts[1])))
        elif cmd == "BEGIN":
            stack.append({})
        elif cmd == "COMMIT":
            if not stack:
                out.append("NO TRANSACTION")
            else:
                for layer in stack:
                    for k, v in layer.items():
                        if v is None:
                            base.pop(k, None)
                        else:
                            base[k] = v
                stack = []
        elif cmd == "ROLLBACK":
            if not stack:
                out.append("NO TRANSACTION")
            else:
                stack.pop()
        elif cmd == "END":
            break
        # Any other line is ignored.

    sys.stdout.write("\n".join(out))
    if out:
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
