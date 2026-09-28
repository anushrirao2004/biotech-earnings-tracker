"""The internal tracker: what the junior analyst agent knows about when each
biotech will report, in US Eastern and converted to the India update slot.

State lives in state/tracker.json and is committed by every scan run, so the
laptop can pull it and schedule model updates from it.

India convention (user, 2026-09-25):
  US after-close release  -> next India MORNING slot
  US before-open release  -> same-day India EVENING slot
Saturday mornings are real update slots (Friday after-close releases).

Biotech differences from the REIT tracker:
  - Every company reports on its OWN fiscal quarter (enGene closes in
    October, Nurix in November): company_target() gives the quarter each one
    is reporting on, and the entry carries that label in "quarter".
  - Half-yearly filers (Telix, Nanobiotix) report H1 with Q2 and the full
    year with Q4; in Q1 and Q3 they get status "not_applicable".
  - The season window stretches to the company's longest observed lag from
    biotech_universe.json: a 6-K filer can take 90+ days over a half year.
"""
import json
import os
import statistics
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import common

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("TRACKER_STATE") or os.path.join(ROOT, "state", "tracker.json")
ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

# When the laptop should start the model update, India time.
WATCH_FROM_IST = time(15, 0)      # start polling EDGAR here on the US release date, every release

# Days before a fiscal quarter ends at which the tracker starts watching for
# THAT quarter (the notice can come two to four weeks ahead of results).
PRE_ROLL_DAYS = 14


def target_quarter(today=None):
    """The CALENDAR quarter whose release season we are in or about to enter;
    the tracker file is labelled by it and rolls over on it.

    From about two weeks before a quarter ends the tracker must already be
    watching for THAT quarter (25 Sep -> 3Q26, not 2Q26). Before that point
    the previous quarter is still the one being reported.
    """
    today = today or date.today()
    return common.fiscal_target(today + timedelta(days=PRE_ROLL_DAYS), 12)


def company_target(company, today=None, forced=None):
    """(quarter, fiscal_year) THIS company is reporting on.

    Same pre-roll rule as target_quarter(), on the company's own fiscal
    calendar. With `forced` = (calendar q, y) from TARGET_QUARTER, the
    equivalent is the company's fiscal quarter that ENDS inside that
    calendar quarter: 2Q26 -> enGene's Q2 FY26 (ended 30 Apr), Nurix's Q2
    FY26 (ended 31 May). For a calendar-year company both rules give the
    calendar quarter itself.
    """
    fye = company.get("fye_month") or 12
    if forced:
        ref = common.quarter_end(*forced) + timedelta(days=1)
    else:
        ref = (today or date.today()) + timedelta(days=PRE_ROLL_DAYS)
    q, fy = common.fiscal_target(ref, fye)
    # Half-yearly filers (NBTX, TLX) stay in the loop every quarter (user,
    # 2026-09-28: "they release anytime"). In a Q1/Q3 season their live
    # period is the half-year that closed with the previous quarter: H1 (Q2)
    # during Q3, the full year (Q4 of the prior fiscal year) during Q1.
    if company.get("half_yearly") and q in (1, 3):
        q, fy = (2, fy) if q == 3 else (4, fy - 1)
    return q, fy


def company_quarter_end(company, q, fy):
    return common.fiscal_quarter_end(q, fy, company.get("fye_month") or 12)


def company_label(company, q, fy):
    """'2Q26' for a quarterly filer; '1H26' / 'FY26' for a half-yearly one."""
    if company.get("half_yearly"):
        return {2: f"1H{fy % 100:02d}", 4: f"FY{fy % 100:02d}"}.get(q, f"{q}Q{fy % 100:02d}")
    return common.quarter_label(q, fy)


def not_applicable(company, q):
    """Never, since 2026-09-28: half-yearly filers are tracked on their live
    half-year period in every quarter (see company_target)."""
    return False


def season_window(company, qe, q=None):
    """Dates between which this period's advisories and releases can appear.

    Opens 35 days before the period end (the notice comes early) and closes
    80 days after it, or 14 days past the company's longest observed lag in
    the 8-K/6-K history when that is later (Nanobiotix took 132 days over a
    half year), and never under 100 days for a full year (10-K/20-F season).
    """
    hi = 80
    lag = (company.get("universe") or {}).get("reporting_lag_days") or {}
    if lag.get("max"):
        hi = max(hi, int(lag["max"]) + 14)
    if q == 4:
        hi = max(hi, 100)
    return qe - timedelta(days=35), qe + timedelta(days=hi)


def load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"quarter": None, "updated": None, "companies": {}}


def save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["updated"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def roll_quarter(state, q, y, labels):
    """Start a fresh tracker when the calendar reporting quarter changes; keep
    the old one on disk. `labels` maps ticker -> the company's current period
    label: an entry still on that period (enGene's fiscal Q3, released on
    8 Sep, is still its current period when the calendar rolls to 3Q26 on
    16 Sep) is carried over instead of being rediscovered."""
    label = common.quarter_label(q, y)
    if state.get("quarter") == label:
        return state
    if state.get("quarter"):
        old = os.path.join(os.path.dirname(STATE_PATH), f"tracker_{state['quarter']}.json")
        with open(old, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    kept = {t: e for t, e in state.get("companies", {}).items()
            if e.get("quarter") and e.get("quarter") == labels.get(t)}
    return {"quarter": label, "updated": None, "companies": kept}


# ---------------------------------------------------------------- India conversion
def india_slot(release_date, release_time_et=None, session=None):
    """(india_update_date, 'Morning'|'Evening', release_datetime_ist|None, assumed:bool).

    A clock time beats the session word: 4:15 pm ET is 01:45 IST next day ->
    Morning; 8:00 am ET is 17:30 IST same day -> Evening. With only the
    session word, after-close -> next-day Morning, before-open -> Evening.
    With neither, assume after close (most of these biotechs) and say so.
    """
    assumed = False
    if release_time_et:
        h, m = (int(x) for x in release_time_et.split(":"))
        dt_et = datetime.combine(release_date, time(h, m), tzinfo=ET)
        dt_ist = dt_et.astimezone(IST)
        # Released by 19:00 IST (9:30 am ET, i.e. before the US open) -> the
        # team updates it the same evening. Anything later waits for the
        # next India morning (after-close releases land 01:30-03:30 IST).
        if dt_ist.date() == release_date and dt_ist.time() <= time(19, 0):
            return release_date, "Evening", dt_ist, False
        if dt_ist.date() == release_date:
            return release_date + timedelta(days=1), "Morning", dt_ist, False
        return dt_ist.date(), "Morning", dt_ist, False
    if session == "before open":
        return release_date, "Evening", None, False
    if session == "after close":
        return release_date + timedelta(days=1), "Morning", None, False
    return release_date + timedelta(days=1), "Morning", None, True


def run_at_ist(india_date, slot, dt_ist=None, release_date=None):
    """When the laptop should START WATCHING EDGAR for this release.

    The slot (Morning/Evening) is only a label for the digest. The watch
    always begins at 15:00 IST on the US release date - before the earliest
    pre-market release (~06:00 ET = 15:30 IST) - and the release runner then
    polls EDGAR until the 8-K/6-K appears, through the US close and into the
    India night. So a notice that says "after close" when the company in fact
    reports before the open (or the reverse) cannot cause a miss: the update
    starts the moment the filing exists, whichever session it lands in.
    """
    if release_date is None:
        release_date = india_date if slot == "Evening" else india_date - timedelta(days=1)
    return datetime.combine(release_date, WATCH_FROM_IST, tzinfo=IST)


# ---------------------------------------------------------------- upserts
def set_announced(state, ticker, release_date, session, release_time_et, source_url,
                  announced_on, title):
    india_date, slot, dt_ist, assumed = india_slot(release_date, release_time_et, session)
    entry = state["companies"].setdefault(ticker, {})
    changed = (entry.get("release_date") != release_date.isoformat()
               or entry.get("release_time_et") != release_time_et
               or entry.get("session") != session)
    # A "released" flag with a release date still in the future came from a
    # stray item (a nav link or an events entry): revert it.
    if entry.get("status") == "released" and release_date > date.today():
        for k in ("released_seen", "results_url", "results_title"):
            entry.pop(k, None)
        entry["status"] = "announced"
    entry.update({
        "status": "announced" if entry.get("status") != "released" else "released",
        "release_date": release_date.isoformat(),
        "release_time_et": release_time_et,
        "session": session,
        "session_assumed": assumed,
        "india_update_date": india_date.isoformat(),
        "india_slot": slot,
        "release_time_ist": dt_ist.strftime("%H:%M") if dt_ist else None,
        "run_at_ist": run_at_ist(india_date, slot, dt_ist, release_date).strftime("%Y-%m-%d %H:%M"),
        "source_url": source_url,
        "announced_on": announced_on.isoformat() if announced_on else None,
        "advisory_title": title,
        "confidence": "pr",
    })
    if changed:
        entry.setdefault("history", []).append({
            "seen": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
            "release_date": release_date.isoformat(),
            "release_time_et": release_time_et,
            "session": session,
        })
    return changed


def set_expected(state, ticker, release_date, session, note):
    """Estimate for a company with no advisory yet. Never overrides a PR."""
    entry = state["companies"].setdefault(ticker, {})
    if entry.get("confidence") == "pr":
        return False
    india_date, slot, dt_ist, assumed = india_slot(release_date, None, session)
    entry.update({
        "status": "expected",
        "release_date": release_date.isoformat(),
        "release_time_et": None,
        "session": session,
        "session_assumed": assumed,
        "india_update_date": india_date.isoformat(),
        "india_slot": slot,
        "release_time_ist": None,
        "run_at_ist": None,          # the laptop never schedules off an estimate
        "confidence": "estimate",
        "estimate_note": note,
    })
    return True


def set_released(state, ticker, seen_on, url, title, session=None):
    """`session` is the company's usual session, used only when no notice
    gave a release date (the India slot of an unannounced release)."""
    entry = state["companies"].setdefault(ticker, {})
    if entry.get("status") == "released":
        return False
    # Nothing is released before its announced date: an events entry or a
    # stale item must not mark a company done.
    if entry.get("release_date") and seen_on and seen_on.isoformat() < entry["release_date"]:
        return False
    entry.update({"status": "released", "released_seen": seen_on.isoformat(),
                  "results_url": url, "results_title": title})
    if not entry.get("release_date") and seen_on and seen_on != date.today():
        india_date, slot, _, assumed = india_slot(seen_on, None, session)
        entry.update({"release_date": seen_on.isoformat(), "india_update_date": india_date.isoformat(),
                      "india_slot": slot, "session": session, "session_assumed": assumed or bool(session),
                      "confidence": "results_seen"})
    return True


def set_not_applicable(state, ticker, note):
    """Half-yearly filer in a quarter it does not report."""
    entry = state["companies"].setdefault(ticker, {})
    entry.clear()
    entry.update({"status": "not_applicable", "confidence": None, "release_date": None,
                  "india_update_date": None, "india_slot": None, "estimate_note": note})
    return True


def localize_release(release_date, session, tz_name):
    """A notice from a company whose home market is far ahead of New York
    names its LOCAL release day. Telix releases to the ASX at ~8:30 a.m. AEST,
    which is ~6:30 p.m. ET the evening BEFORE ("results on Thursday August 20"
    = 6-K accepted Wednesday 19 Aug 21:48 ET). For a zone 10 or more hours
    ahead of Eastern the US release date is the day before and the session is
    after close; anything closer to New York is left alone.
    """
    try:
        local = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return release_date, session
    noon = datetime.combine(release_date, time(12, 0), tzinfo=local)
    ahead_h = (noon.utcoffset() - noon.astimezone(ET).utcoffset()).total_seconds() / 3600
    if ahead_h >= 10:
        return release_date - timedelta(days=1), "after close"
    return release_date, session


# ---------------------------------------------------------------- estimates
def estimate_from_history(company, qe):
    """Expected release date from the median days-after-period-end in the
    universe's 8-K/6-K history (biotech_universe.json), and the usual session
    from companies.json. (None, None) when the universe has nothing: the
    digest then says so rather than guessing."""
    uni = company.get("universe") or {}
    lag = (uni.get("reporting_lag_days") or {}).get("median")
    if not lag:
        hist = [h.get("days_after_quarter_end") for h in uni.get("earnings_history", [])
                if h.get("days_after_quarter_end")]
        if not hist:
            return None, None
        lag = int(statistics.median(hist))
    d = qe + timedelta(days=int(lag))
    # Skip to a weekday: nobody reports on a Saturday or Sunday.
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d, company.get("usual_session")
