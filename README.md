# 熊本市 子育て支援カレンダー

熊本市内の子育て支援イベント・講座を複数のサイトから自動収集し、
カレンダー・リスト形式で表示するWebアプリです。

🔗 **公開URL**: https://nakinoippatu.github.io/kumamoto-kosodate/

---

## 📋 データソース

| ソース | 取得方法 | 対象 |
|---|---|---|
| [熊本市こども・子育て応援サイト（子育てナビ）](https://www.kumamoto-kekkon-kosodate.jp/) | Playwright（JS描画） | 全講座 |
| [熊本市総合子育て支援センター](https://www.city.kumamoto.jp/kiji0031482/index.html) | Playwright（JS描画） | イベント情報 |
| [熊本市こども文化会館](https://www.kodomobunka.jp/event/) | requests | 乳幼児・保護者向けのみ |

毎朝7時（JST）に自動更新されます。

---

## 🗂️ プロジェクト構成

```
kumamoto-kosodate/
├── scraper.py              # 統合スクレイパー（3ソース対応）
├── requirements.txt        # Python依存パッケージ
├── .github/
│   └── workflows/
│       └── scrape.yml      # 自動実行（GitHub Actions）
└── docs/
    ├── index.html          # フロントエンド（GitHub Pages公開）
    └── events.json         # スクレイピング結果（自動更新）
```

---

## ✨ 機能

- **カレンダー表示 / リスト表示** の切り替え
- **カテゴリ・対象年齢・情報源** によるフィルター
- **★要予約** バッジ表示（事前申し込みが必要なイベント）
- イベントタップで元サイトへ直接移動
- データはHTMLに直接埋め込み（fetchなし・確実表示）

---

## 🚀 セットアップ

### 1. リポジトリをGitHubにプッシュ

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/ユーザー名/kumamoto-kosodate.git
git push -u origin main
```

### 2. GitHub Pages を有効化

1. リポジトリの **Settings** → **Pages** を開く
2. **Source** を `Deploy from a branch` に設定
3. **Branch** を `main` / `docs` に設定
4. `https://ユーザー名.github.io/kumamoto-kosodate/` で公開される

### 3. 初回データ取得

**Actions** タブ → **scrape-and-deploy** → **Run workflow** を実行

### 4. ローカルで試す

```bash
pip install -r requirements.txt
playwright install chromium
python scraper.py
```

---

## ⚙️ カスタマイズ

### スクレイピング頻度の変更

`.github/workflows/scrape.yml` の `cron` を編集：

```yaml
# 毎朝7時（JST）= UTC 22時
- cron: "0 22 * * *"

# 毎週月曜朝7時
- cron: "0 22 * * 1"
```

### カテゴリ・年齢マッピングの変更

`scraper.py` の `CATEGORY_MAP` / `AGE_MAP` を編集してください。

### 新しいソースの追加

`scraper.py` に `scrape_XXX()` 関数を追加し、`scrape()` 内で呼び出してください。

---

## ⚠️ 注意事項

- 各サイトへのアクセスは1日1回のみです
- サイト構造が変更された場合、スクレイパーの修正が必要になることがあります
- 商用利用の際は各サイトの利用規約をご確認ください
