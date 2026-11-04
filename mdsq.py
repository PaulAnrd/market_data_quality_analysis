#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import glob
import html
import itertools
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def _reexec_into_venv() -> None:
    if not VENV_PYTHON.exists():
        return
    try:
        already_in_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    except OSError:
        return
    if not already_in_venv:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    _reexec_into_venv()

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.stattools import acf, adfuller, kpss

logger = logging.getLogger("mdsq")


def _alternatives(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


class Config:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        p = configparser.ConfigParser()
        if not p.read(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        self.path = path

        self.MIN_ROWS = p.getint("general", "min_rows")
        if self.MIN_ROWS < 1:
            raise ValueError(f"config.ini [general] min_rows={self.MIN_ROWS} must be at least 1.")
        self.IGNORE_ZEROS = p.getboolean("general", "ignore_zeros", fallback=False)
        self.MAX_MEMORY_BUDGET_GB = p.getfloat("general", "max_memory_budget_gb")
        self.MAX_MEMORY_BUDGET_BYTES = int(self.MAX_MEMORY_BUDGET_GB * 1024 ** 3)
        self.MEMORY_OVERHEAD_MULTIPLIER = p.getfloat("general", "memory_overhead_multiplier")
        self.GRAFANA_EXPORT_DIR = p.get("general", "grafana_export_dir").strip()

        self.COLUMNS_MAP = {
            "timestamp": _alternatives(p.get("columns", "timestamp")),
            "best_bid_1": _alternatives(p.get("columns", "best_bid_1")),
            "best_ask_1": _alternatives(p.get("columns", "best_ask_1")),
            "best_bid_size_1": _alternatives(p.get("columns", "best_bid_size_1")),
            "best_ask_size_1": _alternatives(p.get("columns", "best_ask_size_1")),
        }
        self.COLUMNS_MAX_LEVELS = p.getint("columns", "max_levels")
        if not (1 <= self.COLUMNS_MAX_LEVELS <= 7):
            raise ValueError(
                f"config.ini [columns] max_levels={self.COLUMNS_MAX_LEVELS} must be between 1 and 7 "
                f"(this tool's canonical schema supports at most 7 order-book levels)."
            )
        self.COLUMNS_LEVEL_TEMPLATES = {
            "best_bid": _alternatives(p.get("columns", "best_bid_level_template")),
            "best_ask": _alternatives(p.get("columns", "best_ask_level_template")),
            "best_bid_size": _alternatives(p.get("columns", "best_bid_size_level_template")),
            "best_ask_size": _alternatives(p.get("columns", "best_ask_size_level_template")),
        }

        self.LOG_BACKUP_DAYS = p.getint("logging", "backup_days")

        self.GAP_THRESHOLD_S = p.getfloat("continuity", "gap_threshold_s")
        self.TOP_N_GAPS = p.getint("continuity", "top_n_gaps")

        self.SIGNIFICANCE_LEVEL = p.getfloat("returns", "significance_level")
        self.LJUNG_BOX_LAGS = p.getint("returns", "ljung_box_lags")
        self.DT_NORM_FLOOR_QUANTILE = p.getfloat("returns", "dt_norm_floor_quantile")

        self.HAMPEL_WINDOW = p.getint("outliers", "hampel_window")
        self.HAMPEL_THRESHOLD = p.getfloat("outliers", "hampel_threshold")
        self.OUTLIER_REPLACEMENT_MODE = p.getint("outliers", "replacement_mode")

        self.COMPARISON_ALIGNMENT_FREQ_S = p.getfloat("comparison", "alignment_freq_s")
        self.COMPARISON_MAX_STALENESS_S = p.getfloat("comparison", "max_staleness_s")
        self.COMPARISON_PRICE_DIFF_THRESHOLD_BPS = p.getfloat("comparison", "price_diff_threshold_bps")
        self.COMPARISON_TOP_N_DIVERGENCES = p.getint("comparison", "top_n_divergences")
        self.COMPARISON_MAX_GRID_POINTS = p.getint("comparison", "max_grid_points")
        self.COMPARISON_MAX_SOURCES = p.getint("comparison", "max_sources")
        self.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY = p.getint("comparison", "downsample_points_per_day")
        self.COMPARISON_DOWNSAMPLE_MIN_ROWS = p.getint("comparison", "downsample_min_rows")

        self.RUN_REPORT_INPUTS = _alternatives(p.get("run", "report_inputs", fallback=""))
        self.RUN_COMPARE_INPUTS = _alternatives(p.get("run", "compare_inputs", fallback=""))
        self.RUN_REPORT_DIR = p.get("run", "report_dir", fallback="report").strip() or "report"

        self.DASHBOARD_MAX_CHART_POINTS = p.getint("dashboard", "max_chart_points", fallback=20000)
        if self.DASHBOARD_MAX_CHART_POINTS < 100:
            raise ValueError(f"config.ini [dashboard] max_chart_points={self.DASHBOARD_MAX_CHART_POINTS} must be at least 100.")


CFG: Config | None = None


def load_config(path: Path | str) -> Config:
    global CFG
    CFG = Config(path)
    return CFG


LOG_DIR = ROOT / "log"
LOG_FILE = LOG_DIR / "pipeline.log"


def configure_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=CFG.LOG_BACKUP_DAYS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    logging.captureWarnings(True)


def atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


class FileTooLargeError(Exception):
    pass


class MemoryBudgetExceededError(Exception):
    pass


def check_file_is_safe_to_load(path: Path) -> None:
    file_size = os.path.getsize(path)
    estimated_peak_bytes = file_size * CFG.MEMORY_OVERHEAD_MULTIPLIER

    if estimated_peak_bytes > CFG.MAX_MEMORY_BUDGET_BYTES:
        raise FileTooLargeError(
            f"{path}: {file_size / 1024**3:.1f} GB on disk, estimated peak memory "
            f"{estimated_peak_bytes / 1024**3:.1f} GB (x{CFG.MEMORY_OVERHEAD_MULTIPLIER:g} "
            f"overhead) exceeds the {CFG.MAX_MEMORY_BUDGET_BYTES / 1024**3:.0f} GB budget "
            f"(config.ini [general] max_memory_budget_gb) -- rejected before loading "
            f"to protect the system."
        )


def apply_process_memory_limit() -> None:
    try:
        import resource
    except ImportError:
        logger.warning(
            "resource module unavailable on this platform (POSIX only) -- "
            "RLIMIT_AS not applied, relying on the pre-flight check and the "
            "runtime memory watchdog instead."
        )
        return

    budget = CFG.MAX_MEMORY_BUDGET_BYTES
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    new_hard = budget if hard == resource.RLIM_INFINITY else min(budget, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (budget, new_hard))
    except (ValueError, OSError) as exc:
        logger.warning(
            "Could not apply hard memory limit (RLIMIT_AS unsupported on this "
            "platform: %s) -- relying on the pre-flight check and the runtime "
            "memory watchdog instead.",
            exc,
        )
        return
    logger.info("Process memory hard-capped at %.1f GB (RLIMIT_AS)", budget / 1024**3)


def _current_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return max_rss if sys.platform == "darwin" else max_rss * 1024


def check_memory_budget(context: str = "") -> None:
    used_bytes = _current_rss_bytes()
    if used_bytes is None:
        return
    if used_bytes > CFG.MAX_MEMORY_BUDGET_BYTES:
        where = f" while {context}" if context else ""
        raise MemoryBudgetExceededError(
            f"Process memory ({used_bytes / 1024**3:.2f} GB) exceeded the "
            f"{CFG.MAX_MEMORY_BUDGET_BYTES / 1024**3:.0f} GB budget{where} "
            f"(config.ini [general] max_memory_budget_gb)."
        )


MAX_SUPPORTED_LEVELS = 7

MANDATORY_COLUMNS = ["timestamp", "best_bid_1", "best_ask_1", "best_bid_size_1", "best_ask_size_1"]


def level_columns(level: int) -> list[str]:
    return [f"best_bid_{level}", f"best_ask_{level}", f"best_bid_size_{level}", f"best_ask_size_{level}"]


def present_levels(df: pd.DataFrame) -> list[int]:
    return [lvl for lvl in range(1, MAX_SUPPORTED_LEVELS + 1) if all(c in df.columns for c in level_columns(lvl))]


def raw_schema_columns(df: pd.DataFrame) -> list[str]:
    cols = ["timestamp"]
    for lvl in present_levels(df):
        cols.extend(level_columns(lvl))
    return cols


def price_columns(df: pd.DataFrame) -> list[str]:
    return [c for lvl in present_levels(df) for c in (f"best_bid_{lvl}", f"best_ask_{lvl}")]


def size_columns(df: pd.DataFrame) -> list[str]:
    return [c for lvl in present_levels(df) for c in (f"best_bid_size_{lvl}", f"best_ask_size_{lvl}")]


def zero_as_missing(values):
    return values.where(values != 0) if CFG.IGNORE_ZEROS else values


def n_zero_values_ignored(df: pd.DataFrame) -> int:
    if not CFG.IGNORE_ZEROS:
        return 0
    return int((df[price_columns(df) + size_columns(df)] == 0).sum().sum())


def book_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    levels = present_levels(df)
    book = zero_as_missing(df[price_columns(df) + size_columns(df)])
    level_bid = pd.Series(False, index=df.index)
    level_ask = pd.Series(False, index=df.index)
    for lo, hi in zip(levels, levels[1:]):
        level_bid |= book[f"best_bid_{hi}"] > book[f"best_bid_{lo}"]
        level_ask |= book[f"best_ask_{hi}"] < book[f"best_ask_{lo}"]
    return {
        "level_bid": level_bid,
        "level_ask": level_ask,
        "nonpos_size": (book[size_columns(df)] <= 0).any(axis=1),
    }


def _resolve_column(df_columns: set[str], canonical: str, candidates: list[str]) -> str | None:
    present = [c for c in candidates if c in df_columns]
    if len(present) > 1:
        raise ValueError(
            f"Ambiguous column for '{canonical}': more than one configured alternative is "
            f"present in this file ({present}) -- expected exactly one (config.ini [columns])."
        )
    return present[0] if present else None


def _parse_timestamps(raw: pd.Series, path: str) -> pd.Series:
    try:
        ts = pd.to_datetime(raw)
    except ValueError:
        ts = pd.to_datetime(raw, utc=True)
    if ts.dt.tz is not None:
        logger.warning("%s: timestamps carry a UTC offset -- converted to UTC and stored without offset", path)
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


def load_raw(path: str) -> tuple[pd.DataFrame, int]:
    df = pd.read_csv(path)
    df_columns = set(df.columns)

    rename = {}
    missing = []
    for canon in MANDATORY_COLUMNS:
        actual = _resolve_column(df_columns, canon, CFG.COLUMNS_MAP[canon])
        if actual is None:
            missing.append((canon, CFG.COLUMNS_MAP[canon]))
        else:
            rename[actual] = canon
    if missing:
        detail = "; ".join(f"'{canon}' (tried {alts})" for canon, alts in missing)
        raise ValueError(f"Missing mandatory columns -- {detail} (see config.ini [columns]).")

    for lvl in range(2, CFG.COLUMNS_MAX_LEVELS + 1):
        level_rename = {}
        for key, canon in zip(("best_bid", "best_ask", "best_bid_size", "best_ask_size"), level_columns(lvl)):
            candidates = [t.format(level=lvl) for t in CFG.COLUMNS_LEVEL_TEMPLATES[key]]
            actual = _resolve_column(df_columns, canon, candidates)
            if actual is not None:
                level_rename[actual] = canon

        if len(level_rename) == 4:
            rename.update(level_rename)
        elif level_rename:
            raise ValueError(
                f"Level {lvl} is only partially present {list(level_rename)} (expected all 4 "
                f"of its fields, or none) -- check config.ini [columns] level templates "
                f"against this file's header."
            )

    df = df.rename(columns=rename)
    df["timestamp"] = _parse_timestamps(df["timestamp"], path)

    n_zero_rows = 0
    if CFG.IGNORE_ZEROS:
        zero_quote = (df["best_bid_1"] == 0) | (df["best_ask_1"] == 0)
        n_zero_rows = int(zero_quote.sum())
        if n_zero_rows:
            logger.info(
                "%s: ignoring %d of %d row(s) with a zero best bid or ask (config.ini [general] ignore_zeros)",
                path, n_zero_rows, len(df),
            )
            df = df.loc[~zero_quote].reset_index(drop=True)

    if len(df):
        first = df.iloc[0]
        if pd.isna(first["best_bid_1"]) or pd.isna(first["best_ask_1"]) \
                or first["best_bid_1"] <= 0 or first["best_ask_1"] <= 0:
            logger.warning(
                "%s: dropping row 0 (best_bid_1=%s, best_ask_1=%s) -- looks like a zeroed "
                "startup row rather than a real quote",
                path, first["best_bid_1"], first["best_ask_1"],
            )
            df = df.iloc[1:].reset_index(drop=True)

    return df, n_zero_rows


def compute_returns(mid: pd.Series, dt_s: pd.Series) -> tuple[pd.Series, pd.Series]:
    log_return = np.log(mid.where(mid > 0)).diff()
    positive_dt = dt_s[dt_s > 0]
    dt_floor = max(positive_dt.quantile(CFG.DT_NORM_FLOOR_QUANTILE), 1e-3) if len(positive_dt) else 1e-3
    dt_norm_return = log_return / np.sqrt(dt_s.clip(lower=dt_floor))
    return log_return, dt_norm_return


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)
    df["mid"] = (df["best_bid_1"] + df["best_ask_1"]) / 2
    df["spread"] = df["best_ask_1"] - df["best_bid_1"]
    dt_s = df["timestamp"].diff().dt.total_seconds()
    df["log_return"], dt_norm_return = compute_returns(df["mid"], dt_s)
    df["dt_s"] = dt_s
    df["dt_norm_return"] = dt_norm_return
    return df


def load_quotes(path: str) -> pd.DataFrame:
    df_raw, _ = load_raw(path)
    return enrich(df_raw)


def check_structural(df_raw: pd.DataFrame, n_zero_rows_ignored: int = 0) -> dict:
    ts = df_raw["timestamp"]
    diffs = ts.diff().dt.total_seconds()

    n_duplicate_timestamps = int((diffs == 0).sum())
    n_out_of_order = int((diffs < 0).sum())

    crossed = df_raw["best_bid_1"] >= df_raw["best_ask_1"]
    book = book_masks(df_raw)

    n_missing = int(df_raw[raw_schema_columns(df_raw)].isna().sum().sum())

    return {
        "n_rows": int(len(df_raw)),
        "n_levels_present": len(present_levels(df_raw)),
        "ignore_zeros": CFG.IGNORE_ZEROS,
        "n_zero_rows_ignored": int(n_zero_rows_ignored),
        "n_zero_values_ignored": n_zero_values_ignored(df_raw),
        "n_duplicate_timestamps": n_duplicate_timestamps,
        "n_out_of_order_timestamps": n_out_of_order,
        "n_crossed_book": int(crossed.sum()),
        "n_level_bid_inconsistent": int(book["level_bid"].sum()),
        "n_level_ask_inconsistent": int(book["level_ask"].sum()),
        "n_non_positive_sizes": int(book["nonpos_size"].sum()),
        "n_missing_values": n_missing,
    }


def check_continuity(df: pd.DataFrame) -> dict:
    threshold = CFG.GAP_THRESHOLD_S
    dt = df["dt_s"].dropna()

    gaps = df.loc[dt[dt > threshold].index]
    top_gaps = (
        gaps.assign(gap_s=dt[dt > threshold])
        .nlargest(CFG.TOP_N_GAPS, "gap_s")[["timestamp", "gap_s"]]
    )

    return {
        "session_start": df["timestamp"].iloc[0].isoformat(),
        "session_end": df["timestamp"].iloc[-1].isoformat(),
        "session_duration_s": float(
            (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds()
        ),
        "mean_inter_arrival_s": float(dt.mean()),
        "median_inter_arrival_s": float(dt.median()),
        "p95_inter_arrival_s": float(dt.quantile(0.95)),
        "max_inter_arrival_s": float(dt.max()),
        "gap_threshold_s": threshold,
        "n_gaps_over_threshold": int((dt > threshold).sum()),
        "top_gaps": [
            {"timestamp": row.timestamp.isoformat(), "gap_s": float(row.gap_s)}
            for row in top_gaps.itertuples()
        ],
    }


def ticks_per_minute(df: pd.DataFrame) -> pd.Series:
    return df.set_index("timestamp").resample("1min").size()


def check_liquidity(df: pd.DataFrame) -> dict:
    spread = df["spread"]

    return {
        "spread_mean": float(spread.mean()),
        "spread_median": float(spread.median()),
        "spread_std": float(spread.std()),
        "spread_min": float(spread.min()),
        "spread_max": float(spread.max()),
        "n_negative_spread": int((spread < 0).sum()),
        "n_zero_spread": int((spread == 0).sum()),
        "n_zero_or_negative_sizes": int(book_masks(df)["nonpos_size"].sum()),
        "bid_size_1_median": _finite(zero_as_missing(df["best_bid_size_1"]).median()),
        "ask_size_1_median": _finite(zero_as_missing(df["best_ask_size_1"]).median()),
    }


MAD_CONSISTENCY = 1.4826


def _hampel_mask(x: pd.Series, window: int | None = None, threshold: float | None = None) -> pd.Series:
    if window is None:
        window = CFG.HAMPEL_WINDOW
    if threshold is None:
        threshold = CFG.HAMPEL_THRESHOLD

    active = x[x != 0]
    if active.empty:
        return pd.Series(False, index=x.index)

    min_periods = window // 2 + 1
    median = active.rolling(window, center=True, min_periods=min_periods).median()
    local_mad = (active - median).abs().rolling(window, center=True, min_periods=min_periods).median()

    global_mad = (active - active.median()).abs().median()
    scale = (MAD_CONSISTENCY * local_mad).where(local_mad > 0, MAD_CONSISTENCY * global_mad)
    scale = scale.replace(0, np.nan)

    z = (active - median) / scale
    flags = z.abs() > threshold
    return flags.reindex(x.index, fill_value=False)


def outlier_mask(df: pd.DataFrame) -> pd.Series:
    r = df["dt_norm_return"]
    flags = _hampel_mask(r.dropna())
    mask = pd.Series(False, index=df.index)
    mask.loc[flags.index] = flags
    return mask


def n_outliers(df: pd.DataFrame) -> int:
    r = df["dt_norm_return"].dropna()
    return int(_hampel_mask(r).sum())


def _finite(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _is_constant(x: pd.Series) -> bool:
    if len(x) < 2:
        return True
    std, mean = _finite(x.std()), _finite(x.mean())
    return std is None or mean is None or std <= 1e-9 * max(1.0, abs(mean))


def _run_test(name: str, fn, *args, **kwargs) -> tuple[float | None, float | None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("%s not computed: %s", name, exc)
            return None, None
    stat, pvalue = _finite(out[0]), _finite(out[1])
    if stat is None or pvalue is None:
        logger.warning("%s not computed: statistic or p-value is not finite", name)
        return None, None
    return stat, pvalue


def _ljung_box(x: pd.Series, lags: int) -> tuple[float, float]:
    lb = acorr_ljungbox(x, lags=[lags], return_df=True)
    return float(lb["lb_stat"].iloc[0]), float(lb["lb_pvalue"].iloc[0])


def check_returns(df: pd.DataFrame) -> dict:
    alpha = CFG.SIGNIFICANCE_LEVEL
    lags = CFG.LJUNG_BOX_LAGS

    r = df["dt_norm_return"].dropna()
    n_out = n_outliers(df)

    if _is_constant(r):
        logger.warning("Returns are constant (stuck feed?); distribution and dependence tests not computable")
        skew = kurt = None
        jb_stat = jb_p = lb_stat = lb_p = lbs_stat = lbs_p = adf_stat = adf_p = kpss_stat = kpss_p = None
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            skew = _finite(stats.skew(r))
            kurt = _finite(stats.kurtosis(r))
        jb_stat, jb_p = _run_test("Jarque-Bera", stats.jarque_bera, r)
        lb_stat, lb_p = _run_test("Ljung-Box (returns)", _ljung_box, r, lags)
        lbs_stat, lbs_p = _run_test("Ljung-Box (squared returns)", _ljung_box, r ** 2, lags)
        adf_stat, adf_p = _run_test("ADF (returns)", adfuller, r, autolag="AIC")
        kpss_stat, kpss_p = _run_test("KPSS (returns)", kpss, r, regression="c", nlags="auto")

    return {
        "significance_level": alpha,
        "n_returns": int(len(r)),
        "mean": _finite(r.mean()),
        "std": _finite(r.std()),
        "skew": skew,
        "kurtosis_excess": kurt,
        "jarque_bera_stat": jb_stat,
        "jarque_bera_pvalue": jb_p,
        "jarque_bera_normal": None if jb_p is None else bool(jb_p > alpha),
        "n_outliers_hampel": n_out,
        "outlier_hampel_window": CFG.HAMPEL_WINDOW,
        "outlier_hampel_threshold": CFG.HAMPEL_THRESHOLD,
        "ljung_box_lags": lags,
        "ljung_box_stat": lb_stat,
        "ljung_box_pvalue": lb_p,
        "ljung_box_autocorrelated": None if lb_p is None else bool(lb_p < alpha),
        "ljung_box_sq_stat": lbs_stat,
        "ljung_box_sq_pvalue": lbs_p,
        "ljung_box_sq_vol_clustering": None if lbs_p is None else bool(lbs_p < alpha),
        "adf_stat": adf_stat,
        "adf_pvalue": adf_p,
        "adf_stationary": None if adf_p is None else bool(adf_p < alpha),
        "kpss_stat": kpss_stat,
        "kpss_pvalue": kpss_p,
        "kpss_stationary": None if kpss_p is None else bool(kpss_p > alpha),
    }


def check_price_level(df: pd.DataFrame) -> dict:
    alpha = CFG.SIGNIFICANCE_LEVEL
    p = df["mid"].dropna()
    if _is_constant(p):
        logger.warning("Mid price is constant over the recording (stuck feed?); stationarity tests not computable")
        adf_stat = adf_p = kpss_stat = kpss_p = None
    else:
        adf_stat, adf_p = _run_test("ADF (price level)", adfuller, p, autolag="AIC")
        kpss_stat, kpss_p = _run_test("KPSS (price level)", kpss, p, regression="c", nlags="auto")

    return {
        "start": _finite(p.iloc[0]) if len(p) else None,
        "end": _finite(p.iloc[-1]) if len(p) else None,
        "mean": _finite(p.mean()),
        "std": _finite(p.std()),
        "min": _finite(p.min()),
        "max": _finite(p.max()),
        "adf_stat": adf_stat,
        "adf_pvalue": adf_p,
        "adf_stationary": None if adf_p is None else bool(adf_p < alpha),
        "kpss_stat": kpss_stat,
        "kpss_pvalue": kpss_p,
        "kpss_stationary": None if kpss_p is None else bool(kpss_p > alpha),
    }


MODE_NONE = 0
MODE_NAN = 1
MODE_INTERPOLATE = 2
MODE_FFILL = 3
MODE_ROLLING_MEDIAN = 4
VALID_MODES = {MODE_NONE, MODE_NAN, MODE_INTERPOLATE, MODE_FFILL, MODE_ROLLING_MEDIAN}


def _replace_nan(mid: pd.Series, mask: pd.Series) -> float:
    return np.nan


def _replace_interpolate(mid: pd.Series, mask: pd.Series) -> pd.Series:
    return mid.mask(mask).interpolate(method="linear", limit_direction="both")[mask]


def _replace_ffill(mid: pd.Series, mask: pd.Series) -> pd.Series:
    return mid.mask(mask).ffill()[mask]


def _replace_rolling_median(mid: pd.Series, mask: pd.Series) -> pd.Series:
    window = CFG.HAMPEL_WINDOW
    min_periods = window // 2 + 1
    rolling = mid.rolling(window, center=True, min_periods=min_periods).median()
    return rolling[mask]


_REPLACERS = {
    MODE_NAN: _replace_nan,
    MODE_INTERPOLATE: _replace_interpolate,
    MODE_FFILL: _replace_ffill,
    MODE_ROLLING_MEDIAN: _replace_rolling_median,
}


def _shifted_quote_columns(df: pd.DataFrame) -> list[str]:
    cols = []
    for lvl in present_levels(df):
        cols.extend([f"best_bid_{lvl}", f"best_ask_{lvl}"])
    return cols


def treat_outliers(df: pd.DataFrame, mode: int | None = None) -> pd.DataFrame:
    if mode is None:
        mode = CFG.OUTLIER_REPLACEMENT_MODE
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown outlier replacement mode: {mode!r}, must be one of {sorted(VALID_MODES)}")
    if mode == MODE_NONE:
        return df.copy()

    mask = outlier_mask(df)
    df = df.copy()
    if not mask.any():
        return df

    new_mid = _REPLACERS[mode](df["mid"], mask)
    delta = new_mid - df.loc[mask, "mid"]
    for col in _shifted_quote_columns(df):
        values = df.loc[mask, col]
        shifted = values + delta
        if CFG.IGNORE_ZEROS:
            shifted = shifted.where(values != 0, values)
        df.loc[mask, col] = shifted
    df.loc[mask, "mid"] = new_mid
    df.loc[mask, "spread"] = df.loc[mask, "best_ask_1"] - df.loc[mask, "best_bid_1"]
    df["log_return"], df["dt_norm_return"] = compute_returns(df["mid"], df["dt_s"])
    return df


_FILENAME_PATTERN = re.compile(r"^(\d{8})_([^_]+)(?:_([^_]+))?(?:_([^_]+))?(?:_([^_]+))?$")


@dataclass(frozen=True)
class FeedMetadata:
    date: str
    product: str
    market: str
    server: str
    source: str
    path: Path

    @property
    def instrument_key(self) -> tuple[str, str]:
        return (self.date, self.product)

    @property
    def label(self) -> str:
        parts = [p for p in (self.market, self.server, self.source) if p]
        return "_".join(parts) if parts else self.path.stem


def parse_filename_metadata(path: Path) -> FeedMetadata | None:
    m = _FILENAME_PATTERN.match(path.stem)
    if not m:
        return None
    date, product, market, server, source = m.groups()
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return None
    return FeedMetadata(
        date=date, product=product, market=market or "", server=server or "", source=source or "", path=path,
    )


def group_by_instrument(metas: list[FeedMetadata]) -> dict[tuple[str, str], list[FeedMetadata]]:
    groups: dict[tuple[str, str], list[FeedMetadata]] = {}
    for m in metas:
        groups.setdefault(m.instrument_key, []).append(m)
    return groups


SECONDS_PER_DAY = 86400


def downsample(
    df: pd.DataFrame,
    points_per_day: int | None = None,
    min_rows: int | None = None,
) -> pd.DataFrame:
    if points_per_day is None:
        points_per_day = CFG.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY
    if min_rows is None:
        min_rows = CFG.COMPARISON_DOWNSAMPLE_MIN_ROWS
    if points_per_day <= 0 or len(df) < max(2, min_rows):
        return df

    start, end = df["timestamp"].min(), df["timestamp"].max()
    duration_s = (end - start).total_seconds()
    if duration_s <= 0:
        return df

    target_freq_s = SECONDS_PER_DAY / points_per_day
    target_points = int(duration_s / target_freq_s) + 1
    if len(df) <= target_points:
        return df

    grid = pd.DataFrame({"timestamp": pd.date_range(start, end, freq=pd.Timedelta(seconds=target_freq_s))})
    return pd.merge_asof(grid, df.sort_values("timestamp"), on="timestamp", direction="backward")


def build_aligned_grid(sources: dict[str, pd.DataFrame], max_staleness_s: float | None = None) -> pd.DataFrame:
    freq_s = CFG.COMPARISON_ALIGNMENT_FREQ_S
    if freq_s <= 0:
        raise ValueError(
            f"config.ini [comparison] alignment_freq_s must be positive, got {freq_s}."
        )

    window_start = max(df["timestamp"].iloc[0] for df in sources.values())
    window_end = min(df["timestamp"].iloc[-1] for df in sources.values())
    if window_start >= window_end:
        raise ValueError(
            "Sources have no overlapping time window to compare "
            f"(latest start {window_start.isoformat()} >= earliest end {window_end.isoformat()})."
        )

    window_seconds = (window_end - window_start).total_seconds()
    estimated_points = int(window_seconds / freq_s) + 1
    if estimated_points > CFG.COMPARISON_MAX_GRID_POINTS:
        raise ValueError(
            f"Aligning these sources would need ~{estimated_points:,} grid points "
            f"({window_seconds:,.0f}s overlap at a {freq_s:g}s step), exceeding "
            f"the {CFG.COMPARISON_MAX_GRID_POINTS:,} cap (config.ini [comparison] max_grid_points) -- "
            f"rejected before allocating it. Narrow the input time range or raise "
            f"alignment_freq_s."
        )

    grid = pd.DataFrame({
        "timestamp": pd.date_range(window_start, window_end, freq=pd.Timedelta(seconds=freq_s))
    })
    tolerance = pd.Timedelta(
        seconds=max_staleness_s if max_staleness_s is not None else CFG.COMPARISON_MAX_STALENESS_S
    )

    aligned = grid.copy()
    for name, df in sources.items():
        src = df[["timestamp", "mid", "spread"]].sort_values("timestamp")
        merged = pd.merge_asof(grid, src, on="timestamp", direction="backward", tolerance=tolerance)
        aligned[f"mid__{name}"] = merged["mid"]
        aligned[f"spread__{name}"] = merged["spread"]
    return aligned


def _safe_float(x) -> float | None:
    x = float(x)
    return None if np.isnan(x) else x


def compare_pair(aligned: pd.DataFrame, name_a: str, name_b: str) -> dict:
    a, b, ts = aligned[f"mid__{name_a}"], aligned[f"mid__{name_b}"], aligned["timestamp"]
    both = a.notna() & b.notna()
    n_common = int(both.sum())

    result = {
        "source_a": name_a,
        "source_b": name_b,
        "n_grid_points": int(len(aligned)),
        "n_common_points": n_common,
        "coverage": n_common / len(aligned) if len(aligned) else 0.0,
    }
    if n_common < 2:
        return result

    a_c, b_c, ts_c = a[both], b[both], ts[both]
    diff = a_c - b_c
    mid_ref = ((a_c + b_c) / 2).replace(0, np.nan)
    diff_bps = (diff / mid_ref) * 1e4

    ret_a, ret_b = np.log(a_c.where(a_c > 0)).diff(), np.log(b_c.where(b_c > 0)).diff()
    valid_ret = ret_a.notna() & ret_b.notna()
    return_correlation = None
    if valid_ret.sum() > 1:
        return_correlation = _safe_float(ret_a[valid_ret].corr(ret_b[valid_ret]))

    exceed = diff_bps.abs() > CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS
    top_idx = diff_bps[exceed].abs().nlargest(CFG.COMPARISON_TOP_N_DIVERGENCES).index
    top_divergences = [
        {
            "timestamp": ts_c.loc[i].isoformat(),
            "mid_a": float(a_c.loc[i]),
            "mid_b": float(b_c.loc[i]),
            "diff": float(diff.loc[i]),
            "diff_bps": float(diff_bps.loc[i]),
        }
        for i in top_idx
    ]

    result.update({
        "mean_diff": float(diff.mean()),
        "mean_abs_diff": float(diff.abs().mean()),
        "median_abs_diff": float(diff.abs().median()),
        "rms_diff": float(np.sqrt((diff ** 2).mean())),
        "max_abs_diff": float(diff.abs().max()),
        "mean_abs_diff_bps": _safe_float(diff_bps.abs().mean()),
        "max_abs_diff_bps": _safe_float(diff_bps.abs().max()),
        "return_correlation": return_correlation,
        "n_divergences": int(exceed.sum()),
        "top_divergences": top_divergences,
    })
    return result


def compute_status(report: dict) -> str:
    return "OK" if all(pair.get("n_divergences", 1) == 0 for pair in report["pairs"]) else "KO"


def compare_sources(sources: dict[str, pd.DataFrame], max_staleness_s: float | None = None) -> tuple[dict, pd.DataFrame]:
    if len(sources) < 2:
        raise ValueError(f"Need at least 2 sources to compare, got {len(sources)}.")

    aligned = build_aligned_grid(sources, max_staleness_s=max_staleness_s)

    source_summaries = [
        {
            "source": name,
            "n_rows": int(len(df)),
            "session_start": df["timestamp"].iloc[0].isoformat(),
            "session_end": df["timestamp"].iloc[-1].isoformat(),
        }
        for name, df in sources.items()
    ]
    pairs = [compare_pair(aligned, a, b) for a, b in itertools.combinations(sources.keys(), 2)]

    return {
        "sources": source_summaries,
        "alignment_freq_s": CFG.COMPARISON_ALIGNMENT_FREQ_S,
        "max_staleness_s": max_staleness_s if max_staleness_s is not None else CFG.COMPARISON_MAX_STALENESS_S,
        "price_diff_threshold_bps": CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS,
        "n_grid_points": int(len(aligned)),
        "window_start": aligned["timestamp"].iloc[0].isoformat(),
        "window_end": aligned["timestamp"].iloc[-1].isoformat(),
        "pairs": pairs,
    }, aligned


def attribute_divergence(pairs: list[dict], metadata: dict[str, FeedMetadata]) -> dict:
    usable = [p for p in pairs if p.get("mean_abs_diff_bps") is not None]

    scores: dict[str, list[float]] = {}
    for p in usable:
        scores.setdefault(p["source_a"], []).append(p["mean_abs_diff_bps"])
        scores.setdefault(p["source_b"], []).append(p["mean_abs_diff_bps"])
    source_outlier_ranking = sorted(
        (
            {"source": name, "avg_diff_vs_others_bps": sum(vals) / len(vals), "n_pairs": len(vals)}
            for name, vals in scores.items()
        ),
        key=lambda r: r["avg_diff_vs_others_bps"], reverse=True,
    )

    dimension_attribution = []
    for dim in ("market", "server", "source"):
        distinct_values = sorted({getattr(metadata[name], dim) for name in scores})
        if len(distinct_values) < 2:
            continue

        differs, matches = [], []
        for p in usable:
            meta_a, meta_b = metadata[p["source_a"]], metadata[p["source_b"]]
            bucket = differs if getattr(meta_a, dim) != getattr(meta_b, dim) else matches
            bucket.append(p["mean_abs_diff_bps"])
        if not differs or not matches:
            continue

        dimension_attribution.append({
            "dimension": dim,
            "distinct_values": distinct_values,
            "avg_diff_bps_when_differs": sum(differs) / len(differs),
            "avg_diff_bps_when_matches": sum(matches) / len(matches),
            "n_pairs_differs": len(differs),
            "n_pairs_matches": len(matches),
        })
    dimension_attribution.sort(
        key=lambda r: r["avg_diff_bps_when_differs"] - r["avg_diff_bps_when_matches"], reverse=True
    )

    return {"source_outlier_ranking": source_outlier_ranking, "dimension_attribution": dimension_attribution}


SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_1 = "#2a78d6"
CRITICAL = "#d03b3b"
FLAG = "#6250d6"

SOURCE_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]

FIGSIZE = (6.4, 3.6)
_FONT = {"family": "sans-serif", "size": 9}


def _style_axes(ax):
    ax.set_facecolor(SURFACE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(BASELINE)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=INK_SECONDARY, labelsize=8.5, length=3)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.title.set_color(INK_PRIMARY)
    ax.title.set_fontsize(10)
    ax.title.set_fontweight("semibold")
    ax.xaxis.label.set_color(INK_SECONDARY)
    ax.yaxis.label.set_color(INK_SECONDARY)
    ax.xaxis.label.set_fontsize(9)
    ax.yaxis.label.set_fontsize(9)


def _new_fig(figsize=FIGSIZE):
    fig, ax = plt.subplots(figsize=figsize, facecolor=SURFACE)
    _style_axes(ax)
    return fig, ax


def _legend(ax):
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, loc="best")


def _format_datetime_axis(ax):
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))


def _save(fig, out_dir: str, name: str) -> str:
    path = os.path.join(out_dir, name)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


class FigureNotComputable(Exception):
    pass


def _returns_for_figure(df: pd.DataFrame) -> pd.Series:
    r = df["dt_norm_return"].dropna()
    if _is_constant(r):
        raise FigureNotComputable("returns are constant or undefined")
    return r


def _placeholder_figure(out_dir: str, name: str, title: str, reason: str) -> str:
    fig, ax = _new_fig()
    ax.set_title(title)
    ax.text(0.5, 0.5, f"Not computable: {reason}", ha="center", va="center",
            transform=ax.transAxes, color=INK_SECONDARY)
    ax.set_xticks([])
    ax.set_yticks([])
    return _save(fig, out_dir, name)


def plot_price(df: pd.DataFrame, out_dir: str) -> str:
    fig, ax = _new_fig()
    ax.fill_between(df["timestamp"], df["best_bid_1"], df["best_ask_1"],
                    color=SERIES_1, alpha=0.12, linewidth=0, label="Bid/ask range")
    ax.plot(df["timestamp"], df["mid"], color=SERIES_1, linewidth=1.2, label="Mid price")

    mask = outlier_mask(df)
    if mask.any():
        ax.scatter(df.loc[mask, "timestamp"], df.loc[mask, "mid"],
                   s=22, facecolors=FLAG, edgecolors=SURFACE, linewidths=1.0, zorder=5,
                   label="Return outlier (rolling Hampel filter)")

    ax.set_title("Mid price with bid/ask range")
    ax.set_ylabel("Price")
    _legend(ax)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "01_price_timeseries.png")


def plot_spread(df: pd.DataFrame, out_dir: str) -> str:
    fig, ax = _new_fig()
    ax.plot(df["timestamp"], df["spread"], color=SERIES_1, linewidth=1.0)
    ax.axhline(0, color=BASELINE, linewidth=1)

    bad = df["spread"] <= 0
    if bad.any():
        ax.scatter(df.loc[bad, "timestamp"], df.loc[bad, "spread"],
                   s=22, facecolors=CRITICAL, edgecolors=SURFACE, linewidths=1.0, zorder=5,
                   label="Non-positive spread (exception)")
        _legend(ax)

    ax.set_title("Best bid/ask spread")
    ax.set_ylabel("Spread")
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "02_spread_timeseries.png")


def plot_interarrival(df: pd.DataFrame, out_dir: str) -> str:
    gap_threshold = CFG.GAP_THRESHOLD_S
    dt = df["dt_s"].dropna()
    dt = dt[dt > 0]
    fig, ax = _new_fig()
    lo, hi = float(dt.min()), float(dt.max())
    if hi <= lo:
        hi = lo * 10 if lo > 0 else 1.0
    bins = np.logspace(np.log10(lo), np.log10(hi), 40)
    ax.hist(dt, bins=bins, color=SERIES_1, alpha=0.9, edgecolor=SURFACE, linewidth=0.5)
    ax.axvline(dt.median(), color=INK_MUTED, linewidth=1.0,
               label=f"Median = {dt.median():.2f} s")
    ax.axvline(gap_threshold, color=CRITICAL, linewidth=1.0,
               label=f"Gap threshold = {gap_threshold:g} s")
    ax.set_xscale("log")
    ax.set_title("Inter-arrival time distribution")
    ax.set_xlabel("Seconds between ticks (log scale)")
    ax.set_ylabel("Ticks")
    _legend(ax)
    return _save(fig, out_dir, "03_interarrival_hist.png")


def plot_return_hist(df: pd.DataFrame, out_dir: str) -> str:
    r = _returns_for_figure(df)
    fig, ax = _new_fig()
    ax.hist(r, bins=60, density=True, color=SERIES_1, alpha=0.9,
            edgecolor=SURFACE, linewidth=0.4, label="Observed")
    x = np.linspace(r.min(), r.max(), 200)
    ax.plot(x, stats.norm.pdf(x, r.mean(), r.std()), color=INK_MUTED,
            linewidth=1.2, label="Normal with same mean/std")
    ax.set_title("Time-normalized log-return distribution")
    ax.set_xlabel("Log-return / sqrt(dt)")
    ax.set_ylabel("Density")
    _legend(ax)
    return _save(fig, out_dir, "04_return_hist.png")


def plot_return_qq(df: pd.DataFrame, out_dir: str) -> str:
    r = _returns_for_figure(df)
    fig, ax = _new_fig()
    (osm, osr), (slope, intercept, _) = stats.probplot(r, dist="norm")
    ax.scatter(osm, osr, color=SERIES_1, s=8, alpha=0.7, linewidths=0, label="Returns")
    ax.plot(osm, slope * osm + intercept, color=INK_MUTED, linewidth=1.2,
            label="Normal reference")
    ax.set_title("Normal Q-Q plot of returns")
    ax.set_xlabel("Theoretical quantiles")
    ax.set_ylabel("Sample quantiles")
    _legend(ax)
    return _save(fig, out_dir, "05_return_qq.png")


def _plot_acf_series(series: pd.Series, title: str, out_dir: str, name: str, nlags: int) -> str:
    fig, ax = _new_fig()
    conf = 1.96 / np.sqrt(len(series))
    vals = acf(series, nlags=nlags, fft=True)
    lags = np.arange(len(vals))
    ax.axhspan(-conf, conf, color=GRID, alpha=0.9, linewidth=0, label="95% band under no autocorrelation")
    ax.vlines(lags[1:], 0, vals[1:], color=SERIES_1, linewidth=1.2)
    ax.scatter(lags[1:], vals[1:], color=SERIES_1, s=12, zorder=5, linewidths=0)
    ax.axhline(0, color=BASELINE, linewidth=1)
    ax.set_title(title)
    ax.set_xlabel("Lag")
    ax.set_ylabel("Autocorrelation")
    _legend(ax)
    return _save(fig, out_dir, name)


def plot_acf_returns(df: pd.DataFrame, out_dir: str, nlags: int = 20) -> str:
    r = _returns_for_figure(df)
    return _plot_acf_series(r, "Autocorrelation of returns", out_dir, "06_acf_returns.png", nlags)


def plot_acf_squared_returns(df: pd.DataFrame, out_dir: str, nlags: int = 20) -> str:
    r = _returns_for_figure(df)
    return _plot_acf_series(r ** 2, "Autocorrelation of squared returns (volatility clustering)",
                            out_dir, "07_acf_squared_returns.png", nlags)


def plot_activity(df: pd.DataFrame, out_dir: str) -> str:
    counts = ticks_per_minute(df)
    fig, ax = _new_fig()
    ax.fill_between(counts.index, counts.values, color=SERIES_1, alpha=0.12, linewidth=0)
    ax.plot(counts.index, counts.values, color=SERIES_1, linewidth=1.0)
    ax.set_title("Tick activity per minute")
    ax.set_ylabel("Ticks / minute")
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "08_activity_profile.png")


def generate_all_plots(df: pd.DataFrame, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    plt.rcParams["font.family"] = _FONT["family"]
    plt.rcParams["font.size"] = _FONT["size"]
    figures = [
        (plot_price, "01_price_timeseries.png", "Mid price with bid/ask range"),
        (plot_spread, "02_spread_timeseries.png", "Best bid/ask spread"),
        (plot_interarrival, "03_interarrival_hist.png", "Inter-arrival time distribution"),
        (plot_return_hist, "04_return_hist.png", "Time-normalized log-return distribution"),
        (plot_return_qq, "05_return_qq.png", "Normal Q-Q plot of returns"),
        (plot_acf_returns, "06_acf_returns.png", "Autocorrelation of returns"),
        (plot_acf_squared_returns, "07_acf_squared_returns.png", "Autocorrelation of squared returns (volatility clustering)"),
        (plot_activity, "08_activity_profile.png", "Tick activity per minute"),
    ]
    paths = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for fn, name, title in figures:
            try:
                paths.append(fn(df, out_dir))
            except FigureNotComputable as exc:
                plt.close("all")
                paths.append(_placeholder_figure(out_dir, name, title, str(exc)))
            except Exception as exc:
                plt.close("all")
                logger.warning("Figure %s could not be drawn: %s", name, exc)
                paths.append(_placeholder_figure(out_dir, name, title, "see the pipeline log"))
    return paths


def plot_comparison_overlay(aligned: pd.DataFrame, source_names: list[str], out_dir: str) -> str:
    fig, ax = _new_fig()
    for i, name in enumerate(source_names):
        ax.plot(aligned["timestamp"], aligned[f"mid__{name}"], color=SOURCE_COLORS[i % len(SOURCE_COLORS)],
                linewidth=1.0, alpha=0.95, label=name)
    ax.set_title("Mid price by source (aligned grid)")
    ax.set_ylabel("Price")
    _legend(ax)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "c1_price_overlay.png")


def plot_comparison_diff(aligned: pd.DataFrame, name_a: str, name_b: str, threshold_bps: float, out_dir: str) -> str:
    a, b = aligned[f"mid__{name_a}"], aligned[f"mid__{name_b}"]
    mid_ref = ((a + b) / 2).replace(0, np.nan)
    diff_bps = (a - b) / mid_ref * 1e4

    fig, ax = _new_fig()
    ax.plot(aligned["timestamp"], diff_bps, color=SERIES_1, linewidth=0.9)
    ax.axhline(0, color=BASELINE, linewidth=1)
    ax.axhline(threshold_bps, color=CRITICAL, linewidth=0.9,
               label=f"+/-{threshold_bps:g} bps divergence threshold")
    ax.axhline(-threshold_bps, color=CRITICAL, linewidth=0.9)
    ax.set_title(f"Mid price difference (bps)\n{name_a} minus {name_b}")
    ax.set_ylabel("Difference (bps)")
    _legend(ax)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, f"c2_diff_{name_a}_vs_{name_b}.png")


def plot_comparison_diff_hist(aligned: pd.DataFrame, name_a: str, name_b: str, out_dir: str) -> str:
    a, b = aligned[f"mid__{name_a}"], aligned[f"mid__{name_b}"]
    mid_ref = ((a + b) / 2).replace(0, np.nan)
    diff_bps = ((a - b) / mid_ref * 1e4).dropna()

    fig, ax = _new_fig()
    ax.hist(diff_bps, bins=60, color=SERIES_1, alpha=0.9, edgecolor=SURFACE, linewidth=0.4)
    if len(diff_bps):
        ax.axvline(diff_bps.median(), color=INK_MUTED, linewidth=1.0,
                   label=f"Median = {diff_bps.median():.2f} bps")
        _legend(ax)
    ax.set_title(f"Difference distribution (bps)\n{name_a} minus {name_b}")
    ax.set_xlabel("Difference (bps)")
    ax.set_ylabel("Grid points")
    return _save(fig, out_dir, f"c3_diff_hist_{name_a}_vs_{name_b}.png")


def generate_all_comparison_plots(aligned: pd.DataFrame, source_names: list[str],
                                  threshold_bps: float, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    plt.rcParams["font.family"] = _FONT["family"]
    plt.rcParams["font.size"] = _FONT["size"]
    paths = [plot_comparison_overlay(aligned, source_names, out_dir)]
    for a, b in itertools.combinations(source_names, 2):
        paths.append(plot_comparison_diff(aligned, a, b, threshold_bps, out_dir))
        paths.append(plot_comparison_diff_hist(aligned, a, b, out_dir))
    return paths


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

_EXCEPTION_MASKS = {
    "n_duplicate_timestamps": "dup_ts",
    "n_out_of_order_timestamps": "out_of_order",
    "n_crossed_book": "crossed",
    "n_level_bid_inconsistent": "level_bid",
    "n_level_ask_inconsistent": "level_ask",
    "n_non_positive_sizes": "nonpos_size",
    "n_missing_values": "missing",
    "n_gaps_over_threshold": "gap",
    "n_negative_spread": "neg_spread",
    "n_zero_or_negative_sizes": "nonpos_size",
}

_FLAG_DEFS = [
    (1, "crossed", "Crossed book (bid >= ask)", "exception"),
    (2, "nonpos_size", "Non-positive order size", "exception"),
    (4, "missing", "Missing value", "exception"),
    (8, "level_bid", "Level-depth bid inconsistency", "exception"),
    (16, "level_ask", "Level-depth ask inconsistency", "exception"),
    (32, "dup_ts", "Duplicate timestamp", "exception"),
    (64, "out_of_order", "Out-of-order timestamp", "exception"),
    (128, "gap", "Silent gap ends here", "gap"),
    (256, "outlier", "Return outlier (Hampel)", "outlier"),
]


def count_exceptions(report: dict) -> tuple[int, int]:
    counts = [report[section][key] for section, key, _ in EXCEPTION_METRICS]
    return sum(1 for c in counts if c > 0), len(counts)


def _epoch_ms(ts: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    return ((pd.Index(ts) - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)).to_numpy(dtype="int64")


def _ms(ts) -> int:
    return int((pd.Timestamp(ts) - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1))


def _col(values, decimals: int | None = None) -> list:
    arr = np.asarray(values, dtype="float64")
    if decimals is not None:
        arr = np.round(arr, decimals)
    out = arr.tolist()
    return [None if (v != v) else v for v in out]


def _select_rows(primary: np.ndarray, flags: np.ndarray, max_points: int) -> np.ndarray:
    n = len(primary)
    if n <= max_points:
        return np.arange(n)
    n_buckets = max(1, max_points // 2)
    bucket = (np.arange(n) * n_buckets) // n
    finite = np.isfinite(primary)
    lo = pd.Series(np.where(finite, primary, np.inf))
    hi = pd.Series(np.where(finite, primary, -np.inf))
    keep = [lo.groupby(bucket).idxmin().to_numpy(), hi.groupby(bucket).idxmax().to_numpy()]
    flagged = np.flatnonzero(flags != 0)
    if len(flagged) > max_points:
        flagged = flagged[np.linspace(0, len(flagged) - 1, max_points).astype(int)]
    keep.append(flagged)
    keep.append(np.array([0, n - 1]))
    return np.unique(np.concatenate(keep))


def _infer_price_decimals(df_raw: pd.DataFrame) -> int:
    sample = pd.concat([df_raw["best_bid_1"], df_raw["best_ask_1"]]).dropna().head(4000)
    best = 2
    for v in sample.to_numpy():
        s = repr(float(v))
        if "e" in s or "." not in s:
            continue
        best = max(best, len(s.split(".")[1]))
    return min(best, 8)


def build_report_chart_payload(df: pd.DataFrame, df_raw: pd.DataFrame, report: dict) -> dict:
    n = len(df)
    order = np.argsort(df_raw["timestamp"].to_numpy(), kind="stable")
    raw_diffs = df_raw["timestamp"].diff().dt.total_seconds().to_numpy()
    book = book_masks(df_raw)

    raw_masks = {
        "crossed": (df_raw["best_bid_1"] >= df_raw["best_ask_1"]).to_numpy(),
        "nonpos_size": book["nonpos_size"].to_numpy(),
        "missing": df_raw[raw_schema_columns(df_raw)].isna().any(axis=1).to_numpy(),
        "level_bid": book["level_bid"].to_numpy(),
        "level_ask": book["level_ask"].to_numpy(),
        "dup_ts": np.nan_to_num(raw_diffs, nan=1.0) == 0,
        "out_of_order": np.nan_to_num(raw_diffs, nan=1.0) < 0,
    }
    masks = {k: v[order] for k, v in raw_masks.items()}
    dt = df["dt_s"].to_numpy()
    masks["gap"] = np.nan_to_num(dt, nan=0.0) > CFG.GAP_THRESHOLD_S
    masks["outlier"] = outlier_mask(df).to_numpy()
    masks["neg_spread"] = (df["spread"] < 0).to_numpy()

    flags = np.zeros(n, dtype="int64")
    for bit, key, _, _ in _FLAG_DEFS:
        flags |= masks[key].astype("int64") * bit

    t_ms = _epoch_ms(df["timestamp"])
    keep = _select_rows(df["mid"].to_numpy(dtype="float64"), flags, CFG.DASHBOARD_MAX_CHART_POINTS)
    sub = df.iloc[keep]
    ticks = {
        "t": t_ms[keep].tolist(),
        "bid": _col(sub["best_bid_1"], 8),
        "ask": _col(sub["best_ask_1"], 8),
        "mid": _col(sub["mid"], 8),
        "spread": _col(sub["spread"], 8),
        "bs": _col(zero_as_missing(sub["best_bid_size_1"]), 4),
        "as": _col(zero_as_missing(sub["best_ask_size_1"]), 4),
        "r": _col(sub["dt_norm_return"], 7),
        "dt": _col(sub["dt_s"], 6),
        "f": flags[keep].tolist(),
    }
    activity = ticks_per_minute(df)
    activity_tbl = {"t": _epoch_ms(activity.index).tolist(), "n": activity.to_numpy(dtype="float64").tolist()}

    alerts = []
    ts_iso = df["timestamp"]
    for section, key, label in EXCEPTION_METRICS:
        count = int(report[section][key])
        mask = masks[_EXCEPTION_MASKS[key]]
        idx = np.flatnonzero(mask)
        alerts.append({
            "key": key,
            "label": label,
            "count": count,
            "rows": int(len(idx)),
            "share": float(len(idx) / n) if n else 0.0,
            "first": ts_iso.iloc[idx[0]].isoformat() if len(idx) else None,
            "last": ts_iso.iloc[idx[-1]].isoformat() if len(idx) else None,
        })

    present_flags = [
        {"bit": bit, "key": key, "label": label, "lane": lane}
        for bit, key, label, lane in _FLAG_DEFS
        if (masks[key].any() or lane == "exception")
    ]
    if not masks["gap"].any():
        present_flags = [f for f in present_flags if f["lane"] != "gap"]
    if not masks["outlier"].any():
        present_flags = [f for f in present_flags if f["lane"] != "outlier"]

    s1, s2 = SOURCE_COLORS[0], SOURCE_COLORS[1]
    modes = [
        {"id": "mid", "label": "Mid price", "table": "ticks",
         "series": [{"key": "mid", "label": "Mid price", "color": s1, "kind": "line"}],
         "band": {"lo": "bid", "hi": "ask", "label": "Bid/ask range"},
         "y": {"label": "Price", "zero": False}, "markers": True},
        {"id": "spread", "label": "Spread", "table": "ticks",
         "series": [{"key": "spread", "label": "Spread (ask - bid)", "color": s1, "kind": "line"}],
         "y": {"label": "Spread", "zero": True}, "markers": True},
        {"id": "returns", "label": "Returns", "table": "ticks",
         "series": [{"key": "r", "label": "Log-return / sqrt(dt)", "color": s1, "kind": "stem"}],
         "y": {"label": "Return / sqrt(dt)", "zero": True}, "markers": True},
        {"id": "bidask", "label": "L1 bid / ask", "table": "ticks",
         "series": [{"key": "bid", "label": "Best bid (L1)", "color": s1, "kind": "step"},
                    {"key": "ask", "label": "Best ask (L1)", "color": s2, "kind": "step"}],
         "y": {"label": "Price", "zero": False}, "markers": True},
        {"id": "sizes", "label": "L1 sizes", "table": "ticks",
         "series": [{"key": "bs", "label": "Bid size (L1)", "color": s1, "kind": "step"},
                    {"key": "as", "label": "Ask size (L1)", "color": s2, "kind": "step"}],
         "y": {"label": "Size", "zero": True}, "markers": False},
        {"id": "activity", "label": "Activity", "table": "activity",
         "series": [{"key": "n", "label": "Ticks per minute", "color": s1, "kind": "column", "width_ms": 60000}],
         "y": {"label": "Ticks / minute", "zero": True}, "markers": False},
    ]
    readout = [
        {"key": "bid", "label": "Bid", "fmt": "price"},
        {"key": "ask", "label": "Ask", "fmt": "price"},
        {"key": "mid", "label": "Mid", "fmt": "price"},
        {"key": "spread", "label": "Spread", "fmt": "price"},
        {"key": "bs", "label": "Bid size", "fmt": "int"},
        {"key": "as", "label": "Ask size", "fmt": "int"},
        {"key": "dt", "label": "dt (s)", "fmt": "sec"},
        {"key": "r", "label": "Return / sqrt(dt)", "fmt": "sci"},
    ]
    return {
        "kind": "report",
        "meta": {
            "t0": int(t_ms[0]), "t1": int(t_ms[-1]), "n_ticks": int(n), "n_points": int(len(keep)),
            "bucketed": bool(len(keep) < n), "price_decimals": _infer_price_decimals(df_raw),
            "gap_threshold_s": CFG.GAP_THRESHOLD_S,
        },
        "tables": {"ticks": ticks, "activity": activity_tbl},
        "nav": {"table": "ticks", "key": "mid"},
        "flags": present_flags,
        "modes": modes,
        "readout": readout,
        "alerts": alerts,
    }


def build_comparison_chart_payload(aligned: pd.DataFrame, source_names: list[str], price_decimals: int = 5) -> dict:
    n = len(aligned)
    thr = CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS
    t_ms = _epoch_ms(aligned["timestamp"])
    mids = [aligned[f"mid__{name}"].to_numpy(dtype="float64") for name in source_names]
    pairs = list(itertools.combinations(range(len(source_names)), 2))

    flags = np.zeros(n, dtype="int64")
    flag_defs, diffs = [], {}
    for k, (i, j) in enumerate(pairs):
        ref = (mids[i] + mids[j]) / 2
        ref = np.where(ref == 0, np.nan, ref)
        d = (mids[i] - mids[j]) / ref * 1e4
        diffs[(i, j)] = d
        bit = 1 << k
        flags |= (np.abs(np.nan_to_num(d, nan=0.0)) > thr).astype("int64") * bit
        flag_defs.append({"bit": bit, "key": f"d{i}_{j}", "lane": "exception",
                          "label": f"Divergence over {thr:g} bps: {source_names[i]} vs {source_names[j]}"})

    keep = _select_rows(mids[0], flags, CFG.DASHBOARD_MAX_CHART_POINTS)
    grid = {"t": t_ms[keep].tolist(), "f": flags[keep].tolist()}
    for i in range(len(source_names)):
        grid[f"m{i}"] = _col(mids[i][keep], 8)
    for (i, j), d in diffs.items():
        grid[f"d{i}_{j}"] = _col(d[keep], 4)

    modes = [{
        "id": "mid", "label": "Mid by source", "table": "grid",
        "series": [{"key": f"m{i}", "label": name, "color": SOURCE_COLORS[i % len(SOURCE_COLORS)], "kind": "line"}
                   for i, name in enumerate(source_names)],
        "y": {"label": "Mid price", "zero": False}, "markers": False,
    }]
    for (i, j) in pairs:
        a, b = source_names[i], source_names[j]
        modes.append({
            "id": f"d{i}_{j}", "label": f"Diff: {a} - {b}", "table": "grid",
            "series": [{"key": f"d{i}_{j}", "label": f"{a} minus {b} (bps)", "color": SOURCE_COLORS[0], "kind": "line"}],
            "y": {"label": "Difference (bps)", "zero": True}, "markers": True,
            "thresholds": [{"v": thr, "label": f"+{thr:g} bps threshold"}, {"v": -thr, "label": f"-{thr:g} bps"}],
        })
    readout = [{"key": f"m{i}", "label": name, "fmt": "price"} for i, name in enumerate(source_names)]
    if len(pairs) <= 6:
        readout += [{"key": f"d{i}_{j}", "label": f"{source_names[i]} - {source_names[j]} (bps)", "fmt": "bps"}
                    for (i, j) in pairs]

    return {
        "kind": "compare",
        "meta": {
            "t0": int(t_ms[0]), "t1": int(t_ms[-1]), "n_ticks": int(n), "n_points": int(len(keep)),
            "bucketed": bool(len(keep) < n), "price_decimals": price_decimals,
        },
        "tables": {"grid": grid},
        "nav": {"table": "grid", "key": "m0"},
        "flags": flag_defs,
        "modes": modes,
        "readout": readout,
    }


def _esc(x) -> str:
    return html.escape(str(x))


def _num(x, decimals=4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        if x != x:
            return "n/a"
        return f"{x:,.{decimals}g}"
    if isinstance(x, int):
        return f"{x:,}"
    return html.escape(str(x))


def _int(x) -> str:
    return "n/a" if x is None else f"{int(x):,}"


def _fixed(x, decimals: int) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "n/a"
    return f"{x:,.{decimals}f}"


def _pct(x, decimals: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{decimals}f}%"


def _pvalue(p) -> str:
    if p is None or (isinstance(p, float) and p != p):
        return "n/a"
    if p < 0.001:
        return "&lt; 0.001"
    return f"{p:.3f}"


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{seconds:.3f}s" if seconds < 10 else f"{s}s"


def _fmt_ts(iso: str, with_date: bool = False) -> str:
    ts = pd.Timestamp(iso)
    s = ts.strftime("%H:%M:%S")
    if ts.microsecond:
        s += f".{ts.microsecond // 1000:03d}"
    return f"{ts.strftime('%Y-%m-%d')} {s}" if with_date else s


def _goto(iso: str, with_date: bool = False) -> str:
    return (f'<a class="goto" href="#explorer" data-t="{_ms(iso)}" '
            f'title="Show this moment in the explorer">{_esc(_fmt_ts(iso, with_date))}</a>')


def _row(label: str, value: str, note: str = "") -> str:
    note_html = f'<td class="note-cell">{_esc(note)}</td>' if note else "<td></td>"
    return f'<tr><td>{_esc(label)}</td><td class="num">{value}</td>{note_html}</tr>'


def _kv(label: str, value: str) -> str:
    return f'<tr><td>{_esc(label)}</td><td class="num">{value}</td></tr>'


def _kv_text(label: str, value: str) -> str:
    return f'<tr><td class="kv-label">{_esc(label)}</td><td>{value}</td></tr>'


def _exception_row(label: str, count: int) -> str:
    if count > 0:
        return (f'<tr class="exception"><td>{_esc(label)}</td><td class="num">{_int(count)}</td>'
                f'<td class="status"><span class="badge badge-critical">Exception</span></td></tr>')
    return (f'<tr><td>{_esc(label)}</td><td class="num">{_int(count)}</td>'
            f'<td class="status"><span class="badge badge-ok">OK</span></td></tr>')


def _table(headers: list[str], rows: str, num_cols: set[int] = frozenset(), cls: str = "") -> str:
    head = "".join(
        f'<th class="num">{_esc(h)}</th>' if i in num_cols else f"<th>{_esc(h)}</th>"
        for i, h in enumerate(headers)
    )
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<table{cls_attr}><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'


def _tile(label: str, value: str, sub: str = "", alert: bool = False) -> str:
    cls = "tile alert" if alert else "tile"
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return f'<div class="{cls}"><div class="label">{_esc(label)}</div><div class="value">{value}</div>{sub_html}</div>'


def _figure(plot_paths: list[str], name: str, caption: str) -> str:
    path = next((p for p in plot_paths if Path(p).name == name), None)
    if not path:
        return ""
    return f'<figure><img src="{_esc(path)}" alt="{_esc(caption)}"><figcaption>{_esc(caption)}</figcaption></figure>'


_STYLE = """
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb;
    --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10); --gap: #d8d7d0;
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4; --s6: #008300; --s7: #6250d6; --s8: #e34948;
    --critical: #d03b3b; --good: #0ca30c; --good-text: #006300;
  }
  * { box-sizing: border-box; }
  html { background: var(--page); }
  body {
    margin: 0; padding: 24px 32px 48px; color: var(--ink);
    font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header.report-header {
    display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
    border-bottom: 2px solid var(--ink); padding-bottom: 10px; margin-bottom: 18px;
  }
  header.report-header h1 { font-size: 18px; font-weight: 600; margin: 0 0 2px; }
  header.report-header .tag { color: var(--ink-3); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
  header.report-header .meta { text-align: right; color: var(--ink-2); font-size: 12px; line-height: 1.5; }
  header.report-header .meta strong { color: var(--ink); font-weight: 600; }
  section.block { background: var(--surface); border: 1px solid var(--border); padding: 14px 16px 16px; margin-bottom: 16px; }
  h2 { font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-2); margin: 0 0 10px; }
  h3 { font-size: 12px; font-weight: 600; color: var(--ink); margin: 16px 0 6px; }
  h3:first-of-type { margin-top: 0; }
  p.note { color: var(--ink-3); font-size: 12px; margin: 6px 0 8px; }
  .columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; align-items: start; }
  .columns > div { min-width: 0; }
  .column-title { font-size: 12px; font-weight: 600; color: var(--ink); letter-spacing: 0.02em; margin: 0 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--ink); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 4px 6px; border-bottom: 1px solid var(--grid); text-align: left; vertical-align: top; }
  th { color: var(--ink-3); font-weight: 500; font-size: 11px; letter-spacing: 0.03em; text-transform: uppercase; border-bottom: 1px solid var(--baseline); white-space: nowrap; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.status { white-space: nowrap; text-align: right; }
  td.note-cell { color: var(--ink-3); font-size: 12px; }
  td.kv-label { white-space: nowrap; width: 1%; padding-right: 14px; }
  tr.exception td { color: var(--critical); font-weight: 600; }
  .badge { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; }
  .badge::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }
  .badge-critical { color: var(--critical); }
  .badge-ok { color: var(--good-text); }
  .badge-muted { color: var(--ink-3); }
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
  .tile { border: 1px solid var(--border); padding: 10px 12px; min-width: 0; }
  .tile .label { font-size: 11px; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.06em; }
  .tile .value { font-size: 24px; font-weight: 600; line-height: 1.2; margin-top: 2px; color: var(--ink); white-space: nowrap; }
  .tile .value small { font-size: 13px; font-weight: 500; color: var(--ink-2); }
  .tile .sub { font-size: 12px; color: var(--ink-2); margin-top: 2px; }
  .tile.alert .value { color: var(--critical); }
  a.goto { color: var(--ink); text-decoration: underline; text-decoration-color: var(--baseline); text-underline-offset: 2px; cursor: pointer; font-variant-numeric: tabular-nums; }
  a.goto:hover { text-decoration-color: var(--ink); }
  figure { margin: 12px 0 0; }
  figure img { width: 100%; display: block; border: 1px solid var(--grid); }
  figcaption { font-size: 11px; color: var(--ink-2); margin-top: 4px; }
  .empty { color: var(--ink-3); font-size: 12px; padding: 6px 0; }
  .explorer-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }
  .explorer-head h2 { margin-bottom: 8px; }
  .range-info { color: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .explorer-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 20px; margin-bottom: 10px; font-size: 12px; }
  .ctl { display: inline-flex; align-items: center; gap: 8px; }
  .ctl-label { color: var(--ink-3); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  .seg { display: inline-flex; border: 1px solid var(--baseline); }
  .seg button { background: var(--surface); border: 0; border-right: 1px solid var(--baseline); padding: 4px 10px; font: inherit; font-size: 12px; color: var(--ink-2); cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button[aria-pressed="true"] { background: var(--ink); color: #fff; }
  .seg button:focus-visible, .btn:focus-visible, canvas:focus-visible { outline: 2px solid var(--s1); outline-offset: 1px; }
  select.seg-select { font: inherit; font-size: 12px; padding: 3px 6px; border: 1px solid var(--baseline); background: var(--surface); color: var(--ink); }
  .ctl label { display: inline-flex; align-items: center; gap: 5px; color: var(--ink-2); cursor: pointer; }
  .btn { border: 1px solid var(--baseline); background: var(--surface); padding: 4px 10px; font: inherit; font-size: 12px; color: var(--ink); cursor: pointer; min-width: 30px; }
  .btn + .btn { margin-left: -1px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--ink-2); margin-left: auto; }
  .legend .item { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
  .legend .key { display: inline-block; width: 16px; height: 0; border-top: 2px solid; }
  .legend .key.band { height: 10px; border: 0; opacity: 0.25; }
  .legend .key.tick { width: 3px; height: 10px; border: 0; }
  .legend .key.dot { width: 8px; height: 8px; border: 0; border-radius: 50%; }
  canvas { display: block; width: 100%; }
  #ex-main { height: 340px; cursor: crosshair; }
  #ex-nav { height: 62px; margin-top: 6px; }
  .readout { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 6px 14px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--grid); font-size: 12px; }
  .readout > div { min-width: 0; }
  .readout > div:first-child { grid-column: span 2; }
  .readout .k { color: var(--ink-3); font-size: 11px; overflow-wrap: anywhere; }
  .readout .v { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .readout .v.flagged { color: var(--critical); }
  .readout > div:last-child { grid-column: span 2; }
  .readout > div:last-child .v { white-space: normal; }
  .readout .v.muted { color: var(--ink-3); font-weight: 400; }
  footer { margin-top: 28px; padding-top: 10px; border-top: 1px solid var(--grid); color: var(--ink-3); font-size: 11px; }
  @media (max-width: 1100px) { .columns { grid-template-columns: 1fr; } }
  @media print {
    body { padding: 0; }
    .explorer-controls, .range-info, p.note.hint { display: none; }
    section.block { break-inside: avoid; border-color: var(--grid); }
    .columns { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }
"""

_EXPLORER_JS = r"""
(function () {
  'use strict';
  var dataEl = document.getElementById('mdsq-data');
  var main = document.getElementById('ex-main');
  if (!dataEl || !main) return;
  var D = JSON.parse(dataEl.textContent);
  var nav = document.getElementById('ex-nav');
  var readoutEl = document.getElementById('ex-readout');
  var rangeEl = document.getElementById('ex-range');
  var legendEl = document.getElementById('ex-legend');
  var modeEl = document.getElementById('ex-mode');
  var css = getComputedStyle(document.documentElement);
  function cv(n) { return css.getPropertyValue(n).trim(); }
  var C = { ink: cv('--ink'), ink2: cv('--ink-2'), ink3: cv('--ink-3'), grid: cv('--grid'), base: cv('--baseline'),
            surface: cv('--surface'), critical: cv('--critical'), gap: cv('--gap'), flag: cv('--s7') };
  var SLOTS = [cv('--s1'), cv('--s2'), cv('--s3'), cv('--s4'), cv('--s5'), cv('--s6'), cv('--s7'), cv('--s8')];
  var LANE = { exception: { color: C.critical, label: 'Exceptions', legend: 'Data-integrity exception' },
               outlier: { color: C.flag, label: 'Outliers', legend: 'Return outlier (Hampel, descriptive)' },
               gap: { color: C.ink3, label: 'Gaps', legend: 'Silent gap over threshold' } };
  var FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';

  var T0 = D.meta.t0, T1 = D.meta.t1;
  if (!(T1 > T0)) T1 = T0 + 1000;
  var MIN_SPAN = Math.max(200, Math.min(2000, (T1 - T0) / 1e5));
  var x0 = T0, x1 = T1;
  var mode = D.modes[0];
  var overlays = { flags: true, gaps: true };
  var hoverT = null, pinnedT = null, lastReadout = -1;
  var mctx = main.getContext('2d'), nctx = nav.getContext('2d');
  var dpr = 1, W = 0, H = 0, NW = 0, NH = 0;
  var PAD = { l: 72, r: 16, t: 6, b: 24 };
  var LANE_H = 10;
  var ticks = D.tables[D.nav.table];
  var lanes = [], laneMask = {}, bitLabel = {};
  (D.flags || []).forEach(function (f) {
    if (!laneMask[f.lane]) { laneMask[f.lane] = 0; lanes.push(f.lane); }
    laneMask[f.lane] |= f.bit; bitLabel[f.bit] = f.label;
  });
  var EXC_MASK = laneMask.exception || 0, GAP_MASK = laneMask.gap || 0, OUT_MASK = laneMask.outlier || 0;

  function lowerBound(arr, v) { var lo = 0, hi = arr.length; while (lo < hi) { var m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; } return lo; }
  function nearestIndex(arr, v) { if (!arr.length) return -1; var i = lowerBound(arr, v); if (i <= 0) return 0; if (i >= arr.length) return arr.length - 1; return (v - arr[i - 1] <= arr[i] - v) ? i - 1 : i; }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function plotW() { return Math.max(10, W - PAD.l - PAD.r); }
  function plotTop() { return PAD.t + lanes.length * LANE_H + (lanes.length ? 8 : 0); }
  function plotH() { return Math.max(10, H - plotTop() - PAD.b); }
  function xs(t) { return PAD.l + (t - x0) / (x1 - x0) * plotW(); }
  function tx(px) { return x0 + (px - PAD.l) / plotW() * (x1 - x0); }
  function inPlot(e) { return e.offsetX >= PAD.l && e.offsetX <= W - PAD.r; }
  function rgba(hex, a) { var h = hex.replace('#', ''); if (h.length === 3) h = h.replace(/(.)/g, '$1$1'); var n = parseInt(h, 16); return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')'; }
  function niceStep(raw) { if (!(raw > 0)) return 1; var p = Math.pow(10, Math.floor(Math.log10(raw))); var r = raw / p; var m = r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10; return m * p; }
  function decimalsFor(step) { if (step >= 1) return 0; return Math.min(10, Math.ceil(-Math.log10(step) - 1e-9)); }
  function fmtAxis(v, dec) { if (Math.abs(v) < 1e-12) v = 0; if (Math.abs(v) >= 10000 && dec === 0) { var a = Math.abs(v); if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M'; return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'K'; } return v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtDate(ms) { var d = new Date(ms); return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()); }
  function fmtTime(ms, level) {
    var d = new Date(ms); var hms = pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds());
    if (level === 'ms') return hms + '.' + String(d.getUTCMilliseconds()).padStart(3, '0');
    if (level === 's') return hms; if (level === 'm') return hms.slice(0, 5); return fmtDate(ms);
  }
  function fmtFull(ms) { return fmtDate(ms) + ' ' + fmtTime(ms, 'ms'); }
  function fmtDur(ms) { var s = Math.round(ms / 1000); var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60; if (h) return h + 'h ' + pad2(m) + 'm'; if (m) return m + 'm ' + pad2(r) + 's'; return (ms < 10000 ? (ms / 1000).toFixed(2) : String(r)) + 's'; }
  var X_STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 900000, 1800000, 3600000, 7200000, 10800000, 21600000, 43200000, 86400000];
  function xStep() { var target = Math.max(2, plotW() / 95); for (var i = 0; i < X_STEPS.length; i++) if ((x1 - x0) / X_STEPS[i] <= target) return X_STEPS[i]; return 86400000 * Math.ceil((x1 - x0) / target / 86400000); }
  function fmtVal(v, fmt) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    if (fmt === 'price') return v.toLocaleString('en-US', { minimumFractionDigits: D.meta.price_decimals, maximumFractionDigits: D.meta.price_decimals });
    if (fmt === 'int') return Math.round(v).toLocaleString('en-US');
    if (fmt === 'bps') return v.toFixed(2);
    if (fmt === 'sec') return v.toFixed(3);
    if (fmt === 'sci') return Math.abs(v) < 1e-3 && v !== 0 ? v.toExponential(3) : v.toPrecision(4);
    return v.toPrecision(6);
  }
  function visible(tbl) { var i0 = Math.max(0, lowerBound(tbl.t, x0) - 1), i1 = Math.min(tbl.t.length - 1, lowerBound(tbl.t, x1)); return [i0, i1]; }
  function yDomain(tbl, i0, i1) {
    var lo = Infinity, hi = -Infinity;
    function acc(arr) { if (!arr) return; for (var i = i0; i <= i1; i++) { var v = arr[i]; if (v === null || v === undefined) continue; if (v < lo) lo = v; if (v > hi) hi = v; } }
    mode.series.forEach(function (s) { acc(tbl[s.key]); });
    if (mode.band) { acc(tbl[mode.band.lo]); acc(tbl[mode.band.hi]); }
    (mode.thresholds || []).forEach(function (th) { if (th.v < lo) lo = th.v; if (th.v > hi) hi = th.v; });
    if (mode.y && mode.y.zero) { if (0 < lo) lo = 0; if (0 > hi) hi = 0; }
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-12) { var e = Math.abs(hi) * 1e-6 || 1; lo -= e; hi += e; }
    var pad = (hi - lo) * 0.07; return [lo - pad, hi + pad];
  }

  function drawMain() {
    var ctx = mctx; ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.surface; ctx.fillRect(0, 0, W, H);
    var tbl = D.tables[mode.table]; var vis = visible(tbl), i0 = vis[0], i1 = vis[1];
    var dom = yDomain(tbl, i0, i1); var pt = plotTop(), ph = plotH(), pw = plotW();
    function ys(v) { return pt + (dom[1] - v) / (dom[1] - dom[0]) * ph; }
    var tv = visible(ticks);
    ctx.font = FONT; ctx.lineWidth = 1;

    if (overlays.gaps && GAP_MASK && ticks.dt) {
      ctx.fillStyle = rgba(C.gap, 0.45);
      for (var g = tv[0]; g <= tv[1]; g++) {
        if ((ticks.f[g] & GAP_MASK) && ticks.dt[g]) {
          var ga = clamp(xs(ticks.t[g] - ticks.dt[g] * 1000), PAD.l, PAD.l + pw), gb = clamp(xs(ticks.t[g]), PAD.l, PAD.l + pw);
          ctx.fillRect(ga, pt, Math.max(1, gb - ga), ph);
        }
      }
    }
    var ystep = niceStep((dom[1] - dom[0]) / 4.5), ydec = decimalsFor(ystep);
    ctx.strokeStyle = C.grid; ctx.fillStyle = C.ink3; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (var yv = Math.ceil(dom[0] / ystep) * ystep; yv <= dom[1] + ystep * 1e-6; yv += ystep) {
      var yy = Math.round(ys(yv)) + 0.5; ctx.beginPath(); ctx.moveTo(PAD.l, yy); ctx.lineTo(PAD.l + pw, yy); ctx.stroke();
      ctx.fillText(fmtAxis(yv, ydec), PAD.l - 8, yy);
    }
    var xstep = xStep(), level = xstep < 1000 ? 'ms' : xstep < 60000 ? 's' : xstep < 86400000 ? 'm' : 'd';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (var xv = Math.ceil(x0 / xstep) * xstep; xv <= x1; xv += xstep) {
      var xx = Math.round(xs(xv)) + 0.5; ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(xx, pt); ctx.lineTo(xx, pt + ph); ctx.stroke();
      ctx.fillStyle = C.ink3; ctx.fillText(fmtTime(xv, level), xx, pt + ph + 6);
    }
    ctx.strokeStyle = C.base; ctx.beginPath(); ctx.moveTo(PAD.l, Math.round(pt + ph) + 0.5); ctx.lineTo(PAD.l + pw, Math.round(pt + ph) + 0.5); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD.l + 0.5, pt); ctx.lineTo(PAD.l + 0.5, pt + ph); ctx.stroke();
    if (mode.y && mode.y.zero && dom[0] < 0 && dom[1] > 0) { var y0 = Math.round(ys(0)) + 0.5; ctx.beginPath(); ctx.moveTo(PAD.l, y0); ctx.lineTo(PAD.l + pw, y0); ctx.stroke(); }
    ctx.fillStyle = C.ink3; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'; ctx.fillText(mode.y ? mode.y.label : '', PAD.l + 6, pt + 14);

    ctx.save(); ctx.beginPath(); ctx.rect(PAD.l, pt, pw, ph); ctx.clip();
    if (mode.band) {
      var lo = tbl[mode.band.lo], hi = tbl[mode.band.hi]; ctx.fillStyle = rgba(mode.series[0].color || SLOTS[0], 0.12);
      var seg = [];
      function flush() { if (seg.length >= 2) { ctx.beginPath(); for (var k = 0; k < seg.length; k++) { if (k) ctx.lineTo(seg[k][0], seg[k][1]); else ctx.moveTo(seg[k][0], seg[k][1]); } for (var q = seg.length - 1; q >= 0; q--) ctx.lineTo(seg[q][0], seg[q][2]); ctx.closePath(); ctx.fill(); } seg = []; }
      for (var b = i0; b <= i1; b++) { if (lo[b] === null || hi[b] === null) { flush(); continue; } seg.push([xs(tbl.t[b]), ys(hi[b]), ys(lo[b])]); }
      flush();
    }
    mode.series.forEach(function (s, si) {
      var col = s.color || SLOTS[si % 8], arr = tbl[s.key]; if (!arr) return;
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      var i, v, px, py, pen = false, prevY = 0;
      if (s.kind === 'stem') {
        ctx.lineWidth = 1; var yz = ys(0); ctx.beginPath();
        for (i = i0; i <= i1; i++) { v = arr[i]; if (v === null) continue; px = Math.round(xs(tbl.t[i])) + 0.5; ctx.moveTo(px, yz); ctx.lineTo(px, ys(v)); }
        ctx.stroke();
      } else if (s.kind === 'column') {
        var bw = Math.max(1, Math.min(24, (s.width_ms || 60000) / (x1 - x0) * pw - 2)); var yz2 = ys(0);
        for (i = i0; i <= i1; i++) { v = arr[i]; if (v === null) continue; px = xs(tbl.t[i]); ctx.fillRect(px + 1, ys(v), bw, yz2 - ys(v)); }
      } else {
        ctx.lineWidth = 2; ctx.beginPath();
        for (i = i0; i <= i1; i++) {
          v = arr[i]; if (v === null) { pen = false; continue; }
          px = xs(tbl.t[i]); py = ys(v);
          if (!pen) { ctx.moveTo(px, py); pen = true; } else if (s.kind === 'step') { ctx.lineTo(px, prevY); ctx.lineTo(px, py); } else ctx.lineTo(px, py);
          prevY = py;
        }
        ctx.stroke();
      }
    });
    (mode.thresholds || []).forEach(function (th) {
      var yt = Math.round(ys(th.v)) + 0.5; ctx.strokeStyle = C.critical; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(PAD.l, yt); ctx.lineTo(PAD.l + pw, yt); ctx.stroke();
      ctx.fillStyle = C.ink2; ctx.textAlign = 'right'; ctx.textBaseline = 'bottom'; ctx.fillText(th.label, PAD.l + pw - 4, yt - 2);
    });
    if (overlays.flags && mode.markers !== false && mode.table === D.nav.table) {
      var arr0 = tbl[mode.series[0].key]; var count = 0;
      for (var m = tv[0]; m <= tv[1]; m++) if (ticks.f[m]) count++;
      if (count <= 4000) {
        for (var m2 = tv[0]; m2 <= tv[1]; m2++) {
          var f = ticks.f[m2]; if (!f) continue; var val = arr0[m2]; if (val === null) continue;
          var mx = xs(ticks.t[m2]), my = ys(val);
          if (f & EXC_MASK) { ctx.beginPath(); ctx.arc(mx, my, 3.5, 0, Math.PI * 2); ctx.fillStyle = C.critical; ctx.strokeStyle = C.surface; ctx.lineWidth = 2; ctx.fill(); ctx.stroke(); }
          else if (f & OUT_MASK) { ctx.beginPath(); ctx.arc(mx, my, 4, 0, Math.PI * 2); ctx.fillStyle = C.flag; ctx.strokeStyle = C.surface; ctx.lineWidth = 2; ctx.fill(); ctx.stroke(); }
        }
      }
    }
    ctx.restore();

    lanes.forEach(function (lane, k) {
      var ly = PAD.t + k * LANE_H, info = LANE[lane] || { color: C.ink3, label: lane };
      ctx.fillStyle = C.ink3; ctx.font = '10px system-ui, -apple-system, "Segoe UI", sans-serif'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      ctx.fillText(info.label, PAD.l - 8, ly + LANE_H / 2);
      ctx.fillStyle = rgba(C.grid, 0.6); ctx.fillRect(PAD.l, ly + 1, pw, LANE_H - 2);
      ctx.fillStyle = info.color; var mask = laneMask[lane];
      for (var i = tv[0]; i <= tv[1]; i++) {
        if (!(ticks.f[i] & mask)) continue;
        if (lane === 'gap' && ticks.dt && ticks.dt[i]) {
          var a = clamp(xs(ticks.t[i] - ticks.dt[i] * 1000), PAD.l, PAD.l + pw), bb = clamp(xs(ticks.t[i]), PAD.l, PAD.l + pw);
          ctx.fillRect(a, ly + 1, Math.max(2, bb - a), LANE_H - 2);
        } else {
          var lx = clamp(Math.round(xs(ticks.t[i])) - 1, PAD.l, PAD.l + pw - 2); ctx.fillRect(lx, ly + 1, 2, LANE_H - 2);
        }
      }
      ctx.font = FONT;
    });

    if (pinnedT !== null && pinnedT >= x0 && pinnedT <= x1) {
      var ppx = Math.round(xs(pinnedT)) + 0.5; ctx.strokeStyle = C.ink; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(ppx, pt); ctx.lineTo(ppx, pt + ph); ctx.stroke();
      ctx.fillStyle = C.ink; ctx.beginPath(); ctx.moveTo(ppx - 5, pt); ctx.lineTo(ppx + 5, pt); ctx.lineTo(ppx, pt + 6); ctx.closePath(); ctx.fill();
    }
    var ro = -1;
    if (hoverT !== null) {
      var hi2 = nearestIndex(tbl.t, hoverT);
      if (hi2 >= 0) {
        var cx = Math.round(xs(tbl.t[hi2])) + 0.5;
        ctx.strokeStyle = C.ink2; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(cx, pt); ctx.lineTo(cx, pt + ph); ctx.stroke();
        mode.series.forEach(function (s, si) {
          var v = tbl[s.key] ? tbl[s.key][hi2] : null; if (v === null || v === undefined) return;
          ctx.beginPath(); ctx.arc(cx, ys(v), 4, 0, Math.PI * 2); ctx.fillStyle = s.color || SLOTS[si % 8]; ctx.strokeStyle = C.surface; ctx.lineWidth = 2; ctx.fill(); ctx.stroke();
        });
        ctx.fillStyle = C.ink; ctx.textAlign = cx > PAD.l + pw / 2 ? 'right' : 'left'; ctx.textBaseline = 'top';
        ctx.fillText(fmtTime(tbl.t[hi2], 'ms'), cx + (cx > PAD.l + pw / 2 ? -6 : 6), pt + 4);
      }
      ro = nearestIndex(ticks.t, hoverT);
    }
    if (ro >= 0) lastReadout = ro;
    updateReadout(lastReadout, ro < 0);
  }

  function navX(t) { return PAD.l + (t - T0) / (T1 - T0) * (NW - PAD.l - PAD.r); }
  function navT(px) { return T0 + (px - PAD.l) / (NW - PAD.l - PAD.r) * (T1 - T0); }
  function drawNav() {
    var ctx = nctx; ctx.clearRect(0, 0, NW, NH); ctx.fillStyle = C.surface; ctx.fillRect(0, 0, NW, NH);
    var tbl = D.tables[D.nav.table], arr = tbl[D.nav.key]; var L = PAD.l, R = NW - PAD.r; var top = 4, bot = NH - 16, h = bot - top;
    var lo = Infinity, hi = -Infinity, i, v;
    for (i = 0; i < arr.length; i++) { v = arr[i]; if (v === null) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
    if (!isFinite(lo)) { lo = 0; hi = 1; } if (hi - lo < 1e-12) { lo -= 1; hi += 1; }
    function ny(val) { return top + (hi - val) / (hi - lo) * h; }
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.strokeRect(L + 0.5, top + 0.5, R - L - 1, h);
    ctx.strokeStyle = C.ink2; ctx.lineWidth = 1; ctx.beginPath(); var pen = false;
    for (i = 0; i < arr.length; i++) { v = arr[i]; if (v === null) { pen = false; continue; } var px = navX(tbl.t[i]), py = ny(v); if (!pen) { ctx.moveTo(px, py); pen = true; } else ctx.lineTo(px, py); }
    ctx.stroke();
    if (EXC_MASK || OUT_MASK) {
      for (i = 0; i < ticks.t.length; i++) { var f = ticks.f[i]; if (!f) continue; if (f & EXC_MASK) ctx.fillStyle = C.critical; else if (f & OUT_MASK) ctx.fillStyle = C.flag; else continue; ctx.fillRect(Math.round(navX(ticks.t[i])) - 0.5, bot + 2, 1, 4); }
    }
    var a = navX(x0), b = navX(x1); ctx.fillStyle = rgba(C.surface, 0.62); ctx.fillRect(L, top, Math.max(0, a - L), h); ctx.fillRect(b, top, Math.max(0, R - b), h);
    ctx.strokeStyle = C.ink; ctx.lineWidth = 1; ctx.strokeRect(Math.round(a) + 0.5, top + 0.5, Math.max(1, Math.round(b - a)), h);
    ctx.fillStyle = C.ink; ctx.fillRect(Math.round(a) - 2, top + h / 2 - 7, 4, 14); ctx.fillRect(Math.round(b) - 2, top + h / 2 - 7, 4, 14);
    ctx.fillStyle = C.ink3; ctx.font = '10px system-ui, -apple-system, "Segoe UI", sans-serif'; ctx.textBaseline = 'top';
    ctx.textAlign = 'left'; ctx.fillText(fmtFull(T0), L, bot + 7); ctx.textAlign = 'right'; ctx.fillText(fmtFull(T1), R, bot + 7);
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText('Session', L - 8, top + h / 2);
  }

  var fields = [{ key: '__t', label: 'Time' }].concat(D.readout || []).concat([{ key: '__flags', label: 'Flags' }]);
  var cells = {};
  fields.forEach(function (f) {
    var cell = document.createElement('div'); var k = document.createElement('div'); k.className = 'k'; k.textContent = f.label;
    var v = document.createElement('div'); v.className = 'v'; v.textContent = '—'; cell.appendChild(k); cell.appendChild(v); readoutEl.appendChild(cell); cells[f.key] = v;
  });
  function updateReadout(j, stale) {
    if (j < 0) return;
    fields.forEach(function (f) {
      var el = cells[f.key], txt;
      if (f.key === '__t') txt = fmtFull(ticks.t[j]);
      else if (f.key === '__flags') { var names = []; var fl = ticks.f[j]; for (var bit in bitLabel) if (fl & bit) names.push(bitLabel[bit]); txt = names.length ? names.join(', ') : 'none'; el.classList.toggle('flagged', names.length > 0); el.title = txt; }
      else txt = fmtVal(ticks[f.key] ? ticks[f.key][j] : null, f.fmt);
      el.textContent = txt; el.classList.toggle('muted', !!stale && f.key !== '__flags');
    });
  }
  function updateRangeInfo() {
    var vis = visible(ticks); var n = Math.max(0, vis[1] - vis[0] - 1);
    rangeEl.textContent = 'Visible: ' + fmtFull(x0) + ' → ' + fmtTime(x1, 'ms') + ' (' + fmtDur(x1 - x0) + ') · ' + n.toLocaleString('en-US') + ' plotted points';
  }

  function buildLegend() {
    while (legendEl.firstChild) legendEl.removeChild(legendEl.firstChild);
    function item(cls, color, label) { var it = document.createElement('span'); it.className = 'item'; var k = document.createElement('span'); k.className = 'key ' + cls; k.style.borderColor = color; k.style.background = (cls === 'line') ? 'transparent' : color; var t = document.createElement('span'); t.textContent = label; it.appendChild(k); it.appendChild(t); legendEl.appendChild(it); }
    mode.series.forEach(function (s, si) { item('line', s.color || SLOTS[si % 8], s.label); });
    if (mode.band) item('band', mode.series[0].color || SLOTS[0], mode.band.label);
    (mode.thresholds || []).slice(0, 1).forEach(function () { item('line', C.critical, 'Divergence threshold'); });
    lanes.forEach(function (l) { var info = LANE[l]; if (info) item(l === 'gap' ? 'band' : 'tick', info.color, info.legend); });
  }

  var raf = null;
  function scheduleDraw() { if (raf) return; raf = requestAnimationFrame(function () { raf = null; drawMain(); drawNav(); updateRangeInfo(); }); }
  function setRange(a, b, keepSpan) {
    var span = b - a;
    if (span < MIN_SPAN) { var c = (a + b) / 2; a = c - MIN_SPAN / 2; b = c + MIN_SPAN / 2; span = MIN_SPAN; }
    if (span >= T1 - T0) { a = T0; b = T1; }
    else if (keepSpan) { if (a < T0) { a = T0; b = T0 + span; } if (b > T1) { b = T1; a = T1 - span; } }
    else { a = Math.max(T0, a); b = Math.min(T1, b); }
    x0 = a; x1 = b; scheduleDraw();
  }
  function zoomAround(anchor, f) { setRange(anchor - (anchor - x0) * f, anchor + (x1 - anchor) * f, true); }
  function reset() { pinnedT = null; setRange(T0, T1, false); if (location.hash) history.replaceState(null, '', location.pathname + location.search); }
  function goTo(t) {
    var span = Math.min(x1 - x0, 10 * 60000); if (span < 5000) span = 60000;
    pinnedT = t; hoverT = null; setRange(t - span / 2, t + span / 2, true);
    history.replaceState(null, '', '#mode=' + encodeURIComponent(mode.id) + '&t=' + Math.round(t));
    lastReadout = nearestIndex(ticks.t, t); updateReadout(lastReadout, false);
    main.scrollIntoView({ block: 'start', behavior: 'smooth' }); main.focus({ preventScroll: true });
  }
  function setMode(m) { mode = m; buildLegend(); scheduleDraw(); if (modeEl.tagName === 'SELECT') modeEl.value = m.id; else Array.prototype.forEach.call(modeEl.children, function (b) { b.setAttribute('aria-pressed', b.dataset.id === m.id ? 'true' : 'false'); }); }

  if (D.modes.length > 6) {
    var sel = document.createElement('select'); sel.className = 'seg-select'; sel.setAttribute('aria-label', 'Series');
    D.modes.forEach(function (m) { var o = document.createElement('option'); o.value = m.id; o.textContent = m.label; sel.appendChild(o); });
    sel.addEventListener('change', function () { var m = D.modes.filter(function (q) { return q.id === sel.value; })[0]; if (m) setMode(m); });
    modeEl.parentNode.replaceChild(sel, modeEl); modeEl = sel;
  } else {
    D.modes.forEach(function (m) { var b = document.createElement('button'); b.type = 'button'; b.textContent = m.label; b.dataset.id = m.id; b.setAttribute('aria-pressed', m === mode ? 'true' : 'false'); b.addEventListener('click', function () { setMode(m); }); modeEl.appendChild(b); });
  }
  var ovFlags = document.getElementById('ex-ov-flags'), ovGaps = document.getElementById('ex-ov-gaps');
  if (ovFlags) ovFlags.addEventListener('change', function () { overlays.flags = ovFlags.checked; scheduleDraw(); });
  if (ovGaps) ovGaps.addEventListener('change', function () { overlays.gaps = ovGaps.checked; scheduleDraw(); });
  document.getElementById('ex-reset').addEventListener('click', reset);
  document.getElementById('ex-zoom-in').addEventListener('click', function () { zoomAround((x0 + x1) / 2, 0.6); });
  document.getElementById('ex-zoom-out').addEventListener('click', function () { zoomAround((x0 + x1) / 2, 1 / 0.6); });
  document.addEventListener('click', function (e) { var a = e.target.closest ? e.target.closest('a.goto[data-t]') : null; if (!a) return; e.preventDefault(); goTo(+a.dataset.t); });

  var drag = null;
  main.addEventListener('pointerdown', function (e) { if (e.button !== 0) return; main.setPointerCapture(e.pointerId); drag = { kind: 'pan', px: e.offsetX, x0: x0, x1: x1 }; main.style.cursor = 'grabbing'; });
  main.addEventListener('pointermove', function (e) {
    if (drag && drag.kind === 'pan') { var shift = (drag.px - e.offsetX) / plotW() * (drag.x1 - drag.x0); setRange(drag.x0 + shift, drag.x1 + shift, true); }
    hoverT = inPlot(e) ? tx(e.offsetX) : null; scheduleDraw();
  });
  function endDrag(e) { if (drag && e.pointerId !== undefined) { try { e.target.releasePointerCapture(e.pointerId); } catch (err) {} } drag = null; main.style.cursor = 'crosshair'; }
  main.addEventListener('pointerup', endDrag); main.addEventListener('pointercancel', endDrag);
  main.addEventListener('pointerleave', function () { hoverT = null; scheduleDraw(); });
  main.addEventListener('wheel', function (e) {
    e.preventDefault();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { var shift = e.deltaX / plotW() * (x1 - x0); setRange(x0 + shift, x1 + shift, true); return; }
    var f = Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0018)); zoomAround(inPlot(e) ? tx(e.offsetX) : (x0 + x1) / 2, f);
  }, { passive: false });
  main.addEventListener('dblclick', reset);
  main.addEventListener('keydown', function (e) {
    var span = x1 - x0, handled = true;
    if (e.key === 'ArrowLeft') setRange(x0 - span * 0.15, x1 - span * 0.15, true);
    else if (e.key === 'ArrowRight') setRange(x0 + span * 0.15, x1 + span * 0.15, true);
    else if (e.key === '+' || e.key === '=') zoomAround((x0 + x1) / 2, 0.6);
    else if (e.key === '-' || e.key === '_') zoomAround((x0 + x1) / 2, 1 / 0.6);
    else if (e.key === 'Home') reset();
    else handled = false;
    if (handled) e.preventDefault();
  });

  nav.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return; nav.setPointerCapture(e.pointerId); var px = e.offsetX, a = navX(x0), b = navX(x1);
    if (Math.abs(px - a) <= 6) drag = { kind: 'navL' };
    else if (Math.abs(px - b) <= 6) drag = { kind: 'navR' };
    else if (px > a && px < b) drag = { kind: 'navMove', px: px, x0: x0, x1: x1 };
    else drag = { kind: 'navNew', start: clamp(navT(px), T0, T1), moved: false, px: px };
  });
  nav.addEventListener('pointermove', function (e) {
    var px = e.offsetX, t = clamp(navT(px), T0, T1);
    if (!drag) { var a = navX(x0), b = navX(x1); nav.style.cursor = (Math.abs(px - a) <= 6 || Math.abs(px - b) <= 6) ? 'ew-resize' : (px > a && px < b) ? 'grab' : 'crosshair'; return; }
    if (drag.kind === 'navL') setRange(Math.min(t, x1 - MIN_SPAN), x1, false);
    else if (drag.kind === 'navR') setRange(x0, Math.max(t, x0 + MIN_SPAN), false);
    else if (drag.kind === 'navMove') { var shift = (px - drag.px) / (NW - PAD.l - PAD.r) * (T1 - T0); setRange(drag.x0 + shift, drag.x1 + shift, true); }
    else if (drag.kind === 'navNew') { if (Math.abs(px - drag.px) > 3) drag.moved = true; if (drag.moved) setRange(Math.min(drag.start, t), Math.max(drag.start, t), false); }
  });
  function endNav(e) {
    if (drag && drag.kind === 'navNew' && !drag.moved) { var t = clamp(navT(e.offsetX), T0, T1), span = x1 - x0; setRange(t - span / 2, t + span / 2, true); }
    if (drag && e.pointerId !== undefined) { try { nav.releasePointerCapture(e.pointerId); } catch (err) {} } drag = null;
  }
  nav.addEventListener('pointerup', endNav); nav.addEventListener('pointercancel', endNav);

  function resize() {
    dpr = window.devicePixelRatio || 1;
    var r = main.getBoundingClientRect(); W = Math.max(100, Math.floor(r.width)); H = Math.max(60, Math.floor(r.height));
    main.width = Math.round(W * dpr); main.height = Math.round(H * dpr); mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var rn = nav.getBoundingClientRect(); NW = Math.max(100, Math.floor(rn.width)); NH = Math.max(30, Math.floor(rn.height));
    nav.width = Math.round(NW * dpr); nav.height = Math.round(NH * dpr); nctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawMain(); drawNav(); updateRangeInfo();
  }
  if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(main.parentNode); else window.addEventListener('resize', resize);
  lastReadout = nearestIndex(ticks.t, T0);
  (function applyHash() {
    var h = location.hash.replace(/^#/, ''); if (!h || h.indexOf('=') < 0) return; var q = {};
    h.split('&').forEach(function (kv) { var pr = kv.split('='); if (pr[0]) q[decodeURIComponent(pr[0])] = decodeURIComponent(pr[1] || ''); });
    if (q.mode) { var m = D.modes.filter(function (x) { return x.id === q.mode; })[0]; if (m) mode = m; }
    if (q.from && q.to && +q.from < +q.to) { x0 = clamp(+q.from, T0, T1); x1 = clamp(+q.to, T0, T1); }
    if (q.t && !isNaN(+q.t)) {
      var tt = clamp(+q.t, T0, T1); pinnedT = tt; lastReadout = nearestIndex(ticks.t, tt);
      if (!(q.from && q.to)) { var span = Math.min(T1 - T0, 10 * 60000); x0 = tt - span / 2; x1 = tt + span / 2; if (x0 < T0) { x0 = T0; x1 = T0 + span; } if (x1 > T1) { x1 = T1; x0 = T1 - span; } }
    }
    if (x1 - x0 < MIN_SPAN) { x0 = T0; x1 = T1; }
  })();
  setMode(mode);
  updateReadout(lastReadout, true);
  resize();
})();
"""


def _explorer_html(payload: dict | None, hint: str) -> str:
    if payload is None:
        return ('<section class="block" id="explorer"><h2>Time series explorer</h2>'
                '<p class="empty">No embedded tick data for this report.</p></section>')
    meta = payload["meta"]
    has_gaps = any(f["lane"] == "gap" for f in payload["flags"])
    gaps_ctl = ' <label><input type="checkbox" id="ex-ov-gaps" checked> Shade gaps</label>' if has_gaps else ""
    thinned = ""
    if meta.get("bucketed"):
        thinned = (f" The plot is thinned to {meta['n_points']:,} of {meta['n_ticks']:,} rows "
                   f"(per-bucket minimum and maximum kept, so spikes survive; every flagged row is kept).")
    data_json = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    return f"""
  <section class="block" id="explorer">
    <div class="explorer-head"><h2>Time series explorer</h2><div class="range-info" id="ex-range"></div></div>
    <div class="explorer-controls">
      <div class="ctl"><span class="ctl-label">Series</span><div class="seg" id="ex-mode" role="group" aria-label="Series"></div></div>
      <div class="ctl"><label><input type="checkbox" id="ex-ov-flags" checked> Flag markers</label>{gaps_ctl}</div>
      <div class="ctl"><span class="ctl-label">Range</span><button type="button" class="btn" id="ex-zoom-in" title="Zoom in">+</button><button type="button" class="btn" id="ex-zoom-out" title="Zoom out">&minus;</button><button type="button" class="btn" id="ex-reset">Full session</button></div>
      <div class="legend" id="ex-legend"></div>
    </div>
    <canvas id="ex-main" tabindex="0" aria-label="Time series explorer. Drag to pan, scroll to zoom, arrow keys to pan, Home for the full session."></canvas>
    <canvas id="ex-nav" aria-label="Session overview. Drag the window or its handles to choose the visible range."></canvas>
    <div class="readout" id="ex-readout"></div>
    <p class="note hint">{_esc(hint)}{_esc(thinned)}</p>
  </section>
  <script type="application/json" id="mdsq-data">{data_json}</script>"""


def _page(title: str, subtitle: str, meta_html: str, body_html: str, generated_at: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
{_STYLE}
</style>
</head>
<body>
  <header class="report-header">
    <div><h1>{_esc(title)}</h1><div class="tag">{_esc(subtitle)}</div></div>
    <div class="meta">{meta_html}</div>
  </header>
{body_html}
  <footer>Generated by market_data_statistical_quality -- {_esc(generated_at.replace('T', ' ').replace('+00:00', ''))} UTC. Hypothesis tests are descriptive; data-integrity checks are the only exceptions raised.</footer>
<script>
{_EXPLORER_JS}
</script>
</body>
</html>
"""


def _verdict(flag: bool | None, if_true: str, if_false: str) -> str:
    if flag is None:
        return "not computable (constant series)"
    return if_true if flag else if_false


def _stationarity_reading(block: dict, expected_nonstationary: bool) -> str:
    adf, kp = block["adf_stationary"], block["kpss_stationary"]
    if adf is None and kp is None:
        return "Not computable: the series is constant" + (" -- stuck feed?" if expected_nonstationary else "")
    if adf is None or kp is None:
        name, flag = ("KPSS", kp) if adf is None else ("ADF", adf)
        return f"Only {name} computed: " + ("stationary" if flag else "non-stationary")
    if adf and kp:
        verdict = "Stationary (ADF and KPSS agree)"
    elif not adf and not kp:
        verdict = "Non-stationary (ADF and KPSS agree)"
    else:
        verdict = "Mixed: ADF says " + ("stationary" if adf else "non-stationary") + ", KPSS says " + ("stationary" if kp else "non-stationary")
    if expected_nonstationary:
        return verdict + (" -- expected for a traded price" if not adf and not kp else " -- unusual for a traded price, worth investigating (stuck feed?)")
    return verdict + (" -- expected for returns" if adf and kp else " -- unusual for returns, worth investigating")


def render_dashboard(report: dict, plot_paths: list[str], payload: dict | None = None) -> str:
    s, c, r, l = report["structural"], report["continuity"], report["returns"], report["liquidity"]
    pl = report["price_level"]
    n_exceptions, n_checks = count_exceptions(report)
    decimals = payload["meta"]["price_decimals"] if payload else 4
    price = lambda x: _fixed(x, decimals)
    n_rows = s["n_rows"]
    multi_day = pd.Timestamp(c["session_start"]).date() != pd.Timestamp(c["session_end"]).date()
    duration_min = c["session_duration_s"] / 60 if c["session_duration_s"] else 0.0

    tiles = "".join([
        _tile("Checks with exceptions", f"{n_exceptions} <small>of {n_checks}</small>",
              "data-integrity checks that fired" if n_exceptions else "no data-integrity exception", alert=n_exceptions > 0),
        _tile("Ticks recorded", _int(n_rows), f"{s['n_levels_present']} book level{'s' if s['n_levels_present'] != 1 else ''}"
              + (f"; {_int(s['n_zero_rows_ignored'])} zero rows ignored" if s.get("n_zero_rows_ignored") else "")),
        _tile("Session", _esc(_duration(c["session_duration_s"])),
              f"{_esc(_fmt_ts(c['session_start'], multi_day))} &ndash; {_esc(_fmt_ts(c['session_end'], multi_day))}"),
    ])

    alerts = payload["alerts"] if payload else [
        {"key": key, "label": label, "count": report[sec][key], "rows": None, "share": None, "first": None, "last": None}
        for sec, key, label in EXCEPTION_METRICS
    ]
    fired = [a for a in alerts if a["count"] > 0]
    if fired:
        alert_rows = "".join(
            f'<tr class="exception"><td>{_esc(a["label"])}</td><td class="num">{_int(a["count"])}</td>'
            f'<td class="num">{_pct(a["share"]) if a["share"] is not None else "n/a"}</td>'
            f'<td class="num">{_goto(a["first"], multi_day) if a["first"] else "n/a"}</td>'
            f'<td class="num">{_goto(a["last"], multi_day) if a["last"] else "n/a"}</td></tr>'
            for a in fired
        )
        alerts_html = _table(["Exception", "Count", "Ticks", "First", "Last"], alert_rows, num_cols={1, 2, 3, 4})
        alerts_note = ('<p class="note">Count is the metric as reported; ticks is the share of rows affected. '
                       'Click a time to show it in the explorer.</p>')
    else:
        alerts_html = '<p class="empty">No data-integrity exception. All checks passed.</p>'
        alerts_note = ""

    summary_rows = "".join([
        _kv("Source file", _esc(report["source_file"])),
        _kv("Generated (UTC)", _esc(report["generated_at"].replace("T", " ").replace("+00:00", ""))),
        _kv("Session date", _esc(pd.Timestamp(c["session_start"]).strftime("%Y-%m-%d"))),
        _kv("First / last tick", f"{_esc(_fmt_ts(c['session_start'], multi_day))} / {_esc(_fmt_ts(c['session_end'], multi_day))}"),
        _kv("Mean tick rate", f"{n_rows / duration_min:,.1f} / min" if duration_min else "n/a"),
        _kv("Median inter-arrival", f"{c['median_inter_arrival_s']:.3f} s"),
        _kv("Mid price, first / last", f"{price(pl['start'])} / {price(pl['end'])}"),
        _kv("Mid price, min / max", f"{price(pl['min'])} / {price(pl['max'])}"),
        _kv("Mean spread", f"{price(l['spread_mean'])}" + (f" ({l['spread_mean'] / pl['mean'] * 1e4:.2f} bps of mid)" if pl["mean"] else "")),
        _kv("Return outliers (Hampel)", f"{_int(r['n_outliers_hampel'])} ({_pct(r['n_outliers_hampel'] / r['n_returns'] if r['n_returns'] else 0)})"),
    ])

    kurt, skew = r["kurtosis_excess"], r["skew"]
    if kurt is None or skew is None:
        distribution = "not computable (constant series)"
    else:
        tails = ("heavy tails" if kurt > 1 else "light tails" if kurt < -0.5 else "near-normal tails")
        distribution = (f"{tails} (excess kurtosis {kurt:,.2f}, skew {skew:,.2f}); "
                        + _verdict(r["jarque_bera_normal"], "consistent with normal", "normality rejected")
                        + " (Jarque-Bera)")
    reading_rows = "".join([
        _kv_text("Price level", _esc(_stationarity_reading(pl, expected_nonstationary=True))),
        _kv_text("Returns", _esc(_stationarity_reading(r, expected_nonstationary=False))),
        _kv_text("Return distribution", _esc(distribution)),
        _kv_text("Serial dependence", _esc(_verdict(r["ljung_box_autocorrelated"], "autocorrelation present", "no return autocorrelation")
                                      + "; " + _verdict(r["ljung_box_sq_vol_clustering"], "volatility clustering present", "no volatility clustering")
                                      + f" (Ljung-Box, lag {r['ljung_box_lags']})")),
    ])

    col1 = f"""
    <div>
      <div class="column-title">Alerts &amp; summary</div>
      <section class="block">
        <div class="tiles">{tiles}</div>
        <h2>Alerts</h2>
        {alerts_html}{alerts_note}
      </section>
      <section class="block">
        <h2>Summary</h2>
        <table class="kv"><tbody>{summary_rows}</tbody></table>
      </section>
      <section class="block">
        <h2>Statistical reading</h2>
        <p class="note">Descriptive, not pass/fail. Significance level {r['significance_level']:.0%}; details in the analysis column.</p>
        <table class="kv"><tbody>{reading_rows}</tbody></table>
      </section>
    </div>"""

    structural_rows = "".join([
        _row("Rows processed", _int(n_rows)),
        _row("Order-book levels present", _int(s["n_levels_present"])),
        *([_row("Zero-quote rows ignored", _int(s["n_zero_rows_ignored"]), "best bid or ask was 0"),
           _row("Zero values ignored", _int(s["n_zero_values_ignored"]), "depth or size was 0")]
          if s.get("ignore_zeros") else []),
        _exception_row("Duplicate timestamps", s["n_duplicate_timestamps"]),
        _exception_row("Out-of-order timestamps", s["n_out_of_order_timestamps"]),
        _exception_row("Crossed book ticks (bid >= ask)", s["n_crossed_book"]),
        _exception_row("Level-depth bid inconsistencies", s["n_level_bid_inconsistent"]),
        _exception_row("Level-depth ask inconsistencies", s["n_level_ask_inconsistent"]),
        _exception_row("Non-positive order sizes", s["n_non_positive_sizes"]),
        _exception_row("Missing values", s["n_missing_values"]),
    ])
    continuity_rows = "".join([
        _row("Mean inter-arrival", f"{c['mean_inter_arrival_s']:.3f} s"),
        _row("Median inter-arrival", f"{c['median_inter_arrival_s']:.3f} s"),
        _row("P95 inter-arrival", f"{c['p95_inter_arrival_s']:.3f} s"),
        _row("Max inter-arrival", f"{c['max_inter_arrival_s']:.3f} s"),
        _row("Gap threshold", f"{c['gap_threshold_s']:g} s"),
        _exception_row("Silent gaps over threshold", c["n_gaps_over_threshold"]),
    ])
    gap_rows = "".join(
        f'<tr><td class="num">{_goto(g["timestamp"], multi_day)}</td><td class="num">{_esc(_duration(g["gap_s"]))}</td></tr>'
        for g in c["top_gaps"]
    )
    gaps_html = (_table(["Gap ends at", "Silence"], gap_rows, num_cols={0, 1})
                 if gap_rows else '<p class="empty">No gap over the threshold.</p>')
    liquidity_rows = "".join([
        _row("Mean / median spread", f"{price(l['spread_mean'])} / {price(l['spread_median'])}"),
        _row("Spread std. dev.", price(l["spread_std"])),
        _row("Spread min / max", f"{price(l['spread_min'])} / {price(l['spread_max'])}"),
        _exception_row("Negative spread ticks", l["n_negative_spread"]),
        _row("Zero spread ticks (locked book)", _int(l["n_zero_spread"])),
        _exception_row("Zero-or-negative sizes", l["n_zero_or_negative_sizes"]),
        _row("Median L1 bid / ask size", f"{_fixed(l['bid_size_1_median'], 0)} / {_fixed(l['ask_size_1_median'], 0)}"),
    ])
    col2 = f"""
    <div>
      <div class="column-title">Recording status</div>
      <section class="block">
        <h2>Structural integrity</h2>
        <p class="note">Any non-zero count is a data-integrity exception.</p>
        {_table(["Check", "Count", "Status"], structural_rows, num_cols={1})}
      </section>
      <section class="block">
        <h2>Coverage &amp; continuity</h2>
        {_table(["Metric", "Value", "Status"], continuity_rows, num_cols={1})}
        <h3>Largest gaps</h3>
        {gaps_html}
      </section>
      <section class="block">
        <h2>Spread &amp; liquidity</h2>
        {_table(["Metric", "Value", "Status"], liquidity_rows, num_cols={1})}
      </section>
    </div>"""

    def test_row(label: str, stat: float, p: float, result: str) -> str:
        return (f'<tr><td>{_esc(label)}</td><td class="num">{_num(stat, 4)}</td>'
                f'<td class="num">{_pvalue(p)}</td><td class="note-cell">{_esc(result)}</td></tr>')

    price_rows = "".join([
        _row("Mean / std. dev.", f"{price(pl['mean'])} / {price(pl['std'])}"),
    ])
    price_tests = "".join([
        test_row("ADF", pl["adf_stat"], pl["adf_pvalue"], _verdict(pl["adf_stationary"], "stationary", "non-stationary (expected)")),
        test_row("KPSS", pl["kpss_stat"], pl["kpss_pvalue"], _verdict(pl["kpss_stationary"], "stationary", "non-stationary (expected)")),
    ])
    return_rows = "".join([
        _row("Returns analysed", _int(r["n_returns"])),
        _row("Mean / std. dev.", f"{_num(r['mean'], 3)} / {_num(r['std'], 3)}"),
        _row("Skew / excess kurtosis", f"{_num(r['skew'], 3)} / {_num(r['kurtosis_excess'], 3)}"),
        _row(f"Hampel outliers (window {r['outlier_hampel_window']}, z &gt; {r['outlier_hampel_threshold']:g})".replace("&gt;", ">"),
             f"{_int(r['n_outliers_hampel'])}"),
    ])
    return_tests = "".join([
        test_row("Jarque-Bera normality", r["jarque_bera_stat"], r["jarque_bera_pvalue"],
                 _verdict(r["jarque_bera_normal"], "consistent with normal", "normality rejected")),
        test_row(f"Ljung-Box, returns (lag {r['ljung_box_lags']})", r["ljung_box_stat"], r["ljung_box_pvalue"],
                 _verdict(r["ljung_box_autocorrelated"], "autocorrelation present", "no autocorrelation")),
        test_row(f"Ljung-Box, squared returns (lag {r['ljung_box_lags']})", r["ljung_box_sq_stat"], r["ljung_box_sq_pvalue"],
                 _verdict(r["ljung_box_sq_vol_clustering"], "volatility clustering", "no volatility clustering")),
        test_row("ADF", r["adf_stat"], r["adf_pvalue"], _verdict(r["adf_stationary"], "stationary", "non-stationary")),
        test_row("KPSS", r["kpss_stat"], r["kpss_pvalue"], _verdict(r["kpss_stationary"], "stationary", "non-stationary")),
    ])
    figures = "".join([
        _figure(plot_paths, "04_return_hist.png", "Time-normalized log-return distribution against a normal fit"),
        _figure(plot_paths, "05_return_qq.png", "Normal Q-Q plot of returns"),
        _figure(plot_paths, "06_acf_returns.png", "Autocorrelation of returns"),
        _figure(plot_paths, "07_acf_squared_returns.png", "Autocorrelation of squared returns"),
        _figure(plot_paths, "03_interarrival_hist.png", "Inter-arrival time distribution"),
    ])
    col3 = f"""
    <div>
      <div class="column-title">Time series analysis</div>
      <section class="block">
        <h2>Price level (mid)</h2>
        <table><tbody>{price_rows}</tbody></table>
        <h3>Stationarity</h3>
        {_table(["Test", "Statistic", "p-value", "Reading"], price_tests, num_cols={1, 2})}
      </section>
      <section class="block">
        <h2>Returns (log-return / sqrt(dt))</h2>
        <table><tbody>{return_rows}</tbody></table>
        <h3>Hypothesis tests (alpha = {r['significance_level']:g})</h3>
        {_table(["Test", "Statistic", "p-value", "Reading"], return_tests, num_cols={1, 2})}
      </section>
      <section class="block">
        <h2>Distributions</h2>
        {figures}
      </section>
    </div>"""

    meta_html = (f'<div><strong>{_esc(report["source_file"])}</strong></div>'
                 f'<div>Generated {_esc(report["generated_at"].replace("T", " ").replace("+00:00", ""))} UTC</div>')
    explorer = _explorer_html(payload, "Drag to pan, scroll to zoom, double-click for the full session; drag on the overview to "
                                       "select a range. Click any timestamp in the tables to jump to it.")
    body = f"""{explorer}
  <div class="columns">{col1}{col2}{col3}
  </div>"""
    return _page("Market Data Quality Report", "Historical recording usability assessment", meta_html, body, report["generated_at"])


def write_dashboard(report: dict, plot_paths: list[str], out_path: Path, payload: dict | None = None) -> Path:
    atomic_write_text(out_path, render_dashboard(report, plot_paths, payload))
    return out_path


def render_comparison_dashboard(report: dict, plot_paths: list[str], payload: dict | None = None) -> str:
    decimals = payload["meta"]["price_decimals"] if payload else 5
    price = lambda x: _fixed(x, decimals)
    multi_day = pd.Timestamp(report["window_start"]).date() != pd.Timestamp(report["window_end"]).date()
    pairs = report["pairs"]
    status = report.get("status")
    n_div_total = sum(p.get("n_divergences") or 0 for p in pairs)
    n_uncomparable = sum(1 for p in pairs if p["n_common_points"] < 2)
    instrument = report.get("instrument")

    if status == "KO":
        status_value = '<span class="badge badge-critical">KO</span>'
    elif status == "OK":
        status_value = '<span class="badge badge-ok">OK</span>'
    else:
        status_value = '<span class="badge badge-muted">n/a</span>'
    status_sub = ("divergence over threshold or insufficient overlap" if status == "KO" else "all pairs within threshold")
    tiles = "".join([
        _tile("Status", status_value, status_sub, alert=status == "KO"),
        _tile("Divergences", _int(n_div_total), f"grid points over {report['price_diff_threshold_bps']:g} bps", alert=n_div_total > 0),
        _tile("Sources", _int(len(report["sources"])), f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''} compared"),
    ])
    pair_alert_rows = "".join(
        (f'<tr class="exception"><td>{_esc(p["source_a"])} vs {_esc(p["source_b"])}</td>'
         f'<td class="num">{_int(p["n_divergences"])}</td><td class="num">{_fixed(p["max_abs_diff_bps"], 2)}</td>'
         f'<td class="status"><span class="badge badge-critical">Exception</span></td></tr>')
        if p["n_common_points"] >= 2 and p["n_divergences"] > 0 else
        (f'<tr class="exception"><td>{_esc(p["source_a"])} vs {_esc(p["source_b"])}</td><td class="num">n/a</td><td class="num">n/a</td>'
         f'<td class="status"><span class="badge badge-critical">No overlap</span></td></tr>')
        if p["n_common_points"] < 2 else
        (f'<tr><td>{_esc(p["source_a"])} vs {_esc(p["source_b"])}</td><td class="num">0</td>'
         f'<td class="num">{_fixed(p["max_abs_diff_bps"], 2)}</td>'
         f'<td class="status"><span class="badge badge-ok">OK</span></td></tr>')
        for p in pairs
    )
    top_div = sorted(
        ({"pair": f'{p["source_a"]} vs {p["source_b"]}', **d} for p in pairs for d in p.get("top_divergences", [])),
        key=lambda d: abs(d["diff_bps"]), reverse=True,
    )[:CFG.COMPARISON_TOP_N_DIVERGENCES]
    many_pairs = len(pairs) > 1
    div_rows = "".join(
        f'<tr><td class="num">{_goto(d["timestamp"], multi_day)}</td>'
        + (f'<td>{_esc(d["pair"])}</td>' if many_pairs else "")
        + f'<td class="num">{_fixed(d["diff_bps"], 2)}</td>'
        f'<td class="num">{price(d["mid_a"])}</td><td class="num">{price(d["mid_b"])}</td></tr>'
        for d in top_div
    )
    if div_rows:
        headers = ["Time", "Pair", "bps", "Mid A", "Mid B"] if many_pairs else ["Time", "bps", "Mid A", "Mid B"]
        div_html = _table(headers, div_rows, num_cols={0, 2, 3, 4} if many_pairs else {0, 1, 2, 3})
        if not many_pairs:
            div_html = (f'<p class="note">A = {_esc(pairs[0]["source_a"])}, B = {_esc(pairs[0]["source_b"])}; '
                        f'bps = (A - B) / mid.</p>' + div_html)
    else:
        div_html = '<p class="empty">No divergence over the threshold.</p>'
    summary_rows = "".join(
        ([_kv("Instrument / date", f"{_esc(instrument['product'])} / {_esc(instrument['date'])}")] if instrument else []) + [
        _kv("Generated (UTC)", _esc(report["generated_at"].replace("T", " ").replace("+00:00", ""))),
        _kv("Overlapping window", f"{_esc(_fmt_ts(report['window_start'], True))} &ndash; {_esc(_fmt_ts(report['window_end'], multi_day))}"),
        _kv("Alignment grid step", f"{report['alignment_freq_s']:g} s"),
        _kv("Grid points in overlap", _int(report["n_grid_points"])),
        _kv("Max staleness (as-of tolerance)", f"{report['max_staleness_s']:g} s"),
        _kv("Divergence threshold", f"{report['price_diff_threshold_bps']:g} bps"),
    ])
    col1 = f"""
    <div>
      <div class="column-title">Alerts &amp; summary</div>
      <section class="block">
        <div class="tiles">{tiles}</div>
        <h2>Pair verdicts</h2>
        {_table(["Pair", "Divergences", "Max |bps|", "Status"], pair_alert_rows, num_cols={1, 2})}
        <h3>Largest divergences</h3>
        {div_html}
        <p class="note">Click a time to show it in the explorer.</p>
      </section>
      <section class="block">
        <h2>Summary</h2>
        <table class="kv"><tbody>{summary_rows}</tbody></table>
      </section>
    </div>"""

    source_blocks = []
    for src in report["sources"]:
        rows = [_kv("File", _esc(src.get("file", "")))]
        if "metadata" in src:
            m = src["metadata"]
            rows.append(_kv("Market / server / feed", _esc(" / ".join(x or "-" for x in (m["market"], m["server"], m["source"])))))
        rows_val = (_int(src["n_rows"])
                    + (f' <span class="note-cell">(thinned from {_int(src["raw_rows"])})</span>' if "raw_rows" in src else "")
                    + (f' <span class="note-cell">({_int(src["n_zero_rows_ignored"])} zero rows ignored)</span>'
                       if src.get("n_zero_rows_ignored") else ""))
        rows.append(_kv("Rows", rows_val))
        rows.append(_kv("First / last tick", f"{_esc(_fmt_ts(src['session_start'], True))} / {_esc(_fmt_ts(src['session_end'], multi_day))}"))
        source_blocks.append(f'<h3>{_esc(src["source"])}</h3><table class="kv"><tbody>{"".join(rows)}</tbody></table>')
    coverage_rows = "".join(
        f'<tr><td>{_esc(p["source_a"])} vs {_esc(p["source_b"])}</td><td class="num">{_int(p["n_common_points"])} of {_int(p["n_grid_points"])}</td>'
        f'<td class="num">{_pct(p["coverage"], 1)}</td></tr>'
        for p in pairs
    )
    col2 = f"""
    <div>
      <div class="column-title">Recording status</div>
      <section class="block">
        <h2>Sources</h2>
        {"".join(source_blocks)}
      </section>
      <section class="block">
        <h2>Overlap coverage</h2>
        <p class="note">Grid points where both sources had a quote no older than the staleness tolerance.</p>
        {_table(["Pair", "Common points", "Coverage"], coverage_rows, num_cols={1, 2})}
      </section>
    </div>"""

    pair_sections = []
    for p in pairs:
        a, b = p["source_a"], p["source_b"]
        if p["n_common_points"] < 2:
            body = f'<p class="empty">Fewer than 2 common grid points (coverage {_pct(p["coverage"], 1)}) -- not enough overlap to compare.</p>'
        else:
            stat_rows = "".join([
                _row("Mean diff", price(p["mean_diff"])),
                _row("Mean / median |diff|", f"{price(p['mean_abs_diff'])} / {price(p['median_abs_diff'])}"),
                _row("RMS diff", price(p["rms_diff"])),
                _row("Max |diff|", price(p["max_abs_diff"])),
                _row("Mean / max |diff| (bps)", f"{_fixed(p['mean_abs_diff_bps'], 2)} / {_fixed(p['max_abs_diff_bps'], 2)}"),
                _row("Return correlation", _fixed(p["return_correlation"], 3)),
                _exception_row("Divergences over threshold", p["n_divergences"]),
            ])
            body = _table(["Metric", "Value", "Status"], stat_rows, num_cols={1})
        fig = _figure(plot_paths, f"c3_diff_hist_{a}_vs_{b}.png", f"Difference distribution, {a} minus {b} (bps)")
        pair_sections.append(f'<section class="block"><h2>{_esc(a)} vs {_esc(b)}</h2>{body}{fig}</section>')

    attribution_html = ""
    if "attribution" in report:
        ranking = report["attribution"]["source_outlier_ranking"]
        rank_rows = "".join(
            f'<tr><td>{_esc(x["source"])}</td><td class="num">{_fixed(x["avg_diff_vs_others_bps"], 2)}</td><td class="num">{_int(x["n_pairs"])}</td></tr>'
            for x in ranking
        ) or '<tr><td colspan="3" class="empty">Not enough overlapping data in any pair</td></tr>'
        dims = report["attribution"]["dimension_attribution"]
        dim_rows = "".join(
            f'<tr><td>{_esc(d["dimension"])}</td><td>{_esc(", ".join(d["distinct_values"]))}</td>'
            f'<td class="num">{_fixed(d["avg_diff_bps_when_differs"], 2)}</td><td class="num">{_fixed(d["avg_diff_bps_when_matches"], 2)}</td>'
            f'<td class="num">{_int(d["n_pairs_differs"])} / {_int(d["n_pairs_matches"])}</td></tr>'
            for d in dims
        ) or '<tr><td colspan="5" class="empty">Every recording shares the same market/server/feed, or too few pairs to contrast</td></tr>'
        attribution_html = f"""
      <section class="block">
        <h2>Divergence attribution</h2>
        <p class="note">Descriptive cross-referencing, not a verdict -- with few recordings a dimension can be confounded with another.</p>
        <h3>Which recording disagrees most with the rest</h3>
        {_table(["Source", "Avg |diff| vs others (bps)", "Pairs"], rank_rows, num_cols={1, 2})}
        <h3>Divergence by dimension</h3>
        {_table(["Dimension", "Values", "Differs (bps)", "Matches (bps)", "Pairs"], dim_rows, num_cols={2, 3, 4})}
      </section>"""
    col3 = f"""
    <div>
      <div class="column-title">Pair analysis</div>
      {attribution_html}
      {"".join(pair_sections)}
    </div>"""

    title = "Feed Comparison Report" + (f" -- {instrument['product']}" if instrument else "")
    meta_html = ((f'<div><strong>{_esc(instrument["product"])} &middot; {_esc(instrument["date"])}</strong></div>' if instrument
                  else f'<div><strong>{_esc(", ".join(s["source"] for s in report["sources"]))}</strong></div>')
                 + f'<div>Generated {_esc(report["generated_at"].replace("T", " ").replace("+00:00", ""))} UTC</div>')
    explorer = _explorer_html(payload, "Drag to pan, scroll to zoom, double-click for the full window; drag on the overview to "
                                       "select a range. Switch series to see each pair's difference in bps against the threshold.")
    body = f"""{explorer}
  <div class="columns">{col1}{col2}{col3}
  </div>"""
    return _page(title, "Cross-source time-series comparison, same instrument", meta_html, body, report["generated_at"])


def write_comparison_dashboard(report: dict, plot_paths: list[str], out_path: Path, payload: dict | None = None) -> Path:
    atomic_write_text(out_path, render_comparison_dashboard(report, plot_paths, payload))
    return out_path


RECORDING_EXTENSIONS = {".csv", ".txt"}


def fatal(msg: str) -> None:
    logger.error(msg)
    raise SystemExit(2)


def _export_report(name: str, content: str, report_dir: Path | str) -> None:
    json_dir = Path(report_dir) / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(json_dir / f"{name}.json", content)

    if CFG.GRAFANA_EXPORT_DIR:
        try:
            external_dir = Path(CFG.GRAFANA_EXPORT_DIR)
            external_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(external_dir / f"{name}.json", content)
        except Exception:
            logger.exception(
                "Could not write Grafana export copy for %s to config.ini "
                "[general] grafana_export_dir=%s", name, CFG.GRAFANA_EXPORT_DIR,
            )


INDEX_MAX_ENTRIES = 2000
_INDEX_FIELDS = ("name", "kind", "status", "generated_at", "date", "product",
                 "n_sources", "n_pairs", "n_divergences", "max_abs_diff_bps",
                 "n_exceptions", "n_checks", "error")


def _index_entry(**kwargs) -> dict:
    return {field: kwargs.get(field) for field in _INDEX_FIELDS}


def _write_index_copy(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


def _update_index(entry: dict, report_dir: Path | str) -> None:
    index_path = Path(report_dir) / "json" / "index.json"
    try:
        existing = json.loads(index_path.read_text()) if index_path.exists() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    key = (entry["name"], entry["kind"])
    existing = [e for e in existing if (e.get("name"), e.get("kind")) != key]
    existing.append(entry)
    existing.sort(key=lambda e: e.get("generated_at") or "")
    existing = existing[-INDEX_MAX_ENTRIES:]
    content = json.dumps(existing, indent=2)

    _write_index_copy(index_path, content)
    if CFG.GRAFANA_EXPORT_DIR:
        try:
            _write_index_copy(Path(CFG.GRAFANA_EXPORT_DIR) / "index.json", content)
        except Exception:
            logger.exception("Could not write Grafana index copy to %s", CFG.GRAFANA_EXPORT_DIR)


def run(input_path: Path, output_dir: Path, report_dir: Path) -> dict:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    check_file_is_safe_to_load(input_path)

    df_raw, n_zero_rows = load_raw(str(input_path))
    if len(df_raw) < CFG.MIN_ROWS:
        ignored = f" after ignoring {n_zero_rows} zero-quote row(s)" if n_zero_rows else ""
        raise ValueError(
            f"Recording has only {len(df_raw)} rows{ignored}; need at least "
            f"{CFG.MIN_ROWS} for a meaningful assessment "
            f"(see config.ini [general] min_rows)."
        )
    df = enrich(df_raw)
    check_memory_budget(f"loading {input_path.name}")

    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"

    report = {
        "source_file": input_path.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structural": check_structural(df_raw, n_zero_rows),
        "continuity": check_continuity(df),
        "price_level": check_price_level(df),
        "returns": check_returns(df),
        "liquidity": check_liquidity(df),
    }
    check_memory_budget(f"computing statistics for {input_path.name}")

    plot_paths = generate_all_plots(df, str(plots_dir))
    report["plots"] = [str(Path(p).relative_to(output_dir)) for p in plot_paths]
    check_memory_budget(f"generating plots for {input_path.name}")

    if CFG.OUTLIER_REPLACEMENT_MODE != MODE_NONE:
        cleaned = treat_outliers(df, CFG.OUTLIER_REPLACEMENT_MODE)
        cleaned_name = "cleaned_data.csv"
        atomic_write_text(output_dir / cleaned_name, cleaned[raw_schema_columns(cleaned)].to_csv(index=False))
        report["cleaned_data_file"] = cleaned_name
        check_memory_budget(f"writing cleaned data for {input_path.name}")

    atomic_write_text(output_dir / "quality_report.json", json.dumps(report, indent=2))
    payload = build_report_chart_payload(df, df_raw, report)
    write_dashboard(report, report["plots"], output_dir / "dashboard.html", payload)
    check_memory_budget(f"writing dashboard for {input_path.name}")

    _export_report(input_path.stem, json.dumps(report, indent=2), report_dir)
    n_exceptions, n_checks = count_exceptions(report)
    _update_index(_index_entry(
        name=input_path.stem, kind="report", generated_at=report["generated_at"],
        n_exceptions=n_exceptions, n_checks=n_checks,
    ), report_dir)

    return report


def resolve_inputs(raw_paths: list[str]) -> list[Path]:
    resolved = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            resolved.extend(sorted(
                f for f in path.iterdir()
                if f.is_file() and f.suffix.lower() in RECORDING_EXTENSIONS
            ))
        else:
            resolved.append(path)
    return resolved


def _label_sources(paths: list[Path]) -> dict[str, Path]:
    stems = [p.stem for p in paths]
    labels: dict[str, Path] = {}
    for path, stem in zip(paths, stems):
        label = stem if stems.count(stem) == 1 else f"{path.parent.name}_{stem}"
        if label in labels and labels[label] != path:
            raise ValueError(
                f"Source label '{label}' is ambiguous between {labels[label]} and {path} "
                f"-- rename one of the files (or its parent directory) so compare_inputs can "
                f"tell them apart."
            )
        labels[label] = path
    return labels


def _run_comparison_core(
    labeled_paths: dict[str, Path],
    output_dir: Path,
    report_dir: Path,
    metadata_by_label: dict[str, FeedMetadata] | None = None,
    instrument: dict | None = None,
) -> dict:
    downsample_points_per_day = CFG.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY

    sources = {}
    raw_row_counts = {}
    zero_row_counts = {}
    for label, path in labeled_paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {path}")
        check_file_is_safe_to_load(path)
        df_raw, zero_row_counts[label] = load_raw(str(path))
        if len(df_raw) < CFG.MIN_ROWS:
            ignored = f" after ignoring {zero_row_counts[label]} zero-quote row(s)" if zero_row_counts[label] else ""
            raise ValueError(
                f"{path}: only {len(df_raw)} rows{ignored}; need at least {CFG.MIN_ROWS} "
                f"(see config.ini [general] min_rows)."
            )
        n_raw = len(df_raw)
        df_raw = downsample(df_raw, downsample_points_per_day, CFG.COMPARISON_DOWNSAMPLE_MIN_ROWS)
        if len(df_raw) < n_raw:
            raw_row_counts[label] = n_raw
            logger.info(
                "Downsampled %s: %d -> %d ticks (target ~%d/day)",
                path.name, n_raw, len(df_raw), downsample_points_per_day,
            )
        sources[label] = enrich(df_raw)
    effective_staleness_s = CFG.COMPARISON_MAX_STALENESS_S
    if raw_row_counts:
        effective_staleness_s = max(effective_staleness_s, SECONDS_PER_DAY / downsample_points_per_day)
        logger.info("Staleness tolerance set to %.1fs because %d source(s) were thinned",
                    effective_staleness_s, len(raw_row_counts))
    check_memory_budget("loading comparison sources")

    report, aligned = compare_sources(sources, max_staleness_s=effective_staleness_s)
    check_memory_budget("aligning comparison sources")

    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in report["sources"]:
        label = s["source"]
        s["file"] = labeled_paths[label].name
        s["n_zero_rows_ignored"] = zero_row_counts[label]
        if label in raw_row_counts:
            s["raw_rows"] = raw_row_counts[label]
        if metadata_by_label:
            m = metadata_by_label[label]
            s["metadata"] = {"date": m.date, "product": m.product, "market": m.market,
                             "server": m.server, "source": m.source}

    if metadata_by_label:
        report["attribution"] = attribute_divergence(report["pairs"], metadata_by_label)
    if instrument:
        report["instrument"] = instrument
    report["status"] = compute_status(report)

    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plot_paths = generate_all_comparison_plots(
        aligned, list(sources.keys()), CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, str(plots_dir)
    )
    report["plots"] = [str(Path(p).relative_to(output_dir)) for p in plot_paths]
    check_memory_budget("generating comparison plots")

    atomic_write_text(output_dir / "comparison_report.json", json.dumps(report, indent=2))
    price_decimals = min(8, max(_infer_price_decimals(df) for df in sources.values()) + 1)
    payload = build_comparison_chart_payload(aligned, list(sources.keys()), price_decimals)
    write_comparison_dashboard(report, report["plots"], output_dir / "comparison_dashboard.html", payload)

    _export_report(output_dir.name, json.dumps(report, indent=2), report_dir)
    n_divergences = sum(p.get("n_divergences") or 0 for p in report["pairs"])
    pair_diffs = [p["max_abs_diff_bps"] for p in report["pairs"] if p.get("max_abs_diff_bps") is not None]
    _update_index(_index_entry(
        name=output_dir.name, kind="compare", status=report["status"], generated_at=report["generated_at"],
        date=(instrument or {}).get("date"), product=(instrument or {}).get("product"),
        n_sources=len(report["sources"]), n_pairs=len(report["pairs"]), n_divergences=n_divergences,
        max_abs_diff_bps=max(pair_diffs) if pair_diffs else None,
    ), report_dir)

    return report


def run_comparison(input_paths: list[Path], output_dir: Path, report_dir: Path) -> dict:
    return _run_comparison_core(_label_sources(input_paths), output_dir, report_dir)


def run_comparison_group(members: list[FeedMetadata], output_dir: Path, report_dir: Path) -> dict:
    labeled_paths: dict[str, Path] = {}
    metadata_by_label: dict[str, FeedMetadata] = {}
    for m in members:
        label = m.label
        if label in labeled_paths and labeled_paths[label] != m.path:
            raise ValueError(
                f"Ambiguous recording label '{label}' within {m.date}/{m.product}: "
                f"{labeled_paths[label]} and {m.path} -- market+server+source should be "
                f"unique per instrument per day."
            )
        labeled_paths[label] = m.path
        metadata_by_label[label] = m
    instrument = {"date": members[0].date, "product": members[0].product}
    return _run_comparison_core(labeled_paths, output_dir, report_dir,
                                metadata_by_label=metadata_by_label, instrument=instrument)


def _write_export_error(name: str, report_dir: str, error: BaseException, kind: str) -> None:
    try:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {"status": "ERROR", "generated_at": generated_at, "error": str(error)}
        _export_report(name, json.dumps(record, indent=2), report_dir)
        _update_index(_index_entry(
            name=name, kind=kind, status="ERROR", generated_at=generated_at, error=str(error),
        ), report_dir)
    except Exception:
        logger.exception("Could not write failure export for %s", name)


def run_report_mode(input_paths: list[Path], report_dir: str) -> None:
    logger.info(
        "Run started: report mode | %d input(s) | gap_threshold_s=%s alpha=%s hampel_window=%s "
        "hampel_threshold=%s outlier_replacement_mode=%s min_rows=%s max_memory_budget_gb=%s",
        len(input_paths), CFG.GAP_THRESHOLD_S, CFG.SIGNIFICANCE_LEVEL,
        CFG.HAMPEL_WINDOW, CFG.HAMPEL_THRESHOLD, CFG.OUTLIER_REPLACEMENT_MODE,
        CFG.MIN_ROWS, CFG.MAX_MEMORY_BUDGET_GB,
    )

    batch_start = time.perf_counter()
    failures = []
    for input_path in input_paths:
        output_dir = Path(report_dir) / input_path.stem
        file_start = time.perf_counter()
        try:
            report = run(input_path, output_dir, Path(report_dir))
        except Exception as exc:
            logger.exception("FAILED processing %s", input_path)
            _write_export_error(input_path.stem, report_dir, exc, kind="report")
            failures.append(input_path)
            continue
        elapsed = time.perf_counter() - file_start
        n_exceptions, n_checks = count_exceptions(report)
        logger.info(
            "OK %s -> %s (%d/%d data-integrity exceptions, %.1fs)",
            input_path, output_dir / "dashboard.html", n_exceptions, n_checks, elapsed,
        )

    total_elapsed = time.perf_counter() - batch_start
    n_ok = len(input_paths) - len(failures)
    logger.info("Run finished: %d/%d recordings processed successfully in %.1fs",
                n_ok, len(input_paths), total_elapsed)

    if failures:
        logger.error("%d recording(s) failed: %s", len(failures), [str(p) for p in failures])
        raise SystemExit(1)


def _check_group_size(members: list, group_name: str) -> bool:
    if len(members) > CFG.COMPARISON_MAX_SOURCES:
        logger.error(
            "FAILED %s: %d recordings exceeds config.ini [comparison] max_sources=%d "
            "(pairs grow as n*(n-1)/2) -- narrow the group or raise max_sources.",
            group_name, len(members), CFG.COMPARISON_MAX_SOURCES,
        )
        return False
    return True


def run_compare_mode(input_paths: list[Path], report_dir: str) -> None:
    if CFG.COMPARISON_ALIGNMENT_FREQ_S <= 0:
        fatal(
            f"config.ini [comparison] alignment_freq_s={CFG.COMPARISON_ALIGNMENT_FREQ_S} "
            f"must be positive"
        )

    parsed, unmatched = [], []
    for path in input_paths:
        meta = parse_filename_metadata(path)
        if meta is None:
            unmatched.append(path)
        else:
            parsed.append(meta)

    if parsed:
        for path in unmatched:
            logger.warning(
                "Excluding %s from auto-grouping: doesn't match the "
                "YYYYMMDD_product_market_server_source naming convention", path,
            )
        _run_compare_grouped(parsed, report_dir)
    else:
        _run_compare_legacy(input_paths, report_dir)


def _run_compare_grouped(metas: list[FeedMetadata], report_dir: str) -> None:
    groups = group_by_instrument(metas)
    comparable = {key: members for key, members in groups.items() if len(members) >= 2}
    singletons = {key: members for key, members in groups.items() if len(members) < 2}

    logger.info(
        "Run started: compare mode (auto-grouped by date+product) | %d file(s) -> %d "
        "instrument(s) (%d comparable, %d with only 1 recording) | alignment_freq_s=%s "
        "max_staleness_s=%s price_diff_threshold_bps=%s downsample_points_per_day=%s",
        len(metas), len(groups), len(comparable), len(singletons),
        CFG.COMPARISON_ALIGNMENT_FREQ_S, CFG.COMPARISON_MAX_STALENESS_S,
        CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, CFG.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY,
    )
    for date, product in singletons:
        logger.warning("Skipping %s/%s: only 1 recording, nothing to compare it against", date, product)

    if not comparable:
        fatal("no instrument (date+product) has 2+ recordings to compare -- see the warnings above")

    batch_start = time.perf_counter()
    failures = []
    for (date, product), members in comparable.items():
        group_name = f"{date}/{product}"
        export_name = f"{date}_{product}"
        if not _check_group_size(members, group_name):
            _write_export_error(export_name, report_dir, ValueError(f"{len(members)} recordings exceeds max_sources"), kind="compare")
            failures.append(group_name)
            continue
        group_output_dir = Path(report_dir) / export_name
        start = time.perf_counter()
        try:
            run_comparison_group(members, group_output_dir, Path(report_dir))
        except Exception as exc:
            logger.exception("FAILED comparing %s (%d recordings)", group_name, len(members))
            _write_export_error(export_name, report_dir, exc, kind="compare")
            failures.append(group_name)
            continue
        logger.info(
            "OK %s (%d recordings) -> %s (%.1fs)",
            group_name, len(members), group_output_dir / "comparison_dashboard.html",
            time.perf_counter() - start,
        )

    total_elapsed = time.perf_counter() - batch_start
    logger.info("Run finished: %d/%d instrument(s) compared successfully in %.1fs",
                len(comparable) - len(failures), len(comparable), total_elapsed)
    if failures:
        logger.error("%d instrument(s) failed: %s", len(failures), failures)
        raise SystemExit(1)


def _run_compare_legacy(input_paths: list[Path], report_dir: str) -> None:
    if len(input_paths) < 2:
        fatal("compare_inputs needs at least 2 recordings to compare")
    export_name = f"compare_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if not _check_group_size(input_paths, "this comparison"):
        _write_export_error(export_name, report_dir, ValueError(f"{len(input_paths)} recordings exceeds max_sources"), kind="compare")
        raise SystemExit(1)

    logger.info(
        "Run started: compare mode | %d source(s) -- none match the YYYYMMDD_product naming "
        "convention, so this run can't be named by date+product | alignment_freq_s=%s "
        "max_staleness_s=%s price_diff_threshold_bps=%s downsample_points_per_day=%s "
        "min_rows=%s max_memory_budget_gb=%s",
        len(input_paths), CFG.COMPARISON_ALIGNMENT_FREQ_S, CFG.COMPARISON_MAX_STALENESS_S,
        CFG.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, CFG.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY,
        CFG.MIN_ROWS, CFG.MAX_MEMORY_BUDGET_GB,
    )

    compare_output_dir = Path(report_dir) / export_name
    start = time.perf_counter()
    try:
        run_comparison(input_paths, compare_output_dir, Path(report_dir))
    except Exception as exc:
        logger.exception("FAILED comparing %d sources", len(input_paths))
        _write_export_error(export_name, report_dir, exc, kind="compare")
        raise SystemExit(1)
    logger.info(
        "OK comparison of %d sources -> %s (%.1fs)",
        len(input_paths), compare_output_dir / "comparison_dashboard.html",
        time.perf_counter() - start,
    )


SYN_OUT_PATH = "data/synthetic_eurusd_quotes.txt"
SYN_SEED = 42

SYN_SESSION_START = pd.Timestamp("2024-03-04 08:00:00")
SYN_SESSION_SECONDS = 8 * 60 * 60
SYN_MEAN_INTER_ARRIVAL_S = 1.2

SYN_START_PRICE = 1.08500
SYN_ANNUALIZED_VOL = 0.07
SYN_TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600

SYN_MEAN_SPREAD = 0.00015
SYN_SIZE_MIN, SYN_SIZE_MAX = 500, 20000

SYN_FEED_B_OUT_PATH = "data/synthetic_eurusd_quotes_feedB.txt"
SYN_FEED_B_SEED = 43
SYN_FEED_B_SUBSAMPLE_FRAC = 0.85
SYN_FEED_B_LATENCY_MEAN_S = 0.15
SYN_FEED_B_LATENCY_STD_S = 0.05
SYN_FEED_B_NOISE_STD = 0.000015
SYN_FEED_B_N_DIVERGENCES = 5
SYN_FEED_B_DIVERGENCE_MIN, SYN_FEED_B_DIVERGENCE_MAX = 0.0006, 0.0012

SYN_QUOTE_COLUMNS = [
    "best_bid_1", "best_ask_1", "best_bid_2", "best_ask_2",
]


def generate_synthetic() -> pd.DataFrame:
    rng = np.random.default_rng(SYN_SEED)

    n_est = int(SYN_SESSION_SECONDS / SYN_MEAN_INTER_ARRIVAL_S * 1.2)
    elapsed = np.cumsum(rng.exponential(SYN_MEAN_INTER_ARRIVAL_S, size=n_est))
    elapsed = elapsed[elapsed <= SYN_SESSION_SECONDS]
    timestamps = SYN_SESSION_START + pd.to_timedelta(elapsed, unit="s")
    n = len(timestamps)

    dt = np.diff(elapsed, prepend=0.0).clip(min=1e-6)
    sigma_per_sec = SYN_ANNUALIZED_VOL / np.sqrt(SYN_TRADING_SECONDS_PER_YEAR)
    log_returns = rng.normal(0.0, sigma_per_sec * np.sqrt(dt))
    log_returns[0] = 0.0
    mid = SYN_START_PRICE * np.exp(np.cumsum(log_returns))

    half_spread = np.abs(rng.normal(SYN_MEAN_SPREAD / 2, SYN_MEAN_SPREAD / 6, size=n)) + 1e-5
    best_bid_1 = mid - half_spread
    best_ask_1 = mid + half_spread

    level2_extra = np.abs(rng.normal(SYN_MEAN_SPREAD, SYN_MEAN_SPREAD / 4, size=n)) + 1e-5
    best_bid_2 = best_bid_1 - level2_extra
    best_ask_2 = best_ask_1 + level2_extra

    def sizes():
        return rng.integers(SYN_SIZE_MIN, SYN_SIZE_MAX, size=n)

    return pd.DataFrame({
        "timestamp": timestamps,
        "best_bid_size_1": sizes(),
        "best_bid_1": best_bid_1.round(5),
        "best_ask_1": best_ask_1.round(5),
        "best_ask_size_1": sizes(),
        "best_bid_size_2": sizes(),
        "best_bid_2": best_bid_2.round(5),
        "best_ask_2": best_ask_2.round(5),
        "best_ask_size_2": sizes(),
    })


def generate_synthetic_feed_b(feed_a: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(SYN_FEED_B_SEED)

    df = feed_a.sample(frac=SYN_FEED_B_SUBSAMPLE_FRAC, random_state=SYN_FEED_B_SEED).sort_values("timestamp")
    df = df.reset_index(drop=True)
    n = len(df)

    latency_s = rng.normal(SYN_FEED_B_LATENCY_MEAN_S, SYN_FEED_B_LATENCY_STD_S, size=n).clip(min=0.0)
    df["timestamp"] = df["timestamp"] + pd.to_timedelta(latency_s, unit="s")
    df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)

    noise = rng.normal(0.0, SYN_FEED_B_NOISE_STD, size=n)
    for col in SYN_QUOTE_COLUMNS:
        df[col] = (df[col] + noise).round(5)

    divergence_idx = rng.choice(n, size=min(SYN_FEED_B_N_DIVERGENCES, n), replace=False)
    shocks = rng.choice([-1.0, 1.0], size=len(divergence_idx)) * rng.uniform(
        SYN_FEED_B_DIVERGENCE_MIN, SYN_FEED_B_DIVERGENCE_MAX, size=len(divergence_idx)
    )
    for idx, shock in zip(divergence_idx, shocks):
        df.loc[idx, SYN_QUOTE_COLUMNS] = (df.loc[idx, SYN_QUOTE_COLUMNS] + shock).round(5)

    def sizes():
        return rng.integers(SYN_SIZE_MIN, SYN_SIZE_MAX, size=n)

    df["best_bid_size_1"], df["best_ask_size_1"] = sizes(), sizes()
    df["best_bid_size_2"], df["best_ask_size_2"] = sizes(), sizes()
    return df


USAGE = (
    "Usage: mdsq.py <path-to-config-file>\n"
    "       mdsq.py treat-outliers <input.csv> [--mode N] [-o out.csv] [--config cfg.ini]\n"
    "       mdsq.py generate-synthetic"
)


def _expand(patterns: list[str]) -> list[Path]:
    matches = []
    for pattern in patterns:
        matches.extend(sorted(glob.glob(pattern)))
    unique, seen = [], set()
    for path in resolve_inputs(matches):
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _run_pipeline(argv: list[str]) -> None:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    config_path = Path(argv[0])
    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        raise SystemExit(2)

    load_config(config_path.resolve())
    configure_logging()
    apply_process_memory_limit()

    if CFG.OUTLIER_REPLACEMENT_MODE not in VALID_MODES:
        fatal(
            f"config.ini [outliers] replacement_mode={CFG.OUTLIER_REPLACEMENT_MODE} is invalid; "
            f"must be one of {sorted(VALID_MODES)}"
        )

    if not CFG.RUN_REPORT_INPUTS and not CFG.RUN_COMPARE_INPUTS:
        fatal("config [run] has neither report_inputs nor compare_inputs set -- nothing to do")

    failed = False

    if CFG.RUN_REPORT_INPUTS:
        paths = _expand(CFG.RUN_REPORT_INPUTS)
        if not paths:
            logger.warning("report_inputs matched no files: %s", CFG.RUN_REPORT_INPUTS)
        else:
            try:
                run_report_mode(paths, CFG.RUN_REPORT_DIR)
            except SystemExit:
                failed = True

    if CFG.RUN_COMPARE_INPUTS:
        paths = _expand(CFG.RUN_COMPARE_INPUTS)
        if not paths:
            logger.warning("compare_inputs matched no files: %s", CFG.RUN_COMPARE_INPUTS)
        else:
            try:
                run_compare_mode(paths, CFG.RUN_REPORT_DIR)
            except SystemExit:
                failed = True

    if failed:
        raise SystemExit(1)


def _cli_treat_outliers(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="mdsq.py treat-outliers",
        description="Correct the ticks the rolling Hampel filter flags, independently of the main pipeline.",
    )
    parser.add_argument("input", help="Quote recording to clean (csv/txt)")
    parser.add_argument("-o", "--output", help="Path for the cleaned CSV (default: <input>_cleaned.csv)")
    parser.add_argument("--mode", type=int, default=None, choices=sorted(VALID_MODES),
                        help="0 none, 1 nan, 2 interpolate, 3 ffill, 4 rolling_median "
                             "(default: [outliers] replacement_mode from the config file)")
    parser.add_argument("--config", default=str(ROOT / "config.ini"),
                        help="Config file to read column mapping and tunables from (default: %(default)s)")
    args = parser.parse_args(argv)

    load_config(args.config)
    mode = CFG.OUTLIER_REPLACEMENT_MODE if args.mode is None else args.mode

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path.with_name(f"{in_path.stem}_cleaned.csv")

    df = load_quotes(str(in_path))
    n_flagged = int(outlier_mask(df).sum())
    cleaned = treat_outliers(df, mode=mode)
    cleaned[raw_schema_columns(cleaned)].to_csv(out_path, index=False)
    print(f"Flagged {n_flagged}/{len(df)} ticks, mode={mode} -> wrote {out_path}")


def _cli_generate_synthetic(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="mdsq.py generate-synthetic",
        description=f"Regenerate {SYN_OUT_PATH} and {SYN_FEED_B_OUT_PATH} (relative to the current directory).",
    )
    parser.parse_args(argv)

    feed_a = generate_synthetic()
    feed_a.to_csv(SYN_OUT_PATH, index=False)
    print(f"Wrote {len(feed_a)} rows to {SYN_OUT_PATH}")

    feed_b = generate_synthetic_feed_b(feed_a)
    feed_b.to_csv(SYN_FEED_B_OUT_PATH, index=False)
    print(f"Wrote {len(feed_b)} rows to {SYN_FEED_B_OUT_PATH}")


SUBCOMMANDS = {
    "treat-outliers": _cli_treat_outliers,
    "generate-synthetic": _cli_generate_synthetic,
}


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        SUBCOMMANDS[argv[0]](argv[1:])
        return
    _run_pipeline(argv)


if __name__ == "__main__":
    main()

