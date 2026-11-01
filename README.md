# mdsq -- market data statistical quality

`mdsq` assesses whether a recorded market data file (level-1 or deeper
quotes) is statistically fit for use. For each recording it runs
data-integrity checks, computes descriptive statistics and writes a JSON
report, a set of figures and a self-contained HTML dashboard. When several
feeds recorded the same instrument, it aligns them on a common time grid and
measures how much they disagree.

Two kinds of result are kept apart throughout:

- **Data-integrity exceptions**: hard facts about the file (crossed book,
  silent gaps, missing values, ...). Any non-zero count is an exception.
- **Descriptive statistics**: stationarity, normality, autocorrelation,
  outliers. They characterise the series and are never pass/fail; fat tails
  and volatility clustering are normal in real returns.

Contents

1. [How to use](#1-how-to-use)
2. [Configuration](#2-configuration)
3. [Checks and statistics per recording](#3-checks-and-statistics-per-recording)
4. [Cross-source comparison](#4-cross-source-comparison)

---

## 1. How to use

### Install

Python 3.12 or newer.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`./mdsq.py` re-executes itself under `.venv/bin/python` whenever that venv
exists next to it, so the venv never needs activating.

### Run

```
./mdsq.py config.ini
```

The config file holds both what to process and every tunable. The `[run]`
section selects the inputs:

```ini
[run]
report_inputs = data/20260906_AAPL*
compare_inputs = data/20260906_*
report_dir = report
```

- `report_inputs`: comma-separated glob pattern(s). Each matched file is
  assessed on its own. One bad file does not stop the others.
- `compare_inputs`: glob pattern(s). Matched files are grouped by instrument
  (see *Filename convention*) and cross-compared, at least two per group.
- `report_dir`: where everything is written.

Leave either list empty to skip that phase. Globs resolve against the
current working directory; a bare directory expands to the `.csv` and
`.txt` files directly inside it.

The shipped `config.ini` runs both phases on the sample data. Open the
results in a browser:

```
report/sample_market_data/dashboard.html
report/compare_<UTC-timestamp>/comparison_dashboard.html
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | every recording and comparison that ran succeeded |
| 1 | at least one recording or comparison failed, or the config contents are invalid |
| 2 | usage error: wrong arguments, or the config file does not exist |

Data-integrity exceptions found *in* a recording are normal output, not a
failure. Read them from the JSON or the dashboard.

Every run logs to the console and to `log/pipeline.log` next to `mdsq.py`,
rotated daily, keeping `[logging] backup_days` days.

### Input files

CSV with a header row, one quote snapshot per line, one instrument per
file. The canonical header is

```
timestamp,best_bid_1,best_ask_1,best_bid_size_1,best_ask_size_1,best_bid_2,best_ask_2,best_bid_size_2,best_ask_size_2,...
2024-03-04 08:00:00.125,1.08498,1.08512,1500000,2000000,1.08495,1.08515,3000000,2500000
```

- `timestamp` and level 1 are mandatory. Levels 2 to 7 are optional; a level
  that is only partially present (a bid column without its ask, say) is
  rejected as malformed.
- Header names are mapped in `[columns]`, so vendor files need no editing.
- Timestamps are parsed by pandas (ISO 8601 or `YYYY-MM-DD HH:MM:SS.fff`,
  any sub-second precision) and used as recorded. No timezone conversion is
  applied, so feeds that are compared must share a clock basis.
- Columns outside the schema are ignored, except that their empty cells
  still count toward the missing-values check.
- A first row whose best bid or ask is empty, zero or negative is treated
  as a zeroed start-up row and dropped, with a logged warning.
- A file with fewer than `[general] min_rows` rows is rejected.

**Filename convention.** Comparison inputs are grouped automatically when
the filename starts with a date and a product:

```
YYYYMMDD_product[_market[_server[_source]]].txt
20260906_EURUSD_LDN_srv1_vendorA.txt
```

Files sharing `(date, product)` are compared together, one comparison per
instrument per day, up to `max_sources` files. The optional trailing tokens
distinguish same-day files and, when present, let the report attribute a
divergence to the market, server or source dimension. A product name cannot
contain an underscore. Files that do not follow the convention are excluded
when at least one matched file does; when none does, all matched files are
compared as a single group.

Sample recordings in `data/`:

- `sample_market_data.txt`: 5,000 equity-style ticks with injected defects
  (crossed book, negative spread, a price spike, a five-minute outage, a
  duplicated timestamp) that exercise every check.
- `synthetic_eurusd_quotes.txt` and `synthetic_eurusd_quotes_feedB.txt`:
  one EURUSD session as seen by two vendors; feed B is derived from feed A
  with its own timing, latency, noise and five injected divergences.

### Output

For an input `data/<name>.txt`, `report_inputs` writes `report_dir/<name>/`:

| File | Content |
|---|---|
| `quality_report.json` | `source_file`, `generated_at`, then the sections `structural`, `continuity`, `price_level`, `returns`, `liquidity` with the fields listed in section 3, the `plots` list, and `cleaned_data_file` when outlier treatment is on |
| `plots/*.png` | price with bid/ask range and outlier marks, spread, inter-arrival distribution, return distribution, normal Q-Q plot, autocorrelation of returns and of squared returns, ticks per minute |
| `dashboard.html` | the interactive report, one file, no server or network needed |
| `cleaned_data.csv` | only when `[outliers] replacement_mode` is not 0 |

`compare_inputs` writes `report_dir/<date>_<product>/` per grouped
instrument, or `report_dir/compare_<UTC-timestamp>/` for an ungrouped run:

| File | Content |
|---|---|
| `comparison_report.json` | `sources` (rows, session bounds, file, metadata), the alignment parameters actually used, `window_start`, `window_end`, `n_grid_points`, `pairs` (statistics of section 4 plus `top_divergences`), `status`, and `attribution` and `instrument` when grouped |
| `plots/*.png` | mid price overlaid by source, a difference plot and a difference histogram per pair |
| `comparison_dashboard.html` | same layout as the per-recording dashboard |

**The dashboard.** A time-series explorer across the top and three columns
below: alerts and summary, recording status, time-series analysis. In the
explorer, drag to pan, scroll to zoom, double-click for the full session,
drag on the overview strip to select a range; the arrow keys, `+`/`-` and
`Home` work when the chart has focus. The series switch offers mid with the
bid/ask band, spread, normalised returns, level-1 sizes and tick activity
(on the comparison page: mid by source and one difference-in-bps view per
pair). Lanes above the plot mark exceptions, Hampel outliers and gaps. Every
timestamp in the tables links into the explorer and pins that moment; the
page URL then carries `#mode=<series>&t=<epoch-ms>` and can be shared. The
embedded tick data is capped at `[dashboard] max_chart_points` rows; larger
recordings are thinned keeping each bucket's minimum and maximum and every
flagged row, and the page says so.

**Flat exports.** `report_dir/json/` receives a copy of every JSON report
plus `index.json`, one row per run with `name`, `kind` (`report` or
`compare`), `status` (`OK`/`KO` for comparisons, `ERROR` for a failed run,
`null` for a report), `generated_at`, `date`, `product`, `n_sources`,
`n_pairs`, `n_divergences`, `max_abs_diff_bps`, `n_exceptions`, `n_checks`
and `error`. Rows are upserted per `(name, kind)` and capped at the 2,000
most recent. A run that fails still writes an `ERROR` row and export, so a
stale success never looks current. `[general] grafana_export_dir` copies the
exports to a second fixed path; `grafana/mdsq_dashboard.json` is a Grafana
dashboard reading `index.json` through the Infinity datasource.

### Other commands

```
./mdsq.py treat-outliers <input.csv> [--mode N] [-o out.csv] [--config cfg.ini]
./mdsq.py generate-synthetic
```

`treat-outliers` applies the outlier treatment of section 3.8 to one file
without running the pipeline; it defaults to `<input>_cleaned.csv`, the
mode from the config, and the `config.ini` next to `mdsq.py`.
`generate-synthetic` regenerates the two synthetic EURUSD files into `data/`
under the current directory.

---

## 2. Configuration

All keys live in the file passed to `mdsq.py`; `config.ini` is the
template. Defaults below are as shipped.

### `[run]`

| Key | Default | Meaning |
|---|---|---|
| `report_inputs` | `data/sample_market_data.txt` | glob(s) of recordings to assess individually |
| `compare_inputs` | `data/synthetic_eurusd_quotes*.txt` | glob(s) of recordings to cross-compare |
| `report_dir` | `report` | output root |

### `[general]`

| Key | Default | Meaning |
|---|---|---|
| `min_rows` | 40 | a file with fewer rows is rejected (must be >= 1) |
| `max_memory_budget_gb` | 16 | process memory budget |
| `memory_overhead_multiplier` | 6 | assumed peak memory per byte of input file |
| `grafana_export_dir` | empty | optional second copy of `report_dir/json/` |

Three layers enforce the budget: a file is rejected before loading when
`file size x memory_overhead_multiplier` exceeds the budget; the process
address space is hard-capped at the budget where the OS allows it (Linux
yes, macOS no); and peak resident memory is compared with the budget at each
pipeline checkpoint. Nothing is streamed: a file that needs more than the
budget is rejected, not processed in pieces.

### `[columns]`

| Key | Default | Meaning |
|---|---|---|
| `timestamp` | `timestamp` | header name(s) of the timestamp column |
| `best_bid_1`, `best_ask_1` | `best_bid_1, BEST_BID_1, RT_22` / `best_ask_1, BEST_ASK_1, RT_25` | header name(s) of the level-1 prices |
| `best_bid_size_1`, `best_ask_size_1` | `best_bid_size_1` / `best_ask_size_1` | header name(s) of the level-1 sizes |
| `max_levels` | 2 | deepest level looked for, 1 to 7 |
| `best_bid_level_template`, `best_ask_level_template`, `best_bid_size_level_template`, `best_ask_size_level_template` | `best_bid_{level}`, `best_ask_{level}`, `best_bid_size_{level}`, `best_ask_size_{level}` | how deeper levels are named; `{level}` is substituted with 2, 3, ... |

Each mandatory key accepts a comma-separated list of alternatives. Exactly
one of them must be present in a file; two present at once is an error.

### `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `backup_days` | 30 | daily log files kept |

### `[continuity]`

| Key | Default | Meaning |
|---|---|---|
| `gap_threshold_s` | 5.0 | silence longer than this between consecutive ticks is a gap |
| `top_n_gaps` | 10 | largest gaps listed in the report |

Set `gap_threshold_s` relative to the feed's normal cadence: a few multiples
of the median inter-arrival time flags outages without flagging quiet
minutes.

### `[returns]`

| Key | Default | Meaning |
|---|---|---|
| `significance_level` | 0.05 | alpha used by every hypothesis test |
| `ljung_box_lags` | 10 | lag h of both Ljung-Box tests |
| `dt_norm_floor_quantile` | 0.05 | quantile of the positive inter-arrival times used as the floor in the sqrt(dt) normalisation (section 3.1) |

### `[outliers]`

| Key | Default | Meaning |
|---|---|---|
| `hampel_window` | 31 | centred window length of the Hampel filter, in non-zero returns; use an odd number |
| `hampel_threshold` | 4.0 | a return is an outlier beyond this many robust standard deviations |
| `replacement_mode` | 0 | 0 none, 1 nan, 2 interpolate, 3 ffill, 4 rolling_median (section 3.8) |

### `[comparison]`

| Key | Default | Meaning |
|---|---|---|
| `alignment_freq_s` | 1.0 | step of the common time grid, seconds |
| `max_staleness_s` | 5.0 | a source's last quote older than this at a grid point counts as missing |
| `price_diff_threshold_bps` | 5.0 | a mid difference beyond this, in basis points, is a divergence |
| `top_n_divergences` | 10 | largest divergences listed per pair |
| `max_grid_points` | 5,000,000 | a grid larger than this is refused before allocation |
| `max_sources` | 8 | largest group compared; pairs grow as n(n-1)/2 |
| `downsample_points_per_day` | 1000 | target density when thinning large sources; 0 disables thinning |
| `downsample_min_rows` | 1,000,000 | sources with fewer rows are never thinned |

`alignment_freq_s` should be no finer than the feeds' typical inter-arrival
time, and `max_staleness_s` a small multiple of it. When
`downsample_points_per_day` is non-zero the staleness tolerance is raised
to at least `86400 / downsample_points_per_day` seconds (86.4 s with the
defaults), whether or not any source was actually thinned; set it to 0 to
compare at exactly `max_staleness_s`.

### `[dashboard]`

| Key | Default | Meaning |
|---|---|---|
| `max_chart_points` | 20000 | rows embedded in the explorer (>= 100); about 100 bytes per row |

---

## 3. Checks and statistics per recording

### 3.1 Derived series

Rows are sorted by timestamp (stable sort) before anything except the
structural checks. For tick t with best bid b_t, best ask a_t and
timestamp T_t:

```
mid            m_t  = (b_t + a_t) / 2
spread         s_t  = a_t - b_t
inter-arrival  dt_t = T_t - T_(t-1)                      [seconds]
log return     r_t  = ln(m_t) - ln(m_(t-1))
normalised     z_t  = r_t / sqrt( max(dt_t, dt_floor) )
               dt_floor = max( Q_q({dt > 0}), 0.001 )     q = dt_norm_floor_quantile
```

Under a diffusion the variance of a return over an interval is proportional
to the interval, so dividing by sqrt(dt) puts a quote that arrives after a
long silence on the same footing as one that arrives a millisecond later.
The floor stops bursts of near-simultaneous ticks from exploding z and
handles duplicated timestamps (dt = 0). All return statistics are computed
on z.

### 3.2 Data-integrity checks

Ten counts. Any non-zero value is an exception. The `structural` counts are
evaluated on the file in its original row order; the others on the sorted
series. The `structural` section also records `n_rows` and
`n_levels_present`.

| Section | Field | Counted when |
|---|---|---|
| structural | `n_duplicate_timestamps` | T_t = T_(t-1) in file order |
| structural | `n_out_of_order_timestamps` | T_t < T_(t-1) in file order |
| structural | `n_crossed_book` | b_t >= a_t |
| structural | `n_level_bid_inconsistent` | for some consecutive present levels k < k': bid_(k') > bid_k |
| structural | `n_level_ask_inconsistent` | for some consecutive present levels k < k': ask_(k') < ask_k |
| structural | `n_non_positive_sizes` | any size at any present level <= 0 |
| structural | `n_missing_values` | empty cells, counted per cell over every column of the file |
| continuity | `n_gaps_over_threshold` | dt_t > gap_threshold_s |
| liquidity | `n_negative_spread` | s_t < 0 |
| liquidity | `n_zero_or_negative_sizes` | any size at any present level <= 0 |

Except for `n_missing_values`, which counts cells, a row counts once per
check however many levels or columns fail. Two checks overlap by
construction: a negative spread is also a crossed book,
and the two size checks count the same rows, so one defect can fire two
counts. `n_exceptions` in `index.json` is the number of the ten checks with
a non-zero count.

### 3.3 Continuity

On the sorted inter-arrival times dt_2 .. dt_n: `session_start`,
`session_end`, `session_duration_s`, `mean_inter_arrival_s`,
`median_inter_arrival_s`, `p95_inter_arrival_s` (empirical 95th percentile
with linear interpolation), `max_inter_arrival_s`. A gap is dt_t >
`gap_threshold_s`; `top_gaps` lists the `top_n_gaps` largest with the
timestamp of the tick that *ends* each gap and its length.

### 3.4 Liquidity

On the spread: `spread_mean`, `spread_median`, `spread_std` (sample standard
deviation, n-1 denominator), `spread_min`, `spread_max`, `n_zero_spread`
(locked book, s_t = 0), `n_negative_spread`. On sizes:
`n_zero_or_negative_sizes`, `bid_size_1_median`, `ask_size_1_median`.

### 3.5 Price level

On the mid m_t: `start`, `end`, `mean`, `std`, `min`, `max`, then the
Augmented Dickey-Fuller and KPSS tests of section 3.7. A traded price is
expected to be non-stationary (both tests agree on a unit root). A level
that tests stationary is itself a finding: a stuck or replayed feed looks
like that.

### 3.6 Returns

On z_2 .. z_n, n = `n_returns`. With the sample mean z-bar and the central
moments

```
m_k = (1/n) * sum_t (z_t - zbar)^k
```

| Field | Definition |
|---|---|
| `mean`, `std` | sample mean and standard deviation (n-1) |
| `skew` | g1 = m_3 / m_2^(3/2) |
| `kurtosis_excess` | g2 = m_4 / m_2^2 - 3 (0 for a normal distribution) |
| `jarque_bera_stat`, `jarque_bera_pvalue` | JB = n/6 * ( g1^2 + g2^2/4 ), chi-squared with 2 degrees of freedom under normality |
| `jarque_bera_normal` | p > alpha |
| `ljung_box_stat`, `ljung_box_pvalue` | Q_h on z (below), h = `ljung_box_lags` |
| `ljung_box_autocorrelated` | p < alpha: returns are serially correlated |
| `ljung_box_sq_stat`, `ljung_box_sq_pvalue` | Q_h on z^2 |
| `ljung_box_sq_vol_clustering` | p < alpha: squared returns are serially correlated, i.e. volatility clusters |
| `adf_*`, `kpss_*` | section 3.7, on z; returns are expected to be stationary |
| `n_outliers_hampel` | section 3.8 |
| `significance_level`, `ljung_box_lags`, `outlier_hampel_window`, `outlier_hampel_threshold` | the parameters used, echoed for the record |

The Ljung-Box statistic at lag h, with the sample autocorrelation rho_k:

```
rho_k = sum_(t=k+1..n) (x_t - xbar)(x_(t-k) - xbar) / sum_(t=1..n) (x_t - xbar)^2

Q_h   = n (n + 2) * sum_(k=1..h) rho_k^2 / (n - k)        ~ chi-squared(h) under no autocorrelation
```

Significant autocorrelation of returns at tick level is common (bid-ask
bounce, quote updates on one side of the book). Volatility clustering is the
norm for any traded instrument. Neither is a defect; both matter when the
file is used to fit or test a model that assumes independence.

### 3.7 Stationarity tests

Both tests run with a constant and no trend, on the mid (section 3.5) and
on the normalised returns (section 3.6). They have opposite null
hypotheses, which is why both are reported.

**Augmented Dickey-Fuller.** Regression

```
dy_t = c + gamma * y_(t-1) + sum_(i=1..p) phi_i * dy_(t-i) + e_t
```

Null hypothesis: gamma = 0, the series has a unit root (non-stationary).
Statistic `adf_stat` = gamma-hat / se(gamma-hat), compared with the
MacKinnon distribution; `adf_pvalue` is MacKinnon's approximate p-value. The
lag order p is chosen by AIC among 0 .. ceil(12 (n/100)^(1/4)).
`adf_stationary` = p-value < alpha (the unit root is rejected).

**KPSS.** Null hypothesis: the series is stationary around its mean. With
e_t = y_t - y-bar and partial sums S_t = e_1 + ... + e_t,

```
kpss_stat = (1/n^2) * sum_(t=1..n) S_t^2 / s2_LR
```

where s2_LR is the Newey-West long-run variance of e_t (Bartlett kernel,
bandwidth chosen automatically by the Hobijn-Franses-Ooms rule). A large
statistic rejects stationarity. `kpss_pvalue` is interpolated from the
published critical-value table and is therefore bounded: 0.01 means "beyond
the 1% critical value" and 0.1 means "below the 10% critical value".
`kpss_stationary` = p-value > alpha (stationarity is not rejected).

The dashboard reads the pair as *stationary* when ADF rejects the unit root
and KPSS does not reject stationarity, *non-stationary* when neither
rejects, and *mixed* otherwise (typically a near-unit-root or a series with
a structural break).

### 3.8 Hampel outlier scan and treatment

The scan runs on the normalised returns z with the zeros removed, so that
runs of unchanged quotes do not collapse the local dispersion to zero. For
a centred window of w = `hampel_window` observations (at least floor(w/2)+1
of them available, which allows the edges), with h = floor(w/2):

```
med_t   = median( x_(t-h) .. x_(t+h) )
MAD_t   = median( |x_i - med_t| , i = t-h .. t+h )
sigma_t = 1.4826 * MAD_t          if MAD_t > 0
        = 1.4826 * MAD_global     otherwise, MAD_global = median |x - median(x)| over the whole series
flag_t  = |x_t - med_t| > tau * sigma_t          tau = hampel_threshold
```

1.4826 = 1 / Phi^(-1)(3/4) makes the MAD a consistent estimator of the
standard deviation for normal data, so tau reads as a number of standard
deviations: with tau = 4, normal returns and an exact scale, about 6 in
100,000 ticks would be flagged by chance (somewhat more in practice, since a
31-tick window estimates the scale with noise). Median and MAD have a 50% breakdown point, so a
cluster of bad ticks does not hide itself the way it would with a mean and
standard deviation. If both MADs are zero nothing is flagged. Because the
window is counted in ticks, not seconds, it adapts to the feed's cadence.

The flagged count is `n_outliers_hampel`. Note that the scan works on
returns: a single bad tick produces a jump in and a jump back out, so it
flags two returns and, below, two ticks.

**Treatment** is opt-in (`replacement_mode`, or `--mode` for the standalone
command) and never changes the statistics above: the report always
describes the file as recorded, and the corrected copy is written beside
it. For each flagged tick the mid is replaced by m'_t, and the delta
m'_t - m_t is added to every bid and ask at every present level, so the
book keeps its shape; sizes are untouched; spread and returns are
recomputed.

| Mode | m'_t |
|---|---|
| 1 nan | missing (every bid and ask of the tick becomes missing) |
| 2 interpolate | linear interpolation, in row index, between the nearest unflagged ticks on each side; a flagged tick before the first or after the last unflagged one takes that unflagged value |
| 3 ffill | the last unflagged mid before t (no look-ahead; missing if there is none) |
| 4 rolling_median | the centred rolling median of the recorded mid over `hampel_window` ticks, which is robust to the flagged value itself |

`cleaned_data.csv` keeps only the schema columns and can be fed back
through `report_inputs` like any recording.

---

## 4. Cross-source comparison

Every source is loaded and validated as a single recording, then:

**4.1 Thinning.** A source with at least `downsample_min_rows` rows and
`downsample_points_per_day` > 0 is reduced to one quote per
g = 86400 / `downsample_points_per_day` seconds: on a grid from its first
to its last timestamp, each point takes the last quote at or before it. A
source that is already sparser is left alone.

**4.2 Common grid.** The overlap window runs from the latest first tick to
the earliest last tick across the sources; no overlap is an error. The grid
is

```
T_k = start + k * f,   k = 0 .. floor((end - start) / f),   f = alignment_freq_s
```

and is refused before allocation if it would exceed `max_grid_points`.

**4.3 Alignment.** At each T_k a source contributes its last quote with
timestamp <= T_k, provided T_k minus that timestamp is at most the
staleness tolerance; otherwise it is missing at T_k. The tolerance is

```
tol = max_staleness_s                                     if downsample_points_per_day = 0
    = max( max_staleness_s, 86400 / downsample_points_per_day )   otherwise
```

and the value used is written to the report as `max_staleness_s`.

**4.4 Pair statistics.** For each pair of sources (A, B), on the set C of
grid points where both are present, with mids a_k and b_k:

```
d_k      = a_k - b_k
dbps_k   = 10^4 * d_k / ((a_k + b_k) / 2)
```

| Field | Definition |
|---|---|
| `source_a`, `source_b` | the two sources, labelled by their filename tokens or stems |
| `n_grid_points`, `n_common_points`, `coverage` | grid size, size of C, their ratio |
| `mean_diff` | mean of d_k |
| `mean_abs_diff`, `median_abs_diff`, `max_abs_diff` | mean, median, maximum of abs(d_k) |
| `rms_diff` | sqrt( mean of d_k^2 ) |
| `mean_abs_diff_bps`, `max_abs_diff_bps` | mean and maximum of abs(dbps_k) |
| `return_correlation` | Pearson correlation between ln(a_k) - ln(a_(k-1)) and ln(b_k) - ln(b_(k-1)) over consecutive points of C |
| `n_divergences` | number of k with abs(dbps_k) > `price_diff_threshold_bps` |
| `top_divergences` | the `top_n_divergences` largest abs(dbps_k), with timestamp, both mids, d_k and dbps_k |

A pair with fewer than two common points carries only the first row of the
table. A return correlation near 1 with a non-zero mean difference points to
a constant offset (different clocks, fees or conventions); a low correlation
with small differences points to one feed lagging the other by more than a
grid step.

**4.5 Status.** `status` is `OK` when every pair has `n_divergences` = 0,
and `KO` otherwise, including when a pair has too few common points to be
compared.

**4.6 Attribution** (grouped runs only, from the filename tokens).
`source_outlier_ranking` scores each source by the average of
`mean_abs_diff_bps` over the pairs it belongs to, highest first: the source
that disagrees most with all the others. `dimension_attribution` takes each
of market, server and source with at least two distinct values and
contrasts the average `mean_abs_diff_bps` of the pairs that *differ* on
that dimension with that of the pairs that *match*; dimensions are sorted by
the difference, so the first one is the most likely explanation of the
divergence. A dimension on which all pairs differ, or all match, is not
reported.
