# Biotech junior analyst agent - release-date tracker

Sister of the REIT tracker (`reit-earnings-monitor`), for the 18 biotechs
whose models the team updates. Scans every company's press releases for the
notice that announces WHEN results will be released, converts it to the India
update slot, keeps `state/tracker.json` current, and posts a digest image to
Telegram when asked. The laptop-side scheduler in `biotech-earnings-automation`
pulls the tracker and launches the model update at the right time.

- `scan.py` every two hours, all year (GitHub cron `41 */2 * * *`, plus
  manual runs). Updates the tracker. Never sends.
- `digest.py` on `workflow_dispatch` ONLY - there is no cron. An external
  scheduler calls `gh workflow run biotech-tracker-digest` at the India
  morning and evening slots. Sends the table as a PNG with a caption that names
  EVERY active company still without a confirmed date.
- `companies.json` one entry per biotech with verified press-release sources,
  `fye_month`, `half_yearly`, `usual_session`, and `name` (the headline-is-ours
  check on the Yahoo mirror needs it). Set `pinned: true` after a hand fix.
- `biotech_universe.json` a copy of the EDGAR-derived reference built by
  `biotech-earnings-automation/build_universe.py`: per-company 8-K/6-K
  results history and `reporting_lag_days`. Used only for the expected date
  of a company that has not announced. Refresh it by copying the file over.
- `state/tracker.json` the tracker. Committed by every scan run.
  `state/seen.json` the items already processed.

India convention: US after-close release -> next India morning slot;
US before-open release -> same India evening slot. Saturday mornings count.

## What is biotech-specific

Everything the REIT tracker does not have to think about.

**Fiscal quarters.** Every company is scanned for ITS OWN current period.
enGene's fiscal year ends 31 October (fiscal Q3 FY26 ended 31 Jul 2026,
released 8 Sep), Nurix's ends 30 November (fiscal Q3 FY26 ended 31 Aug). The
tracker file is labelled by the calendar quarter (`3Q26`) and each entry
carries the company's own label in `quarter`. `common.fiscal_target()` is the
old biotech monitor's function, unchanged.

**Half-yearly filers.** Telix (ASX, 6-K) and Nanobiotix (Euronext, 6-K)
report H1 with Q2 and the full year with Q4 - entries `1H26` and `FY26`. In
Q1 and Q3 they get status `not_applicable` and the digest lists them as such.
Telix's quarterly revenue "trading updates" are not results and are skipped
by `period_kind()`. Nanobiotix's H1 lands 86-115 days after 30 June, so its
season window runs to the longest lag in the universe file plus 14 days.

**Clinical noise.** The old monitor's `is_clinical_noise()` runs before any
headline is taken as results OR as a date notice: "Reports Positive Phase 2
Results" is a trial; "to Host Conference Call on Initial Results of Oral
Dabogratinib" is a data call, not an earnings date. A headline that names
its FINANCIAL results explicitly is never noise ("... Financial Results and
Highlights Positive Phase 2 Topline Data"). Conference appearances ("to
Present at the Morgan Stanley Annual Healthcare Conference") are not
advisories either.

**Late or no notice.** Most of these companies never announce a date, or
announce it a week ahead. With no notice the entry is an `estimate`: Nasdaq's
algorithmic date when it has one inside the season window, else the median
days-after-period-end from the universe file, skipped to a weekday, with the
company's `usual_session` from `companies.json`. A Nasdaq date that Nasdaq
marks as company-confirmed becomes `confidence: pr` with source Nasdaq. When
a results release is seen with no notice before it, the entry is
`results_seen` with the company's usual session.

**Home-market timing.** Telix names its Melbourne release day, which is the
previous US evening (H1 2026: "results on Thursday August 20" = 6-K accepted
Wednesday 19 Aug 21:48 ET). `release_tz` in `companies.json` makes
`tracker.localize_release()` shift the date back a day and call it after
close, so the India slot is the right morning.

## Sources

Same source kinds as the REIT tracker: `ir_json` (Q4 public JSON service),
`ir_rss`, `html_list`, `yahoo_rss`, `google_news`. The Q4-hosted IR sites of
most small biotechs answer 403 to a bare browser User-Agent but serve their
feeds and detail pages to a feed-reader UA; `common.get()` retries a 403 with
that UA. Every entry carries the Yahoo Finance per-ticker feed as a mirror;
third-party stories on it (Zacks "Reports Q2 Loss, Misses Estimates", "Q2
Earnings Call Highlights") are filtered by `looks_like_company_release()`.
A mirror never overrides the company's own notice unless it is strictly newer.

Plain HTML listings get a date from the link path (Nuvalent's PR
Newswire-hosted site), the anchor text (prnewswire.com) or the card text
(engene.com), so undated items no longer sort last.

## Running

```
pip install -r requirements.txt
python scan.py                      # all companies, current quarter
ONLY_TICKERS=TLX,NBTX VERBOSE=1 python scan.py
TARGET_QUARTER=2Q26 TRACKER_STATE=/tmp/t.json SEEN_STATE=/tmp/s.json python scan.py   # replay a past quarter
DRY_RUN=1 python digest.py          # renders state/digest.png, sends nothing
```

Secrets (GitHub Actions): `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. Locally they
live in `.env.local`, which is git-ignored.

## Validation

Replayed against the 2Q26 season (all 18 companies, temp state) on
2026-09-26: 9 date notices read with the right date and session (BNTX, CATX,
IOVA, KURA, KYMR, SMMT, SNDX, TLX; ENGN's fiscal Q2 had none), every results
release seen on its EDGAR filing date, NUVL (acquired by GSK, no 2Q26 filing)
left as an estimate. Sessions and dates were checked against the acceptance
time of each results 8-K/6-K on EDGAR.
