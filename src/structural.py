from __future__ import annotations

import numpy as np
import pandas as pd

import loader


def check_structural(df_raw: pd.DataFrame) -> dict:
    ts = df_raw["timestamp"]
    diffs = ts.diff().dt.total_seconds()

    n_duplicate_timestamps = int((diffs == 0).sum())
    n_out_of_order = int((diffs < 0).sum())

    crossed = df_raw["best_bid_1"] >= df_raw["best_ask_1"]

    levels = loader.present_levels(df_raw)
    level_bid_inconsistent = pd.Series(False, index=df_raw.index)
    level_ask_inconsistent = pd.Series(False, index=df_raw.index)
    for lo, hi in zip(levels, levels[1:]):
        level_bid_inconsistent |= df_raw[f"best_bid_{hi}"] > df_raw[f"best_bid_{lo}"]
        level_ask_inconsistent |= df_raw[f"best_ask_{hi}"] < df_raw[f"best_ask_{lo}"]

    size_cols = [c for lvl in levels for c in (f"best_bid_size_{lvl}", f"best_ask_size_{lvl}")]
    non_positive_sizes = (df_raw[size_cols] <= 0).any(axis=1)

    n_missing = int(df_raw.isna().sum().sum())

    return {
        "n_rows": int(len(df_raw)),
        "n_levels_present": len(levels),
        "n_duplicate_timestamps": n_duplicate_timestamps,
        "n_out_of_order_timestamps": n_out_of_order,
        "n_crossed_book": int(crossed.sum()),
        "n_level_bid_inconsistent": int(level_bid_inconsistent.sum()),
        "n_level_ask_inconsistent": int(level_ask_inconsistent.sum()),
        "n_non_positive_sizes": int(non_positive_sizes.sum()),
        "n_missing_values": n_missing,
    }
