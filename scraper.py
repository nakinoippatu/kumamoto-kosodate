"""
熊本市 子育て支援 統合スクレイパー
ソース:
  A) 子育てナビ（kumamoto-kekkon-kosodate.jp） Playwright使用
  B) 総合子育て支援センター（city.kumamoto.jp）  requests使用
  C) こども文化会館（kodomobunka.jp）            requests使用
  D) 各児童館（幸田/西部/西原/花園/託麻/秋津/五福/天明/大江/城南）PDF解析

必要ライブラリ:
  pip install requests beautifulsoup4 playwright pdfplumber
  playwright install chromium
"""

import io
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# URL定数
# ─────────────────────────────────────────
BASE_URL_A = "https://www.kumamoto-kekkon-kosodate.jp"
LIST_URL_A = f"{BASE_URL_A}/hpkiji/pub/List.aspx?c_id=3&class_set_id=1&class_id=523"

URL_B = "https://www.city.kumamoto.jp/kiji0031482/index.html"
SOURCE_B = "総合子育て支援センター"
LOCATION_B = "総合子育て支援センター（中央区本荘）"

URL_C = "https://www.kodomobunka.jp/event/"
BASE_URL_C = "https://www.kodomobunka.jp"
SOURCE_C = "こども文化会館"
LOCATION_C = "熊本市こども文化会館"
# 乳幼児・保護者向けフィルタキーワード
KODOMOBUNKA_KW = [
    "乳幼児", "保護者同伴", "乳児", "赤ちゃん", "ベビー",
    "0歳", "ハーフバースデー", "ハイハイ", "みつばち",
]

# ════════════════════════════════════════════════════════
# ソースD: 児童館 PDF解析ユーティリティ・スクレイパー
# ════════════════════════════════════════════════════════

# ── 共通ユーティリティ ────────────────────────────────────

def _z2h(s: str) -> str:
    """全角数字・コロンを半角に変換"""
    if not s:
        return s
    return s.translate(str.maketrans('０１２３４５６７８９：', '0123456789:'))

def _normalize(s: str) -> str:
    """制御文字除去・空白正規化"""
    if not s:
        return ''
    s = re.sub(r'\(cid:\d+\)', '', s)
    s = _z2h(s)
    s = re.sub(r'[　\s]+', ' ', s).strip()
    return s

TIME_RE = re.compile(r'(\d{1,2}):(\d{2})[〜～ー](\d{1,2}):(\d{2})')

def _extract_time(text: str) -> str | None:
    """テキストから "HH:MM〜HH:MM" を抽出。なければ None"""
    m = TIME_RE.search(_z2h(text))
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}〜{int(m.group(3)):02d}:{m.group(4)}"
    return None

def _guess_category(text: str) -> str:
    if re.search(r'離乳食|栄養|食育',                    text): return "食育・栄養"
    if re.search(r'発達|言語|相談|聴覚',                  text): return "発達・育児相談"
    if re.search(r'マッサージ|アロマ|ピラティス|エクササイズ|ストレッチ', text): return "産前・産後"
    if re.search(r'ダンス|体操|リトミック|体を動|サーキット|運動|体力', text): return "親子ふれあい"
    if re.search(r'おはなし|読み聞かせ|工作|製作|おもちゃ|あそび|遊び|ふれあい', text): return "親子ふれあい"
    if re.search(r'身体測定|すくすく|ハイハイ|赤ちゃん|0歳',      text): return "健康・医療"
    if re.search(r'パパ|父|ひとり親',                    text): return "父親・家族支援"
    return "その他"

# 自由あそび・休館など「イベントでない」コンテンツのパターン
NON_EVENT_RE = re.compile(
    r'^(自由\s*あそび|休館日?|開館|★|閉館|お知らせ|\(cid:)$',
    re.IGNORECASE
)

def _is_non_event(text: str) -> bool:
    """イベントとして登録しない内容かどうか"""
    t = re.sub(r'[\(（].*?[\)）]', '', text).strip()
    t = re.sub(r'\s', '', t)
    return not t or bool(NON_EVENT_RE.match(t))

def _fetch_pdf_bytes(url: str) -> bytes | None:
    """URLからPDFバイト列を取得"""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        logger.error(f"PDF取得失敗 {url}: {e}")
        return None


# ════════════════════════════════════════════════════════
# 【汎用】月次カレンダー型PDF パーサー
#
# 対象: 幸田児童館など「月〜日の7列カレンダー表」形式のPDF
# テーブル構造:
#   - ヘッダー行: 月 火 水 木 金 土 日
#   - 日付行と内容行が交互に並ぶ
# ════════════════════════════════════════════════════════

WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']

def _parse_calendar_table(table: list[list], year: int, month: int,
                           source: str, url: str,
                           default_time: str = "10:30〜11:00") -> list[dict]:
    """
    月〜日の7列カレンダーテーブルからイベントを抽出する汎用パーサー。

    Args:
        table:        pdfplumber の extract_tables() が返す2次元リスト
        year, month:  対象年月
        source:       イベントの出典名（例: "幸田児童館"）
        url:          施設ページURL
        default_time: 時刻が明記されていない朝の活動のデフォルト時刻

    Returns:
        イベント辞書のリスト
    """
    # ヘッダー行を探して曜日ブロック範囲を構築
    wd_cols = []  # [(weekday, start_col, end_col)]
    header_row_idx = None

    for ri, row in enumerate(table):
        found = [ci for ci, c in enumerate(row) if c and c.strip() in WEEKDAYS]
        if len(found) >= 5:  # 5曜日以上見つかればヘッダー確定
            header_row_idx = ri
            for ci in found:
                wd_cols.append([row[ci].strip(), ci, len(row)])
            for i in range(len(wd_cols) - 1):
                wd_cols[i][2] = wd_cols[i + 1][1]
            break

    if not wd_cols:
        logger.warning(f"{source}: カレンダーヘッダーが見つかりません")
        return []

    def get_weekday_block(col):
        for wd, s, e in wd_cols:
            if s <= col < e:
                return wd, s, e
        return None, None, None

    def get_block_content(content_row, start_col, end_col):
        parts = []
        for ci in range(start_col, min(end_col, len(content_row))):
            c = content_row[ci]
            if c and c.strip():
                n = _normalize(c)
                if n not in parts:
                    parts.append(n)
        return '\n'.join(parts) if parts else ''

    events = []
    i = header_row_idx + 1

    while i < len(table):
        row = table[i]

        # 日付行の検出: 全角/半角数字のみのセルが4つ以上
        day_cells = []
        for ci, c in enumerate(row):
            if c and re.match(r'^[０-９\d]+$', c.strip()):
                day_cells.append((ci, int(_z2h(c.strip()))))

        if len(day_cells) >= 4:
            content_row = table[i + 1] if i + 1 < len(table) else []

            for day_ci, day_num in day_cells:
                wd, s, e = get_weekday_block(day_ci)
                if wd is None:
                    continue

                raw = get_block_content(content_row, s, e)
                if not raw or _is_non_event(raw):
                    continue

                # タイトルと説明を分離
                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                title_parts, desc_parts = [], []
                for l in lines:
                    if re.search(r'\d{1,2}:\d{2}', l) or l.startswith(('（', '(', '※', '★', '【')):
                        desc_parts.append(l)
                    else:
                        title_parts.append(l)

                title = ' '.join(title_parts).strip()

                # title が空: raw が1行でタイトル・括弧・時刻が混在している場合
                # 例: "身体測定 （どなたでもどうぞ） 10:30〜11:00"
                # → 括弧と時刻を除去してタイトルを取り出す
                if not title:
                    clean = re.sub(r'[（(][^）)]*[）)]', '', raw)
                    clean = TIME_RE.sub('', clean).strip()
                    clean = re.sub(r'[　\s]+', ' ', clean).strip()
                    title = clean

                if _is_non_event(title):
                    continue

                time_str = _extract_time(raw) or default_time
                desc = ' '.join(desc_parts)

                try:
                    ev_date = date(year, month, day_num)
                except ValueError:
                    continue

                events.append({
                    "title":       title,
                    "date":        ev_date.strftime("%Y-%m-%d"),
                    "time":        time_str,
                    "description": desc,
                    "source":      source,
                    "url":         url,
                    "category":    _guess_category(title + desc),
                })

            i += 2
        else:
            i += 1

    return events


def _get_year_month_from_pdf_text(text: str, fallback_year: int, fallback_month: int):
    """PDFテキストから年月を推定（令和/西暦/括弧入り/年度+月号 対応）"""
    t = _z2h(text)
    # 令和N年M月（年度ではない）
    m = re.search(r'令和\s*(\d+)\s*年\s*(\d+)\s*月', t)
    if m:
        return int(m.group(1)) + 2018, int(m.group(2))
    # 西暦N年M月（括弧なし）
    m = re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月', t)
    if m:
        return int(m.group(1)), int(m.group(2))
    # "令和N年（2026年）〜 M月号" 形式
    m_year = re.search(r'[（(](20\d{2})年[）)]', t)
    m_month = re.search(r'(\d+)月号', t)
    if m_year and m_month:
        return int(m_year.group(1)), int(m_month.group(1))
    # "令和N年度" + テキスト先頭付近の "M月" (天明児童室等)
    m_nendo = re.search(r'令和\s*(\d+)\s*年度', t)
    m_tsuki = re.search(r'(\d{1,2})\s*月', t[:150])
    if m_nendo and m_tsuki:
        reiwa = int(m_nendo.group(1))
        mo    = int(m_tsuki.group(1))
        year  = reiwa + 2018 + (1 if mo <= 3 else 0)
        return year, mo
    return fallback_year, fallback_month


def _get_year_month_from_metadata(metadata: dict, text: str,
                                   fallback_year: int, fallback_month: int) -> tuple[int, int]:
    """
    PDFメタデータ + テキストから年月を推定。
    タイトルが画像でテキスト抽出できない場合に使用。
    戦略:
      1. テキストに 令和N年M月 があればそれを使用
      2. テキストの M月号 + 作成日の年で補完
      3. 作成日の翌月をデフォルト（前月作成が典型的）
    """
    y, mo = _get_year_month_from_pdf_text(text, 0, 0)
    if y:
        return y, mo

    # 作成日をパース: D:20260217140119+09'00'
    cd_m = re.search(r'D:(\d{4})(\d{2})(\d{2})', metadata.get('CreationDate', ''))

    # "N月号" + 作成日から補完
    m = re.search(r'(\d+)月号', _z2h(text))
    if m and cd_m:
        month = int(m.group(1))
        cy, cmo = int(cd_m.group(1)), int(cd_m.group(2))
        return (cy, month) if month >= cmo else (cy + 1, month)

    # 作成日の翌月
    if cd_m:
        y2, mo2 = int(cd_m.group(1)), int(cd_m.group(2))
        mo2 += 1
        if mo2 > 12:
            mo2, y2 = 1, y2 + 1
        return y2, mo2

    return fallback_year, fallback_month


# ════════════════════════════════════════════════════════
# 施設別スクレイパー
# ════════════════════════════════════════════════════════

# ── 幸田児童館 ────────────────────────────────────────────
KODA_URL      = "https://www.city.kumamoto.jp/kiji0031630/index.html"
KODA_SOURCE   = "幸田児童館"

def scrape_koda(pdf_bytes: bytes) -> list[dict]:
    """
    幸田児童館の乳幼児向けPDFを解析してイベントを返す。

    PDF構造:
        TABLE[0]: 年月ヘッダー
        TABLE[1]: 朝の活動説明
        TABLE[2]: 月〜日カレンダー  ← メイン
        TABLE[3]: 申込制活動詳細
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page   = pdf.pages[0]
        tables = page.extract_tables()
        text   = page.extract_text() or ""

    if not tables or len(tables) < 3:
        logger.warning(f"{KODA_SOURCE}: テーブルが不足しています")
        return []

    year, month = _get_year_month_from_pdf_text(text, datetime.now().year, datetime.now().month)
    logger.info(f"{KODA_SOURCE}: {year}年{month}月 解析開始")

    # TABLE[2] がカレンダー本体（最大の表）
    cal_table = max(tables, key=lambda t: len(t) * len(t[0]) if t else 0)
    events = _parse_calendar_table(
        cal_table, year, month,
        source=KODA_SOURCE,
        url=KODA_URL,
        default_time="10:30〜11:00",
    )

    logger.info(f"{KODA_SOURCE}: {len(events)} 件取得")
    return events


# ── 西部児童館 ────────────────────────────────────────────
SEIBU_URL    = "https://www.city.kumamoto.jp/kiji0031631/index.html"
SEIBU_SOURCE = "西部児童館"

def scrape_seibu(pdf_bytes: bytes) -> list[dict]:
    """
    西部児童館の乳幼児向けPDFを解析してイベントを返す。

    幸田との差異:
      - 曜日順が「日〜土」（日曜始まり）
      - 年月がタイトル画像に埋め込まれてテキスト抽出不可
        → PDFメタデータの作成日から推定
      - 日付行と内容行が1行ずつ交互（幸田と同じ）
      - イベントに「★」プレフィックスあり → 除去
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page     = pdf.pages[0]
        tables   = page.extract_tables()
        text     = page.extract_text() or ""
        metadata = pdf.metadata or {}

    if not tables:
        logger.warning(f"{SEIBU_SOURCE}: テーブルが見つかりません")
        return []

    year, month = _get_year_month_from_metadata(
        metadata, text, datetime.now().year, datetime.now().month
    )
    logger.info(f"{SEIBU_SOURCE}: {year}年{month}月 解析開始")

    # 最大テーブルがカレンダー本体
    cal_table = max(tables, key=lambda t: len(t) * len(t[0]) if t else 0)

    # 汎用パーサーで処理（日曜始まりにも対応済み）
    events = _parse_calendar_table(
        cal_table, year, month,
        source=SEIBU_SOURCE,
        url=SEIBU_URL,
        default_time="11:00〜",
    )

    # タイトルの「★」プレフィックスを除去
    for e in events:
        e["title"] = e["title"].lstrip("★").strip()

    logger.info(f"{SEIBU_SOURCE}: {len(events)} 件取得")
    return events


# ── 西原公園児童館 ─────────────────────────────────────────
NISHIHARA_URL    = "https://www.city.kumamoto.jp/kiji00322778/index.html"
NISHIHARA_SOURCE = "西原公園児童館"

def scrape_nishihara(pdf_bytes: bytes) -> list[dict]:
    """
    西原公園児童館のPDFを解析してイベントを返す。

    PDF構造（幸田・西部と異なる）:
        カレンダー形式ではなくリスト形式。
        TABLE[0]: 児童クラブ日程（乳幼児対象外）
        TABLE[1]: 朝の活動日程（日付・内容の2列）← メイン
        テキスト: 時刻・対象者情報
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page     = pdf.pages[0]
        tables   = page.extract_tables()
        text     = page.extract_text() or ""
        metadata = pdf.metadata or {}

    if not tables or len(tables) < 2:
        logger.warning(f"{NISHIHARA_SOURCE}: テーブルが不足しています")
        return []

    year, month = _get_year_month_from_pdf_text(text, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(
            metadata, text, datetime.now().year, datetime.now().month
        )
    logger.info(f"{NISHIHARA_SOURCE}: {year}年{month}月 解析開始")

    # 朝の活動時刻: テキストの「朝の活動」以降に最初に出てくる時刻
    text_z = _z2h(text)
    朝_pos = text_z.find('朝の活動')
    time_str = "10:00〜11:00"
    if 朝_pos >= 0:
        tm = re.search(r'(\d{1,2}:\d{2})[〜～](\d{1,2}:\d{2})', text_z[朝_pos:])
        if tm:
            time_str = f"{tm.group(1)}〜{tm.group(2)}"

    # TABLE[1] が朝の活動日程（2列: 日付, 内容）
    # TABLE[0] は児童クラブ（乳幼児対象外）なのでスキップ
    act_table = tables[1]

    events = []
    for row in act_table:
        if len(row) < 2:
            continue
        day_raw   = _normalize(row[0] or "")
        title_raw = _normalize(row[1] or "")

        # 注意書き・空行はスキップ
        if not day_raw or not title_raw:
            continue
        if title_raw.startswith('※') or _is_non_event(title_raw):
            continue

        # 日にち抽出: "18日" "1８日" → 18
        day_m = re.match(r'^(\d+)日?$', re.sub(r'日.*', '', day_raw).strip())
        if not day_m:
            continue
        day_num = int(day_m.group(1))

        try:
            ev_date = date(year, month, day_num)
        except ValueError:
            continue

        events.append({
            "title":       title_raw,
            "date":        ev_date.strftime("%Y-%m-%d"),
            "time":        time_str,
            "description": "",
            "source":      NISHIHARA_SOURCE,
            "url":         NISHIHARA_URL,
            "category":    _guess_category(title_raw),
        })

    logger.info(f"{NISHIHARA_SOURCE}: {len(events)} 件取得")
    return events


# ── 花園児童館 ─────────────────────────────────────────────
HANAZONO_URL        = "https://www.city.kumamoto.jp/kiji00319844/index.html"
HANAZONO_SOURCE     = "花園児童館"
# 毎月2枚のPDF: 表面(カレンダー)と裏面(詳細)
# URLパターン: 表面は末尾に _up_XXXX.pdf (ファイル番号が若い方)
# 運用上は pdf_front と pdf_back の2バイト列をまとめてスクレイパーに渡す

def _hanazono_build_wd_cols(header_row: list) -> list[tuple]:
    """
    花園児童館のカレンダーは21列で、曜日ヘッダーの1列左にデータが入る。
    例: ヘッダーが ['', '月', '', '', '火', ...] → 月のデータはci=0から
    → 各曜日のブロック = (ヘッダー位置-1) 〜 (次の曜日ヘッダー位置-1)
    """
    WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']
    wd_header_pos = [ci for ci, c in enumerate(header_row) if c and c.strip() in WEEKDAYS]
    wd_data_starts = [ci - 1 for ci in wd_header_pos]
    wd_cols = []
    for i, (wd, start) in enumerate(zip(WEEKDAYS, wd_data_starts)):
        end = wd_data_starts[i + 1] if i + 1 < len(wd_data_starts) else len(header_row)
        wd_cols.append((wd, start, end))
    return wd_cols


def _hanazono_parse_back(back_table: list[list], year: int) -> dict[tuple, dict]:
    """
    裏面テーブルをパースして {(month, day): detail_dict} を返す。
    「小学生対象」のみのセルはスキップ。
    """
    TITLE_RE = re.compile(r'^[「『](.+?)[」』]')
    detail = {}

    for row in back_table:
        for cell in row:
            text = _z2h(cell or "")
            title_m = TITLE_RE.search(text)
            if not title_m:
                continue
            title = title_m.group(1).strip()

            # 小学生専用はスキップ
            if '小学' in text and '乳幼児' not in text and '0歳' not in text:
                continue

            date_m = re.search(r'(\d+)月\s*(\d+)日', text)
            if not date_m:
                continue
            mo, day = int(date_m.group(1)), int(date_m.group(2))

            # 時刻: "10:30～11:15" 形式
            times = re.findall(r'(\d{1,2}:\d{2})[〜～](\d{1,2}:\d{2})', text)
            time_str = f"{times[0][0]}〜{times[0][1]}" if times else None

            # 対象
            tgt_m = re.search(r'【対象】(.+?)(?=【|$)', text, re.DOTALL)
            target = tgt_m.group(1).strip().replace('\n', ' ') if tgt_m else ""

            try:
                ev_date = date(year, mo, day)
            except ValueError:
                continue

            detail[(mo, day)] = {
                "title":       title,
                "date":        ev_date.strftime("%Y-%m-%d"),
                "time":        time_str,
                "description": target,
                "month":       mo,
            }
    return detail


def scrape_hanazono(pdf_front: bytes, pdf_back: bytes) -> list[dict]:
    """
    花園児童館の表面(カレンダー)＋裏面(詳細)PDFをパースしてイベントを返す。

    戦略:
      - 表面カレンダーで日付・イベント名を取得
      - 裏面詳細で時刻終了・対象を補完
      - 裏面のみのイベント（翌月分）も追加
      - 休館日・自由あそび・祝日開館案内はスキップ
    """
    # ── 表面 ──────────────────────────────────────────────
    with pdfplumber.open(io.BytesIO(pdf_front)) as pdf:
        page   = pdf.pages[0]
        tables = page.extract_tables()
        text   = page.extract_text() or ""
        meta   = pdf.metadata or {}

    year, month = _get_year_month_from_metadata(meta, text, datetime.now().year, datetime.now().month)
    logger.info(f"{HANAZONO_SOURCE}: {year}年{month}月 解析開始")

    cal_table = tables[0]
    wd_cols = _hanazono_build_wd_cols(cal_table[0])

    def get_wd_block(col):
        for wd, s, e in wd_cols:
            if s <= col < e:
                return wd, s, e
        return None, None, None

    def get_block(rows, s, e):
        parts = []
        for row in rows:
            for ci in range(s, min(e, len(row))):
                c = row[ci]
                if c and c.strip():
                    n = _normalize(c)
                    if n and n not in parts:
                        parts.append(n)
        return '\n'.join(parts)

    # 日付行を収集
    day_row_indices = []
    for ri, row in enumerate(cal_table):
        day_cells = [(ci, int(_z2h(c.strip()))) for ci, c in enumerate(row)
                     if c and re.match(r'^[０-９\d]+$', c.strip())]
        if len(day_cells) >= 3:
            day_row_indices.append((ri, day_cells))

    # ── 裏面 ──────────────────────────────────────────────
    with pdfplumber.open(io.BytesIO(pdf_back)) as pdf:
        back_tables = pdf.pages[0].extract_tables()

    back_detail = _hanazono_parse_back(back_tables[0], year)

    # ── カレンダー→イベント化 ──────────────────────────────
    SKIP_TITLES = re.compile(r'(天皇誕生日|建国記念|開館してます|祝日)')

    front_events = []
    seen_days = set()

    for idx, (ri, day_cells) in enumerate(day_row_indices):
        next_ri = day_row_indices[idx + 1][0] if idx + 1 < len(day_row_indices) else len(cal_table)
        content_rows = cal_table[ri + 1:next_ri]

        for day_ci, day_num in day_cells:
            wd, s, e = get_wd_block(day_ci)
            if wd is None:
                continue

            raw = get_block(content_rows, s, e)
            if not raw or _is_non_event(raw):
                continue

            # タイトル抽出
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            title_parts, desc_parts = [], []
            for l in lines:
                if re.search(r'\d{1,2}:\d{2}', _z2h(l)) or l.startswith(('（', '(', '【', '※')):
                    desc_parts.append(l)
                else:
                    title_parts.append(l)

            title = ' '.join(title_parts).strip()
            if not title:
                clean = re.sub(r'[（(][^）)]*[）)]', '', raw)
                clean = re.sub(r'\d{1,2}:\d{2}', '', _z2h(clean))
                title = re.sub(r'\s+', ' ', clean).strip()

            if _is_non_event(title) or SKIP_TITLES.search(title):
                continue

            # 裏面詳細で補完
            back = back_detail.get((month, day_num))
            final_title = back["title"] if back else title
            time_str    = (back["time"] if back and back["time"]
                           else _extract_time(raw) or "10:30〜")
            description = back["description"] if back else ""

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            front_events.append({
                "title":       final_title,
                "date":        ev_date.strftime("%Y-%m-%d"),
                "time":        time_str,
                "description": description,
                "source":      HANAZONO_SOURCE,
                "url":         HANAZONO_URL,
                "category":    _guess_category(final_title + description),
            })
            seen_days.add((month, day_num))

    # ── 裏面のみのイベント（翌月分など）を追加 ──────────────
    for (mo, day), d in back_detail.items():
        if (mo, day) in seen_days:
            continue
        front_events.append({
            "title":       d["title"],
            "date":        d["date"],
            "time":        d["time"] or "10:00〜",
            "description": d["description"],
            "source":      HANAZONO_SOURCE,
            "url":         HANAZONO_URL,
            "category":    _guess_category(d["title"] + d["description"]),
        })

    front_events.sort(key=lambda x: x["date"])
    logger.info(f"{HANAZONO_SOURCE}: {len(front_events)} 件取得")
    return front_events


# ── 託麻児童館 ─────────────────────────────────────────────
TAKUMA_URL    = "https://www.city.kumamoto.jp/kiji0031634/index.html"
TAKUMA_SOURCE = "託麻児童館"

def scrape_takuma(pdf_bytes: bytes) -> list[dict]:
    """
    託麻児童館のPDFを解析してイベントを返す。

    PDF構造:
        2列レイアウト（左右列が混在してテキスト抽出される）
        TABLE[3]: 日〜土の33列カレンダー
        テキスト: 2列混在のため page.crop() で左右に分割して詳細を抽出
        朝の活動(★印): 10:30〜固定
        詳細イベント: 左列・右列からイベント名ベースで抽出
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page     = pdf.pages[0]
        tables   = page.extract_tables()
        meta     = pdf.metadata or {}
        full_text = page.extract_text() or ""

        # 左右に分割してテキスト取得
        mid = page.width / 2
        left_text  = _z2h(page.crop((0, 0, mid, page.height)).extract_text() or "")
        right_text = _z2h(page.crop((mid, 0, page.width, page.height)).extract_text() or "")

    year, month = _get_year_month_from_pdf_text(full_text, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(meta, full_text, datetime.now().year, datetime.now().month)
    logger.info(f"{TAKUMA_SOURCE}: {year}年{month}月 解析開始")

    # ── 詳細ブロックをイベント名で抽出 ──────────────────────
    def _find_detail(text: str, keyword: str) -> dict | None:
        idx = text.find(keyword)
        if idx < 0:
            return None
        snippet = text[idx:idx + 300]
        dm = re.search(r'(\d+)月\s*(\d+)日', snippet)
        if not dm:
            return None
        mo, day = int(dm.group(1)), int(dm.group(2))
        tm = re.search(
            r'(\d{1,2})時\s*(\d{0,2})\s*分?[〜～]\s*(\d{0,2})\s*時?\s*(\d{0,2})\s*分?', snippet
        )
        if tm:
            h1, m1 = int(tm.group(1)), int(tm.group(2) or 0)
            h2, m2 = int(tm.group(3) or 0), int(tm.group(4) or 0)
            time_str = f"{h1:02d}:{m1:02d}〜{h2:02d}:{m2:02d}" if h2 else f"{h1:02d}:{m1:02d}〜"
        else:
            time_str = "10:30〜"
        tgt = re.search(r'[〈《]\s*対\s*象\s*[〉》]\s*(.+?)(?=[〈《]|$)', snippet, re.DOTALL)
        target = tgt.group(1).strip().replace('\n', ' ')[:50] if tgt else ""
        return {"month": mo, "day": day, "time": time_str, "target": target}

    def _find_trampoline(text: str) -> dict | None:
        """①②時間帯形式の親子トランポリン専用パーサー"""
        idx = text.find("親子トランポリン")
        if idx < 0:
            return None
        snippet = text[idx:idx + 300]
        dm = re.search(r'(\d+)月\s*(\d+)日', snippet)
        if not dm:
            return None
        mo, day = int(dm.group(1)), int(dm.group(2))
        tm = re.search(r'①\s*(\d{1,2})時[〜～](\d{1,2})時(\d{2})分', snippet)
        time_str = (f"{int(tm.group(1)):02d}:00〜{int(tm.group(2)):02d}:{tm.group(3)}"
                    if tm else "10:00〜")
        tgt = re.search(r'[〈《]\s*対\s*象\s*[〉》]\s*(.+?)(?=[〈《]|$)', snippet, re.DOTALL)
        target = tgt.group(1).strip().replace('\n', ' ')[:50] if tgt else ""
        return {"month": mo, "day": day, "time": time_str, "target": target}

    # 左列: 救急法指導, 親子バルーンアート
    # 右列: 親子トランポリン
    detail_map: dict[tuple, dict] = {}
    for keyword in ("救急法指導", "親子バルーンアート"):
        d = _find_detail(left_text, keyword)
        if d:
            detail_map[(d["month"], d["day"])] = d
    trampoline = _find_trampoline(right_text)
    if trampoline:
        detail_map[(trampoline["month"], trampoline["day"])] = trampoline

    # ── カレンダー解析 ──────────────────────────────────────
    # TABLE[3] が33列カレンダー
    cal_table = next(
        (t for t in tables if t and len(t[0]) >= 20),
        max(tables, key=lambda t: len(t) * len(t[0]) if t else 0)
    )

    WEEKDAYS_STR = ['日', '月', '火', '水', '木', '金', '土']
    wd_pos = [ci for ci, c in enumerate(cal_table[0]) if c and c.strip() in WEEKDAYS_STR]
    wd_cols = []
    for i, (wd, start) in enumerate(zip(WEEKDAYS_STR, wd_pos)):
        end = wd_pos[i + 1] if i + 1 < len(wd_pos) else len(cal_table[0])
        wd_cols.append((wd, start, end))

    def get_wd(col):
        for wd, s, e in wd_cols:
            if s <= col < e:
                return wd, s, e
        return None, None, None

    def get_block(rows, s, e):
        parts = []
        for row in rows:
            for ci in range(s, min(e, len(row))):
                c = row[ci]
                if c and c.strip():
                    n = _normalize(c)
                    if n and n not in parts:
                        parts.append(n)
        return '\n'.join(parts)

    SKIP_RE = re.compile(r'(臨時休館|休館|自由遊び|製作セットとは|春分の日|祝日開館|まちづくりセンター)')

    day_row_idx = []
    for ri, row in enumerate(cal_table):
        days = [(ci, int(_z2h(c.strip()))) for ci, c in enumerate(row)
                if c and re.match(r'^[０-９\d]+$', c.strip())]
        if len(days) >= 3:
            day_row_idx.append((ri, days))

    events = []
    for idx, (ri, day_cells) in enumerate(day_row_idx):
        next_ri = day_row_idx[idx + 1][0] if idx + 1 < len(day_row_idx) else len(cal_table)
        content_rows = cal_table[ri + 1:next_ri]

        for day_ci, day_num in day_cells:
            wd, s, e = get_wd(day_ci)
            if wd is None:
                continue

            raw = get_block(content_rows, s, e)
            if not raw:
                continue

            # 各行から★・午前予約制活動 除去、スキップ対象を除いた行を収集
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            clean_lines = []
            for l in lines:
                l2 = l.replace('★', '').replace('午前予約制活動', '').strip()
                if l2 and not SKIP_RE.search(l2):
                    clean_lines.append(l2)

            if not clean_lines:
                continue

            title = clean_lines[0]

            # 詳細補完
            detail = detail_map.get((month, day_num))
            time_str  = detail["time"]   if detail else "10:30〜"
            target    = detail["target"] if detail else ""

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            events.append({
                "title":       title,
                "date":        ev_date.strftime("%Y-%m-%d"),
                "time":        time_str,
                "description": target,
                "source":      TAKUMA_SOURCE,
                "url":         TAKUMA_URL,
                "category":    _guess_category(title + target),
            })

    events.sort(key=lambda x: x["date"])

    # ── カレンダーに載っていない詳細イベントも追加 ──────────────
    # （例: 親子バルーンアートは土曜「自由遊び」欄に埋もれて別掲）
    cal_days = {int(e["date"].split("-")[2]) for e in events}
    for (mo, day), d in detail_map.items():
        if mo != month or day in cal_days:
            continue
        # イベント名をテキストから取得（詳細ブロックの直前行）
        title = "詳細イベント"
        for col_text in (left_text, right_text):
            dm = re.search(r'(\d+)月\s*(\d+)日', _z2h(col_text))
            # キーワード直前行を探す
            for kw in ("親子バルーンアート", "救急法指導", "親子トランポリン"):
                idx = col_text.find(kw)
                if idx >= 0 and re.search(rf'{mo}月\s*{day}日', _z2h(col_text[idx:idx+100])):
                    title = kw
                    break
        try:
            ev_date = date(year, mo, day)
        except ValueError:
            continue
        events.append({
            "title":       title,
            "date":        ev_date.strftime("%Y-%m-%d"),
            "time":        d["time"],
            "description": d["target"],
            "source":      TAKUMA_SOURCE,
            "url":         TAKUMA_URL,
            "category":    _guess_category(title + d["target"]),
        })

    events.sort(key=lambda x: x["date"])
    logger.info(f"{TAKUMA_SOURCE}: {len(events)} 件取得")
    return events


# ── 秋津児童館 ─────────────────────────────────────────────
AKITSU_URL    = "https://www.city.kumamoto.jp/kiji00311960/index.html"
AKITSU_SOURCE = "秋津児童館"

def scrape_akitsu(pdf_bytes: bytes) -> list[dict]:
    """
    秋津児童館のPDFを解析してイベントを返す。

    PDF構造:
        TABLE[0]: 日〜土の21列カレンダー (花園と同形式)
        ヘッダー位置-1がデータ開始列
        セル内に改行区切りで複数行あり → _normalize前に行分割が必要
        テキスト: 2列混在 → 右列に詳細情報（朝の活動時刻:10:45〜）

    注意点:
        - 17日(親子ふれあい遊び) はカレンダーセルが
          '～事前申込制～\n親子ふれあい遊び\n〈下記参照〉' → 行分割後に「下記参照」を除去
        - 20日(合同お誕生会) はROW6-9に分散 → 全内容行を走査して収集
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page   = pdf.pages[0]
        tables = page.extract_tables()
        text   = page.extract_text() or ""
        meta   = pdf.metadata or {}

    year, month = _get_year_month_from_pdf_text(text, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(meta, text, datetime.now().year, datetime.now().month)
    logger.info(f"{AKITSU_SOURCE}: {year}年{month}月 解析開始")

    cal_table = tables[0]

    # 曜日ブロック (花園と同じ: ヘッダー位置-1)
    WEEKDAYS_STR = ['日', '月', '火', '水', '木', '金', '土']
    wd_header_pos = [ci for ci, c in enumerate(cal_table[0]) if c and c.strip() in WEEKDAYS_STR]
    wd_data_starts = [ci - 1 for ci in wd_header_pos]
    wd_cols = []
    for i, (wd, start) in enumerate(zip(WEEKDAYS_STR, wd_data_starts)):
        end = wd_data_starts[i + 1] if i + 1 < len(wd_data_starts) else len(cal_table[0])
        wd_cols.append((wd, start, end))

    def get_wd(col):
        for wd, s, e in wd_cols:
            if s <= col < e:
                return wd, s, e
        return None, None, None

    def get_cell_lines(rows: list, s: int, e: int) -> list[str]:
        """
        ブロック内の全セルを改行分割して行リストで返す。
        _normalize ではなくセル単位での行分割を行う（混在防止）。
        """
        lines = []
        for row in rows:
            for ci in range(s, min(e, len(row))):
                c = row[ci]
                if not c or not c.strip():
                    continue
                for l in c.splitlines():
                    l = l.strip()
                    if l and l not in lines:
                        lines.append(l)
        return lines

    SKIP_LINE_RE = re.compile(
        r'(休館日?|自由あそび|天皇誕生日|建国記念|開館します|下記参照|事前申込制)'
    )
    # 「事前申込制」は単独行ならスキップ、タイトルの一部なら残す
    SKIP_PREFIX_RE = re.compile(r'^[〜～].+[〜～]$')  # "～事前申込制～" 形式

    # テキストから詳細情報（朝の活動時刻）を取得
    text_z = _z2h(text)
    # "朝の活動" の時刻
    asa_time = "10:45〜"
    m = re.search(r'朝の活動.*?(\d{1,2})\s*時\s*(\d{0,2})\s*分?[〜～]', text_z, re.DOTALL)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        asa_time = f"{h:02d}:{mi:02d}〜"

    # テキストから各イベント詳細
    def get_time_for_day(day_num: int, title: str) -> str:
        """イベント名・日にちから適切な時刻を返す"""
        if '朝の活動' in title or '身体測定' in title or 'ひな祭り' in title or 'じゃがいも' in title:
            return asa_time
        if '誕生会' in title:
            m = re.search(r'(\d{1,2})月\s*(\d{1,2})日.{0,10}(\d{1,2})\s*時\s*(\d{0,2})\s*分',
                          text_z[text_z.find('誕生会'):text_z.find('誕生会') + 100])
            if m:
                h, mi = int(m.group(3)), int(m.group(4) or 0)
                return f"{h:02d}:{mi:02d}〜"
            return "10:30〜"
        if 'ふれあい' in title:
            return "10:00〜"
        return "10:30〜"

    # 日付行を収集
    day_row_idx = []
    for ri, row in enumerate(cal_table):
        days = [(ci, int(_z2h(c.strip()))) for ci, c in enumerate(row)
                if c and re.match(r'^[０-９\d]+$', c.strip())]
        if len(days) >= 3:
            day_row_idx.append((ri, days))

    events = []
    for idx, (ri, day_cells) in enumerate(day_row_idx):
        next_ri = day_row_idx[idx + 1][0] if idx + 1 < len(day_row_idx) else len(cal_table)
        content_rows = cal_table[ri + 1:next_ri]

        for day_ci, day_num in day_cells:
            wd, s, e = get_wd(day_ci)
            if wd is None:
                continue

            raw_lines = get_cell_lines(content_rows, s, e)
            if not raw_lines:
                continue

            # スキップ行を除去してタイトルを構築
            clean = []
            for l in raw_lines:
                if SKIP_LINE_RE.search(l) or SKIP_PREFIX_RE.match(l):
                    continue
                clean.append(l)

            if not clean:
                continue

            title = ' '.join(clean)

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            time_str = get_time_for_day(day_num, title)

            events.append({
                "title":       title,
                "date":        ev_date.strftime("%Y-%m-%d"),
                "time":        time_str,
                "description": "",
                "source":      AKITSU_SOURCE,
                "url":         AKITSU_URL,
                "category":    _guess_category(title),
            })

    events.sort(key=lambda x: x["date"])
    logger.info(f"{AKITSU_SOURCE}: {len(events)} 件取得")
    return events


# ── 五福児童室 ─────────────────────────────────────────────
GOFUKU_URL    = "https://www.city.kumamoto.jp/kiji00003568/index.html"
GOFUKU_SOURCE = "五福児童室"

def scrape_gofuku(pdf_bytes: bytes, manual_json_path: str | None = None) -> list[dict]:
    """
    五福児童室のPDFを解析してイベントを返す。

    五福児童室はスキャンPDF（Canon複合機でスキャン）のため自動抽出不可。
    manual_json_path が指定されている場合はそちらを読み込んで返す（手動メンテ方式）。
    指定がない場合は空リストを返す。

    手動メンテ方式の運用:
        1. 毎月初に市HPで新しいPDFを確認・ダウンロード
        2. カレンダーとイベント詳細を見てgofuku_events.jsonを更新
        3. スクリプト実行時に manual_json_path を渡す
    """
    # 手動JSONが指定されていればそれを返す
    if manual_json_path:
        import pathlib
        p = pathlib.Path(manual_json_path)
        if p.exists():
            import json as _json
            events = _json.loads(p.read_text(encoding="utf-8"))
            logger.info(f"{GOFUKU_SOURCE}: 手動JSON読み込み {len(events)} 件")
            return events
        else:
            logger.warning(f"{GOFUKU_SOURCE}: 手動JSONが見つかりません: {manual_json_path}")

    # スキャンPDFからの自動抽出を試みる（ほぼ失敗する）
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        meta = pdf.metadata or {}

    year, month = _get_year_month_from_pdf_text(text, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(
            meta, text, datetime.now().year, datetime.now().month
        )
    logger.warning(f"{GOFUKU_SOURCE}: スキャンPDFのため自動抽出不可。0件を返します（手動JSONを用意してください）。")
    return []


# ── 天明児童室 ─────────────────────────────────────────────
TENMEI_URL    = "https://www.city.kumamoto.jp/kiji00003855/index.html"
TENMEI_SOURCE = "天明児童室"

def scrape_tenmei(pdf_bytes: bytes) -> list[dict]:
    """
    天明児童室のPDFを解析してイベントを返す。

    PDF構造:
        TABLE[1]: 月〜日の7列カレンダー
            日付行と内容行が交互（ROW1=日付,ROW2=内容,ROW3=日付...）
        テキスト: 2列レイアウト → 右列に申込制イベントの詳細あり
        年月: "令和N年度\nM月" 形式 → _get_year_month_from_pdf_text が対応済み

    時刻: "午前N時M分〜午前N時M分" 形式をパース
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page   = pdf.pages[0]
        tables = page.extract_tables()
        text   = page.extract_text() or ""
        meta   = pdf.metadata or {}
        mid    = page.width / 2
        right_text = _z2h(page.crop((mid, 0, page.width, page.height)).extract_text() or "")

    year, month = _get_year_month_from_pdf_text(text, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(meta, text, datetime.now().year, datetime.now().month)
    logger.info(f"{TENMEI_SOURCE}: {year}年{month}月 解析開始")

    cal = tables[1]  # TABLE[1] が7列カレンダー

    # ── 右列テキストから詳細情報を収集 ────────────────────────
    # "午前N時M分～午前N時M分" → "HH:MM〜HH:MM"
    KANJI_TIME_RE = re.compile(
        r'午前\s*(\d{1,2})\s*時\s*(\d{0,2})\s*分?\s*[〜～]\s*午前\s*(\d{1,2})\s*時\s*(\d{0,2})\s*分?'
    )

    def find_detail(keyword: str) -> dict | None:
        idx = right_text.find(keyword)
        if idx < 0:
            return None
        snippet = right_text[idx:idx + 300]
        dm = re.search(r'(\d+)月\s*(\d+)日', snippet)
        if not dm:
            return None
        mo, day = int(dm.group(1)), int(dm.group(2))
        tm = KANJI_TIME_RE.search(snippet)
        if tm:
            h1, m1 = int(tm.group(1)), int(tm.group(2) or 0)
            h2, m2 = int(tm.group(3)), int(tm.group(4) or 0)
            time_str = f"{h1:02d}:{m1:02d}〜{h2:02d}:{m2:02d}"
        else:
            time_str = "10:30〜"
        tgt = re.search(r'【対\s*象】\s*(.+?)(?=【|$)', snippet, re.DOTALL)
        target = tgt.group(1).strip().replace('\n', ' ')[:40] if tgt else ""
        return {"month": mo, "day": day, "time": time_str, "target": target}

    detail_map: dict[int, dict] = {}
    for kw in ("まめまき", "親子でふれあい体操"):
        d = find_detail(kw)
        if d:
            detail_map[d["day"]] = d

    # ── カレンダーパース ─────────────────────────────────────
    SKIP_RE  = re.compile(r'(休室日|自由あそび|祝日開室日|マークは朝)')
    CLEAN_RE = re.compile(r'(★|（事前申込）|（当日受付）|\d{1,2}[：:]\d{2}[〜～]?|[１1][０0][：:][３3][０0][〜～]?)')

    events = []
    i = 1
    while i < len(cal):
        date_row    = cal[i]
        content_row = cal[i + 1] if i + 1 < len(cal) else []
        i += 2

        for ci, cell in enumerate(date_row):
            if not cell or not re.match(r'^[０-９\d]+$', cell.strip()):
                continue
            day_num = int(_z2h(cell.strip()))

            raw = _normalize(content_row[ci] or "") if ci < len(content_row) else ""
            if not raw or SKIP_RE.search(raw):
                continue

            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            clean = [CLEAN_RE.sub('', l).strip() for l in lines if not SKIP_RE.search(l)]
            clean = [l for l in clean if l]
            if not clean:
                continue

            title = ' '.join(clean)

            # 詳細補完
            detail = detail_map.get(day_num)
            time_str  = detail["time"]   if detail else (_extract_time(raw) or "10:30〜")
            target    = detail["target"] if detail else ""

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            events.append({
                "title":       title,
                "date":        ev_date.strftime("%Y-%m-%d"),
                "time":        time_str,
                "description": target,
                "source":      TENMEI_SOURCE,
                "url":         TENMEI_URL,
                "category":    _guess_category(title),
            })

    events.sort(key=lambda x: x["date"])
    logger.info(f"{TENMEI_SOURCE}: {len(events)} 件取得")
    return events


# ── 大江児童室 ─────────────────────────────────────────────
OOE_URL    = "https://www.city.kumamoto.jp/kiji00065744/index.html"
OOE_SOURCE = "大江児童室"

def scrape_ooe(pdf_bytes: bytes) -> list[dict]:
    """
    大江公民館・児童室のPDFを解析してイベントを返す。

    PDF構造:
        カレンダー形式ではなく、イベントごとに「日 時/場 所/対 象」形式のブロックが
        2列レイアウトで記載されている。
        テーブル抽出不可 → page.crop() で左右列に分割してテキスト抽出。

    対象イベント（児童室からのお知らせ）のみ抽出:
        - わらべ唄とおはなし会（乳幼児向け）
        - よちよち★たいむ（読み聞かせ）
        - はっぴぃたいむ系（次月分も含む）

    時刻: "午前N時半" → "HH:30〜" に変換
    年月: "令和8年(2026年)2月" 形式から抽出
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
        text = page.extract_text() or ""
        meta = pdf.metadata or {}
        mid  = page.width / 2
        left_text  = _z2h(page.crop((0, 0, mid, page.height)).extract_text() or "")
        right_text = _z2h(page.crop((mid, 0, page.width, page.height)).extract_text() or "")

    # 年月: "(2026年)2月" 形式を優先
    t_all = _z2h(text)
    m_yr = re.search(r'(20\d{2})年.*?(\d{1,2})月', t_all)
    if m_yr:
        year, month = int(m_yr.group(1)), int(m_yr.group(2))
    else:
        year, month = _get_year_month_from_metadata(meta, text, datetime.now().year, datetime.now().month)
    logger.info(f"{OOE_SOURCE}: {year}年{month}月 解析開始")

    # ── 時刻パース（時半対応） ──────────────────────────────
    def parse_time(snippet: str) -> str:
        t = snippet
        # "午前N時半"
        m = re.search(r'午前\s*(\d{1,2})\s*時半', t)
        if m:
            return f"{int(m.group(1)):02d}:30〜"
        # "午前N時M分"
        m = re.search(r'午前\s*(\d{1,2})\s*時\s*(\d{0,2})\s*分?', t)
        if m:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            return f"{h:02d}:{mi:02d}〜"
        return "10:00〜"

    def parse_block(snippet: str) -> dict | None:
        dm = re.search(r'(\d+)月\s*(\d+)日', snippet)
        if not dm:
            return None
        mo, day = int(dm.group(1)), int(dm.group(2))
        time_str = parse_time(snippet)
        tgt = re.search(r'対\s*象\s*(.+?)(?=定\s*員|受\s*付|$)', snippet, re.DOTALL)
        target = tgt.group(1).strip().replace('\n', ' ')[:30] if tgt else "乳幼児と保護者"
        try:
            ev_date = date(year, mo, day)
        except ValueError:
            return None
        return {"date": ev_date.strftime("%Y-%m-%d"), "time": time_str, "target": target}

    events = []
    seen_dates: set[tuple] = set()

    def make_col_lines(words_list, x_min: float, x_max: float, y_round: int = 8):
        """x座標でフィルタしてy座標順の行リストを作成"""
        from collections import defaultdict
        by_y: dict = defaultdict(list)
        for w in words_list:
            if x_min <= w['x0'] < x_max:
                y = round(w['top'] / y_round) * y_round
                by_y[y].append((w['x0'], _z2h(w['text'])))
        result = []
        for y in sorted(by_y):
            line = ' '.join(t for _, t in sorted(by_y[y]))
            result.append(line)
        return result

    DATE_MARKER = re.compile(r'^日\s*時\s*(\d+)月\s*(\d+)日')

    for col_lines in (
        make_col_lines(words, 0, page.width * 0.5),
        make_col_lines(words, page.width * 0.5, page.width),
    ):
        i = 0
        while i < len(col_lines):
            dm = DATE_MARKER.match(col_lines[i].strip())
            if dm:
                mo, day = int(dm.group(1)), int(dm.group(2))
                if mo in (month, month % 12 + 1) and (mo, day) not in seen_dates:
                    snippet = '\n'.join(col_lines[i:i + 10])

                    # 成人向け除外
                    if re.search(r'(どなたでも|Android|スマホ|600円)', snippet):
                        i += 1
                        continue
                    # 乳幼児対象か確認
                    if not re.search(r'(乳幼児|0歳|1歳|2歳|赤ちゃん)', snippet):
                        i += 1
                        continue

                    time_str = parse_time(snippet)

                    # タイトル推定（識別キーワードで分類・優先順位順）
                    if re.search(r'(熊日童話|大ホール|20組)', snippet):
                        title = "はっぴぃたいむ ひなまつりおはなし会"
                    elif re.search(r'7組（先着順）', snippet):
                        # わらべ唄は7組・和茶室・受付が必要な申込制
                        title = "わらべ唄とおはなし会"
                    elif re.search(r'(まど|0歳児|各9組)', snippet):
                        title = "よちよち★たいむ"
                    else:
                        title = f"大江児童室 活動（{mo}月{day}日）"

                    try:
                        ev_date = date(year, mo, day)
                    except ValueError:
                        i += 1
                        continue

                    seen_dates.add((mo, day))
                    events.append({
                        "title":       title,
                        "date":        ev_date.strftime("%Y-%m-%d"),
                        "time":        time_str,
                        "description": "乳幼児と保護者",
                        "source":      OOE_SOURCE,
                        "url":         OOE_URL,
                        "category":    _guess_category(title),
                    })
            i += 1

    events.sort(key=lambda x: x["date"])
    logger.info(f"{OOE_SOURCE}: {len(events)} 件取得")
    return events


# ── 城南児童館 ─────────────────────────────────────────────
JONAN_URL    = "https://share.google/ZQwwGHym5zYntsZ0x"  # 児童館だよりページ
JONAN_SOURCE = "城南児童館"

# 乳幼児向けキーワード（これに合致するもののみ抽出）
_JONAN_INFANT_RE = re.compile(
    r'(身体測定|豆まき|はじめの一歩|朝の活動|マザーズヨガ|わくわく|あかちゃん|'
    r'おはなしかい|季節の制作|ひなまつり|ピラティス|ベビーアロマ|English|'
    r'育児講座|ふれあいサロン|骨盤体操|こども発達|つくってあそぼ|おゆずりマルシェ)'
)
# 乳幼児向け除外キーワード（小学生専用・成人専用・施設案内）
_JONAN_SKIP_RE = re.compile(
    r'(書き方教室|Let\'s Dance|キッズ体操|ボードゲーム|スイーツクッキング|'
    r'おもちゃ病院|はるまつり|インスタグラム|乳幼児おすすめ|地域子育てクラブピカピカイベント)'
)


def scrape_jonan(pdf_bytes: bytes) -> list[dict]:
    """
    城南児童館のPDFを解析してイベントを返す。

    PDF構造:
        TABLE[0]: 日〜土の7列カレンダー
            日付と内容が同一セルに格納（"2\\n身体測定...\\n11:00〜" 形式）
            1セルに複数イベントが混在する場合あり
        P2: 裏面に詳細（予約制・当日先着の区分等）

    年月: "令和8年2月号" → _get_year_month_from_pdf_text で取得
    乳幼児向けのみ抽出（小学生・成人向けは除外）
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page0  = pdf.pages[0]
        tables = page0.extract_tables()
        text0  = page0.extract_text() or ""
        meta   = pdf.metadata or {}

    year, month = _get_year_month_from_pdf_text(text0, 0, 0)
    if not year:
        year, month = _get_year_month_from_metadata(meta, text0, datetime.now().year, datetime.now().month)
    logger.info(f"{JONAN_SOURCE}: {year}年{month}月 解析開始")

    cal = tables[0]  # 7列カレンダー

    events = []

    for ri, row in enumerate(cal):
        if ri <= 1 or ri == len(cal) - 1:
            continue  # ヘッダー・説明行・告知行スキップ

        for ci, cell in enumerate(row):
            if not cell or not cell.strip():
                continue

            cell_z = _z2h(cell.strip())
            lines  = [l.strip() for l in cell_z.splitlines() if l.strip()]
            if not lines:
                continue

            # 最初の行が日付数字か確認
            day_m = re.match(r'^(\d+)$', lines[0])
            if not day_m:
                continue
            day_num       = int(day_m.group(1))
            content_lines = lines[1:]
            if not content_lines:
                continue

            # セル内のイベントを「タイトル行 → 時刻行」単位に分割
            # 時刻行: "HH:MM〜HH:MM" or "HH:MM〜HH:MM\n（予約先）"
            TIME_RE = re.compile(r'^(\d{1,2}:\d{2})[〜～](\d{1,2}:\d{2})')
            sub_events: list[tuple[str, str]] = []
            cur_title: list[str] = []

            for l in content_lines:
                tm = TIME_RE.match(l)
                if tm:
                    time_str = f"{tm.group(1)}〜{tm.group(2)}"
                    if cur_title:
                        sub_events.append((' '.join(cur_title), time_str))
                        cur_title = []
                else:
                    cur_title.append(l)
            if cur_title:
                sub_events.append((' '.join(cur_title), "11:00〜"))

            for title, time_str in sub_events:
                # 除外判定
                if _JONAN_SKIP_RE.search(title):
                    continue
                # 乳幼児向けでなければスキップ
                if not _JONAN_INFANT_RE.search(title):
                    continue

                try:
                    ev_date = date(year, month, day_num)
                except ValueError:
                    continue

                events.append({
                    "title":       title,
                    "date":        ev_date.strftime("%Y-%m-%d"),
                    "time":        time_str,
                    "description": "",
                    "source":      JONAN_SOURCE,
                    "url":         JONAN_URL,
                    "category":    _guess_category(title),
                })

    events.sort(key=lambda x: x["date"])
    logger.info(f"{JONAN_SOURCE}: {len(events)} 件取得")
    return events
# ════════════════════════════════════════════════════════

# 各施設の設定: (source名, URL, scraper関数)
# 今後施設が増えるたびに HALL_CONFIGS に追加するだけでOK
HALL_CONFIGS = [
    {
        "source":  KODA_SOURCE,
        "url":     KODA_URL,
        "scraper": scrape_koda,
        "pdf_url": None,
    },
    {
        "source":  SEIBU_SOURCE,
        "url":     SEIBU_URL,
        "scraper": scrape_seibu,
        "pdf_url": None,
    },
    {
        "source":  NISHIHARA_SOURCE,
        "url":     NISHIHARA_URL,
        "scraper": scrape_nishihara,
        "pdf_url": None,
    },
    # 花園児童館は2枚PDF構成のため scrape_all_halls での自動実行に対応しない
    # scrape_hanazono(pdf_front, pdf_back) を直接呼び出すこと
    {
        "source":  TAKUMA_SOURCE,
        "url":     TAKUMA_URL,
        "scraper": scrape_takuma,
        "pdf_url": None,
    },
    {
        "source":  AKITSU_SOURCE,
        "url":     AKITSU_URL,
        "scraper": scrape_akitsu,
        "pdf_url": None,
    },
    {
        "source":  GOFUKU_SOURCE,
        "url":     GOFUKU_URL,
        "scraper": scrape_gofuku,
        "pdf_url": None,
    },
    {
        "source":  TENMEI_SOURCE,
        "url":     TENMEI_URL,
        "scraper": scrape_tenmei,
        "pdf_url": None,
    },
    {
        "source":  OOE_SOURCE,
        "url":     OOE_URL,
        "scraper": scrape_ooe,
        "pdf_url": None,
    },
    {
        "source":  JONAN_SOURCE,
        "url":     JONAN_URL,
        "scraper": scrape_jonan,
        "pdf_url": None,
    },
]


def scrape_all_halls(pdf_map: dict[str, bytes] | None = None) -> list[dict]:
    """
    全施設のイベントを取得して返す。

    Args:
        pdf_map: {source名: PDFバイト列} の辞書。
                 手動アップロード時に渡す。
                 None の場合は pdf_url から自動取得を試みる。

    Returns:
        scraper.py の events リストに extend できる形式のリスト。
    """
    all_events = []
    pdf_map = pdf_map or {}

    for cfg in HALL_CONFIGS:
        source  = cfg["source"]
        scraper = cfg["scraper"]
        pdf_url = cfg.get("pdf_url")

        # PDFバイト列を取得
        if source in pdf_map:
            pdf_bytes = pdf_map[source]
        elif pdf_url:
            logger.info(f"{source}: PDF取得中 {pdf_url}")
            pdf_bytes = _fetch_pdf_bytes(pdf_url)
        else:
            logger.debug(f"{source}: PDFが未設定のためスキップ")
            continue

        if not pdf_bytes:
            continue

        try:
            events = scraper(pdf_bytes)
            all_events.extend(events)
        except Exception as e:
            logger.error(f"{source}: 解析エラー {e}", exc_info=True)

    return all_events


# ════════════════════════════════════════════════════════
# CLI テスト
# ════════════════════════════════════════════════════════


# ── ソースD アダプター ────────────────────────────────────
def _hall_event_to_common(ev: dict) -> dict:
    """
    児童館スクレイパーが返す形式を scraper.py の共通形式に変換する。
    児童館形式: date, time, source, url, category, title, description
    共通形式:   date_iso, date_raw, time_raw, ...
    """
    title = ev.get("title", "")
    desc  = ev.get("description", "")
    needs_res = title.startswith("★") or "申込" in desc or "予約" in desc
    display_title = title if title.startswith("★") else ("★" + title if needs_res else title)
    return {
        "title":           display_title,
        "date_raw":        ev.get("date", ""),
        "date_iso":        ev.get("date", ""),
        "time_raw":        ev.get("time", ""),
        "location":        ev.get("source", ""),
        "apply_info":      desc[:100] if desc else "",
        "category":        ev.get("category", "その他"),
        "target_age":      "指定なし",
        "url":             ev.get("url", ""),
        "source":          ev.get("source", ""),
        "needs_reservation": needs_res,
        "body_preview":    "",
    }


def scrape_all_halls_adapted() -> list[dict]:
    """児童館イベントを共通形式で返す"""
    print("\n=== ソースD: 各児童館（PDF解析）===")
    raw = scrape_all_halls()
    adapted = [_hall_event_to_common(e) for e in raw]
    print(f"  ソースD 合計: {len(adapted)} 件")
    return adapted


# ─────────────────────────────────────────
# 共通マップ
# ─────────────────────────────────────────
CATEGORY_MAP = {
    "離乳食": "食育・栄養", "食育": "食育・栄養", "栄養": "食育・栄養",
    "健康": "健康・医療", "歯": "健康・医療", "医療": "健康・医療",
    "発達": "発達・育児相談", "相談": "発達・育児相談", "育児": "発達・育児相談",
    "パパ": "父親・家族支援",
    "ふれあい": "親子ふれあい", "遊び": "親子ふれあい", "リトミック": "親子ふれあい",
    "マッサージ": "親子ふれあい", "絵本": "親子ふれあい", "体操": "親子ふれあい",
    "おはなし": "親子ふれあい", "ハイハイ": "親子ふれあい", "ベビーサイン": "親子ふれあい",
    "ひとり親": "ひとり親支援",
    "産前": "産前・産後", "産後": "産前・産後", "骨盤": "産前・産後",
    "ヨガ": "産前・産後", "ピラティス": "産前・産後", "マタニティ": "産前・産後",
    "お金": "生活支援", "メイク": "生活支援", "カラー": "生活支援",
}
AGE_MAP = {
    "妊婦": "妊娠中", "妊娠中": "妊娠中",
    "産後": "0歳", "ベビー": "0歳", "0歳": "0歳", "乳児": "0歳",
    "赤ちゃん": "0歳", "ハーフバースデー": "0歳", "ハイハイ": "0歳",
    "1歳": "1〜2歳", "2歳": "1〜2歳", "１歳": "1〜2歳", "２歳": "1〜2歳",
    "3歳": "3〜5歳", "4歳": "3〜5歳", "5歳": "3〜5歳",
    "未就学": "3〜5歳", "幼児": "3〜5歳",
    "小学": "小学生以上", "乳幼児": "0歳〜未就学",
}


def guess_category(t):
    for k, v in CATEGORY_MAP.items():
        if k in t:
            return v
    return "その他"


def guess_age(t):
    for k, v in AGE_MAP.items():
        if k in t:
            return v
    return "指定なし"


# ─────────────────────────────────────────
# 共通ユーティリティ
# ─────────────────────────────────────────
def normalize_date(text, base_year=None, base_month=None):
    """各種日付表記を YYYY-MM-DD に変換"""
    if not text:
        return ""
    text = text.strip()
    now = datetime.now()
    year = base_year or now.year

    # 令和
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        y = 2018 + int(m.group(1))
        return f"{y}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    # 西暦フル
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    # 月日のみ
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        y = year
        if base_month and mo < base_month:
            y += 1
        return f"{y}-{mo:02d}-{dy:02d}"

    return ""


def normalize_time(text):
    """時刻表記を HH:MM〜HH:MM 形式に正規化"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text.strip())

    def to_24h(ampm, h, m_str):
        h = int(h)
        m = int(m_str) if m_str else 0
        if ampm == "午後" and h != 12:
            h += 12
        if ampm == "午前" and h == 12:
            h = 0
        return f"{h:02d}:{m:02d}"

    def parse_one(s):
        if not s:
            return ""
        if "正午" in s:
            return "12:00"
        s = s.replace("：", ":")
        m = re.search(r"(午前|午後)(\d{1,2})時(?:(\d{1,2})分)?", s)
        if m:
            return to_24h(m.group(1), m.group(2), m.group(3))
        m = re.search(r"(\d{1,2})時(?:(\d{1,2})分)?", s)
        if m:
            return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"
        m = re.search(r"(\d{1,2}):(\d{2})", s)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
        return ""

    parts = re.split(r"[〜～]|から|より", text, maxsplit=1)
    start = parse_one(parts[0])
    end_text = re.sub(r"まで.*", "", parts[1]) if len(parts) > 1 else ""
    end = parse_one(end_text)

    if start and end:
        return f"{start}〜{end}"
    return start


def is_reservation_required(text):
    """★予約必要判定"""
    if not text:
        return False
    if any(kw in text for kw in ["予約不要", "申込不要", "当日申込可", "当日先着"]):
        return False
    if re.search(r"https?://", text):
        return True
    if any(kw in text for kw in ["事前申込", "要申込", "電話申込", "申込み", "申し込み"]):
        return True
    return False


def make_event(title, date_raw, date_iso, time_raw, location, apply_info,
               category, target_age, url, source, needs_reservation=False):
    display_title = f"★{title}" if needs_reservation else title
    return {
        "title": display_title,
        "date_raw": date_raw,
        "date_iso": date_iso,
        "time_raw": time_raw,
        "location": location,
        "apply_info": apply_info,
        "category": category,
        "target_age": target_age,
        "url": url,
        "source": source,
        "needs_reservation": needs_reservation,
        "body_preview": "",
    }


def fetch_html(url, timeout=15):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "ja,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        print(f"  fetch失敗: {url} -> {e}")
        return ""


# ─────────────────────────────────────────
# Playwright共通: JSレンダリング後のHTMLを取得
# ─────────────────────────────────────────
def fetch_html_playwright(pw_page, url, wait_text=None, timeout=20000):
    """Playwrightでページを開きJS描画後のHTMLを返す"""
    print(f"  GET(PW) {url}")
    pw_page.goto(url, wait_until="networkidle", timeout=30000)
    if wait_text:
        try:
            pw_page.wait_for_function(
                f"document.body.innerText.includes('{wait_text}')",
                timeout=timeout,
            )
        except Exception:
            print(f"  ⚠️ 待機テキスト「{wait_text}」が見つかりません（続行）")
    pw_page.wait_for_timeout(1500)
    return pw_page.content()


# ─────────────────────────────────────────
# ソースA: 子育てナビ（Playwright）
# ─────────────────────────────────────────
def to_iso(s):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}" if m else ""


def find_kidate(a_tag):
    parent = a_tag.parent
    if parent:
        parent_text = parent.get_text(" ", strip=True)
        m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", parent_text)
        if m:
            return m.group(1)
        nxt = parent.find_next_sibling()
        for _ in range(3):
            if nxt is None:
                break
            text = nxt.get_text(" ", strip=True)
            m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
            if m:
                return m.group(1)
            nxt = nxt.find_next_sibling()
    return ""


def parse_kosodate_html(html):
    soup = BeautifulSoup(html, "html.parser")
    all_a = soup.find_all("a", href=re.compile(r"page\d+\.html"))
    print(f"  page*.html aタグ数: {len(all_a)}")
    seen_urls = set()
    events = []
    for a in all_a:
        url = a.get("href", "")
        if url.startswith("/"):
            url = BASE_URL_A + url
        title = a.get_text(strip=True)
        if not title:
            continue
        date_raw = find_kidate(a)
        if not date_raw:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        events.append(make_event(
            title=title,
            date_raw=date_raw,
            date_iso=to_iso(date_raw),
            time_raw="",
            location="",
            apply_info="",
            category=guess_category(title),
            target_age=guess_age(title),
            url=url,
            source="子育てナビ",
        ))
    print(f"  取得: {len(events)} 件")
    return events


def scrape_kosodate_with_page(pw_page):
    """ソースA: Playwrightページを受け取って子育てナビをスクレイプ"""
    print("\n=== ソースA: 子育てナビ ===")
    all_events = []
    for page_num in range(1, 11):
        url = LIST_URL_A if page_num == 1 else f"{LIST_URL_A}&page={page_num}"
        print(f"  GET {url}")
        pw_page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            pw_page.wait_for_function(
                "document.body.innerText.includes('期日')", timeout=15000
            )
        except Exception:
            print("  期日テキスト待機タイムアウト")
        pw_page.wait_for_timeout(2000)
        html = pw_page.content()
        events = parse_kosodate_html(html)
        if not events:
            break
        existing = {e["url"] for e in all_events}
        new = [e for e in events if e["url"] not in existing]
        if not new:
            print(f"  {page_num}ページ目: 重複のみ -> 終了")
            break
        all_events.extend(new)
        time.sleep(1)
    print(f"  ソースA 合計: {len(all_events)} 件")
    return all_events


# ─────────────────────────────────────────
# ソースB: 総合子育て支援センター（Playwright）
# JavaScriptで動的レンダリングされるため requests では取得不可
# ─────────────────────────────────────────
def scrape_sogo_center_with_page(pw_page):
    print("\n=== ソースB: 総合子育て支援センター ===")
    html = fetch_html_playwright(pw_page, URL_B, wait_text="イベント情報")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now()

    # ページ全体のテキストで「イベント情報」が存在するか確認
    page_text = soup.get_text()
    if "イベント情報" not in page_text:
        print("  イベント情報セクションが見つかりません")
        print(f"  ページテキスト冒頭200字: {page_text[:200]}")
        return []

    # h3タグを全取得し「イベント情報」h2より後ろのものを対象にする
    # find_next_sibling()はdivラッパーを超えられないため使用しない
    all_h2 = soup.find_all(["h2", "h3"])
    
    # 「イベント情報」h2のインデックスを特定
    event_start_idx = None
    event_end_idx = len(all_h2)
    for i, tag in enumerate(all_h2):
        txt = tag.get_text(strip=True)
        if tag.name == "h2" and "イベント情報" in txt:
            event_start_idx = i
        elif tag.name == "h2" and event_start_idx is not None and i > event_start_idx:
            event_end_idx = i
            break

    if event_start_idx is None:
        print("  イベント情報h2が見つかりません")
        print(f"  h2一覧: {[t.get_text(strip=True)[:20] for t in soup.find_all('h2')]}")
        return []

    # イベント情報セクション内のh3だけを抽出
    target_h3s = [
        t for t in all_h2[event_start_idx+1:event_end_idx]
        if t.name == "h3"
    ]
    print(f"  イベントh3数: {len(target_h3s)}")

    events = []
    for h3 in target_h3s:
        title = h3.get_text(strip=True)
        # h3の直後（兄弟・子孫問わず）最初のtableを探す
        table = h3.find_next("table")
        if not table:
            continue

        # tableが次のh3より前にあるか確認
        next_h3 = h3.find_next("h3")
        if next_h3:
            # tableがnext_h3より後ならスキップ
            try:
                h3_pos = str(soup).find(str(h3))
                table_pos = str(soup).find(str(table))
                next_h3_pos = str(soup).find(str(next_h3))
                if table_pos > next_h3_pos:
                    continue
            except Exception:
                pass

        fields = {}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                key = tds[0].get_text(strip=True)
                val = tds[1].get_text(" ", strip=True)
                fields[key] = val

        date_raw = fields.get("■期日", "")
        if not date_raw:
            continue

        time_text = fields.get("■時間", "")
        location = fields.get("■場所", LOCATION_B)
        target_text = fields.get("■対象", "")
        apply_text = fields.get("■申込み", "")
        needs_res = is_reservation_required(apply_text)

        date_iso = normalize_date(date_raw, base_year=now.year, base_month=now.month)
        time_norm = normalize_time(time_text)
        target_age = guess_age(target_text) if target_text else guess_age(title)

        if date_iso:
            ev = make_event(
                title=title,
                date_raw=date_raw,
                date_iso=date_iso,
                time_raw=time_norm,
                location=location,
                apply_info=apply_text[:100],
                category=guess_category(title),
                target_age=target_age,
                url=URL_B,
                source=SOURCE_B,
                needs_reservation=needs_res,
            )
            events.append(ev)
            print(f"  OK: {title[:30]} / {date_raw} / {'★要予約' if needs_res else '予約不要'}")

    print(f"  ソースB 合計: {len(events)} 件")
    return events


# ─────────────────────────────────────────
# ソースC: こども文化会館（requests）
# ─────────────────────────────────────────
def scrape_kodomobunka():
    print("\n=== ソースC: こども文化会館 ===")
    html = fetch_html(URL_C)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now()

    # event.cgiリンクを全取得
    event_links = soup.find_all("a", href=re.compile(r"event\.cgi"))
    print(f"  event.cgiリンク数: {len(event_links)}")

    seen = set()
    events = []

    for a in event_links:
        title = a.get_text(strip=True)
        if not title or title in seen:
            continue
        href = a.get("href", "")
        url = (href if href.startswith("http")
               else BASE_URL_C + "/event/" + href.lstrip("./"))

        # 祖先要素からコンテキストを取得
        container = a
        for _ in range(8):
            if container.parent is None:
                break
            container = container.parent
            ct = container.get_text(" ", strip=True)
            if re.search(r"20\d{2}", ct) and re.search(r"\d{1,2}月\d{1,2}日", ct):
                break

        ct = container.get_text(" ", strip=True) if container else ""

        # 年
        ym = re.search(r"(20\d{2})", ct)
        year = ym.group(1) if ym else str(now.year)

        # 日付（期間の場合は開始日）
        dm = re.search(r"(\d{1,2})月(\d{1,2})日", ct)
        if not dm:
            continue
        mo, dy = dm.group(1).zfill(2), dm.group(2).zfill(2)
        date_iso = f"{year}-{mo}-{dy}"
        date_raw = f"{year}年{mo}月{dy}日"

        # 時間
        time_m = re.search(
            r"(\d{1,2})\s*時(\d{2})分より[\s\S]{0,20}?(\d{1,2})時(\d{2})分まで", ct
        )
        if time_m:
            time_raw = (f"{int(time_m.group(1)):02d}:{time_m.group(2)}"
                        f"〜{int(time_m.group(3)):02d}:{time_m.group(4)}")
        else:
            time_raw = ""

        # 対象
        tgt_m = re.search(r"対象[/／]([^\s　参加費定員]+)", ct)
        target_text = tgt_m.group(1) if tgt_m else ""

        # 申込
        appl_m = re.search(r"(事前申込|当日申込可)", ct)
        apply_text = appl_m.group(1) if appl_m else ""
        needs_res = "事前申込" in apply_text

        # 乳幼児・保護者向けフィルタ
        check = title + " " + target_text
        if not any(kw in check for kw in KODOMOBUNKA_KW):
            continue

        seen.add(title)
        ev = make_event(
            title=title,
            date_raw=date_raw,
            date_iso=date_iso,
            time_raw=time_raw,
            location=LOCATION_C,
            apply_info=apply_text,
            category=guess_category(title),
            target_age=guess_age(target_text + title),
            url=url,
            source=SOURCE_C,
            needs_reservation=needs_res,
        )
        events.append(ev)
        print(f"  OK: {title[:30]} / {date_iso} / {'★要予約' if needs_res else '予約不要'}")

    print(f"  ソースC 合計: {len(events)} 件")
    return events


# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────
def scrape():
    all_events = []

    # ソースA・Bは同一Playwrightブラウザで実行（起動コスト節約）
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pw_page = browser.new_page()
        pw_page.set_extra_http_headers({"Accept-Language": "ja,en;q=0.9"})

        try:
            all_events.extend(scrape_kosodate_with_page(pw_page))
        except Exception as e:
            print(f"ソースAエラー: {e}")

        try:
            all_events.extend(scrape_sogo_center_with_page(pw_page))
        except Exception as e:
            print(f"ソースBエラー: {e}")

        browser.close()

    # ソースC はrequestsで取得（JSなし静的HTML）
    try:
        all_events.extend(scrape_kodomobunka())
    except Exception as e:
        print(f"ソースCエラー: {e}")

    # ソースD: 各児童館（PDF解析）
    try:
        all_events.extend(scrape_all_halls_adapted())
    except Exception as e:
        print(f"ソースDエラー: {e}")

    # 日付順ソート
    all_events.sort(key=lambda e: e.get("date_iso") or "9999")
    print(f"\n=== 全ソース合計: {len(all_events)} 件 ===")
    return all_events


def save(events):
    out_path = Path("docs/events.json")
    out_path.parent.mkdir(exist_ok=True)
    output = {
        "updated_at": datetime.now().isoformat(),
        "count": len(events),
        "events": events,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"保存完了: {out_path} ({len(events)} 件)")


def update_html(events_data):
    html_path = Path("docs/index.html")
    if not html_path.exists():
        print("警告: docs/index.html が見つかりません")
        return
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    start_marker = "/* EVENTS_DATA_START */"
    end_marker = "/* EVENTS_DATA_END */"
    s = html.find(start_marker)
    e = html.find(end_marker)
    if s == -1 or e == -1:
        print("警告: index.htmlのプレースホルダーが見つかりません")
        return
    json_str = json.dumps(events_data, ensure_ascii=False)
    new_block = f"{start_marker}\nconst INLINE_EVENTS = {json_str};\n{end_marker}"
    html = html[:s] + new_block + html[e + len(end_marker):]
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"index.html更新完了 ({events_data['count']}件埋め込み)")


if __name__ == "__main__":
    events = scrape()
    save(events)
    update_html({
        "updated_at": datetime.now().isoformat(),
        "count": len(events),
        "events": events,
    })
