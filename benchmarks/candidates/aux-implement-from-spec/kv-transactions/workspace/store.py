"""An in-memory key/value store driven by commands on standard input.

See README.md for the command set and transaction semantics to implement.
"""
import sys


def main():
    for _line in sys.stdin:
        pass  # TODO: implement the command loop


if __name__ == "__main__":
    main()
