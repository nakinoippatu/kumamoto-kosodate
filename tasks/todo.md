# 引継ぎ資料：花園児童館おたより取得不具合の調査

作成日：2026-05-15

---

## 概要

花園児童館（Source D）の5月分おたよりイベントが `docs/events.json` に反映されていない。
熊本市公式サイトには5月分PDFが2枚公開済みであることをユーザーが確認済み。

---

## 現状

- `events.json` における花園児童館イベント数：**0件**（2026-04-04 以降継続）
- 最後に取得できた日：**2026-04-03**（10件・4月分おたより）
- スクレイパーの定期実行：毎朝7時 JST（GitHub Actions）

---

## 5月分PDFのURL（ユーザー確認済み）

| 種別 | URL | サイズ |
|------|-----|--------|
| 表面（カレンダー） | `https://www.city.kumamoto.jp/kiji00319844/3_19844_499828_up_xtnesgjp.pdf` | 125.2 KB |
| 裏面（詳細） | `https://www.city.kumamoto.jp/kiji00319844/3_19844_499827_up_vcwgg6zg.pdf` | 146.6 KB |

---

## スクレイパーの花園児童館処理フロー（`scraper.py`）

```
scrape_all_halls_adapted(pw_page)          # 行1771
  └─ _fetch_pdf_urls_from_page(pw_page, HANAZONO_URL, count=2)   # 行1803
       └─ Playwright で kiji00319844/index.html を開く
          BeautifulSoup で <a href="*.pdf"> を探す
          _is_real_kumamoto_pdf() で ReadSpeaker リンクを除外
          → 2件見つかれば [表URL, 裏URL] を返す
  └─ _fetch_pdf_bytes(hanazono_pdf_urls[0])  → pdf_front（表）
  └─ _fetch_pdf_bytes(hanazono_pdf_urls[1])  → pdf_back（裏）
  └─ scrape_hanazono(pdf_front, pdf_back)    # 行636
       └─ 表PDFからカレンダー解析
          裏PDFから詳細（時間・対象）補完
          → イベントリスト返却
```

重要仕様：**表・裏2枚が揃わないと0件を返す**（1枚でも欠けると処理しない）。

---

## 調査で判明したこと

### 1. Source D 全体が低調
児童館系（Source D）は全般的に取得が不安定。過去47日分のうち：
- 花園：1日のみ成功（2026-04-03）
- 幸田：複数日成功（最も安定しているがそれでも散発的）
- 西部・西原・秋津・五福・天明・大江：ほぼ0件

### 2. スクレイパーのコード変更（2026-04-19）
コミット `82bbb57` で以下が変更された：
- `_is_real_kumamoto_pdf()` 関数を追加（ReadSpeaker リンク除外フィルター）
- `_fetch_pdf_bytes()` に Content-Type・マジックバイト・最小サイズの検証を追加

ただし、この変更は4月4日以降の失敗が始まった後のため、失敗の直接原因ではない。

### 3. このClaudeリモート環境からは熊本市サイトへアクセス不可
ネットワークポリシー（アウトバウンドの許可リスト）により `www.city.kumamoto.jp` がブロックされている。
Playwright での確認結果：`Host not in allowlist`（172バイト）

→ **GitHub Actions（Ubuntu runner）では通常アクセス可能なはず**

### 4. 原因の候補（絞り込めていない）

| 可能性 | 根拠 |
|--------|------|
| ① Playwright がPDFリンクを発見できていない | ページ構造変更の可能性 |
| ② `_fetch_pdf_bytes` がPDFダウンロードで403 | Refererヘッダーなしで弾かれる場合あり |
| ③ 表・裏のPDFが逆順に割り当てられてパース失敗 | 0件・例外なしは逆順時の挙動と一致 |

---

## 特定に必要なもの

**下記のいずれか一つ**

### A）GitHub Actions の実行ログ
[https://github.com/nakinoippatu/kumamoto-kosodate/actions](https://github.com/nakinoippatu/kumamoto-kosodate/actions)

最新 Run のログで「花園児童館」を検索し、以下のどのメッセージが出ているか確認：

```
花園児童館: ⚠️ PDFリンクが見つかりませんでした  → ① が原因
花園児童館: ⚠️ PDFが1枚のみ（2枚必要）         → ① が原因
花園児童館: ❌ PDF取得失敗                      → ② が原因
花園児童館: ❌ 解析エラー                       → ③ が原因
花園児童館: 0 件取得                            → ③ が原因（パース空振り）
```

### B）PDFファイルを2枚アップロード
Chrome で上記の表・裏PDFをダウンロードし、Claude の会話にアップロードすると、
`scrape_hanazono` パーサーを直接実行して取得件数と失敗箇所を即座に特定できる。

---

## 修正候補（原因判明後に実施）

| 原因 | 修正内容 | 対象箇所 |
|------|----------|----------|
| ① PDFリンク未発見 | ページの wait 時間延長 or `networkidle` 待機に変更 | `_fetch_pdf_urls_from_page()` 行1678 |
| ② ダウンロード403 | `Referer` ヘッダーを追加 | `_fetch_pdf_bytes()` 行146 |
| ③ 表・裏逆順 | どちらが表かを PDF 内容（カレンダー有無）で判定 | `scrape_all_halls_adapted()` 行1801 |

---

## 関連コード箇所（`scraper.py`）

| 関数 | 行番号 | 役割 |
|------|--------|------|
| `HANAZONO_URL` | 566 | `https://www.city.kumamoto.jp/kiji00319844/index.html` |
| `scrape_hanazono()` | 636 | 表・裏PDF解析メイン |
| `_hanazono_build_wd_cols()` | 572 | カレンダー列構造の解析 |
| `_hanazono_parse_back()` | 588 | 裏面詳細の解析 |
| `_fetch_pdf_urls_from_page()` | 1678 | ページからPDF URL発見（Playwright） |
| `_fetch_pdf_bytes()` | 146 | PDF バイナリダウンロード（requests） |
| `scrape_all_halls_adapted()` | 1771 | Source D 統括（花園の特殊処理含む） |
