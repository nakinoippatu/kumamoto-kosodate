# 熊本市 子育て支援 講習会カレンダー

熊本市こども・子育て応援サイトから子育て支援のイベント・講座情報を自動収集し、
カレンダー形式で表示するWebアプリです。

## 📁 プロジェクト構成

```
kumamoto-kosodate/
├── scraper.py              # スクレイピングスクリプト
├── requirements.txt        # Python依存パッケージ
├── .github/
│   └── workflows/
│       └── scrape.yml      # 毎日自動実行（GitHub Actions）
└── docs/
    ├── index.html          # フロントエンド（GitHub Pagesで公開）
    └── events.json         # スクレイピング結果（自動更新）
```

## 🚀 セットアップ手順

### 1. このリポジトリをGitHubにプッシュ

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/あなたのユーザー名/kumamoto-kosodate.git
git push -u origin main
```

### 2. GitHub Pages を有効化

1. リポジトリの **Settings** → **Pages** を開く
2. **Source** を `Deploy from a branch` に設定
3. **Branch** を `main` / `docs` に設定
4. 保存すると `https://あなたのユーザー名.github.io/kumamoto-kosodate/` で公開される

### 3. GitHub Actions の確認

- **Actions** タブを開き、`毎日スクレイピング & GitHub Pages 更新` が有効になっていることを確認
- 手動実行したい場合は **Run workflow** ボタンを押す
- 毎朝7時（JST）に自動実行され、`docs/events.json` が更新される

### 4. ローカルでスクレイパーを試す

```bash
pip install -r requirements.txt
python scraper.py
```

## ⚙️ カスタマイズ

### スクレイピング頻度の変更

`.github/workflows/scrape.yml` の `cron` を編集：

```yaml
# 毎朝7時（JST）
- cron: "0 22 * * *"

# 毎週月曜日の朝7時
- cron: "0 22 * * 1"
```

### カテゴリの追加・変更

`scraper.py` の `CATEGORY_MAP` と `AGE_MAP` を編集してください。

## ⚠️ 利用上の注意

- 熊本市公式サイトへのアクセスは1日1回のみに制限しています
- 商用利用の際は熊本市の利用規約をご確認ください
- サイト構造が変更された場合、スクレイパーの修正が必要になることがあります

## 📜 データソース

- [熊本市こども・子育て応援サイト](https://www.kumamoto-kekkon-kosodate.jp/)
- [熊本市公式サイト 子育て支援](https://www.city.kumamoto.jp/list04038.html)
