#!/usr/bin/env python3
"""Generate a self-contained, publishable OpenBench HTML report."""

import argparse
import html
import json
import math
import os
import re
import stat
import sys
import tempfile
from collections import defaultdict
from datetime import date

from . import compare, stats

TOKEN_FIELDS = ("tokens_input_uncached", "tokens_output", "tokens_cache_read")
PALETTE = (
    "#0072B2", "#E69F00", "#009E73", "#D55E00",
    "#CC79A7", "#56B4E9", "#7A6F00", "#000000",
)
CLI_ARMS = {"cursor", "opencode"}
MATERIAL_TIMEOUT_RATE = 0.05
TOKEN_NOTE = ("Total tokens/solve (incl. cache reads) is computed uniformly from the split "
              "fields: uncached input + output + cache read. The vendor `tokens` field is never "
              "used. Split fields use each harness's own classification and are NOT "
              "cross-comparable. Badge bases: unmetered / self-reported / proxy-measured.")
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
    """Normalize arm badge to unmetered / self-reported / proxy-measured."""
    values = set()
    for row in rows:
        basis = stats.display_token_basis(row)
        values.add(basis if basis is not None else "unknown")
    if not values:
        return "unknown"
    ranked = [stats.TOKEN_BASIS_UNMETERED, stats.TOKEN_BASIS_PROXY,
              stats.TOKEN_BASIS_SELF, "unknown"]
    ordered = [label for label in ranked if label in values]
    ordered.extend(sorted(values - set(ordered)))
    return ", ".join(ordered)


def split_total_tokens_per_solve(rows, solved=None):
    """Total tokens/solve from uncached input + output + cache read splits.

    This intentionally never consults the vendor aggregate ``tokens`` field.
    As with the split columns, proxy splits are used only when a native split is
    unavailable, and incomplete rows make the arm unmetered rather than mixing
    unlike accounting bases.
    """
    if solved is None:
        solved = sum(bool(row.get("success")) for row in rows)
    if not solved:
        return None
    totals = []
    selected_source = None
    for row in rows:
        native = [row.get(field) for field in TOKEN_FIELDS]
        if all(stats.is_nonnegative_number(value) for value in native):
            source, splits = "native", native
        elif row.get("token_basis_proxy") == "proxy_measured":
            source = "proxy"
            splits = [
                row.get("tokens_proxy_" + field.removeprefix("tokens_"))
                for field in TOKEN_FIELDS
            ]
            if not all(stats.is_nonnegative_number(value) for value in splits):
                return None
        else:
            return None
        if selected_source is not None and source != selected_source:
            return None
        selected_source = source
        totals.append(sum(splits))
    return sum(totals) / solved


def _cost_per_solve(rows, solved, pricing):
    if not solved or not pricing:
        return None
    values = [stats.row_cost(row, pricing) for row in rows]
    return None if any(value is None for value in values) else sum(values) / solved


def _arm_identity(row):
    kind = "candidate" if isinstance(row.get("candidate_provenance"), dict) else "baseline"
    return kind, compare._row_arm(row)


def _arm_labels(rows):
    """Give baseline and candidate arms distinct, stable labels."""
    identities = sorted({_arm_identity(row) for row in rows})
    baseline_names = {name for kind, name in identities if kind == "baseline"}
    labels = {}
    used = set()
    for kind, name in identities:
        base = name + " (candidate)" if kind == "candidate" and name in baseline_names else name
        label = base
        suffix = 2
        while label in used:
            label = f"{base}-{suffix}"
            suffix += 1
        labels[(kind, name)] = label
        used.add(label)
    return labels


def assemble_tables(datasets, pricing=None, tasks_dirs=None):
    """Return model tables assembled with canonical compare/stat helpers."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    grouped = defaultdict(list)
    titles = {}
    matching_policy = {}
    for dataset in datasets:
        loaded_rows = stats.load_rows([dataset["path"]])
        invalid_count = sum(not stats.is_valid_result_row(row) for row in loaded_rows)
        if invalid_count:
            raise ValueError(
                f"{dataset['path']}: {invalid_count} invalid result row(s); refusing to publish"
            )
        valid_rows = loaded_rows
        for row in valid_rows:
            grouped[str(row["model"])].append(row)
        dataset_models = {str(row["model"]) for row in valid_rows}
        if dataset.get("title") and len(dataset_models) == 1:
            titles[next(iter(dataset_models))] = str(dataset["title"])
        policy = bool(dataset.get("matched"))
        for dataset_model in dataset_models:
            if dataset_model in matching_policy and matching_policy[dataset_model] != policy:
                raise ValueError(
                    f"model {dataset_model!r} has conflicting matched policies across datasets"
                )
            matching_policy[dataset_model] = policy

    models = []
    for model, model_rows in grouped.items():
        arms = []
        prepared = {}
        arm_labels = _arm_labels(model_rows)
        for identity, arm_label in arm_labels.items():
            eligible, excluded = compare._filter_arm(
                [row for row in model_rows if _arm_identity(row) == identity], roots)
            unique, duplicates = compare._unique_cells(eligible)
            if duplicates:
                raise ValueError(
                    f"{model} × {arm_label}: {duplicates} duplicate (task, trial) cell(s)"
                )
            prepared[arm_label] = (eligible, unique, excluded, duplicates)
        common_cells = set.intersection(*(set(values[1]) for values in prepared.values()))
        matched = matching_policy.get(model, False)
        provenance_rows = []
        for harness, (eligible, unique, excluded, duplicates) in prepared.items():
            rows = ([unique[cell] for cell in sorted(common_cells)]
                    if matched else eligible)
            provenance_rows.extend(dict(row, _report_arm=harness) for row in rows)
            if rows:
                canonical = stats.aggregate_table(rows, (), min_n=0, pricing=pricing)[0]
            else:
                canonical = {"solved": 0, "n": 0, "solve_rate": None,
                             "wilson95": list(stats.wilson_ci(0, 0)),
                             "mean_score": None}
            solved = canonical["solved"]
            finished = [r for r in rows if stats.class_for_report(r) != "timeout"]
            timeout_count = len(rows) - len(finished)
            finished_solved = sum(bool(r["success"]) for r in finished)
            solved_rows = [r for r in rows if r["success"]]
            mean_wall, med_wall = compare._mean_median(solved_rows, "wall_time_s")
            tokens = {field: compare._sum_per_solve(rows, field, solved) for field in TOKEN_FIELDS}
            # Uniform total is derived from the three displayed split fields;
            # never trust the vendor aggregate, whose semantics vary by harness.
            total_tokens = split_total_tokens_per_solve(rows, solved)
            arms.append({
                "arm": harness,
                "solved": solved,
                "n": canonical["n"],
                "rate": canonical["solve_rate"],
                "wilson": canonical["wilson95"],
                "mean_score": canonical.get("mean_score"),
                "finished_solved": finished_solved,
                "finished_n": len(finished),
                "finished_rate": finished_solved / len(finished) if finished else None,
                "timeout_count": timeout_count,
                "has_timeout": timeout_count > 0,
                "material_timeout": bool(rows and timeout_count / len(rows) > MATERIAL_TIMEOUT_RATE),
                "mean_wall": mean_wall,
                "med_wall": med_wall,
                **tokens,
                "total_tokens": total_tokens,
                "cost_per_solve": _cost_per_solve(rows, solved, pricing),
                "token_basis": _token_basis(rows),
                "cli_basis": any(str(row.get("harness", "")).lower() in CLI_ARMS
                                 for row in rows),
                "excluded": dict(excluded),
                "duplicates": duplicates,
                "unmatched": len(unique) - len(common_cells) if matched else 0,
            })
        arms.sort(key=lambda r: (-(r["rate"] if r["rate"] is not None else -1),
                                 r["med_wall"] if r["med_wall"] is not None else float("inf"),
                                 r["total_tokens"] if r["total_tokens"] is not None else float("inf"),
                                 r["arm"]))
        provenance = stats.build_provenance(provenance_rows, ("_report_arm",))
        models.append({
            "model": model,
            "title": titles.get(model, model),
            "arms": arms,
            "has_timeouts": any(a["has_timeout"] for a in arms),
            "material_timeouts": any(a["material_timeout"] for a in arms),
            "has_pricing": bool(pricing and model in pricing),
            "provenance": provenance,
        })
    models.sort(key=lambda m: m["title"].lower())
    return models


def _chart_data(model):
    """Extract the available numeric values consumed by the three charts."""
    return [{
        "arm": arm["arm"],
        "rate": arm.get("rate"),
        "wilson": arm.get("wilson"),
        "total_tokens": arm.get("total_tokens"),
        "med_wall": arm.get("med_wall"),
        "cli_basis": arm.get(
            "cli_basis", arm["arm"].split(" (", 1)[0].lower() in CLI_ARMS),
    } for arm in model["arms"]]


def _svg_text(x, y, text, **attrs):
    attributes = " ".join(f'{key.replace("_", "-")}="{html.escape(str(value))}"'
                          for key, value in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {attributes}>{html.escape(str(text))}</text>'


def _empty_chart(title, message):
    return (f'<svg class="chart" viewBox="0 0 820 180" role="img" '
            f'aria-label="{html.escape(title)}"><title>{html.escape(title)}</title>'
            f'{_svg_text(410, 95, message, text_anchor="middle")}</svg>')


def _correctness_chart(data, colors):
    rows = [row for row in data if row["rate"] is not None and row["wilson"]]
    if not rows:
        return _empty_chart("Correctness by harness", "Correctness data unavailable.")
    rows.sort(key=lambda row: (-row["rate"], row["arm"]))
    left = max(145, max(len(row["arm"]) for row in rows) * 7 + 20)
    plot_w, right, top, row_h = 605, 70, 38, 38
    width, height = left + plot_w + right, top + len(rows) * row_h + 38
    parts = []
    for index, row in enumerate(rows):
        y = top + index * row_h
        rate_x = left + row["rate"] * plot_w
        low_x, high_x = (left + bound * plot_w for bound in row["wilson"])
        color = colors[row["arm"]]
        parts.append(
            f'<g data-arm="{html.escape(row["arm"])}">'
            f'<rect x="{left}" y="{y - 11}" width="{max(0, rate_x-left):.1f}" '
            f'height="22" rx="3" fill="{color}" opacity=".78"/>'
            f'<path d="M {low_x:.1f} {y} H {high_x:.1f} M {low_x:.1f} {y-6} V {y+6} '
            f'M {high_x:.1f} {y-6} V {y+6}" class="whisker"/>'
            + _svg_text(left - 10, y + 5, row["arm"], text_anchor="end")
            + _svg_text(min(rate_x + 8, width - 48), y + 5, _pct(row["rate"]),
                        font_weight="700")
            + "</g>"
        )
    for tick in range(0, 101, 20):
        x = left + tick / 100 * plot_w
        parts.append(f'<path d="M {x:.1f} {top-20} V {height-30}" class="grid"/>')
        parts.append(_svg_text(x, height - 9, f"{tick}%", text_anchor="middle"))
    label = "Correctness by harness with Wilson 95% confidence intervals"
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{label}">'
            f'<title>{label}</title>' + "".join(parts) + "</svg>")


def _scatter_chart(data, colors, field, title, log_scale=False):
    rows = [row for row in data if row["rate"] is not None and
            isinstance(row.get(field), (int, float)) and
            (row[field] > 0 if log_scale else row[field] >= 0)]
    if not rows:
        return _empty_chart(f"{title} by correctness", f"{title} data unavailable.")
    left, top, bottom = 92, 28, 65
    plot_w = 573
    right = max(155, max(len(row["arm"]) for row in rows) * 7 + 38)
    width = left + plot_w + right
    plot_h = max(337, (len(rows) - 1) * 30 + 24)
    height = top + plot_h + bottom
    values = [row[field] for row in rows]
    transformed = [math.log10(value) if log_scale else value for value in values]
    low, high = min(transformed), max(transformed)
    if low == high:
        low, high = low - .5, high + .5
    else:
        padding = (high - low) * .12
        low, high = low - padding, high + padding
    if not log_scale:
        low = max(0, low)
    parts = []
    for tick in range(0, 101, 20):
        x = left + tick / 100 * plot_w
        parts.extend([f'<path d="M {x:.1f} {top} V {top+plot_h}" class="grid"/>',
                      _svg_text(x, height - 31, f"{tick}%", text_anchor="middle")])
    for index in range(5):
        scaled = low + (high - low) * index / 4
        value = 10 ** scaled if log_scale else scaled
        y = top + plot_h - index / 4 * plot_h
        label = _num(value) if log_scale else _num(value, 1) + "s"
        parts.extend([f'<path d="M {left} {y:.1f} H {left+plot_w}" class="grid"/>',
                      _svg_text(left - 10, y + 5, label, text_anchor="end")])
    points = []
    for row in rows:
        x = left + row["rate"] * plot_w
        scaled = math.log10(row[field]) if log_scale else row[field]
        y = top + (high - scaled) / (high - low) * plot_h
        points.append((row, x, y))
    # Keep labels in a dedicated right gutter and spread clustered values apart.
    label_positions = []
    previous = top - 30
    for row, x, y in sorted(points, key=lambda point: point[2]):
        label_y = max(y, previous + 30)
        label_positions.append([row, x, y, label_y])
        previous = label_y
    overflow = max(0, label_positions[-1][3] - (top + plot_h - 12))
    for row, x, y, label_y in label_positions:
        label_y -= overflow
        color = colors[row["arm"]]
        if row["cli_basis"]:
            shape = (f'<path d="M {x:.1f} {y-8:.1f} L {x+8:.1f} {y:.1f} '
                     f'L {x:.1f} {y+8:.1f} L {x-8:.1f} {y:.1f} Z" fill="white" '
                     f'stroke="{color}" stroke-width="4"/>')
        else:
            shape = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}" stroke="white" stroke-width="2"/>'
        label_x = left + plot_w + 14
        value_label = _num(row[field]) if log_scale else _num(row[field], 1) + "s"
        parts.append(
            f'<g data-arm="{html.escape(row["arm"])}" data-correctness="{row["rate"]:.4f}" '
            f'data-value="{row[field]:.4f}"><path d="M {x+8:.1f} {y:.1f} L {label_x-5:.1f} '
            f'{label_y:.1f}" stroke="{color}" stroke-width="1" fill="none" opacity=".65"/>'
            + shape
            + _svg_text(label_x, label_y - 3, row["arm"], fill=color, font_weight="700")
            + _svg_text(label_x, label_y + 13, f'{_pct(row["rate"])} · {value_label}',
                        fill="#52606d", font_size="11")
            + "</g>"
        )
    parts.extend([_svg_text(left + plot_w / 2, height - 7, "Correctness (higher →)", text_anchor="middle", font_weight="700"),
                  _svg_text(17, top + plot_h / 2, title, text_anchor="middle", font_weight="700", transform=f"rotate(-90 17 {top + plot_h / 2:.1f})")])
    label = f"{title} by correctness for each harness"
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">'
            f'<title>{html.escape(label)}</title>' + "".join(parts) + "</svg>")


def _charts(model, colors):
    data = _chart_data(model)
    correctness = _correctness_chart(data, colors)
    efficiency = _scatter_chart(data, colors, "total_tokens", "Total tokens / solve incl. cache reads (log scale)", True)
    speed = _scatter_chart(data, colors, "med_wall", "Median wall time")
    return f'''<div class="charts">
<figure><h3>Correctness</h3>{correctness}<figcaption>Bars show solve rate; whiskers show Wilson 95% confidence intervals.</figcaption></figure>
<figure class="hero"><h3>Efficiency</h3>{efficiency}<figcaption>Lower-right is better: more correct and leaner. Diamonds identify cursor and opencode; their token totals are self-reported.</figcaption></figure>
<figure><h3>Speed</h3>{speed}<figcaption>Lower-right is better: more correct with a lower median wall time among solved runs.</figcaption></figure>
</div>'''


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


def render_page(models, methodology, title="OpenBench report", headline=None,
                highlight_arms=None, banner_warnings=None):
    """Render a self-contained HTML report.

    Optional ``highlight_arms`` marks matching arm labels (CSS class
    ``highlight``). Optional ``banner_warnings`` are shown above the first
    section. Both default to off so the release-site flow is unchanged.
    """
    total = sum(a["n"] for m in models for a in m["arms"])
    headline = headline or f"{len(models)} models, {total:,} countable harness × task × trial cells."
    harnesses = sorted({a["arm"] for m in models for a in m["arms"]})
    colors = {harness: PALETTE[index % len(PALETTE)] for index, harness in enumerate(harnesses)}
    highlight = {str(name) for name in (highlight_arms or [])}
    sections = []
    if banner_warnings:
        for message in banner_warnings:
            sections.append(
                '<p class="warning"><strong>Comparability warning:</strong> '
                + html.escape(str(message)) + "</p>"
            )
    for model in models:
        dual = model.get("material_timeouts", any(
            a.get("timeout_count", int(bool(a.get("has_timeout")))) / a.get("n", 1) > MATERIAL_TIMEOUT_RATE
            for a in model["arms"] if a.get("n", 0)
        ))
        heads = ["Arm (harness × model)", "Solved/countable"]
        heads += ["Solve rate @cap", "Solve rate finished"] if dual else ["Solve rate"]
        heads += ["Wilson 95% CI", "Med wall", "Total tokens/solve (incl. cache reads)", "Uncached in/solve",
                  "Output/solve", "Cache-read/solve"]
        if model["has_pricing"]:
            heads.append("$/solve")
        heads.extend(["Token basis", "Excluded / unmatched / duplicates"])
        warning = ""
        if not model["provenance"]["ok"]:
            flags = model["provenance"]["flags"]
            # A real comparability problem is the SAME harness at two CLI
            # versions (harness_version) or a mixed timeout cap. image_digest
            # and harness_version_source alone are rebuild / host-vs-container
            # stamping artifacts — render those as a neutral note, not a red
            # non-comparable banner.
            advisory_only = flags and all(
                flag["field"] in ("image_digest", "harness_version_source")
                for flag in flags
            )
            messages = "; ".join(
                f"{flag['field']} {flag['type'].replace('_', ' ')}" for flag in flags
            )
            if advisory_only:
                warning = ('<p class="tag"><strong>Provenance note:</strong> '
                           + html.escape(messages)
                           + " — a container-rebuild / host-vs-container stamping artifact, "
                           "not a CLI version change. Pinned CLI versions are identical across arms.</p>")
            else:
                warning = ('<p class="warning"><strong>Non-comparable provenance:</strong> '
                           + html.escape(messages) + ". Inspect source rows before publishing.</p>")
        body = []
        for a in model["arms"]:
            values = [f"{a['arm']} × {model['model']}", f"{a['solved']}/{a['n']}"]
            values += [_pct(a["rate"]), _pct(a["finished_rate"])] if dual else [_pct(a["rate"])]
            values += [f"{a['wilson'][0] * 100:.1f}–{a['wilson'][1] * 100:.1f}%",
                       _num(a["med_wall"], 1) + ("s" if a["med_wall"] is not None else ""),
                       _num(a["total_tokens"]), _num(a["tokens_input_uncached"]),
                       _num(a["tokens_output"]), _num(a["tokens_cache_read"])]
            if model["has_pricing"]:
                values.append(
                    "—" if a["cost_per_solve"] is None else f"${a['cost_per_solve']:.3f}"
                )
            values.extend([a["token_basis"],
                           f"{sum(a['excluded'].values())} / {a['unmatched']} / {a['duplicates']}"])
            cells = []
            total_index = heads.index("Total tokens/solve (incl. cache reads)")
            split_indexes = {heads.index(name) for name in
                             ("Uncached in/solve", "Output/solve", "Cache-read/solve")}
            for index, value in enumerate(values):
                classes = []
                if index == total_index:
                    classes.append("total-tokens")
                if a["cli_basis"] and index in split_indexes:
                    classes.append("cli-split")
                css_class = f' class="{" ".join(classes)}"' if classes else ""
                cells.append(f"<td{css_class}>{html.escape(str(value))}</td>")
            color = colors[a["arm"]]
            row_class = " highlight" if a["arm"] in highlight else ""
            body.append(
                f'<tr class="arm-row{row_class}" style="--arm-color:{color}"'
                f' data-arm="{html.escape(a["arm"], quote=True)}">'
                + "".join(cells) + "</tr>"
            )
        head_cells = "".join(
            f'<th class="total-tokens">{html.escape(h)}</th>' if h == "Total tokens/solve (incl. cache reads)"
            else f"<th>{html.escape(h)}</th>" for h in heads)
        timeout_note = ""
        if not dual:
            timeout_arms = [a for a in model["arms"] if a.get("timeout_count", 0)]
            if timeout_arms:
                details = "; ".join(
                    f"{a['arm']}: {a['timeout_count']} timeout{'s' if a['timeout_count'] != 1 else ''}; "
                    f"finished-basis {_pct(a['finished_rate'])}" for a in timeout_arms
                )
                timeout_note = ('<p class="table-note timeout-note"><strong>Timeouts:</strong> '
                                + html.escape(details) + ".</p>")
        token_note = '<p class="table-note token-note">' + html.escape(TOKEN_NOTE) + "</p>"
        sections.append(f'<section><h2>{html.escape(model["title"])}</h2>{warning}' +
                        _charts(model, colors) +
                        '<div class="scroll"><table class="results"><thead><tr>' + head_cells +
                        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>" +
                        timeout_note + token_note + "</section>")
    model_names = [m["model"] for m in models]
    lookup = {(a["arm"], m["model"]): _pct(a["rate"]) for m in models for a in m["arms"]}
    grid_rows = "".join("<tr><th>" + html.escape(h) + "</th>" + "".join(
        f"<td>{html.escape(lookup.get((h, model), '—'))}</td>" for model in model_names) + "</tr>" for h in harnesses)
    grid = '<section><h2>Harness × model correctness</h2><div class="scroll"><table><thead><tr><th>Harness</th>' + "".join(
        f"<th>{html.escape(m)}</th>" for m in model_names) + "</tr></thead><tbody>" + grid_rows + "</tbody></table></div></section>"
    css = """body{font:16px/1.5 system-ui,sans-serif;color:#17202a;max-width:1200px;margin:auto;padding:2rem;background:#f7f8fa}header{padding:2rem;background:#18253b;color:white;border-radius:12px}section{margin:2rem 0;background:white;padding:1.4rem;border-radius:10px;box-shadow:0 1px 4px #ccd}h1{margin:.1rem 0}.scroll{overflow:auto}table,.chart{font-variant-numeric:tabular-nums}table{border-collapse:collapse;width:100%}th,td{padding:.65rem;border-bottom:1px solid #dde2e8;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.results tbody tr{border-left:4px solid var(--arm-color)}.results tbody tr.highlight{background:#fff8e6;box-shadow:inset 0 0 0 1px #e6b800}thead th{background:#edf2f7}.total-tokens{font-weight:800;background:#e6f2f8}.cli-split{color:#7b8794}.table-note{font-size:.82rem;color:#52606d;margin:.65rem .2rem 0;max-width:105ch}.charts{display:grid;gap:1rem;margin:1rem 0 1.5rem}.charts figure{margin:0;border:1px solid #dde2e8;border-radius:8px;padding:.8rem}.charts .hero{border-width:2px;background:#fbfdff}.charts h3{margin:.1rem 0}.chart{width:100%;height:auto}.chart text{font-family:system-ui,sans-serif;font-size:12px;fill:#17202a}.grid{stroke:#d8dee7;stroke-width:1}.whisker{stroke:#17202a;stroke-width:2;fill:none}figcaption,.empty-chart{font-size:.85rem;color:#52606d}.warning{background:#fff1d6;border-left:4px solid #b45309;padding:.75rem}.tag{font-size:.85rem;color:#52606d}footer{color:#52606d;text-align:center}@media(max-width:700px){body{padding:.6rem}section,header{padding:1rem}.charts{grid-template-columns:1fr}.charts .hero{grid-column:auto}}"""
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>" + html.escape(title) + "</title><style>" + css + "</style></head><body><header><div class=\"tag\">OPENBENCH</div><h1>" + html.escape(title) + "</h1><p>" + html.escape(headline) + "</p></header>" + "".join(sections) + grid + '<section id="methodology"><h2>Methodology &amp; limitations</h2>' + _markdown(methodology) + "</section><footer>Generated by OpenBench · static, self-contained HTML</footer></body></html>\n"


def _site_index(releases, community=None, packs=None):
    """Render docs/index.html from releases + optional community/packs lists."""
    cards = []
    for release in reversed(releases):
        models = ", ".join(release["models"])
        cards.append(
            '<li><a href="' + html.escape(release["path"], quote=True) + '"><strong>'
            + html.escape(release["title"]) + "</strong></a><br><time datetime=\""
            + html.escape(release["date"], quote=True) + '\">' + html.escape(release["date"])
            + "</time> · " + html.escape(models) + "</li>"
        )
    listing = "".join(cards) or "<li>No releases published yet.</li>"

    community = list(community or [])
    community_cards = []
    for entry in sorted(
        community,
        key=lambda item: (item.get("date") or "", item.get("id") or ""),
        reverse=True,
    ):
        claim = entry.get("claim") or ""
        submitter = entry.get("submitter") or ""
        title = entry.get("title") or claim or entry.get("id") or "Community submission"
        path = entry.get("path") or ""
        date_s = entry.get("date") or ""
        meta = f'@{html.escape(submitter)}' if submitter else ""
        if claim and claim != title:
            meta = (meta + " · " if meta else "") + html.escape(claim)
        elif not meta:
            meta = html.escape(claim) if claim else ""
        extra = ""
        link = entry.get("link")
        if isinstance(link, str) and link.strip():
            extra = (
                ' · <a href="' + html.escape(link.strip(), quote=True)
                + '">source</a>'
            )
        community_cards.append(
            '<li><a href="' + html.escape(path, quote=True) + '"><strong>'
            + html.escape(title) + "</strong></a><br><time datetime=\""
            + html.escape(date_s, quote=True) + '\">' + html.escape(date_s)
            + "</time>" + ((" · " + meta) if meta else "") + extra + "</li>"
        )
    community_listing = (
        "".join(community_cards) or "<li>No community submissions yet.</li>"
    )

    packs = list(packs or [])
    pack_cards = []
    for entry in sorted(packs, key=lambda item: (item.get("id") or "")):
        pack_id = entry.get("id") or ""
        latest = entry.get("latest") or ""
        kind = entry.get("kind") or "tasks"
        desc = entry.get("description") or ""
        source = entry.get("source") or ""
        sha = entry.get("content_sha256") or ""
        title = f"{pack_id}@{latest}" if latest else pack_id
        source_html = ""
        if isinstance(source, str) and source.strip():
            href = source.strip()
            if href.startswith(("http://", "https://")):
                source_html = (
                    ' · <a href="' + html.escape(href, quote=True)
                    + '">source</a>'
                )
            else:
                source_html = " · <code>" + html.escape(href) + "</code>"
        sha_html = (
            f' · <code>{html.escape(sha[:12])}…</code>' if sha else ""
        )
        pack_cards.append(
            "<li><strong>" + html.escape(title) + "</strong>"
            " · " + html.escape(kind)
            + (("<br>" + html.escape(desc)) if desc else "")
            + "<br><span class=\"tag\">" + html.escape(pack_id)
            + source_html + sha_html + "</span></li>"
        )
    packs_listing = "".join(pack_cards) or "<li>No packs published yet.</li>"

    css = ("body{font:16px/1.5 system-ui,sans-serif;color:#17202a;max-width:800px;margin:auto;"
           "padding:2rem;background:#f7f8fa}header,main{background:white;padding:1.5rem;"
           "border-radius:10px;margin-bottom:1rem}h1,h2{margin:.1rem 0}li{margin:1rem 0}"
           "a{color:#075985}time{color:#52606d}.tag{color:#52606d;font-size:.9rem}"
           ".lead a{font-weight:600}code{font-size:.85em}")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>OpenBench releases</title><style>'
            + css + '</style></head><body><header><div>OPENBENCH</div><h1>Benchmark releases</h1>'
            '<p>Static, reproducible harness comparison reports.</p>'
            '<p class="lead"><a href="leaderboard.html">Leaderboard</a>'
            ' — per-bundle ranks from verified <code>results.jsonl</code> bundles '
            '(not a cross-task global ranking).</p></header><main><ul>' + listing
            + '</ul></main><main id="community"><h2>Community results</h2>'
            '<p class="tag">Third-party publish bundles re-verified by CI '
            '(<code>obench verify</code>). Digests prove tamper-evidence, not '
            'that runs were not cherry-picked.</p><ul>' + community_listing
            + '</ul></main><main id="packs"><h2>Packs</h2>'
            '<p class="tag">Versioned task and harness packs '
            '(<code>obench pack install org/name@version --from …</code>). '
            'Index maintained via <code>obench pack publish-index</code>.</p><ul>'
            + packs_listing + '</ul></main></body></html>\n')


def _validated_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("release date must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("release date must be a valid YYYY-MM-DD date")
    return value


def _temporary_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False)
    try:
        with handle:
            handle.write(content)
        os.chmod(handle.name, 0o644)
        return handle.name
    except BaseException:
        os.unlink(handle.name)
        raise


def build_site(site_dir, release_id, release_date, title, models, page):
    """Add one report while serializing the site's manifest update."""
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("--site-dir requires POSIX file locking") from exc
    os.makedirs(site_dir, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(site_dir, flags)
    try:
        site_stat = os.fstat(lock_fd)
        if (not stat.S_ISDIR(site_stat.st_mode) or site_stat.st_uid != os.geteuid()
                or site_stat.st_mode & 0o022):
            raise ValueError("site directory must be owned by the current user and not group-writable")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _build_site_locked(site_dir, release_id, release_date, title, models, page)
    finally:
        os.close(lock_fd)


def _owned_directory(path):
    """Create or validate a non-symlink directory below the trusted site root."""
    try:
        os.mkdir(path, 0o755)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ValueError(f"unsafe site directory: {path}")


def _build_site_locked(site_dir, release_id, release_date, title, models, page):
    """Publish one release; caller holds the site-level lock."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_id):
        raise ValueError("release id must be a path-safe slug")
    release_date = _validated_date(release_date)
    manifest_path = os.path.join(site_dir, "releases.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            releases = json.load(fh)
        if not isinstance(releases, list):
            raise ValueError("releases.json must contain a JSON list")
    else:
        releases = []
    release_path = f"releases/{release_id}/index.html"
    release = {"id": release_id, "title": title, "date": release_date,
               "models": sorted(m["model"] for m in models), "path": release_path}
    existing = next((entry for entry in releases if entry.get("id") == release_id), None)
    if existing is not None and existing != release:
        raise ValueError(f"release {release_id!r} conflicts with the existing manifest entry")
    if existing is None:
        releases.append(release)
    releases_dir = os.path.join(site_dir, "releases")
    release_dir = os.path.join(releases_dir, release_id)
    _owned_directory(releases_dir)
    _owned_directory(release_dir)
    destination = os.path.join(release_dir, "index.html")
    try:
        with open(destination, encoding="utf-8") as fh:
            if fh.read() != page:
                raise ValueError(f"release artifact conflicts with this build: {destination}")
    except FileNotFoundError:
        pass
    manifest_text = json.dumps(releases, indent=2, ensure_ascii=False) + "\n"
    index_path = os.path.join(site_dir, "index.html")
    community_path = os.path.join(site_dir, "community.json")
    community = []
    if os.path.isfile(community_path):
        with open(community_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, list):
            raise ValueError("community.json must contain a JSON list")
        community = loaded
    packs_path = os.path.join(site_dir, "packs.json")
    packs = []
    if os.path.isfile(packs_path):
        with open(packs_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, list):
            raise ValueError("packs.json must contain a JSON list")
        packs = loaded
    index_text = _site_index(releases, community=community, packs=packs)
    pending = []
    originals = {}
    replaced = []
    try:
        for content, final in ((page, destination),
                               (manifest_text, manifest_path),
                               (index_text, index_path)):
            pending.append((_temporary_text(final, content), final))
        for _, final in pending:
            try:
                with open(final, "rb") as fh:
                    originals[final] = fh.read()
            except FileNotFoundError:
                originals[final] = None
        for temporary, final in pending:
            os.replace(temporary, final)
            replaced.append(final)
    except BaseException:
        for final in reversed(replaced):
            original = originals[final]
            if original is None:
                try:
                    os.unlink(final)
                except FileNotFoundError:
                    pass
            else:
                handle = tempfile.NamedTemporaryFile(dir=os.path.dirname(final), delete=False)
                with handle:
                    handle.write(original)
                os.chmod(handle.name, 0o644)
                os.replace(handle.name, final)
        raise
    finally:
        for temporary, _ in pending:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return destination


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", nargs="*", help="OpenBench result JSONL files")
    output = p.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", help="output HTML path")
    output.add_argument("--site-dir", help="append a release under this static site directory")
    p.add_argument("--release-id", help="path-safe release identifier (required with --site-dir)")
    p.add_argument("--release-date", help="release date (YYYY-MM-DD; defaults from release id)")
    p.add_argument("--title", help="report/release title (overrides config)")
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
    models = assemble_tables(datasets, pricing)
    title = args.title or config.get("title", "OpenBench report")
    page = render_page(models, methodology, title, config.get("headline"))
    if args.site_dir:
        if not args.release_id:
            raise SystemExit("report_page: --release-id is required with --site-dir")
        release_date = args.release_date
        if not release_date and re.match(r"^\d{4}-\d{2}-\d{2}", args.release_id):
            release_date = args.release_id[:10]
        if not release_date:
            raise SystemExit("report_page: --release-date is required when release id has no date")
        build_site(args.site_dir, args.release_id, release_date, title, models, page)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh: fh.write(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
