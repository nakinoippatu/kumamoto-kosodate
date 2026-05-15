# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

熊本市の子育て支援イベント情報を5系統のソースから毎朝自動収集し、GitHub Pages で公開するサービスです。  
Public URL: https://nakinoippatu.github.io/kumamoto-kosodate/

## Setup & Running

```bash
# Install dependencies (note: playwright and pdfplumber are not in requirements.txt but are required)
pip install requests beautifulsoup4 playwright pdfplumber lxml
playwright install chromium

# Run the full scrape (updates docs/events.json and docs/index.html)
python scraper.py
```

There are no tests or lint commands defined for this project.

After running, open `docs/index.html` in a browser to verify locally.

## Architecture

The entire backend is a single file: **`scraper.py`** (~4,500 lines). The frontend is a single file: **`docs/index.html`** (FullCalendar 6, self-contained). `docs/events.json` is auto-generated — do not edit it by hand.

### Data Flow

```
scraper.py
  └─ scrape()              # orchestrates all 5 sources
       ├─ Source A+B       # shared Playwright browser (cost optimization)
       ├─ Source C         # requests (static HTML)
       └─ Source D+E       # separate shared Playwright browser + pdfplumber
  └─ _merge_with_cache()  # supplements missing sources from previous run if total < 150 events
  └─ save()               # writes docs/events.json
  └─ update_html()        # injects JSON inline into docs/index.html via marker comments
```

### scraper.py Internal Structure

| Section | Functions | Description |
|---|---|---|
| Utilities | `_z2h`, `_normalize`, `_extract_time`, `_guess_category`, `_is_non_event`, `_fetch_pdf_bytes`, `_is_real_kumamoto_pdf` | Shared text/PDF helpers |
| Source A | `scrape_kosodate_with_page()` | 子育てナビ — Playwright |
| Source B | `scrape_sogo_center_with_page()` | 総合子育て支援センター — Playwright |
| Source C | `scrape_kodomobunka()` | こども文化会館 — requests |
| Source D | `scrape_koda/seibu/nishihara/hanazono/takuma/akitsu/gofuku/tenmei/ooe/jonan()`, `scrape_all_halls()`, `scrape_all_halls_adapted()` | 各児童館 — pdfplumber |
| Source E | `_fetch_center_pdf_urls()`, `_parse_center_*()` variants, `scrape_all_support_centers()` | 18子育て支援センター — pdfplumber |
| Orchestration | `scrape()`, `save()`, `update_html()`, `_load_cached_events()`, `_merge_with_cache()` | Main flow |

### Key Conventions in scraper.py

- **Per-source error isolation**: each source is wrapped in `try/except` inside `scrape()` so one failure doesn't abort the rest.
- **PDF validation**: `_fetch_pdf_bytes()` checks magic bytes (`%PDF-`), minimum size (5,000 bytes), and Content-Type before parsing. `_is_real_kumamoto_pdf()` filters out ReadSpeaker audio links that point to PDFs.
- **Cache fallback**: if `len(new_events) < 150`, `_merge_with_cache()` supplements source-by-source from the previous `events.json`, excluding past-dated events.
- **Category assignment**: `_guess_category()` uses keyword regex over the event title — add new keywords here when new event types appear.
- **`_parse_calendar_table()`**: generic 7-column (Mon–Sun) PDF calendar parser reused across multiple facilities. Facility-specific parsers exist when layouts deviate from this standard.
- **`update_html()` injection**: uses `/* EVENTS_DATA_START */` and `/* EVENTS_DATA_END */` markers in index.html to embed `const INLINE_EVENTS = {...}`. Never remove these markers from index.html.

### events.json Schema

```json
{
  "updated_at": "2026-02-26T07:00:00",
  "count": 217,
  "events": [{
    "title": "★離乳食講座",
    "date_raw": "2026年3月5日",
    "date_iso": "2026-03-05",
    "time_raw": "10:00〜11:30",
    "location": "総合子育て支援センター（中央区本荘）",
    "apply_info": "要電話申込",
    "category": "食育・栄養",
    "target_age": "0歳",
    "url": "https://...",
    "source": "施設名",
    "needs_reservation": true,
    "body_preview": ""
  }]
}
```

The `★` prefix in `title` and `needs_reservation: true` are set together. `date_iso` is the sort key. `source` is used by the cache merge logic to identify which facility's data is present.

## GitHub Actions

`.github/workflows/scrape.yml` runs daily at 07:00 JST (22:00 UTC) on `main`. It installs dependencies, runs `python scraper.py`, then commits and pushes `docs/events.json` and `docs/index.html`. Manual runs are available via `workflow_dispatch`.

The workflow always checks out `main` — changes to scraper.py must be merged to `main` to take effect in scheduled runs.

## Known Fragile Areas

- **PDF layout changes**: When a facility changes its PDF format, the facility-specific parser returns 0 events silently. The cache fallback compensates temporarily but the parser must be updated.
- **Scanned PDFs**: 城南子育て支援センター and similar facilities with image-only PDFs yield 0 events — pdfplumber cannot extract text from scanned images.
- **Source D dynamic pages**: 五福/天明/大江 児童室 use Playwright to find the PDF URL before downloading.
