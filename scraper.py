"""
熊本市 子育て支援 診断用スクレイパー
HTMLの生内容をログ出力して構造を確認する
"""
import re
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.kumamoto-kekkon-kosodate.jp"
LIST_URL = f"{BASE_URL}/hpkiji/pub/List.aspx?c_id=3&class_set_id=1&class_id=523"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KumamotoKosodate-Bot/1.0)",
    "Accept-Language": "ja,en;q=0.9",
}

resp = requests.get(LIST_URL, headers=HEADERS, timeout=20)
resp.encoding = resp.apparent_encoding
html = resp.text

print(f"ステータス: {resp.status_code}")
print(f"文字数: {len(html)}")

soup = BeautifulSoup(html, "html.parser")
main = soup.select_one("#maincont") or soup.body

# 全aタグのhrefを出力
all_a = main.find_all("a") if main else soup.find_all("a")
print(f"\n全aタグ数: {len(all_a)}")
print("\n--- hrefの一覧（最初の30件）---")
for a in all_a[:30]:
    href = a.get("href","")
    text = a.get_text(strip=True)[:30]
    print(f"  href='{href}' text='{text}'")

# maincont内のテキストを確認
print("\n--- maincont内テキスト（最初の2000文字）---")
if main:
    print(main.get_text()[:2000])
