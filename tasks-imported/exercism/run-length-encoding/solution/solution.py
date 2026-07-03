"""Reference solution: run-length encoding."""
import re


def encode(string):
    return re.sub(r"(.)\1+", lambda m: str(len(m.group(0))) + m.group(1), string)


def decode(string):
    return re.sub(r"(\d+)(.)", lambda m: m.group(2) * int(m.group(1)), string)
