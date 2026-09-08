from __future__ import annotations

import numpy as np
import pandas as pd

import config

HAMPEL_WINDOW = config.HAMPEL_WINDOW
HAMPEL_THRESHOLD = config.HAMPEL_THRESHOLD
MAD_CONSISTENCY = 1.4826


def _hampel_mask(x: pd.Series, window: int = HAMPEL_WINDOW, threshold: float = HAMPEL_THRESHOLD) -> pd.Series:
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
