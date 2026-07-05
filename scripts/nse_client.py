"""
nse_client.py
Thin wrapper around NSE India's unofficial JSON endpoints.

NSE has no public API. Their website's own frontend calls internal
endpoints under nseindia.com/api/*. To use them you must:
  1. Hit the homepage first so NSE sets session cookies.
  2. Reuse those cookies + a browser-like User-Agent on every call.
  3. Re-warm the session if you get a 401/403 (cookies expire).

This is the same approach used by community tools like nsepython.
It is NOT officially supported by NSE and can break if they change
their site. Keep request volume modest and add delays (see
generate_signals.py) to avoid being rate-limited/blocked.
"""

import time
import json
import datetime as dt
from pathlib import Path

import requests

DIAGNOSTICS_DIR = Path(__file__).resolve().parent.parent / "diagnostics"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

NSE_HOME = "https://www.nseindia.com"


class NSEClient:
    def __init__(self, warm_delay=1.5):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.warm_delay = warm_delay
        self._warm_session()

    def _warm_session(self):
        """Visit homepage (and one market-data page) to collect cookies."""
        try:
            self.session.get(NSE_HOME, timeout=10)
            time.sleep(self.warm_delay)
            self.session.get(
                f"{NSE_HOME}/market-data/live-equity-market", timeout=10
            )
            time.sleep(self.warm_delay)
        except requests.RequestException:
            pass

    def get_json(self, url, params=None, retries=3, backoff=3):
        last_err = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403, 429):
                    self._warm_session()
                last_err = f"HTTP {resp.status_code} for {url}"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            time.sleep(backoff * (attempt + 1))
        raise RuntimeError(f"Failed to fetch {url}: {last_err}")

    def save_diagnostic(self, label: str, raw_data, expected_keys, note=""):
        """
        Dump a raw response that didn't match the expected shape, so we
        have evidence of exactly what NSE changed instead of just a
        KeyError with no context. Picked up by generate_signals.py's
        schema-drift check and surfaced as a GitHub Issue.
        """
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "label": label,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "expected_keys": expected_keys,
            "note": note,
            "raw_sample": raw_data if not isinstance(raw_data, str) else raw_data[:4000],
        }
        fname = DIAGNOSTICS_DIR / f"schema_break_{label}.json"
        with open(fname, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    def _validate(self, label, data, required_keys):
        """Raise + capture a diagnostic if the response is missing keys
        our parser depends on (the tell-tale sign NSE changed something)."""
        missing = [k for k in required_keys if k not in data]
        if missing:
            self.save_diagnostic(
                label, data, required_keys,
                note=f"Missing expected key(s): {missing}",
            )
            raise SchemaDriftError(
                f"{label}: response shape changed, missing {missing}. "
                f"Sample saved to diagnostics/schema_break_{label}.json"
            )

    # ---- Endpoints ----

    def get_index_constituents(self, index_name: str):
        """Returns live snapshot (price/%chg) for all stocks in a sectoral index.
        e.g. index_name='NIFTY BANK'
        """
        url = f"{NSE_HOME}/api/equity-stockIndices"
        data = self.get_json(url, params={"index": index_name})
        self._validate(f"index_{index_name.replace(' ', '_')}", data, ["data"])
        rows = data["data"]
        if rows and "symbol" not in rows[0]:
            self.save_diagnostic(
                f"index_{index_name.replace(' ', '_')}_row", rows[0], ["symbol"],
                note="First row missing 'symbol' field.",
            )
            raise SchemaDriftError(f"index_{index_name}: row shape changed, no 'symbol' field.")
        return rows

    def get_historical(self, symbol: str, from_date: str, to_date: str):
        """Daily OHLCV candles. Dates as DD-MM-YYYY strings."""
        url = f"{NSE_HOME}/api/historical/cm/equity"
        params = {
            "symbol": symbol,
            "series": '["EQ"]',
            "from": from_date,
            "to": to_date,
        }
        data = self.get_json(url, params=params)
        self._validate(f"historical_{symbol}", data, ["data"])
        rows = data["data"]
        required_row_keys = ["CH_TIMESTAMP", "CH_CLOSING_PRICE", "CH_TOT_TRADED_QTY"]
        if rows:
            missing = [k for k in required_row_keys if k not in rows[0]]
            if missing:
                self.save_diagnostic(
                    f"historical_{symbol}_row", rows[0], required_row_keys,
                    note=f"Row missing: {missing}",
                )
                raise SchemaDriftError(f"historical_{symbol}: row fields changed, missing {missing}.")
        return rows

    def get_trade_info(self, symbol: str):
        """Latest day trade info incl. delivery % for a single stock."""
        url = f"{NSE_HOME}/api/quote-equity"
        data = self.get_json(
            url, params={"symbol": symbol, "section": "trade_info"}
        )
        if "securityWiseDP" not in data:
            self.save_diagnostic(
                f"trade_info_{symbol}", data, ["securityWiseDP"],
                note="Missing 'securityWiseDP' block (delivery % source).",
            )
            raise SchemaDriftError(f"trade_info_{symbol}: 'securityWiseDP' missing.")
        return data


class SchemaDriftError(Exception):
    """Raised when NSE's response shape no longer matches what we expect —
    the signal that something on their end changed, as opposed to a plain
    network hiccup."""
    pass
