"""
indicators.py
Standard RSI (Wilder's smoothing) and MACD calculations using pandas.
No ta-lib dependency (avoids C-compilation headaches in CI).
"""

import pandas as pd


def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def compute_macd(closes: pd.Series, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 3),
        round(float(signal_line.iloc[-1]), 3),
        round(float(hist.iloc[-1]), 3),
    )


def volume_ratio(volumes: pd.Series, lookback: int = 20) -> float:
    if len(volumes) < lookback + 1:
        return None
    avg = volumes.iloc[-(lookback + 1):-1].mean()
    if avg == 0:
        return None
    return round(float(volumes.iloc[-1] / avg), 2)


def swing_score(rsi, macd_hist, macd_line, macd_signal, vol_ratio, delivery_pct, sector_avg_delivery):
    """
    Weighted 0-100 score for delivery-based swing trade candidates.
    Rewards: bullish MACD crossover/momentum, RSI in a healthy
    (not overbought) zone, above-average volume, and above-sector
    delivery % (fresh conviction buying, not just intraday churn).
    """
    if None in (rsi, macd_hist, macd_line, macd_signal, vol_ratio, delivery_pct):
        return 0, "Insufficient Data"

    score = 0

    # MACD momentum (40 pts)
    if macd_line > macd_signal:
        score += 22
    if macd_hist > 0:
        score += 18

    # RSI sweet spot 45-65 favors continuation without being overbought (25 pts)
    if 45 <= rsi <= 65:
        score += 25
    elif 65 < rsi <= 72:
        score += 12
    elif 35 <= rsi < 45:
        score += 8

    # Volume above average (15 pts)
    if vol_ratio >= 1.5:
        score += 15
    elif vol_ratio >= 1.2:
        score += 9

    # Delivery % vs sector average (20 pts) - proxy for genuine accumulation
    if sector_avg_delivery and sector_avg_delivery > 0:
        rel = delivery_pct / sector_avg_delivery
        if rel >= 1.3:
            score += 20
        elif rel >= 1.1:
            score += 12
        elif rel >= 1.0:
            score += 6

    score = min(score, 100)

    if score >= 75:
        label = "Strong Buy"
    elif score >= 55:
        label = "Buy"
    elif score >= 35:
        label = "Watch"
    else:
        label = "Neutral"

    return score, label
