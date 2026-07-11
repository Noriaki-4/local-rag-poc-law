# 評価設計

## 1. 共通評価観点

```text
answer_accuracy
retrieval_hit@k
citation_hit@k
graph_expansion_hit
route_score
decomposition_score
choice_judgement_accuracy
faithfulness
latency_ms
cost_estimate
```

## 2. lawqa_jp 評価

前提: lawqa_jpの問題文・選択肢はAgentへの入力、正解・期待参照条文は評価用goldである。実行時のRAG検索対象は、e-Govから事前取得してローカルに登録した法令本文であり、e-Gov自体やlawqa_jp付属コンテキストを検索先にはしない。


### 入力

```text
question
choices
```

### gold

```text
goldAnswer
expectedReferences
expectedLawIds
```

### 出力

```json
{
  "predictedAnswer": "C",
  "choiceJudgements": {
    "A": "contradicted",
    "B": "not_supported",
    "C": "supported",
    "D": "contradicted"
  },
  "citations": []
}
```

### 指標

- `answerAccuracy`: predictedAnswer == goldAnswer
- `citationHit`: citations が expectedReferences に当たるか
- `retrievalHit@k`: 検索上位k件に期待根拠が含まれるか
- `graphExpansionHit`: Graph展開で新たに期待根拠を取得できたか

## 3. Trace ログ

すべてのパターンで共通ログを出す。

```json
{
  "runId": "run-001",
  "pattern": "pattern_3_controlled_agentic_rag",
  "dataset": "lawqa_jp",
  "questionId": "q-001",
  "inputType": "multiple_choice_legal_qa",
  "searchPlan": [],
  "toolCalls": [],
  "retrievedContentUnitIds": [],
  "retrievedGraphNodeIds": [],
  "retrievedGraphEdgeIds": [],
  "citations": [],
  "predictedAnswer": "C",
  "goldAnswer": "C",
  "scores": {
    "answerAccuracy": 1,
    "citationHit": 1,
    "retrievalHitAt5": 1,
    "graphExpansionHit": 0
  },
  "latencyMs": 1234
}
```



## 4. 統計的評価設計

### lawqa_jp件数

POC開始時に、利用するlawqa_jpの総問題数、評価対象件数、除外件数を固定して記録する。

```yaml
lawqa_eval_split:
  total_questions: "FIXME"
  used_questions: "FIXME"
  excluded_questions: "FIXME"
  exclusion_reason: []
```

初期は全件評価を原則とする。ただし、RAG検索対象に含めないPDFガイドライン由来問題を除外する場合は、除外理由と件数を明示する。

### Pattern間比較

- Pattern 1〜4は同じ問題集合で評価する。
- embedding_model、chunk_strategy、LLM、top_k、hybrid_weightは固定する。
- accuracy差は問題数が少ない場合、厳密な有意差ではなく探索的評価として扱う。
- 可能であればbootstrap信頼区間またはMcNemar検定を追加する。

## 5. citationHit / retrievalHit の照合粒度

gold referenceが条単位で、retrievalが項・号単位の場合があるため、以下の階層照合を行う。

```text
exact:      contentUnitId完全一致
ancestor:   retrievedの親Articleがgold Articleと一致
descendant: gold Article配下のParagraph/Itemがretrievedに含まれる
law_only:   法令番号のみ一致
miss:       不一致
```

集計方針:

```text
primary_hit = 1: exact / descendant / ancestor
primary_hit = 0: law_only / miss
```

`ancestor` は条単位goldに対して上位Articleが取得できている状態であり、初期POCではhit扱いにする。ただし粒度不足として別カウントする。

`law_only` は二値hitには混ぜない。補助スコアとして `partial_credit = 0.5` を別列に保持してもよいが、Pattern間の主集計は exact / descendant / ancestor の件数で比較する。

報告例:

```text
citation_exact_count
citation_descendant_count
citation_ancestor_count
citation_law_only_count
citation_miss_count
primary_citation_hit_rate
partial_credit_score_optional
```

## 6. LLM-as-Judgeの扱い

`faithfulness`, `route_score`, `decomposition_score` は初期は補助評価とする。

- answerAccuracy: ルール評価
- citationHit: ルール評価
- retrievalHit@k: ルール評価
- route_score: 期待routeがあるサンプルのみルール評価。自由質問は任意でLLM-as-Judge
- decomposition_score: Pattern 3/4のみ。初期は人手レビューまたはLLM-as-Judge
- faithfulness: LLM-as-Judgeを使う場合はjudge_modelを固定

LLM-as-Judgeの出力は最終正解ではなく、分析補助として扱う。
