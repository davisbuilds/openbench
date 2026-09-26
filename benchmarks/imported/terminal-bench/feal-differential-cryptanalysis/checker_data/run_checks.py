#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'feal-differential-cryptanalysis' test.

cwd = a fresh copy of the workspace: feal.py (reference cipher) is present, and
the agent is expected to have written attack.py exposing
``attack(encrypt_fn) -> int`` that recovers key[5] via differential cryptanalysis.

The upstream pytest builds a C extension of FEAL and checks
``feal_in_c.get_keys()[5] == attack.attack(feal_in_c.encrypt)``. Since the
minimal image has no C toolchain, we verify against the equivalent reference
pure-Python cipher instead. Like upstream's C build, the oracle is
CHECKER-OWNED: the cipher is loaded from checker_data/feal_ref.py (read-only
task dir), never from the agent-editable workspace copy of feal.py — so
doctoring workspace files cannot pin or leak the key. The attack is randomized,
so we require it to succeed on every one of TRIALS independent random keys (the
reference solution passes 20/20 in testing); a correct attack recovers the key
deterministically, an incorrect one will fail well within TRIALS.
"""
import importlib
import importlib.util
import os
import sys

TRIALS = 5


def load_ref_cipher():
    path = os.path.join(os.environ["TASK_DIR"], "checker_data", "feal_ref.py")
    spec = importlib.util.spec_from_file_location("_checker_feal_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    if not os.path.exists(os.path.join(cwd, "attack.py")):
        print("FAIL: attack.py does not exist")
        sys.exit(1)

    feal = load_ref_cipher()
    try:
        attack = importlib.import_module("attack")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not import attack.py: {e}")
        sys.exit(1)

    if not hasattr(attack, "attack"):
        print("FAIL: attack.py does not define attack(encrypt_fn)")
        sys.exit(1)

    for trial in range(1, TRIALS + 1):
        feal.create_random_keys()
        try:
            recovered = attack.attack(feal.encrypt)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: attack raised on trial {trial}: {e}")
            sys.exit(1)
        real = feal.key[5]
        if int(recovered) & 0xFFFFFFFF != int(real) & 0xFFFFFFFF:
            print(f"FAIL: trial {trial}: recovered key[5]={recovered:#010x}, "
                  f"actual={real:#010x}")
            sys.exit(1)

    print(f"PASS: attack recovered key[5] on all {TRIALS} trials")
    sys.exit(0)


if __name__ == "__main__":
    main()
