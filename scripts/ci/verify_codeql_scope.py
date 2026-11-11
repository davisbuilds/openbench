#!/usr/bin/env python3
"""Check actual extracted-source controls in a finalized CodeQL database.

CodeQL's src.zip records scanned source files. Inspect its directory locally;
never extract or upload the database. See GitHub's "CodeQL scanned fewer lines
than expected" documentation for this diagnostic surface.
"""

import json
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile


CONTROLS = {
    'python': {
        'included': (
            'obench/cli.py',
            'scripts/ci/verify_codeql_scope.py',
            'experiments/repair_cases/am123/driver.py',
            'benchmarks/local/am-benchmark-pr106-v2/checker_data/check.py',
            'data/packs/openbench-tb-hard/dna-insert/checker_data/harbor-tests/test_outputs.py',
        ),
        'excluded': (
            'benchmarks/core/webcore/workspace/webcore/__init__.py',
            'benchmarks/core/webcore/solution/webcore/__init__.py',
            'data/packs/openbench-core-smoke/make-it-run/workspace/main.py',
        ),
    },
    'javascript-typescript': {
        'included': (
            'experiments/ablations/tools/pi-deepseek-provider.mjs',
            'benchmarks/local/am-benchmark-pr106-v2/checker_data/golden.test.ts',
        ),
        'excluded': (
            'benchmarks/harbor/local/am-benchmark-pr106-v4/environment/app/src/server.ts',
            'experiments/repair_cases/am123/source/src/db/schema.ts',
            'experiments/repair_cases/am123/reference/src/db/schema.ts',
            'experiments/repair_cases/am123/partial/src/db/schema.ts',
        ),
    },
    'actions': {
        'included': ('.github/workflows/ci.yml', '.github/workflows/codeql.yml'),
        'excluded': (),
    },
}


def verify_archive(database, language, root):
    """Return control observations, failing closed on missing diagnostic inputs."""
    controls = CONTROLS[language]
    root = Path(root).resolve()
    for path in (*controls['included'], *controls['excluded']):
        if not (root / path).is_file():
            raise ValueError('scope control is missing from checkout: ' + path)
    archive = Path(database) / 'src.zip'
    repository_files = set()
    external_files = 0
    prefix = root.as_posix().lstrip('/') + '/'
    with zipfile.ZipFile(archive) as sources:
        for entry in sources.infolist():
            if entry.is_dir():
                continue
            name = entry.filename.lstrip('/')
            if '..' in PurePosixPath(name).parts or '\\' in name:
                raise ValueError('unrecognized source archive path: ' + entry.filename)
            if name.startswith(prefix):
                repository_files.add(name[len(prefix):])
            else:
                # Extractors may archive external libraries as well as this
                # checkout. Do not confuse them with repository-relative names.
                external_files += 1
    if not repository_files:
        raise ValueError('source archive has no paths rooted at checkout: ' + str(root))
    included = {path: path in repository_files for path in controls['included']}
    excluded = {path: path not in repository_files for path in controls['excluded']}
    return {
        'language': language,
        'repository_files': len(repository_files),
        'external_files': external_files,
        'included_controls': included,
        'excluded_controls': excluded,
        'ok': all(included.values()) and all(excluded.values()),
    }


def main():
    try:
        language = os.environ['CODEQL_LANGUAGE']
        if language not in CONTROLS:
            raise ValueError('unsupported scope-control language: ' + language)
        databases = json.loads(os.environ['CODEQL_DATABASES'])
        aliases = {language, 'javascript'} if language == 'javascript-typescript' else {language}
        if (not isinstance(databases, dict) or len(databases) != 1
                or not set(databases).issubset(aliases)
                or not all(isinstance(path, str) and path for path in databases.values())):
            raise ValueError('expected one database for the current language')
        report = verify_archive(next(iter(databases.values())), language,
                                os.environ['GITHUB_WORKSPACE'])
    except (KeyError, ValueError, OSError, zipfile.BadZipFile) as error:
        print('CodeQL scope verification failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f"### CodeQL extracted-source controls: {language}\n\n")
            summary.write(f"Repository source files: {report['repository_files']}; "
                          f"external source files ignored: {report['external_files']}.\n\n")
            for kind in ('included', 'excluded'):
                for path, passed in report[kind + '_controls'].items():
                    summary.write(f"- {'PASS' if passed else 'FAIL'} {kind}: `{path}`\n")
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
