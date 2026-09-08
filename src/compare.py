from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

import config
import filename_metadata

ALIGNMENT_FREQ_S = config.COMPARISON_ALIGNMENT_FREQ_S
MAX_STALENESS_S = config.COMPARISON_MAX_STALENESS_S
PRICE_DIFF_THRESHOLD_BPS = config.COMPARISON_PRICE_DIFF_THRESHOLD_BPS
TOP_N_DIVERGENCES = config.COMPARISON_TOP_N_DIVERGENCES
MAX_GRID_POINTS = config.COMPARISON_MAX_GRID_POINTS
DOWNSAMPLE_POINTS_PER_DAY = config.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY
DOWNSAMPLE_MIN_ROWS = config.COMPARISON_DOWNSAMPLE_MIN_ROWS

SECONDS_PER_DAY = 86400


def downsample(
    df: pd.DataFrame,
    points_per_day: int = DOWNSAMPLE_POINTS_PER_DAY,
    min_rows: int = DOWNSAMPLE_MIN_ROWS,
) -> pd.DataFrame:
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
    if ALIGNMENT_FREQ_S <= 0:
        raise ValueError(
            f"config.ini [comparison] alignment_freq_s must be positive, got {ALIGNMENT_FREQ_S}."
        )

    window_start = max(df["timestamp"].iloc[0] for df in sources.values())
    window_end = min(df["timestamp"].iloc[-1] for df in sources.values())
    if window_start >= window_end:
        raise ValueError(
            "Sources have no overlapping time window to compare "
            f"(latest start {window_start.isoformat()} >= earliest end {window_end.isoformat()})."
        )

    window_seconds = (window_end - window_start).total_seconds()
    estimated_points = int(window_seconds / ALIGNMENT_FREQ_S) + 1
    if estimated_points > MAX_GRID_POINTS:
        raise ValueError(
            f"Aligning these sources would need ~{estimated_points:,} grid points "
            f"({window_seconds:,.0f}s overlap at a {ALIGNMENT_FREQ_S:g}s step), exceeding "
            f"the {MAX_GRID_POINTS:,} cap (config.ini [comparison] max_grid_points) -- "
            f"rejected before allocating it. Narrow the input time range or raise "
            f"alignment_freq_s."
        )

    grid = pd.DataFrame({
        "timestamp": pd.date_range(window_start, window_end, freq=pd.Timedelta(seconds=ALIGNMENT_FREQ_S))
    })
    tolerance = pd.Timedelta(seconds=max_staleness_s if max_staleness_s is not None else MAX_STALENESS_S)

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

    ret_a, ret_b = np.log(a_c).diff(), np.log(b_c).diff()
    valid_ret = ret_a.notna() & ret_b.notna()
    return_correlation = None
    if valid_ret.sum() > 1:
        return_correlation = _safe_float(ret_a[valid_ret].corr(ret_b[valid_ret]))

    exceed = diff_bps.abs() > PRICE_DIFF_THRESHOLD_BPS
    top_idx = diff_bps[exceed].abs().nlargest(TOP_N_DIVERGENCES).index
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
        "alignment_freq_s": ALIGNMENT_FREQ_S,
        "max_staleness_s": max_staleness_s if max_staleness_s is not None else MAX_STALENESS_S,
        "price_diff_threshold_bps": PRICE_DIFF_THRESHOLD_BPS,
        "n_grid_points": int(len(aligned)),
        "window_start": aligned["timestamp"].iloc[0].isoformat(),
        "window_end": aligned["timestamp"].iloc[-1].isoformat(),
        "pairs": pairs,
    }, aligned


def attribute_divergence(pairs: list[dict], metadata: dict[str, filename_metadata.FeedMetadata]) -> dict:
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
