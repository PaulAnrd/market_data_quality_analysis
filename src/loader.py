from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

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


def _resolve_column(df_columns: set[str], canonical: str, candidates: list[str]) -> str | None:
    present = [c for c in candidates if c in df_columns]
    if len(present) > 1:
        raise ValueError(
            f"Ambiguous column for '{canonical}': more than one configured alternative is "
            f"present in this file ({present}) -- expected exactly one (config.ini [columns])."
        )
    return present[0] if present else None


def load_raw(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df_columns = set(df.columns)

    rename = {}
    missing = []
    for canon in MANDATORY_COLUMNS:
        actual = _resolve_column(df_columns, canon, config.COLUMNS_MAP[canon])
        if actual is None:
            missing.append((canon, config.COLUMNS_MAP[canon]))
        else:
            rename[actual] = canon
    if missing:
        detail = "; ".join(f"'{canon}' (tried {alts})" for canon, alts in missing)
        raise ValueError(f"Missing mandatory columns -- {detail} (see config.ini [columns]).")

    for lvl in range(2, config.COLUMNS_MAX_LEVELS + 1):
        level_rename = {}
        for key, canon in zip(("best_bid", "best_ask", "best_bid_size", "best_ask_size"), level_columns(lvl)):
            candidates = [t.format(level=lvl) for t in config.COLUMNS_LEVEL_TEMPLATES[key]]
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
    df["timestamp"] = pd.to_datetime(df["timestamp"])

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

    return df


def compute_returns(mid: pd.Series, dt_s: pd.Series) -> tuple[pd.Series, pd.Series]:
    log_return = np.log(mid).diff()
    positive_dt = dt_s[dt_s > 0]
    dt_floor = max(positive_dt.quantile(config.DT_NORM_FLOOR_QUANTILE), 1e-3) if len(positive_dt) else 1e-3
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
    return enrich(load_raw(path))
