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
    "A": "not_supported",
    "B": "not_supported",
    "C": "supported",
    "D": "not_supported"
  },
  "citations": []
}
```

### 指標

- `answerAccuracy`: predictedAnswer == goldAnswer
- `citationHit`: citations が expectedReferences に当たるか
- `retrievalHit@k`: 検索上位k件に期待根拠が含まれるか
- `graphExpansionHit`: Graph展開で新たに期待根拠を取得できたか
- `citationLawHit`: 期待法令IDと引用法令IDが一致したか
- `citationArticleHit`: 条単位goldが存在する場合だけ計算。法令URLしかない場合はnull
- `citationParagraphHit`: 項・号単位goldが存在する場合だけ計算。それ以外はnull

### 2.1 ゴールデンセットの構造と「答え」を渡さない保証

lawqa_jp は**ゴールデンセット**であり、各問題は正解に加えて「必要な条文（根拠）」を
持っている。ただし本POCの評価では、これらの gold 情報を**推論時にはシステムへ渡さず、
採点にのみ使う**。これにより「必要な条文を与えられて読解するだけ」の穴埋めテストでは
なく、**必要な条文を自力で検索して引けるか**という検索性能テストになる。

#### データセットが持つ gold フィールド

lawqa_jp native JSON（`data/selection.json`）の1問は次を持つ:

| フィールド | 内容 | 評価での役割 |
|---|---|---|
| `問題文` / `選択肢` | 設問と4択 | **入力**（システムへ渡す） |
| `output` | 正解の選択肢ラベル（例: `c`） | gold回答（採点のみ） |
| `コンテキスト` | 根拠条文を階層見出し付きで収録（`## 法令名` / `### 第N条` / `#### 第N項` / `##### 第N号` + 本文） | **gold根拠（条・項・号レベル）**（採点のみ） |
| `references` | e-Gov法令URL・ガイドラインPDF URL | 出典。多くは**親法レベル**（採点のみ） |

#### システムへ渡す入力（gold を含めない）

`eval-runner` が `/answer` へ送るのは `question` と `choices` だけ
（[run_eval.py](../../../eval-runner/run_eval.py) の `run_lawqa`）。
`output`・`コンテキスト`・`references` は送らない。システムは seed 済みのローカル
コーパスから条文を自力検索する。

#### 期待参照（expectedReferences）の生成

gold条文は2経路で `expectedReferences`（採点用の contentUnitId/lawId のリスト）へ変換する
（`normalize_lawqa_sample`）:

1. `references` のURL → `_reference_from_url` で法令番号（lawId）を抽出。多くは
   `law-<法令番号>` の**法令レベル**まで。
2. `コンテキスト` の見出し → `_context_expected_references` で
   `法令名→lawId` を対応付けたうえで、`### 第N条`→`law-<id>-article-N`、
   `#### 第N項`→`…-paragraph-P`、`##### 第N号`→`…-item-K` と、**条・項・号レベルの
   contentUnitId** を組み立てる。`references` が親法しか持たなくても、コンテキストから
   条文粒度の gold を復元できる。

gold の最も細かい粒度を `referenceGranularity`（`law`/`article`/`paragraph`/`item`）として
記録し、指標の照合粒度を決める。gold が法令URLしか無ければ上限は `law`。

#### 採点（retrieved citations との突き合わせ）

システムの回答（`predictedAnswer`）と引用（`citations`）を gold と照合する。
照合はすべて **ID集合の積（共通要素が1つでもあれば hit=1、無ければ 0）** で行う。

- `answerAccuracy` = `predictedAnswer == goldAnswer`（選択肢ラベルの一致、1/0）。
- **引用の照合は gold の粒度で使う指標を切り替える**:
  - gold が**法令URLだけ**（`referenceGranularity=law`）→ `citationLawHit` =
    「期待 documentId 集合 ∩ 引用 documentId 集合 ≠ ∅」。
  - gold に**条以下がある**（`article`/`paragraph`/`item`）→ `citationArticleHit` =
    期待・引用の双方を**Article ID に正規化**（`-paragraph-…` / `-item-…` を落とす）した
    集合の積。`citationParagraphHit` は正規化せず contentUnitId を**厳密一致**で比較。
  - `citationHit` は「gold が law粒度なら citationLawHit、条以下なら citationArticleHit」を
    採用する代表値。
  - `citationLawFamilyHit` は、親法を期待して委任法令（施行令・府令）を引いたケースを
    救うため、法令ファミリー単位（`law_registry.json` の `familyRoot`）で照合する別軸。
- gold が全く無い問題（非e-Gov PDFのみが根拠等）は `referenceScorable=false` とし、
  引用系は `null`（採点対象外。率の分母から除く）。
- **段階別 hit**（`candidatePoolHit` / `fusionHit` / `rerankerHit`）は、trace の
  `candidatePoolContentUnitIds` / `fusionTopContentUnitIds` / `rerankerTopContentUnitIds`
  それぞれに対し上と同じ照合（law粒度なら documentId、条以下なら Article ID 正規化）を行い、
  **候補プール→RRF融合→reranker のどの段階で gold を落としたか**を切り分ける
  （検索ミスの原因診断に使う。例: candidatePoolHit=1 かつ rerankerHit=0 なら「候補には
  あったが再ランクで落とした」）。
- `graphExpansionHit` は Graph が新規取得した `graphExpandedContentUnitIds`（およびその親条・
  親法令ID）が gold に当たった場合だけ1。

> 実装は [run_eval.py](../../../eval-runner/run_eval.py) の `run_lawqa` 内。期待側は
> `expected`（contentUnitId集合）/ `expected_document_ids` / `expected_articles`、引用側は
> `retrieved` / `retrieved_document_ids` / `retrieved_articles`、段階別は `_hit_at()` に対応。

> 要約: lawqa_jp は「正解＋必要な条文」を持つゴールデンセットだが、それらは
> **答え合わせ専用**であり、システムには問題文と選択肢しか渡さない。`citationArticleHit`
> は「システムが自力で引いた条文が、コンテキスト由来の gold条文と一致したか」を測る。

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

Agent APIのtraceから `retrievedGraphNodeIds`、`retrievedGraphEdgeIds`、`graphExpandedContentUnitIds` を転記する。`graphExpansionHit` はGraph探索が実行されたかではなく、Graphが新規取得した `graphExpandedContentUnitIds` の根拠がgold referenceに一致した場合だけ1とする。各評価行には `referenceGranularity=law|article|paragraph|item` を記録し、lawqa_jp nativeデータのように法令URLしかない場合は `citationHit` を法令単位hitとして明示する。


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

eval-runner は以下の入力を受け付ける。

```text
LAWQA_EVAL_URL  # lawqa_jp data/selection*.json のURL
LAWQA_EVAL_PATH # コンテナ内の lawqa_jp data/selection*.json へのパス
EVAL_LIMIT      # 0なら全件。疎通確認時は10などに制限
EVAL_OFFSET     # 再開・分割実行用
EVAL_PATTERN    # 評価対象のAgent pattern
EVAL_SKIP_SEED  # trueならeval-runner起動時に/admin/seedを呼ばない
REQUEST_TIMEOUT_SEC
AGENT_USE_BM25 / AGENT_USE_VECTOR # Agent API側の検索方式。現行POCの既定はBM25 + bge-m3 vector
```

lawqa_jp native JSON の `コンテキスト` と `output` は Agent API に送らない。`output` と `references` は評価後の答え合わせだけに使う。

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

`law_only` は、contentUnitId を持つ gold では二値hitに混ぜない。補助スコアとして `partial_credit = 0.5` を別列に保持してもよいが、Pattern間の主集計は exact / descendant / ancestor の件数で比較する。
ただし lawqa_jp native JSON の `references` が法令URLだけを持ち条・項粒度を持たない場合は、評価可能な上限粒度が law_only になるため、当該入力に限り `citationHit` は法令ID一致として扱う。

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
