"""
熊本市 子育て支援 イベント・講座 スクレイパー
取得元: 熊本市こども・子育て応援サイト
実行: python scraper.py
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
    "User-Agent": "Mozilla/5.0 (compatible; KumamotoKosodate-Bot/1.0; +https://github.com/yourname/kumamoto-kosodate)",
    "Accept-Language": "ja,en;q=0.9",
}

# カテゴリキーワードマッピング
CATEGORY_MAP = {
    "離乳食": "食育・栄養",
    "食育": "食育・栄養",
    "栄養": "食育・栄養",
    "健康": "健康・医療",
    "歯": "健康・医療",
    "医療": "健康・医療",
    "発達": "発達・育児相談",
    "相談": "発達・育児相談",
    "育児": "発達・育児相談",
    "パパ": "父親・家族支援",
    "パパママ": "父親・家族支援",
    "ふれあい": "親子ふれあい",
    "遊び": "親子ふれあい",
    "手遊び": "親子ふれあい",
    "絵本": "親子ふれあい",
    "体操": "親子ふれあい",
    "リトミック": "親子ふれあい",
    "工作": "親子ふれあい",
    "ひとり親": "ひとり親支援",
    "産前": "産前・産後",
    "産後": "産前・産後",
    "マタニティ": "産前・産後",
    "妊娠": "産前・産後",
    "妊婦": "産前・産後",
}

# 対象年齢キーワード
AGE_MAP = {
    "妊娠中": "妊娠中",
    "妊婦": "妊娠中",
    "産後": "0歳",
    "0歳": "0歳",
    "０歳": "0歳",
    "乳児": "0歳",
    "新生児": "0歳",
    "1歳": "1〜2歳",
    "１歳": "1〜2歳",
    "2歳": "1〜2歳",
    "２歳": "1〜2歳",
    "1～2歳": "1〜2歳",
    "3歳": "3〜5歳",
    "３歳": "3〜5歳",
    "4歳": "3〜5歳",
    "４歳": "3〜5歳",
    "5歳": "3〜5歳",
    "５歳": "3〜5歳",
    "未就学": "3〜5歳",
    "幼児": "3〜5歳",
    "小学": "小学生以上",
    "乳幼児": "0歳〜未就学",
}


def guess_category(title: str, body: str) -> str:
    text = title + body
    for keyword, category in CATEGORY_MAP.items():
        if keyword in text:
            return category
    return "その他"


def guess_age(title: str, body: str) -> str:
    text = title + body
    for keyword, age in AGE_MAP.items():
        if keyword in text:
            return age
    return "指定なし"


def fetch_list_page(page: int = 1) -> BeautifulSoup:
    url = LIST_URL
    if page > 1:
        url = f"{BASE_URL}/hpkiji/pub/List.aspx?c_id=3&class_set_id=1&class_id=523&page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "html.parser")


def extract_links_with_dates(soup: BeautifulSoup) -> list[dict]:
    """一覧ページからイベントリンクと日付を抽出"""
    links = []
    # 記事リスト部分を探す（日付＋タイトル＋リンクのセット）
    for item in soup.select("li, .kiji_list_cell, p"):
        a_tag = item.find("a", href=re.compile(r"/page\d+\.html"))
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        href = a_tag.get("href", "")
        if not href.startswith("http"):
            href = BASE_URL + href
        # 同じliの中の日付テキストを探す
        text = item.get_text(" ", strip=True)
        date_match = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", text)
        date_raw = date_match.group(1) if date_match else ""
        if title:
            links.append({"title": title, "url": href, "date_raw": date_raw})

    # 上記で取れない場合のフォールバック
    if not links:
        for a_tag in soup.select(f"a[href*='/page']"):
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if not re.match(r".*/page\d+\.html", href):
                continue
            if not href.startswith("http"):
                href = BASE_URL + href
            if title:
                links.append({"title": title, "url": href, "date_raw": ""})
    return links


def fetch_detail(url: str) -> dict:
    """詳細ページからイベント情報を抽出"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        # 本文テキスト取得
        body_elem = soup.select_one("#maincont") or soup.select_one(".detail_body") or soup.find("main")
        body_text = body_elem.get_text(" ", strip=True) if body_elem else ""

        # タイトル
        title_elem = soup.select_one("h2") or soup.select_one("h1")
        title = title_elem.get_text(strip=True) if title_elem else ""

        # 日付パターンを本文から検索
        date_pattern = re.compile(r"(令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日|(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})日?)")
        date_match = date_pattern.search(body_text)
        date_str = date_match.group(0) if date_match else ""

        # 時間パターン
        time_pattern = re.compile(r"(\d{1,2})[：:時](\d{0,2})[\s～〜~\-]+(\d{1,2})[：:時](\d{0,2})")
        time_match = time_pattern.search(body_text)
        time_str = time_match.group(0) if time_match else ""

        # 場所パターン
        place_pattern = re.compile(r"(会場|場所|開催場所)[：:\s]*([^\n。、]{2,30})")
        place_match = place_pattern.search(body_text)
        place_str = place_match.group(2).strip() if place_match else ""

        # 申込み
        apply_pattern = re.compile(r"(申込|申し込み|受付)[：:\s]*([^\n。]{5,50})")
        apply_match = apply_pattern.search(body_text)
        apply_str = apply_match.group(0).strip() if apply_match else ""

        # カテゴリ・年齢
        category = guess_category(title, body_text)
        age = guess_age(title, body_text)

        return {
            "title": title,
            "date_raw": date_str,
            "time_raw": time_str,
            "location": place_str,
            "apply_info": apply_str,
            "category": category,
            "target_age": age,
            "url": url,
            "body_preview": body_text[:200],
        }
    except Exception as e:
        print(f"  [WARN] 詳細取得失敗: {url} -> {e}")
        return {}


def scrape() -> list[dict]:
    print("=== 熊本市 子育て支援イベント スクレイピング開始 ===")
    all_links = []

    # 1ページ目取得
    soup = fetch_list_page(1)
    links = extract_links_with_dates(soup)
    all_links.extend(links)
    print(f"1ページ目: {len(links)} 件取得")

    # ページネーション確認（最大5ページ）
    for page in range(2, 6):
        time.sleep(1)
        try:
            soup = fetch_list_page(page)
            links = extract_links_with_dates(soup)
            if not links:
                break
            all_links.extend(links)
            print(f"{page}ページ目: {len(links)} 件取得")
        except Exception as e:
            print(f"{page}ページ目でエラー: {e}")
            break

    # 重複除去
    seen = set()
    unique_links = []
    for link in all_links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique_links.append(link)

    print(f"\n合計 {len(unique_links)} 件のイベントを取得。詳細を取得します...")

    events = []
    for i, link in enumerate(unique_links, 1):
        print(f"  [{i}/{len(unique_links)}] {link['title'][:40]}...")
        detail = fetch_detail(link["url"])
        if detail:
            if not detail.get("title"):
                detail["title"] = link["title"]
            # 一覧ページで取得した日付で補完
            if not detail.get("date_raw") and link.get("date_raw"):
                detail["date_raw"] = link["date_raw"]
            events.append(detail)
        time.sleep(0.8)  # サーバー負荷軽減

    print(f"\n取得完了: {len(events)} 件")
    return events


def save(events: list[dict]):
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
