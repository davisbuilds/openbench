# Documentation

- [Repository layout](project/REPOSITORY_LAYOUT.md): where files belong and path compatibility.
- [Setup](guides/setup.md) and [task authoring](guides/contributing-tasks.md).
- [Campaign launch and recovery](benchmark-operations.md).
- [Fork workflow](project/FORK_WORKFLOW.md), [backlog](project/BACKLOG.md), and [extra-hard repair work](project/EXTRA_HARD_REPAIRS.md).
- [Practical agent evaluation design](project/PRACTICAL_AGENT_EVAL_DESIGN.md): task families, outcome measures, usability evaluation, and browser runtime requirements.
- [Activity explorer calibration candidate](../benchmarks/harbor/local/activity-explorer-c1-o1/README.md): first browser task, scoring limits, runtime and quality controls.
- [Token accounting contract](reference/token-accounting.md).
- Historical [results](reports/results.md), [writeup](reports/writeup.md), and [architecture diagram](system/openbench-architecture.html).

## Published GitHub Pages

This directory is a self-contained static release site. To publish it:

1. In the repository settings, enable **GitHub Pages** with **Deploy from a branch**, select the publishing branch, and choose the `/docs` folder.
2. Build each release from its JSONL results. The command writes `docs/releases/<release-id>/index.html`, appends metadata to `docs/releases.json`, and regenerates `docs/index.html`.
3. Review the generated files, then commit them with the source changes.

Example using the GPT-5.6 sample:

```sh
python3 -m obench.report_page .worker/data/gpt56-final.jsonl --site-dir docs --release-id 2026-07-20-gpt56 --title "GPT-5.6 harness comparison"
```

The release date defaults to the leading `YYYY-MM-DD` in the release ID. For other IDs, pass `--release-date YYYY-MM-DD`. Use a fresh release ID for every publication. Repeating an identical build is safe (and repairs an interrupted publication); conflicting reuse of an ID is refused.

For a standalone report instead of a release site, continue to use `--out path/to/report.html`.
