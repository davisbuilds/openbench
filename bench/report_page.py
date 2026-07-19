#!/usr/bin/env python3
"""Generate a self-contained, publishable OpenBench HTML report."""

import argparse
import html
import json
import os
import sys
from collections import defaultdict

try:
    from . import compare, stats
except ImportError:
    import compare
    import stats

TOKEN_FIELDS = ("tokens_input_uncached", "tokens_output", "tokens_cache_read")
DEFAULT_METHODOLOGY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs",
    "report-methodology.md",
)


def _pct(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value, digits=0):
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _token_basis(rows):
    values = {
        str(row.get("token_basis") or row.get("token_basis_proxy") or "unknown")
        for row in rows
    }
    return ", ".join(sorted(values))


def _cost_per_solve(rows, solved, pricing):
    if not solved or not pricing:
        return None
    values = [stats.row_cost(row, pricing) for row in rows]
    return None if any(value is None for value in values) else sum(values) / solved


def assemble_tables(datasets, pricing=None, tasks_dirs=None):
    """Return model tables assembled with canonical compare/stat helpers."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    grouped = defaultdict(list)
    titles = {}
    for dataset in datasets:
        valid_rows = [row for row in stats.load_rows([dataset["path"]])
                      if stats.is_valid_result_row(row)]
        for row in valid_rows:
            grouped[str(row["model"])].append(row)
        if dataset.get("title"):
            dataset_models = {str(row["model"]) for row in valid_rows}
            if len(dataset_models) == 1:
                titles[next(iter(dataset_models))] = str(dataset["title"])

    models = []
    for model, model_rows in grouped.items():
        arms = []
        prepared = {}
        for harness in sorted({str(row["harness"]) for row in model_rows}):
            eligible, excluded = compare._filter_arm(
                [row for row in model_rows if str(row["harness"]) == harness], roots)
            unique, duplicates = compare._unique_cells(eligible)
            prepared[harness] = (unique, excluded, duplicates)
        common_cells = set.intersection(*(set(values[0]) for values in prepared.values()))
        for harness, (unique, excluded, duplicates) in prepared.items():
            rows = [unique[cell] for cell in sorted(common_cells)]
            canonical = stats.aggregate_table(rows, (), min_n=0, pricing=pricing)[0]
            solved = canonical["solved"]
            finished = [r for r in rows if stats.class_for_report(r) != "timeout"]
            finished_solved = sum(bool(r["success"]) for r in finished)
            solved_rows = [r for r in rows if r["success"]]
            _, med_wall = compare._mean_median(solved_rows, "wall_time_s")
            tokens = {field: compare._sum_per_solve(rows, field, solved) for field in TOKEN_FIELDS}
            total_tokens = sum(v for v in tokens.values() if v is not None) if all(
                v is not None for v in tokens.values()) else None
            arms.append({
                "arm": harness,
                "solved": solved,
                "n": canonical["n"],
                "rate": canonical["solve_rate"],
                "wilson": canonical["wilson95"],
                "finished_solved": finished_solved,
                "finished_n": len(finished),
                "finished_rate": finished_solved / len(finished) if finished else None,
                "has_timeout": len(finished) != len(rows),
                "med_wall": med_wall,
                **tokens,
                "total_tokens": total_tokens,
                "cost_per_solve": _cost_per_solve(rows, solved, pricing),
                "token_basis": _token_basis(rows),
                "excluded": dict(excluded),
                "duplicates": duplicates,
                "unmatched": len(unique) - len(common_cells),
            })
        arms.sort(key=lambda r: (-(r["rate"] if r["rate"] is not None else -1),
                                 r["med_wall"] if r["med_wall"] is not None else float("inf"),
                                 r["total_tokens"] if r["total_tokens"] is not None else float("inf"),
                                 r["arm"]))
        models.append({
            "model": model,
            "title": titles.get(model, model),
            "arms": arms,
            "has_timeouts": any(a["has_timeout"] for a in arms),
            "has_pricing": bool(pricing and model in pricing),
        })
    models.sort(key=lambda m: m["title"].lower())
    return models


def _markdown(text):
    """Render the small, deliberately supported methodology Markdown subset."""
    blocks = []
    paragraph = []
    def flush():
        if paragraph:
            blocks.append("<p>" + html.escape(" ".join(paragraph)) + "</p>")
            paragraph.clear()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush(); continue
        if line.startswith("### "):
            flush(); blocks.append("<h3>" + html.escape(line[4:]) + "</h3>")
        elif line.startswith("## "):
            flush(); blocks.append("<h2>" + html.escape(line[3:]) + "</h2>")
        elif line.startswith("# "):
            flush(); blocks.append("<h2>" + html.escape(line[2:]) + "</h2>")
        elif line.startswith("- "):
            flush()
            item = "<li>" + html.escape(line[2:]) + "</li>"
            if blocks and blocks[-1].startswith("<ul>"):
                blocks[-1] = blocks[-1][:-5] + item + "</ul>"
            else:
                blocks.append("<ul>" + item + "</ul>")
        else:
            paragraph.append(line)
    flush()
    return "\n".join(blocks)


def render_page(models, methodology, title="OpenBench report", headline=None):
    total = sum(a["n"] for m in models for a in m["arms"])
    headline = headline or f"{len(models)} models, {total:,} countable harness × task × trial cells."
    harnesses = sorted({a["arm"] for m in models for a in m["arms"]})
    sections = []
    for model in models:
        dual = model["has_timeouts"]
        heads = ["Arm (harness × model)", "Solved/countable"]
        heads += ["Rate @cap", "Rate finished"] if dual else ["Rate"]
        heads += ["Wilson 95% CI", "Med wall", "Uncached in/solve", "Output/solve",
                  "Cache-read/solve"]
        if model["has_pricing"]:
            heads.append("$/solve")
        heads.extend(["Token basis", "Excluded / unmatched / duplicates"])
        body = []
        for a in model["arms"]:
            values = [f"{a['arm']} × {model['model']}", f"{a['solved']}/{a['n']}"]
            values += [_pct(a["rate"]), _pct(a["finished_rate"])] if dual else [_pct(a["rate"])]
            values += [f"{a['wilson'][0] * 100:.1f}–{a['wilson'][1] * 100:.1f}%",
                       _num(a["med_wall"], 1) + ("s" if a["med_wall"] is not None else ""),
                       _num(a["tokens_input_uncached"]), _num(a["tokens_output"]),
                       _num(a["tokens_cache_read"])]
            if model["has_pricing"]:
                values.append(
                    "—" if a["cost_per_solve"] is None else f"${a['cost_per_solve']:.3f}"
                )
            values.extend([a["token_basis"],
                           f"{sum(a['excluded'].values())} / {a['unmatched']} / {a['duplicates']}"])
            body.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in values) + "</tr>")
        sections.append(f'<section><h2>{html.escape(model["title"])}</h2><div class="scroll"><table class="results"><thead><tr>' +
                        "".join(f"<th>{html.escape(h)}</th>" for h in heads) +
                        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div></section>")
    model_names = [m["model"] for m in models]
    lookup = {(a["arm"], m["model"]): _pct(a["rate"]) for m in models for a in m["arms"]}
    grid_rows = "".join("<tr><th>" + html.escape(h) + "</th>" + "".join(
        f"<td>{html.escape(lookup.get((h, model), '—'))}</td>" for model in model_names) + "</tr>" for h in harnesses)
    grid = '<section><h2>Harness × model correctness</h2><div class="scroll"><table><thead><tr><th>Harness</th>' + "".join(
        f"<th>{html.escape(m)}</th>" for m in model_names) + "</tr></thead><tbody>" + grid_rows + "</tbody></table></div></section>"
    css = """body{font:16px/1.5 system-ui,sans-serif;color:#17202a;max-width:1200px;margin:auto;padding:2rem;background:#f7f8fa}header{padding:2rem;background:#18253b;color:white;border-radius:12px}section{margin:2rem 0;background:white;padding:1.4rem;border-radius:10px;box-shadow:0 1px 4px #ccd}h1{margin:.1rem 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:.65rem;border-bottom:1px solid #dde2e8;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}thead th{background:#edf2f7}.tag{font-size:.85rem;color:#52606d}footer{color:#52606d;text-align:center}@media(max-width:600px){body{padding:.6rem}section,header{padding:1rem}}"""
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>" + html.escape(title) + "</title><style>" + css + "</style></head><body><header><div class=\"tag\">OPENBENCH</div><h1>" + html.escape(title) + "</h1><p>" + html.escape(headline) + "</p></header>" + "".join(sections) + grid + '<section id="methodology"><h2>Methodology &amp; limitations</h2>' + _markdown(methodology) + "</section><footer>Generated by OpenBench · static, self-contained HTML</footer></body></html>\n"


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", nargs="*", help="OpenBench result JSONL files")
    p.add_argument("--out", required=True, help="output HTML path")
    p.add_argument("--config", help="JSON config containing datasets and titles")
    p.add_argument("--methodology", help="methodology/limitations Markdown")
    p.add_argument("--pricing", help="model pricing JSON (input_per_mtok/output_per_mtok)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    config = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh: config = json.load(fh)
    base = os.path.dirname(os.path.abspath(args.config)) if args.config else os.getcwd()
    datasets = []
    for item in config.get("datasets", []):
        item = {"path": item} if isinstance(item, str) else dict(item)
        item["path"] = os.path.join(base, item["path"]) if not os.path.isabs(item["path"]) else item["path"]
        datasets.append(item)
    datasets += [{"path": path} for path in args.results]
    if not datasets: raise SystemExit("report_page: at least one dataset is required")
    methodology_path = args.methodology or config.get("methodology") or DEFAULT_METHODOLOGY
    if not os.path.isabs(methodology_path):
        methodology_path = os.path.join(base, methodology_path)
    with open(methodology_path, encoding="utf-8") as fh:
        methodology = fh.read()
    pricing_path = args.pricing or config.get("pricing")
    if pricing_path and not os.path.isabs(pricing_path): pricing_path = os.path.join(base, pricing_path)
    pricing = stats.load_pricing(pricing_path)
    page = render_page(assemble_tables(datasets, pricing), methodology,
                       config.get("title", "OpenBench report"), config.get("headline"))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh: fh.write(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
