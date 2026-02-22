# 🍊 熊本市 子育て支援イベントカレンダー

熊本市内の子育て支援イベントを自動収集し、カレンダー形式で表示するWebサービスです。

🔗 **公開URL**: https://nakinoippatu.github.io/kumamoto-kosodate/

---

## 📌 概要

乳幼児・保護者向けのイベント情報を複数ソースから毎朝自動取得し、GitHub Pages で公開します。予約が必要なイベントは「★」マークで一目で分かるようにしています。

---

## 🗂️ ファイル構成

```
kumamoto-kosodate/
├── scraper.py                  # 統合スクレイパー（全ソース）
├── .github/
│   └── workflows/
│       └── scrape.yml          # GitHub Actions 自動実行設定
└── docs/
    ├── index.html              # フロントエンド（カレンダー表示）
    └── events.json             # スクレイピング結果（自動生成）
```

---

## 📡 データソース

### A) 子育てナビ
- **URL**: https://www.kumamoto-kekkon-kosodate.jp
- **取得方法**: Playwright（JavaScript レンダリング対応）
- **内容**: 熊本市内の子育て講座・イベント全般

### B) 総合子育て支援センター
- **URL**: https://www.city.kumamoto.jp/kiji0031482/index.html
- **取得方法**: Playwright（動的ページのため）
- **内容**: 中央区本荘の支援センターイベント

### C) こども文化会館
- **URL**: https://www.kodomobunka.jp/event/
- **取得方法**: requests（静的HTML）
- **内容**: 乳幼児・保護者向けに絞ってフィルタリング

### D) 各児童館（PDF解析）
pdfplumber を使って各施設の月次PDFカレンダーを解析します。

| 施設名 | 備考 |
|---|---|
| 幸田児童館 | 月〜日カレンダー形式 |
| 西部児童館 | メタデータから年月推定 |
| 西原公園児童館 | リスト形式PDF |
| 花園児童館 | 表面＋裏面の2枚PDF構成 |
| 託麻児童館 | 2列レイアウトPDF |
| 秋津児童館 | 花園と同形式（21列） |
| 五福児童室 | スキャンPDFのため手動JSON対応 |
| 天明児童室 | 右列に詳細情報あり |
| 大江児童室 | テーブルなし・テキスト抽出 |
| 城南児童館 | 日〜土カレンダー・乳幼児向けのみ抽出 |

---

## 🎨 カテゴリ・凡例

| カテゴリ | 色 |
|---|---|
| 食育・栄養 | 🟢 緑 |
| 健康・医療 | 🔵 青 |
| 発達・育児相談 | 🟣 紫 |
| 親子ふれあい | 🩷 ピンク |
| 父親・家族支援 | 🟠 オレンジ |
| 産前・産後 | 🟤 茶 |
| ひとり親支援 | 💙 淡青 |
| その他 | ⚫ グレー |

**★ 要予約** … 事前申込が必要なイベント

---

## ⚙️ セットアップ（ローカル実行）

### 1. 依存パッケージのインストール

```bash
pip install requests beautifulsoup4 playwright pdfplumber lxml
playwright install chromium
```

### 2. スクレイピング実行

```bash
python scraper.py
```

`docs/events.json` と `docs/index.html` が自動更新されます。

### 3. 児童館PDFを手動で渡す場合

各施設のPDFを手動で取得して渡すこともできます：

```python
from scraper import scrape_all_halls

pdf_map = {
    "幸田児童館": open("koda.pdf", "rb").read(),
}
events = scrape_all_halls(pdf_map=pdf_map)
```

**五福児童室**（スキャンPDF）は手動JSONで対応：

```bash
python scraper.py 五福 gofuku.pdf gofuku_events.json
```

---

## 🤖 GitHub Actions 自動実行

`.github/workflows/scrape.yml` により、**毎朝7時（JST）** に自動実行されます。

```
スケジュール: 0 22 * * * (UTC) = 毎朝7:00 JST
```

実行フロー：
1. リポジトリをチェックアウト
2. 依存パッケージをインストール
3. `python scraper.py` を実行
4. `docs/events.json` と `docs/index.html` をコミット＆プッシュ

手動実行も可能（GitHub の Actions タブ → `workflow_dispatch`）。

---

## 🛠️ 技術スタック

| 用途 | ライブラリ |
|---|---|
| JSレンダリングページ取得 | Playwright |
| 静的HTML取得 | requests |
| HTML解析 | BeautifulSoup4 |
| PDF解析 | pdfplumber |
| カレンダーUI | FullCalendar 6 |
| フォント | Noto Sans JP / Kaisei Decol |
| ホスティング | GitHub Pages |

---

## 📝 データ形式

`events.json` のイベントオブジェクト：

```json
{
  "title": "★離乳食講座",
  "date_raw": "2026年3月5日",
  "date_iso": "2026-03-05",
  "time_raw": "10:00〜11:30",
  "location": "総合子育て支援センター（中央区本荘）",
  "apply_info": "要電話申込",
  "category": "食育・栄養",
  "target_age": "0歳",
  "url": "https://...",
  "source": "総合子育て支援センター",
  "needs_reservation": true,
  "body_preview": ""
}
```

---

## 📄 ライセンス

MIT License

---

*このプロジェクトは熊本市の子育て世帯を応援するために個人が作成・運営しています。*
*掲載情報は各施設の公式サイト・配布資料に基づきますが、最新情報は必ず各施設にご確認ください。*
