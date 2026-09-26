"""Reference solution: balanced brackets."""

CLOSE_TO_OPEN = {")": "(", "]": "[", "}": "{"}
OPENERS = set(CLOSE_TO_OPEN.values())


def is_paired(value):
    stack = []
    for ch in value:
        if ch in OPENERS:
            stack.append(ch)
        elif ch in CLOSE_TO_OPEN:
            if not stack or stack.pop() != CLOSE_TO_OPEN[ch]:
                return False
    return not stack
