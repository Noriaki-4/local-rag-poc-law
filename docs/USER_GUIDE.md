# 利用者ガイド（法令RAG 質問デモ）

このガイドは、法令RAG POCに**人間が自然言語で質問する**方法と、**デジタル庁データセット
（lawqa_jp）の問題を手動でテストする**方法をまとめたものです。
セットアップ・運用の詳細は [RUNBOOK.md](../RUNBOOK.md) を参照してください。

---

## 1. できること

- 投入済みの法令・ガイドラインに基づいて、日本語の質問に回答します。
- 回答には**根拠とした条文・資料を引用**します（条文本文・出典つき）。
- 選択式（4択）でも質問でき、その場合はどの選択肢が正解かも判定します。

回答は検索された条文に基づく参考情報です。**法的判断そのものを断定するものではなく、
重要な判断は専門家に確認してください。**

---

## 2. 回答できる範囲

質問は**投入済みの法令・ガイドラインの範囲**に限られます。範囲外を聞くと
「引用できる条文が見つかりませんでした」と表示されるか、確度の低い回答になります。

| 分野 | 投入されている法令・資料 | 注意 |
|---|---|---|
| 借地借家・賃貸借 | 借地借家法、民法（賃貸借） | **民法は賃貸借（第601〜622条の2）のみ**。総則・物権・相続などは対象外 |
| 金融商品取引法 | 金商法本体・施行令・関連府省令、監督指針/開示/公開買付けの各ガイドライン | 全条投入 |
| 薬機法 | 医薬品医療機器等法・施行規則、法令遵守ガイドラインQ&A・適正広告基準 | 全条投入。対応表の条文はグラフ（EXPLAINS）で紐付け済み |

外部ガイドライン（PDF、6件）: 金融庁の監督指針・開示ガイドライン・公開買付けQ&A、
厚労省の法令遵守ガイドラインQ&A・適正広告基準、国交省の原状回復ガイドライン。

> 実際に投入されるのは `SEED_LAWQA_EGOV=true`（法令）と `SEED_EXTERNAL_GUIDANCE=true`
> （ガイドライン）を指定して seed した場合です。詳細は RUNBOOK.md の該当節を参照。

---

## 3. UIで質問する

### 3.1 起動

```bash
docker compose up --build -d
```

ブラウザで **http://localhost:8501** を開きます（`agent-ui` サービス）。
API 単体の疎通確認は `curl -s http://localhost:8000/health | jq .`。

### 3.2 使い方

1. 左サイドバーの**質問例**をクリックすると入力欄にセットされます。そのまま、または
   編集して使えます。
2. 中央の入力欄に質問を書き、**「質問する」**を押します（回答生成に最大2分程度）。
3. **回答**とその下に**引用した条文・資料**が表示されます。各引用を開くと条文本文と
   出典が読めます。

「詳細設定（開発者向け）」は通常触る必要はありません。探索方式（pattern）の変更、
選択式（4択）入力、引用件数などの調整、検索ルート/グラフ/traceの表示に使います。

### 3.3 質問のコツ

- **自然な日本語**で構いません。「〜は何年ですか」「〜に〜は含まれますか」
  「〜の場合に必要な措置は何ですか」のように具体的に。
- **「根拠条文も示してください」**と付けると、引用条文が明確になります。
- 対象の**分野・法令名**（借地権、有価証券、製造販売業者 など）を質問に含めると
  検索が絞り込まれ精度が上がります。

### 3.4 質問例（動作確認済み）

借地借家・賃貸借:
- 借地権の存続期間は何年ですか。根拠条文も示してください。 → 借地借家法第3条（30年）
- 賃貸借が終了したとき、敷金はいつ返還されますか。
- 借地権の存続期間が満了した場合、借地上の建物はどう扱われますか。

金融商品取引法:
- 有価証券の定義に国債証券は含まれますか。根拠条文も示してください。 → 金商法第2条第1項第1号
- 有価証券報告書は誰が、いつまでに提出する必要がありますか。
- 株券等の公開買付けとは何ですか。

薬機法:
- 製造販売業者が整備すべき法令遵守体制とはどのようなものですか。根拠条文も示してください。
  → 薬機法第18条の2ほか
- 総括製造販売責任者の役割は何ですか。

---

## 4. APIで質問する（UIを使わない場合）

`/answer` に POST します。`choices` を省略すると自由入力（フリーQA）になります。

```bash
curl -s -X POST http://localhost:8000/answer \
  -H 'content-type: application/json' \
  -d '{
        "question": "借地権の存続期間は何年ですか。根拠条文も示してください。",
        "pattern": "pattern_4_deepsearch",
        "topK": 5
      }' | jq '{answer, citations: [.citations[].contentUnitId]}'
```

主なレスポンス項目: `answer`（回答本文）、`citations`（引用条文。`title`/`heading`/
`text`/`sourceObjectUri`）、`route`/`trace`/`graphPaths`（デバッグ用）。

---

## 5. デジタル庁データセット（lawqa_jp）を手動でテストする

lawqa_jp の選択式140問（金商法80・薬機法39・借地借家法21）を、`eval-runner` で
**1問だけ／数問だけ**実行して、システムの予測回答と正解（gold）を比較できます。

### 5.1 事前準備

法令・ガイドラインが seed 済みであること（RUNBOOK.md の seed 手順）。以下は
`EVAL_SKIP_SEED=true` で**再seedせず**に評価する前提です。

### 5.2 特定の1問だけ実行する

問題はデータセットの**並び順**で指定します（`EVAL_OFFSET` = 先頭から数えた0始まりの位置、
`EVAL_LIMIT=1` = 1問だけ）。例: 5番目の問題を実行:

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_OFFSET=4 \
EVAL_LIMIT=1 \
EVAL_PATTERN=pattern_4_deepsearch \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

結果は `eval-results/eval-<日時>.jsonl` に1行で書き出されます。

### 5.3 数問まとめて実行する（例: 先頭10問）

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_LIMIT=10 \
EVAL_PATTERN=pattern_4_deepsearch \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

### 5.4 結果の読み方

出力 JSONL の各行から、予測回答と正解、正誤を取り出します:

```bash
latest=$(ls -t eval-results/*.jsonl | head -1)
jq -c '{問題: .questionId, 予測: .predictedAnswer, 正解: .goldAnswer, 正誤: .scores.answerAccuracy, LLM使用: .llmUsed}' "$latest"
```

- `scores.answerAccuracy` が `1` なら正解、`0` なら不正解。
- `citations` に引用した条文ID（`retrievedContentUnitIds` は検索候補全体）。
- `llmUsed` が `false` の場合はタイムアウト等でLLM判定が行われていない（結果は参考外）。

特定の問題を名前で拾いたいときは、範囲実行した結果を `questionId` で絞り込みます:

```bash
jq -c 'select(.questionId | test("借地借家")) | {questionId, predictedAnswer, goldAnswer}' "$latest"
```

### 5.5 全140問を実行する

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

実行の詳細（所要時間・タイムアウト対策・集計指標）は RUNBOOK.md の「評価実行」節を
参照してください。

---

## 6. うまく回答されないとき

- **範囲外の質問**: 第2章「回答できる範囲」を確認。民法は賃貸借のみです。
- **引用が見つからない**: 法令名や分野の語を質問に足す、「根拠条文も示して」を付ける。
- **時間がかかる/タイムアウト**: `pattern_4_deepsearch` は探索が深く時間がかかります。
  詳細設定で `pattern_2_rule_based_agentic_rag` に下げると速くなります。
- **サービスが応答しない**: `docker compose ps` で各コンテナの状態、
  `docker compose logs -f agent-api` でログを確認。
