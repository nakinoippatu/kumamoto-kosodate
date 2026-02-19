"""
熊本市 子育て支援 イベント・講座 スクレイパー
取得元: 熊本市こども・子育て応援サイト
出力: docs/events.json
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.kumamoto-kekkon-kosodate.jp"
LIST_URL = f"{BASE_URL}/hpkiji/pub/List.aspx?c_id=3&class_set_id=1&class_id=523"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KumamotoKosodate-Bot/1.0)",
    "Accept-Language": "ja,en;q=0.9",
}

CATEGORY_MAP = {
    "離乳食": "食育・栄養", "食育": "食育・栄養", "栄養": "食育・栄養",
    "健康": "健康・医療", "歯": "健康・医療", "医療": "健康・医療",
    "発達": "発達・育児相談", "相談": "発達・育児相談", "育児": "発達・育児相談",
    "パパ": "父親・家族支援",
    "ふれあい": "親子ふれあい", "遊び": "親子ふれあい", "手遊び": "親子ふれあい",
    "絵本": "親子ふれあい", "体操": "親子ふれあい", "リトミック": "親子ふれあい",
    "マッサージ": "親子ふれあい", "工作": "親子ふれあい",
    "ひとり親": "ひとり親支援",
    "産前": "産前・産後", "産後": "産前・産後", "マタニティ": "産前・産後",
    "妊娠": "産前・産後", "妊婦": "産前・産後", "骨盤": "産前・産後",
    "ヨガ": "産前・産後", "ピラティス": "産前・産後",
    "お金": "生活支援", "メイク": "生活支援", "カラー": "生活支援",
}

AGE_MAP = {
    "妊娠中": "妊娠中", "妊婦": "妊娠中",
    "産後": "0歳", "0歳": "0歳", "０歳": "0歳", "乳児": "0歳", "新生児": "0歳", "ベビー": "0歳",
    "1歳": "1〜2歳", "１歳": "1〜2歳", "2歳": "1〜2歳", "２歳": "1〜2歳",
    "3歳": "3〜5歳", "３歳": "3〜5歳", "4歳": "3〜5歳", "5歳": "3〜5歳",
    "未就学": "3〜5歳", "幼児": "3〜5歳",
    "小学": "小学生以上",
    "乳幼児": "0歳〜未就学",
}


def guess_category(title):
    for keyword, category in CATEGORY_MAP.items():
        if keyword in title:
            return category
    return "その他"


def guess_age(title):
    for keyword, age in AGE_MAP.items():
        if keyword in title:
            return age
    return "指定なし"


def to_iso(date_jp):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_jp)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return ""


def find_kidate(a_tag):
    """aタグの親の次の兄弟要素から「期日」テキストを探す"""
    parent = a_tag.parent
    if parent is None:
        return ""
    next_el = parent.find_next_sibling()
    for _ in range(3):
        if next_el is None:
            break
        text = next_el.get_text(" ", strip=True)
        if "期日" in text:
            dates = re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", text)
            if dates:
                return dates[0]  # 開始日
        next_el = next_el.find_next_sibling()
    return ""


def fetch_html(page=1):
    url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#maincont") or soup.find("main") or soup
    events = []

    for a_tag in main.find_all("a", href=re.compile(r"/page\d+\.html")):
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        url = href if href.startswith("http") else BASE_URL + href
        date_raw = find_kidate(a_tag)

        events.append({
            "title": title,
            "date_raw": date_raw,
            "date_iso": to_iso(date_raw),
            "time_raw": "",
            "location": "",
            "apply_info": "",
            "category": guess_category(title),
            "target_age": guess_age(title),
            "url": url,
            "body_preview": "",
        })
    return events


def total_count(html):
    m = re.search(r"全(\d+)件", html)
    return int(m.group(1)) if m else 0


def scrape():
    print("=== 熊本市 子育て支援イベント スクレイピング開始 ===")
    all_events = []

    html = fetch_html(1)
    total = total_count(html)
    print(f"合計件数（サイト表示）: {total} 件")

    events = parse_page(html)
    all_events.extend(events)
    print(f"1ページ目: {len(events)} 件取得")

    for page in range(2, 11):
        if len(all_events) >= total:
            break
        time.sleep(1)
        try:
            html = fetch_html(page)
            events = parse_page(html)
            if not events:
                break
            existing = {e["url"] for e in all_events}
            new = [e for e in events if e["url"] not in existing]
            if not new:
                break
            all_events.extend(new)
            print(f"{page}ページ目: {len(new)} 件取得")
        except Exception as ex:
            print(f"{page}ページ目でエラー: {ex}")
            break

    print(f"\n取得完了: {len(all_events)} 件")
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


if __name__ == "__main__":
    events = scrape()
    save(events)
