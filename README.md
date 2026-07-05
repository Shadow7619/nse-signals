# Sector Pulse — NSE Sector & Swing Signal PWA

A PWA (installable on Android via "Add to Home Screen") that shows:
- Sector-wise bullish/bearish score, aggregated from MACD + RSI across each
  sector's constituent stocks.
- Per-stock swing trade signals combining MACD, RSI, volume ratio, and
  delivery % — flagged Strong Buy / Buy / Watch / Neutral.

## How it works

```
GitHub Actions (daily, after market close)
   → scripts/generate_signals.py fetches NSE data, computes indicators
   → writes data/signals.json
   → commits it back to the repo
GitHub Pages serves index.html, which fetches data/signals.json directly
```

No backend server is needed. NSE blocks direct calls from a browser
(CORS + bot detection), which is why the fetch has to happen from GitHub
Actions (server-to-server) rather than from the PWA itself.

## Setup

1. **Create a new GitHub repo** (e.g. `nse-signals`) and push everything in
   this folder to it.
2. **Enable GitHub Pages**: repo Settings → Pages → Source: `main` branch,
   root folder.
3. **Enable Actions**: the workflow at `.github/workflows/update-signals.yml`
   runs automatically Mon–Fri at 4:00 PM IST. You can also trigger it
   manually: Actions tab → "Update NSE Signals" → "Run workflow" — do this
   once right after setup so `data/signals.json` gets populated instead of
   showing the empty state.
4. **Install on Android**: open the GitHub Pages URL in Chrome →
   menu → "Add to Home screen." It'll behave like a native app icon.

## When NSE changes something (schema drift handling)

Rather than silently failing or shipping stale/empty data, the pipeline
now watches its own health:

1. **Validation, not blind parsing** — `nse_client.py` checks that each
   response has the fields the parser expects (`data`, `symbol`,
   `CH_CLOSING_PRICE`, `securityWiseDP`, etc.). If a field is missing, it
   saves the raw response to `diagnostics/schema_break_<label>.json` and
   raises a `SchemaDriftError` instead of a cryptic `KeyError`.
2. **Run-level health check** — `generate_signals.py` tracks the success
   rate across the run. If any schema drift is detected, or the success
   rate drops below 50%, it writes `diagnostics/NEEDS_ATTENTION.json`
   instead of quietly shipping a half-empty `signals.json`.
3. **GitHub Issue, automatically** — the workflow checks for that flag and
   opens (or comments on) a GitHub Issue in your repo with the diagnostic
   summary. You'll get a GitHub notification/email — no need to babysit
   Action logs.
4. **Optional: Claude diagnoses it for you** — if you add an
   `ANTHROPIC_API_KEY` repo secret (Settings → Secrets and variables →
   Actions → New repository secret), a step sends the broken raw response
   + current parsing code to Claude, which posts a suggested fix as a
   comment on that Issue. It's a suggestion for you to review and apply
   manually — nothing auto-patches the live scraper. That's deliberate: a
   script that silently rewrites its own scraping logic against unverified
   output is a worse failure mode than one that fails loudly and asks for
   a human to look.

Once you fix the underlying issue (usually a field rename in
`nse_client.py`), the next successful run automatically clears the flag
file.

## Important caveats (please read)

- **Unofficial API**: NSE has no public API. This uses the same internal
  JSON endpoints their own website calls, the same approach tools like
  `nsepython` use. NSE can change these endpoints or tighten bot detection
  at any time without notice — if the Action starts failing, that's usually
  why. Check the Actions log first.
- **Rate limiting**: the script pauses ~1.2s between stock requests to
  avoid getting blocked. With ~14 sectors × ~15-30 stocks each, a full run
  takes roughly 15-25 minutes. This is fine for GitHub Actions' free tier
  (well under its limits) but don't reduce the delay much or NSE may
  temporarily block the runner's IP.
- **I couldn't test this against live NSE myself** — my sandbox's network
  is restricted to a few domains (PyPI, GitHub, etc.) and doesn't include
  nseindia.com. The endpoint shapes (`CH_TIMESTAMP`, `CH_CLOSING_PRICE`,
  `securityWiseDP.deliveryToTradedQuantity`, etc.) match NSE's documented
  historical/quote response format, but you should run it once locally or
  via manual Action trigger and check `data/signals.json` looks sane before
  relying on it.
- **This is a personal research tool, not investment advice** — signals
  are a mechanical blend of technical indicators, not a guarantee.
- Sector index names in `scripts/generate_signals.py` (`SECTORS` list) can
  be edited freely — add or remove any NSE sectoral index name NSE
  recognises.

## Running locally to test

```bash
cd scripts
pip install -r ../requirements.txt
python generate_signals.py
```

Then open `index.html` with a local server (not `file://`, since `fetch()`
needs http): `python -m http.server 8000` from the repo root, then visit
`http://localhost:8000`.

## Tuning the swing score

Weights live in `scripts/indicators.py` → `swing_score()`. Currently:
MACD momentum (40 pts) + RSI 45-65 sweet spot (25 pts) + volume ratio
(15 pts) + delivery % vs sector average (20 pts). Adjust freely — e.g. if
you want to weight delivery % more heavily like your TARIL analysis style,
bump that block up and trim MACD's share.
