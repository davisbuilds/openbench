"""Private browser backend v3: case-insensitive accessible names.

The observation schema, sandbox policy and host comparisons remain v2. Private
packages selecting this backend must advance their own oracle revision.
"""
from .activity_explorer_v2 import validate_runtime
from ..browser_worker_v3 import program as worker_program
