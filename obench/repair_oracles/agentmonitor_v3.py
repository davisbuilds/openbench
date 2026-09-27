"""Version 3: observe preserved benchmark rows without fixing storage IDs."""
from . import agentmonitor as legacy

worker_program = legacy.worker_program
validate_runtime = legacy.validate_runtime


def cases():
    fixtures = legacy.cases()
    for name, _, request in fixtures:
        if name == 'MIG2':
            request['steps'][2]['sql'] = (
                "SELECT study_id,study,tokens_in FROM events WHERE source='benchmark'"
            )
    return fixtures


def grade(results):
    # Same preserved values, row cardinality, and all other expectations.
    return {**legacy.grade(results), 'oracle_version': 3}
