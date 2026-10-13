"""Two-hourly scan: read every company's press-release sources, pick up date
advisories and results releases for the period each company is reporting
on, and keep state/tracker.json current. Sends nothing to Telegram;
digest.py does that.

Runs on GitHub Actions (no sec.gov access) and on the laptop alike.
Env: ONLY_TICKERS (comma list), TARGET_QUARTER (calendar label, e.g. 3Q26;
mapped to each company's fiscal equivalent), VERBOSE=1.

Expected dates for companies with no notice come from biotech_universe.json
(a copy of the file built by biotech-earnings-automation/build_universe.py)
when it sits next to this script; without it the tracker simply says
"no notice".
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import feedparser
from bs4 import BeautifulSoup

import common
import tracker

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.environ.get("SEEN_STATE") or os.path.join(ROOT, "state", "seen.json")
UNIVERSE_PATH = os.path.join(ROOT, "biotech_universe.json")


def log(*a):
    print(*a, flush=True)


def load_companies():
    with open(os.path.join(ROOT, "companies.json"), encoding="utf-8") as f:
        companies = json.load(f)
    universe = {}
    if os.path.exists(UNIVERSE_PATH):
        with open(UNIVERSE_PATH, encoding="utf-8") as f:
            universe = json.load(f).get("companies", {})
    for c in companies:
        c["universe"] = universe.get(c["ticker"], {})
    return companies


def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1, sort_keys=True)


# ---------------------------------------------------------------- sources
def _entry_date(e):
    for k in ("published_parsed", "updated_parsed"):
        v = e.get(k)
        if v:
            return date(v.tm_year, v.tm_mon, v.tm_mday)
    return None


def items_from_feed(url):
    r = common.get(url)
    fp = feedparser.parse(r.content)
    out = []
    for e in fp.entries[:60]:
        out.append({"title": " ".join((e.get("title") or "").split()),
                    "link": e.get("link"), "date": _entry_date(e),
                    "summary": BeautifulSoup(e.get("summary", "") or "", "lxml").get_text(" ")})
    return out


def items_from_json(url):
    """Q4 Inc public press-release service: up to 40 items with the full body inline."""
    r = common.get(url)
    lst = r.json().get("GetPressReleaseListResult", [])
    base = url.split("/feed/")[0]
    out = []
    for e in lst:
        raw = e.get("PressReleaseDate") or ""
        try:
            pub = datetime.strptime(raw[:10], "%m/%d/%Y").date()
        except ValueError:
            pub = None
        link = e.get("LinkToDetailPage") or ""
        if link.startswith("/"):
            link = base + link
        body = BeautifulSoup(e.get("Body") or "", "lxml").get_text(" ")
        out.append({"title": " ".join((e.get("Headline") or "").split()), "link": link,
                    "date": pub, "summary": body, "body_inline": True})
    return out


def clean_title(title, kind):
    """Google News appends ' - Publisher' to every headline."""
    if kind == "google_news" and " - " in title:
        return title.rsplit(" - ", 1)[0].strip()
    return title


_GENERIC_FIRST = {"american", "national", "independence", "urban", "essential", "healthcare",
                  "four", "first", "global", "new", "the", "united", "federal", "public",
                  "extra", "equity", "realty", "sun", "mid", "summit", "perspective"}


def name_key(company):
    """Short, distinctive prefix of the company name used to make sure a
    mirror-feed headline is about THIS company (Yahoo's BNTX feed is mostly
    Pfizer and Moderna news). First word, or the first two when the first is
    generic or very short ('CG Oncology')."""
    words = [w for w in re.split(r"\s+", (company.get("name") or "").strip()) if w]
    words = [w for w in words if w.strip(".,").lower() not in {"inc", "corp", "corporation", "ltd", "llc", "l.p", "lp", "plc", "se", "s.a", "sa", "holdings"}]
    if not words:
        return company["ticker"].lower()
    first = words[0].strip(".,").lower()
    if len(first) < 3 or first in _GENERIC_FIRST:
        return " ".join(words[:2]).lower()
    return first


def headline_is_ours(title, company):
    key = name_key(company)
    t = " ".join(title.lower().split())
    return key in t or company["ticker"].lower() in re.findall(r"\(([a-z.]+)\)", t)


def looks_like_company_release(title, company):
    """A mirror feed (Yahoo, Google News) carries third-party articles next to
    the company's own releases: "Jasper Therapeutics beats Q2 expectations
    but shares fall after results" classifies as results yet is a news story
    published days after the real release. The company's own headline starts
    with its name and carries a reporting verb after it."""
    t = " ".join(title.lower().split())
    # Zacks / Simply Wall St / Motley Fool forms that carry the company name
    # AND a reporting verb: "Celldex Therapeutics (CLDX) Reports Q2 Loss,
    # Misses Revenue Estimates", "Kymera Therapeutics Q2 Earnings Call
    # Highlights", "Why Kura Oncology (KURA) Is Up 14.5% After ... Q2 Results".
    if _THIRD_PARTY.search(t):
        return False
    key = name_key(company)
    pos = t.find(key)
    if pos < 0:
        return False
    tail = t[pos:]
    return bool(common._REPORT_VERB.search(tail)
                or re.search(r"\b(to|will)\s+(report|announce|host|release|provide|issue|present)\b", tail))


_THIRD_PARTY = re.compile(r"\b(estimates?|zacks|analysts?|wall st(?:reet)?|call highlights|transcript|"
                          r"stock|shares|undervalued|overvalued|why\s+\w|is\s+up|is\s+down|beats?|misses|lags|tops)\b", re.I)


NASDAQ_H = {"User-Agent": common.HEADERS["User-Agent"], "Accept": "application/json",
            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


_NASDAQ_FAILS = 0
NASDAQ_MAX_FAILS = 3


def nasdaq_date(ticker):
    """(date, session, confirmed:bool) from Nasdaq's per-ticker earnings page, or None.

    Wording seen 2026-09-25: "is expected* to report earnings on 10/15/2026
    before market open" once the company has announced; "is estimated to
    report ... derived from an algorithm" before that; "Our vendor ... hasn't
    provided us with the upcoming earnings report date" for the foreign
    filers (TLX, NBTX) and enGene.

    Single attempt with a short timeout and a circuit breaker: Nasdaq hangs a
    throttled client to the timeout.
    """
    global _NASDAQ_FAILS
    if _NASDAQ_FAILS >= NASDAQ_MAX_FAILS:
        return None
    try:
        r = common.session.get(f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date",
                               headers=NASDAQ_H, timeout=12)
        d = (r.json() or {}).get("data") or {}
    except Exception as e:  # noqa: BLE001
        _NASDAQ_FAILS += 1
        log(f"  {ticker}: nasdaq failed ({_NASDAQ_FAILS}/{NASDAQ_MAX_FAILS}): {e}")
        return None
    _NASDAQ_FAILS = 0
    text = " ".join((d.get("reportText") or "").split())
    m = re.search(r"\b(\d{2})/(\d{2})/(20\d{2})\b", text)
    if not m:
        return None
    dt = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    confirmed = "estimated" not in text.lower() and "algorithm" not in text.lower()
    return dt, common.market_timing(text), confirmed


_URL_DATE = re.compile(r"(20\d{2})[-/](\d{2})[-/](\d{2})")
_PRN_PREFIX = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+20\d{2},\s+\d{1,2}:\d{2}\s+[A-Z]{2,4}\s+")


def _html_item_date(a, href, title):
    """Publication date of a listing entry: from the link (Nuvalent's
    investors.nuvalent.com/2026-05-07-...), the anchor text (PR Newswire's
    "May 07, 2026, 06:30 ET ..."), or the surrounding card ("Press Releases
    June 15, 2026 in Press Releases" on engene.com)."""
    m = _URL_DATE.search(href or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    par = a.find_parent(["article", "li", "tr", "div"])
    for text in (title, " ".join(par.get_text(" ").split())[:300] if par else ""):
        for mon, day, yr in common.all_dates(text):
            if yr:
                try:
                    return date(yr, mon, day)
                except ValueError:
                    continue
    return None


def items_from_html(url):
    """Plain HTML press-release listing: every anchor whose text classifies."""
    r = common.get(url)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        raw = " ".join(a.get_text(" ").split())
        t = _PRN_PREFIX.sub("", raw)
        if len(t) < 20 or not common.classify(t):
            continue
        href = a["href"]
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        out.append({"title": t, "link": href, "date": _html_item_date(a, href, raw), "summary": ""})
    return out


def pdf_text(content):
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(content))
    return " ".join(" ".join((p.extract_text() or "").split()) for p in reader.pages[:4])


_WIRE_DATELINE = re.compile(r"\((?:GLOBE\s?NEWSWIRE|BUSINESS WIRE|ACCESSWIRE|ACCESS Newswire|Newsfile Corp\.?)\)"
                            r"|/PRNewswire/|\(PRNewswire\)", re.I)


def strip_page_chrome(text):
    """Cut everything before the wire dateline. A mirror page (Yahoo Finance)
    repeats the headline and stamps its own date above the body - "Kymera
    Therapeutics to Report ... on August 5, 2026 Kymera Therapeutics, Inc.
    July 29, 2026 1 min read" - and that stamp, the earliest date after the
    announcement, was being read as the release date. The dateline city and
    date ("WATERTOWN, Mass., July 28, 2026") sit just before the marker and
    are kept."""
    m = _WIRE_DATELINE.search(text or "")
    if not m:
        return text
    start = max(0, m.start() - 80)
    cut = text.rfind(" ", 0, start)
    return text[cut + 1 if cut > 0 else start:]


def body_text(url):
    """Readable text of a press release. Handles a PDF link directly and a
    JS-rendered page whose only real content is a 'Download pdf' link."""
    r = common.get(url)
    ctype = (r.headers.get("content-type") or "").lower()
    if url.lower().split("?")[0].endswith(".pdf") or "application/pdf" in ctype:
        return pdf_text(r.content)
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    # Prefer the <article> element: Yahoo's page chrome carries a market
    # ticker strip ("Crude Oil Nov 26 93.63") that once became a release date.
    art = soup.find("article")
    text = " ".join((art or soup).get_text(" ").split())
    if art and len(text) < 300:
        text = " ".join(soup.get_text(" ").split())
    text = strip_page_chrome(text)
    if not common.all_dates(text) or len(text) < 400:
        from urllib.parse import urljoin
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().split("?")[0].endswith(".pdf"):
                try:
                    return text + " " + pdf_text(common.get(urljoin(url, href)).content)
                except Exception as e:  # noqa: BLE001
                    log(f"  pdf fallback failed {href}: {e}")
                break
    return text


# ---------------------------------------------------------------- per company
def alert_release(ticker, title, link):
    """Instant Telegram alert the moment a results release is seen (user,
    2026-09-28): biotechs release without warning, so the reviewer must know
    at once, laptop on or off. The digest is separate and unchanged."""
    import requests as _rq
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log(f"  {ticker}: (no Telegram secrets in this run, alert not sent)")
        return False
    text = f"❗ RESULTS ANNOUNCED: {ticker}\n{title[:140]}"
    if link:
        text += f"\n{link}"
    try:
        r = _rq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=20)
        ok = r.ok and r.json().get("ok")
    except Exception as e:  # noqa: BLE001
        ok = False
        log(f"  {ticker}: alert failed: {e}")
    log(f"  {ticker}: instant alert {'sent' if ok else 'FAILED'}")
    return bool(ok)


def scan_company(c, state, seen, q, y, verbose=False):
    ticker = c["ticker"]
    qe = tracker.company_quarter_end(c, q, y)
    window_lo, window_hi = tracker.season_window(c, qe, q)
    found_any = False
    for src in c.get("sources", []):
        url = src.get("url")
        if not url or src.get("disabled"):
            continue
        if src.get("verified") is False and src.get("kind") != "google_news":
            continue
        skind = src.get("kind")
        if skind == "google_news" and found_any:
            continue   # headline-only fallback, only needed when nothing real was found
        try:
            if skind == "ir_json":
                items = items_from_json(url)
            elif skind == "html_list":
                items = items_from_html(url)
            else:
                items = items_from_feed(url)
        except Exception as e:  # noqa: BLE001
            log(f"  {ticker}: source failed {url}: {e}")
            continue
        # Oldest first so a later notice overrides an earlier one; undated
        # items (plain HTML lists) go LAST, or a nav link like "Latest
        # Financial Results" is processed before the real notice.
        items.sort(key=lambda i: (i["date"] or date.max))
        for it in items:
            title, link, pub = clean_title(it["title"], skind), it["link"], it["date"]
            kind = common.classify(title)
            if not kind:
                continue
            # The old monitor's guard, before anything is taken as results:
            # "Reports Positive Phase 2 Results" is a trial, not a quarter.
            # Applied to advisories too: "Tyra to Host Conference Call on
            # Initial Results of Oral Dabogratinib from the Phase 2 SURF302
            # Study" is a data call, and its date is not a release date.
            if common.is_clinical_noise(title):
                if verbose:
                    log(f"  {ticker}: clinical noise skipped: {title[:80]}")
                continue
            if skind in ("yahoo_rss", "google_news"):
                if not headline_is_ours(title, c):
                    continue
                if not looks_like_company_release(title, c):
                    continue
            # A half-yearly filer's quarterly trading update ("Telix Q2
            # Revenue US$247M") is not its H1 results.
            if kind == "results" and c.get("half_yearly") and common.period_kind(title) == "quarter":
                continue
            # Quarter gate: the title must name the target quarter, or name no
            # quarter at all while sitting inside this season's window.
            qo = common.quarter_of(title)
            if qo:
                if not common.matches_quarter(title, q, y):
                    continue
                # "Second Quarter" with no year matches every year's item in a
                # long feed; a year-less title must also sit in the window.
                if qo[1] is None and pub and not (window_lo <= pub <= window_hi):
                    continue
            elif pub and not (window_lo <= pub <= window_hi):
                continue
            key = f"{ticker}|{kind}|{link or title}"
            if key in seen and seen[key].get("resolved"):
                continue
            found_any = True
            if kind == "results":
                before = dict(state["companies"].get(ticker, {}))
                seen_on = pub or date.today()
                changed = tracker.set_released(state, ticker, seen_on, link, title,
                                               c.get("usual_session"))
                seen[key] = {"resolved": True, "when": str(date.today())}
                if changed:
                    log(f"  {ticker}: RESULTS OUT  {title}")
                    # Alert only when the release was UNEXPECTED (user, 2026-09-28):
                    # no date notice, or it landed more than a day off the notice.
                    expected = None
                    if before.get("confidence") == "pr" and before.get("release_date"):
                        expected = date.fromisoformat(before["release_date"])
                    if expected and abs((seen_on - expected).days) <= 1:
                        log(f"  {ticker}: released as announced ({expected}); no alert")
                    else:
                        alert_release(ticker, title, link)
                continue
            # advisory: read the body for date / session / time
            if skind == "google_news":
                # Headline-only source: its links are JS redirects, so the body
                # is unreadable. Leave these to the Nasdaq check.
                seen[key] = {"resolved": True, "when": str(date.today()), "why": "google news, headline only"}
                log(f"  {ticker}: advisory seen on newswire, date left to Nasdaq: {title[:80]}")
                continue
            text = it["summary"] or ""
            if src.get("body_readable", True) and link and not it.get("body_inline"):
                try:
                    text = body_text(link) + " " + text
                except Exception as e:  # noqa: BLE001
                    log(f"  {ticker}: body unreadable {link}: {e}")
            d, sess, tm = common.find_release_details(text, pub)
            if not d:
                d = common.pick_release_date(common.all_dates(title), pub)
                sess = sess or common.market_timing(title)
            if not d:
                seen[key] = {"resolved": False, "when": str(date.today()), "why": "no date found"}
                log(f"  {ticker}: advisory but no date: {title}")
                continue
            mon, day, yr = d
            yr = yr or (pub.year if pub else y)
            try:
                rd = date(yr, mon, day)
            except ValueError:
                continue
            if not (window_lo <= rd <= window_hi):
                continue
            if c.get("release_tz") and not tm:
                # ASX-listed Telix names its Melbourne release day, which is
                # the previous US evening (see tracker.localize_release).
                rd, sess = tracker.localize_release(rd, sess, c["release_tz"])
            if not sess:
                # A notice that names no session: Nasdaq's page says "before
                # market open" once confirmed; failing that, the company's
                # usual session from last quarter.
                nd = nasdaq_date(ticker)
                if nd and nd[0] == rd and nd[1]:
                    sess = nd[1]
                elif c.get("usual_session"):
                    sess = c["usual_session"]
            # A mirror (Yahoo, newswire) never overrides the company's own
            # notice unless it is strictly newer - a reschedule. Same-day
            # copies of the same notice are just confirmations.
            cur = state["companies"].get(ticker, {})
            if (skind in ("yahoo_rss", "google_news") and cur.get("confidence") == "pr"
                    and cur.get("source_kind") not in (None, "yahoo_rss", "google_news")
                    and (pub or date.min).isoformat() <= (cur.get("announced_on") or "0000")):
                seen[key] = {"resolved": True, "when": str(date.today()), "why": "company source already has it"}
                if rd.isoformat() != cur.get("release_date"):
                    log(f"  {ticker}: mirror reads {rd} but company notice says {cur.get('release_date')}; company wins")
                continue
            changed = tracker.set_announced(state, ticker, rd, sess, tm, link, pub, title)
            state["companies"][ticker]["source_kind"] = skind
            seen[key] = {"resolved": True, "when": str(date.today())}
            log(f"  {ticker}: {'NEW/CHANGED' if changed else 'confirmed'} {rd} {sess or '?'} {tm or ''}  <- {title}")
        # Every readable source is scanned every run (IR site, then the Yahoo
        # Finance mirror). Only the headline-only Google News fallback is
        # skipped once something real has been found.
        if found_any and skind == "google_news":
            break
    return found_any


def main():
    only = [t.strip().upper() for t in os.environ.get("ONLY_TICKERS", "").split(",") if t.strip()]
    verbose = os.environ.get("VERBOSE") == "1"
    forced = None
    q, y = tracker.target_quarter()
    if os.environ.get("TARGET_QUARTER"):
        m = re.match(r"([1-4])Q(\d{2})", os.environ["TARGET_QUARTER"].upper())
        q, y = int(m.group(1)), 2000 + int(m.group(2))
        forced = (q, y)
    label = common.quarter_label(q, y)
    log(f"scan for {label}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    companies = load_companies()
    targets = {c["ticker"]: tracker.company_target(c, forced=forced) for c in companies}
    labels = {c["ticker"]: tracker.company_label(c, *targets[c["ticker"]]) for c in companies}
    state = tracker.roll_quarter(tracker.load(), q, y, labels)
    # Inactive names leave the state too, so the digest, its caption and the
    # laptop scheduler stop listing them (user, 2026-10-13: 12-name list).
    active = {c["ticker"] for c in companies if c.get("active", True)}
    state["companies"] = {t: e for t, e in state.get("companies", {}).items() if t in active}
    seen = load_seen()

    for c in companies:
        t = c["ticker"]
        if only and t not in only:
            continue
        if not c.get("active", True):
            continue
        cq, cy = targets[t]
        clabel = labels[t]
        # Half-yearly filers are never "not applicable" any more: company_target
        # gives them their live half-year period in every season.
        qe = tracker.company_quarter_end(c, cq, cy)
        fiscal_note = "" if (c.get("fye_month") or 12) == 12 else f" (fiscal, FYE month {c['fye_month']})"
        if verbose or clabel != label:
            log(f"  {t}: target {clabel}{fiscal_note}, period end {qe}")
        try:
            scan_company(c, state, seen, cq, cy, verbose)
        except Exception as e:  # noqa: BLE001
            log(f"  {t}: scan error {e}")
        # No press release found: ask Nasdaq. Its page says "expected ... on
        # <date> before market open" once the company has announced (treated as
        # confirmed, source nasdaq) and "estimated ... algorithm" before that
        # (used as the expected date). Last resort is the 8-K/6-K lag history.
        entry = state["companies"].setdefault(t, {})
        entry["quarter"] = clabel
        entry["period_end"] = qe.isoformat()
        if entry.get("confidence") == "pr" and entry.get("release_date"):
            # Keep the watch-start time in step with the current rule, so a
            # rule change reaches entries recorded under the old one.
            entry["run_at_ist"] = tracker.run_at_ist(
                date.fromisoformat(entry["india_update_date"]), entry.get("india_slot"), None,
                date.fromisoformat(entry["release_date"])).strftime("%Y-%m-%d %H:%M")
        if entry.get("confidence") == "pr" and entry.get("status") != "released" \
                and entry.get("nasdaq_checked") != str(date.today()):
            # Once a day, cross-check a confirmed date against Nasdaq. A
            # company-confirmed Nasdaq date that differs is flagged in the
            # tracker and the digest caption; the notice still wins.
            nd = nasdaq_date(t)
            entry["nasdaq_checked"] = str(date.today())
            if nd and nd[2] and nd[0].isoformat() != entry.get("release_date"):
                entry["nasdaq_conflict"] = f"Nasdaq says {nd[0].isoformat()} {nd[1] or ''}".strip()
                log(f"  {t}: CONFLICT notice {entry.get('release_date')} vs {entry['nasdaq_conflict']}")
            else:
                entry.pop("nasdaq_conflict", None)
        if entry.get("confidence") == "pr" and not entry.get("session") and entry.get("status") != "released":
            # Confirmed by a notice that named no session: ask Nasdaq each
            # scan until it says, then fall back to the usual session.
            nd = nasdaq_date(t)
            sess = nd[1] if nd and nd[0].isoformat() == entry.get("release_date") else None
            sess = sess or c.get("usual_session")
            if sess:
                tracker.set_announced(state, t, date.fromisoformat(entry["release_date"]), sess,
                                      entry.get("release_time_et"), entry.get("source_url"),
                                      date.fromisoformat(entry["announced_on"]) if entry.get("announced_on") else None,
                                      entry.get("advisory_title"))
                log(f"  {t}: session filled in as {sess}")
        if entry.get("confidence") != "pr" and entry.get("status") != "released":
            nd = nasdaq_date(t)
            lo, hi = tracker.season_window(c, qe, cq)
            if nd and not (lo <= nd[0] <= hi):
                nd = None
            if nd and nd[2]:
                rd, sess, _ = nd
                tracker.set_announced(state, t, rd, sess or c.get("usual_session"), None,
                                      f"https://www.nasdaq.com/market-activity/stocks/{t.lower()}/earnings",
                                      date.today(), "Nasdaq: company-confirmed earnings date")
                log(f"  {t}: nasdaq confirmed {rd} {sess or '?'}")
            elif nd:
                tracker.set_expected(state, t, nd[0], nd[1] or c.get("usual_session"),
                                     "Nasdaq algorithmic estimate")
            else:
                est, sess = tracker.estimate_from_history(c, qe)
                if est:
                    tracker.set_expected(state, t, est, sess,
                                         "median days after period end from the 8-K/6-K history")
                elif not entry.get("status"):
                    entry.update({"status": "no_notice", "confidence": None, "release_date": None,
                                  "india_update_date": None, "india_slot": None,
                                  "estimate_note": "no notice yet and no history to estimate from"})
            time.sleep(1.0)
        time.sleep(0.5)

    tracker.save(state)
    save_seen(seen)
    n_pr = sum(1 for v in state["companies"].values() if v.get("confidence") == "pr")
    n_rel = sum(1 for v in state["companies"].values() if v.get("status") == "released")
    n_na = sum(1 for v in state["companies"].values() if v.get("status") == "not_applicable")
    log(f"tracker: {len(state['companies'])} companies, {n_pr} announced by PR, {n_rel} released, {n_na} not applicable")


if __name__ == "__main__":
    sys.exit(main())
