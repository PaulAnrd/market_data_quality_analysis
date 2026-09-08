from __future__ import annotations

import configparser
import os
from pathlib import Path

_CONFIG_PATH = Path(os.environ.get("MDSQ_CONFIG_PATH") or Path(__file__).parent / "config.ini")

_parser = configparser.ConfigParser()
if not _parser.read(_CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found: {_CONFIG_PATH}")

MIN_ROWS = _parser.getint("general", "min_rows")
if MIN_ROWS < 1:
    raise ValueError(f"config.ini [general] min_rows={MIN_ROWS} must be at least 1.")
MAX_MEMORY_BUDGET_GB = _parser.getfloat("general", "max_memory_budget_gb")
MEMORY_OVERHEAD_MULTIPLIER = _parser.getfloat("general", "memory_overhead_multiplier")
GRAFANA_EXPORT_DIR = _parser.get("general", "grafana_export_dir").strip()


def _alternatives(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


COLUMNS_MAP = {
    "timestamp": _alternatives(_parser.get("columns", "timestamp")),
    "best_bid_1": _alternatives(_parser.get("columns", "best_bid_1")),
    "best_ask_1": _alternatives(_parser.get("columns", "best_ask_1")),
    "best_bid_size_1": _alternatives(_parser.get("columns", "best_bid_size_1")),
    "best_ask_size_1": _alternatives(_parser.get("columns", "best_ask_size_1")),
}
COLUMNS_MAX_LEVELS = _parser.getint("columns", "max_levels")
if not (1 <= COLUMNS_MAX_LEVELS <= 7):
    raise ValueError(
        f"config.ini [columns] max_levels={COLUMNS_MAX_LEVELS} must be between 1 and 7 "
        f"(this tool's canonical schema supports at most 7 order-book levels)."
    )
COLUMNS_LEVEL_TEMPLATES = {
    "best_bid": _alternatives(_parser.get("columns", "best_bid_level_template")),
    "best_ask": _alternatives(_parser.get("columns", "best_ask_level_template")),
    "best_bid_size": _alternatives(_parser.get("columns", "best_bid_size_level_template")),
    "best_ask_size": _alternatives(_parser.get("columns", "best_ask_size_level_template")),
}

LOG_BACKUP_DAYS = _parser.getint("logging", "backup_days")

GAP_THRESHOLD_S = _parser.getfloat("continuity", "gap_threshold_s")
TOP_N_GAPS = _parser.getint("continuity", "top_n_gaps")

SIGNIFICANCE_LEVEL = _parser.getfloat("returns", "significance_level")
LJUNG_BOX_LAGS = _parser.getint("returns", "ljung_box_lags")
DT_NORM_FLOOR_QUANTILE = _parser.getfloat("returns", "dt_norm_floor_quantile")

HAMPEL_WINDOW = _parser.getint("outliers", "hampel_window")
HAMPEL_THRESHOLD = _parser.getfloat("outliers", "hampel_threshold")
OUTLIER_REPLACEMENT_MODE = _parser.getint("outliers", "replacement_mode")

COMPARISON_ALIGNMENT_FREQ_S = _parser.getfloat("comparison", "alignment_freq_s")
COMPARISON_MAX_STALENESS_S = _parser.getfloat("comparison", "max_staleness_s")
COMPARISON_PRICE_DIFF_THRESHOLD_BPS = _parser.getfloat("comparison", "price_diff_threshold_bps")
COMPARISON_TOP_N_DIVERGENCES = _parser.getint("comparison", "top_n_divergences")
COMPARISON_MAX_GRID_POINTS = _parser.getint("comparison", "max_grid_points")
COMPARISON_MAX_SOURCES = _parser.getint("comparison", "max_sources")
COMPARISON_DOWNSAMPLE_POINTS_PER_DAY = _parser.getint("comparison", "downsample_points_per_day")
COMPARISON_DOWNSAMPLE_MIN_ROWS = _parser.getint("comparison", "downsample_min_rows")

RUN_REPORT_INPUTS = _alternatives(_parser.get("run", "report_inputs", fallback=""))
RUN_COMPARE_INPUTS = _alternatives(_parser.get("run", "compare_inputs", fallback=""))
RUN_REPORT_DIR = _parser.get("run", "report_dir", fallback="report").strip() or "report"
