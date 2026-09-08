from __future__ import annotations

import pandas as pd

import config

GAP_THRESHOLD_S = config.GAP_THRESHOLD_S
TOP_N_GAPS = config.TOP_N_GAPS


def check_continuity(df: pd.DataFrame) -> dict:
    dt = df["dt_s"].dropna()

    gaps = df.loc[dt[dt > GAP_THRESHOLD_S].index]
    top_gaps = (
        gaps.assign(gap_s=dt[dt > GAP_THRESHOLD_S])
        .nlargest(TOP_N_GAPS, "gap_s")[["timestamp", "gap_s"]]
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
        "gap_threshold_s": GAP_THRESHOLD_S,
        "n_gaps_over_threshold": int((dt > GAP_THRESHOLD_S).sum()),
        "top_gaps": [
            {"timestamp": row.timestamp.isoformat(), "gap_s": float(row.gap_s)}
            for row in top_gaps.itertuples()
        ],
    }


def ticks_per_minute(df: pd.DataFrame) -> pd.Series:
    return df.set_index("timestamp").resample("1min").size()
