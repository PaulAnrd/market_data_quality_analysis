# market_data_statistical_quality

Statistical usability assessment for historical market data recordings (L2+
quotes) before they get fed into quant models. Given CSV/TXT recordings of
best-bid/ask quotes, it runs structural and statistical checks and produces
a JSON report, matplotlib plots, and a static HTML dashboard per recording.
When the same instrument was recorded by more than one feed, it can also
cross-compare those recordings and flag where they diverge.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```
./mdsq.py path/to/config.ini
```

(or `.venv/bin/python mdsq.py path/to/config.ini`, if the venv isn't
activated in your shell).

Everything -- what to run and every tunable parameter -- lives in one
config file. Start from `src/config.ini` as a template and set the
`[run]` section:

```ini
[run]
report_inputs = data/20260906APPL*
compare_inputs = data/20260906*
report_dir = report
```

- `report_inputs` -- glob pattern(s) (comma-separated) of recordings to run
  the per-file quality assessment on. Each is processed independently;
  one bad file doesn't stop the rest.
- `compare_inputs` -- glob pattern(s) for cross-source comparison. If every
  matched filename starts with `YYYYMMDD_product` (optionally followed by
  up to 3 more `_`-separated tokens -- market, server, source, in that
  order, e.g. `YYYYMMDD_product_market_server_source.txt`), inputs are
  auto-grouped by `(date, product)` and one comparison is run per
  instrument, named `<date>_<product>` (e.g. `20260907_EUR`), however many
  files exist for it that day. The trailing tokens are only used to tell
  same-day-same-product files apart and, when all 3 are present, to
  attribute a divergence to market/server/source (see below) -- omit
  whichever don't apply to your feeds. Otherwise (no matched filename has
  a leading date), all matched files are compared together as one
  instrument (needs 2+ files either way).
- `report_dir` -- where all output goes.

Leave either `report_inputs` or `compare_inputs` empty to skip that phase;
a run does both if both are set. Glob patterns are resolved relative to the
current working directory, same as a shell glob; a bare directory is
expanded to the `.csv`/`.txt` files directly inside it.

Exit codes: `0` if everything that ran succeeded, `1` if anything failed,
`2` for a usage/config error (bad CLI args, invalid config). A recording's
own data-integrity exceptions are normal output, not a run failure -- check
the JSON/dashboard for those.

## Output

For an input file `data/<name>.txt`, `report_inputs` writes to
`report_dir/<name>/`:

- `quality_report.json` -- all KPIs and statistical test results
- `plots/*.png`
- `dashboard.html` -- open directly in a browser
- `cleaned_data.csv` -- only when `config.ini [outliers] replacement_mode`
  is non-zero (see "Outlier treatment" below)

`compare_inputs` writes to `report_dir/<date>_<product>/` per instrument
when auto-grouped, or `report_dir/compare_<UTC-timestamp>/` when it isn't
(no filename had a leading date to name it by, so each such run gets its
own timestamped directory rather than overwriting a shared one):

- `comparison_report.json` -- per-source-pair diff stats (coverage, mean/
  median/max/RMS price difference, return correlation, largest divergences),
  a `status` of `"OK"`/`"KO"` (see below), and, when auto-grouped, an
  `attribution` section pointing at which of market/server/source the
  disagreement traces back to
- `plots/*.png` -- mid price overlaid by source, plus a difference plot and
  histogram per source pair
- `comparison_dashboard.html`

**`status`** is the one aggregate pass/fail verdict this tool computes
anywhere: `"KO"` if any source pair had a divergence over
`price_diff_threshold_bps`, or didn't even have enough time overlap to be
compared; `"OK"` otherwise. (`-report`'s checks stay purely descriptive --
see "Checks" below -- since a single verdict would misrepresent them, but a
divergence count is already a per-pair threshold, so collapsing it to one
flag here is a direct summary, not a new judgment call.)

**`report_dir/json/`** additionally gets a flat copy of every JSON report --
`<name>.json`, `<date>_<product>.json`, or `compare_<UTC-timestamp>.json` --
meant for an
external dashboard (e.g. Grafana via a JSON-file datasource) to scan across
runs without walking the per-recording directory structure. A run that
fails outright still writes `{"status": "ERROR", "generated_at": ...,
"error": "<message>"}` there, so a crash doesn't just leave the previous
successful run's export looking current.

**`report_dir/json/index.json`** is a single flat array summarizing every
run (one row per `(name, kind)`, upserted and re-sorted by `generated_at`
each time that name reruns, capped at the 2000 most recent rows). This is
what an external dashboard should actually query -- one file, one request,
covers every report and comparison ever produced -- rather than reading
each `<name>.json` individually. Each row:

```json
{
  "name": "20260906_EURUSD",
  "kind": "compare",
  "status": "KO",
  "generated_at": "2026-09-06T18:00:03+00:00",
  "date": "20260906",
  "product": "EURUSD",
  "n_sources": 2,
  "n_pairs": 1,
  "n_divergences": 3,
  "max_abs_diff_bps": 10.93,
  "n_exceptions": null,
  "n_checks": null,
  "error": null
}
```

- `kind` is `"report"` or `"compare"`.
- `status` is only populated for `kind: "compare"` (`"OK"`/`"KO"`) or a
  failed run of either kind (`"ERROR"`, with `error` set); a successful
  report row leaves `status` `null` since `-report` computes no verdict --
  see `n_exceptions`/`n_checks` instead.
- `date`/`product` are only set for auto-grouped comparisons.
- Fields that don't apply to a given row (e.g. `n_pairs` on a report row)
  are `null` rather than omitted, so every row has the same shape.

Set `config.ini [general] grafana_export_dir` to also copy every one of
those flat exports to a second, fixed path (e.g. a shared VM folder your
Grafana host can read) regardless of `report_dir`. A write failure there is
only logged as a warning -- it never fails the run, since the recording was
still genuinely checked; only the extra copy didn't land.

## Grafana dashboard

`grafana/mdsq_dashboard.json` is a ready-to-import dashboard reading
`index.json`: a top row of KPI stats (comparisons OK/KO, failed runs,
total divergences, last run time) for a daily glance, plus two detail
tables below it (all comparisons, all per-file reports), most recent
first, with `status`/`n_exceptions` cells colored.

Setup:

1. Point `config.ini [general] grafana_export_dir` at a path your Grafana
   host can read (e.g. a shared VM folder) -- this is what keeps
   `index.json` there regardless of `report_dir`.
2. On the Grafana host: `grafana-cli plugins install
   yesoreyeram-infinity-datasource`, restart Grafana.
3. Add an Infinity datasource. Under its network/security settings, allow
   local file access and allowlist the directory from step 1 (exact field
   name varies by plugin version -- look for "Allowed hosts"/"Allowed
   paths" for local files).
4. In Grafana, Dashboards -> New -> Import, upload
   `grafana/mdsq_dashboard.json`, and select that Infinity datasource
   when prompted.
5. Every panel's query URL is the `$index_url` dashboard variable, not a
   hardcoded path -- set it once via Dashboard settings -> Variables (or
   the variable dropdown at the top of the dashboard) to wherever
   `index.json` actually lands: `report_dir/json/index.json` if you didn't
   set `grafana_export_dir`, or the `grafana_export_dir` copy of it
   otherwise (e.g. `file:///mnt/shared/MDSQ/index.json` if
   `grafana_export_dir = /mnt/shared/MDSQ`, or
   `file:///mnt/shared/MDSQ/data/json/index.json` if `report_dir = data`
   and `grafana_export_dir` is unset but `data/` itself is on the shared
   mount).

The dashboard's queries pull the full `index.json` and filter/aggregate
with Grafana's own transformations (not plugin-specific filter syntax),
so they should survive Infinity plugin upgrades; if a panel comes back
empty after import, open its query editor and re-run it once -- some
plugin versions don't perfectly restore query state from imported JSON.

## Input format

CSV with a header row. Canonically:

```
timestamp, best_bid_1, best_ask_1, best_bid_size_1, best_ask_size_1,
best_bid_2, best_ask_2, best_bid_size_2, best_ask_size_2, ...
```

Only `timestamp` and level 1 are mandatory; deeper levels (up to 7) are
optional. Header names and depth are both configurable in `config.ini
[columns]`, no code changes:

- Map each mandatory field to whatever your CSV actually calls it. Each
  accepts a comma-separated list of alternatives (e.g.
  `best_bid_1 = best_bid_1, BEST_BID_1, RT_22`), for when different
  files/vendors label the same field differently -- a given file must match
  exactly one alternative.
- `max_levels` plus `{level}`-templated column names (e.g.
  `best_bid_level_template = best_bid_{level}`) control how many levels
  beyond 1 to look for. A level missing entirely from a file is fine; one
  that's *partially* present (e.g. a bid column with no matching ask) is
  rejected as malformed.

Sample recordings in `data/`:

- `sample_market_data.txt` -- deliberately injected defects (crossed book,
  negative spread, a price spike, a feed outage) exercising every check.
- `synthetic_eurusd_quotes.txt` / `synthetic_eurusd_quotes_feedB.txt` -- the
  same instrument/session as two vendors, the second derived from the first
  with its own timing, noise, and a few injected divergences. Regenerate or
  vary with `.venv/bin/python src/generate_synthetic.py`.

## Configuration

Everything below lives in the config file passed to `mdsq.py`
(`src/config.ini` as a template):

- `[run]` -- what to run; see "Usage" above
- `[general]` -- minimum row count to run at all, process memory budget
  (GB) and file-size overhead multiplier for the oversized-file protection,
  `grafana_export_dir`
- `[columns]` -- CSV header mapping and book depth; see "Input format"
- `[logging]` -- days of rotated `log/pipeline.log` to keep
- `[continuity]` -- silent-gap threshold, how many gaps to list
- `[returns]` -- significance level for every hypothesis test, Ljung-Box
  lag count, the time-normalization floor quantile
- `[outliers]` -- Hampel filter window/threshold, `replacement_mode` (see
  "Outlier treatment")
- `[comparison]` -- alignment grid step, as-of staleness tolerance,
  divergence threshold, `max_sources`, `max_grid_points`, and downsampling
  (`downsample_points_per_day`/`downsample_min_rows`) for multi-million-tick
  recordings -- thins a source to roughly that many points/day before
  comparing, widening the staleness tolerance to match so the thinned
  source's sparser updates aren't mistaken for missing data

## Checks

**Structural** -- timestamp duplicates/out-of-order, crossed book, level-depth
consistency (bid/ask should widen monotonically with depth), non-positive
sizes, missing values. Any non-zero count is an exception.

**Coverage & continuity** -- inter-arrival time distribution, silent gaps
above threshold.

**Price level** -- descriptive stats plus ADF/KPSS stationarity on the raw
mid-price. A real traded price is expected to fail stationarity; one that
tests as stationary is itself a signal (e.g. a stuck feed).

**Returns** -- on the mid-price return series, time-normalized by `sqrt(dt)`:
skew/kurtosis, Jarque-Bera normality, Ljung-Box autocorrelation (on returns
and squared returns), ADF/KPSS stationarity, and a rolling Hampel filter for
tick-level outliers. These are descriptive, not exceptions -- fat tails and
volatility clustering are normal in real returns.

**Liquidity** -- spread sign/sanity, zero-or-negative sizes (exceptions),
spread and size distributions.

`-report` computes no single pass/fail verdict -- the dashboard rolls up the
count of data-integrity exceptions so a reviewer can triage quickly, but the
judgment call on usability is left to the reviewer.

## Outlier treatment

`src/outlier_treatment.py` can correct ticks the Hampel filter flags. Opt-in,
off by default (`config.ini [outliers] replacement_mode = 0`) -- the quality
report always scores the recording as it actually is. Modes:

| Mode | Name | Behavior |
|---|---|---|
| 0 | none | no-op (default) |
| 1 | nan | flagged tick's quote becomes missing |
| 2 | interpolate | linear interpolation between nearest non-flagged ticks |
| 3 | ffill | carry forward the last non-flagged quote (no lookahead) |
| 4 | rolling_median | replace with the local rolling median |

Correction shifts the whole quote snapshot (every level's bid/ask) by the
same delta as `mid`, so it survives re-enrichment on reload; sizes are
untouched. `cleaned_data.csv` contains only the raw schema, so it can be
re-fed into a `report_inputs` pattern like any other recording.

Standalone use, independent of the main pipeline:

```
.venv/bin/python src/outlier_treatment.py <input.csv> --mode 2 -o cleaned.csv
```

## Running in production

Intended to run unattended on a schedule (e.g. a daily cron job against a
data drop):

- Every run logs to `log/pipeline.log` (rotated at midnight, 30 days kept by
  default) as well as the console.
- Input validation (schema, minimum row count) happens before any
  statistical test runs, so bad input fails fast with a clear message.
- **Oversized-file protection** (`src/memory_guard.py`): a pre-flight
  file-size check rejects a file outright if `size x
  memory_overhead_multiplier` would exceed `max_memory_budget_gb`; an OS
  `RLIMIT_AS` cap backs this up where the platform supports it (not
  guaranteed everywhere -- e.g. not on macOS); a runtime watchdog checks
  actual process memory at each pipeline checkpoint regardless. None of
  this makes arbitrarily large files analyzable -- a file needing more
  memory than the budget is rejected, not streamed. Also set an
  OS/container-level memory limit as the outermost layer in production.
- `quality_report.json`, `dashboard.html`, and `cleaned_data.csv` are all
  written via a temp file + rename, so a process killed mid-write never
  leaves a truncated file behind.

Example crontab entry (daily at 06:00, from the project root):

```
0 6 * * * cd /path/to/market_data_statistical_quality && ./mdsq.py config/daily.ini >> /dev/null 2>&1
```

(stdout/stderr can be discarded since everything is already in
`log/pipeline.log`.)

## Project layout

```
data/                 input recordings
grafana/              mdsq_dashboard.json -- importable Grafana dashboard
report/<name>/          per-file report, plots, dashboard
report/<date>_<product>/ auto-grouped comparison output, one per instrument
report/compare_<ts>/       non-grouped comparison output (no leading date in filenames)
report/json/                flat JSON exports for an external dashboard
log/                          daily-rotating pipeline log (gitignored)
mdsq.py                     entry point: ./mdsq.py <config-file>
src/
  config.ini         tunable parameters + [run] (see Configuration above)
  config.py           loads the config file (path overridable via
                       MDSQ_CONFIG_PATH, set by mdsq.py)
  logging_setup.py     configures logging to log/ + console
  io_utils.py           atomic file writes
  memory_guard.py        oversized-file protection
  loader.py                column mapping/depth, load + enrich
  structural.py            timestamp / book-integrity checks
  continuity.py             inter-arrival / gap analysis
  returns_stats.py          price-level & return statistics
  outliers.py                rolling Hampel filter
  outlier_treatment.py        opt-in correction of flagged ticks
  liquidity.py                 spread / size checks
  compare.py                    cross-source comparison
  filename_metadata.py           parses YYYYMMDD_product[_market[_server[_source]]].txt
  plots.py                        matplotlib visualizations
  dashboard.py                     static HTML report renderer
  generate_synthetic.py             synthetic reference data generator
  main.py                            pipeline implementation, called by mdsq.py
```
