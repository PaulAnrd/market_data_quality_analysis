from __future__ import annotations

import pandas as pd

import loader


def check_liquidity(df: pd.DataFrame) -> dict:
    spread = df["spread"]
    levels = loader.present_levels(df)
    size_cols = [c for lvl in levels for c in (f"best_bid_size_{lvl}", f"best_ask_size_{lvl}")]

    return {
        "spread_mean": float(spread.mean()),
        "spread_median": float(spread.median()),
        "spread_std": float(spread.std()),
        "spread_min": float(spread.min()),
        "spread_max": float(spread.max()),
        "n_negative_spread": int((spread < 0).sum()),
        "n_zero_spread": int((spread == 0).sum()),
        "n_zero_or_negative_sizes": int((df[size_cols] <= 0).any(axis=1).sum()),
        "bid_size_1_median": float(df["best_bid_size_1"].median()),
        "ask_size_1_median": float(df["best_ask_size_1"].median()),
    }
