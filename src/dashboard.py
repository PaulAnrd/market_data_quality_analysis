from __future__ import annotations

import html
from pathlib import Path

import io_utils

EXCEPTION_METRICS = [
    ("structural", "n_duplicate_timestamps", "Duplicate timestamps"),
    ("structural", "n_out_of_order_timestamps", "Out-of-order timestamps"),
    ("structural", "n_crossed_book", "Crossed book ticks (bid >= ask)"),
    ("structural", "n_level_bid_inconsistent", "Level-depth bid inconsistencies"),
    ("structural", "n_level_ask_inconsistent", "Level-depth ask inconsistencies"),
    ("structural", "n_non_positive_sizes", "Non-positive order sizes"),
    ("structural", "n_missing_values", "Missing values"),
    ("continuity", "n_gaps_over_threshold", "Silent gaps over threshold"),
    ("liquidity", "n_negative_spread", "Negative spread ticks"),
    ("liquidity", "n_zero_or_negative_sizes", "Zero-or-negative sizes"),
]


def _esc(x) -> str:
    return html.escape(str(x))


def _num(x, decimals=4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        return f"{x:,.{decimals}g}"
    if isinstance(x, int):
        return f"{x:,}"
    return html.escape(str(x))


def _row(label: str, value: str, note: str = "") -> str:
    note_html = f'<td class="note">{_esc(note)}</td>' if note else "<td></td>"
    return f"<tr><td>{_esc(label)}</td><td class=\"num\">{value}</td>{note_html}</tr>"


def _exception_row(label: str, count: int) -> str:
    if count > 0:
        return (
            f'<tr class="exception"><td>{_esc(label)}</td>'
            f'<td class="num">{_num(count)}</td>'
            f'<td class="note">EXCEPTION</td></tr>'
        )
    return f'<tr><td>{_esc(label)}</td><td class="num">{_num(count)}</td><td></td></tr>'


def _table(headers: list[str], rows: str, num_cols: set[int] = frozenset()) -> str:
    head = "".join(
        f'<th class="num">{_esc(h)}</th>' if i in num_cols else f"<th>{_esc(h)}</th>"
        for i, h in enumerate(headers)
    )
    return f'<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'


def count_exceptions(report: dict) -> tuple[int, int]:
    counts = [report[section][key] for section, key, _ in EXCEPTION_METRICS]
    return sum(1 for c in counts if c > 0), len(counts)


_STYLE = """
  :root {
    --page: #ececea;
    --bg: #ffffff;
    --ink: #111111;
    --ink-secondary: #444444;
    --ink-muted: #767676;
    --border: #cfcfcf;
    --border-strong: #111111;
    --header-bg: #f2f2f2;
    --exception: #a30000;
  }
  * { box-sizing: border-box; }
  html { background: var(--page); }
  body {
    margin: 24px auto; padding: 32px 48px 64px; max-width: 1400px;
    background: var(--bg); color: var(--ink);
    border: 1px solid var(--border);
    font-family: Arial, Helvetica, system-ui, sans-serif;
    font-size: 13px; line-height: 1.5;
  }
  header.report-header {
    border-bottom: 3px solid var(--border-strong);
    padding-bottom: 12px; margin-bottom: 24px;
  }
  header.report-header h1 {
    font-size: 1.25rem; margin: 0 0 2px; letter-spacing: 0.01em;
  }
  header.report-header .tag {
    color: var(--ink-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em;
  }
  h2 {
    font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--ink); margin: 0 0 8px; padding-bottom: 4px;
    border-bottom: 1px solid var(--border-strong);
  }
  section { margin-bottom: 28px; }
  .section-note { color: var(--ink-muted); font-size: 0.8rem; margin: -2px 0 10px; font-style: italic; }
  table { border-collapse: collapse; width: 100%; }
  th, td {
    text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--border);
    font-variant-numeric: tabular-nums;
  }
  th {
    background: var(--header-bg); color: var(--ink-secondary);
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border-strong);
  }
  th.num, td.num { text-align: right; white-space: nowrap; }
  td.note { color: var(--ink-muted); font-size: 0.85rem; }
  tr.exception td { color: var(--exception); font-weight: 700; }
  tr.exception td.note { color: var(--exception); }
  .grid-2 { display: grid; grid-template-columns: 1.6fr 1fr; gap: 24px; align-items: start; }
  .gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 16px; }
  figure { margin: 0; border: 1px solid var(--border); padding: 8px; }
  figure img { width: 100%; height: auto; display: block; }
  figcaption {
    font-size: 0.78rem; color: var(--ink-secondary); margin-top: 6px;
    text-align: center; text-transform: uppercase; letter-spacing: 0.03em;
  }
  footer {
    margin-top: 40px; padding-top: 10px; border-top: 1px solid var(--border);
    color: var(--ink-muted); font-size: 0.75rem;
  }
"""


def render_dashboard(report: dict, plot_paths: list[str]) -> str:
    s, c, r, l = report["structural"], report["continuity"], report["returns"], report["liquidity"]
    pl = report["price_level"]

    n_exceptions, n_checks = count_exceptions(report)

    summary_rows = "".join([
        _row("Source file", report["source_file"]),
        _row("Report generated (UTC)", report["generated_at"].replace("T", " ").replace("+00:00", "")),
        _row("Total ticks", _num(s["n_rows"])),
        _row("Data-integrity exceptions", f"{n_exceptions} of {n_checks} checks"),
    ])

    structural_rows = "".join([
        _row("Rows processed", _num(s["n_rows"])),
        _row("Order-book levels present", _num(s["n_levels_present"])),
        _exception_row("Duplicate timestamps", s["n_duplicate_timestamps"]),
        _exception_row("Out-of-order timestamps", s["n_out_of_order_timestamps"]),
        _exception_row("Crossed book ticks (bid >= ask)", s["n_crossed_book"]),
        _exception_row("Level-depth bid inconsistencies (any level > shallower level)", s["n_level_bid_inconsistent"]),
        _exception_row("Level-depth ask inconsistencies (any level < shallower level)", s["n_level_ask_inconsistent"]),
        _exception_row("Non-positive order sizes", s["n_non_positive_sizes"]),
        _exception_row("Missing values", s["n_missing_values"]),
    ])

    continuity_rows = "".join([
        _row("Mean inter-arrival time", f"{c['mean_inter_arrival_s']:.3f} s"),
        _row("Median inter-arrival time", f"{c['median_inter_arrival_s']:.3f} s"),
        _row("P95 inter-arrival time", f"{c['p95_inter_arrival_s']:.3f} s"),
        _row("Max inter-arrival time", f"{c['max_inter_arrival_s']:.3f} s"),
        _row("Gap threshold", f"{c['gap_threshold_s']:.0f} s"),
        _exception_row("Gaps over threshold", c["n_gaps_over_threshold"]),
    ])

    gap_rows = "".join(
        f"<tr><td>{_esc(g['timestamp'])}</td><td class=\"num\">{g['gap_s']:.1f} s</td></tr>"
        for g in c["top_gaps"]
    ) or '<tr><td colspan="2">None</td></tr>'

    def interp(flag, pos_label, neg_label):
        return pos_label if flag else neg_label

    price_level_rows = "".join([
        f'<tr><td>Start / end price</td><td class="num">{_num(pl["start"])} / {_num(pl["end"])}</td><td></td><td></td></tr>',
        f'<tr><td>Min / max price</td><td class="num">{_num(pl["min"])} / {_num(pl["max"])}</td><td></td><td></td></tr>',
        f'<tr><td>Mean / std. dev.</td><td class="num">{_num(pl["mean"])} / {_num(pl["std"])}</td><td></td><td></td></tr>',
        f'<tr><td>Augmented Dickey-Fuller (stationarity)</td><td class="num">{_num(pl["adf_stat"])}</td>'
        f'<td class="num">{_num(pl["adf_pvalue"])}</td>'
        f'<td class="note">{interp(pl["adf_stationary"], "Stationary", "Non-stationary (expected)")}</td></tr>',
        f'<tr><td>KPSS (stationarity)</td><td class="num">{_num(pl["kpss_stat"])}</td>'
        f'<td class="num">{_num(pl["kpss_pvalue"])}</td>'
        f'<td class="note">{interp(pl["kpss_stationary"], "Stationary", "Non-stationary (expected)")}</td></tr>',
    ])

    returns_rows = "".join([
        f'<tr><td>Distribution shape</td><td class="num">skew {_num(r["skew"])}, '
        f'excess kurtosis {_num(r["kurtosis_excess"])}</td><td></td><td></td></tr>',
        f'<tr><td>Jarque-Bera (normality)</td><td class="num">{_num(r["jarque_bera_stat"])}</td>'
        f'<td class="num">{_num(r["jarque_bera_pvalue"])}</td>'
        f'<td class="note">{interp(r["jarque_bera_normal"], "Consistent with normal", "Rejects normality")}</td></tr>',
        f'<tr><td>Ljung-Box (return autocorrelation, lag {r["ljung_box_lags"]})</td>'
        f'<td class="num">{_num(r["ljung_box_stat"])}</td><td class="num">{_num(r["ljung_box_pvalue"])}</td>'
        f'<td class="note">{interp(r["ljung_box_autocorrelated"], "Autocorrelation present", "No autocorrelation")}</td></tr>',
        f'<tr><td>Ljung-Box (squared returns, lag {r["ljung_box_lags"]})</td>'
        f'<td class="num">{_num(r["ljung_box_sq_stat"])}</td><td class="num">{_num(r["ljung_box_sq_pvalue"])}</td>'
        f'<td class="note">{interp(r["ljung_box_sq_vol_clustering"], "Volatility clustering present", "No volatility clustering")}</td></tr>',
        f'<tr><td>Augmented Dickey-Fuller (stationarity)</td><td class="num">{_num(r["adf_stat"])}</td>'
        f'<td class="num">{_num(r["adf_pvalue"])}</td>'
        f'<td class="note">{interp(r["adf_stationary"], "Stationary", "Non-stationary")}</td></tr>',
        f'<tr><td>KPSS (stationarity)</td><td class="num">{_num(r["kpss_stat"])}</td>'
        f'<td class="num">{_num(r["kpss_pvalue"])}</td>'
        f'<td class="note">{interp(r["kpss_stationary"], "Stationary", "Non-stationary")}</td></tr>',
        f'<tr><td>Rolling Hampel outlier scan (window {r["outlier_hampel_window"]}, z &gt; {r["outlier_hampel_threshold"]})</td>'
        f'<td class="num">{_num(r["n_outliers_hampel"])} of {_num(r["n_returns"])} ticks</td><td></td><td></td></tr>',
    ])

    liquidity_rows = "".join([
        _row("Mean spread", _num(l["spread_mean"])),
        _row("Median spread", _num(l["spread_median"])),
        _row("Spread std. dev.", _num(l["spread_std"])),
        _row("Spread range (min / max)", f"{_num(l['spread_min'])} / {_num(l['spread_max'])}"),
        _exception_row("Negative spread ticks", l["n_negative_spread"]),
        _exception_row("Zero-or-negative sizes", l["n_zero_or_negative_sizes"]),
        _row("Median L1 bid / ask size", f"{l['bid_size_1_median']:,.0f} / {l['ask_size_1_median']:,.0f}"),
    ])

    plot_titles = {
        "01_price_timeseries.png": "Mid price with bid/ask band",
        "02_spread_timeseries.png": "Spread over time",
        "03_interarrival_hist.png": "Inter-arrival time distribution",
        "04_return_hist_qq.png": "Return distribution & Q-Q plot",
        "05_acf_returns.png": "Autocorrelation (returns & squared returns)",
        "06_activity_profile.png": "Tick activity per minute",
    }
    gallery = "".join(
        f'<figure><img src="{_esc(p)}" alt="{_esc(plot_titles.get(Path(p).name, Path(p).name))}">'
        f'<figcaption>{_esc(plot_titles.get(Path(p).name, Path(p).name))}</figcaption></figure>'
        for p in plot_paths
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Data Quality Report -- {_esc(report['source_file'])}</title>
<style>
{_STYLE}
</style>
</head>
<body>
  <header class="report-header">
    <h1>Market Data Quality Report</h1>
    <div class="tag">Historical recording usability assessment</div>
  </header>

  <section>
    <h2>Summary</h2>
    {_table(["Field", "Value", ""], summary_rows, num_cols={1})}
  </section>

  <div class="grid-2">
    <section>
      <h2>Structural Integrity</h2>
      <p class="section-note">Data-integrity checks. Any non-zero count is flagged as an exception.</p>
      {_table(["Check", "Count", "Status"], structural_rows, num_cols={1})}
    </section>
    <section>
      <h2>Largest Gaps</h2>
      {_table(["Timestamp", "Gap"], gap_rows, num_cols={1})}
    </section>
  </div>

  <section>
    <h2>Coverage &amp; Continuity</h2>
    {_table(["Metric", "Value", "Status"], continuity_rows, num_cols={1})}
  </section>

  <section>
    <h2>Price Level (Mid)</h2>
    <p class="section-note">A real traded price is expected to fail stationarity (behave like a random walk) -- that's why the tests below run on returns, not the level. A level that tests as stationary is itself a signal worth investigating (e.g. a stuck feed). Significance level (alpha) = {r['significance_level']:.0%}.</p>
    {_table(["Test", "Statistic", "p-value", "Result"], price_level_rows, num_cols={1, 2})}
  </section>

  <section>
    <h2>Return Statistics</h2>
    <p class="section-note">Test outcomes are descriptive, not exceptions -- fat tails and volatility clustering are expected in financial returns. Significance level (alpha) = {r['significance_level']:.0%}.</p>
    {_table(["Test", "Statistic", "p-value", "Result"], returns_rows, num_cols={1, 2})}
  </section>

  <section>
    <h2>Spread &amp; Liquidity</h2>
    {_table(["Metric", "Value", "Status"], liquidity_rows, num_cols={1})}
  </section>

  <section>
    <h2>Visualizations</h2>
    <div class="gallery">{gallery}</div>
  </section>

  <footer>Generated by market_data_statistical_quality -- {_esc(report['generated_at'])}</footer>
</body>
</html>
"""


def write_dashboard(report: dict, plot_paths: list[str], out_path: Path) -> Path:
    io_utils.atomic_write_text(out_path, render_dashboard(report, plot_paths))
    return out_path


def render_comparison_dashboard(report: dict, plot_paths: list[str]) -> str:
    has_metadata = any("metadata" in s for s in report["sources"])

    def rows_cell(s: dict) -> str:
        if "raw_rows" in s:
            return f'<td class="num">{_num(s["n_rows"])} (downsampled from {_num(s["raw_rows"])})</td>'
        return f'<td class="num">{_num(s["n_rows"])}</td>'

    if has_metadata:
        source_headers = ["Source", "File", "Market", "Server", "Feed", "Rows", "Session start", "Session end"]
        source_num_cols = {5}
        source_rows = "".join(
            f'<tr><td>{_esc(s["source"])}</td><td>{_esc(s.get("file", ""))}</td>'
            f'<td>{_esc(s["metadata"]["market"])}</td><td>{_esc(s["metadata"]["server"])}</td>'
            f'<td>{_esc(s["metadata"]["source"])}</td>'
            f'{rows_cell(s)}'
            f'<td>{_esc(s["session_start"].replace("T", " "))}</td>'
            f'<td>{_esc(s["session_end"].replace("T", " "))}</td></tr>'
            for s in report["sources"]
        )
    else:
        source_headers = ["Source", "File", "Rows", "Session start", "Session end"]
        source_num_cols = {2}
        source_rows = "".join(
            f'<tr><td>{_esc(s["source"])}</td><td>{_esc(s.get("file", ""))}</td>'
            f'{rows_cell(s)}'
            f'<td>{_esc(s["session_start"].replace("T", " "))}</td>'
            f'<td>{_esc(s["session_end"].replace("T", " "))}</td></tr>'
            for s in report["sources"]
        )

    summary_rows = []
    if "status" in report:
        css_class = ' class="exception"' if report["status"] == "KO" else ""
        summary_rows.append(
            f'<tr{css_class}><td>Status</td><td class="num">{_esc(report["status"])}</td><td></td></tr>'
        )
    if "instrument" in report:
        summary_rows.append(_row("Instrument (date / product)",
                                  f"{report['instrument']['date']} / {_esc(report['instrument']['product'])}"))
    summary_rows.extend([
        _row("Report generated (UTC)", report["generated_at"].replace("T", " ").replace("+00:00", "")),
        _row("Sources compared", _num(len(report["sources"]))),
        _row("Alignment grid step", f"{report['alignment_freq_s']:g} s"),
        _row("Max staleness (as-of tolerance)", f"{report['max_staleness_s']:g} s"),
        _row("Divergence threshold", f"{report['price_diff_threshold_bps']:g} bps"),
        _row("Overlapping window", f"{report['window_start'].replace('T', ' ')} -> {report['window_end'].replace('T', ' ')}"),
        _row("Grid points in overlap", _num(report["n_grid_points"])),
    ])
    summary_rows = "".join(summary_rows)

    overlay_plot = next((p for p in plot_paths if Path(p).name == "c1_price_overlay.png"), None)
    overlay_html = (
        f'<figure><img src="{_esc(overlay_plot)}" alt="Mid price by source">'
        f'<figcaption>Mid price by source (aligned grid)</figcaption></figure>'
        if overlay_plot else ""
    )

    def pair_section(pair: dict) -> str:
        a, b = pair["source_a"], pair["source_b"]
        if pair["n_common_points"] < 2:
            body = (
                f'<p class="section-note">Fewer than 2 grid points with a current quote from both '
                f'sources (coverage {pair["coverage"]:.1%}) -- not enough overlap to compare.</p>'
            )
            divergence_html = ""
        else:
            stat_rows = "".join([
                _row("Common grid points", f"{_num(pair['n_common_points'])} of {_num(pair['n_grid_points'])} ({pair['coverage']:.1%})"),
                _row("Mean diff", _num(pair["mean_diff"])),
                _row("Mean / median abs. diff", f"{_num(pair['mean_abs_diff'])} / {_num(pair['median_abs_diff'])}"),
                _row("RMS diff", _num(pair["rms_diff"])),
                _row("Max abs. diff", _num(pair["max_abs_diff"])),
                _row("Mean / max abs. diff (bps)", f"{_num(pair['mean_abs_diff_bps'])} / {_num(pair['max_abs_diff_bps'])}"),
                _row("Return correlation", _num(pair["return_correlation"])),
                _exception_row("Divergences over threshold", pair["n_divergences"]),
            ])
            body = _table(["Metric", "Value", "Status"], stat_rows, num_cols={1})

            div_rows = "".join(
                f'<tr><td>{_esc(d["timestamp"])}</td><td class="num">{_num(d["mid_a"])}</td>'
                f'<td class="num">{_num(d["mid_b"])}</td><td class="num">{_num(d["diff_bps"])}</td></tr>'
                for d in pair["top_divergences"]
            ) or '<tr><td colspan="4">None</td></tr>'
            divergence_html = (
                f'<h3 style="font-size:0.78rem;text-transform:uppercase;letter-spacing:0.04em;'
                f'color:var(--ink-secondary);margin:16px 0 8px;">Largest divergences</h3>'
                f'{_table(["Timestamp", f"Mid ({a})", f"Mid ({b})", "Diff (bps)"], div_rows, num_cols={1, 2, 3})}'
            )

        pair_plots = "".join(
            f'<figure><img src="{_esc(p)}" alt="{_esc(Path(p).stem)}"></figure>'
            for p in plot_paths
            if Path(p).name in (f"c2_diff_{a}_vs_{b}.png", f"c3_diff_hist_{a}_vs_{b}.png")
        )

        return f"""
  <section>
    <h2>{_esc(a)} vs {_esc(b)}</h2>
    {body}
    {divergence_html}
    <div class="gallery">{pair_plots}</div>
  </section>"""

    pair_sections = "".join(pair_section(p) for p in report["pairs"])

    attribution_html = ""
    if "attribution" in report:
        ranking = report["attribution"]["source_outlier_ranking"]
        rank_rows = "".join(
            f'<tr><td>{_esc(r["source"])}</td><td class="num">{_num(r["avg_diff_vs_others_bps"])}</td>'
            f'<td class="num">{_num(r["n_pairs"])}</td></tr>'
            for r in ranking
        ) or '<tr><td colspan="3">Not enough overlapping data in any pair</td></tr>'

        dims = report["attribution"]["dimension_attribution"]
        dim_rows = "".join(
            f'<tr><td>{_esc(d["dimension"])}</td><td>{_esc(", ".join(d["distinct_values"]))}</td>'
            f'<td class="num">{_num(d["avg_diff_bps_when_differs"])}</td>'
            f'<td class="num">{_num(d["avg_diff_bps_when_matches"])}</td>'
            f'<td class="num">{_num(d["n_pairs_differs"])} / {_num(d["n_pairs_matches"])}</td></tr>'
            for d in dims
        ) or '<tr><td colspan="5">Every recording shares the same market/server/source, or too few pairs to contrast</td></tr>'

        attribution_html = f"""
  <section>
    <h2>Divergence Attribution</h2>
    <p class="section-note">Descriptive cross-referencing, not a verdict -- with few recordings a
      dimension can be confounded with another (e.g. if a server is only ever paired with one
      source in this group, the two can't be told apart from pairwise data alone).</p>
    <h3 style="font-size:0.78rem;text-transform:uppercase;letter-spacing:0.04em;color:var(--ink-secondary);margin:0 0 8px;">Which recording disagrees most with the rest</h3>
    {_table(["Source", "Avg. diff vs. others (bps)", "Pairs"], rank_rows, num_cols={1, 2})}
    <h3 style="font-size:0.78rem;text-transform:uppercase;letter-spacing:0.04em;color:var(--ink-secondary);margin:16px 0 8px;">Divergence by dimension</h3>
    {_table(["Dimension", "Distinct values", "Avg. diff when differs (bps)", "Avg. diff when matches (bps)", "Pairs (differs/matches)"], dim_rows, num_cols={2, 3, 4})}
  </section>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feed Comparison Report{" -- " + _esc(report["instrument"]["product"]) if "instrument" in report else ""}</title>
<style>
{_STYLE}
</style>
</head>
<body>
  <header class="report-header">
    <h1>Feed Comparison Report{" -- " + _esc(report["instrument"]["product"]) if "instrument" in report else ""}</h1>
    <div class="tag">Cross-source time-series comparison, same instrument{
      " (" + _esc(report["instrument"]["date"]) + ")" if "instrument" in report else ""
    }</div>
  </header>

  <section>
    <h2>Summary</h2>
    {_table(["Field", "Value", ""], summary_rows, num_cols={1})}
  </section>

  <section>
    <h2>Sources</h2>
    {_table(source_headers, source_rows, num_cols=source_num_cols)}
  </section>

  <section>
    <h2>Price Overlay</h2>
    <div class="gallery">{overlay_html}</div>
  </section>
  {attribution_html}
  {pair_sections}

  <footer>Generated by market_data_statistical_quality -- {_esc(report['generated_at'])}</footer>
</body>
</html>
"""


def write_comparison_dashboard(report: dict, plot_paths: list[str], out_path: Path) -> Path:
    io_utils.atomic_write_text(out_path, render_comparison_dashboard(report, plot_paths))
    return out_path
