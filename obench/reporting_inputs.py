"""Strict read-only input checks shared by convenience reports."""
import json
from collections import defaultdict

from . import stats


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON key {key!r}')
        value[key] = item
    return value


def _constant(value):
    raise ValueError(f'nonfinite JSON number {value}')


def load_jsonl(paths):
    rows = []
    for path in paths:
        with open(path, encoding='utf-8') as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
                    if not isinstance(row, dict):
                        raise ValueError('row is not an object')
                except ValueError as exc:
                    raise ValueError(f'{path}:{number}: invalid result: {exc}') from exc
                # Evidence locations are reader-owned, never trusted from row data.
                row['_src'] = f'{path}:{number}'
                rows.append(row)
    return rows


def validate_rows(rows):
    """Validate before filtering: a filter cannot hide broken suite coverage."""
    mode = stats.validate_matched_comparison_rows(rows)
    studies = {r.get('study_sha256') for r in rows if not stats.is_harbor_result_row(r)}
    if len(studies) > 1:
        raise ValueError('incompatible study identity; report inputs separately')
    identities = defaultdict(set)
    runs = defaultdict(set)
    harbor_cells = set()
    for row in rows:
        provenance = row.get('candidate_provenance') or {}
        if not isinstance(provenance, dict):
            raise ValueError('invalid candidate provenance')
        digest = row.get('task_content_digest') or provenance.get('openbench_task_content_digest')
        identities[row.get('task')].add(json.dumps(digest, sort_keys=True))
        if stats.is_harbor_result_row(row):
            key = (provenance['comparison_arm_id'], stats.comparison_cell_key(row))
            if key in harbor_cells:
                raise ValueError('duplicate Harbor comparison cell; report inputs separately')
            harbor_cells.add(key)
        else:
            key = (row.get('harness'), row.get('model'), row.get('task'), row.get('trial'))
            runs[key].add(row.get('run_id'))
    if any(len(values) > 1 for values in identities.values()):
        raise ValueError('incompatible task content identity; report inputs separately')
    if any(len(values) > 1 for values in runs.values()):
        raise ValueError('ambiguous legacy run identity for one cell; report inputs separately')
    return mode
