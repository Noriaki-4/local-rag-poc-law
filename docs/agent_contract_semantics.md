# Agent契約・Prompt・実行ロジック対応表

## 目的

LLMへ渡す契約項目の意味を、Promptだけで独自に定義しないための対応表である。
項目名・値・意味を変更するときは、型、実行ロジック、Context投影、Provider Schema、Promptを
この順に照合する。

## 正本と役割

| 層 | 正本 | 役割 |
|---|---|---|
| 意味契約 | `app/agent_framework/state.py`、`contracts.py`、`context.py`の`Field.description` | 項目名、値、ID種別、状態の意味 |
| 決定的検証・遷移 | `app/agent_framework/validation.py` | 既知ID、型、全件性、許可された遷移、相互参照を検証する |
| LLM入力の投影 | `app/agent_framework/context.py:build_solver_context` | CaseStateから意味選別せずSolverContextを組み立てる |
| Provider Schema | `app/adapters/models/structured_json.py` | 型の`description`をProvider用JSON Schemaへ転記し、実行時の既知ID・件数上限を加える |
| Prompt用語集 | `app/agent_framework/contract_rendering.py` | 同じ`Field.description`から`contract_glossary`を生成する |
| 手順・判断ルール | `app/domains/legal/prompts/` | 現在の処理段階で、契約項目をいつ、何に基づいて更新するかをLLMへ指示する |

Promptは、型やstatusの意味を上書きしない。Provider Schemaも独自の意味説明を手書きせず、
正規契約の`Field.description`を再利用する。Programは法的関連性や根拠の十分性を決めず、
LLMが返した意味判断の構造だけを検証する。

契約の構造制約と、Profile固有の作業手順は区別する。例えば汎用契約は、
`basis_hypothesis_ids`に値があれば既知IDか、状態と矛盾しないか、必要な引用があるかを検証する。
一方、Legal Profileは各Hypothesisの意味と下位規範確認をSolverに判断させる。Programは登録済みの
Hypothesisがすべて根拠付きで判定済みで、`gaps`と`needs_action`が残らない場合にWorkItemを
`resolved`へ更新し、当該WorkItemに所属する判定済みHypothesisを根拠IDとして集約する。

## 中核項目の対応

| 項目 | 契約上の意味 | Programが行うこと | LLMが行うこと | 主なPrompt |
|---|---|---|---|---|
| `WorkItem.question` | 1つの完了判定で閉じられる確認事項 | ID、一意性、親子循環、状態整合を検証 | 質問を重複しない確認事項へ分解 | `solver_common.md`、`solver_question_decomposition.md` |
| `Hypothesis.work_item_id` | Hypothesisが検証する所属WorkItem | 既知WorkItemとの完全一致を検証 | WorkItemごとに本文で支持・否定できる命題を置く | `solver_common.md`、`solver_hypothesis_generation.md` |
| `WorkItem.basis_hypothesis_ids` | openでは作業の作成・継続を前提づけるHypothesis、resolvedでは所属する判定済みHypothesis | openの前提IDを検証し、resolved時は所属・判定・Evidenceから機械集約する | open子WorkItemを作る場合の前提だけを選ぶ | `solver_common.md`、`solver_cycle_close.md` |
| `Hypothesis.judgment` | `unresolved`は未確認、`supported`はstatementを本文が支持、`contradicted`は本文が否定 | 判定済みならEvidence必須、既知ID、反証時の影響処理を検証 | 提示されたgrounding本文からstatementを判定 | `solver_evidence_integration.md` |
| `Hypothesis.evidence_ids` | 現在までの判定と`gaps`の判断に使った取得本文 | 既存対応をCaseStoreに保持し、`HypothesisUpdate.evidence_ids`の新規IDを追記する。既知かつ提示済みのgrounding Evidence IDを検証し、判定済みでは1件以上必須 | 今回の評価で新たに使った本文だけを差分へ指定する。`unresolved`でも一部確認に使った本文は指定する | `solver_evidence_integration.md` |
| `Hypothesis.gaps` | HypothesisをWorkItemへの回答に使うため、本文でさらに確認すべき具体的情報。`supported`でも残り得る | 文字列として保存・引継ぎ。同じ応答でHypothesisを更新して`needs_action`としたのに、そのWorkItemの全Hypothesisで空になる矛盾を拒否 | 取得本文と下位規範確認から追加・維持・解消を判断する | `solver_evidence_integration.md` |
| `Evidence` | Toolから得た保存済み情報 | 来歴、本文提示有無、引用可能性を投影 | `material_evidence`に本文があるgrounding Evidenceだけで意味判断 | `solver_evidence_integration.md`、`solver_finalization.md` |
| `evidence_hypothesis_candidates` | 取得Articleと、本文取得前に対応候補と判断されたHypothesisの来歴 | 保存済みArticle・Hypothesis IDを結合してObservationへ投影 | 手掛かりとして本文を再評価し、支持・反証または部分確認を判断する | `solver_evidence_integration.md` |
| `ToolRequest` | open WorkItem・Hypothesisを検証するread-only Tool要求 | Tool名、引数Schema、既知ID、件数、重複scopeを検証して実行 | Hypothesisと、観察後に残ったgapに応じてToolと引数を選ぶ | `solver_tools.md`、各モードPrompt |
| `DependencyDecision` | 質問に関係する下位規範確認の状態 | 対象WorkItem全件性、Evidence、ToolRequest対応を検証 | 本文から`not_required / needs_action / resolved`を判断 | `solver_completion.md`、`solver_integration.md` |
| `SearchCandidateReview` | OpenSearch候補の本文取得対象、対応Hypothesis、保留候補 | 候補ID、Hypothesis ID、重複、全件性、取得上限を検証 | 検索抜粋をHypothesisに照らして選別 | `solver_search_review.md`、`solver_search_reselection.md` |
| `GraphCandidateReview` | Graph候補の`select / defer / reject`判断 | Link・Frontier・Request ID、全件性、上限を検証 | Relationの種類・方向とHypothesisから関連性を判断 | `solver_graph_review.md` |
| `SolverDecision.next` | 次のaction-observationを続けるか、回答を確定するか | actionまたはanswerの有無、Cycle境界を検証 | 根拠、gap、上限から`continue / finalize`を判断 | 全Solverモード |
| `required_transition` | Observation・Dependency反映後に必要なCycle遷移 | Hypothesis等から導出したWorkItem進捗と次Cycle可否から`start_next_cycle / finalize`を導出 | 指定された遷移に応じて引継ぎ内容または最終回答を作る | `solver_cycle_close.md` |
| `start_next_cycle` | 内部`SolverDecision`で現Cycleを閉じて次Cycleへ移ることを表す | `required_transition`から設定し、Cycle上限と境界処理との整合を検証 | 直接出力しない | なし |
| `FinalAnswer` | 根拠付き回答、引用、制約、未解決ID | 引用可能Evidence、open WorkItemとの一致、下位規範根拠を検証 | 取得本文が示す範囲で回答を統合 | `solver_completion.md`、`solver_finalization.md` |

## `basis_hypothesis_ids`の状態別意味

同じ項目を単なるWorkItemとHypothesisの逆参照として使わない。

```text
質問から直接作るopen WorkItem W1
├─ basis_hypothesis_ids = []
└─ Hypothesis H1
   └─ work_item_id = W1

H1を前提に追加したopen子WorkItem W2
└─ basis_hypothesis_ids = [H1]

W1の全HypothesisとDependency確認が完了
├─ state = resolved（Programが導出）
└─ basis_hypothesis_ids = [W1に属する判定済みHypothesis]
```

open WorkItemのbasisが新たに`contradicted`になった場合、ProgramはそのWorkItemを自動変更せず、
`WorkItemImpactDecision`の対象IDを決定的に要求する。維持・置換・破棄の意味判断はSolverが行う。

Cycle Closeではopen WorkItemを未完了の正本とする。本番Loopでは、本文取得後のWorkItem単位の
Observation IntegrationがHypothesis更新、下位規範判断及び直後のTool最大1件を同じDecisionで返し、
ProgramがWorkItem進捗を導出する。どの法的命題が必要か、本文が命題を
支持・否定するか、下位規範確認が必要かはSolverが判断し、Programはその意味判断を補わない。

Observation IntegrationのTool要求が重複又は探索上限により実行前に棄却されても、同じDecisionの
Hypothesis、gap及びEvidenceの差分まで巻き戻さない。Programは棄却対象のTool要求と、それを参照する
`DependencyDecision.action_request_id`だけを除き、残る意味差分を通常の契約で検証して保存する。
意味差分自体が契約違反の場合だけ、そのWorkItemをLLM修復へ戻す。

## 変更時の確認順序

1. `Field.description`で項目の意味とID種別を確定する。
2. `validation.py`がその意味に反する遷移を許可・強制していないか確認する。
3. `build_solver_context`がLLMへ別の意味で投影していないか確認する。
4. Provider Schemaが`contract_field_description()`から同じ説明を取得しているか確認する。
5. `contract_glossary`へ同じ説明が生成されるか確認する。
6. Promptには項目の再定義ではなく、現在の処理での手順と判断基準だけを書く。
7. fixtureで契約項目間のID対応を固定し、契約・Prompt・Schemaの整合テストを行う。
8. Prompt assetを変更した場合はProfile versionを更新する。

`SolverContext`と`SolverDecision`から到達できるLLM可視項目に`description`がない場合、
契約テストを失敗させる。主要なProvider更新Schemaについても、Pydanticのdescriptionと一致することを
テストし、手書き説明の分岐を防ぐ。
