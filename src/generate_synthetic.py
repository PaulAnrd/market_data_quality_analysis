from __future__ import annotations

import numpy as np
import pandas as pd

OUT_PATH = "data/synthetic_eurusd_quotes.txt"
SEED = 42

SESSION_START = pd.Timestamp("2024-03-04 08:00:00")
SESSION_SECONDS = 8 * 60 * 60
MEAN_INTER_ARRIVAL_S = 1.2

START_PRICE = 1.08500
ANNUALIZED_VOL = 0.07
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600

MEAN_SPREAD = 0.00015
SIZE_MIN, SIZE_MAX = 500, 20000

FEED_B_OUT_PATH = "data/synthetic_eurusd_quotes_feedB.txt"
FEED_B_SEED = 43
FEED_B_SUBSAMPLE_FRAC = 0.85
FEED_B_LATENCY_MEAN_S = 0.15
FEED_B_LATENCY_STD_S = 0.05
FEED_B_NOISE_STD = 0.000015
FEED_B_N_DIVERGENCES = 5
FEED_B_DIVERGENCE_MIN, FEED_B_DIVERGENCE_MAX = 0.0006, 0.0012


def generate() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)

    n_est = int(SESSION_SECONDS / MEAN_INTER_ARRIVAL_S * 1.2)
    elapsed = np.cumsum(rng.exponential(MEAN_INTER_ARRIVAL_S, size=n_est))
    elapsed = elapsed[elapsed <= SESSION_SECONDS]
    timestamps = SESSION_START + pd.to_timedelta(elapsed, unit="s")
    n = len(timestamps)

    dt = np.diff(elapsed, prepend=0.0).clip(min=1e-6)
    sigma_per_sec = ANNUALIZED_VOL / np.sqrt(TRADING_SECONDS_PER_YEAR)
    log_returns = rng.normal(0.0, sigma_per_sec * np.sqrt(dt))
    log_returns[0] = 0.0
    mid = START_PRICE * np.exp(np.cumsum(log_returns))

    half_spread = np.abs(rng.normal(MEAN_SPREAD / 2, MEAN_SPREAD / 6, size=n)) + 1e-5
    best_bid_1 = mid - half_spread
    best_ask_1 = mid + half_spread

    level2_extra = np.abs(rng.normal(MEAN_SPREAD, MEAN_SPREAD / 4, size=n)) + 1e-5
    best_bid_2 = best_bid_1 - level2_extra
    best_ask_2 = best_ask_1 + level2_extra

    def sizes():
        return rng.integers(SIZE_MIN, SIZE_MAX, size=n)

    return pd.DataFrame({
        "timestamp": timestamps,
        "best_bid_size_1": sizes(),
        "best_bid_1": best_bid_1.round(5),
        "best_ask_1": best_ask_1.round(5),
        "best_ask_size_1": sizes(),
        "best_bid_size_2": sizes(),
        "best_bid_2": best_bid_2.round(5),
        "best_ask_2": best_ask_2.round(5),
        "best_ask_size_2": sizes(),
    })


QUOTE_COLUMNS = [
    "best_bid_1", "best_ask_1", "best_bid_2", "best_ask_2",
]


def generate_feed_b(feed_a: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(FEED_B_SEED)

    df = feed_a.sample(frac=FEED_B_SUBSAMPLE_FRAC, random_state=FEED_B_SEED).sort_values("timestamp")
    df = df.reset_index(drop=True)
    n = len(df)

    latency_s = rng.normal(FEED_B_LATENCY_MEAN_S, FEED_B_LATENCY_STD_S, size=n).clip(min=0.0)
    df["timestamp"] = df["timestamp"] + pd.to_timedelta(latency_s, unit="s")
    df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)

    noise = rng.normal(0.0, FEED_B_NOISE_STD, size=n)
    for col in QUOTE_COLUMNS:
        df[col] = (df[col] + noise).round(5)

    divergence_idx = rng.choice(n, size=min(FEED_B_N_DIVERGENCES, n), replace=False)
    shocks = rng.choice([-1.0, 1.0], size=len(divergence_idx)) * rng.uniform(
        FEED_B_DIVERGENCE_MIN, FEED_B_DIVERGENCE_MAX, size=len(divergence_idx)
    )
    for idx, shock in zip(divergence_idx, shocks):
        df.loc[idx, QUOTE_COLUMNS] = (df.loc[idx, QUOTE_COLUMNS] + shock).round(5)

    def sizes():
        return rng.integers(SIZE_MIN, SIZE_MAX, size=n)

    df["best_bid_size_1"], df["best_ask_size_1"] = sizes(), sizes()
    df["best_bid_size_2"], df["best_ask_size_2"] = sizes(), sizes()
    return df


def main():
    feed_a = generate()
    feed_a.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(feed_a)} rows to {OUT_PATH}")

    feed_b = generate_feed_b(feed_a)
    feed_b.to_csv(FEED_B_OUT_PATH, index=False)
    print(f"Wrote {len(feed_b)} rows to {FEED_B_OUT_PATH}")


if __name__ == "__main__":
    main()
