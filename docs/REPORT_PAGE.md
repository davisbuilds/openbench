# Static HTML report

Generate the GitHub Pages-ready, self-contained report after replacing or adding datasets in the config:

```sh
python3 bench/report_page.py --config bench/report-config.sample.json --out site/index.html
```

You can also bypass the config: `python3 bench/report_page.py --out site/index.html --methodology docs/report-methodology.md results/*.jsonl`. The JSON config lists dataset paths and display titles; paths are relative to the config. Set `"matched": true` on a dataset to compare its harnesses only on shared `(task, trial)` cells. Optional pricing uses the same `input_per_mtok` / `output_per_mtok` schema as `bench/stats.py`. The output has inline CSS and no network or JavaScript dependencies.
