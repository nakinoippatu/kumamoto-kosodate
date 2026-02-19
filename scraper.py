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
    for k, v in CATEGORY_MAP.items():
        if k in t: return v
    return "その他"

def guess_age(t):
    for k, v in AGE_MAP.items():
        if k in t: return v
    return "指定なし"

def to_iso(s):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}" if m else ""

def find_kidate(a_tag):
    """
    aタグの近くから「期日XXXX年X月X日」を探す。
    パターン1: 親要素のテキスト内（li構造）
    パターン2: 親の次の兄弟要素（p構造）
    """
    parent = a_tag.parent
    if parent:
        # パターン1: liなど親要素のテキスト全体に期日がある
        parent_text = parent.get_text(" ", strip=True)
        m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", parent_text)
        if m:
            return m.group(1)
        # パターン2: 次の兄弟p要素に期日がある
        nxt = parent.find_next_sibling()
        for _ in range(3):
            if nxt is None: break
            text = nxt.get_text(" ", strip=True)
            m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
            if m:
                return m.group(1)
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

def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#maincont") or soup.find("main") or soup

    # 全aタグを走査し、「期日」が取れたものだけ採用する
    # これにより「新着情報」セクション（期日なし）を自動除外できる
    all_a = main.find_all("a", href=re.compile(r"page\d+\.html"))
    print(f"  page*.html aタグ数: {len(all_a)}")

    seen_urls = set()
    events = []
    for a in all_a:
        url = a.get("href", "")
        # 相対URLを絶対URLに変換
        if url.startswith("/"):
            url = BASE_URL + url
        title = a.get_text(strip=True)
        if not title:
            continue

        date_raw = find_kidate(a)

        # 期日がないものはスキップ（新着情報セクション・常設ページ）
        if not date_raw:
            continue

        # 同じURLの重複は除外
        if url in seen_urls:
            continue
        seen_urls.add(url)

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

    print(f"  取得: {len(events)} 件")
    return events

def scrape():
    print("=== 熊本市 子育て支援イベント スクレイピング開始 ===")
    all_events = []

    html = fetch_html(1)
    # 全件数をログに出力
    total_m = re.search(r"関する記事.*?全(\d+)件", html, re.DOTALL)
    if total_m:
        print(f"  サイト表示総件数: {total_m.group(1)} 件")

    events = parse_page(html)
    all_events.extend(events)

    for page in range(2, 11):
        time.sleep(1)
        try:
            html = fetch_html(page)
            events = parse_page(html)
            if not events:
                print(f"  {page}ページ目: 新規なし → 終了")
                break
            existing = {e["url"] for e in all_events}
            new = [e for e in events if e["url"] not in existing]
            if not new:
                print(f"  {page}ページ目: 重複のみ → 終了")
                break
            all_events.extend(new)
        except Exception as ex:
            print(f"  {page}ページ目エラー: {ex}")
            break

    print(f"=== 合計取得: {len(all_events)} 件 ===")
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
