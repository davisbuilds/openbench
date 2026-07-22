#!/bin/bash
# OpenBench: dependencies are pinned in the task image; checker runs offline.
set -e
exec /opt/openbench-venv/bin/pytest -q -p no:cacheprovider /tests/test_outputs.py
