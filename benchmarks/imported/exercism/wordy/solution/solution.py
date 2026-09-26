"""Reference solution: word-problem calculator."""

OPS = {
    "plus": lambda a, b: a + b,
    "minus": lambda a, b: a - b,
    "multiplied": lambda a, b: a * b,
    "divided": lambda a, b: int(a / b),
}


def _is_int(token):
    try:
        int(token)
        return True
    except ValueError:
        return False


def answer(question):
    if not question.startswith("What is") or not question.endswith("?"):
        raise ValueError("unknown question")
    body = question[len("What is"):-1].strip()
    body = body.replace("multiplied by", "multiplied").replace("divided by", "divided")
    tokens = body.split()
    if not tokens or not _is_int(tokens[0]):
        raise ValueError("expected a number")

    result = int(tokens[0])
    i = 1
    while i < len(tokens):
        op = tokens[i]
        if op not in OPS:
            raise ValueError("expected an operation")
        if i + 1 >= len(tokens) or not _is_int(tokens[i + 1]):
            raise ValueError("expected a number")
        result = OPS[op](result, int(tokens[i + 1]))
        i += 2
    return result
