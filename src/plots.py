from __future__ import annotations

import itertools
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.tsa.stattools import acf

import config
from outliers import outlier_mask
from continuity import ticks_per_minute

GAP_THRESHOLD_S = config.GAP_THRESHOLD_S

SURFACE = "#ffffff"
INK_PRIMARY = "#111111"
INK_SECONDARY = "#444444"
INK_MUTED = "#767676"
GRID = "#e5e5e5"
BASELINE = "#cfcfcf"
BLUE = "#1a4d8f"
CRITICAL = "#a30000"

SOURCE_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
]

FIGSIZE = (9, 4.5)


def _style_axes(ax):
    ax.set_facecolor(SURFACE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(BASELINE)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)
    ax.title.set_color(INK_PRIMARY)
    ax.xaxis.label.set_color(INK_SECONDARY)
    ax.yaxis.label.set_color(INK_SECONDARY)


def _new_fig(figsize=FIGSIZE):
    fig, ax = plt.subplots(figsize=figsize, facecolor=SURFACE)
    _style_axes(ax)
    return fig, ax


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


def plot_price(df: pd.DataFrame, out_dir: str) -> str:
    fig, ax = _new_fig()
    ax.fill_between(df["timestamp"], df["best_bid_1"], df["best_ask_1"],
                     color=BLUE, alpha=0.12, linewidth=0, label="Bid/ask band")
    ax.plot(df["timestamp"], df["mid"], color=BLUE, linewidth=1.4, label="Mid price")

    mask = outlier_mask(df)
    if mask.any():
        ax.scatter(df.loc[mask, "timestamp"], df.loc[mask, "mid"],
                    color=CRITICAL, marker="x", s=45, linewidths=1.6, zorder=5,
                    label="Return outlier (rolling Hampel filter)")

    ax.set_title("Mid price with bid/ask band")
    ax.set_ylabel("Price")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "01_price_timeseries.png")


def plot_spread(df: pd.DataFrame, out_dir: str) -> str:
    fig, ax = _new_fig()
    ax.plot(df["timestamp"], df["spread"], color=BLUE, linewidth=1.2, label="Spread")
    ax.axhline(0, color=BASELINE, linewidth=1)

    bad = df["spread"] <= 0
    if bad.any():
        ax.scatter(df.loc[bad, "timestamp"], df.loc[bad, "spread"],
                    color=CRITICAL, marker="x", s=45, linewidths=1.6, zorder=5,
                    label="Non-positive spread")

    ax.set_title("Best bid/ask spread over time")
    ax.set_ylabel("Spread")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "02_spread_timeseries.png")


def plot_interarrival(df: pd.DataFrame, out_dir: str) -> str:
    dt = df["dt_s"].dropna()
    dt = dt[dt > 0]
    fig, ax = _new_fig()
    bins = np.logspace(np.log10(dt.min()), np.log10(dt.max()), 40)
    ax.hist(dt, bins=bins, color=BLUE, alpha=0.85, edgecolor=SURFACE, linewidth=0.5)
    ax.axvline(dt.median(), color=INK_MUTED, linewidth=1.2, linestyle="--",
               label=f"Median = {dt.median():.2f}s")
    ax.axvline(GAP_THRESHOLD_S, color=CRITICAL, linewidth=1.2, linestyle="--",
               label=f"Gap threshold = {GAP_THRESHOLD_S:.0f}s")
    ax.set_xscale("log")
    ax.set_title("Inter-arrival time distribution")
    ax.set_xlabel("Seconds between ticks (log scale)")
    ax.set_ylabel("Count")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    return _save(fig, out_dir, "03_interarrival_hist.png")


def plot_return_distribution(df: pd.DataFrame, out_dir: str) -> str:
    r = df["dt_norm_return"].dropna()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), facecolor=SURFACE)
    for ax in axes:
        _style_axes(ax)

    ax = axes[0]
    ax.hist(r, bins=60, density=True, color=BLUE, alpha=0.85,
            edgecolor=SURFACE, linewidth=0.4, label="Returns")
    x = np.linspace(r.min(), r.max(), 200)
    ax.plot(x, stats.norm.pdf(x, r.mean(), r.std()), color=INK_MUTED,
            linewidth=1.6, label="Normal fit")
    ax.set_title("Time-normalized log-return distribution")
    ax.set_xlabel("Log-return / sqrt(dt)")
    ax.set_ylabel("Density")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)

    ax = axes[1]
    (osm, osr), (slope, intercept, _) = stats.probplot(r, dist="norm")
    ax.scatter(osm, osr, color=BLUE, s=10, alpha=0.7, label="Returns")
    ax.plot(osm, slope * osm + intercept, color=CRITICAL, linewidth=1.4,
            label="Normal reference")
    ax.set_title("Normal Q-Q plot")
    ax.set_xlabel("Theoretical quantiles")
    ax.set_ylabel("Sample quantiles")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)

    return _save(fig, out_dir, "04_return_hist_qq.png")


def plot_acf(df: pd.DataFrame, out_dir: str, nlags: int = 20) -> str:
    r = df["dt_norm_return"].dropna()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), facecolor=SURFACE)
    for ax in axes:
        _style_axes(ax)

    n = len(r)
    conf = 1.96 / np.sqrt(n)

    for ax, series, title in (
        (axes[0], r, "ACF - returns"),
        (axes[1], r ** 2, "ACF - squared returns (volatility clustering)"),
    ):
        vals = acf(series, nlags=nlags, fft=True)
        lags = np.arange(len(vals))
        ax.axhspan(-conf, conf, color=GRID, alpha=0.8, linewidth=0)
        ax.vlines(lags[1:], 0, vals[1:], color=BLUE, linewidth=1.4)
        ax.scatter(lags[1:], vals[1:], color=BLUE, s=14, zorder=5)
        ax.axhline(0, color=BASELINE, linewidth=1)
        ax.set_title(title)
        ax.set_xlabel("Lag")
        ax.set_ylabel("Autocorrelation")

    return _save(fig, out_dir, "05_acf_returns.png")


def plot_activity(df: pd.DataFrame, out_dir: str) -> str:
    counts = ticks_per_minute(df)
    fig, ax = _new_fig()
    ax.fill_between(counts.index, counts.values, color=BLUE, alpha=0.25, linewidth=0)
    ax.plot(counts.index, counts.values, color=BLUE, linewidth=1.2)
    ax.set_title("Tick activity per minute")
    ax.set_ylabel("Ticks / minute")
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "06_activity_profile.png")


def generate_all_plots(df: pd.DataFrame, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    plt.rcParams["font.family"] = "sans-serif"
    return [
        plot_price(df, out_dir),
        plot_spread(df, out_dir),
        plot_interarrival(df, out_dir),
        plot_return_distribution(df, out_dir),
        plot_acf(df, out_dir),
        plot_activity(df, out_dir),
    ]


def plot_comparison_overlay(aligned: pd.DataFrame, source_names: list[str], out_dir: str) -> str:
    fig, ax = _new_fig()
    for name, color in zip(source_names, itertools.cycle(SOURCE_COLORS)):
        ax.plot(aligned["timestamp"], aligned[f"mid__{name}"], color=color,
                linewidth=1.2, alpha=0.9, label=name)
    ax.set_title("Mid price by source (aligned grid)")
    ax.set_ylabel("Price")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, "c1_price_overlay.png")


def plot_comparison_diff(aligned: pd.DataFrame, name_a: str, name_b: str, threshold_bps: float, out_dir: str) -> str:
    a, b = aligned[f"mid__{name_a}"], aligned[f"mid__{name_b}"]
    mid_ref = ((a + b) / 2).replace(0, np.nan)
    diff_bps = (a - b) / mid_ref * 1e4

    fig, ax = _new_fig()
    ax.plot(aligned["timestamp"], diff_bps, color=BLUE, linewidth=1.0)
    ax.axhline(0, color=BASELINE, linewidth=1)
    ax.axhline(threshold_bps, color=CRITICAL, linewidth=1, linestyle="--",
               label=f"+/-{threshold_bps:g} bps threshold")
    ax.axhline(-threshold_bps, color=CRITICAL, linewidth=1, linestyle="--")
    ax.set_title(f"Mid price difference: {name_a} minus {name_b}")
    ax.set_ylabel("Diff (bps)")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _format_datetime_axis(ax)
    return _save(fig, out_dir, f"c2_diff_{name_a}_vs_{name_b}.png")


def plot_comparison_diff_hist(aligned: pd.DataFrame, name_a: str, name_b: str, out_dir: str) -> str:
    a, b = aligned[f"mid__{name_a}"], aligned[f"mid__{name_b}"]
    mid_ref = ((a + b) / 2).replace(0, np.nan)
    diff_bps = ((a - b) / mid_ref * 1e4).dropna()

    fig, ax = _new_fig()
    ax.hist(diff_bps, bins=60, color=BLUE, alpha=0.85, edgecolor=SURFACE, linewidth=0.4)
    ax.axvline(diff_bps.median(), color=INK_MUTED, linewidth=1.2, linestyle="--",
               label=f"Median = {diff_bps.median():.2f} bps")
    ax.set_title(f"Mid price difference distribution: {name_a} minus {name_b}")
    ax.set_xlabel("Diff (bps)")
    ax.set_ylabel("Count")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    return _save(fig, out_dir, f"c3_diff_hist_{name_a}_vs_{name_b}.png")


def generate_all_comparison_plots(aligned: pd.DataFrame, source_names: list[str],
                                   threshold_bps: float, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    plt.rcParams["font.family"] = "sans-serif"
    paths = [plot_comparison_overlay(aligned, source_names, out_dir)]
    for a, b in itertools.combinations(source_names, 2):
        paths.append(plot_comparison_diff(aligned, a, b, threshold_bps, out_dir))
        paths.append(plot_comparison_diff_hist(aligned, a, b, out_dir))
    return paths
