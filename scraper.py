"""
熊本市 子育て支援 統合スクレイパー
ソース:
  A) 子育てナビ（kumamoto-kekkon-kosodate.jp） Playwright使用
  B) 総合子育て支援センター（city.kumamoto.jp）  requests使用
  C) こども文化会館（kodomobunka.jp）            requests使用
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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


def scrape_kosodate():
    print("\n=== ソースA: 子育てナビ ===")
    all_events = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pw_page = browser.new_page()
        pw_page.set_extra_http_headers({"Accept-Language": "ja,en;q=0.9"})
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
        browser.close()
    print(f"  ソースA 合計: {len(all_events)} 件")
    return all_events


# ─────────────────────────────────────────
# ソースB: 総合子育て支援センター（requests）
# ─────────────────────────────────────────
def scrape_sogo_center():
    print("\n=== ソースB: 総合子育て支援センター ===")
    html = fetch_html(URL_B)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now()

    # 「イベント情報」h2を探す
    event_h2 = None
    for h2 in soup.find_all("h2"):
        if "イベント情報" in h2.get_text():
            event_h2 = h2
            break
    if not event_h2:
        print("  イベント情報セクションが見つかりません")
        return []

    events = []
    current_title = None
    current = event_h2.find_next_sibling()

    while current:
        if current.name == "h2":
            break
        if current.name == "h3":
            current_title = current.get_text(strip=True)
        elif current.name == "table" and current_title:
            fields = {}
            for tr in current.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    key = tds[0].get_text(strip=True)
                    val = tds[1].get_text(" ", strip=True)
                    fields[key] = val

            date_raw = fields.get("■期日", "")
            if not date_raw:
                current = current.find_next_sibling()
                current_title = None
                continue

            time_text = fields.get("■時間", "")
            location = fields.get("■場所", LOCATION_B)
            target_text = fields.get("■対象", "")
            apply_text = fields.get("■申込み", "")
            needs_res = is_reservation_required(apply_text)

            date_iso = normalize_date(date_raw, base_year=now.year, base_month=now.month)
            time_norm = normalize_time(time_text)
            target_age = guess_age(target_text) if target_text else guess_age(current_title)

            if date_iso:
                ev = make_event(
                    title=current_title,
                    date_raw=date_raw,
                    date_iso=date_iso,
                    time_raw=time_norm,
                    location=location,
                    apply_info=apply_text[:100],
                    category=guess_category(current_title),
                    target_age=target_age,
                    url=URL_B,
                    source=SOURCE_B,
                    needs_reservation=needs_res,
                )
                events.append(ev)
                print(f"  OK: {current_title[:30]} / {date_raw} / {'★要予約' if needs_res else '予約不要'}")
            current_title = None

        current = current.find_next_sibling()

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

    try:
        all_events.extend(scrape_kosodate())
    except Exception as e:
        print(f"ソースAエラー: {e}")

    try:
        all_events.extend(scrape_sogo_center())
    except Exception as e:
        print(f"ソースBエラー: {e}")

    try:
        all_events.extend(scrape_kodomobunka())
    except Exception as e:
        print(f"ソースCエラー: {e}")

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
