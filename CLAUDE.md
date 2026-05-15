# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

熊本市の子育て支援イベント情報を5系統のソースから毎朝自動収集し、GitHub Pages で公開するサービスです。
Public URL: https://nakinoippatu.github.io/kumamoto-kosodate/

## Setup & Running

```bash
pip install requests beautifulsoup4 playwright pdfplumber lxml
playwright install chromium
python scraper.py
```

No tests or lint commands. After running, open `docs/index.html` in a browser to verify locally.

## Architecture

The entire backend is **`scraper.py`** (~4,500 lines). The frontend is **`docs/index.html`** (FullCalendar 6, self-contained). `docs/events.json` is auto-generated - do not edit by hand.

### Data Flow

```
scraper.py
  scrape()              # orchestrates all 5 sources
    Source A+B          # shared Playwright browser (launch cost optimization)
    Source C            # requests (static HTML)
    Source D+E          # separate shared Playwright browser + pdfplumber
  _merge_with_cache()   # supplements from previous run if total < 150 events
  save()                # writes docs/events.json
  update_html()         # injects JSON inline into docs/index.html via markers
```

Each source is individually wrapped in try/except. Cross-source deduplication is NOT performed - each scraper deduplicates internally.

### scraper.py Structure

| Section | Key names | Description |
|---|---|---|
| Utilities | `_z2h`, `_normalize`, `_extract_time`, `_guess_category`, `_is_non_event`, `_fetch_pdf_bytes`, `_is_real_kumamoto_pdf` | Shared helpers |
| Source A | `scrape_kosodate_with_page(pw_page)` | Playwright, paginates up to 10 pages |
| Source B | `scrape_sogo_center_with_page(pw_page)` | Playwright, single page |
| Source C | `scrape_kodomobunka()` | requests; filtered by `KODOMOBUNKA_KW` |
| Source D | `HALL_CONFIGS` + `scrape_all_halls_adapted()` | 10 halls - pdfplumber + Playwright |
| Source E | `CENTER_DEFS` + `_parse_center_*()` + `scrape_all_centers()` | 18 centers - cascading parsers |
| Orchestration | `scrape()`, `save()`, `update_html()`, `_load_cached_events()`, `_merge_with_cache()` | Main flow |

### Source D - Hall Registry Pattern

Each hall has a `scrape_X()` function in `HALL_CONFIGS`. `scrape_all_halls_adapted(pw_page)` iterates the registry, uses `_fetch_pdf_url_from_page()` to discover the PDF URL, downloads via `_fetch_pdf_bytes()`, then calls the parser. `_hall_event_to_common()` converts the internal dict to the shared schema and adds the star prefix.

`gofuku` is a known exception: scanned PDF yields no text. Returns `[]`, or loads manual `gofuku_events.json` if present.

### Source E - Cascading Parser Chain

`_scrape_center_pdf()` tries parsers in order:
1. Scanned guard (text < 50 chars)
2. `_parse_ayumi_notice()` - ayumi format
3. `_parse_center_yamabiko_text()` - yamabiko format (Ueki)
4. `_parse_center_calendar_table()` - 7-col or 17-col calendar
5. `_parse_center_5col_weekly()` - Mon-Fri 5-column
6. `_parse_center_star_detail_calendar()` - star-block format
7. `_parse_center_word_position_cal()` - coordinate-based
8. `_parse_center_text_list()` - regex fallback

### Key Conventions

- PDF validation: checks `%PDF-` magic bytes, 5,000 byte minimum, Content-Type, and `city.kumamoto.jp` hostname
- Cache fallback: `_merge_with_cache()` triggers when `len(new_events) < 150`
- Category: `_guess_category()` keyword regex - add new keywords here for new event types
- HTML injection: `update_html()` uses `/* EVENTS_DATA_START */` and `/* EVENTS_DATA_END */` markers. Never remove them from `index.html`

### events.json Schema

```json
{
  "updated_at": "2026-02-26T07:00:00",
  "count": 217,
  "events": [{
    "title": "★離乳食講座",
    "date_iso": "2026-03-05",
    "date_raw": "2026年3月5日",
    "time_raw": "10:00〜11:30",
    "location": "施設名",
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

`date_iso` is the sort key. `source` identifies which facility's data the cache merge uses.

## GitHub Actions

`.github/workflows/scrape.yml` runs daily at 07:00 JST on `main`. Changes to `scraper.py` must be merged to `main` to take effect.

## Known Fragile Areas

- PDF layout changes: facility-specific parsers silently return 0 events when layouts change
- Scanned PDFs: `gofuku` and similar image-only PDFs yield 0 events
- Parser cascade failures: check logs when Source E returns 0 - `_scrape_center_pdf` prints diagnostics
- Dynamic PDF discovery: `_fetch_pdf_url_from_page()` returns None if city page structure changes

---

## 作業ルール（AIへの指示）

### 進め方の基本

- 3ステップ以上または重要な判断を含む作業は、必ず `tasks/todo.md` にチェックリスト形式で計画を立ててから始める。実装前にオーナーに確認する。
- 何かおかしくなったら即座に止めて再計画する。押し切らない。
- すべての説明は日本語で、専門用語は平易な言い換えを先に置く。
- 完了前に自問する：「経験豊富な人が見てもOKと言えるか？」

### 役割分担

- 調査・探索・並列作業は別の担当に任せ、メインの作業メモを散らかさない。
- 担当1人につきタスク1つ。

### 修正を受けたとき

- `tasks/lessons.md` に学びと再発防止ルールを記録する。
- セッション開始時に関連する学びを見直す。

### バグ対応

- バグ報告時は確認なしに調査・修正に入る。一時しのぎ禁止。

---

## 安全ルール

### 既存ファイルの上書き

既存ファイルを編集・上書きする前に、必ず「〇〇を上書きしますが、よろしいですか？」と確認する。

### 削除コマンド

`rm`・`del`・`rmdir` などの削除系コマンドは原則実行しない。必要時は対象ファイル名と理由を明示して承認を得る。`rm -rf` はいかなる場合も実行しない。

### パッケージ追加

`pip install`・`npm install` などの前に、「何をインストールするか・なぜ必要か・影響範囲」を説明して承認を得る。

### 専門的なコマンド

技術的・専門的なコマンドの前に、「何をするか・実行するとどうなるか・リスク」を日本語で説明し「実行してよいですか？」と確認する。
