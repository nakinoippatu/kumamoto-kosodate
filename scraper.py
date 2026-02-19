"""
熊本市 子育て支援 イベント・講座 スクレイパー
Playwright使用（JavaScript描画対応）
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.kumamoto-kekkon-kosodate.jp"
LIST_URL = f"{BASE_URL}/hpkiji/pub/List.aspx?c_id=3&class_set_id=1&class_id=523"

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
    parent = a_tag.parent
    if parent:
        parent_text = parent.get_text(" ", strip=True)
        m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", parent_text)
        if m: return m.group(1)
        nxt = parent.find_next_sibling()
        for _ in range(3):
            if nxt is None: break
            text = nxt.get_text(" ", strip=True)
            m = re.search(r"期日\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
            if m: return m.group(1)
            nxt = nxt.find_next_sibling()
    return ""

def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")

    # 全aタグを対象（要素を絞らない）
    all_a = soup.find_all("a", href=re.compile(r"page\d+\.html"))
    print(f"  page*.html aタグ数: {len(all_a)}")

    # デバッグ: 最初の5件のhrefを出力
    for a in all_a[:5]:
        print(f"    href='{a.get('href')}' text='{a.get_text(strip=True)[:30]}'")

    seen_urls = set()
    events = []
    for a in all_a:
        url = a.get("href", "")
        if url.startswith("/"):
            url = BASE_URL + url
        title = a.get_text(strip=True)
        if not title: continue
        date_raw = find_kidate(a)
        if not date_raw: continue
        if url in seen_urls: continue
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

def get_page_html(page, url):
    print(f"  GET {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)

    # #maincontが描画されるまで待つ（hiddenでも可）
    # 代わりに「期日」テキストが出現するまで待つ
    try:
        page.wait_for_function(
            "document.body.innerText.includes('期日')",
            timeout=15000
        )
        print("  ✅ 期日テキスト確認")
    except Exception:
        print("  ⚠️ 期日テキスト待機タイムアウト（続行）")

    # 追加で2秒待機（念のため）
    page.wait_for_timeout(2000)

    html = page.content()
    print(f"  文字数: {len(html)}")

    # デバッグ: bodyテキストの最初の500文字
    body_text = page.evaluate("document.body.innerText")
    print(f"  bodyテキスト冒頭: {body_text[:300]}")

    return html

def scrape():
    print("=== 熊本市 子育て支援イベント スクレイピング開始（Playwright）===")
    all_events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pw_page = browser.new_page()
        pw_page.set_extra_http_headers({"Accept-Language": "ja,en;q=0.9"})

        html = get_page_html(pw_page, LIST_URL)
        events = parse_html(html)
        all_events.extend(events)

        for page_num in range(2, 11):
            time.sleep(1)
            try:
                url = f"{LIST_URL}&page={page_num}"
                html = get_page_html(pw_page, url)
                events = parse_html(html)
                if not events:
                    print(f"  {page_num}ページ目: 新規なし → 終了")
                    break
                existing = {e["url"] for e in all_events}
                new = [e for e in events if e["url"] not in existing]
                if not new:
                    print(f"  {page_num}ページ目: 重複のみ → 終了")
                    break
                all_events.extend(new)
            except Exception as ex:
                print(f"  {page_num}ページ目エラー: {ex}")
                break

        browser.close()

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
