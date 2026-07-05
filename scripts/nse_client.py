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
from playwright.sync_api import sync_playwright

DIAGNOSTICS_DIR = Path(__file__).resolve().parent.parent / "diagnostics"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}

NSE_HOME = "https://www.nseindia.com"


def fetch_browser_cookies():
    """
    NSE sits behind Akamai bot-protection: the verification cookies it
    needs (_abck, bm_sz, ak_bmsc, etc.) are set by JavaScript that runs
    in a real browser, not by a plain HTTP GET. A `requests` session can
    never earn these on its own — that's why every API call was coming
    back as a fake "Resource not found" 404 (Akamai's soft-block page).

    This launches headless Chromium once, lets the page's JS run and
    collect those cookies naturally, then hands them off to a normal
    `requests.Session` for the actual (fast) data pulls.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=BASE_HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(NSE_HOME, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3000)
        # Visit a data-heavy page too — some cookies are only set once the
        # equity/market-data scripts on that page run.
        page.goto(f"{NSE_HOME}/market-data/live-equity-market", wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        cookies = context.cookies()
        browser.close()
        return cookies


class NSEClient:
    def __init__(self, warm_delay=2.5):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.warm_delay = warm_delay
        self._warm_session()

    def _warm_session(self):
        """Get a fresh browser-verified cookie jar and load it into the
        requests session. This is the expensive step (launches Chromium,
        ~5-10s) but only runs at startup and again if we start getting
        blocked mid-run (cookies can expire during a long run)."""
        try:
            cookies = fetch_browser_cookies()
            self.session.cookies.clear()
            for c in cookies:
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"),
                )
        except Exception as e:  # noqa: BLE001
            print(f"  ! browser cookie warm-up failed: {e}")

    def get_json(self, url, params=None, retries=3, backoff=3):
        last_err = None
        last_body_snippet = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        # Got a 200 but not JSON (e.g. an HTML page) — treat as a block.
                        last_body_snippet = resp.text[:500]
                        last_err = "HTTP 200 but non-JSON body (likely a block/challenge page)"
                        self._warm_session()
                        time.sleep(backoff * (attempt + 1))
                        continue
                # NSE's WAF frequently returns 401/403/404/429 for requests it
                # flags as bot traffic (e.g. datacenter/cloud IPs), not just
                # genuine "not found" — re-warm the session and retry either way.
                if resp.status_code in (401, 403, 404, 429):
                    self._warm_session()
                last_body_snippet = resp.text[:500]
                last_err = f"HTTP {resp.status_code} for {url}"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            time.sleep(backoff * (attempt + 1))

        # Save whatever the server actually sent back, so we can tell a
        # genuine schema/URL problem apart from a WAF block page.
        if last_body_snippet:
            self.save_diagnostic(
                f"http_error_{url.rsplit('/', 1)[-1]}",
                last_body_snippet,
                expected_keys=["<200 JSON response>"],
                note=last_err,
            )
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
