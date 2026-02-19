"""
熊本市 子育て支援 イベント・講座 スクレイパー
"""

import json
import re
import sys
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
    "離乳食":"食育・栄養","食育":"食育・栄養","栄養":"食育・栄養",
    "健康":"健康・医療","歯":"健康・医療","医療":"健康・医療",
    "発達":"発達・育児相談","相談":"発達・育児相談","育児":"発達・育児相談",
    "パパ":"父親・家族支援",
    "ふれあい":"親子ふれあい","遊び":"親子ふれあい","リトミック":"親子ふれあい",
    "マッサージ":"親子ふれあい","絵本":"親子ふれあい","体操":"親子ふれあい",
    "ひとり親":"ひとり親支援",
    "産前":"産前・産後","産後":"産前・産後","骨盤":"産前・産後",
    "ヨガ":"産前・産後","ピラティス":"産前・産後","マタニティ":"産前・産後",
    "お金":"生活支援","メイク":"生活支援","カラー":"生活支援",
}
AGE_MAP = {
    "妊婦":"妊娠中","妊娠中":"妊娠中",
    "産後":"0歳","ベビー":"0歳","0歳":"0歳","乳児":"0歳",
    "1歳":"1〜2歳","2歳":"1〜2歳","１歳":"1〜2歳","２歳":"1〜2歳",
    "3歳":"3〜5歳","4歳":"3〜5歳","5歳":"3〜5歳","未就学":"3〜5歳","幼児":"3〜5歳",
    "小学":"小学生以上","乳幼児":"0歳〜未就学",
}

def guess_category(t):
    for k,v in CATEGORY_MAP.items():
        if k in t: return v
    return "その他"

def guess_age(t):
    for k,v in AGE_MAP.items():
        if k in t: return v
    return "指定なし"

def to_iso(s):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}" if m else ""

def find_kidate(a_tag):
    parent = a_tag.parent
    if not parent: return ""
    nxt = parent.find_next_sibling()
    for _ in range(3):
        if nxt is None: break
        text = nxt.get_text(" ", strip=True)
        if "期日" in text:
            dates = re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", text)
            if dates: return dates[0]
        nxt = nxt.find_next_sibling()
    return ""

def fetch_html(page=1):
    url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
    print(f"  GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    print(f"  → ステータス: {resp.status_code}, サイズ: {len(resp.text)} 文字")
    return resp.text

def parse_page(html, page_num=1):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#maincont") or soup.find("main") or soup

    # ページ上の全件数表示を確認
    total_match = re.search(r"全(\d+)件", html)
    if total_match and page_num == 1:
        print(f"  サイト表示件数: 全{total_match.group(1)}件")

    # aタグの数をデバッグ出力
    all_a = main.find_all("a")
    page_a = main.find_all("a", href=re.compile(r"/page\d+\.html"))
    print(f"  aタグ総数: {len(all_a)}, /pageXXXX.html 形式: {len(page_a)}")

    events = []
    seen = set()
    for a in page_a:
        title = a.get_text(strip=True)
        href = a.get("href","")
        url = href if href.startswith("http") else BASE_URL + href
        if url in seen: continue
        seen.add(url)
        date_raw = find_kidate(a)
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

def scrape():
    print("=== スクレイピング開始 ===")
    all_events = []

    html = fetch_html(1)
    events = parse_page(html, 1)
    all_events.extend(events)
    print(f"  1ページ目取得: {len(events)} 件")

    for page in range(2, 11):
        time.sleep(1)
        try:
            html = fetch_html(page)
            events = parse_page(html, page)
            if not events: break
            existing = {e["url"] for e in all_events}
            new = [e for e in events if e["url"] not in existing]
            if not new: break
            all_events.extend(new)
            print(f"  {page}ページ目取得: {len(new)} 件")
        except Exception as ex:
            print(f"  {page}ページ目エラー: {ex}")
            break

    print(f"=== 合計取得: {len(all_events)} 件 ===")

    # 0件でも終了コードを0にして続行（pushさせるため）
    if len(all_events) == 0:
        print("警告: 0件でした。サイト構造が変わった可能性があります。")

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
