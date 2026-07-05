"""
generate_signals.py
Fetches sector + stock data from NSE, computes MACD/RSI/volume/delivery
signals, and writes data/signals.json for the PWA to consume.

Run via GitHub Actions once daily after market close. Can also be run
locally: `python scripts/generate_signals.py`
"""

import json
import time
import datetime as dt
from pathlib import Path

import pandas as pd

from nse_client import NSEClient, SchemaDriftError
from indicators import compute_rsi, compute_macd, volume_ratio, swing_score

# Sectoral indices to track. Add/remove as needed - NSE will reject
# a name it doesn't recognise (that sector is just skipped, logged).
SECTORS = [
    "NIFTY BANK",
    "NIFTY AUTO",
    "NIFTY IT",
    "NIFTY PHARMA",
    "NIFTY FMCG",
    "NIFTY METAL",
    "NIFTY ENERGY",
    "NIFTY REALTY",
    "NIFTY MEDIA",
    "NIFTY PSU BANK",
    "NIFTY FIN SERVICE",
    "NIFTY PVT BANK",
    "NIFTY HEALTHCARE INDEX",
    "NIFTY OIL AND GAS",
]

REQUEST_DELAY = 1.2  # seconds between per-stock calls, keep NSE happy
HIST_LOOKBACK_DAYS = 130  # enough for MACD(26)+signal(9) to warm up

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.json"


def fetch_stock_signal(client: NSEClient, symbol: str, stats: dict):
    """Fetch history + trade info for one stock, compute indicators.
    `stats` is a shared dict this run's counters get accumulated into,
    so main() can tell a genuine schema break apart from an ordinary
    network blip or a single thin/illiquid stock.
    """
    to_date = dt.date.today()
    from_date = to_date - dt.timedelta(days=HIST_LOOKBACK_DAYS)

    try:
        hist = client.get_historical(
            symbol, from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y")
        )
        if not hist or len(hist) < 35:
            return None

        df = pd.DataFrame(hist)
        # NSE historical fields: CH_TIMESTAMP, CH_CLOSING_PRICE, CH_TOT_TRADED_QTY
        df["CH_TIMESTAMP"] = pd.to_datetime(df["CH_TIMESTAMP"])
        df = df.sort_values("CH_TIMESTAMP")
        closes = df["CH_CLOSING_PRICE"].astype(float)
        volumes = df["CH_TOT_TRADED_QTY"].astype(float)

        rsi = compute_rsi(closes)
        macd_line, macd_signal, macd_hist = compute_macd(closes)
        vol_ratio = volume_ratio(volumes)

        ltp = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        change_pct = round((ltp - prev_close) / prev_close * 100, 2)

        delivery_pct = None
        try:
            trade_info = client.get_trade_info(symbol)
            dp = trade_info.get("securityWiseDP", {})
            delivery_pct = dp.get("deliveryToTradedQuantity")
            if delivery_pct is not None:
                delivery_pct = float(delivery_pct)
        except SchemaDriftError as e:
            stats["drift_errors"] += 1
            stats["drift_samples"].append(str(e))
        except Exception:  # noqa: BLE001
            pass

        stats["succeeded"] += 1
        return {
            "symbol": symbol,
            "ltp": round(ltp, 2),
            "change_pct": change_pct,
            "rsi": rsi,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "volume_ratio": vol_ratio,
            "delivery_pct": delivery_pct,
        }
    except SchemaDriftError as e:
        stats["drift_errors"] += 1
        stats["drift_samples"].append(str(e))
        print(f"  ! SCHEMA DRIFT on {symbol}: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  ! skipped {symbol}: {e}")
        return None


def classify_sector(stock_signals):
    valid = [s for s in stock_signals if s and s["rsi"] is not None and s["macd"] is not None]
    if not valid:
        return {"avg_rsi": None, "macd_bullish_pct": None, "bullish_score": 0, "classification": "No Data"}

    avg_rsi = round(sum(s["rsi"] for s in valid) / len(valid), 2)
    bullish_count = sum(1 for s in valid if s["macd"] > s["macd_signal"])
    macd_bullish_pct = round(bullish_count / len(valid) * 100, 1)

    # Blend: 50% breadth (macd bullish %), 50% avg RSI positioning
    rsi_component = max(0, min(100, (avg_rsi - 30) / (70 - 30) * 100))
    bullish_score = round(0.5 * macd_bullish_pct + 0.5 * rsi_component, 1)

    if bullish_score >= 65:
        classification = "Bullish"
    elif bullish_score >= 45:
        classification = "Neutral"
    else:
        classification = "Bearish"

    return {
        "avg_rsi": avg_rsi,
        "macd_bullish_pct": macd_bullish_pct,
        "bullish_score": bullish_score,
        "classification": classification,
    }


def main():
    client = NSEClient()
    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sectors": [],
    }
    stats = {"attempted": 0, "succeeded": 0, "drift_errors": 0, "drift_samples": []}

    for sector_name in SECTORS:
        print(f"Processing {sector_name} ...")
        try:
            constituents = client.get_index_constituents(sector_name)
        except SchemaDriftError as e:
            stats["drift_errors"] += 1
            stats["drift_samples"].append(str(e))
            print(f"  ! SCHEMA DRIFT fetching constituents: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not fetch constituents: {e}")
            continue

        symbols = [c["symbol"] for c in constituents if c.get("symbol") and c["symbol"] != sector_name]

        stock_signals = []
        for sym in symbols:
            stats["attempted"] += 1
            sig = fetch_stock_signal(client, sym, stats)
            time.sleep(REQUEST_DELAY)
            if sig:
                stock_signals.append(sig)

        sector_stats = classify_sector(stock_signals)
        sector_avg_delivery = None
        delivery_vals = [s["delivery_pct"] for s in stock_signals if s.get("delivery_pct") is not None]
        if delivery_vals:
            sector_avg_delivery = sum(delivery_vals) / len(delivery_vals)

        for s in stock_signals:
            score, label = swing_score(
                s["rsi"], s["macd_hist"], s["macd"], s["macd_signal"],
                s["volume_ratio"], s["delivery_pct"], sector_avg_delivery,
            )
            s["swing_score"] = score
            s["swing_signal"] = label

        stock_signals.sort(key=lambda s: s["swing_score"], reverse=True)

        result["sectors"].append({
            "name": sector_name,
            **sector_stats,
            "sector_avg_delivery": round(sector_avg_delivery, 2) if sector_avg_delivery else None,
            "stocks": stock_signals,
        })

    result["sectors"].sort(key=lambda s: s["bullish_score"] or 0, reverse=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {OUTPUT_PATH}")

    check_health(stats)


def check_health(stats):
    """
    Decide whether this run looks like NSE changed something on their
    end, vs. an ordinary blip (a couple of illiquid stocks with no
    delivery data, a transient timeout). If it looks systemic, write a
    flag file the workflow checks to open a GitHub Issue.
    """
    success_rate = stats["succeeded"] / stats["attempted"] if stats["attempted"] else 0
    is_degraded = stats["drift_errors"] > 0 or (stats["attempted"] > 0 and success_rate < 0.5)

    flag_path = Path(__file__).resolve().parent.parent / "diagnostics" / "NEEDS_ATTENTION.json"
    if is_degraded:
        summary = {
            "flagged_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempted": stats["attempted"],
            "succeeded": stats["succeeded"],
            "success_rate_pct": round(success_rate * 100, 1),
            "drift_errors": stats["drift_errors"],
            "drift_samples": stats["drift_samples"][:5],  # keep it short for the issue body
            "likely_cause": (
                "NSE response schema appears to have changed (field names/structure "
                "differ from what the parser expects)."
                if stats["drift_errors"] > 0
                else "Success rate dropped well below normal — could be schema drift, "
                     "a session/cookie change, or NSE rate-limiting the runner."
            ),
        }
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        with open(flag_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"HEALTH CHECK: degraded run flagged -> {flag_path}")
    else:
        # Clear any stale flag from a previous bad run now that things look fine.
        if flag_path.exists():
            flag_path.unlink()
        print(f"HEALTH CHECK: OK ({stats['succeeded']}/{stats['attempted']} stocks, "
              f"{success_rate*100:.1f}% success rate)")


if __name__ == "__main__":
    main()
