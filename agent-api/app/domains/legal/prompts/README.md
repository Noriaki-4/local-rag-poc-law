# 法令Domain Prompt構成

このディレクトリには、法令調査と法令関係分類でLLMへ渡すPromptを置きます。
このREADMEは保守者向けの説明であり、実行時Promptには合成しません。

## 法令調査Solver

法令調査は一つのSolverが担当します。処理ごとにAgentを増やすのではなく、
`AgentLoop`が現在の構造的な状態から一つの処理モードを選び、そのモードに必要なPromptだけを渡します。
Programが法的関連性、根拠の十分性、候補の採否、次のToolを判断することはありません。

### 合成関係

```text
solver_common.md ───────────────┐
                               │
solver_tools.md ───────┐       │
                       ├───────┼─→ 用途別のModelCallProfile.system_prompt
solver_completion.md ──┤       │
                       │       │
用途別Prompt ──────────┴───────┘
                               │
                               ▼
                  共通出力契約・輸送指示・SolverContext
                               │
                               ▼
                           LLM Provider
```

実際の組み合わせは次のとおりです。

| 呼出し用途 | 合成するPrompt | 役割 |
|---|---|---|
| `research` | common + tools + research + completion | 案件開始時に質問を分解し、WorkItem、Hypothesis、最初の探索を決める。 |
| `integration` | common + tools + integration + completion | ToolResultを評価し、状態を逐次更新して次の行動または完了を決める。次Cycle開始後の再計画もこの用途で扱う。 |
| `cycle_close` | common + cycle_close + completion | 現Cycleを評価して閉じ、未取得候補を処理し、完了または次Cycleへの引継ぎを決める。新しいToolは要求しない。 |
| `finalization` | common + finalization + completion | 実行上限時に追加Toolなしで、確認済み範囲と未確認範囲を分けた回答を作る。 |
| `reviewer_revision` | common + tools + reviewer_revision + completion | Reviewer Findingを全件処理し、回答修正または追加調査を決める。 |
| `search_selection` | search_review → search_reselection | OpenSearch候補を全件要約し、その短い一覧から今回本文取得する候補を決める。 |
| `graph_selection` | graph_reviewのみ | 新しい1ホップGraph候補を差分評価し、本文取得する候補と保留・除外を決める。 |

`search_selection`と`graph_selection`は同じSolverの処理モードです。任意実行のReviewer Agentではありません。
両モードは入力と出力が他モードより限定されるため、共通fragmentを合成しない独立Promptにしています。

### 各ファイルの役割

| ファイル | 内容 |
|---|---|
| `solver_common.md` | 判断主体、WorkItem・Hypothesis・Evidence、ID、Cycleに関する全モード共通の不変条件。 |
| `solver_tools.md` | OpenSearch候補の`search_candidates`投影、本文取得、1ホップGraph探索、RelationAssertionの意味と方向。Toolを使えるモードだけに合成する。 |
| `solver_completion.md` | grounding Evidence、citation、下位規範、通常完了と上限時限定回答の共通条件。 |
| `solver_research.md` | 初回の作業分解、仮説、法令検索表現、最初の探索。 |
| `solver_integration.md` | 観察結果の評価、状態更新、下位規範監査、次の行動。 |
| `solver_cycle_close.md` | Cycle終了と次Cycleへの構造化引継ぎ。 |
| `solver_finalization.md` | `finalize_only=true`時の限定最終化。 |
| `solver_reviewer_revision.md` | Reviewer Findingの受領、反映、反論、再調査。 |
| `solver_search_review.md` | OpenSearch候補を候補別の検索抜粋から全件要約する。この一時結果では候補を選ばない。 |
| `solver_search_reselection.md` | 検索抜粋を再掲せず、前段の短い自己要約一覧から本文取得候補を選ぶ。 |
| `solver_graph_review.md` | Graph差分候補の`select / defer / reject`と本文取得順。 |

### モードの選択

選択処理は`app/agent_framework/loop.py`、Promptの組み合わせは
`app/domains/legal/profiles.py`を正本とします。選択には次の構造情報だけを使います。

1. 未評価Graph候補があるか
2. 未評価OpenSearch候補があるか
3. Reviewer Findingがあるか
4. `finalize_only=true`か
5. `cycle_close_required=true`か
6. すでにToolResultを得た後か

法的意味を見てモードを選択したり、Programが検索方法を補完したりしません。
実際に選ばれた用途は診断情報の`modelCalls[].purpose`で確認できます。

### 出力契約と修復Prompt

このディレクトリのPromptだけがProvider入力の全体ではありません。
`StructuredJSONModelAdapter`が、用途別Promptへ次を追加します。

- `SolverDecision`の共通出力原則と状態契約
- Provider別の構造化出力・輸送指示
- 現在の`SolverContext`
- 契約違反があった場合だけ、該当違反に対応する修復指示

修復指示のassetは`app/agent_framework/prompts/`にあります。法令判断の手順はこのディレクトリ、
Provider輸送と契約修復はFramework側へ分け、同じ規則を両方へ重複記載しません。
JSON SchemaとPydantic型が出力形状の正本であり、Promptは値の意味と使い方を説明します。
LLMが返すToolRequestの`request_id`は同じDecision内の参照用です。AdapterがCase内で一意な
永続化用IDへ置き換え、同じDecisionの`action_request_id`も機械的に追随させます。
Toolの種類、引数、対象WorkItem・Hypothesisは変更しません。

## Reviewer

`reviewer.md`は任意実行のReviewer Agent専用です。最終回答、WorkItem、Hypothesis、Evidenceの
整合性を検査しますが、検索、Tool選択、CaseState更新は行いません。
`reviewer_revision`はReviewer自身ではなく、Findingを受け取ったSolverの処理モードです。

## 非同期法令関係分類

次のPromptは回答時のSolverループとは別に使います。

| ファイル | 内容 |
|---|---|
| `relation_classifier.md` | 構造確認済みのArticleペアと参照箇所について、指定された意味関係が成立するかを判定する。 |
| `relation_grounder.md` | 成立済みの意味関係を変更せず、SUBJECT・OBJECTと既知の根拠IDを対応付ける。 |

## 変更時の原則

- 共通Promptには、すべてのモードで必要な不変条件だけを置きます。
- 手順は用途別Prompt、共有する判断ルールは必要最小限のfragmentへ置きます。
- モード固有の例外を`solver_common.md`へ追加しません。
- Promptだけでschema違反を隠さず、出力形状は型・schema・validatorで検証します。
- Prompt assetを変更したらLegal Profile versionとPrompt契約テストを同じ変更で更新します。
- 合成後のSolver PromptはH1を一つだけにします。共有fragmentと用途別PromptはH2以下から始めます。
- 新しいモードを追加する前に、既存モードの構造条件と責務で表現できないか確認します。
