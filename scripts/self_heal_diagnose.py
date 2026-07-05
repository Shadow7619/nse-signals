"""
self_heal_diagnose.py
Optional step, only runs if you've added an ANTHROPIC_API_KEY repo secret.

When generate_signals.py flags a degraded run (diagnostics/NEEDS_ATTENTION.json),
this script sends the raw broken response sample(s) alongside the current
parsing code to Claude and asks for a diagnosis + suggested patch. The
result is written to diagnostics/suggested_fix.md and posted as a comment
on the auto-created GitHub Issue — it is a *suggestion for you to review*,
not an auto-applied change. Deliberately not wired to commit code changes
on its own: a scraper silently patching its own live logic against
unverified output is a worse failure mode than a loud, human-reviewed one.
"""

import os
import sys
import json
import glob
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = ROOT / "diagnostics"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def main():
    if not API_KEY:
        print("No ANTHROPIC_API_KEY set — skipping self-heal diagnosis.")
        return

    flag_path = DIAG_DIR / "NEEDS_ATTENTION.json"
    if not flag_path.exists():
        print("No NEEDS_ATTENTION.json — nothing to diagnose.")
        return

    summary = json.loads(flag_path.read_text())

    samples = []
    for fp in sorted(glob.glob(str(DIAG_DIR / "schema_break_*.json")))[:4]:
        samples.append(Path(fp).read_text())

    client_code = (ROOT / "scripts" / "nse_client.py").read_text()

    prompt = f"""You are debugging a Python scraper that calls NSE India's
unofficial JSON endpoints. It just flagged a likely schema change.

Run summary:
{json.dumps(summary, indent=2)}

Raw response sample(s) that failed validation (may be truncated):
{chr(10).join(samples) if samples else "(no raw samples captured)"}

Current client code (scripts/nse_client.py):
```python
{client_code}
```

Please:
1. Identify exactly what field(s) or structure changed, based on the raw samples.
2. Give a minimal patch (just the changed function(s), as a diff-like snippet) to
   scripts/nse_client.py that fixes parsing.
3. Note if this looks like a genuine schema change vs. something else (e.g. a
   block page, a login wall, rate limiting) — those need a different fix
   (rotate headers/cookies) rather than a parsing change.

Keep it concise and actionable — this will be posted as a GitHub Issue comment
for a developer to review before applying."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    diagnosis = "\n".join(text_parts) or "(Claude returned no text content.)"

    out_path = DIAG_DIR / "suggested_fix.md"
    out_path.write_text(
        f"# Auto-diagnosis for NSE schema drift\n\n{diagnosis}\n\n"
        f"---\n*Generated automatically — review before applying any patch.*\n"
    )
    print(f"Wrote diagnosis to {out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        # Never let the optional diagnosis step break the workflow.
        print(f"self-heal diagnosis failed (non-fatal): {e}")
        sys.exit(0)
