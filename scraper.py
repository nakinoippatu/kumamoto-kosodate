"""
熊本市 子育て支援 統合スクレイパー
ソース:
  A) 子育てナビ（kumamoto-kekkon-kosodate.jp） Playwright使用
  B) 総合子育て支援センター（city.kumamoto.jp）  requests使用
  C) こども文化会館（kodomobunka.jp）            requests使用
  D) 各児童館（幸田/西部/西原/花園/託麻/秋津/五福/天明/大江/城南）PDF解析
  E) 子育て支援センター18施設（保育園内設置）    PDF解析
     統括ページ: https://www.city.kumamoto.jp/kiji00364201/index.html
     施設: 総合/白山/京塚/イルカクラブ/ながみね/やまなみ/画図/
           小島/池上/京町台/幸田/さくらっこ/だいいち/城南/
           植木/清水/西里/あゆみ

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

# CIDフォント由来の文字化けパターン（pdfplumberが○や□に変換するもの）
_CID_GARBLED_RE = re.compile(r'^[○◯□■●◆▲△▽▼◇◎※〇]+$')

def _is_non_event(text: str) -> bool:
    """イベントとして登録しない内容かどうか"""
    t = re.sub(r'[\(（].*?[\)）]', '', text).strip()
    t = re.sub(r'\s', '', t)
    if not t:
        return True
    # 1〜2文字以下で日本語を含まないものは文字化け・記号単体とみなす
    if len(t) <= 2 and not re.search(r'[ぁ-ん一-龯]', t):
        return True
    # ○□■などの記号のみ（CIDフォント文字化け）
    if _CID_GARBLED_RE.match(t):
        return True
    return bool(NON_EVENT_RE.match(t))

def _fetch_pdf_bytes(url: str, retries: int = 3) -> bytes | None:
    """URLからPDFバイト列を取得（500/503エラー時は指数バックオフでリトライ）"""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # 500/503/504はサーバー側の一時障害 → リトライ対象
            if attempt < retries and status in (500, 503, 504, None):
                wait = 5 * attempt
                logger.warning(f"PDF取得失敗({status}) {url} → {wait}秒後リトライ ({attempt}/{retries})")
                time.sleep(wait)
            else:
                logger.error(f"PDF取得失敗 {url}: {e}")
                return None
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


def _fetch_pdf_urls_from_page(pw_page, page_url: str, count: int = 2) -> list[str]:
    """
    Playwrightで施設ページを開き、PDFのURLを最大 count 件返す。
    花園児童館のように複数PDFが必要な場合に使用する。
    """
    try:
        pw_page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        pw_page.wait_for_timeout(2000)
        html = pw_page.content()
    except Exception as e:
        print(f"  ⚠️ ページ取得失敗 {page_url}: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    all_pdf_links = soup.find_all("a", href=re.compile(r"\.pdf", re.I))

    urls = []
    for a in all_pdf_links:
        href = a.get("href", "")
        url = href if href.startswith("http") else "https://www.city.kumamoto.jp" + href
        if url not in urls:
            urls.append(url)
        if len(urls) >= count:
            break
    return urls


def _fetch_pdf_url_from_page(pw_page, page_url: str, keyword: str = "乳幼児") -> str | None:
    """
    Playwrightで施設ページを開き、乳幼児向けPDFのURLを動的取得する。
    熊本市公式サイトはJSレンダリングのため requests では取得不可。
    """
    try:
        pw_page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        pw_page.wait_for_timeout(2000)
        html = pw_page.content()
    except Exception as e:
        print(f"  ⚠️ ページ取得失敗 {page_url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    all_pdf_links = soup.find_all("a", href=re.compile(r"\.pdf", re.I))

    # "乳幼児" を含むaタグのhrefからPDFを探す
    for a in all_pdf_links:
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if keyword in text:
            if href.startswith("http"):
                return href
            return "https://www.city.kumamoto.jp" + href

    # キーワードなしでも最初のPDFを返す（フォールバック）
    if all_pdf_links:
        href = all_pdf_links[0].get("href", "")
        if href.startswith("http"):
            return href
        return "https://www.city.kumamoto.jp" + href

    # PDF未発見時: デバッグ情報を出力
    all_links = soup.find_all("a", href=True)
    print(f"  ⚠️ PDFリンクなし（全リンク数: {len(all_links)}件）")
    # .pdf以外のダウンロード可能ファイルがあれば表示
    for a in all_links[:5]:
        href = a.get("href", "")
        if any(ext in href.lower() for ext in [".pdf", ".doc", ".xls", "download", "attach"]):
            print(f"     候補リンク: {href[:80]}")
    return None


def scrape_all_halls_adapted(pw_page=None) -> list[dict]:
    """
    児童館イベントを共通形式で返す。
    pw_page が渡された場合は各施設ページから PDF URL を動的取得する。
    """
    print("\n=== ソースD: 各児童館（PDF解析）===")

    pdf_map: dict[str, bytes] = {}

    if pw_page:
        for cfg in HALL_CONFIGS:
            source   = cfg["source"]
            page_url = cfg["url"]
            # 城南児童館はGoogle Drive URLのためスキップ（PDF自動取得不可）
            if "google" in page_url or "share.google" in page_url:
                print(f"  {source}: Google Drive URL のため自動取得スキップ")
                continue
            print(f"  {source}: PDFリンク取得中...")
            pdf_url = _fetch_pdf_url_from_page(pw_page, page_url, keyword="乳幼児")
            if not pdf_url:
                print(f"  {source}: PDFリンクが見つかりませんでした")
                continue
            print(f"  {source}: PDF取得中 {pdf_url}")
            pdf_bytes = _fetch_pdf_bytes(pdf_url)
            if pdf_bytes:
                pdf_map[source] = pdf_bytes
                print(f"  {source}: ✅ PDF取得成功 ({len(pdf_bytes):,} bytes)")
            else:
                print(f"  {source}: ❌ PDF取得失敗")

        # ── 花園児童館: 表面(カレンダー) + 裏面(詳細) の2枚PDF ──
        print(f"  {HANAZONO_SOURCE}: PDFリンク取得中...")
        hanazono_pdf_urls = _fetch_pdf_urls_from_page(pw_page, HANAZONO_URL, count=2)
        if len(hanazono_pdf_urls) >= 2:
            pdf_front = _fetch_pdf_bytes(hanazono_pdf_urls[0])
            pdf_back  = _fetch_pdf_bytes(hanazono_pdf_urls[1])
            if pdf_front and pdf_back:
                print(f"  {HANAZONO_SOURCE}: ✅ PDF2枚取得成功 (表面:{len(pdf_front):,} bytes / 裏面:{len(pdf_back):,} bytes)")
                try:
                    hanazono_events = scrape_hanazono(pdf_front, pdf_back)
                    # scrape_all_halls を経由せず直接 adapted に追加するため一時保持
                    hanazono_adapted = [_hall_event_to_common(ev) for ev in hanazono_events]
                    print(f"  {HANAZONO_SOURCE}: {len(hanazono_adapted)} 件取得")
                except Exception as e:
                    print(f"  {HANAZONO_SOURCE}: ❌ 解析エラー {e}")
                    hanazono_adapted = []
            else:
                print(f"  {HANAZONO_SOURCE}: ❌ PDF取得失敗")
                hanazono_adapted = []
        elif len(hanazono_pdf_urls) == 1:
            print(f"  {HANAZONO_SOURCE}: ⚠️ PDFが1枚のみ（2枚必要）")
            hanazono_adapted = []
        else:
            print(f"  {HANAZONO_SOURCE}: ⚠️ PDFリンクが見つかりませんでした")
            hanazono_adapted = []
    else:
        hanazono_adapted = []

    raw = scrape_all_halls(pdf_map=pdf_map if pdf_map else None)
    adapted = [_hall_event_to_common(e) for e in raw] + hanazono_adapted
    print(f"  ソースD 合計: {len(adapted)} 件")
    return adapted


# ════════════════════════════════════════════════════════
# ソースE: 子育て支援センター18施設 PDF解析スクレイパー
#
# 統括ページ: https://www.city.kumamoto.jp/kiji00364201/index.html
# 各施設の月次PDFおたよりを統括ページから動的取得し解析する。
#
# 施設一覧:
#   中央区: 総合（公立）、白山（公立）
#   東区:   京塚（公立）、イルカクラブ、ながみね、やまなみ、画図
#   西区:   小島（公立）、池上（公立）、京町台（公立）
#   南区:   幸田（公立）、さくらっこ、だいいち、城南
#   北区:   植木（公立）、清水（公立）、西里（公立）、あゆみ
# ════════════════════════════════════════════════════════

CENTER_LIST_URL = "https://www.city.kumamoto.jp/kiji00364201/index.html"

CENTER_DEFS = [
    # ── 中央区 ──────────────────────────────────────────
    {
        "source":   "総合子育て支援センター",
        "ward":     "中央区",
        "location": "熊本市中央区本荘（本荘保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji0031482/index.html",
        "list_key": "総合",
        "public":   True,
    },
    {
        "source":   "白山子育て支援センター",
        "ward":     "中央区",
        "location": "熊本市中央区白山（白山保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364625/index.html",
        "list_key": "白山",
        "public":   True,
    },
    # ── 東区 ────────────────────────────────────────────
    {
        "source":   "京塚子育て支援センター",
        "ward":     "東区",
        "location": "熊本市東区尾ノ上（京塚保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364621/index.html",
        "list_key": "京塚",
        "public":   True,
    },
    {
        "source":   "イルカクラブ子育て支援センター",
        "ward":     "東区",
        "location": "熊本市東区佐土原（エンゼル保育園内）",
        "page_url": "https://sadohara-fukushikai.net/iruka.php",
        "list_key": "イルカクラブ",
        "public":   False,
    },
    {
        "source":   "ながみね子育て支援センター",
        "ward":     "東区",
        "location": "熊本市東区長嶺南（つばめこども園内）",
        "page_url": "https://kosodate-web.com/tsubamehoikuen/support.php",
        "list_key": "ながみね",
        "public":   False,
    },
    {
        "source":   "やまなみ子育て支援センター",
        "ward":     "東区",
        "location": "熊本市東区戸島西（やまなみこども園内）",
        "page_url": "https://yamanami-kodomoen.com/summary/",
        "list_key": "やまなみ",
        "public":   False,
    },
    {
        "source":   "画図子育て支援センター",
        "ward":     "東区",
        "location": "熊本市東区下江津（画図保育園内）",
        "page_url": "https://www.ezuhoikuen.jp/childcare/",
        "list_key": "画図",
        "public":   False,
    },
    # ── 西区 ────────────────────────────────────────────
    {
        "source":   "小島子育て支援センター",
        "ward":     "西区",
        "location": "熊本市西区小島（小島保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364624/index.html",
        "list_key": "小島",
        "public":   True,
    },
    {
        "source":   "池上子育て支援センター",
        "ward":     "西区",
        "location": "熊本市西区池上（池上保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364626/index.html",
        "list_key": "池上",
        "public":   True,
    },
    {
        "source":   "京町台子育て支援センター",
        "ward":     "西区",
        "location": "熊本市西区池田（京町台保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364627/index.html",
        "list_key": "京町台",
        "public":   True,
    },
    # ── 南区 ────────────────────────────────────────────
    {
        "source":   "幸田子育て支援センター",
        "ward":     "南区",
        "location": "熊本市南区良町（幸田保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364628/index.html",
        "list_key": "幸田",
        "public":   True,
    },
    {
        "source":   "さくらっこ子育て支援センター",
        "ward":     "南区",
        "location": "熊本市南区合志（力合さくら子ども園内）",
        "page_url": "https://www.rikigo-sakura.jp/childcare/",
        "list_key": "さくらっこ",
        "public":   False,
    },
    {
        "source":   "だいいち子育て支援センター",
        "ward":     "南区",
        "location": "熊本市南区富合町（第一幼稚園内）",
        "page_url": "https://youikuen.com/support/",
        "list_key": "だいいち",
        "public":   False,
    },
    {
        "source":   "城南子育て支援センター",
        "ward":     "南区",
        "location": "熊本市南区城南町（小木こども園内）",
        "page_url": "https://ogihoiku.com/page-76/",
        "list_key": "城南",
        "public":   False,
    },
    # ── 北区 ────────────────────────────────────────────
    {
        "source":   "植木子育て支援センター",
        "ward":     "北区",
        "location": "熊本市北区植木町（山本保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364623/index.html",
        "list_key": "植木",
        "public":   True,
    },
    {
        "source":   "清水子育て支援センター",
        "ward":     "北区",
        "location": "熊本市北区清水本町（清水保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364622/index.html",
        "list_key": "清水",
        "public":   True,
    },
    {
        "source":   "西里子育て支援センター",
        "ward":     "北区",
        "location": "熊本市北区硯川町（西里保育園内）",
        "page_url": "https://www.city.kumamoto.jp/kiji00364618/index.html",
        "list_key": "西里",
        "public":   True,
    },
    {
        "source":   "あゆみ子どもセンター",
        "ward":     "北区",
        "location": "熊本市北区武蔵ヶ丘（あゆみ保育園内）",
        "page_url": "https://www.kumamoto-ayumi.org/ayumicenter/",
        "list_key": "あゆみ",
        "public":   False,
    },
]

_WEEKDAYS_7 = {"月", "火", "水", "木", "金", "土", "日"}


def _fetch_center_pdf_urls(html: str) -> dict[str, str]:
    """
    統括ページHTMLを解析して {list_key: pdf_url} を返す。

    戦略:
      1. 各 <td> 単位で施設名とPDFリンクを紐付ける（セル内に施設名+PDFリンクが同居）
      2. セル単位でマッチしない場合は、<tr> 単位で施設名セルの順序と
         PDFリンクの順序を対応させる（植木・清水が同一trに並ぶ場合の対策）
      3. 上記でも取れない場合は従来の「tr全体テキスト+先頭PDF」フォールバック
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}
    BASE_CITY = "https://www.city.kumamoto.jp"
    PDF_RE = re.compile(r"\.pdf", re.I)

    def to_url(href: str) -> str:
        return href if href.startswith("http") else BASE_CITY + href

    # ── Step1: td単位マッチング（最も確実）──────────────────────
    for td in soup.find_all("td"):
        td_text = td.get_text(" ", strip=True)
        for cdef in CENTER_DEFS:
            key = cdef["list_key"]
            if key in result:
                continue
            if key not in td_text:
                continue
            # このtd内、またはtr内でこのtdの直後に続くPDFリンクを取得
            # まずtd内を探す
            a = td.find("a", href=PDF_RE)
            if a:
                result[key] = to_url(a["href"])
                continue
            # td内になければ、同じtr内で施設名tdの後に来るPDFリンクtdを探す
            parent_tr = td.find_parent("tr")
            if not parent_tr:
                continue
            tds = parent_tr.find_all("td")
            td_idx = tds.index(td) if td in tds else -1
            if td_idx < 0:
                continue
            # この施設名tdより後ろのtdからPDFを探す
            for next_td in tds[td_idx:]:
                a = next_td.find("a", href=PDF_RE)
                if a:
                    result[key] = to_url(a["href"])
                    break

    # ── Step2: tr単位で複数施設・複数PDFを順序対応させる ──────────
    # Step1で取れなかった施設向け（植木・清水が同一trの別々のtdにある場合）
    missing_keys = [c["list_key"] for c in CENTER_DEFS if c["list_key"] not in result]
    if missing_keys:
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            # このtr内でマッチする施設キーを出現順に収集
            matched_keys = []
            for td in tds:
                td_text = td.get_text(" ", strip=True)
                for key in missing_keys:
                    if key in result:
                        continue
                    if key in td_text and key not in matched_keys:
                        matched_keys.append(key)
            if not matched_keys:
                continue
            # このtr内のPDFリンクを順番に収集
            pdf_urls = [to_url(a["href"]) for a in tr.find_all("a", href=PDF_RE)]
            # 施設キーとPDFを順番に対応させる
            for i, key in enumerate(matched_keys):
                if key not in result and i < len(pdf_urls):
                    result[key] = pdf_urls[i]

    # ── Step3: 従来フォールバック（tr全体テキスト+先頭PDF）──────────
    still_missing = [c["list_key"] for c in CENTER_DEFS if c["list_key"] not in result]
    if still_missing:
        for tr in soup.find_all("tr"):
            cell_text = " ".join(td.get_text(" ", strip=True) for td in tr.find_all("td"))
            for cdef in CENTER_DEFS:
                key = cdef["list_key"]
                if key in result or key not in cell_text:
                    continue
                for a in tr.find_all("a", href=PDF_RE):
                    result[key] = to_url(a["href"])
                    break

    return result


def _center_guess_category(text: str) -> str:
    if re.search(r"離乳食|栄養|食育", text):                              return "食育・栄養"
    if re.search(r"発達|言語|相談|聴覚", text):                           return "発達・育児相談"
    if re.search(r"マッサージ|アロマ|ピラティス|ヨガ|骨盤|産前|産後|マタニティ", text): return "産前・産後"
    if re.search(r"パパ|父", text):                                       return "父親・家族支援"
    if re.search(r"ひとり親", text):                                       return "ひとり親支援"
    if re.search(r"ダンス|体操|リトミック|運動|ふれあい|おはなし|読み聞かせ|工作|あそび|遊び|ハイハイ|ベビーサイン|手遊び|絵本", text): return "親子ふれあい"
    if re.search(r"身体測定|健康|歯|0歳|赤ちゃん", text):                 return "健康・医療"
    return "その他"


def _center_guess_age(text: str) -> str:
    if re.search(r"妊婦|妊娠中|マタニティ|プレママ|プレパパ|やすらぎタイム", text): return "妊娠中"
    if re.search(r"0歳|乳児|産後|ベビー|赤ちゃん|ハイハイ|ハーフバースデー", text): return "0歳"
    if re.search(r"1歳|2歳|１歳|２歳", text):                            return "1〜2歳"
    if re.search(r"3歳|4歳|5歳|未就学|幼児", text):                       return "3〜5歳"
    if re.search(r"乳幼児", text):                                        return "0歳〜未就学"
    return "指定なし"


def _center_guess_age_from_target(target_str: str) -> str:
    """
    対象文字列（「0歳の乳児と保護者」等）から target_age を精密に推定。
    「0歳のみ」と「0〜N歳」を正しく区別する。京町台形式のPDFで使用。
    """
    t = target_str
    # "0歳" のみで他の年齢を含まない
    if re.search(r"0[歳才]", t) and not re.search(r"[1-9][歳才～〜]|[1-9]歳", t):
        return "0歳"
    # "0歳〜N歳" 系（"0～2歳" "0歳～3歳" 等）
    if re.search(r"0[歳才～〜]", t) and re.search(r"[1-9]歳", t):
        return "0歳〜未就学"
    if re.search(r"1歳以上", t):                      return "1〜2歳"
    if re.search(r"[12]歳", t):                       return "1〜2歳"
    if re.search(r"[345]歳|未就学", t):               return "3〜5歳"
    if re.search(r"乳幼児", t):                        return "0歳〜未就学"
    return "指定なし"


def _center_get_year_month(text: str, metadata: dict,
                            fallback_year: int, fallback_month: int) -> tuple[int, int]:
    """テキストとメタデータから年月を推定"""
    t = _z2h(text)
    # 令和N年M月
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月", t)
    if m:
        return int(m.group(1)) + 2018, int(m.group(2))
    # 西暦N年M月
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    # (2026年) + N月号
    my = re.search(r"[（(](20\d{2})年[）)]", t)
    mm = re.search(r"(\d+)月号", t)
    if my and mm:
        return int(my.group(1)), int(mm.group(1))
    # 令和N年度 + テキスト先頭のM月
    mn = re.search(r"令和\s*(\d+)\s*年度", t)
    mk = re.search(r"(\d{1,2})\s*月", t[:150])
    if mn and mk:
        reiwa = int(mn.group(1))
        mo    = int(mk.group(1))
        year  = reiwa + 2018 + (1 if mo <= 3 else 0)
        return year, mo
    # メタデータ作成日 + N月号
    cd_m = re.search(r"D:(\d{4})(\d{2})(\d{2})", metadata.get("CreationDate", ""))
    if mm and cd_m:
        month = int(mm.group(1))
        cy, cmo = int(cd_m.group(1)), int(cd_m.group(2))
        return (cy, month) if month >= cmo else (cy + 1, month)
    # メタデータの翌月
    if cd_m:
        y2, mo2 = int(cd_m.group(1)), int(cd_m.group(2))
        mo2 += 1
        if mo2 > 12:
            mo2, y2 = 1, y2 + 1
        return y2, mo2
    return fallback_year, fallback_month


def _parse_center_calendar_table(
    table: list[list],
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
    default_time: str = "10:30〜11:00",
    detail_target_map: dict | None = None,
    all_reserved: bool = False,
) -> list[dict]:
    """
    子育て支援センターのカレンダーテーブルをパースしてイベントリストを返す。

    対応形式:
      - 7列（月〜日 or 日〜土）: 標準形式
      - 17列（月〜土 × 3列マージン）: 京町台形式
        ヘッダー位置-1がデータ開始列（花園・秋津と同じ構造）

    Args:
        detail_target_map: {day_num: target_str} 詳細ブロックから取得した対象情報
        all_reserved: True のとき全イベントを事前予約制（★付き）とする
    """
    # ─────────────────────────────────────────────────────────
    # ヘッダー行を特定し、カレンダー形式を判定
    #
    # 形式A (7列標準): ヘッダー列とデータ列が同じ位置
    #   ['月', '火', '水', '木', '金', '土', '日']
    #   ['2',  '3',  '4',  '5',  '6',  '7', None]
    #   ['内容', ...]
    #
    # 形式B (17列京町台): ヘッダー列-1がデータ開始列
    #   ['', '月', '', '', '火', '', '', '水', ...]
    #   ['', '2',  '', '', '3',  '', '', '4',  ...]
    #   ['内容', None, None, '内容', None, ...]
    # ─────────────────────────────────────────────────────────
    wd_cols   = []  # [(wd_char, data_start_col, data_end_col)]
    header_ri = None
    use_offset = False  # True=形式B（ヘッダー-1がデータ列）

    for ri, row in enumerate(table):
        found = [ci for ci, c in enumerate(row) if c and c.strip() in _WEEKDAYS_7]
        if len(found) >= 5:
            header_ri = ri
            # 形式B判定: 各曜日ヘッダーの直前列が空かつ全体列数が12+
            if len(row) >= 12 and all(
                (ci > 0 and not row[ci - 1]) for ci in found
            ):
                use_offset = True
                # データ列はヘッダー列-1から始まり次の曜日ヘッダー-1まで
                for i, ci in enumerate(found):
                    wd = row[ci].strip()
                    s  = ci - 1
                    e  = found[i + 1] - 1 if i + 1 < len(found) else len(row)
                    wd_cols.append([wd, s, e])
            else:
                # 形式A: ヘッダー列がそのままデータ範囲の開始
                for ci in found:
                    wd_cols.append([row[ci].strip(), ci, len(row)])
                for i in range(len(wd_cols) - 1):
                    wd_cols[i][2] = wd_cols[i + 1][1]
            break

    if not wd_cols:
        return []

    def get_wd_block(col):
        for wd, s, e in wd_cols:
            if s <= col < e:
                return wd, s, e
        return None, None, None

    def get_block_text(rows, s, e):
        parts = []
        for row in rows:
            for ci in range(s, min(e, len(row))):
                c = row[ci]
                if not c or not c.strip():
                    continue
                for line in c.splitlines():
                    n = _normalize(line)
                    if n and n not in parts:
                        parts.append(n)
        return "\n".join(parts)

    SKIP_RE_C = re.compile(
        r"(自由あそび|自由遊び|休館日?|祝日|天皇誕生日|建国記念|振替休日|春分|秋分|開館|閉館)"
    )
    # 時刻なしで〜/～で終わる → PDFのセル分割による途中切れタイトル
    TRUNCATED_TITLE_RE = re.compile(r"(?<!\d)[〜～]$")

    events = []
    i = header_ri + 1
    while i < len(table):
        row = table[i]
        day_cells = []
        for ci, c in enumerate(row):
            if c and re.match(r"^[０-９\d]+$", c.strip()):
                try:
                    day_cells.append((ci, int(_z2h(c.strip()))))
                except ValueError:
                    pass

        if len(day_cells) >= 3:
            content_row = table[i + 1] if i + 1 < len(table) else []
            for day_ci, day_num in day_cells:
                # 形式Bでは日付列はヘッダー位置と同じ → コンテンツは day_ci-1
                content_ci_for_b = day_ci - 1 if use_offset else day_ci
                wd, s, e = get_wd_block(content_ci_for_b)
                if wd is None:
                    continue

                if use_offset:
                    # 形式B: 内容は content_row の s セル（単一セル）
                    raw_cell = content_row[s] if s < len(content_row) else ""
                    raw = _normalize(raw_cell or "")
                else:
                    raw = get_block_text([content_row], s, e)

                if not raw or SKIP_RE_C.search(raw) or _is_non_event(raw):
                    continue

                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                title_parts, desc_parts = [], []
                for l in lines:
                    if re.search(r"\d{1,2}:\d{2}", _z2h(l)) or l.startswith(("（", "(", "※", "【", "〈")):
                        desc_parts.append(l)
                    else:
                        title_parts.append(l)

                title = " ".join(title_parts).strip()
                if not title:
                    clean = re.sub(r"[（(][^）)]*[）)]", "", raw)
                    clean = TIME_RE.sub("", _z2h(clean)).strip()
                    title = re.sub(r"[　\s]+", " ", clean).strip()

                # 先頭の☆・★はカレンダーマーカーなので除去（後で★を付与するため）
                title = re.sub(r"^[☆★]+\s*", "", title).strip()

                if not title or SKIP_RE_C.search(title) or _is_non_event(title):
                    continue
                # 時刻なしで〜で終わる → PDFセル分割による途中切れタイトル → スキップ
                if TRUNCATED_TITLE_RE.search(title):
                    continue

                time_str  = _extract_time(raw) or default_time
                desc      = " ".join(desc_parts)

                # 予約要否: all_reserved フラグ または テキストから判定
                needs_res = all_reserved or bool(
                    re.search(r"(要申込|事前申込|申し込み|申込み|電話で)", desc + title)
                )

                # 詳細ブロックから対象情報を補完
                target_str = ""
                target_age = _center_guess_age(title + desc)
                if detail_target_map and day_num in detail_target_map:
                    target_str = detail_target_map[day_num]
                    target_age = _center_guess_age_from_target(target_str)

                try:
                    ev_date = date(year, month, day_num)
                except ValueError:
                    continue

                display_title = f"★{title}" if needs_res else title
                apply_info_str = target_str[:100] if target_str else (desc[:100] if desc else "")
                events.append({
                    "title":            display_title,
                    "date_raw":         f"{year}年{month}月{day_num}日",
                    "date_iso":         ev_date.strftime("%Y-%m-%d"),
                    "time_raw":         time_str,
                    "location":         location,
                    "apply_info":       apply_info_str,
                    "category":         _center_guess_category(title + desc),
                    "target_age":       target_age,
                    "url":              url,
                    "source":           source,
                    "needs_reservation": needs_res,
                    "body_preview":     "",
                })
            i += 2
        else:
            i += 1

    return events


def _parse_center_star_detail_calendar(
    tables: list[list[list]],
    text: str,
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
) -> list[dict]:
    """
    幸田子育て支援センター形式のPDFをパースしてイベントリストを返す。

    特徴:
      - テキストに「☆イベント名（N日 or N日・M日 or N日〜M日）」形式の詳細ブロックがある
      - カレンダーは7列（月〜土）で「日付行＋内容行」の2行ペア構成
      - 内容セルに「（自由遊びもできます）」のような括弧付き補足がある
      - 全イベントが予約制（「予約制」注記あり）

    活用タイミング:
      _parse_center_calendar_table がイベントを取得できなかった場合の
      追加フォールバックとして _scrape_center_pdf から呼び出す。
    """
    text_z = _z2h(text)

    # ── ☆形式の詳細ブロックから {day: ev_name} を構築 ────────
    # 対応形式:
    #   ☆節分の会（3日）
    #   ☆ひな飾りづくり（6日・9日・13日）
    #   ☆カレンダー作り（16日〜20日）
    DETAIL_RE = re.compile(r"☆([^\n☆（(]+)[（(]([^）)]+)[）)]")
    detail_map: dict[int, str] = {}

    for m in DETAIL_RE.finditer(text_z):
        ev_name  = m.group(1).strip()
        date_raw = _z2h(m.group(2))
        # 範囲「N日〜M日」
        range_m = re.search(r"(\d+)日[〜～](\d+)日", date_raw)
        if range_m:
            for d in range(int(range_m.group(1)), int(range_m.group(2)) + 1):
                detail_map[d] = ev_name
        else:
            for d in [int(x) for x in re.findall(r"(\d+)日", date_raw)]:
                detail_map[d] = ev_name

    if not detail_map:
        return []  # ☆形式がなければこのパーサーは使わない

    # ── カレンダーテーブルを探す ──────────────────────────────
    cal = None
    for t in tables:
        if t and len(t) >= 6 and len(t[0]) >= 6:
            header_row = t[0]
            wd_count = sum(
                1 for c in header_row
                if c and c.strip() in {"月", "火", "水", "木", "金", "土", "日"}
            )
            if wd_count >= 5:
                cal = t
                break

    if cal is None:
        return []

    SKIP_FULL_RE = re.compile(r"(建国記念|天皇誕生日|★自由遊び★|振替休日|春分|秋分)")
    DAY_START_RE = re.compile(r"^(\d{1,2})")

    events = []

    for ri, row in enumerate(cal):
        first = next((c for c in row if c and c.strip()), None)
        if not first or not DAY_START_RE.match(_z2h(first.strip())):
            continue

        content_row = cal[ri + 1] if ri + 1 < len(cal) else []

        for ci, cell in enumerate(row):
            if ci == 1 or not cell:
                continue
            cell_z = _z2h(cell.strip())
            dm = DAY_START_RE.match(cell_z)
            if not dm:
                continue
            day_num = int(dm.group(1))
            if not (1 <= day_num <= 31):
                continue

            if SKIP_FULL_RE.search(cell_z):
                continue

            # 内容取得
            content = ""
            if ci < len(content_row) and content_row[ci]:
                content = _z2h(content_row[ci].strip())

            if SKIP_FULL_RE.search(content):
                continue

            # 詳細マップにあるイベントを優先
            if day_num in detail_map:
                title    = detail_map[day_num]
                time_m   = re.search(r"(\d{1,2}:\d{2})", content)
                time_str = time_m.group(1) + "〜" if time_m else "11:00〜"
                needs_res = True  # 幸田は全イベント予約制
            else:
                if not content:
                    continue
                # 括弧除去してタイトル抽出
                title_raw = re.sub(r"[（(][^）)]*[）)]", "", content).strip()
                lines = [l.strip() for l in title_raw.splitlines() if l.strip()]
                title = lines[0] if lines else ""
                title = re.sub(r"(期間中随時|おあつまり|\d{1,2}:\d{2}〜?.*)", "", title).strip()
                if not title or re.match(r"^(自由遊び|自由あそび)$", title):
                    continue
                time_m    = re.search(r"(\d{1,2}:\d{2})", content)
                time_str  = time_m.group(1) + "〜" if time_m else "11:00〜"
                needs_res = bool(re.search(r"予約制", cell_z))

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            display_title = f"★{title}" if needs_res else title
            events.append({
                "title":            display_title,
                "date_raw":         f"{year}年{month}月{day_num}日",
                "date_iso":         ev_date.strftime("%Y-%m-%d"),
                "time_raw":         time_str,
                "location":         location,
                "apply_info":       "",
                "category":         _center_guess_category(title),
                "target_age":       _center_guess_age(title),
                "url":              url,
                "source":           source,
                "needs_reservation": needs_res,
                "body_preview":     "",
            })

    events.sort(key=lambda x: x["date_iso"])
    return events


def _parse_center_text_list(
    text: str,
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
) -> list[dict]:
    """
    テキストから「N月N日（曜）イベント名 HH:MM〜HH:MM」形式のイベントを抽出。
    テーブル抽出に失敗した場合のフォールバック用。
    """
    t = _z2h(text)
    events = []
    seen: set[tuple] = set()

    DATE_BLOCK_RE = re.compile(
        r"(\d{1,2})月\s*(\d{1,2})日"
        r"\s*(?:[（(][月火水木金土日祝・]{1,4}[）)])?\s*"
        r"([^\n]{1,80})",
    )

    SKIP_C = re.compile(r"(自由あそび|自由遊び|休館|祝日|振替)")

    for m in DATE_BLOCK_RE.finditer(t):
        mo, day = int(m.group(1)), int(m.group(2))
        if mo != month and mo != (month % 12 + 1):
            continue
        if (mo, day) in seen:
            continue

        snippet = m.group(3).strip()
        if not snippet or SKIP_C.search(snippet) or _is_non_event(snippet):
            continue

        time_str  = _extract_time(snippet)
        title_raw = TIME_RE.sub("", snippet).strip()
        title_raw = re.sub(r"[　\s]+", " ", title_raw).strip()
        if not title_raw:
            continue

        needs_res = bool(re.search(r"(要申込|事前申込|申し込み|電話)", snippet))
        display_title = f"★{title_raw}" if needs_res else title_raw

        try:
            ev_date = date(year, mo, day)
        except ValueError:
            continue

        seen.add((mo, day))
        events.append({
            "title":            display_title,
            "date_raw":         f"{year}年{mo}月{day}日",
            "date_iso":         ev_date.strftime("%Y-%m-%d"),
            "time_raw":         time_str or "10:30〜",
            "location":         location,
            "apply_info":       "",
            "category":         _center_guess_category(title_raw),
            "target_age":       _center_guess_age(title_raw),
            "url":              url,
            "source":           source,
            "needs_reservation": needs_res,
            "body_preview":     "",
        })

    return events


def _parse_center_5col_weekly(
    tables: list[list[list]],
    text: str,
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
) -> list[dict]:
    """
    さくらっこ形式（月〜金の5列、日付行なし・週ごとにコンテンツ行）のPDFをパース。

    PDF構造:
      - TABLE[0]: N行×5列（月〜金）
      - ROW[0]: ヘッダー（月 火 水 木 金）
      - ROW[1]: 空行（日付なし）
      - ROW[2]: 第1週のコンテンツ
      - ROW[3]: 空行
      - ROW[4]: 第2週のコンテンツ
      ... (偶数インデックス=コンテンツ, 奇数=空行)

    日付の復元:
      calendar.monthcalendar() から月〜金の日付マトリクスを構築。

    特徴:
      - セルに「（予約制）」が含まれると★要予約
      - 「自由遊び」「祝日」は除外
      - 複数行のタイトルはスペース連結
      - 時刻はセル内の明示時刻 > キーワードマップ > デフォルト
    """
    import calendar as _cal

    # 5列（月〜金）カレンダーテーブルか確認
    cal = None
    for t in tables:
        if not t or len(t[0]) != 5:
            continue
        wd_count = sum(
            1 for c in t[0]
            if c and c.strip() in {"月", "火", "水", "木", "金"}
        )
        if wd_count >= 4:
            cal = t
            break

    if cal is None:
        return []

    # 月〜金の日付マトリクス（calendar.weekday: 月=0）
    cal_matrix = _cal.monthcalendar(year, month)
    week_dates = [
        [wk[0], wk[1], wk[2], wk[3], wk[4]]
        for wk in cal_matrix
        if any(wk[:5])
    ]

    # コンテンツ行: ROW[2], ROW[4], ROW[6], ...
    content_rows = [
        cal[i] for i in range(2, len(cal), 2)
        if i < len(cal)
    ]

    SKIP_RE = re.compile(
        r"(自由遊び|自由あそび|建国記念|天皇誕生日|避難訓練|リッキークラブ|振替休日|春分|秋分)"
    )

    # キーワード→時刻マップ（スペースなしタイトルで比較）
    _TIME_MAP = {
        "身体測定": "10:00〜",
        "おはなし会": "10:45〜",
        "英語で遊ぼう": "11:00〜",
        "ベビーマッサージ": "10:45〜",
        "誕生会": "10:15〜",
        "給食": "11:00〜",
        "離乳食": "11:00〜",
    }

    def _get_time(title_ns: str, cell_z: str) -> str:
        m = re.search(r"(\d{1,2}:\d{2})", cell_z)
        if m:
            return m.group(1) + "〜"
        for kw, t in _TIME_MAP.items():
            if kw in title_ns:
                return t
        return "10:00〜"

    events = []

    for wi, (days, content_row) in enumerate(zip(week_dates, content_rows)):
        for ci, (day_num, cell) in enumerate(zip(days, content_row)):
            if day_num == 0 or not cell:
                continue
            cell_z = _z2h(cell.strip())

            if SKIP_RE.search(cell_z):
                continue

            # タイトル: 括弧・時刻を除去
            title_raw = re.sub(r"[（(][^）)]*[）)]", "", cell_z)
            title_raw = re.sub(r"\d{1,2}:\d{2}[〜～]?", "", title_raw)
            title_raw = re.sub(r"\s+", " ", title_raw).strip()
            title_ns  = re.sub(r"\s", "", title_raw)  # スペースなし版

            if not title_ns or SKIP_RE.search(title_ns):
                continue

            needs_res = "予約制" in cell_z
            time_str  = _get_time(title_ns, cell_z)

            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            display_title = f"★{title_raw}" if needs_res else title_raw
            events.append({
                "title":            display_title,
                "date_raw":         f"{year}年{month}月{day_num}日",
                "date_iso":         ev_date.strftime("%Y-%m-%d"),
                "time_raw":         time_str,
                "location":         location,
                "apply_info":       "",
                "category":         _center_guess_category(title_ns),
                "target_age":       _center_guess_age(title_ns),
                "url":              url,
                "source":           source,
                "needs_reservation": needs_res,
                "body_preview":     "",
            })

    events.sort(key=lambda x: x["date_iso"])
    return events


def _parse_center_word_position_cal(
    words: list[dict],
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
) -> list[dict]:
    """
    だいいち形式（word座標ベース）の7列（日〜土）カレンダーPDFをパース。

    PDF構造（だいいち子育て支援センター）:
      - 7列（日〜土）カレンダー
      - セル内に日付数字と内容が混在（"９※予約制\\nおひな様\\n制作"）
      - 補足行が直下に続く（スノードーム作りがペットボトルの下行など）
      - 時刻は画像内テキストで「（ : ～ : ）」と抽出されるため除去
      - 「3」が「13」として表示されるフォント誤変換あり
        → x座標で前後の日付から推定修正

    戦略:
      1. extract_words() のx座標でカレンダー列を特定
      2. ヘッダー行（日〜土）のx座標から各列の境界を設定
      3. 日付行（数字≥5個の行）を検出
      4. 各日付とそのx列に対応するテキストを収集
      5. フッター行（「＊園庭開放」等）以降を除外
    """
    from collections import defaultdict

    # ヘッダー行のy座標を取得
    WEEKDAY_CHARS = {"日", "月", "火", "水", "木", "金", "土"}
    header_y = next((w["top"] for w in words if w["text"] in WEEKDAY_CHARS), None)
    if header_y is None:
        return []

    # フッター行のy座標（「＊園庭開放」または「園庭開放」を含む行）
    footer_y = next(
        (w["top"] for w in words if "園庭開放" in w["text"] and w["top"] > header_y),
        None
    )

    # カレンダー範囲の単語
    cal_words = [
        w for w in words
        if w["top"] >= header_y
        and (footer_y is None or w["top"] < footer_y)
    ]

    # y座標でグループ化（6px単位）
    by_y: dict = defaultdict(list)
    for w in cal_words:
        y_bucket = round(w["top"] / 6) * 6
        by_y[y_bucket].append(w)
    yd_keys = sorted(by_y.keys())

    # ヘッダー行から各曜日のx境界を決定
    header_row = sorted(by_y[yd_keys[0]], key=lambda w: w["x0"])
    col_boundaries = [w["x0"] for w in header_row if w["text"] in WEEKDAY_CHARS]
    if len(col_boundaries) < 5:
        return []
    col_boundaries.append(float("inf"))

    def get_col(x: float) -> int:
        for i in range(len(col_boundaries) - 1):
            if col_boundaries[i] <= x < col_boundaries[i + 1]:
                return i
        return len(col_boundaries) - 2

    DAY_WORD_RE = re.compile(r"^(\d{1,2})(※予約制)?$")
    SKIP_RE = re.compile(r"(お休み|建国記念|天皇誕生日|振替休日|春分|秋分)")
    SYMBOL_RE = re.compile(r"^[：～（）「」【】＊・…:~()]+$")

    def clean_title(t: str) -> str:
        # 括弧内の時刻表記を除去（数字・コロン・波線のみの括弧内容）
        t = re.sub(r"[（(]\s*[\d：:～〜\s]+\s*[）)]", "", t)
        # 単独記号トークンを除去
        tokens = [tok for tok in t.split() if not SYMBOL_RE.match(tok)]
        return " ".join(tokens).strip()

    # 日付行のy座標を検出（同行に数字トークンが5個以上）
    day_row_ys = []
    for y in yd_keys:
        row_ws = by_y[y]
        day_count = sum(1 for w in row_ws if DAY_WORD_RE.match(_z2h(w["text"])))
        if day_count >= 5:
            day_row_ys.append(y)

    events = []
    seen: set = set()

    for day_y in day_row_ys:
        day_yi = yd_keys.index(day_y)
        next_day_yi = next(
            (i for i in range(day_yi + 1, len(yd_keys)) if yd_keys[i] in day_row_ys),
            len(yd_keys)
        )
        content_ys = yd_keys[day_yi + 1:next_day_yi]

        # 日付行: {col_idx: [(x, day_num, needs_res)]}
        col_day_candidates: dict = defaultdict(list)
        for w in sorted(by_y[day_y], key=lambda w: w["x0"]):
            t = _z2h(w["text"])
            dm = DAY_WORD_RE.match(t)
            if dm:
                ci = get_col(w["x0"])
                col_day_candidates[ci].append(
                    (w["x0"], int(dm.group(1)), bool(dm.group(2)))
                )

        # 同一列に複数の日付がある場合はx座標順に左から別列に割り当て
        col_day: dict = {}
        for ci, candidates in sorted(col_day_candidates.items()):
            candidates.sort(key=lambda c: c[0])
            for k, (x, d, r) in enumerate(candidates):
                col_day[ci - len(candidates) + 1 + k] = (d, r)

        # 1桁数字の誤変換修正（「3」→「13」等）
        # 同週に1〜7の数字がある場合、前後の日付から推定
        for ci in sorted(col_day.keys()):
            d, r = col_day[ci]
            if d <= 7:
                prev_d = col_day.get(ci - 1, (0,))[0]
                nxt_d  = col_day.get(ci + 1, (0,))[0]
                if prev_d > 7:
                    est = (nxt_d - 1) if (nxt_d and nxt_d > prev_d) else prev_d + 1
                    col_day[ci] = (est, r)

        # 各列のテキストを収集
        col_texts: dict = defaultdict(list)
        for cy in content_ys:
            for w in by_y[cy]:
                t = _z2h(w["text"])
                if SYMBOL_RE.match(t):
                    continue
                col_texts[get_col(w["x0"])].append(t)

        # イベント生成
        for ci in sorted(col_day.keys()):
            day_num, needs_res = col_day[ci]
            texts = col_texts.get(ci, [])
            title = clean_title(" ".join(texts))

            if not title or SKIP_RE.search(title) or day_num in seen:
                continue
            seen.add(day_num)
            try:
                ev_date = date(year, month, day_num)
            except ValueError:
                continue

            display_title = f"★{title}" if needs_res else title
            events.append({
                "title":            display_title,
                "date_raw":         f"{year}年{month}月{day_num}日",
                "date_iso":         ev_date.strftime("%Y-%m-%d"),
                "time_raw":         "10:00〜11:30",  # 施設固定（お知らせより）
                "location":         location,
                "apply_info":       "",
                "category":         _center_guess_category(title),
                "target_age":       _center_guess_age(title),
                "url":              url,
                "source":           source,
                "needs_reservation": needs_res,
                "body_preview":     "",
            })

    events.sort(key=lambda x: x["date_iso"])
    return events


def _parse_center_yamabiko_text(
    text: str,
    year: int,
    month: int,
    source: str,
    location: str,
    url: str,
) -> list[dict]:
    """
    植木子育て支援センター「やまびこだより」形式のPDFをテキストから解析。

    PDF特徴:
      - 右側に詳細テキストブロックが明記（カレンダーより正確）
      - 形式: "予約不要 ☆イベント名☆ N日 時刻"
               "※要予約※ ☆イベント名☆ N日・M日・L日"
      - 範囲指定あり（新聞紙遊び: 9日〜13日 → 各日にエントリ）
      - 赤ちゃんタイムは同一ブロックに複数日・複数時刻

    カレンダーテーブルは補助的にしか使えないため（セルにゴミが混入しやすい）
    テキストの詳細ブロックを主軸にパースする。

    通常フォールバック順序でこのパーサーは呼ばれないが、
    _scrape_center_pdf 内で植木形式（「やまびこ」を含む）を判定して直接呼ぶ。
    """
    t = _z2h(text)
    events = []

    SKIP_TITLE_RE = re.compile(r'(フォトスポット|顔出しパネル|バースデーカード|散歩)')

    # ① 要予約イベント（「※要予約※」または「要予約：」を含むブロック）
    # カレンダー制作: "16日・17日・19日" 等の複数日
    cal_m = re.search(
        r'カレンダー制作.*?(\d+)日[・、]\s*(\d+)日[・、]\s*(\d+)\s*日',
        t, re.DOTALL
    )
    if cal_m:
        for gn in (1, 2, 3):
            d = int(cal_m.group(gn))
            try:
                ev_date = date(year, month, d)
            except ValueError:
                continue
            events.append({
                "title":            "★カレンダー作り",
                "date_raw":         f"{year}年{month}月{d}日",
                "date_iso":         ev_date.strftime("%Y-%m-%d"),
                "time_raw":         "9:15〜11:45",
                "location":         location,
                "apply_info":       "要予約（電話または来所）",
                "category":         "親子ふれあい",
                "target_age":       "0歳〜未就学",
                "url":              url,
                "source":           source,
                "needs_reservation": True,
                "body_preview":     "",
            })

    def _make_ev(day_num, title, time_str, needs_res, target_age="0歳〜未就学", apply_info=""):
        try:
            ev_date = date(year, month, day_num)
        except ValueError:
            return None
        return {
            "title":            f"★{title}" if needs_res else title,
            "date_raw":         f"{year}年{month}月{day_num}日",
            "date_iso":         ev_date.strftime("%Y-%m-%d"),
            "time_raw":         time_str,
            "location":         location,
            "apply_info":       apply_info,
            "category":         _center_guess_category(title),
            "target_age":       target_age,
            "url":              url,
            "source":           source,
            "needs_reservation": needs_res,
            "body_preview":     "",
        }

    # ② 豆まきごっこ (予約不要・単日)
    m = re.search(r'豆まきごっこ[☆\s]*(\d+)日[^A-Z\d]*AM(\d+:\d+)[〜～](\d+:\d+)', t)
    if m:
        ev = _make_ev(int(m.group(1)), "豆まきごっこ", f"{m.group(2)}〜{m.group(3)}", False)
        if ev:
            events.append(ev)

    # ③ 新聞紙遊び (予約不要・範囲指定 N日〜M日)
    m = re.search(r'新聞紙遊び[☆\s]*(\d+)日[〜～](\d+)日', t)
    if m:
        for d in range(int(m.group(1)), int(m.group(2)) + 1):
            ev = _make_ev(d, "新聞紙あそび", "9:15〜16:30", False)
            if ev:
                events.append(ev)

    # ④ 赤ちゃんタイム (予約不要・複数日)
    # "赤ちゃんタイム☆\n10日 10:30〜11:20 24日 14:30〜15:30" 形式
    # 複数日が1ブロックにまとめて記載されることが多い
    for baby_m in re.finditer(
        r'赤ちゃんタイム[☆\s\n]*(\d+)日\s+(\d+:\d+)[〜～](\d+:\d+)'
        r'(?:\s+(\d+)日\s+(\d+:\d+)[〜～](\d+:\d+))?',
        t, re.DOTALL
    ):
        for d_idx, s_idx, e_idx in [(1,2,3), (4,5,6)]:
            if not baby_m.group(d_idx):
                continue
            d = int(baby_m.group(d_idx))
            ev = _make_ev(
                d, "赤ちゃんタイム",
                f"{baby_m.group(s_idx)}〜{baby_m.group(e_idx)}",
                False, "0歳"
            )
            if ev:
                events.append(ev)

    events.sort(key=lambda x: x["date_iso"])
    return events


def _parse_center_detail_cells(tables: list[list[list]]) -> dict[int, str]:
    """
    支援センターPDFの詳細ブロック（♥イベント名♥ N日（曜）形式）から
    {day_num: target_str} を返す。

    京町台形式の複合セル（同一セルに複数イベントが並ぶ）に対応:
      ♥にこにこベビータイム♥ ♥1歳以上制作♥
      10日（火）・24日（火）  9日（月）・17日（火）
      対象：0歳の乳児と保護者 対象：1歳以上の幼児と保護者
    → day 10,24 → "0歳の乳児と保護者"
       day  9,17 → "1歳以上の幼児と保護者"
    """
    HEART_RE     = re.compile(r"[♥♡]")
    DATE_IN_LINE = re.compile(r"(\d{1,2})日[（(][月火水木金土日祝][）)]")
    TARGET_RE    = re.compile(r"対象[：:]\s*([^\n対]{1,50})")

    def _clean_tgt(t: str) -> str:
        t = t.strip()
        t = re.sub(r"\s+", " ", t)
        # "0歳の乳児と保護者 対象:..." → 最初のパートのみ
        m = re.search(r"(.+?)\s*(?=対象[：:])", t)
        if m:
            t = m.group(1).strip()
        t = re.sub(r"対象\s+\d+.+$", "", t).strip()
        return t[:50]

    target_map: dict[int, str] = {}

    for table in tables:
        for row in table:
            for cell in row:
                if not cell:
                    continue
                t = _z2h(cell)
                lines = t.splitlines()

                # ♥ で分割してイベント名候補を収集
                parts = HEART_RE.split(t)
                ev_names = [
                    p.strip() for i, p in enumerate(parts)
                    if (i % 2 == 1) and p.strip() and len(p.strip()) >= 2
                    and not re.match(r"^\d", p.strip())
                ]
                if not ev_names:
                    continue

                all_targets = [_clean_tgt(m) for m in TARGET_RE.findall(t)]

                # 日付を含む行を探してグループ化
                for line in lines:
                    if not DATE_IN_LINE.search(line) or "対象" in line:
                        continue
                    # 「N日（曜）」の末尾 + 空白 + 数字 の位置で分割
                    # 例: "10日（火）・24日（火） 9日（月）・17日（火）"
                    groups = re.split(r"(?<=）)\s+(?=\d)", line.strip())
                    valid = [(gi, [int(d) for d in DATE_IN_LINE.findall(g)])
                             for gi, g in enumerate(groups)
                             if DATE_IN_LINE.search(g)]

                    for gi, days in valid:
                        ev  = ev_names[gi] if gi < len(ev_names) else ev_names[-1]
                        tgt = all_targets[gi] if gi < len(all_targets) else (
                              all_targets[0] if all_targets else "")
                        for d in days:
                            target_map[d] = tgt

                # 日付が分割できないケース（単一グループ）
                if not any(DATE_IN_LINE.search(l) and "対象" not in l for l in lines):
                    all_days = [int(d) for d in DATE_IN_LINE.findall(t)]
                    if all_days and all_targets:
                        for d in all_days:
                            target_map.setdefault(d, all_targets[0])

    return target_map


def _extract_text_time_map(
    text: str, year: int, month: int
) -> dict[int, str]:
    """
    テキストから日付→時刻のマップを抽出する。

    対応パターン:
      ① 「✿N日（曜）[①]HH:MM〜[ ②HH:MM〜]」形式（西里・あゆみ形式）
         例: "✿19日（木）①10:30～ ②14:30～" → {19: "①10:30〜②14:30〜"}
      ② 「N日(曜) HH:MM〜HH:MM」形式（清水形式）
         例: "お話会 16日(月）11：00～11：30" → {16: "11:00〜11:30"}
      ③ 「N時頃〜」形式
         例: "26日（木）１０時頃〜" → {26: "10:00〜"}
    """
    t = _z2h(text)
    result: dict[int, str] = {}

    # パターン①: ✿N日（曜）[①]HH:MM〜[ ②HH:MM〜]
    PAT_HANA = re.compile(
        r"✿\s*(\d{1,2})\s*日[^0-9✿]{0,8}?"
        r"[①]?\s*(\d{1,2}):(\d{2})\s*[〜～]"
        r"(?:\s*[②]\s*(\d{1,2}):(\d{2})\s*[〜～])?"
    )
    for m in PAT_HANA.finditer(t):
        day = int(m.group(1))
        if not (1 <= day <= 31):
            continue
        t1 = f"{int(m.group(2)):02d}:{m.group(3)}"
        if m.group(4):  # ②あり
            t2 = f"{int(m.group(4)):02d}:{m.group(5)}"
            result[day] = f"①{t1}〜 ②{t2}〜"
        else:
            result[day] = f"{t1}〜"

    # パターン②: N日(曜) HH:MM〜HH:MM（終了時刻あり）
    PAT1 = re.compile(
        r"(\d{1,2})\s*日[（(（\s\w]*[）)）]?\s*"
        r"(\d{1,2}):(\d{2})\s*[〜～]\s*(\d{1,2}):(\d{2})"
    )
    for m in PAT1.finditer(t):
        day = int(m.group(1))
        if day in result or not (1 <= day <= 31):
            continue
        result[day] = (
            f"{int(m.group(2)):02d}:{m.group(3)}"
            f"〜{int(m.group(4)):02d}:{m.group(5)}"
        )

    # パターン③: N時頃〜 → HH:00〜
    PAT2 = re.compile(
        r"(\d{1,2})\s*日[（(（\s\w]*[）)）]?\s*(\d{1,2})\s*時頃?[〜～]"
    )
    for m in PAT2.finditer(t):
        day = int(m.group(1))
        if day not in result and 1 <= day <= 31:
            result[day] = f"{int(m.group(2)):02d}:00〜"

    return result


def _extract_text_no_reservation_days(text: str) -> set[int]:
    """
    テキストから「予約不要」と明記されたイベントの日付セットを返す。

    all_reserved=True の施設でも個別イベントが予約不要の場合に使用。
    例: "✿3日（火）10:00～豆まき会見学 …（4組程度・予約不要）" → {3}
    """
    t = _z2h(text)
    result: set[int] = set()

    # ✿ブロックごとに「予約不要」を含むものを抽出
    blocks = re.split(r"✿", t)
    for block in blocks:
        if "予約不要" in block:
            m = re.match(r"\s*(\d{1,2})\s*日", block)
            if m:
                day = int(m.group(1))
                if 1 <= day <= 31:
                    result.add(day)

    # 「N日（曜）〜…予約不要」形式（✿なし）
    PAT = re.compile(r"(\d{1,2})\s*日[^✿\n]{1,80}?予約不要")
    for m in PAT.finditer(t):
        day = int(m.group(1))
        if 1 <= day <= 31:
            result.add(day)

    return result


def _parse_ayumi_notice(
    text: str, year: int, month: int,
    source: str, location: str, url: str
) -> list[dict]:
    """
    あゆみ子どもセンター専用パーサー。

    PDF 2ページ目の「☆お知らせ☆」ブロックから
    「◎N月N日（曜）[・N日（曜）] HH:MM〜HH:MM [※要予約…]\n タイトル行」
    形式のイベントを抽出する。

    カレンダーテーブルには日付が存在しないため
    テキストブロック解析のみで対応する。

    翌月以降の◎ブロック（例: ◎3月12日）は対象月のみ採用する。
    """
    from datetime import date as _date

    notice_idx = text.find("お知らせ")
    notice_text = text[notice_idx:] if notice_idx >= 0 else text

    events = []
    blocks = re.split(r"(?=◎\d{1,2}月)", notice_text)

    for block in blocks:
        date_m = re.match(r"◎(\d{1,2})月(\d{1,2})日", block)
        if not date_m:
            continue
        ev_month, ev_day = int(date_m.group(1)), int(date_m.group(2))
        if ev_month != month:
            continue  # 翌月以降はスキップ

        # 「・N日」追加日付（例: 12日・19日→[12,19]）
        days = [ev_day]
        extra_days = re.findall(r"[・,、]\s*(\d{1,2})日", block[:40])
        days += [int(d) for d in extra_days]

        # 時刻: ◎行〜翌◎行まで（ブロック先頭3行）から取得
        first_lines = "\n".join(block.split("\n")[:3])
        t_m = re.search(r"(\d{1,2}:\d{2})\s*[〜～]\s*(\d{1,2}:\d{2})", first_lines)
        if t_m:
            time_str = f"{t_m.group(1)}〜{t_m.group(2)}"
        else:
            t_m2 = re.search(r"(\d{1,2}:\d{2})\s*[〜～]", first_lines)
            time_str = f"{t_m2.group(1)}〜" if t_m2 else "10:00〜"

        # 要予約
        needs_res = "要予約" in block

        # タイトル: 『〜』または「〜」を優先、なければ後続行から
        title_m = re.search(r"[『「]([^』」\n]{2,30})[』」]", block)
        if title_m:
            title = title_m.group(1).strip()
        else:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            title = lines[1] if len(lines) > 1 else lines[0]
            # ◎行に混在してしまったキーワードを除去
            title = re.sub(r"^◎\d+月\d+日.*", "", title).strip()

        if not title:
            continue

        display_title = f"★{title}" if needs_res else title
        cat = _center_guess_category(title)
        target_age = _center_guess_age(title)

        for day in days:
            try:
                ev_date = _date(year, ev_month, day)
            except ValueError:
                continue
            events.append({
                "title":            display_title,
                "date_raw":         f"{year}年{ev_month}月{day}日",
                "date_iso":         ev_date.strftime("%Y-%m-%d"),
                "time_raw":         time_str,
                "location":         location,
                "apply_info":       "要予約（電話）" if needs_res else "",
                "category":         cat,
                "target_age":       target_age,
                "url":              url,
                "source":           source,
                "needs_reservation": needs_res,
                "body_preview":     "",
            })

    events.sort(key=lambda x: x["date_iso"])
    return events


def _scrape_center_pdf(pdf_bytes: bytes, cdef: dict) -> list[dict]:
    """
    子育て支援センター1施設のPDFを汎用的に解析してイベントリストを返す。

    解析戦略:
      1. テキスト抽出で年月を特定
      2. スキャンPDF判定（テキスト < 50文字 → 警告して空返却）
      3. 詳細ブロック（♥イベント名♥ N日形式）から対象情報を収集
      4. 7列または17列カレンダーテーブルを探してパース
         - 17列形式（京町台等）: use_offset=True で自動検出
         - テキスト全体に「事前予約制」が含まれる場合は all_reserved=True
      5. テーブルからイベントが取得できなければテキストベースで解析
    """
    source   = cdef["source"]
    location = cdef["location"]
    url      = cdef["page_url"]
    now      = datetime.now()

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page     = pdf.pages[0]
            text     = page.extract_text() or ""
            tables   = page.extract_tables() or []
            metadata = pdf.metadata or {}
            words    = page.extract_words() or []
    except Exception as ex:
        logger.error(f"{source}: PDF解析エラー {ex}")
        return []

    # スキャンPDF判定
    if len(text.strip()) < 50:
        logger.warning(
            f"{source}: テキスト量が極端に少ない（{len(text.strip())}文字）。"
            f"スキャンPDFの可能性。0件を返します。"
        )
        return []

    year, month = _center_get_year_month(text, metadata, now.year, now.month)
    logger.info(f"{source}: {year}年{month}月 解析開始")

    # ── あゆみ子どもセンター形式: 2ページ構成、お知らせ欄から抽出 ─────────
    # 1ページ目: コラム（年月あり）、2ページ目: カレンダー＋お知らせ
    # カレンダーに日付がなくテーブルから取れないため、
    # ☆お知らせ☆の「◎N月N日」ブロックを直接解析する
    if re.search(r"すくすくめぇる|あゆみ子どもセンター|武蔵ヶ丘", text):
        logger.info(f"{source}: あゆみ形式を検出 → 2ページお知らせブロックパーサーを使用")
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as _pdf:
                _p1_text = _z2h(_pdf.pages[1].extract_text() or "") if len(_pdf.pages) > 1 else ""
        except Exception:
            _p1_text = ""
        events = _parse_ayumi_notice(
            _p1_text, year, month, source, location, url
        )
        logger.info(f"{source}: あゆみパース {len(events)} 件")
        return events

    # ── やまびこだより形式（植木）: テキスト詳細ブロック優先 ──────────────
    # 右側に「予約不要 ☆イベント名☆ N日」形式の詳細が明記されている
    if re.search(r"やまびこだより|やまびこ\s*だより", text):
        logger.info(f"{source}: やまびこ形式を検出 → テキストベースパーサーを使用")
        events = _parse_center_yamabiko_text(
            text, year, month, source, location, url
        )
        logger.info(f"{source}: やまびこパース {len(events)} 件")
        return events

    # 全件予約制判定（「利用はすべて事前予約制」「事前予約制です」等のテキストから）
    all_reserved = bool(re.search(
        r"(すべて事前予約制|利用.*予約制|全て.*予約制|事前予約制です|利用.*予約が必要)",
        text
    ))

    # 詳細ブロックから対象情報を収集（全テーブルを渡す）
    detail_target_map = _parse_center_detail_cells(tables) if tables else {}
    if detail_target_map:
        logger.info(f"{source}: 詳細ブロックから {len(detail_target_map)} 日分の対象情報を取得")

    # カレンダーテーブルを探す（7列・17列両対応）
    events: list[dict] = []
    cal_candidates = [
        t for t in tables
        if t and len(t) >= 4 and len(t[0]) >= 7
        and any(
            c and c.strip() in _WEEKDAYS_7
            for row in t[:3]
            for c in row
        )
    ]

    for cal_table in cal_candidates:
        ev = _parse_center_calendar_table(
            cal_table, year, month, source, location, url,
            detail_target_map=detail_target_map,
            all_reserved=all_reserved,
        )
        if ev:
            events.extend(ev)
            logger.info(f"{source}: カレンダーパース {len(ev)} 件")
            break

    # カレンダー未発見 or 0件 → 5列週間形式を試行（さくらっこ形式）
    if not events:
        logger.info(f"{source}: 5列週間カレンダーパーサーを試行")
        events = _parse_center_5col_weekly(
            tables, text, year, month, source, location, url
        )
        if events:
            logger.info(f"{source}: 5列パース {len(events)} 件")

    # 次に☆形式詳細ブロック＋カレンダーフォールバック（幸田形式）
    if not events:
        logger.info(f"{source}: ☆形式詳細ブロックパーサーを試行")
        events = _parse_center_star_detail_calendar(
            tables, text, year, month, source, location, url
        )
        if events:
            logger.info(f"{source}: ☆形式パース {len(events)} 件")

    # word座標ベース（だいいち形式: 7列・日付+内容が同セル）
    if not events:
        logger.info(f"{source}: word座標ベースパーサーを試行")
        events = _parse_center_word_position_cal(
            words, year, month, source, location, url
        )
        if events:
            logger.info(f"{source}: word座標パース {len(events)} 件")

    # それでも0件 → テキストベースにフォールバック
    if not events:
        logger.info(f"{source}: テキストベース解析にフォールバック")
        events = _parse_center_text_list(text, year, month, source, location, url)
        logger.info(f"{source}: テキストパース {len(events)} 件")

    # 全パーサーで0件の場合: 構造診断情報をprintして次回の調査に役立てる
    if not events:
        print(f"  ⚠️ {source}: 全パーサーで0件 → 構造診断:")
        print(f"     テキスト文字数: {len(text.strip())}")
        print(f"     テーブル数: {len(tables)}")
        for i, t in enumerate(tables[:4]):
            cols = len(t[0]) if t else 0
            print(f"     TABLE[{i}]: {len(t)}行×{cols}列  先頭行: {t[0][:6] if t else []}")
        print(f"     テキスト先頭150文字: {text[:150].replace(chr(10), ' ')}")

    events.sort(key=lambda x: x["date_iso"])

    # ── テキストから時刻を後補完（カレンダーセルに時刻がない施設向け） ──
    text_time_map = _extract_text_time_map(text, year, month)
    no_res_days   = _extract_text_no_reservation_days(text)

    if text_time_map or no_res_days:
        logger.info(
            f"{source}: 時刻補完マップ {len(text_time_map)} 件 / 予約不要日 {no_res_days}"
        )
        default_times = {"10:30〜11:00", "10:00〜11:00", "10:30〜", "10:00〜"}
        for ev in events:
            try:
                day_num = int(ev["date_iso"].split("-")[2])
            except (IndexError, ValueError):
                continue

            # 時刻補完（デフォルト時刻のままなら上書き）
            if day_num in text_time_map:
                if ev.get("time_raw") in default_times or not ev.get("time_raw"):
                    ev["time_raw"] = text_time_map[day_num]

            # 予約不要フラグの上書き（all_reserved=True でも個別に不要な場合）
            if day_num in no_res_days and ev.get("needs_reservation"):
                ev["needs_reservation"] = False
                # タイトルの ★ プレフィックスを除去
                if ev["title"].startswith("★"):
                    ev["title"] = ev["title"][1:].strip()

    logger.info(f"{source}: 合計 {len(events)} 件取得")
    return events


def scrape_all_centers(
    pdf_map: dict[str, bytes] | None = None,
    pw_page=None,
) -> list[dict]:
    """
    子育て支援センター18施設のイベントを共通形式で取得して返す。

    Args:
        pdf_map: {source名: PDFバイト列} 手動渡し用（省略可）
        pw_page: Playwrightページ（動的取得用。None なら requests を使用）

    Returns:
        共通形式イベントリスト（date_iso, title, location, source 等）
    """
    print("\n=== ソースE: 子育て支援センター18施設（PDF解析）===")
    all_events: list[dict] = []
    pdf_map = pdf_map or {}

    # ── 統括ページから各施設の最新PDF URLを取得 ──────────────
    pdf_url_map: dict[str, str] = {}
    print(f"  統括ページ取得: {CENTER_LIST_URL}")

    try:
        if pw_page:
            pw_page.goto(CENTER_LIST_URL, wait_until="domcontentloaded", timeout=60000)
            pw_page.wait_for_timeout(2000)
            html = pw_page.content()
        else:
            html = fetch_html(CENTER_LIST_URL)

        if html:
            pdf_url_map = _fetch_center_pdf_urls(html)
            print(f"  PDFリンク確認数: {len(pdf_url_map)} 施設")
        else:
            print("  ⚠️ 統括ページの取得に失敗")
    except Exception as ex:
        print(f"  ⚠️ 統括ページ取得エラー: {ex}")

    # ── 各施設を処理 ──────────────────────────────────────
    for cdef in CENTER_DEFS:
        source   = cdef["source"]
        list_key = cdef["list_key"]

        # PDFバイト列の決定（手動 > URL取得）
        if source in pdf_map:
            pdf_bytes = pdf_map[source]
            print(f"  {source}: 手動PDF使用")
        elif list_key in pdf_url_map:
            pdf_url = pdf_url_map[list_key]
            print(f"  {source}: PDF取得中 {pdf_url}")
            pdf_bytes = _fetch_pdf_bytes(pdf_url)
            if not pdf_bytes:
                print(f"  {source}: ❌ PDF取得失敗")
                continue
            print(f"  {source}: ✅ ({len(pdf_bytes):,} bytes)")
        else:
            print(f"  {source}: PDFリンクが見つかりませんでした")
            continue

        try:
            events = _scrape_center_pdf(pdf_bytes, cdef)
            all_events.extend(events)
            print(f"  {source}: {len(events)} 件")
        except Exception as ex:
            logger.error(f"{source}: 解析エラー {ex}", exc_info=True)
            print(f"  {source}: ❌ 解析エラー {ex}")

    print(f"  ソースE 合計: {len(all_events)} 件")
    return all_events


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
def fetch_html_playwright(pw_page, url, wait_text=None, timeout=20000, retries=3):
    """Playwrightでページを開きJS描画後のHTMLを返す（リトライあり）"""
    print(f"  GET(PW) {url}")
    for attempt in range(1, retries + 1):
        try:
            # networkidle は重いサイトでタイムアウトしやすいため
            # domcontentloaded に落として速度優先、その後テキスト待機で補完
            pw_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if wait_text:
                try:
                    pw_page.wait_for_function(
                        f"document.body.innerText.includes('{wait_text}')",
                        timeout=timeout,
                    )
                except Exception:
                    print(f"  ⚠️ 待機テキスト「{wait_text}」が見つかりません（続行）")
            pw_page.wait_for_timeout(2000)
            return pw_page.content()
        except Exception as e:
            print(f"  ⚠️ 試行{attempt}/{retries} 失敗: {e}")
            if attempt < retries:
                print(f"  　 {3 * attempt}秒後にリトライ...")
                time.sleep(3 * attempt)
    print(f"  ❌ {retries}回試行しましたが取得できませんでした: {url}")
    return ""


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

    # ソースD・E: 児童館 + 子育て支援センター（PDF解析）- Playwrightページを共有
    try:
        with sync_playwright() as p2:
            browser2 = p2.chromium.launch(headless=True)
            pw_page2 = browser2.new_page()
            pw_page2.set_extra_http_headers({"Accept-Language": "ja,en;q=0.9"})

            # ソースD: 各児童館（既存）
            all_events.extend(scrape_all_halls_adapted(pw_page=pw_page2))

            # ソースE: 子育て支援センター18施設（新規）
            try:
                all_events.extend(scrape_all_centers(pw_page=pw_page2))
            except Exception as e_inner:
                print(f"ソースEエラー: {e_inner}")

            browser2.close()
    except Exception as e:
        print(f"ソースD/Eエラー: {e}")

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


def _load_cached_events() -> list[dict]:
    """前回保存した events.json を読み込んで返す。なければ空リスト。"""
    cache_path = Path("docs/events.json")
    if not cache_path.exists():
        return []
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events", [])
        print(f"  キャッシュ読み込み: {len(events)} 件 (更新日時: {data.get('updated_at', '不明')})")
        return events
    except Exception as e:
        print(f"  キャッシュ読み込み失敗: {e}")
        return []


def _merge_with_cache(new_events: list[dict], cached_events: list[dict],
                      min_count: int = 150) -> list[dict]:
    """
    新規取得件数が少なすぎる場合（サーバー障害等）にキャッシュとマージして返す。

    戦略:
      - source 単位で新規取得が 0 件 → そのソースの過去データで補完
      - ただし日付が過去（本日より前）のイベントは除外
      - 全体が min_count 件以上あれば新規データのみを返す
    """
    if len(new_events) >= min_count:
        return new_events

    today_iso = datetime.now().strftime("%Y-%m-%d")
    print(f"\n⚠️ 取得件数が少ない（{len(new_events)}件 < {min_count}件）→ キャッシュで補完")

    # 新規取得に含まれるソース一覧
    new_sources = {e["source"] for e in new_events}

    # キャッシュから「新規取得できなかったソース」の「未来イベント」を抽出
    cache_supplement = [
        e for e in cached_events
        if e["source"] not in new_sources
        and (e.get("date_iso") or "9999") >= today_iso
    ]
    print(f"  補完件数: {len(cache_supplement)} 件（{len(new_sources)} ソースは新規取得済み）")

    merged = new_events + cache_supplement
    merged.sort(key=lambda e: e.get("date_iso") or "9999")
    return merged


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
    # </script> がJSON内に含まれるとXSSになるため無効化する
    json_str = json.dumps(events_data, ensure_ascii=False).replace("</", "<\\/")
    new_block = f"{start_marker}\nconst INLINE_EVENTS = {json_str};\n{end_marker}"
    html = html[:s] + new_block + html[e + len(end_marker):]
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"index.html更新完了 ({events_data['count']}件埋め込み)")


if __name__ == "__main__":
    # 実行前に既存キャッシュを読み込んでおく（障害時の補完用）
    cached = _load_cached_events()

    events = scrape()

    # 取得件数が著しく少ない場合（サーバー障害等）はキャッシュで補完
    events = _merge_with_cache(events, cached)

    save(events)
    update_html({
        "updated_at": datetime.now().isoformat(),
        "count": len(events),
        "events": events,
    })
