"""Twice-daily digest for the user: the tracker as the table they asked for,
rendered to a PNG (a text table breaks on the phone) and sent to Telegram
with a caption. Reads state only; never scans.

The caption names EVERY active company that has no confirmed date yet:
biotechs announce their date late, or never, so the list of who is still
silent is the useful part.

Columns (user's format, 2026-09-25):
  India Update Date | # of biotech models to be updated as per PR: Morning | Evening | Overall
  (confirmed dates only - estimated dates dropped, user 2026-09-29)
Ticker names sit in brackets in the per-PR cells, e.g. "2(BRX, WELL)".

Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (required to send), DRY_RUN=1 to only
write digest.png, FORCE_SEND=1 to send on a weekend or after wrap-up.
"""
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

import tracker

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(ROOT, "state", "digest.png")
IST = ZoneInfo("Asia/Kolkata")


def build_rows(state, today):
    """One row per India update date from today on that has a CONFIRMED release
    (press-release notice). Estimated dates are not shown (user, 2026-09-29): the
    team's Bloomberg expected dates are the reference for those."""
    per_pr = defaultdict(lambda: {"Morning": [], "Evening": []})
    for t, e in state["companies"].items():
        d = e.get("india_update_date")
        if not d or e.get("status") in ("released", "not_applicable") or e.get("confidence") != "pr":
            continue
        if date.fromisoformat(d) < today:
            continue
        per_pr[d][e.get("india_slot") or "Morning"].append(t)
    rows = []
    for d in sorted(per_pr):
        pm, pe = sorted(per_pr[d]["Morning"]), sorted(per_pr[d]["Evening"])
        rows.append([
            datetime.fromisoformat(d).strftime("%m/%d/%Y"),
            f"{len(pm)}({', '.join(pm)})" if pm else "0",
            f"{len(pe)}({', '.join(pe)})" if pe else "0",
            str(len(pm) + len(pe)),
        ])
    return rows


def render(rows, quarter, updated, path=OUT_PNG):
    from render import render_table
    return render_table(rows, f"Biotech junior analyst agent  |  {quarter}  |  tracker as of {updated}", path)


def caption(state, rows, today):
    """Confirmed / released / not announced yet, the next confirmed date and any
    Nasdaq conflict. No estimated dates (user, 2026-09-29)."""
    comps = state["companies"]
    live = {t: e for t, e in comps.items() if e.get("status") != "not_applicable"}
    n_rel = sum(1 for e in live.values() if e.get("status") == "released")
    n_pr = sum(1 for e in live.values() if e.get("confidence") == "pr" and e.get("status") != "released")
    n_none = len(live) - n_rel - n_pr
    nxt = ""
    upcoming = sorted((e["india_update_date"], t) for t, e in live.items()
                      if e.get("confidence") == "pr" and e.get("status") != "released"
                      and e.get("india_update_date")
                      and date.fromisoformat(e["india_update_date"]) >= today)
    if upcoming:
        d, t = upcoming[0]
        nxt = f"\nNext confirmed: {t} on {datetime.fromisoformat(d):%d %b} ({live[t].get('india_slot', '?').lower()})"
    conflicts = sorted(t for t, e in live.items() if e.get("nasdaq_conflict"))
    extra = ("\nNasdaq disagrees with the notice, please check: " + ", ".join(conflicts)) if conflicts else ""
    return (f"Biotech tracker {state.get('quarter')} - {today:%a %d %b}, {datetime.now(IST):%H:%M} IST\n"
            f"{n_pr} dates confirmed by PR, {n_rel} released, {n_none} not announced yet.{nxt}{extra}")


def send_photo(path, text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("!! TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set - nothing sent")
        return False
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data={"chat_id": chat, "caption": text},
                          files={"photo": ("digest.png", f, "image/png")}, timeout=60)
    ok = r.ok and r.json().get("ok")
    print("telegram sent" if ok else f"telegram FAILED: {r.text[:300]}")
    return bool(ok)


def main():
    today = datetime.now(IST).date()
    state = tracker.load()
    if not state.get("quarter"):
        print("no tracker yet; run scan.py first")
        return 1
    if today.weekday() >= 5 and os.environ.get("FORCE_SEND") != "1":
        print("weekend - digest is weekdays only")
        return 0
    rows = build_rows(state, today)
    path = render(rows, state["quarter"], state.get("updated"))
    text = caption(state, rows, today)
    print(text)
    for r in rows:
        print("  ", " | ".join(r))
    if os.environ.get("DRY_RUN") == "1":
        print(f"dry run - wrote {path}")
        return 0
    return 0 if send_photo(path, text) else 2


if __name__ == "__main__":
    sys.exit(main())
