from __future__ import annotations

import pandas as pd
from scipy import stats
from statsmodels.tsa.stattools import adfuller, kpss
from statsmodels.stats.diagnostic import acorr_ljungbox

import config
import outliers

SIGNIFICANCE_LEVEL = config.SIGNIFICANCE_LEVEL
LJUNG_BOX_LAGS = config.LJUNG_BOX_LAGS


def check_returns(df: pd.DataFrame) -> dict:
    r = df["dt_norm_return"].dropna()
    n_outliers = outliers.n_outliers(df)

    jb_stat, jb_pvalue = stats.jarque_bera(r)

    lb = acorr_ljungbox(r, lags=[LJUNG_BOX_LAGS], return_df=True)
    lb_sq = acorr_ljungbox(r ** 2, lags=[LJUNG_BOX_LAGS], return_df=True)

    adf_stat, adf_pvalue, *_ = adfuller(r, autolag="AIC")
    kpss_stat, kpss_pvalue, *_ = kpss(r, regression="c", nlags="auto")

    return {
        "significance_level": SIGNIFICANCE_LEVEL,
        "n_returns": int(len(r)),
        "mean": float(r.mean()),
        "std": float(r.std()),
        "skew": float(stats.skew(r)),
        "kurtosis_excess": float(stats.kurtosis(r)),
        "jarque_bera_stat": float(jb_stat),
        "jarque_bera_pvalue": float(jb_pvalue),
        "jarque_bera_normal": bool(jb_pvalue > SIGNIFICANCE_LEVEL),
        "n_outliers_hampel": n_outliers,
        "outlier_hampel_window": outliers.HAMPEL_WINDOW,
        "outlier_hampel_threshold": outliers.HAMPEL_THRESHOLD,
        "ljung_box_lags": LJUNG_BOX_LAGS,
        "ljung_box_stat": float(lb["lb_stat"].iloc[0]),
        "ljung_box_pvalue": float(lb["lb_pvalue"].iloc[0]),
        "ljung_box_autocorrelated": bool(lb["lb_pvalue"].iloc[0] < SIGNIFICANCE_LEVEL),
        "ljung_box_sq_stat": float(lb_sq["lb_stat"].iloc[0]),
        "ljung_box_sq_pvalue": float(lb_sq["lb_pvalue"].iloc[0]),
        "ljung_box_sq_vol_clustering": bool(lb_sq["lb_pvalue"].iloc[0] < SIGNIFICANCE_LEVEL),
        "adf_stat": float(adf_stat),
        "adf_pvalue": float(adf_pvalue),
        "adf_stationary": bool(adf_pvalue < SIGNIFICANCE_LEVEL),
        "kpss_stat": float(kpss_stat),
        "kpss_pvalue": float(kpss_pvalue),
        "kpss_stationary": bool(kpss_pvalue > SIGNIFICANCE_LEVEL),
    }


def check_price_level(df: pd.DataFrame) -> dict:
    p = df["mid"].dropna()
    adf_stat, adf_pvalue, *_ = adfuller(p, autolag="AIC")
    kpss_stat, kpss_pvalue, *_ = kpss(p, regression="c", nlags="auto")

    return {
        "start": float(p.iloc[0]),
        "end": float(p.iloc[-1]),
        "mean": float(p.mean()),
        "std": float(p.std()),
        "min": float(p.min()),
        "max": float(p.max()),
        "adf_stat": float(adf_stat),
        "adf_pvalue": float(adf_pvalue),
        "adf_stationary": bool(adf_pvalue < SIGNIFICANCE_LEVEL),
        "kpss_stat": float(kpss_stat),
        "kpss_pvalue": float(kpss_pvalue),
        "kpss_stationary": bool(kpss_pvalue > SIGNIFICANCE_LEVEL),
    }
