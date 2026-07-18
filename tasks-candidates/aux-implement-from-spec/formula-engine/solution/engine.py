"""Reference solution: spreadsheet formula engine."""
import re

_TOKEN = re.compile(r"\s*(?:(\d+\.\d+|\d+)|([A-Za-z]+\d+)|([+\-*/()]))")


def _tokenize(text):
    tokens = []
    for num, ref, op in _TOKEN.findall(text):
        if num:
            tokens.append(("num", float(num) if "." in num else int(num)))
        elif ref:
            tokens.append(("ref", ref.upper()))
        else:
            tokens.append(("op", op))
    return tokens


def _parse(text):
    tokens = _tokenize(text)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else ("end", None)

    def eat():
        tok = tokens[pos[0]]
        pos[0] += 1
        return tok

    def expr():
        node = term()
        while peek() in (("op", "+"), ("op", "-")):
            op = eat()[1]
            node = ("bin", op, node, term())
        return node

    def term():
        node = factor()
        while peek() in (("op", "*"), ("op", "/")):
            op = eat()[1]
            node = ("bin", op, node, factor())
        return node

    def factor():
        tok = peek()
        if tok == ("op", "-"):
            eat()
            return ("neg", factor())
        if tok == ("op", "("):
            eat()
            node = expr()
            eat()  # consume ")"
            return node
        if tok[0] == "num":
            eat()
            return ("num", tok[1])
        if tok[0] == "ref":
            eat()
            return ("ref", tok[1])
        raise ValueError("unexpected token {!r}".format(tok))

    return expr()


def evaluate(cells):
    """Return {cell: value} for a grid mapping cell name -> raw content."""
    raw = {k: (v if isinstance(v, str) else str(v)) for k, v in cells.items()}
    memo = {}
    active = set()

    def resolve(name):
        if name in memo:
            return memo[name]
        if name in active:
            return "#CYCLE"
        content = raw.get(name, "")
        if content.strip() == "":
            memo[name] = 0
            return 0
        active.add(name)
        if content.startswith("="):
            value = _eval(_parse(content[1:]))
        else:
            value = float(content) if "." in content else int(content)
        active.discard(name)
        memo[name] = value
        return value

    def _eval(node):
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "ref":
            return resolve(node[1])
        if kind == "neg":
            value = _eval(node[1])
            return value if isinstance(value, str) else -value
        _, op, left, right = node
        a = _eval(left)
        if isinstance(a, str):
            return a
        b = _eval(right)
        if isinstance(b, str):
            return b
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        return "#DIV/0" if b == 0 else a / b

    return {name: resolve(name) for name in cells}
