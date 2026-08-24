# 法令Domain Prompt構成

このディレクトリには、法令調査と法令関係分類でLLMへ渡すPromptを置きます。
このREADMEは保守者向けの説明であり、実行時Promptには合成しません。

## 法令調査Solver

法令調査は一つのSolverが担当します。処理ごとにAgentを増やすのではなく、
`AgentLoop`が現在の構造的な状態から一つの処理モードを選び、そのモードに必要なPromptだけを渡します。
Programが法的関連性、根拠の十分性、候補の採否、次のToolを判断することはありません。

### 合成関係

```text
初回Research（同じSolver・同じCycle）

質問 ──→ Step 1 要求分解 ──→ WorkItemと残りの明示要求
                              │
                              ▼
         Step 2 仮説立案 ──→ Hypothesis
                              │
                              ▼
         Step 3 検索計画 ──→ legal_search要求

各Step = 専用Prompt + 専用入力 + 専用schema + 専用完了確認
初回3 Stepにはsolver_identity / solver_common / solver_toolsを連結しない

Tool結果取得後

通常時：solver_identity + integration + solver_common
                              + solver_tools / solver_completion

Cycle境界：取得本文の評価 ──→ Cycleの終了判断
           各処理 = 専用Prompt + 専用入力 + 専用schema + 専用完了確認
```

用途別の完了確認は`*_check.md`に置きます。完了確認自体は既存の入力を削減・選別せず、
`SolverContext`または候補一覧の後ろへ短く追加します。長い本文を読んだ後でも、
現在の処理が満たすべき条件を出力直前に確認できるようにするためです。
完了確認は新しい意味判断をProgramへ移さず、同じSolverへDecisionの自己点検を要求します。

実際の組み合わせは次のとおりです。

| 呼出し用途 | 合成するPrompt | 役割 |
|---|---|---|
| `research` | question_decompositionのみ | 質問の明示要求を、独立した法的結論を要するWorkItemと、それ以外の明示要求へ分ける。 |
| `hypothesis_generation` | hypothesis_generationのみ | 入力済みの各WorkItemについて、検索前の暫定的な法的命題を作る。 |
| `search_planning` | search_planningのみ | 入力済みHypothesisを検証する今回の`legal_search`要求を作る。 |
| `integration` | identity + integration + common + tools + completion | ToolResultを評価し、状態を逐次更新して次の行動または完了を決める。次Cycle開始後の再計画もこの用途で扱う。 |
| `observation_integration` | observation_integrationのみ | 取得本文を既存Hypothesis・WorkItem・下位規範確認状態へ反映する。次の行動は決めない。 |
| `cycle_close` | cycle_closeのみ | 直前の本文評価を前提に、完了または次Cycleへの引継ぎだけを決める。 |
| `finalization` | identity + finalization + common + completion | 実行上限時に追加Toolなしで、確認済み範囲と未確認範囲を分けた回答を作る。 |
| `reviewer_revision` | identity + reviewer_revision + common + tools + completion | Reviewer Findingを全件処理し、回答修正または追加調査を決める。 |
| `search_selection` | search_review → search_actor_classification → search_reselection | OpenSearch候補を全件要約し、独立した主体照合を経て、今回本文取得する候補を決める。 |
| `graph_selection` | graph_reviewのみ | 新しい1ホップGraph候補を差分評価し、本文取得する候補と保留・除外を決める。 |

`search_selection`と`graph_selection`は同じSolverの処理モードです。任意実行のReviewer Agentではありません。
両モードは入力と出力が他モードより限定されるため、共通fragmentを合成しない独立Promptにしています。

### 各ファイルの役割

担当する判断が切り替わる用途別Promptには、そのモードの役割を明記します。
共通ルール、Toolルール、完了ルール、出力前チェック、修復断片は独立した担当者ではなく、
合成先の役割を補足するため、重複する役割説明を置きません。

| ファイル | 内容 |
|---|---|
| `solver_identity.md` | Tool結果取得後の複数モードで共有するSolver名と役割。初回3 Stepには合成しない。 |
| `solver_common.md` | 判断主体、WorkItem・Hypothesis・Evidence、ID、Cycleに関する全モード共通の不変条件。 |
| `solver_tools.md` | OpenSearch候補の`search_candidates`投影、本文取得、1ホップGraph探索、RelationAssertionの意味と方向。Toolを使えるモードだけに合成する。 |
| `solver_completion.md` | grounding Evidence、citation、下位規範、通常完了と上限時限定回答の共通条件。 |
| `solver_question_decomposition.md` | 初回Step 1。WorkItemと`non_work_item_requirements`への要求分解。 |
| `solver_hypothesis_generation.md` | 初回Step 2。既知WorkItemに対する法的仮説の立案。 |
| `solver_search_planning.md` | 初回Step 3。既知Hypothesisに対する`legal_search`要求の作成。 |
| `solver_research.md` | v154以前の一括Research Prompt。現行Legal Profileでは使用しない。 |
| `solver_integration.md` | 観察結果の評価、状態更新、下位規範監査、次の行動。 |
| `solver_observation_integration.md` | Cycle境界で、取得本文を既存状態へ反映する単一責務の判断。 |
| `solver_cycle_close.md` | Cycle終了と次Cycleへの構造化引継ぎ。 |
| `solver_finalization.md` | `finalize_only=true`時の限定最終化。 |
| `solver_reviewer_revision.md` | Reviewer Findingの受領、反映、反論、再調査。 |
| `solver_search_review.md` | OpenSearch候補を候補別の検索抜粋から全件要約し、主体以外の内容面をHypothesisと照合する。この一時結果では候補を選ばない。 |
| `solver_search_actor_classification.md` | 前段の候補要約について規律主体だけをHypothesisと照合する。内容面の対応を追加せず、後段で両照合結果の共通部分だけを使う。 |
| `solver_search_reselection.md` | 検索抜粋を再掲せず、前段の短い自己要約一覧から本文取得候補を選ぶ。 |
| `solver_graph_review.md` | Graph差分候補の`select / defer / reject`と本文取得順。 |
| `solver_*_check.md` | 対応する処理の入力後に置く、短い出力前完了確認。処理本体の手順や新しい出力項目は定義しない。 |
| `minimal_hypothesis_diagnostic.md` | 本番Promptを合成せず、質問分解・WorkItem・具体的な法的仮説だけを実モデルで切り分ける診断用Prompt。本番Profileでは使用しない。 |

Search Reviewで保留した候補と、本文取得が未完了の選択候補は、次のStep / Cycleでも
`search_candidates`へ再投影されます。新規未評価候補があるときは新規候補だけをSearch Reviewへ渡し、
過去の候補は通常の統合処理で再利用します。

### モードの選択

選択処理は`app/agent_framework/loop.py`、Promptの組み合わせは
`app/domains/legal/profiles.py`を正本とします。選択には次の構造情報だけを使います。

1. 未評価Graph候補があるか
2. 未評価OpenSearch候補があるか
3. Reviewer Findingがあるか
4. `finalize_only=true`か
5. `cycle_close_required=true`か。この場合は取得本文の評価、Cycle終了判断の順に2回呼び出す
6. 初回でWorkItemがまだないか
7. WorkItemはあるがHypothesisがまだないか
8. 初回HypothesisはあるがToolをまだ実行していないか
9. すでにToolResultを得た後か

法的意味を見てモードを選択したり、Programが検索方法を補完したりしません。
実際に選ばれた用途は診断情報の`modelCalls[].purpose`で確認できます。

### 出力契約と修復Prompt

このディレクトリのPromptだけがProvider入力の全体ではありません。
`StructuredJSONModelAdapter`は固定指示と実行時入力を分離し、呼出し時に実送信内容へ組み立てます。
同じ処理モード、Provider輸送方式、契約versionでは、質問やEvidenceが変わっても固定指示のhashは変えません。
固定指示には次を追加します。

- Pydanticの`Field.description`から生成した、初回3 Stepの`input_contract`または
  `SolverContext`と`SolverDecision`の入口だけの`contract_glossary`
- 実行時に利用できる`available_tools`と各Toolの用途・入力Schema・戻り値説明
- `SolverDecision`の共通出力原則と状態契約
- Provider別の構造化出力・輸送指示
- 契約修復モードでは、違反種別に依存しない修復規則一覧

現在の`SolverContext`、候補別名、契約違反、直前出力は実行時入力です。輸送修復も固定規則と
`validation_error`を分離します。レビュー時は、生成された`instructions.md`と`output_schema.json`を対にして確認し、
実行時入力は`input.json`、実送信内容は`request.txt`で確認します。

CaseStoreと完全な`SolverContext`は正本として保持しますが、Providerへは用途別のread modelを渡せます。
初回Researchは同じSolver・同じCycleで3回呼び出します。Step 1には質問だけ、Step 2には質問、WorkItem、
`non_work_item_requirements`、Step 3には質問、WorkItem、Hypothesis、`legal_search`定義と今回の要求数上限だけを
投影します。本文取得枠、Graph、Evidence、後続Cycleの状態は渡しません。各Provider schemaも、そのStepが
判断する意味項目だけを要求します。

Adapterは各Stepの出力へ永続化用IDと既定statusを機械的に付け、共通`SolverDecision`へ正規化します。
Programは文字列の内容を補正せず、ID、件数、既知参照、WorkItemとHypothesisの所属だけを検証します。
Step 1の不変条件は「質問の明示要求全体 = WorkItem + non_work_item_requirements」であり、元の質問は引き続き
CaseStoreの正本です。`non_work_item_requirements`は重要度や全WorkItem共通性を表さず、独立した法的結論を
要しない根拠・出典・引用・対象時点・地域・出力形式等の明示要求を欠落させず保持します。

初回3 Stepの実行時入力は`ResearchStepInput`を正本とし、各Stepで実際に投影した項目だけを`input_contract`へ
生成します。Step 3では`available_tools`の一覧全体の意味と、各Toolが持つ用途・入力Schema・戻り値説明を
完成Promptと`input.json`の組で確認できます。

Cycle境界でも同じ方式を使います。取得本文の評価には既存WorkItem、Hypothesis、grounding Evidence、
下位規範確認対象だけを投影します。続くCycle終了判断には、その評価結果、構造上の残りCycle、
引継ぎ可能なEvidenceを投影します。`fetchable_article_ids`、検索・Graph候補、Tool定義は両方へ渡さず、
Article IDを`retain_evidence_ids`へ混入させない専用契約にします。
前段の意味差分はProgramが機械的に更新後のread modelへ投影します。2つのLLM出力は共通`SolverDecision`へ
正規化し、共通validatorを通過した後にCaseStoreへ一度だけ適用します。

修復指示のassetは`app/agent_framework/prompts/`にあります。法令判断の手順はこのディレクトリ、
Provider輸送と契約修復はFramework側へ分け、同じ規則を両方へ重複記載しません。
Pydantic型とその`Field.description`が項目の形状と基本的な意味の正本です。
`contract_rendering.py`が用語集とProvider schemaの基礎へ決定的に反映します。
入れ子の出力項目はProvider schemaの`description`で説明し、同じ全項目一覧を用語集へ重複掲載しません。
Domain Promptは同じ定義表を手書きせず、現在の処理での手順と判断ルールを説明します。
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
- 指示と出力書式を同じ箇条書きへ混在させません。出力書式には固定された実データ例を置かず、
  プレースホルダーと実際の値を区別します。
- 例は契約項目の必須要素にしません。`description`とルールで解消できない誤解を
  fixtureで確認した場合だけ、契約と分けた例セクションに、固定件数を誘導しない複数例を追加します。
- 出力前完了確認は対応する処理の`*_check.md`へ置き、長い入力の前にだけ記載しません。
- 完了確認fragmentを理由に入力を削除しません。用途別入力投影はModel Profileで明示し、契約テストで固定します。
- モード固有の例外を`solver_common.md`へ追加しません。
- Promptだけでschema違反を隠さず、出力形状は型・schema・validatorで検証します。
- Prompt assetを変更したらLegal Profile versionとPrompt契約テストを同じ変更で更新します。
- 合成後のSolver PromptはH1を一つだけにします。共有fragmentと用途別PromptはH2以下から始めます。
- 新しいモードを追加する前に、既存モードの構造条件と責務で表現できないか確認します。
