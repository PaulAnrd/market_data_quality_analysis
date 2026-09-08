from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import config
import loader
import outliers

MODE_NONE = 0
MODE_NAN = 1
MODE_INTERPOLATE = 2
MODE_FFILL = 3
MODE_ROLLING_MEDIAN = 4
VALID_MODES = {MODE_NONE, MODE_NAN, MODE_INTERPOLATE, MODE_FFILL, MODE_ROLLING_MEDIAN}

REPLACEMENT_MODE = config.OUTLIER_REPLACEMENT_MODE


def _replace_nan(mid: pd.Series, mask: pd.Series) -> float:
    return np.nan


def _replace_interpolate(mid: pd.Series, mask: pd.Series) -> pd.Series:
    return mid.mask(mask).interpolate(method="linear", limit_direction="both")[mask]


def _replace_ffill(mid: pd.Series, mask: pd.Series) -> pd.Series:
    return mid.mask(mask).ffill()[mask]


def _replace_rolling_median(mid: pd.Series, mask: pd.Series) -> pd.Series:
    min_periods = outliers.HAMPEL_WINDOW // 2 + 1
    rolling = mid.rolling(outliers.HAMPEL_WINDOW, center=True, min_periods=min_periods).median()
    return rolling[mask]


_REPLACERS = {
    MODE_NAN: _replace_nan,
    MODE_INTERPOLATE: _replace_interpolate,
    MODE_FFILL: _replace_ffill,
    MODE_ROLLING_MEDIAN: _replace_rolling_median,
}


def _shifted_quote_columns(df: pd.DataFrame) -> list[str]:
    cols = []
    for lvl in loader.present_levels(df):
        cols.extend([f"best_bid_{lvl}", f"best_ask_{lvl}"])
    return cols


def treat_outliers(df: pd.DataFrame, mode: int = REPLACEMENT_MODE) -> pd.DataFrame:
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown outlier replacement mode: {mode!r}, must be one of {sorted(VALID_MODES)}")
    if mode == MODE_NONE:
        return df.copy()

    mask = outliers.outlier_mask(df)
    df = df.copy()
    if not mask.any():
        return df

    new_mid = _REPLACERS[mode](df["mid"], mask)
    delta = new_mid - df.loc[mask, "mid"]
    for col in _shifted_quote_columns(df):
        df.loc[mask, col] = df.loc[mask, col] + delta
    df.loc[mask, "mid"] = new_mid
    df.loc[mask, "spread"] = df.loc[mask, "best_ask_1"] - df.loc[mask, "best_bid_1"]
    df["log_return"], df["dt_norm_return"] = loader.compute_returns(df["mid"], df["dt_s"])
    return df


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Quote recording to clean (csv/txt)")
    parser.add_argument("-o", "--output", help="Path for the cleaned CSV (default: <input>_cleaned.csv)")
    parser.add_argument("--mode", type=int, default=REPLACEMENT_MODE, choices=sorted(VALID_MODES),
                         help="0 none, 1 nan, 2 interpolate, 3 ffill, 4 rolling_median (default: config.ini)")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path.with_name(f"{in_path.stem}_cleaned.csv")

    df = loader.load_quotes(str(in_path))
    n_flagged = int(outliers.outlier_mask(df).sum())
    cleaned = treat_outliers(df, mode=args.mode)
    cleaned[loader.raw_schema_columns(cleaned)].to_csv(out_path, index=False)
    print(f"Flagged {n_flagged}/{len(df)} ticks, mode={args.mode} -> wrote {out_path}")


if __name__ == "__main__":
    main()
