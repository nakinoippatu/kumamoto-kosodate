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

There are no tests or lint commands. After running, open `docs/index.html` in a browser to verify locally.

## Architecture

The entire backend is a single file: **`scraper.py`** (~4,500 lines). The frontend is a single file: **`docs/index.html`** (FullCalendar 6, self-contained). `docs/events.json` is auto-generated — do not edit by hand.

### Data Flow

```
scraper.py
  └─ scrape()              # orchestrates all 5 sources
       ├─ Source A+B       # shared Playwright browser (launch cost optimization)
       ├─ Source C         # requests (static HTML, no JS)
       └─ Source D+E       # separate shared Playwright browser + pdfplumber
  └─ _merge_with_cache()  # supplements from previous run if total < 150 events
  └─ save()               # writes docs/events.json
  └─ update_html()        # injects JSON inline into docs/index.html via marker comments
```

Each source call inside `scrape()` is individually wrapped in `try/except`, so one failure does not abort the others. Cross-source deduplication is **not** performed at the `scrape()` level — each scraper deduplicates internally (by URL or title+date).

### scraper.py Internal Structure

| Section | Key names | Description |
|---|---|---|
| Utilities | `_z2h`, `_normalize`, `_extract_time`, `_guess_category`, `_is_non_event`, `_fetch_pdf_bytes`, `_is_real_kumamoto_pdf` | Shared text/PDF helpers used across all sources |
| Source A | `scrape_kosodate_with_page(pw_page)` | 子育てナビ — Playwright, paginates up to 10 pages |
| Source B | `scrape_sogo_center_with_page(pw_page)` | 総合子育て支援センター — Playwright, single page |
| Source C | `scrape_kodomobunka()` | こども文化会館 — requests; filtered to infant events by `KODOMOBUNKA_KW` |
| Source D | `HALL_CONFIGS` registry + `scrape_koda/seibu/nishihara/hanazono/takuma/akitsu/gofuku/tenmei/ooe/jonan()` + `scrape_all_halls_adapted()` | 10 children's halls — pdfplumber, Playwright for URL discovery |
| Source E | `CENTER_DEFS` list + `_parse_center_*()` variants + `scrape_all_centers()` | 18 childcare support centers — pdfplumber, cascading parsers |
| Orchestration | `scrape()`, `save()`, `update_html()`, `_load_cached_events()`, `_merge_with_cache()` | Main flow + cache fallback |

### Source D — Children's Hall Registry Pattern

Each of the 10 halls has a dedicated `scrape_X()` function registered in `HALL_CONFIGS`. The orchestrator `scrape_all_halls_adapted(pw_page)` iterates the registry, uses `_fetch_pdf_url_from_page()` (Playwright) to discover the current PDF URL from the facility's city.kumamoto.jp page, downloads the PDF via `_fetch_pdf_bytes()`, and calls the facility's parser.

`_hall_event_to_common()` converts each hall's internal event dict (`date`, `time`, `source`, `category`, …) to the shared schema (`date_iso`, `date_raw`, `time_raw`, `location`, `apply_info`, `needs_reservation`, …). The `★` prefix is added to `title` and `needs_reservation` is set here.

花園 is special: it needs **two PDFs** (front = calendar, back = details) and uses `_fetch_pdf_urls_from_page(..., count=2)`. 五福/天明/大江 use Playwright to find the PDF URL dynamically on the city page before downloading.

五福児童室 (`scrape_gofuku`) is a known exception: the PDF is scanned (image-only), so pdfplumber returns no text. The function returns `[]`. If a manual `gofuku_events.json` is placed in the project root, it is loaded as a fallback.

### Source E — Cascading Parser Fallback Chain

The 18 centers are defined in `CENTER_DEFS` (a list of dicts with `source`, `ward`, `location`, `page_url`, `list_key`). The orchestrator `scrape_all_centers()` fetches the 統括ページ and builds a `{list_key: pdf_url}` map via `_fetch_center_pdf_urls()`.

For each facility, `_scrape_center_pdf(pdf_bytes, cdef)` tries parsers in order until one returns events:

1. Scanned PDF guard — text < 50 chars → return `[]` with warning
2. `_parse_ayumi_notice()` — あゆみ形式 (detected by keyword `あゆみ子どもセンター`)
3. `_parse_center_yamabiko_text()` — やまびこ形式 (detected by keyword `やまびこだより`, for 植木)
4. `_parse_center_calendar_table()` — standard 7-col calendar (format A) or 17-col (format B, 京町台 style, `use_offset=True`)
5. `_parse_center_5col_weekly()` — さくらっこ形式 (Mon–Fri 5-column)
6. `_parse_center_star_detail_calendar()` — 幸田形式 (`☆EventName（N日）` blocks)
7. `_parse_center_word_position_cal()` — だいいち形式 (word-coordinate-based, handles font mis-rendering)
8. `_parse_center_text_list()` — final regex fallback on raw text

After parsing, `_extract_text_time_map()` back-fills missing times from the full PDF text, and `_extract_text_no_reservation_days()` identifies days marked `予約不要` to override `all_reserved=True`.

### Key Conventions

- **PDF validation**: `_fetch_pdf_bytes()` checks magic bytes (`%PDF-`), minimum size (5,000 bytes), and Content-Type. `_is_real_kumamoto_pdf()` additionally validates that the URL's `netloc` ends with `city.kumamoto.jp` and `path` ends with `.pdf`, preventing ReadSpeaker proxy links from being treated as PDFs.
- **Cache fallback**: if `len(new_events) < 150`, `_merge_with_cache()` supplements source-by-source from the previous `events.json`, excluding past-dated events.
- **Category assignment**: `_guess_category()` uses keyword regex over the event title — add new keywords here when new event types appear.
- **Generic calendar parser**: `_parse_calendar_table()` handles standard 7-col (Mon–Sun) PDF calendars and is reused across multiple facilities. Facility-specific parsers exist only when layouts deviate.
- **`update_html()` injection**: uses `/* EVENTS_DATA_START */` and `/* EVENTS_DATA_END */` markers in `index.html` to embed `const INLINE_EVENTS = {...}`. The string `</` is escaped to `<\/` to prevent XSS. Never remove these markers from `index.html`.

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

`★` prefix in `title` and `needs_reservation: true` are always set together. `date_iso` is the sort key. `source` is the key used by cache merge logic to identify which facility's data is present.

## GitHub Actions

`.github/workflows/scrape.yml` runs daily at 07:00 JST (22:00 UTC) on `main`. It installs all dependencies (including playwright and pdfplumber), runs `python scraper.py`, then commits and pushes `docs/events.json` and `docs/index.html`. Manual runs are available via `workflow_dispatch`.

The workflow always checks out `main` — changes to `scraper.py` must be merged to `main` to take effect in scheduled runs.

## Known Fragile Areas

- **PDF layout changes**: When a facility redesigns its PDF, the facility-specific parser silently returns 0 events. The cache fallback compensates temporarily but the parser must be updated to match the new layout.
- **Scanned PDFs**: 五福児童室 and similar facilities with image-only PDFs yield 0 events — pdfplumber cannot extract text from scanned images. 五福 falls back to a manual `gofuku_events.json` if present.
- **Parser cascade silent failures**: When all Source E parsers return 0, `_scrape_center_pdf` prints table dimensions and a text preview. Check logs before assuming a source has no events.
- **Source D dynamic PDF discovery**: 五福/天明/大江 use Playwright to find the PDF URL from the city page. If the page structure changes, `_fetch_pdf_url_from_page()` may return `None`.
