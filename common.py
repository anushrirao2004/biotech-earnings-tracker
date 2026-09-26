"""Shared helpers for the Biotech junior analyst agent tracker.

Adapted from the REIT tracker (which itself grew out of the first biotech
monitor): same HTTP wrapper, same advisory/results classifier, same
"earliest date strictly after the announcement" rule for reading a reporting
date out of a press release, release TIME extraction and the wide
market-session wording.

Biotech-specific parts, ported back from the old biotech monitor:
  - fiscal_target(): per-company fiscal quarters (enGene closes in October,
    Nurix in November) and the half-year cadence of the foreign filers
    (Telix, Nanobiotix report H1 and full year only).
  - quarter_of() understands "H1 2026", "Half Year", "First Half", "interim",
    "FY25" as well as the quarter forms.
  - is_clinical_noise(): a "results" headline about a trial, not a quarter.
"""
import calendar
import re
import time
from datetime import date

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml;q=0.9,*/*;q=0.8",
}
# The Q4-hosted IR sites of most small biotechs (ir.iovance.com,
# ir.kuraoncology.com, ir.tyra.bio, investors.biontech.de ...) answer 403 to
# a bare browser User-Agent - a Chrome UA without the rest of a browser's
# fingerprint reads as a bot - yet serve their feeds AND detail pages to a
# plain feed-reader UA. Verified 2026-09-25 on 13 of 18 hosts.
FEED_HEADERS = {
    "User-Agent": "feedparser/6.0 +https://github.com/kurtmckee/feedparser",
    "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8",
}
session = requests.Session()
session.headers.update(HEADERS)


def get(url, timeout=20, **kw):
    """GET with one retry. A 403 under the browser UA is retried at once with
    the feed-reader UA; a 403 under both is a real refusal."""
    last = None
    for attempt in range(2):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True, **kw)
            if r.status_code == 403:
                r2 = session.get(url, timeout=timeout, allow_redirects=True,
                                 headers=FEED_HEADERS, **kw)
                if r2.status_code != 403:
                    r = r2
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- classifier
_PERIOD = (r"(q[1-4]|[1-4]q|h[12]|first|second|third|fourth|full[\s-]?year|fiscal|annual|"
           r"half[\s-]?year|interim|quarter|year[\s-]?end(?:ed)?|(?:three|six|nine|twelve)[\s-]?months)")

_ADVISORY_PATTERNS = [
    re.compile(r"\b(to|will)\s+(report|announce|host|release|provide|issue|present|discuss)\b.{0,80}"
               r"(result|financial|earnings|conference call|" + _PERIOD + r")", re.I),
    re.compile(r"\bannounce[sd]?\s+(the\s+)?(dates?|timing|schedule|details)\b.{0,60}(result|earnings|financial|call)", re.I),
    re.compile(r"\b(earnings|results)\b.{0,30}\b(release|call|webcast)\b.{0,30}\b(date|schedule|details|information)\b", re.I),
    re.compile(r"\b(date|schedule|timing)\b.{0,20}\b(of|for)\b.{0,40}\b(earnings|results)\b", re.I),
    re.compile(r"\b(to|will)\s+host\b.{0,60}(conference call|webcast|earnings call)", re.I),
    re.compile(r"\b(conference call|webcast|earnings call)\b.{0,40}\b(notification|notice|invitation|advisory|details|reminder)\b", re.I),
    # Real REIT title forms seen in 2Q26: "Sets Second Quarter Earnings Call for ...",
    # "Invites You to Join Its ... Earnings Conference Call", "to Discuss 2nd Quarter
    # Results", "Announces ... Earnings Release Date and Conference Call Information".
    re.compile(r"\bsets?\b.{0,50}\b(earnings|results)\b.{0,30}\b(call|release|date)", re.I),
    re.compile(r"\binvites?\s+you\b.{0,80}\b(earnings|results|conference call)", re.I),
    re.compile(r"\bto\s+discuss\b.{0,50}\b(results|earnings)", re.I),
    re.compile(r"\b(earnings|results)\b.{0,20}\b(release|call)\b.{0,10}\bdate", re.I),
    # "EPR Properties Second Quarter 2026 Earnings Conference Call Scheduled for July 30"
    re.compile(r"\b(conference call|earnings call|webcast|release)\b.{0,30}\bscheduled\s+for\b", re.I),
    # "Schedules Second Quarter 2026 Earnings Release and Conference Call" (BNL),
    # "Announces Second Quarter 2026 Earnings Release and Teleconference Dates" (BRX, CPT),
    # "Announces Release and Conference Call Dates for Its Second Quarter 2026 Earnings" (ESS)
    re.compile(r"\bschedules?\b.{0,60}\b(earnings|results)\b", re.I),
    re.compile(r"\b(earnings|results)\b.{0,60}\b(release|call|teleconference|webcast)\b.{0,40}\bdates?\b", re.I),
    re.compile(r"\b(release|call|teleconference)\b.{0,40}\bdates?\b.{0,40}\b(earnings|results)\b", re.I),
    # Verb-less scheduling headline from the old biotech monitor:
    # "Telix H1 2026 Results: Investor Webcast Notification".
    re.compile(r"\b(notification|invitation|advisory)\b.{0,40}\b(webcast|conference call|investor call|earnings call)\b", re.I),
]

_RESULTS_PATTERNS = [
    re.compile(r"\b(report|reports|reported|announce|announces|announced|post|posts|provide|provides|release|releases)\b"
               r".{0,80}\b(results|earnings)\b", re.I),
    re.compile(r"\b(financial results|quarterly results|earnings results|operating results|financial and operating results)\b", re.I),
    re.compile(r"\b(q[1-4]|[1-4]q|h[12]|[12]h|fy\s?\d{2,4}|first|second|third|fourth|half[\s-]?year|full[\s-]?year|fiscal|interim|annual)\b"
               r"[^.]{0,40}\b(results|earnings|revenue)\b", re.I),
    # Biotech forms: "Reports Record Second Quarter 2026 Revenue of ~$99M" (IOVA),
    # "Provides First Half 2026 Operational and Financial Update" (NBTX).
    re.compile(r"\b(report|reports|reported|announce|announces|announced|provide|provides|highlights?)\b"
               r".{0,80}\b(revenue|(?:operational\s+and\s+)?financial\s+(?:update|highlights|performance))\b", re.I),
]

_CONFERENCE = re.compile(r"\bto\s+(present|participate|attend|showcase)\b", re.I)
_REPORT_VERB = re.compile(r"\b(report|reports|reported|announces|announced|releases|released|posts|provides|highlights)\b", re.I)


def classify(title: str):
    """'advisory' (date announcement), 'results' (the actual release), or None."""
    if not title:
        return None
    t = " ".join(title.split())
    # "Telix to Present at the Morgan Stanley 24th Annual Global Healthcare
    # Conference", "Perspective to Present Data ... at the 2026 ASCO Annual
    # Meeting": a conference appearance, not an earnings date ("annual" is in
    # _PERIOD, which is what let these through as advisories).
    if _CONFERENCE.search(t) and not re.search(r"\b(financial results|financial update|earnings|quarterly results|"
                                               r"half[\s-]?year results|full[\s-]?year results)\b", t, re.I):
        return None
    for p in _ADVISORY_PATTERNS:
        if p.search(t):
            return "advisory"
    # "Third Quarter 2026 Earnings Call - Wednesday, November 4, 10:30 AM EDT" is an
    # events listing, not a results release: a call/webcast title with no
    # reporting verb is never "results".
    if re.search(r"\b(earnings call|conference call|webcast)\b", t, re.I) and not _REPORT_VERB.search(t):
        return None
    # A results release names its period or carries a reporting verb. A bare
    # phrase like "Latest Financial Results" (a nav link on OHI's page) is neither.
    qo = quarter_of(t)
    if not (qo or _REPORT_VERB.search(t)):
        return None
    # "Half-year & Quarterly Results" (a nav link on Telix's IR page) names a
    # period but no year and no verb: a real release has one or the other.
    if not (_REPORT_VERB.search(t) or (qo and qo[1])):
        return None
    for p in _RESULTS_PATTERNS:
        if p.search(t):
            if re.search(r"\b(financial|earnings|revenue|ffo|fy\s?\d{2,4}|" + _PERIOD + r")\b", t, re.I):
                return "results"
    return None


# "Phase 1a/b", "Phase 2b", "Phase 1b/2" as well as "Phase 2"; and the trial
# vocabulary of a data headline ("Updated Phase 1a/b Results for BTK Degrader
# Bexobrutideg, Highlighting Durable Responses in Relapsed/Refractory CLL").
_CLINICAL = re.compile(r"\b(phase\s*[1-4i]+[abc]?(?:/[1-4i]*[abc]?)?|topline|top-line|clinical|trial|cohort|study|"
                       r"data\s+(readout|update)|dosing|dosed|enrollment|enrolment|efficacy|endpoint|patients|"
                       r"pivotal|responses?|relapsed|refractory)\b", re.I)
_FINANCIAL_PHRASE = re.compile(r"\b(financial\s+results|financial\s+update|earnings|quarterly\s+results|"
                               r"operating\s+results|half[\s-]?year\s+results|full[\s-]?year\s+results|"
                               r"(?:q[1-4]|h[12])\s+20\d{2}\s+results)\b", re.I)


def is_clinical_noise(title: str) -> bool:
    """Old-monitor guard, applied before a 'results' classification: a
    headline about trial results ("Reports Positive Phase 2 Results") is not
    an earnings release. Relaxed in one way: a title that names its FINANCIAL
    results explicitly is never noise, because biotech results headlines
    routinely carry a trial name ("... Financial Results and Highlights
    Positive Phase 2 Topline Data")."""
    t = title or ""
    return bool(_CLINICAL.search(t)) and not _FINANCIAL_PHRASE.search(t)


# ---------------------------------------------------------------- quarters
_Q_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12}


def reporting_quarter(now):
    """(quarter, year) calendar-year companies are reporting on right now."""
    m = now.month
    if m <= 3:
        return 4, now.year - 1
    if m <= 6:
        return 1, now.year
    if m <= 9:
        return 2, now.year
    return 3, now.year


def quarter_end(q, y):
    m = 3 * q
    return date(y, m, calendar.monthrange(y, m)[1])


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def fiscal_target(today, fye_month=12):
    """(quarter, fiscal year) whose period most recently ENDED for a company.

    Not everyone runs a calendar year. enGene closes in October, so on 7 Aug
    2026 its current period is fiscal Q3 (ended 31 Jul), not calendar Q2, and
    its own press releases label it that way. Nurix closes in November, so
    its "Second Quarter 2026" ended 31 May. Fiscal years are labelled by the
    calendar year they END in, matching how these companies title their
    releases. Ported unchanged from the old biotech monitor.
    """
    if not 1 <= (fye_month or 12) <= 12:
        fye_month = 12
    quarter_end_months = [((fye_month - 3 * k - 1) % 12) + 1 for k in range(4)]
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        for m in quarter_end_months:
            end = _month_end(year, m)
            if end <= today and (best is None or end > best[0]):
                best = (end, m, year)
    _, m, y = best
    months_before_fye = (fye_month - m) % 12
    quarter = 4 - months_before_fye // 3
    fiscal_year = y if m <= fye_month else y + 1
    return quarter, fiscal_year


def fiscal_quarter_end(q, fy, fye_month=12):
    """Last day of fiscal quarter q of fiscal year fy (fy = year the FY ends in)."""
    fye_month = fye_month or 12
    m = ((fye_month - 3 * (4 - q) - 1) % 12) + 1
    y = fy if m <= fye_month else fy - 1
    return _month_end(y, m)


def quarter_label(q, y):
    return f"{q}Q{y % 100:02d}"


def quarter_of(title: str):
    if not title:
        return None
    t = " ".join(title.split())
    m = re.search(r"(?:quarter|months)\s+ended\s+(\w+)\s+\d{1,2},?\s*(20\d{2})?", t, re.I)
    if m and m.group(1).lower() in _MONTHS:
        mon = _MONTHS[m.group(1).lower()]
        return (mon - 1) // 3 + 1, int(m.group(2)) if m.group(2) else None
    m = re.search(r"\bQ([1-4])\b[\s,]*(?:FY)?\s*(20\d{2}|\d{2})?", t, re.I)
    if m:
        return int(m.group(1)), _year(m.group(2))
    m = re.search(r"\b([1-4])Q\s*(20\d{2}|\d{2})?\b", t)
    if m:
        return int(m.group(1)), _year(m.group(2))
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)[\s-]+quarter\b[\s,of]*(?:and\s+(?:full|fiscal)[\s-]?year\s+)?(20\d{2})?", t, re.I)
    if m:
        w = m.group(1).lower()
        return _Q_WORDS.get(w) or int(w[0]), _year(m.group(2)) or _loose_year(t)
    # Half-year reporters (Telix, Nanobiotix and other non-US filers) never
    # say "Q2": they report H1, which closes with Q2, and H2 closes with Q4.
    # Checked before the full-year rule, since "half-year" contains "year".
    m = _H1.search(t)
    if m:
        return 2, _year(m.group(1)) or _loose_year(t)
    m = _H2.search(t)
    if m:
        return 4, _year(m.group(1)) or _loose_year(t)
    m = re.search(r"\b(full[\s-]?year|fiscal[\s-]?year|year[\s-]?end(?:ed)?|annual)\b[\s,of]*(20\d{2})?", t, re.I)
    if m:
        return 4, _year(m.group(2)) or _loose_year(t)
    # "FY25 Results", "FY 2025 Results"
    m = re.search(r"\bFY\s?(20\d{2}|\d{2})\b", t)
    if m:
        return 4, _year(m.group(1))
    return None


_H1 = re.compile(r"\b(?:first[\s-]?half|1st[\s-]?half|h1|1h|half[\s-]?year|six[\s-]?months|interim)\b[\s,of]*(20\d{2})?", re.I)
_H2 = re.compile(r"\b(?:second[\s-]?half|2nd[\s-]?half|h2|2h)\b[\s,of]*(20\d{2})?", re.I)


def period_kind(title: str):
    """'quarter', 'half' or 'year' for the period a results title names, else None.

    A half-yearly filer's "Q2 Revenue" trading update is not its H1 results
    (Telix put out Q2 revenue on 21 Jul 2026 and H1 results on 20 Aug), so
    the scan needs to know which form the title used.
    """
    if not title:
        return None
    t = " ".join(title.split())
    if _H1.search(t) or _H2.search(t):
        return "half"
    if re.search(r"\b(full[\s-]?year|fiscal[\s-]?year|year[\s-]?end(?:ed)?|annual)\b", t, re.I) or re.search(r"\bFY\s?\d{2,4}\b", t):
        return "year"
    return "quarter" if quarter_of(t) else None


def matches_quarter(title, q, y):
    got = quarter_of(title)
    if not got:
        return False
    gq, gy = got
    return gq == q and (gy is None or gy == y)


def _loose_year(t):
    # 19xx too: "Third Quarter of 1999" in a very old item must not read as year-less.
    m = re.search(r"\b((?:19|20)\d{2})\b", t)
    return int(m.group(1)) if m else None


def _year(raw):
    if not raw:
        return None
    y = int(raw)
    return y if y > 100 else 2000 + y


# ---------------------------------------------------------------- dates
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_MON_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
            "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MDY = re.compile(rf"({_MON})\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s*,?\s*(20\d{{2}})?", re.I)
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\s*,?\s*(20\d{{2}})?", re.I)
_ENDED = re.compile(rf"\b(?:quarter|months|period|year)\s+end(?:ed|ing)\s+(?:{_MON}\s+\d{{1,2}}"
                    rf"|\d{{1,2}}\s+{_MON})[,\s]*(?:20\d{{2}})?", re.I)


def all_dates(text):
    t = _ENDED.sub(" ", " ".join((text or "").split()))
    out = []
    for rx, order in ((_DMY, "dmy"), (_MDY, "mdy")):
        for m in rx.finditer(t):
            if order == "dmy":
                day, mon_raw, year = m.group(1), m.group(2), m.group(3)
            else:
                mon_raw, day, year = m.group(1), m.group(2), m.group(3)
            mon = _MON_NUM.get(mon_raw[:3].lower())
            day = int(day)
            if mon and 1 <= day <= 31:
                out.append((mon, day, int(year) if year else None))
    return out


def _sort_key(d, default_year):
    mon, day, year = d
    return (year or default_year, mon, day)


def pick_release_date(dates, announced_on=None):
    """Earliest date strictly after the announcement date (the dateline is
    repeated in the same sentence, so the first date found is usually wrong)."""
    if not dates:
        return None
    if not announced_on:
        return sorted(dates, key=lambda d: _sort_key(d, 2000))[0]
    ay, am, ad = announced_on.year, announced_on.month, announced_on.day
    later = [d for d in dates if _sort_key(d, ay) > (ay, am, ad)]
    return sorted(later, key=lambda d: _sort_key(d, ay))[0] if later else None


_AFTER_CLOSE = re.compile(
    r"after\s+(?:the\s+)?(?:u\.?s\.?\s+)?(?:(?:financial|stock|equity)\s+)?market(?:s)?(?:\s+(?:close|closes|closing))?"
    r"|after\s+(?:the\s+)?(?:close|closing)(?:\s+of)?|following\s+(?:the\s+)?(?:market\s+)?close"
    r"|post[\s-]?market|after[\s-]?hours|after\s+(?:the\s+)?(?:nyse|new york stock exchange)\s+closes?", re.I)
_BEFORE_OPEN = re.compile(
    r"before\s+(?:the\s+)?(?:u\.?s\.?\s+)?(?:(?:financial|stock|equity)\s+)?market(?:s)?(?:\s+(?:open|opens|opening))?"
    r"|before\s+(?:the\s+)?(?:market\s+)?open(?:ing)?|prior\s+to\s+(?:the\s+)?(?:market\s+)?open(?:ing)?"
    r"|pre[\s-]?market|before\s+(?:the\s+)?(?:nyse|new york stock exchange)\s+opens?", re.I)


def market_timing(text):
    if not text:
        return None
    t = " ".join(text.split())
    if _AFTER_CLOSE.search(t):
        return "after close"
    if _BEFORE_OPEN.search(t):
        return "before open"
    return None


_TIME = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)\s*"
    r"(?:\(?\s*(ET|EST|EDT|Eastern(?:\s+(?:Standard|Daylight))?(?:\s+Time)?|CT|CST|CDT|Central(?:\s+Time)?|"
    r"PT|PST|PDT|Pacific(?:\s+Time)?|MT|MST|MDT|Mountain(?:\s+Time)?)\s*\)?)?", re.I)
_TZ_OFFSET_HOURS = {"c": 1, "m": 2, "p": 3}  # hours to ADD to convert to Eastern


def release_time(text):
    """('HH:MM' in Eastern, raw) for the first clock time in the text, else (None, None).

    Times in Central/Mountain/Pacific are shifted to Eastern; no zone = Eastern.
    """
    if not text:
        return None, None
    m = _TIME.search(" ".join(text.split()))
    if not m:
        return None, None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ampm = m.group(3).replace(".", "").replace(" ", "").lower()
    if h == 12:
        h = 0
    if ampm == "pm":
        h += 12
    tz = (m.group(4) or "e").strip().lower()[0]
    h += _TZ_OFFSET_HOURS.get(tz, 0)
    if not 0 <= h <= 23:
        return None, m.group(0)
    return f"{h:02d}:{mi:02d}", m.group(0)


# "will release", "will now issue", "plans to release", "is scheduled to report",
# and the passive "results will be released" / "to be announced".
_RELEASE_VERB = re.compile(
    r"\b(?:will|to|expects?\s+to|plans?\s+to|intends?\s+to|is\s+scheduled\s+to)\s+(?:\w+\s+)?"
    r"(?:be\s+)?(?:report(?:ed)?|releas(?:e|ed)|announc(?:e|ed)|issu(?:e|ed)|provid(?:e|ed)|"
    r"publish(?:ed)?|post(?:ed)?|discuss)\b", re.I)
_CALL_WORDS = re.compile(r"\b(conference call|earnings call|investor call|webcast|call)\b", re.I)
_RELEASE_LABEL = re.compile(r"\b(?:results?|earnings)\s+release\s+date(?:\s+and\s+time)?\s*:", re.I)
# AP-style state abbreviations in wire datelines ("WATERTOWN, Mass., July 28,
# 2026 (GLOBE NEWSWIRE) --") must not end a sentence, or the dateline is cut
# off from the release sentence that follows it.
_ABBREV = re.compile(r"\b(U\.S|a\.m|p\.m|A\.M|P\.M|Inc|Corp|Ltd|Mr|Ms|Dr|No|approx|vs|e\.g|i\.e|"
                     r"Mass|Calif|Conn|Fla|Ill|Mich|Minn|Wash|Pa|N\.J|N\.Y|N\.C|S\.C|Colo|Ariz|Ore|Tenn|Va|Md|Ga|"
                     r"Wis|Ind|Okla|Ky|Nev|Neb|Mo|Ala|Del|Kan|La|Ont|Que|Wyo|Vt|N\.H|N\.M|N\.D|S\.D|R\.I|Miss|Mont|Ark)\.", re.I)


def sentences(text):
    """Split into sentences without breaking on U.S., a.m., p.m., Inc. etc."""
    t = _ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), " ".join((text or "").split()))
    return [s.replace("\x00", ".") for s in re.split(r"(?<=[.!?])\s+", t)]


def find_release_details(text, announced_on=None):
    """(date tuple|None, session|None, time 'HH:MM' ET|None) from a press-release body.

    Sentences that carry a release verb AND an earnings word are the release
    sentences; the date is the earliest one after the announcement, found
    after the verb. A sentence that only talks about the conference call is
    NOT a release sentence - call dates and call times were leaking in as
    release dates (ADC, CTRE, SPG in 2Q26). If only a call sentence exists,
    the call date is returned with `time` None and the caller may infer the
    release date from the session (an after-close release has its call the
    next morning).
    """
    if not text:
        return None, None, None
    sents = sentences(text)
    whole = " ".join(sents)
    # Label form (IRT): "Results Release Date: Monday, August 3, 2026, after the
    # market closes Conference Call Date and Time: Tuesday, August 4, 2026 ..."
    lm = _RELEASE_LABEL.search(whole)
    if lm:
        span = whole[lm.end():lm.end() + 120]
        c = _CALL_WORDS.search(span)
        span = span[:c.start()] if c else span
        d = pick_release_date(all_dates(span), announced_on)
        if d:
            return d, market_timing(span) or market_timing(whole), None
    call_only = None
    for sent in sents:
        m = _RELEASE_VERB.search(sent)
        if not m or not re.search(r"\b(results?|financial|earnings)\b", sent, re.I):
            continue
        # Look after the verb, but stop at the first mention of the call so
        # "release results on Aug 6 ... and host a call on Aug 7" gives Aug 6.
        tail = sent[m.end():]
        c = _CALL_WORDS.search(tail)
        span = tail[:c.start()] if c else tail
        d = pick_release_date(all_dates(span), announced_on) or pick_release_date(all_dates(tail), announced_on)
        if not d:
            # HR: "on Thursday, July 30, 2026, after the market closes, it is
            # scheduled to report results" - the date sits BEFORE the verb.
            head = sent[:m.start()]
            if not _CALL_WORDS.search(head):
                d = pick_release_date(all_dates(head), announced_on)
        if not d:
            continue
        # A sentence whose only verb is about the call ("A conference call to
        # discuss ... is scheduled for Friday") is a call sentence.
        if _CALL_WORDS.search(sent[:m.start()]) and not re.search(r"\b(releas|report|announc|issu)\w*", sent[:m.start()] + span, re.I):
            call_only = call_only or (d, market_timing(sent) or market_timing(whole))
            continue
        sess = market_timing(sent) or market_timing(whole) or session_from_call(sents, d, announced_on)
        tm = None
        if not _CALL_WORDS.search(sent):
            tm, _ = release_time(sent)
        return d, sess, tm
    if call_only:
        return call_only[0], call_only[1], None
    return None, market_timing(whole), None


def session_from_call(sents, release, announced_on=None):
    """Infer the session from the conference call when the notice names none
    (PLD: results October 15, call at 9:00 a.m. PT the same day). A same-day
    call by early afternoon ET means a before-open release; a same-day
    late-afternoon call or a next-day call means after close."""
    for sent in sents:
        if not _CALL_WORDS.search(sent):
            continue
        cd = pick_release_date(all_dates(sent), announced_on)
        tm, _ = release_time(sent)
        if not cd:
            continue
        same_day = cd[:2] == release[:2]
        if same_day and tm:
            return "before open" if tm <= "13:30" else "after close"
        if not same_day:
            return "after close"
    return None
