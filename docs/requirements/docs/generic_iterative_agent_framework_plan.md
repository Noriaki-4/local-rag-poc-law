# シンプルな汎用反復型エージェント基盤 実装計画

> 更新日: 2026-08-19
>
> 本書を新しい実装ロードマップの正本とする。
> 人間向けの概念図と処理イメージは、対になる
> [`generic_iterative_agent_framework_plan_visual.md`](generic_iterative_agent_framework_plan_visual.md)を参照する。
> 図解側は理解補助であり、契約・完了条件が食い違う場合は本書を正とする。
> 過去の実装計画やProfile変更履歴は本書へ残さない。現在のコードから目標仕様へ移すために
> 必要な差分、実装順、完了条件だけを記載する。

## 実装状況（2026-08-19）

この節は変更履歴ではなく、12章の完了条件に対する差分だけを示す。個別のProfile version、
不具合修正、実測結果はGit履歴、[RUNBOOK](../../../RUNBOOK.md)、
[評価設計](evaluation_design.md)を参照し、本書へ時系列に追記しない。

| Phase | 状況 | 未完了の中心 |
|---|---|---|
| Phase 0 | 一部完了 | 代表2問の現行baseline、説明付きstatus契約、生成schema・Prompt用語集のfixture |
| Phase 1 | 一部実装 | `CycleRecord / StepRecord`、discriminator付きCommand、型付きstatusと遷移の一元化、再開契約 |
| Phase 2 | 非同期分類のexport・検証importまで実装 | 実indexの再構築・全件Run、Hypothesis別selector、旧自動Graph経路の撤去 |
| Phase 3 | 未評価 | 新契約に基づくtrace、再開、入力増加、latencyの完了条件 |
| Phase 4 | 未完了 | 新経路による代表2問の合格、既定経路切替、旧試作の撤去 |

現在利用できるものと目標仕様を混同しない。

- `agent_framework`、`InMemoryCaseStore`、用途別Model Profile、read-only Tool並列実行、
  任意Reviewer、法令Tool Adapter、Feature Flag付き経路は存在する。
- Reviewerの既定値は無効で、新経路のFeature Flagも既定では無効である。
- 現行Legal Profileは本文取得に旧Graph検索を自動連動し、
  `domains/legal/profiles.py`の`automatic_tools.fixed_arguments`で
  `REFERENCES / IMPLEMENTS / APPLIED_BY`の3種を固定指定する。別に、`legal_ontology.py`の
  `expandable_edge_types()`は`EDGE_REGISTRY`から`EXPLAINS`を含む4種を導出するが、現行Legal Profileの
  自動Graph設定はこの関数を使用していない。本書が目標とする
  Hypothesis別selector、`from_subject / to_subject`、5 predicateの新Graph契約ではない。
- 現行CaseStateはWorkItem、Hypothesis、Evidence、Graph review履歴を保持するが、
  本書の`ExplorationState / CycleRecord / StepRecord`と説明付きstatus契約は未実装である。
- schema version 9のseedは、同じsnapshotのOpenSearch本文とNeo4jの構造・
  `REFERENCES / EXPLAINS`だけを作り、旧`APPLIED_BY / MENTIONS / RelationAssertion`を生成しない。
  新しい5 predicate契約、候補単位checkpoint、再開可能CLI、Neo4j保存、publish監査は実装済みである。
  実データの再seed・分類と検索時selectorへの接続はまだ行っていない。
- Luna用のlabel-free候補packetと最大5件のshardを決定的に生成するIFは実装済みである。
  packetはsnapshot、schema、prompt、Worker / Reviewer model、両Article全文、全参照出現を含み、
  goldやexpected predicateを型上受け付けない。実indexでのexportは再seed後に行う。
- 現行CLIのOllama `gemma4:e4b`経路はローカル契約試験用であり、手動監査14件の5 predicate完全一致が
  4/14だったため、全件publish用の品質経路には採用しない。正本分類はCodexサブスクリプション内の
  `gpt-5.6-luna`をWorker / Reviewerの両方に使い、候補を複数ペアへ分割して並列実行する。
  既存14件、新規20件、法令94件＋ガイド6件の代表100件でこの方式を確認済みである。
  判定JSONLはReviewer承認証跡付きで`ClassificationRun`へ取り込む検証importを実装済みである。
  詳しい比較結果と運用手順は[RUNBOOK](../../../RUNBOOK.md)を正とする。
- 旧`legal-relation-classifier-v8`は、schema version 7の旧`IMPLEMENTS`候補を
  `implements / reference_only / uncertain`へ分類する移行用機能である。
  本書の5 predicate、`ClassificationRun`、`SUBJECT / OBJECT / CLASSIFIED_IN`を備えた
  新Graph schemaの完成を意味しない。全候補への分類結果登録も実施していない。
- 現行コードには、旧回答経路、新しい`agent_framework/ + domains/legal/`経路、
  `agent_core/`を使う別試作の3系統がある。`framework_agent.py`は`agent_core/`を直接使わず、
  新Framework用の`adapters/persistence/simple_in_memory.py`を使う。`agent_core/`は`llm.py`と
  `adapters/persistence/in_memory.py`から参照されているため、未接続とは扱わないが、本書の移行先にも含めない。

実装済みかどうかはコードとテスト、品質値は評価成果物を正とする。本節とコードが食い違う場合は、
本節へ新しい履歴を足さず、差分表を更新する。

## 1. 決定事項

本計画では、次を確定事項とする。

1. Codex型の反復ループを維持する。
2. 仮説検証を小さく繰り返し、1 Cycle内で複数のaction-observation stepを実行する。
3. 汎用基盤と検索対象のDomain Packを分離する。
4. 仮説、検索scope、関連性、根拠、完了はSolverが判断し、プログラムは実行事実と構造契約だけを扱う。
5. 回答AgentのLLM登場人物はSolverと任意Reviewerだけにし、Projector等を独立Agentにしない。
   オフラインRelation分類のWorker / Reviewerは回答Agentの登場人物へ含めない。
6. CaseStoreを案件状態の唯一の正本とし、Solverは安定IDを使った変更差分だけを返す。
7. Graph探索は案件内Graph、frontier、展開済みscopeで管理し、LLMには未評価・再評価差分と短い台帳を渡す。
8. status、Command、Tool、Model Profileは型付き契約を正本とし、PromptとProvider schemaを同期する。
9. Reviewerは任意かつ既定無効、初期CaseStoreはインメモリ、同一Runのproviderは1つとする。
10. 法令Graphの物理定義、探索上限、非同期Relation分類は5章と7章、実装順は12章を正とする。
11. SQL生成、DB永続化、サブエージェントは初期実装の対象外とする。

## 2. 目的と非目的

### 2.1 目的

検索対象に依存せず、作業分解、Hypothesis作成、検証行動、Tool結果の観察、状態更新、
完了判定、根拠付き成果返却を反復するエージェントループを実装する。

法令検索は、このループを最初に接続して評価する業務ドメインである。

現行実装で1問に3分以上かかる主因である、固定3サイクルと
`explore → deepen → integrate`の直列LLM呼び出しを廃止する。

### 2.2 非目的

初期実装では、次を行わない。

- DB永続化
- Repositoryのエンティティ別分割
- Unit of Workや疑似DB transaction
- EventJournalを正本にするイベントソーシング
- Projector、Scheduler、Integrator等のサービス分割
- 回答Agent内のサブエージェント（オフライン意味分類のWorker / Reviewer並列ペアは別処理）
- 書込みを含む並列実行
- SQL生成
- 自動的な法的判断

将来必要になった機能は、実測された要求が出た時点で追加する。

## 3. 全体構成

構成図、1 Cycleの流れ、CaseStoreとAgentViewの関係は
[対になる視覚ガイド](generic_iterative_agent_framework_plan_visual.md)へ分離する。本章は実装責務だけを定義する。

### 3.1 実行コンポーネント

| コンポーネント | 種別 | 責務 |
|---|---|---|
| Solver | LLM | 作業分解、Hypothesis、検索scope、Evidence採否、Cycle継続・終了、最終回答を判断する |
| Reviewer | 任意LLM | 最終回答案と根拠の不整合を指摘する。既定では無効 |
| AgentLoop | Program | Model・Tool呼出し、予算、checkpoint、構造検証、CaseStore更新を行う |
| Context Projector | Program | CaseStoreの正本から用途別AgentViewを決定的に生成する。独立Agentにはしない |
| ModelPort | Port | provider差を隠し、用途別Profileで選んだmodelへ構造化入出力を提供する |
| ToolPort | Port | Tool定義、権限、read-only・parallel-safe属性、実行結果を共通化する |
| Legal Domain Pack | Domain | 法令Prompt、Evidence変換、検索selector、Graph語彙を提供する |
| OpenSearch / Neo4j Adapter | Adapter | 固定されたTool契約を各backendへ変換する。自由queryをLLMへ開放しない |
| CaseStore | Data Store | 案件状態、探索履歴、Evidence、Cycle・Step checkpointを保持する |

Solverのresearch、integration、Graph Reviewは別Agentではなく、同じSolverの呼び出し用途である。
Projector、Explorer、Integrator、Answerer、Schedulerを独立した登場人物として追加しない。

### 3.2 判断境界

| 判断・処理 | 担当 |
|---|---|
| WorkItem分解、Hypothesis、検索語、Graph selector、候補の関連性、Evidence採否、完了判断 | Solver |
| 最終回答の指摘と差戻し | Reviewer（有効時のみ） |
| 既知ID、enum、件数、権限、予算、参照整合、status遷移、Tool実行事実 | Program |
| 検索候補生成、本文取得、Graph scope実行 | Tool Adapter |

Programは意味判断を補完しない。LLMの出力が構造契約に違反した場合は、既知の候補へ勝手に置換せず、
契約修復または明示的な停止へ進む。

### 3.3 Data Storeの責務

| Data Store | 保存する正本 | 読み出すもの | 保存しないもの |
|---|---|---|---|
| CaseStore | Case、WorkTree、Hypothesis、ExplorationState、Cycle・Step、ToolResult、Evidence、案件内判断 | AgentView生成に必要な案件状態 | 共有法令本文、共有Graph schema |
| OpenSearch | snapshotに属する法令・ガイド本文と検索用index | 候補と指定Articleの全登録済み本文 | 案件ごとの関連性、Hypothesis、Evidence採否 |
| Neo4j | 法令構造、原文`REFERENCES / EXPLAINS`、publish済みRelationAssertionとClassificationRun | 明示selectorに一致する1ホップ候補とcoverage | 案件ごとの判断、本文取得状態、回答 |

非同期Relation分類は回答AgentのCaseStoreを使用しない。OpenSearch・Neo4jの固定snapshotから
`RelationClassificationCandidate`を作り、分類Runのcheckpointと結果を専用jobとして保存する。
分類入力・出力の正式契約は5.1.3、AgentViewとSolverDecisionは5.3、ToolResultは5.4を正とする。

## 4. 反復ループ

### 4.1 Solver判断と1サイクル

1サイクルは、1件または明示的に束ねた少数のHypothesisについて、1つの仮説・探索方針を立て、
その方針に必要な根拠を予算内で探し、結果全体を評価するまでの試行単位である。
根拠を探し切った場合だけでなく、本文取得数・step・Cycle時間の上限に達した場合も、
未評価のToolResultを残さずSolverの終了評価を通してCycleを閉じる。1回のToolRequest、
1回のLLM呼び出し、1ホップのGraph展開をサイクルとは呼ばない。

1. Solverがfocus、Cycle goal、探索方針、完了・失敗条件を決める。
2. SolverのToolRequest、Programによる保存・Tool実行、Solverによる観察と状態更新をStepとして反復する。
3. Programが上限前に新しいactionを止め、SolverがCycle全体を評価する。
4. Solverが`finalize`または次Cycleへの引継ぎを決める。次Cycleのgoal・strategyは次の`start_cycle`で決める。
5. Programが`CycleRecord`を閉じた後にcycle数を加算する。

各Solver呼び出しは、現在のCycle goal、WorkTree、Hypothesis、探索frontier、直前の
ToolResultとEvidenceを読み、次のdiscriminator付きCommandのいずれかを返す。

- `start_cycle`: Active Cycleがないとき、今回のgoal・strategy・完了条件を決めてCycleを開始する。
- `continue_cycle`: 同じ仮説・探索方針のまま、次の検証用ToolRequestを返す。
- `start_next_cycle`: 今回の方針では完了できない理由とCaseStateの変更差分を返し、
  現Cycleを閉じて次Cycleへ引き継ぐ。次Cycleの計画やToolRequestは返さない。
- `finalize`: 必要な根拠を探し切ったと判断し、CaseStateの変更差分と根拠付き最終回答を返す。

同じCycleの間は、OpenSearchで起点を発見し、起点本文と1ホップを取得し、LLMが選んだ隣接本文を取得する。
隣接本文からGraphは再展開しない。新しいGraph候補の関連性と本文取得対象は
各stepでSolverが判断し、プログラムが再帰的に自動選択しない。

処理イメージは[視覚ガイドの「1 Cycleの流れ」](generic_iterative_agent_framework_plan_visual.md#4-1-cycleの流れ)を参照する。

`research_cycle_count`はTool終了時やstep終了時には増やさない。Cycle全体の評価差分が契約検証を通り、
`CycleRecord`が`completed`になった時点で増やす。Tool成功後・評価前に停止した場合は同じCycleの
同じ`StepRecord.observed`から再開し、別Cycleとして数えない。

1サイクルで質問全体の候補を無制限に広げない。Cycle累計の本文取得数、各stepで選択できる
frontier件数、Graph depth、step数、Tool数、Cycle時間をProfileで機械的に制限する。
Solverが同じHypothesisについて明示した1ホップGraph検索は同じstepの観察結果に含め、
隣接Article本文は同じCycleの次stepでSolverが選んで取得する。

### 4.2 サイクル数と時間確保

- 通常: 1〜2 research cyclesで完了する
- 多段探索または再計画が必要な場合: 3〜4 research cycles
- 上限: 4 research cycles
- 1 Cycleの本文取得累計上限: Profileの`max_fetched_resources_per_cycle`。Legal初期値は4
- 1 Cycle内のaction-observation step上限: Profileの`max_steps_per_cycle`
- Run全体のstep上限: Profileの`max_total_steps`
- Solverが`finalize`を返した時点で即終了
- Cycle内step上限、Cycle回数上限、Run全体step上限のいずれかに達した場合は、現在の上限条件で
  許されないToolRequestを実行せず、手元の根拠と探索方針を評価する

Reviewer無効時のSolver呼び出しは次の範囲になる。

| ケース | LLM呼び出し |
|---|---:|
| 取得済み情報だけで回答可能 | 1回 |
| 1 Cycle内で2 action stepを実行 | 3回程度 |
| 1 Cycle内で4 action stepを実行 | 5回程度 |
| 別Cycleへ再計画 | 前Cycleのstep数に次Cycleの呼び出しを加える |

プログラムは各LLM・Tool実行前に、現Cycleを評価して閉じる時間、残りCycleの最小実行時間、
最終回答時間を別々予約する。残り時間が現actionの実行予算と予約の合計を下回る場合、
新しいGraph ReviewやToolを開始せず、`cycle_close_required=true`をSolverへ渡す。
Solverは手元の結果を評価し、`finalize`または次Cycleへの結果・未解決事項・frontierの引継ぎを返す。
次Cycleを開始する場合は、その後の`start_cycle`呼出しでgoal・strategyを決める。
予算によって短縮された中間LLM呼出しがtimeoutした場合は、実際のprovider障害と区別して
`cycle_step_timeout`を保存し、予約時間でCycle終了判断へ進む。

4 Cycleを必ず実行するループにはせず、Tool結果を未評価のまま次Cycleへ進むことも許さない。

### 4.3 ツールの並列実行

Solverは1回の`continue_cycle`で複数のToolRequestを返せる。

初期の法令検索ツールはread-onlyなので、同じSolver stepで返された要求を上限内で並列実行する。
プログラムが要求の意味的な独立性を推測してはならない。並列化できるのは、Tool定義が
`read_only=true`かつ`parallel_safe=true`と明示している場合だけとする。

並列実行後は、全ToolResultをまとめて次のSolverへ渡す。独立したExplorerエージェントは作らない。

### 4.4 上限到達

回数・時間・provider障害は、意味上の根拠不足とは区別する。

- 回数上限: `max_research_cycles`
- Cycle内の本文取得累計上限: `max_fetched_resources_per_cycle`
- Cycle内step上限: `max_steps_per_cycle`
- Cycle時間上限: `max_cycle_wall_time_sec`
- Run全体step上限: `max_total_steps`
- 全体時間上限: `max_wall_time_sec`
- LLM timeout: `model_timeout`
- Tool timeout: ToolResultの`timeout`
- provider障害: `provider_error`
- 構造化出力不正: `protocol_error`

上限到達時、プログラムはHypothesisを`unresolved`へ変更しない。
停止理由と手元の根拠をSolverへ渡し、限定付き回答を含む最終判断をLLMへ求める。

`cycle_budget_reached=true`または`cycle_step_limit_reached=true`でも残りCycle・総step・時間がある場合、
Solverは現方針の結果と未解決命題を引継ぎに示して`start_next_cycle`を選べる。仮説・探索方針の仕切り直しに加え、
Cycleの本文取得枠が尽きても必要と判断した未取得Evidenceが残る場合も対象とする。単なるTool終了や
Graph 1ホップ完了だけを理由にせず、取得済みEvidenceの評価と引き継ぐfrontierを明示する。
Cycle回数、総step、全体時間のいずれかで新しいToolを実行できない場合だけ
`finalize_only=true`とし、`start_cycle / continue_cycle / start_next_cycle`を禁止する。

## 5. 状態と契約

### 5.1 最小CaseState

初期実装は、次を1つの`CaseState`として保持する。

```python
class CaseState:
    case_id: str
    question: str
    contract_version: str
    source_snapshot_id: str
    graph_schema_version: int
    classification_run_id: str | None
    run_status: RunStatus
    research_cycle_count: int
    work_items: list[WorkItem]
    hypotheses: list[Hypothesis]
    exploration: ExplorationState
    cycle_records: list[CycleRecord]
    tool_requests: list[ToolRequest]
    evidence: list[Evidence]
    tool_results: list[ToolResult]
    focus_work_item_ids: list[str]
    retained_evidence_ids: list[str]
    final_answer: FinalAnswer | None
    review: ReviewResult | None
    stop_reason: str | None

class FinalAnswer:
    text: str
    citation_evidence_ids: list[str]
    limitations: list[str]
    unresolved_work_item_ids: list[str]
    unresolved_hypothesis_ids: list[str]
```

Legal ProfileはCase作成時に同じsnapshotのOpenSearch・Neo4jと、publish済みClassificationRunを固定する。
publish済みRunがない場合は`classification_run_id=None`とし、意味Assertionが利用できないことをAgentViewへ
明示する。実行途中に新Runへ自動切替しない。`contract_version`もCase作成時に固定し、load時に現行契約と
異なる場合は登録済みmigrationまたは旧値読替えを通す。対応経路がなければ実行を再開しない。

初期実装では、Case、WorkItem、Hypothesis、ExplorationState等を別Repositoryへ分割しない。
安定IDを持たせるが、単一プロセスで不要なrecord revisionやleaseは持たせない。

### 5.1.1 CaseとWorkItemの関係

`Case`は利用者の案件全体である。`parent_work_item_id=None`のWorkItemは案件直下の作業、
子WorkItemは分解された下位作業、確認作業、個別の問いを表す。階層によって型や名称を変えず、
固定階層数も設けない。

```python
class WorkItem:
    work_item_id: str
    parent_work_item_id: str | None
    question: str
    state: Literal["open", "resolved", "dropped"]
    resolution: str | None
    basis_hypothesis_ids: list[str]
    replaces_work_item_id: str | None

class Hypothesis:
    hypothesis_id: str
    work_item_id: str
    statement: str
    judgment: Literal["supported", "contradicted", "unresolved"]
    evidence_ids: list[str]
    gaps: list[str]
```

- Hypothesisは必ず1つのWorkItemへ所属する。
- Evidenceは複数HypothesisからIDで共有参照し、WorkItemごとに複製しない。
- Hypothesisのstatementを別の意味へ上書きしない。見立てを変更する場合は新しいHypothesisを作る。
- WorkItemのquestionを別の問いへ上書きしない。問いを変更する場合は旧WorkItemを`dropped`にし、
  `replaces_work_item_id`を持つ新しいWorkItemを作る。
- 親子関係は作業分解を表す。WorkItem間に別の依存関係Graphを作らず、次の対象は
  Solverが`focus_work_item_ids`で指定する。

### 5.1.2 再帰探索に強い案件内データ構造

WorkItemの親子構造と、検索対象の探索構造を混在させない。WorkItemは問いの分解を表す木、
ExplorationStateは情報源の発見・取得・展開を表す案件内Graphとする。

探索対象は純粋な木にしない。同じResourceが検索と複数のGraph関係から発見されること、相互参照で
循環することがあるため、Resourceは案件内で1 Nodeに正規化し、発見経路は複数Linkとしてすべて残す。

最初に発見した親や最小hopから表示用の探索木を派生できるが、その木を正本にしない。
法令GraphのLinkが既存Nodeへ戻ってもLinkは保存し、本文取得と同一scopeのGraph展開は重複実行しない。

Framework側はResourceとLinkの一般形だけを持ち、`Article`、`REFERENCES`等はLegal Domain Packの
`resource_kind`と`relation_metadata`へ置く。

設計根拠は、一般的なGraph探索の`frontier + explored set`、Neo4jのnode/path単位のuniqueness、
状態ful agentのstep checkpoint、データ来歴のEntityとDerivationの分離である。法令ドメインについては
LegalRuleMLの、形式化した法的Statementを原文のLegal Sourceへ対応付けるisomorphism、Context、Authority、
Temporal Characteristicの考え方も参考にする。本計画ではLegalRuleMLのXMLや全メタモデルをNeo4jの物理schemaへ
移植せず、Hypothesis・RelationAssertion等の意味仮説と、取得本文、出典、判断時点を混同しないために利用する。
取得元に版・施行期間がない場合は推測せず`unknown`として扱う。外部Frameworkへの依存は追加せず、
この案件に必要な最小型だけを実装する。

- [Generic graph search: frontier](https://artint.info/3e/html/ArtInt3e.Ch3.S4.html)
- [Multiple-path pruning: explored set](https://artint.info/3e/html/ArtInt3e.Ch3.S7.html)
- [Neo4j APOC: BFS/DFS、depth、uniqueness](https://neo4j.com/docs/apoc/current/graph-querying/expand-paths-config/)
- [LangGraph persistence: step checkpoint](https://docs.langchain.com/oss/python/langgraph/persistence)
- [W3C PROV: entity、activity、derivation](https://www.w3.org/TR/prov-primer/)
- [OASIS LegalRuleML Core Specification 1.0](https://docs.oasis-open.org/legalruleml/legalruleml-core-spec/v1.0/os/legalruleml-core-spec-v1.0-os.html)

```python
class ExplorationState:
    nodes: list[ExplorationNode]
    links: list[DiscoveryLink]
    frontier: list[FrontierItem]
    intents: list[ExplorationIntent]

class ExplorationIntent:
    intent_id: str
    work_item_id: str
    hypothesis_ids: list[str]
    objective: str
    discovery_kind: Literal["search", "relation"]
    selectors: dict
    reason: str
    created_cycle: int

class ExplorationNode:
    exploration_node_id: str
    resource_id: str
    resource_kind: str
    minimum_depth: int
    discovered_cycle: int
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]
    evidence_ids: list[str]
    related_hypothesis_ids: list[str]
    expansions: list[ExpansionSlice]

class DiscoveryLink:
    link_id: str
    from_node_id: str | None
    to_node_id: str
    discovery_kind: Literal["search", "relation"]
    navigation_evidence_id: str
    relation_metadata: dict
    discovered_cycle: int

class ExpansionSlice:
    scope_key: str
    policy_version: str
    intent_ids: list[str]
    selector_scope: dict
    status: Literal["not_started", "pending", "partial", "complete", "failed", "timeout"]
    page_request_ids: list[str]
    next_cursor: str | None
    discovered_link_ids: list[str]

class FrontierItem:
    frontier_item_id: str
    node_id: str
    via_link_ids: list[str]
    work_item_id: str
    hypothesis_id: str
    minimum_depth: int
    review_status: Literal["unreviewed", "selected", "relevant_deferred", "rejected"]
    last_reviewed_cycle: int | None
    last_reviewed_step: int | None

class FrontierDecision:
    frontier_item_id: str
    action: Literal["select", "defer", "reject"]
    reason: str

class GraphReviewItem:
    frontier_item_id: str
    review_trigger: Literal["new_frontier", "re_adopted", "new_link"]
    prior_review_status: Literal["selected", "relevant_deferred", "rejected"] | None
    link_ids: list[str]

class GraphReviewLedgerItem:
    frontier_item_id: str
    node_id: str
    article_id: str
    work_item_id: str
    hypothesis_id: str
    review_status: Literal["selected", "relevant_deferred", "rejected"]
    reason: str
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]
    last_reviewed_cycle: int
    last_reviewed_step: int

class CycleRecord:
    cycle_no: int
    phase: Literal["planned", "running", "completed"]
    goal: str
    strategy: str
    completion_criteria: list[str]
    focus_work_item_ids: list[str]
    focus_hypothesis_ids: list[str]
    frontier_before_ids: list[str]
    steps: list[StepRecord]
    fetched_resource_ids: list[str]
    budget_stop_reason: Literal["resource_limit", "step_limit", "time_limit"] | None
    completion_reason: str | None
    frontier_after_ids: list[str]

class StepRecord:
    step_no: int
    phase: Literal["planned", "observed", "completed"]
    tool_request_ids: list[str]
    observed_evidence_ids: list[str]
    applied_update: CaseUpdate | None
    frontier_decisions: list[FrontierDecision]
```

探索規則:

1. Solverは各検索の`ExplorationIntent`へ既知WorkItem・Hypothesis、検証目的、検索経路、selectorを指定する。
   Legal Profileのsearch selectorはquery・検索mode・任意filter、relation selectorは起点Article、
   Graph mode、1つのpredicateまたは原文relation、1つのdirection、分類Run、任意の構造filterを持つ。
   Programは意味的なselectorを追加・削除しない。
2. OpenSearch候補を深さ0のNodeと、IntentのHypothesisに属するfrontierへ追加する。
3. Solverは既知frontier IDから、1 stepで検証する少数の`select`、関連するが今回の
   本文取得枠に入れない`defer`、現在のHypothesisに不要な`reject`を返す。
4. Decisionに現れないfrontierは削除せず`unreviewed`のまま残す。`defer`は
   `relevant_deferred`として同じCycleの後続stepまたは次Cycleへ残す。
5. ProgramはID、selector allowlist、件数、depth、Toolの成功済み重複だけを検証し、
   関連度、優先度、Hypothesisと関係種別の対応を計算しない。
6. 1ホップGraphは、対応Intentが明示したmode・predicateまたは原文relation・direction・構造filterだけを取得して
   同じstepの観察へ追加する。新しい隣接Node本文は同じCycleの
   次step以降に取得する。
7. 同じNodeへ別Linkが追加された場合はLinkとHypothesisの関連だけを追加し、成功済み本文を再取得しない。
   frontierは`Node × Hypothesis`単位にし、あるHypothesisでの`reject`を別Hypothesisへ波及させない。
8. Graph展開済み判定はNode全体ではなく
   `scope_key=(resource_id, mode, predicate_or_relation, direction, structural_filters,
   classification_run_id, policy_version)`単位にする。
   別Hypothesisが同じ物理scopeを要求した場合は既存Linkを再利用して新しいfrontierを作り、Neo4jを再実行しない。
   page cursorとrequest IDは同じExpansionSliceへ蓄積し、pageごとに別scopeを作らない。
9. `partial`と`next_cursor`があるscopeを`complete`として扱わない。未提示候補の不存在を推測しない。
10. `max_exploration_depth`はProfileで`1`または`2`だけを許可する。OpenSearch起点を深さ0、Graph関係を
   1辺たどるごとに深さを1増やす。最大depthのNodeは本文取得とSolverの意味評価を許可するが、そこを
   起点とするGraph展開は実行しない。Programは`minimum_depth < max_exploration_depth`であり、
   かつ既知のrelation用ExplorationIntentがある場合だけ1ホップGraphを実行する。
11. 後から短い経路が見つかった場合、ProgramはNodeと対応frontierの`minimum_depth`だけを小さく更新し、
    過去LinkやCycleRecordを削除しない。
12. `max_exploration_depth`はCase全体に適用する。同じOpenSearch起点からの探索を次Cycleへ引き継いでも
    depthを0へ戻さない。次Cycleの異なる検索で新たに発見したOpenSearch候補だけを新しい深さ0の起点にする。
13. 1 stepの選択件数と1回のGraph取得件数はProfileの機械的上限とし、上限超過候補は削除せず
    `partial`なExpansionSliceの未取得page、または未処理frontierとして残す。Neo4jから取得済みの
    未処理Graph frontierは決定的に分割し、未提示pageを不存在と扱わない。
14. Graph Reviewは全履歴を毎回再評価せず、新しい`unreviewed`候補、新Hypothesisが既存Nodeを
    再採用したことで新たに作られた`Node × Hypothesis` frontier、既評価frontierへ新しいLinkが
    追加された差分を詳細入力とする。過去の評価済みfrontierは短い台帳で参照する。
15. 一度`reject`したfrontierをProgramが別Hypothesisへ自動転用しない。Solverが別Hypothesisの
    検証に再採用した場合は、同じNodeを参照する新しい`unreviewed` FrontierItemを作る。

設定値ごとの到達範囲は次のとおり。

| `max_exploration_depth` | 取得・評価できる範囲 | Graph展開できる起点 |
|---|---|---|
| `1` | 深さ0、1の本文 | 深さ0だけ |
| `2` | 深さ0、1、2の本文 | 深さ0、1 |

案件内探索GraphをNeo4jへ書き戻さない。Neo4jは共有法令Graph、ExplorationStateはCaseStoreに属する
案件固有の探索履歴である。CaseStoreには全Node・Link・FrontierDecisionを保持するが、
Graph ReviewのPromptに過去の評価済み候補と全Linkを毎回再提示しない。次の2投影を分ける。

- `graph_review_batch`: 新規`unreviewed`、新Hypothesisで再採用した候補、既評価候補へ新たに
  取得したLinkの差分。Article ID、法令名、条番号・見出し、起点、対応WorkItem・Hypothesis、
  当該候補について今回までに判明した全relation属性、basis quote、classificationRunIdとcoverage、
  `review_trigger`、直前のreview statusを含める。
- `graph_review_ledger`: 過去のSolver判断の短い台帳。評価済みFrontier ID、Node ID、Article ID、
  対応WorkItem・Hypothesis、`selected / relevant_deferred / rejected`、短い前回理由、content status、
  最終Review cycle/stepを含める。全Link詳細と過去のLLM生応答は含めない。

同じfrontierを再評価した場合も過去のFrontierDecisionは削除せず、ledgerには最新Decisionだけを投影する。
`selected`はSolverが本文取得対象に選んだ意味判断であり、本文取得が成功した意味ではない。
取得の成否は別の`content_status`で表す。

`graph_review_batch`がProfile上限を超える場合は、Programが安定順で機械的にpage分割し、
全pageの未評価状態とcursorを保持する。Programは関連度で候補を選ばず、未提示候補を
`reject`または不存在と扱わない。同じArticleに複数Linkがある場合は、当該Review batchでは
全Linkの短い情報を併記する。ハッシュ化Evidence IDだけを示してLLMが候補を識別できない状態を禁止する。

### 5.1.3 共有法令Graph（Neo4j）の物理定義

Neo4j、OpenSearch、CaseStoreの責務は3.3のData Store契約に従う。

CaseStoreの探索履歴や案件判断をNeo4jへ保存しない。同じArticleを複数案件で共有しても、ある質問での
関連・不関連、仮説支持・反証、RelationAssertionの案件内評価によって共有Graphを変更しない。

#### Node

全Nodeは`:GraphNode`と次の型別labelを持ち、`graphNodeId`を共通の一意IDとする。

| label | 粒度 | 主なプロパティ |
|---|---|---|
| `Document` | 法令またはガイド文書 | `documentId`, `title`, `docType`, `authorityType`, `sourceSnapshotId`, `sourceRevisionId`, `contentHash`, `graphSchemaVersion` |
| `Article` | 条 | `contentUnitId`, `documentId`, `heading`, `articleNumber`, `sourceSnapshotId`, `sourceRevisionId`, `contentHash` |
| `Paragraph` | 項 | `contentUnitId`, `documentId`, `parentContentUnitId`, `paragraphNumber`, `sourceSnapshotId`, `contentHash` |
| `Item` | 号 | `contentUnitId`, `documentId`, `parentContentUnitId`, `itemNumber`, `sourceSnapshotId`, `contentHash` |
| `RelationAssertion` | 非同期LLMが生成した未確認の意味関係候補 | `assertionId`, `candidateKey`, `assertionDedupeKey`, `proposedPredicate`, `basisEdgeId`, `sourceContentUnitId`, `subjectSupportingSpanId`, `objectSupportingSpanId`, `subjectSupportingQuote`, `objectSupportingQuote`, `referenceOccurrenceHash`, `sourceSnapshotId`, `sourceRevisionId`, `classificationRunId`, `classifiedAt`, `graphSchemaVersion` |
| `ClassificationRun` | snapshot単位の非同期意味分類Run | `classificationRunId`, `phase`, `sourceSnapshotId`, `graphSchemaVersion`, `provider`, `model`, `reviewerModel`, `promptVersion`, `skillVersion`, `reasoningEffort`, `candidatesPerModelCall`, `inputCount`, `processedCount`, `classifiedCandidateCount`, `assertionCount`, `referenceOnlyCount`, `uncertainCount`, `failedCount`, `scopeHash`, `publishedAt` |
| `ClassificationCheckpoint` | 1候補の保存済み実行結果。法的意味関係ではない | `checkpointId`, `classificationRunId`, `candidateKey`, `outcome`, `decisionPayloadJson`, `decisionPayloadHash`, `assertionCount`, `errorCode`, `errorStage`, `errorMessage`, `errorPredicate`, `processedAt`, `sourceSnapshotId`, `graphSchemaVersion` |

`Article`を項・号の代用labelにしない。Graph探索をArticle単位へ投影する場合も、元の
`Paragraph / Item.contentUnitId`と親`Article.contentUnitId`を両方保持する。本文はOpenSearchを正本とし、
Neo4jには識別・検索・監査に必要な見出し、revision、hashだけを置く。現在版だけを扱う初期実装では
`Law / LawRevision / Term`等を追加せず、履歴検索を実装するときに別Phaseで導入する。

#### 物理Relation

Neo4jの物理Relationは、決定的に確認できる構造・原文・来歴だけに限定する。法的意味predicateを
同名の物理Edgeとして重複生成しない。

| relation | from | to | 用途 |
|---|---|---|---|
| `HAS_CONTENT_UNIT` | `Document / Article / Paragraph` | `Article / Paragraph / Item` | 文書構造。containerからchild |
| `REFERENCES` | 参照を書いた`Article / Paragraph / Item` | 参照先`Article / Paragraph / Item` | 原文上の明示参照 |
| `EXPLAINS` | ガイド`Document` | 明示的な解説対象`Article` | 対応表・条文注釈等で明示された対応だけ |
| `SUBJECT` | `RelationAssertion` | `Article` | 意味候補のSUBJECT端点 |
| `OBJECT` | `RelationAssertion` | `Article` | 意味候補のOBJECT端点 |
| `CLASSIFIED_IN` | `RelationAssertion` | `ClassificationRun` | 候補を生成・publishした分類Run |

`IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`は物理Relationにせず、
`RelationAssertion.proposedPredicate`の値とする。`APPLIED_BY`と`MENTIONS`は新Graphへ生成しない。
単なる言及はOpenSearch本文として検索し、ガイドとArticleの明示対応だけを`EXPLAINS`にする。

原文Relationは最低限、`graphEdgeId`, `relationSource`, `sourceContentUnitId`, `sourceRevisionId`,
`sourceSnapshotId`, `graphSchemaVersion`を持つ。`REFERENCES`はさらに`citationText`、取得可能なら
`sourceSpanStart / sourceSpanEnd`、`targetResolutionMethod`を持つ。旧`referenceKind`は移行監査用に
読み取ってもよいが、新schemaの意味selectorには使用しない。特にsource content unit全体のキーワードから
付けた`application / definition / exception`等を、個々の参照先の法的意味として扱わない。

#### RelationAssertion

RelationAssertionは「法令間にこの意味関係があり得る」という共有の未確認候補であり、正式Edgeではない。
1 Nodeは1つのpredicate、1組の端点、1つの根拠参照、1つの分類Runに対応する。
同じ根拠参照が複数箇所へ現れる場合は、分類入力で全出現を提示し、採用した両端span IDと原文を
Assertionへ保存する。span IDは本文hashと組み合わせて解釈し、本文改訂後の別snapshotへ流用しない。

各RelationAssertionは`SUBJECT`と`OBJECT`を1本ずつ持ち、`proposedPredicate`は
SUBJECTからOBJECTへ向く意味候補として解釈する。原文`REFERENCES`の向きとは独立であり、
`basisEdgeId`で分類根拠となった原文Relationへ接続する。

`SUBJECT / OBJECT`は端点の役割であり、契約当事者や法律上の主体・客体を意味しない。
RelationAssertionに汎用`status=unverified`を重複保存せず、Nodeとして存在すること自体を未確認候補とする。
同じ端点間でもpredicateまたは根拠箇所が異なれば別Assertionにできる。推移関係をProgramが推論して
新Assertionを書かず、LLMが根拠本文から明示的に分類した直接関係だけを保存する。

predicateと向きを次に固定する。

| `proposedPredicate` | SUBJECT | OBJECT | 例 |
|---|---|---|---|
| `IMPLEMENTS` | 抽象的な親規定 | 具体化する下位規定 | 金商法27条の3 → 公開買付府令10条 |
| `INCORPORATES` | 他規定を準用・読み替える規定 | 取り込まれる規定 | BがAを準用する場合のB → A |
| `USES_DEFINITION` | 定義を利用する規定 | 定義を置く規定 | 利用条文 → 定義条文 |
| `EXCEPTION_TO` | 例外・適用除外を定める規定 | 一般規定 | 施行令7条 → 金商法27条の2 |
| `OVERRIDES` | 優先して適用される規定 | 排除・修正される規定 | 特則 → 一般則 |

非同期分類結果は共有候補にすぎない。検索時Solverが質問に関係する候補だけ両端Article全文で評価し、
その案件判断をHypothesis・EvidenceとともにCaseStoreへ保存する。Neo4jのRelationAssertionを更新・削除したり、
同名の正式Relationへ自動昇格させたりしない。

現行の`fromArticleId / toArticleId / suggestedType / status`だけを持つNodeは新schemaの正本にしない。
再seedと非同期再分類で`SUBJECT / OBJECT / CLASSIFIED_IN`接続と`proposedPredicate`へ生成し直すため、
旧Graph内でのin-place migrationは行わない。

#### 非同期意味分類とpublish

`/admin/seed`はOpenSearch本文、構造Node、`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`までを作り、
LLM分類の完了を待たず終了する。その後の非同期jobは、決定的抽出で得た参照を端点ペアとbasisへ
正規化し、`RelationClassificationCandidate` 1件ごとに次を同じ入力scopeとして扱う。

- 原文上の参照元・参照先Article全文。各本文は候補内に閉じ、`<articleId>::span-N`で一意に識別する
- 同じ候補へ結び付く全`REFERENCES`と、同じ引用がArticle内に複数回現れる場合を含む全参照箇所
- 各参照箇所の`citationText`、参照を書いたcontent unit、basis edge、対応span ID群、content unit内の
  `sourceStart / sourceEnd`、引用直前・直後の`sourcePrefix / sourceSuffix`。同じ引用文言が同一span内で
  反復しても、LLMは位置と局所文脈から出現を区別する
- law family、authority type、snapshot・content hash

複数targetを同じ判断へ束ねず、候補生成元のheuristic、旧`suggestedType / referenceKind`を正解候補として
Promptへ出さない。Luna Workerは1候補の5 predicateを同じ呼出しで比較する。各predicateについて固有の
二必要条件と`finding`を独立に返し、1件以上が`established`なら同じ回答内で
`referenceOccurrenceHash / subjectArticleId / objectArticleId /
referenceSourceSupportingSpanId / referenceTargetSupportingSpanId`も返す。例えば`INCORPORATES`は
`explicitApplicationLanguage / targetRuleApplied`、`USES_DEFINITION`は
`targetDefinesTerm / sourceUsesSameTerm`を使う。複数predicateは同時に成立でき、一方の成立を
他方の根拠へ流用しない。非成立・不確実な関係へ意味方向や根拠を作らせない。

Luna ReviewerはWorkerの全回答と同じ候補入力を受け取り、答えを知らない独立再分類ではなく、
Workerの誤り・不足・根拠不整合を具体的に指摘する。`request_change`の場合は同じWorkerが指摘を参照して
5 predicate全体を再確認し、差戻しは1回だけに制限する。同じReviewerが修正版を差分確認し、
2回目も不合格なら自動再試行せず`unresolved`へ分離する。候補集合は複数のWorker / Reviewerペアへ
分割して並列化できるが、1候補を異なるペアへ重複配布しない。

Programは二条件とfindingの真理値整合、既知ID、成立predicateと根拠件数の対応だけを検証し、
本文から条件値、predicate、意味方向を決めない。内部`candidateKey`はProgramが入力候補へ
機械的に対応付け、LLMに未知IDを生成させない。
`referenceOccurrences`が1件だけなら、その`occurrenceHash`も入力envelopeが所有し、Worker出力後に
Programがassertionへ機械的に束縛する。複数箇所ならどの参照箇所を根拠にするかは意味判断なので、
Workerが既知hashから選び、Programは存在だけを検証する。Programはこの束縛でpredicate、方向、
根拠spanを変更しない。

候補の`referenceSourceArticle / referenceTargetArticle`は原文`REFERENCES`の物理方向だけを表す。
新seedは同一法令参照と親法令参照の両経路で引用位置を保存する。位置を持たない旧Graphを分類する場合は、
既知`citationText`の完全一致位置を決定的に全件復元し、意味を推測せず候補へ付与する。親本文が子chunkへ
再掲された部分の引用は子content unit自身の参照として扱わない。
意味上の`subjectArticleId / objectArticleId`は根拠付与LLMが入力中の2端点から選ぶ。
Programはauthority階層や参照方向から意味方向を補完しない。1件以上`established`なら`CLASSIFIED`、
成立なしで1件以上`uncertain`なら`UNCERTAIN`、
全件`not_established`なら`REFERENCE_ONLY`と機械的に投影し、`established`のassessmentだけを
RelationAssertionへ写す。
これは意味判断ではなく、重複する状態を持たないための決定的投影である。Programは既知decision key、
predicate enum、端点が入力内にあること、選択span IDが対応Articleと参照箇所へ属すること、snapshot・hash・
件数だけを検証し、predicate・finding・方向を補正しない。
`REFERENCE_ONLY / UNCERTAIN / FAILED`はRelationAssertionに変換せず、ClassificationRunの監査件数へ記録する。

分類LLMは各候補について5 predicateを一度に比較し、成立時の根拠選択も同じ候補の回答に含める。
Codexオペレーター実行では1つのWorker sessionへ最大5候補のshardを渡すが、各候補を独立に読み、
候補ごとに1つの判定recordを返す。他候補の本文・判断を根拠へ流用しない。Reviewerも同じ5候補以下の
shardとWorkerの候補別回答を別sessionで検査する。候補間の並列化はshardを独立したWorker / Reviewerペアへ
割り当てることで行う。sessionの処理単位とは別に候補単位の保存checkpointを持ち、
分類jobは`sourceSnapshotId + 参照元/参照先Article ID・content hash + basisEdgeId + 正規化した全reference occurrence hash + promptVersion + provider + model + reviewer model + graphSchemaVersion`で
再開・cache可能にする。
この組を正規化してhash化した`candidateKey`を分類入力の冪等キーとする。1候補から複数predicateが返り得るため、
RelationAssertionの物理重複キーは候補だけにせず、
`assertionDedupeKey = hash(classificationRunId + candidateKey + proposedPredicate)`とする。
同じRun・候補・predicateを異なる`assertionId`で二重登録せず、別predicateと別Runは区別する。
結果は候補単位の`ClassificationCheckpoint`とともに`phase=building`のRunへ書き、入力scopeを処理し終えたRunだけ`phase=published`へ一括publishする。
分類CLIは`building`を既定とし、新5 predicate fixtureと対象scopeの品質確認後に明示した`--publish`だけが
publish遷移を要求できる。構造監査の成功だけで自動publishしない。
Assertionを作らない`REFERENCE_ONLY / UNCERTAIN / FAILED`もcheckpointを持つ。中断再開時に
`REFERENCE_ONLY / UNCERTAIN / CLASSIFIED`は再呼出しせず、`FAILED`だけは同じcandidateを再試行して
checkpointとRun集計をtransactionで置換する。checkpointは実行記録であり、Graph探索の法的関係として使わない。
checkpointの`decisionPayloadJson`には5 predicateの固有条件とfinding、成立時の根拠付与結果を保存し、
`decisionPayloadHash`との一致を監査する。これにより`REFERENCE_ONLY`を含む誤分類を事後に追跡できる。
継続不能なRunは`phase=failed`とする。このphaseはProgram内部の実行事実でありSolverへ判断させない。
Case開始時に最新のpublish済み
`classificationRunId`を固定し、そのCaseの全Graph検索へ渡す。`uncertainCount`または`failedCount`が0でない場合は
Graph ToolResultにcoverageを示し、Assertionがないことを「関係なし」と断定しない。

#### 検索時のArticle投影

Graph Tool Adapterは、正確な端点Content Unit ID、親Article ID、mode、predicateまたは原文Relation、
`basisEdgeId`、`supportingSpans`、`classificationRunId`、`from_subject / to_subject`を同時に保持する。

frontierと本文取得はArticle単位にまとめても、どの項・号に記載された関係から発見したかを
DiscoveryLinkのrelation metadataから失わない。Graph候補だけではEvidenceとせず、Article本文はOpenSearchの
`fetch_articles`で全chunkを取得してSolverが評価する。

#### Graph検索selector

Legal ToolはLLM生成Cypherを受け付けず、次の固定modeをparameterized Cypherへ対応させる。

| mode | 必須scope | 用途 |
|---|---|---|
| `semantic_assertion` | 起点Article、`proposedPredicate` 1件、direction 1件、`classificationRunId` | 仮説に沿った意味候補検索。通常経路 |
| `explicit_reference` | 起点Article、direction 1件 | 原文上の明示参照をたどる。通常は`from_subject`だけ |
| `explains` | 起点DocumentまたはArticle、direction 1件 | ガイドの明示対応をたどる |

`semantic_assertion`は必要な場合だけsame law family、target authority type、document ID等の構造filterを追加できる。
意味predicate、direction、構造filterはSolverがHypothesisから選び、Programは補完しない。同じIntentで複数predicate、
両方向、全modeを一括指定せず、必要なら別selectorに分ける。`explicit_reference/to_subject`は高fan-inになるため
通常QAの既定経路にせず、十分限定された監査目的だけ許可する。

Tool Adapterは結果をmaterializeする前に候補件数を確認する。安全上限を超える場合は任意の上位N件へ切り捨てず、
`scope_too_broad`と構造facet別件数を返す。scopeを変更するかOpenSearchへ戻るかはSolverが判断する。
検索は1ホップだけとし、同じscope keyを重複実行しない。

#### Constraint・監査・再構築

少なくとも次を一意制約・indexとして作成する。

```text
UNIQUE GraphNode.graphNodeId
UNIQUE RelationAssertion.assertionId
UNIQUE RelationAssertion.assertionDedupeKey
UNIQUE ClassificationRun.classificationRunId
UNIQUE ClassificationCheckpoint.checkpointId
INDEX  GraphNode.documentId
INDEX  Document.authorityType
INDEX  RelationAssertion.proposedPredicate
INDEX  RelationAssertion.classificationRunId
INDEX  ClassificationRun.sourceSnapshotId
INDEX  ClassificationCheckpoint.classificationRunId
```

seed監査では、Node/Relationの端点型、dangling relation、重複`graphEdgeId`、`MENTIONS / APPLIED_BY`が0件、
許可した物理Relation以外が0件であることを検査する。分類publish監査では、RelationAssertionごとの
`SUBJECT / OBJECT / CLASSIFIED_IN`各1件、predicate enum、非nullな`basisEdgeId`、両端のsupporting span ID・原文、
同一snapshotの端点・ClassificationRunとの参照整合、重複`assertionId`、重複`assertionDedupeKey`、
`candidateKey`の再計算一致、Run集計件数を検査する。
さらに、同じ入力snapshotから作ったOpenSearchとNeo4jについてDocument・Article ID、`sourceRevisionId`、
`sourceSnapshotId`、`contentHash`の対応を検査する。入力元がrevision IDを提供しない場合は推測値を作らず
`sourceRevisionId=null`とし、seed runで固定した`sourceSnapshotId`とcontent hashで同時生成を確認する。

Graph schema、抽出規則、法令・ガイド入力のいずれかを変更した場合は`graphSchemaVersion`を更新し、
現行`/admin/seed`でOpenSearchとNeo4jの構造・原文Relationを両方再構築した後、新snapshot用の
ClassificationRunを非同期実行する。旧Runは監査用に参照可能でも新snapshotへ流用しない。
Neo4jだけの再seed経路は設けない。

### 5.2 statusを少数に保ち、意味と決定主体を固定する

statusは「実行事実」と「意味判断」を分離する。同じ文字列を別の軸へ流用せず、LLMへ見せる値は
7.3の共通Prompt語彙を必ず合成する。JSON Schemaの`enum`は形式制約であり、意味定義の代わりにしない。

| 対象 | 値 | 決定者 |
|---|---|---|
| Run | `running / completed / failed / cancelled` | プログラム |
| ToolResult | `succeeded / failed / timeout` | プログラム |
| ClassificationRun phase | `building / published / failed` | プログラム |
| ClassificationCheckpoint outcome | `classified / reference_only / uncertain / failed` | LLM（`failed`のみプログラムの実行失敗） |
| Cycle phase | `planned / running / completed` | プログラム |
| Step phase | `planned / observed / completed` | プログラム |
| Resource本文 | `not_requested / pending / succeeded / failed / timeout` | プログラム |
| Graph expansion | `not_started / pending / partial / complete / failed / timeout` | プログラム |
| Frontier review | `unreviewed` | 新規`Node × Hypothesis`からプログラムが初期化 |
| Frontier review | `selected / relevant_deferred / rejected` | Solver |
| Cycle budget flag | `cycle_budget_reached / cycle_close_required / cycle_step_timeout` | プログラム |
| Solverの次動作 | `start_cycle / continue_cycle / start_next_cycle / finalize` | Solver |
| WorkItem | `open / resolved / dropped` | Solver |
| Hypothesis | `supported / contradicted / unresolved` | Solver |
| Frontier action | `select / defer / reject` | Solver |
| Deferred Frontier resolution | `carry_forward / no_longer_needed / unresolved_at_limit` | Solver |
| 未評価Graph候補の解消 | `carry_forward / no_longer_needed / unresolved_at_limit` | Solver |
| Review | `accept / revise` | Reviewer |

#### status契約の保守性

保守性向上の目的は、status追加・名称変更・意味変更のたびに、状態型、Provider schema、Prompt、Projector、
validator、Loopを人手で同期する構造をなくすことである。すべてのstatusを1個の巨大Enumや1個の巨大状態機械へ
まとめるのではなく、Run、Tool、Cycle、Step、WorkItem、Hypothesis、Frontier、Graph expansionごとに
所有者を固定した小さい契約として定義する。

対象ごとの正本は`code / description / owner / persisted / llm_visible / allowed_transitions`を持ち、
Pydantic型、Provider schema、共通Prompt用語集、遷移検証、契約テストの生成元とする。

実装では通常の型付きEnumを使い、アプリケーション内部のstatusを生文字列として持ち回らない。
LLM応答、JSON、永続化backendの文字列は境界のPydanticモデルでEnumへ復元し、JSON・DBへ出すAdapterだけが
値へ直列化する。文字列としても比較できる`str, Enum`へ暗黙依存せず、Pydanticモデルで
`use_enum_values=True`を指定して内部表現を文字列へ戻さない。

```python
class FrontierReviewStatus(Enum):
    UNREVIEWED = "unreviewed"
    SELECTED = "selected"
    RELEVANT_DEFERRED = "relevant_deferred"
    REJECTED = "rejected"

class Frontier(BaseModel):
    review_status: FrontierReviewStatus

# 内部の参照
if frontier.review_status is FrontierReviewStatus.RELEVANT_DEFERRED:
    ...

# JSON・DB境界だけで直列化
record = frontier.model_dump(mode="json")
```

statusの型付き読み取りは許可する。別の処理が`FrontierReviewStatus.RELEVANT_DEFERRED`を直接読むことを
一律に隠すため、同じ意味の`has_unresolved_frontier`等を無条件に追加しない。複数箇所で本当に同じ複合条件を
使う場合だけ、名前付きpolicy関数へ一度定義する。禁止するのは、生文字列比較、状態更新の直接代入、
同じ変換・複合条件を複数箇所へコピーすることである。

statusを持つrecordは読み取り専用として扱い、変更は対象ごとのCommandと共通適用関数を通す。
単純な`current status × command → next status`は対象ごとの小さい遷移表を正本にし、既知ID、親子関係、
Cycle上限等の複数recordにまたがる構造条件はCommand適用処理へ一度だけ記述する。Pydantic validatorは
入力形状、適用処理は状態変更規則、Projectorはread model生成を担当し、同じ条件を重複実装しない。

`apply_case_command`は、対象IDと現在status、対象別遷移表、複数record間の構造条件を検証し、
新しいrecordを生成してCaseStoreへ適用する唯一の更新入口とする。

`next`と`start_next_cycle`のように、1つの操作を複数フィールドの組合せで表さない。
`StartCycle / ContinueCycle / StartNextCycle / Finalize`のdiscriminator付きCommand unionを使い、Commandごとの必須欄を
型で分ける。プログラムはCommandの形式、既知ID、上限、許可された状態変更だけを検証し、どのCommandや
意味statusを選ぶべきかは判断しない。

Projectorが元のToolRequest、ToolResult、Evidence等から再計算できる値は、独立して変更可能な第二の正本にしない。
検索効率のためCaseStoreへ最新値をmaterializeする場合も、共通適用関数だけが元の事実と同時に更新し、
破棄して再生成可能な値として扱う。AgentViewは常にCaseStoreから再生成し、AgentViewのstatusをCaseStoreへ
書き戻さない。

status契約の生成と検証は次を満たす。

- Pydanticの`model_json_schema()`をProvider schemaの基礎にし、別の手書きenum一覧を正本にしない。
- 共通Solver Promptのstatus用語集は、`llm_visible=true`の説明付き契約から決定的に生成する。
- Domain Promptはstatusの業務上の使い方を記述してよいが、値と基本定義を別表現で再定義しない。
- 新しいstatusまたはCommandを追加すると、全状態との許可・拒否が未定義なら網羅性テストを失敗させる。
- serialized valueの変更は`contract_version`を上げ、既存Caseのmigrationまたは旧値読替えを明示する。
- Case開始時の`contract_version`とProfile versionを固定し、実行途中で新契約へ切り替えない。
- statusの意味を変更して他の処理の判断条件も実際に変わる場合は、その依存処理を明示的に修正する。
  不要な中間booleanで影響を隠さず、契約テストで見直し漏れを検出する。

機械的statusの意味:

| 対象・値 | 意味 |
|---|---|
| ToolResult `succeeded` | Tool呼出しが正常終了した。`fetch_articles`では要求した全Articleの全登録済みチャンク取得完了も意味するが、内容の関連性、正しさ、仮説支持を意味しない |
| ToolResult `failed` | Toolがエラー終了した。対象の不存在を意味しない |
| ToolResult `timeout` | 制限時間内に完了しなかった。対象の不存在を意味しない |
| Cycle `planned` | goal、探索方針、完了・失敗条件を保存済みで、最初のstep開始前 |
| Cycle `running` | 同じ仮説・探索方針でaction-observation stepを反復中 |
| Cycle `completed` | Cycle全体の評価差分と終了理由を適用済み。次Cycle開始または最終化が可能 |
| Step `planned` | ToolRequestを保存済みで、全結果の観察前 |
| Step `observed` | 当該stepの全ToolResultを保存済みで、Solverの意味評価前 |
| Step `completed` | Solverの評価差分とfrontier更新を適用済み |
| content `not_requested` | 本文取得をまだ要求していない |
| content `pending` | 本文ToolRequestを保存済みで、終端ToolResultが未保存 |
| content `succeeded` | 当該ArticleについてOpenSearchに登録済みの全本文チャンクを取得した。質問との関連性、根拠採用、元データのインデックス完全性を意味しない |
| content `failed / timeout` | 全本文チャンク取得を完了できず、失敗または時間切れになった。途中pageを部分成功として扱わず、Article不存在も意味しない |
| expansion `not_started` | 当該scopeのGraphをまだ要求していない |
| expansion `pending` | Graph ToolRequestを保存済みで、終端ToolResultが未保存 |
| expansion `partial` | 一部候補だけ取得し、`next_cursor`または未取得範囲が残る |
| expansion `complete` | 当該scopeの取得を完了した。隣接本文の確認完了を意味しない |
| expansion `failed / timeout` | 当該scopeのGraph取得が失敗または時間切れ。関係不存在を意味しない |
| `cycle_budget_reached` | Cycleの本文取得数または他の機械的上限に達し、新しいactionを追加できない |
| `cycle_close_required` | 予約時間を保護するため新しいactionを始めず、現Cycleの終了評価が必要 |
| `cycle_step_timeout` | Cycle予算で短縮された中間呼出しが時間切れ。仮説の否定やprovider障害は意味しない |

#### Article本文取得の完全性

`fetch_articles`の入力単位はArticle、保存するEvidenceの単位はOpenSearch上のArticle・Paragraph・Item
チャンクである。Tool Adapterは各Article IDについて`contentUnitId`の安定順で総件数を確認し、内部page sizeで
全pageを取得する。page sizeは1回のOpenSearch応答件数であり、Article本文の取得上限ではない。

Articleあたりの`max_chunks`を公開Tool契約・Profile上限として設けない。既存の
`LLM_RESEARCH_MAX_CHUNKS_PER_ARTICLE`は新Frameworkの`fetch_articles`取得上限には使用しない。
1回に選択できるArticle数、Cycle内で取得できる重複なしArticle数、Tool・Cycle時間、model context容量は
別の上限として維持する。

Tool Adapterはpage取得中のチャンクを一時バッファへ置き、要求した全Articleの取得が完了してから
ToolResultとEvidenceをCaseStoreへ同じStepの観察結果として適用する。後続pageの失敗・timeout、総件数との
不一致、対象Articleの0件取得があれば`succeeded`にせず、途中チャンクをgrounding Evidenceとして部分commitしない。
本文取得には`partial` statusを追加せず、`partial`はGraph expansionの未取得pageがある状態だけに使用する。

`content=succeeded`は「OpenSearchに現在登録されている全チャンクを取得した」という実行事実である。
元のe-Gov等からOpenSearchへの投入漏れがないことはseed・index監査の責務であり、本文の質問への関連性、
法的意味、Hypothesisの支持はSolverが判断する。

Solverが決める意味status・action:

| 対象・値 | 意味 |
|---|---|
| `start_cycle` | Active Cycleがない状態で、今回のgoal・strategy・完了条件を決めてCycleを開始する |
| `continue_cycle` | 同じCycleの仮説・探索方針を維持し、次のaction-observation stepへ進む |
| `start_next_cycle` | 現Cycleを理由付きで評価して閉じ、構造化した引継ぎを残す。次Cycleの計画は次の`start_cycle`で決める |
| `finalize` | 必要な根拠を探し切ったと判断し、新しいToolを実行せず回答を確定する |
| WorkItem `open` | 問いが未完了で、追加作業が必要 |
| WorkItem `resolved` | 問いへ結論が出ており、`resolution`に結論を持つ |
| WorkItem `dropped` | 前提否定、重複、質問との無関係により対象外とし、`resolution`に理由を持つ |
| Hypothesis `supported` | 今回提示されたgrounding Evidenceがstatementを支持する |
| Hypothesis `contradicted` | 今回提示されたgrounding Evidenceがstatementを否定する |
| Hypothesis `unresolved` | 根拠不足、両義的、未取得で真偽を確定していない |
| Frontier `select` | この候補を次の仮説検証行動へ採用する |
| Frontier `defer` | 現在の質問・Hypothesisに関連するが、今回の本文取得枠に入れず後続Cycle候補として保留する |
| Frontier `reject` | 現在の質問・Hypothesisには関係しないと判断し、理由付きで候補から外す |
| `carry_forward` | 取得上限等によりactive候補のまま次Cycle以降へ保持する |
| `no_longer_needed` | 後続Evidenceを踏まえ、質問への回答には不要と判断する |
| `unresolved_at_limit` | 新しいCycleを開始できない上限時に未確認として残し、limitationsへ示す |

`supported`はWorkItem全体の完了を意味せず、`content=succeeded`や`expansion=complete`から
プログラムが自動生成してはならない。Decisionに現れないfrontierは`reject`と解釈しない。

次は導入しない。

- ClaimとHypothesisに別々の類似statusを持たせること
- `partial`を複数の意味で使うこと
- 実行timeoutを`insufficient`へ変換すること
- Reviewerの`needs_research`
- プログラムがLLMの`finalize`を意味上の理由で`continue_cycle`へ変更すること
- `content=succeeded`を`Hypothesis=supported`へ読み替えること
- `expansion=complete`を隣接Article本文の確認済みへ読み替えること

部分的にしか確認できていない場合は、対象を複数のHypothesisへ分け、
確認できたものを`supported`、未確認のものを`unresolved`として明示する。

### 5.3 Solver契約

SolverはCaseState全体を再出力しない。追加と更新の差分だけを返し、出力に現れなかった
WorkItem、Hypothesis、EvidenceをCaseStateから削除しない。

```python
class CaseUpdate:
    add_work_items: list[WorkItem]
    update_work_items: list[WorkItemUpdate]
    add_hypotheses: list[Hypothesis]
    update_hypotheses: list[HypothesisUpdate]
    impact_decisions: list[WorkItemImpactDecision]

class WorkItemImpactDecision:
    work_item_id: str
    action: Literal["retain", "replace", "drop"]
    reason: str
    new_basis_hypothesis_ids: list[str]
    replacement_work_item_id: str | None
    drop_subtree: bool = False

class CyclePlan:
    goal: str
    strategy: str
    completion_criteria: list[str]
    focus_work_item_ids: list[str]
    focus_hypothesis_ids: list[str]

class FrontierReAdoption:
    node_id: str
    work_item_id: str
    hypothesis_id: str
    reason: str

class DeferredFrontierResolution:
    frontier_item_id: str
    article_id: str
    work_item_id: str
    hypothesis_id: str | None
    action: Literal["carry_forward", "no_longer_needed", "unresolved_at_limit"]
    reason: str

class UnreviewedGraphResolution:
    action: Literal["carry_forward", "no_longer_needed", "unresolved_at_limit"]
    reason: str

class CycleHandoff:
    result_summary: str
    unresolved_hypothesis_ids: list[str]
    carried_frontier_ids: list[str]
    failed_request_ids: list[str]

class SolverCommandBase:
    update: CaseUpdate
    next_focus_work_item_ids: list[str]
    retain_evidence_ids: list[str]
    frontier_decisions: list[FrontierDecision]
    frontier_re_adoptions: list[FrontierReAdoption]
    deferred_frontier_resolutions: list[DeferredFrontierResolution]
    unreviewed_graph_resolution: UnreviewedGraphResolution | None

class StartCycleCommand(SolverCommandBase):
    kind: Literal["start_cycle"]
    cycle_plan: CyclePlan
    tool_requests: list[ToolRequest]

class ContinueCycleCommand(SolverCommandBase):
    kind: Literal["continue_cycle"]
    tool_requests: list[ToolRequest]

class StartNextCycleCommand(SolverCommandBase):
    kind: Literal["start_next_cycle"]
    cycle_completion_reason: str
    handoff: CycleHandoff

class FinalizeCommand(SolverCommandBase):
    kind: Literal["finalize"]
    cycle_completion_reason: str | None
    answer: FinalAnswer

SolverCommand = Annotated[
    StartCycleCommand | ContinueCycleCommand | StartNextCycleCommand | FinalizeCommand,
    Field(discriminator="kind"),
]
```

discriminatorは、`cycle_plan`、ToolRequest、handoff、answer等のCommand固有payloadを型で分離する。
一方、`CaseUpdate`、focus、Evidence保持、frontier更新は複数Commandで共通利用するため基底型に置く。
したがって、型だけで全相互制約を表現できるとはみなさない。共通欄の既知ID、現在状態、全件性、
Commandとの許可組合せは`apply_case_command`の実行時検証とCommand別の契約テストで保証する。
意味上どのCommandを選ぶべきかは検証しない。

主な整合条件は次のとおり。

- `CycleRecord`は同一Caseで同時に1件だけ`planned`または`running`になり、それより後のCycleを開始しない。
- Active Cycleがない状態では`start_cycle`または`finalize`を許可し、Active Cycleがある状態では`start_cycle`を拒否する。
  Active Cycleがない状態の`continue_cycle / start_next_cycle`も拒否する。
  `continue_cycle`は現在のgoal・strategyを別の意味へ上書きしない。
- `StepRecord planned → observed`は全ToolRequestに終端ToolResultが保存された場合だけ許可する。
- `StepRecord observed → completed`はSolverのCaseUpdateとFrontierDecisionを適用した場合だけ許可する。
- Active Cycleがある状態の`start_next_cycle`と`finalize`では`cycle_completion_reason`を必須とし、Cycle全体の
  評価差分適用と`research_cycle_count`更新を同時に行う。
- FrontierDecisionは、そのCycle開始時または各stepの観察結果で追加された既知frontierと、対応する
  WorkItem・Hypothesisだけを参照する。未言及frontierは状態を変えない。
- FrontierDecisionの`select / defer / reject`は、frontierのreview statusをそれぞれ
  `selected / relevant_deferred / rejected`へ更新する。`select`後の本文取得成否は`content_status`だけで更新し、
  review statusへ成功・失敗を混在させない。`selected + succeeded/pending`の重複取得は拒否する。
- `graph_review_batch`内の新規・再採用・新Link差分は全件にFrontierDecisionを必須とする。
  ledgerだけにある`relevant_deferred`、または本文取得の再試行が必要な`selected + failed/timeout`は
  `select`できるが、再度`defer/reject`して過去判断を上書きする対象にはしない。
- FrontierReAdoptionは`graph_review_ledger`に示された既知Nodeと、既知のopen WorkItem・Hypothesisを
  Solverが理由付きで結び直す。Programはその参照整合だけを検証し、新しい`unreviewed` FrontierItemを作る。
  `rejected`の候補を別Hypothesisへ自動転用しない。
- `content_status`とExpansionSliceのstatusは対応するToolRequest・ToolResultからだけ更新し、
  SolverDecisionから変更させない。
- `start_cycle`と`continue_cycle`では1件以上のToolRequestまたは、Profileが決定的なToolRequestへ変換できる
  1件以上の`select` FrontierDecisionを持つ。Graph Reviewで選択がなく、未提示pageもない場合は
  通常integrationのCycle終了・追加検索・完了判断へ戻す。
- `start_next_cycle`は現在Cycleを閉じる評価と`CycleHandoff`を持ち、`cycle_plan`、ToolRequest、回答を持たない。
  ProgramがCycleを閉じた後、次のSolver呼出しが`start_cycle`で新しい計画と最初のToolRequestを決める。
- Cycle境界で未評価Graph候補だけを引き継ぐ場合は、ToolRequestなしの`start_next_cycle`を許す。
  次Cycleの取得枠を確立してから差分Graph Reviewを行い、枠0の状態で全候補へdeferを強制しない。
  `remaining_unreviewed_count > 0`のCycle境界では`UnreviewedGraphResolution`を必須とする。
  `carry_forward`は`start_next_cycle`、`no_longer_needed`は通常finalize、
  `unresolved_at_limit`は次Cycle不能の限定finalizeだけに対応させる。候補の必要性はSolverが判断し、
  Programは候補数が残る事実、actionと次動作の組合せだけを検証する。
- `start_next_cycle`または`finalize`では、本文未取得のactiveな`relevant_deferred`全件に
  `DeferredFrontierResolution`をちょうど1件ずつ持つ。Programは既知frontier・Article・WorkItem・Hypothesis
  の完全一致、全件性、actionと次動作の参照整合だけを検証し、法的な必要性や理由の妥当性は検証しない。
- `carry_forward`は`start_next_cycle`を必要とし、active候補として次Cycleへ残す。次Cycleの`start_cycle`で
  Solverが他の引継ぎ情報と一緒に評価し、本文取得対象を選ぶ。Programは本文取得へ自動転記しない。
  `unresolved_at_limit`は新しいCycleを開始できない最終化時だけ許可し、limitationsを必須とする。
- `finalize`ではToolRequestを持たず、回答を持つ。通常finalizeでは全WorkItemをclosedにし、
  `limitations / unresolved_work_item_ids / unresolved_hypothesis_ids`を空にする。上限等により次Cycleを
  開始できない限定finalizeでは、未完了WorkItemをopen、Hypothesisをunresolvedのまま保ち、
  limitationsと両ID欄を相互参照させる。一般的な注意書きはlimitationsではなく回答本文へ記載する。
- 取得済み情報だけで最初から`finalize`する場合はCycleを作らず、`research_cycle_count`も増やさない。
- 新規IDはCase内で一意、更新IDは既知でなければならない。
- WorkItemの親IDは同じCaseに存在し、親子関係を循環させない。
- HypothesisのWorkItem ID、WorkItemのbasis Hypothesis IDは同じCaseに存在する。
- ToolRequestは既知WorkItemと、当該CaseのHypothesisを参照する。
- `supported`または`contradicted`のHypothesisは既知Evidence IDを1件以上参照する。
- `next_focus_work_item_ids`は既知の`open` WorkItemだけを参照する。
- `retain_evidence_ids`は既知Evidenceだけを参照し、Profileの件数上限を超えない。
- LLMが参照できるのは、当該呼び出しへ提示されたIDだけとする。
- 検証違反は`protocol_error`であり、プログラムが意味statusを書き換えて補正しない。

子WorkItemがすべて終了しても、プログラムは親を自動的に`resolved`へしない。Solverが親を
`resolved`へする場合、残る子を同じCaseUpdateで`resolved`または`dropped`へし、resolutionを返す。
プログラムは親が終了しているのに`open`な子が残る構造を契約違反として拒否するだけで、
子の結論や破棄を決めない。

### 5.3.1 仮説が反証された場合

Hypothesisが`contradicted`になったとき、プログラムは`basis_hypothesis_ids`の完全一致から、
影響を受けるWorkItemとその子孫IDを列挙してSolverへ返す。プログラムはそれらを自動的に
終了・変更しない。

Solverは「そのWorkItemの`question`を変えずに、引き続き親の問いを解くために使えるか」で判断する。
仮説、検索語、検索先、根拠候補が誤っていただけならWorkItemを置き換えず、新しいHypothesisや
ToolRequestを追加する。観点が不足していた場合も、既存WorkItemを置き換えず子または兄弟WorkItemを追加する。
`replace`は、WorkItemの`question`自体を別の意味へ変えなければ親の問いに寄与できない場合だけに使う。
質問との無関係または重複が根拠から判明した場合だけ`drop`する。

問いが有効でHypothesisだけが外れた場合は`retain`して新Hypothesisを追加し、探索方法だけが外れた場合は
ToolRequestを変更する。不足観点は子または兄弟WorkItemとして追加する。問い自体の意味を変える場合だけ
`replace`し、無関係または重複の場合だけ`drop`する。

親WorkItemを`replace`する場合、初期実装では旧部分木の子を新しい親へ付け替えない。Solverが旧部分木の
各open WorkItemを明示的に`drop`するか、`drop_subtree=true`を返し、新しい親子WorkItemを別IDで作る。
これにより旧分解を履歴として保持しながら、親が閉じているのにopenな子が残る状態を避ける。

局所的なHypothesis追加、検索語変更、WorkItem追加で現在のCycle goal・strategyを維持できるなら
`continue_cycle`を選ぶ。初期分解の主要部分が質問を覆っていない、中心Hypothesisの反証で現在の
作業構造が成立しない、検索起点・法令階層の前提を変える必要がある、またはCycle取得枠が尽きても
必要と判断した未取得Evidenceが残る場合に`start_next_cycle`を選ぶ。Tool終了やGraphの1ホップ完了
だけを理由にしない。

Solverは影響を受けるWorkItemごとに、次を明示する。

| action | 意味 |
|---|---|
| `retain` | 作業は依然必要。反証された前提を外すか、別Hypothesisへ付け替える |
| `replace` | 旧WorkItemを`dropped`にし、別IDの新WorkItemへ置き換える |
| `drop` | 作業が不要。必要ならSolverの`drop_subtree=true`指示で子孫も終了する |

`replace`ではWorkItemのquestionを上書きしない。新WorkItemを`add_work_items`へ含め、
旧IDを`replaces_work_item_id`で参照する。`drop_subtree`もSolverの明示指示であり、
プログラムは指定された部分木を機械的に更新するだけである。

同じCaseUpdateで新たに`contradicted`となるHypothesisを前提にした`open` WorkItemがある場合、
Solverは各WorkItemの`impact_decisions`を必ず返す。プログラムは全IDが処理対象になっているか、
`retain`後のbasisから反証Hypothesisが外れているか、`replace`先が新規WorkItemとして存在するか、
`drop`対象が終了状態になるかだけを検証する。action自体は選ばない。

反証されたHypothesis、反証Evidence、droppedになったWorkItemはCaseStateから削除しない。
同じ誤った見立てを後のサイクルで繰り返さないため、全体案内に簡潔に残す。

### 5.4 Tool契約

```python
class ToolRequest:
    request_id: str
    work_item_id: str
    tool_name: str
    arguments: dict
    purpose: str
    hypothesis_ids: list[str]
    exploration_intent_id: str | None

class ToolResult:
    request_id: str
    status: Literal["succeeded", "failed", "timeout"]
    evidence_ids: list[str]
    error_code: str | None
    elapsed_ms: int
```

ToolRequestは実行前にCaseStateへ保存し、ToolResultの`request_id`は既知のToolRequestと完全一致させる。
候補発見・Graph展開・本文取得を行うToolRequestは`exploration_intent_id`を必須とし、同じWorkItem・Hypothesisに
属する既知Intentと完全一致させる。CaseStore内の既知Evidenceを再読込するだけの`load_evidence`等はnullを許可する。
これによりToolResultから検証対象WorkItem、Hypothesis、検索scopeを必ず逆引きできる。
ToolResultは実行事実だけを表す。検索候補が法的に重要か、条文間関係が成立するかはSolverが判断する。

### 5.5 別サイクルへの引継ぎ

CaseStoreへの保存と、次のPromptへ本文を載せることを分ける。CaseStoreには全WorkItem、
Hypothesis、ExplorationState、CycleRecord、StepRecord、ToolResult、Evidenceを残す。次のSolverへは、次の4層を渡す。

| 層 | 引き継ぐ内容 | 選択方法 |
|---|---|---|
| Case | 質問、制約、cycle・step・Tool・時間の残量 | 常に全部 |
| WorkTree | 全WorkItemのID、親ID、question、state、Hypothesis・Evidence件数 | 常に全部を簡潔に表示 |
| Exploration | 現Cycleのgoal・strategy、直前Step、新規・再採用・新Link Graph候補の差分batch、過去の全評価済みfrontierの短いledger、focusへ接続するNode・Link、depth、本文・展開status | 全履歴はCaseStoreに保持し、Promptには未評価・再評価差分と、再採用に必要な短い評価台帳だけを決定的に投影 |
| Focus detail | Solver指定WorkItem、反証の影響を受けるWorkItem、そのHypothesis、直前の全ToolResultと新規Evidence本文、保持Evidence本文 | focusと保持対象はSolver、影響対象はbasis ID完全一致で展開 |

直前のTool実行で新しく得たToolResultの実行状態とEvidence本文は、次のSolver判断へ必ず一度渡す。
そこでSolverが`retain_evidence_ids`へ選んだEvidenceは、その後のサイクルでも本文を渡す。
その他の非Graph Evidenceは削除せず、ID、出典、見出し、サイズ、取得cycleをmanifestとして毎回示す。
Graph navigation Evidenceはmanifestへ重複表示せず、候補情報は後述のArticle・LinkだけでSolverへ示す。
元のEvidence IDと来歴を必要とする監査はCaseStateを参照する。
Evidence本文に使える文字数はProfileの`max_material_evidence_chars`で制御し、Legal Profileの初期値は
50,000文字とする。この本文枠だけをGraph候補の可視性保証には使わない。

`fetch_articles`で新しく取得したArticleは、そのArticleに属する全Evidence chunkを次のSolver判断へ
一度まとめて提示する。ProjectorはArticleの一部chunkだけを表示して全文提示済みに見せず、Article途中で
文字数上限へ達する場合は`context_capacity_exceeded`を明示する。過去Evidenceをmanifestへ退避する処理と、
新規Article本文の完全提示を混同しない。Solverが法的関連性を評価する前にProgramが項・号を選別しない。

Graph navigationはExplorationStateのArticle NodeとDiscoveryLinkへ正規化し、CaseStoreで全件を正本として
保持する。SolverContextはそこから`graph_review_batch`と`graph_review_ledger`を決定的に差分投影する。
同じ候補を複数起点から発見した場合、Article Nodeは1件に正規化し、Linkはすべて保持する。
Review batchで評価対象になったArticleについては、その判断に必要な全Linkを同じbatchへ投影する。
Graph navigation EvidenceのJSON・Evidence IDは`material_evidence`、`evidence_manifest`、
`recent_tool_results.evidence_ids`、`navigation_evidence_ids`、`omitted_evidence_ids`へ重複掲載しない。
Graph ToolResultは実行status、件数、ExplorationStateへの投影済みであることだけをSolverへ示す。
CaseStore上のEvidence、ToolResult、生成元・監査用の関係来歴は正本として完全に残す。

Article Nodeの同一性は`article_id`、Linkの同一発見経路は
`(seed_article_id, candidate_article_id)`を決定的な正規化単位とする。
同じ組に複数のrelationがある場合は、`mode`、`proposedPredicate / rawRelation`、`direction`、
`basisEdgeId`、`classificationRunId`、`sourceKind`の
異なる値を失わず`relations`へ保持する。この正規化は同一IDと関係属性の機械的統合であり、
どのLinkが質問に関係するかをプログラムが判断する処理ではない。

Graphの次pageがまだ取得されていない場合は候補を推測せず、ExpansionSliceの`partial`と`next_cursor`を示す。
探索用Evidence本文を文字数上限で省略しても、今回の`graph_review_batch`と
`graph_review_ledger`に含むArticle ID、法令名、条番号・見出し、minimum depth、content status、review status、
起点Article・Link、mode・predicateまたは原文relation・direction・classificationRunId・sourceKind、
ExpansionSliceの`partial / complete`は
Exploration投影へ残す。未提示pageと過去の全詳細はCaseStoreに保持し、件数、cursor、
各Review statusの集計をContextに示す。LLMが当該batchの内容を識別できないハッシュIDだけを
示すことは禁止する。

本文量上限で省略するとき、プログラムは関連度や法令上の重要度で選ばない。新規、Solver保持指定、
固定の文字数上限という決定的な規則だけを使い、省略した非Graph Evidence IDを明示する。Solverは既知IDを
指定する`load_evidence`で本文の再提示を要求できる。

共通Prompt、WorkTree、Graph review batch・ledger、Evidence本文、出力予約を合計した入力が選択modelの
context容量を超える場合、Context BuilderはReview batchを意味的に間引かず、より小さい機械的pageへ
分割する。1候補または1候補の必要Linkだけでも収まらない場合は
`context_capacity_exceeded`で実行を止める。候補を隠してSolverに完了判断させるfallbackは設けない。
通常運用ではGraphのpage上限、step上限、
50,000文字の本文上限によりこの状態を避け、model変更時はProfile読込み時または実行前に入力・出力予約を検証する。

`load_evidence`はCaseStoreに既にあるEvidenceを読む汎用read-only ToolRequestである。
新しい法的判断やEvidenceを生成せず、指定された既知IDの本文を次のSolver判断へ戻すだけとする。

次のサイクルへPrompt全文、過去のLLM生応答、運用ログを引き継がない。構造化された現在の
CaseStateと変更理由を正本にする。CaseUpdateに現れなかった別系統のWorkItemや未完了WorkItemも、
WorkTree案内とCaseStoreには残る。

各StepのToolResultは直後のSolver判断へ必ず渡す。最後の許可StepでもHypothesis、WorkTree、frontierを
更新してからCycleを閉じ、未評価のToolResultを残したまま次Cycleまたは回答へ進まない。

各Cycleの`start_cycle`はresearch profileを使う。Tool実行後のstep判断、Cycle終了判断、
Reviewer差戻し後の判断はintegration profileを使い、直前結果の意味評価、状態更新、
`continue_cycle / start_next_cycle / finalize`の選択を同じLLM呼び出しで行う。
`start_next_cycle`後の次呼出しで、research profileが引継ぎを読んで新しいCycle計画を作る。
通常終了のためだけの独立Integrator呼び出しは設けない。上限到達時はintegration profileへ
`finalize_only=true`を渡し、追加ToolRequestだけを禁止する。

通常の`finalize`では、Solver自身がすべてのWorkItemを`resolved`または`dropped`へ更新する。
実行上限で新Cycleを開始できない限定`finalize`だけは、open WorkItemとunresolved Hypothesisを保持し、
対応IDと回答への影響をlimitationsへ明示する。プログラムはどちらの意味状態にするかを判断せず、
通常終了と限定終了それぞれの参照整合だけを検証する。

下位法令・委任先の未確認事項も、WorkItem、Hypothesis、gaps、ExplorationIntentで管理する。
取得本文に質問へ関係する委任があれば、Solverは対応するHypothesisを`unresolved`、WorkItemを`open`の
まま追加調査する。Graph結果だけでは根拠にせず、端点Articleを`fetch_articles`で取得して評価する。
どの委任が質問に関係するか、どの本文で確認できたかはSolverが判断し、プログラムは既知ID、
ToolRequest、grounding Evidenceの参照整合だけを検証する。

Legal Profileは`legal_graph_neighbors`をread-only Toolとして登録する。Solverはrelation用
`ExplorationIntent`へ、対象Hypothesis、既知の起点Article、Graph mode、1つのpredicateまたは原文relation、
1つのdirection、必要な構造filterを明示する。ProgramはCaseに固定された`classificationRunId`を加えて
Tool引数へ機械的に投影し、本文取得を選んだという理由だけで全predicateを自動取得しない。
起点本文を取得済みの場合はGraphを直ちに実行でき、同じDecisionで本文取得と
relation Intentが明示された場合は同じStepの観察へまとめる。本文を読んで初めて関係探索が必要と判明した場合は、
直後のSolver判断で新しいIntentを作り、同じCycleの次Stepで実行する。

選択Nodeが最大depthならGraphを実行しない。同じArticle・scopeのGraphは成功後に重複実行せず、別Hypothesisが
同一scopeを要求した場合は保存済みLinkを再利用する。取得した1ホップは各隣接ArticleをExplorationStateの
Node・Link・frontierへ保存する。Solverがfrontierから選んだArticle本文を同じCycleの次stepで取得しても、
relation Intentがなく、または最大depthなら、そのArticleからGraphを再展開しない。

Solverは5つの`proposedPredicate`、原文`REFERENCES / EXPLAINS`、起点から見た
`from_subject / to_subject`を共通Promptの定義どおりに解釈する。候補表示だけで法的結論を出さず、
質問と現在のHypothesisに関係すると判断した既知frontier IDだけを、Profileの少数上限内で次の本文取得へ
`select`する。Graph Reviewの初期選択上限は3件とし、関連するが今回の取得枠に入れない候補は
`defer`として短いledgerへ残す。同じhopの未評価候補や別枝も削除せず、機械的pageまたは
次Cycleへ残す。候補の関連性、取得順、
`reject`はSolver、Node・Linkの重複排除、depth、取得済み判定、Tool実行はプログラムが担当する。

Graphは発見経路の1つであり、必要条文到達の唯一の経路にしない。質問で明示された観点に対応する
open WorkItemが残り、関連するGraph候補がない、最大depthへ達した、または既存のGraph方針を探し切った場合、
SolverはそのWorkItem、確認済み本文の委任・参照表現、法令名、条番号等を基に`legal_search`を要求できる。
その検索結果は新しい深さ0の起点となる。検索語の作成と検索へ切り替える判断はSolverが行い、
プログラムは未解決WorkItemから検索語や必要条文を生成しない。

Solverは`graph_review_batch`に提示された新規・再評価差分と、`graph_review_ledger`の
`relevant_deferred`候補について、質問と現在のHypothesisとの関係を判断する。ledgerの
`selected + failed/timeout`は関連性を再判定せず、取得を再試行するかを判断する。
関係する候補はGraph Reviewの選択上限3件とCycleの残り本文取得枠の小さい方まで`select`し、
上限外は`defer`として次Cycle候補へ残す。関連判断と優先順はSolverが行い、Programは本文取得済み件数と
残り枠を機械的に検証する。関係すると判断した
未確認候補や、明示された質問観点に対応するopen WorkItemを残したまま、通常の`finalize`を選ばない。
実行上限に到達した場合だけ、未確認範囲と回答への影響をlimitationsへ明示して終了する。

Evidence IDとArticle IDは別の名前空間として扱う。Contextは、今回本文を提示し根拠・引用に使える
完全一致IDを`grounding_evidence_ids`、Graph以外の発見用・引用不可Evidence IDを`navigation_evidence_ids`、
本文取得に使えるIDを`fetchable_article_ids`として明示する。SolverはArticle IDからEvidence IDを
組み立てない。プログラムは一覧をmetadataから決定的に展開するだけで、採用対象はSolverが選ぶ。
`legal_search`が返す法令・ガイドの代表chunkも発見用として`navigation_evidence_ids`へ置き、元の
contentUnitIdとは別の`search-nav-*` Evidence IDを付ける。`fetch_articles`で取得した本文だけが
`grounding_evidence_ids`へ入る。これは検索結果の法的関連性をコードが判断する規則ではなく、
「候補検索」と「指定Article本文取得」というToolの取得段階を表す構造契約である。
Graph navigationはEvidence IDをSolverへ再掲せず、ExplorationStateのArticle Nodeと
DiscoveryLinkの起点・候補Article IDから`fetchable_article_ids`を決定的に作る。

Model出力はdiscriminator付きSolver Command全体をProviderのstructured-output schemaへ直接載せる。
内部JSON文字列へ二重エンコードせず、Provider応答をPydanticで復元してからAgentLoopの参照整合検証を適用する。

SolverDecisionが参照・件数・状態等の構造契約に違反した場合、そのDecisionはCaseStateへ適用せず、
違反理由と直前Decisionを同じSolver profileへ1回だけ返す。Solverが意味判断を保ったまま構造を
自己修復する。2回目も違反した場合は`protocol_error`とする。プログラムは未知IDを推測補正せず、
上限超過分の根拠やToolRequestを選別せず、WorkItemの終了理由も生成しない。

## 6. Reviewer

### 6.1 既定値

Reviewerはデフォルトで無効にする。

```yaml
reviewer:
  enabled: false
  max_revisions: 1
```

プログラムは回答内容からReviewerの要否を推測しない。Run開始時に解決したProfileの
`reviewer.enabled`だけで経路を決める。

### 6.2 Reviewer契約

Reviewerへ渡すものは次に限定する。

- 利用者の質問
- Solverの最終回答
- Solverが実際に選んだ引用Evidence
- Solverが明示したlimitations

```python
class ReviewResult:
    verdict: Literal["accept", "revise"]
    findings: list[ReviewFinding]
```

Reviewerは追加調査の実行経路を直接選ばない。`revise`では、誤り、根拠不足、引用との不一致を
具体的に返す。Solverがその指摘を読み、回答修正か追加調査かを判断する。

1回の修正後に再確認する場合、Reviewerをもう1回呼ぶ。2回目も`revise`なら
`review_failed`として未承認を明示し、それ以上繰り返さない。

Reviewer有効時にReviewer自体がtimeoutまたは契約違反になった場合も、勝手に`accept`へしない。

## 7. Model ProfileとPrompt

### 7.1 役割ではなく呼び出し用途でモデルを選ぶ

同一provider内で、research、integration、reviewのモデルを別々に設定できるようにする。
検索・回答経路の初回動作確認には、ローカルOllamaの`gemma4:e4b`をresearchとintegrationの両方へ使い、
Reviewerは無効にする。ここで確認するのは、契約、Prompt、Tool選択、検索、Cycle引継ぎ、根拠利用が
一連で動くことである。不具合時は先に実装、契約、Prompt、入力、traceを調査し、モデル性能だけを
原因にしない。Gemmaでこの動作確認を通した後に限り、必要な品質・性能比較として同一provider内の
別モデルまたは別providerのProfileを実行する。

この検索時の確認Profileは、後述する非同期Relation分類のLuna Worker / Reviewerとは別用途である。
検索確認のためにLunaを使わず、Relation分類の精度評価をGemmaの結果で代替しない。

```yaml
name: legal-default
provider: anthropic

solver:
  common_system_prompt: domains/legal/prompts/solver_common.md
  research:
    model: claude-haiku-4-5-20251001
    max_output_tokens: 4096
    system_prompt: domains/legal/prompts/solver_research.md
  integration:
    model: claude-haiku-4-5-20251001
    max_output_tokens: 4096
    system_prompt: domains/legal/prompts/solver_integration.md

reviewer:
  enabled: false
  model: claude-haiku-4-5-20251001
  max_output_tokens: 4096
  system_prompt: domains/legal/prompts/reviewer.md
  max_revisions: 1

limits:
  max_research_cycles: 4
  max_fetched_resources_per_cycle: 4
  max_steps_per_cycle: 4
  max_total_steps: 8
  max_tool_requests_per_step: 4
  max_parallel_tools: 4
  max_selected_frontier_per_step: 3
  max_graph_candidates_per_scope_page: 20
  max_graph_candidates_per_review_batch: 20
  max_exploration_depth: 1
  max_material_evidence_chars: 50000
  max_solver_input_chars: 240000
  max_retained_evidence: 12
  cycle_close_reserve_sec: 15
  min_next_cycle_budget_sec: 25
  finalization_reserve_sec: 35
  max_wall_time_sec: 180
```

回答Agentとは別に行う正本Relation分類は、次のオペレーター実行Profileを使う。
これはAgent APIの実行時Profileではなく、Codex skillが判定JSONLを作る際の固定契約である。

```yaml
relation_adjudication:
  execution: codex_subscription
  worker_model: gpt-5.6-luna
  reviewer_model: gpt-5.6-luna
  reasoning_effort: high
  predicates_per_candidate: 5
  candidates_per_worker_session: 5
  candidates_per_reviewer_session: 5
  checkpoint_granularity: candidate
  max_active_sessions: 3
  worker_reviewer_separate_contexts: true
  max_revision_rounds: 1
```

Reviewerは最終回答を検査する6章の任意Reviewerではなく、オフライン意味分類専用である。
5件は回答AgentのGraph Review pageサイズや1 Stepの本文取得数とは無関係である。WorkerとReviewerは
別session・別contextとし、差戻しは元のWorker、差分再Reviewは元のReviewerへ戻す。同時に実行中の
Codex sessionは最大3つとする。完了済みshardのcontextを次shardへ再利用せず、最後のshardだけ5件未満を許す。
同じsessionに5候補を提示しても、候補別record、候補別checkpoint、候補別合否を維持し、候補順の変更や
別候補の追加によって既存候補の判断が変わらないことを受入試験で確認する。
WorkerとReviewerのmodel、reasoning effort、差戻し上限、skill versionを成果物manifestへ記録する。
API経由でLunaを呼ばず、Codexサブスクリプションのオペレーター実行とする。
`reasoning_effort: high`は出力token上限ではなく、各Codex sessionの開始時に指定する推論深度である。
WorkerとReviewerの双方へ明示し、同じClassificationRun内では変更しない。5 predicateの二条件、意味方向、
根拠span、構造適合性を一度に照合する品質条件として固定する。別の推論深度を比較する場合は別Run・
別manifestを作り、代表100件の品質ゲートを再実行する。Coordinatorは推論深度を判定結果から推測せず、
session起動設定とmanifestの一致だけを検証する。

現行コードのOllama Profileは比較・契約試験用として残る。Gemmaは手動監査14件で5 predicate完全一致が
4/14だったため、全件Runのpublishには使用しない。Luna方式は既存14件で14/14、新規20件で最終20/20を
確認済みである。さらに代表100件は、法令94件を構造監査し、構造上resolvedの73件中、
Article revision不一致の1件を`needs_resolution`として除いた72件を意味判定対象とし、
Codex GPT-5.6 Solが法令94件と別schemaのガイド6件を全件個別監査して正解を確定した。
確定正解を伏せたLuna再評価では、構造`89/94`、意味predicateとstatusの完全一致が差戻し後
`57/72`、意味方向まで含む完全一致が`56/72`だったため、現行方式を無監査でpublishしない。
Luna Worker / Reviewerの出力はaudit履歴に残すが、Lunaの精度測定から独立した正解とする。
ただし、Luna出力を候補hash・snapshot・Pydantic契約で検証して
`ClassificationRun`へ取り込むimportが完成するまでは、評価JSONLをpublish済みGraphとして扱わない。

`solver_common.md`はresearchとintegrationの両方へ合成する。質問観点、法令階層・委任先追跡、
Evidence利用、Cycle・Stepの意味、完了条件のようにサイクル間で変わらない規則を段階別Promptへ重複記載しない。
`solver_research.md`は初回の作業分解と発見、`solver_integration.md`は観測結果の反映と次の
行動または完了の選択だけを追加する。

モデルID、token上限、timeout、Reviewer有効・無効はProfileだけで変更する。
AgentLoopや法令ツールへmodel IDをハードコードしない。

`limits.max_exploration_depth`はFrameworkのProfile契約として整数`1`または`2`だけを受け付けるが、
Legal Profileは`1`に固定する。未設定、`0`、`3`以上、整数以外はProfile読込み時の設定エラーとし、実行中に丸めたり既定値へ
補正したりしない。設定値はRun開始時に解決してCaseへ固定し、途中のProfile変更で進行中Caseの
到達範囲を変えない。

Graph pageの上限は意味的な枝刈りではなく、Neo4jから1回に取得する機械的な件数上限である。Programは
`minimum_depth`、`discovered_cycle`、`frontier_item_id`の安定順で候補を保存する。Graph page上限に
達したExpansionSliceは`partial`として`next_cursor`を残し、まだ取得していない候補の不存在を推測しない。
取得済みGraph候補はCaseStoreから落とさない。SolverContextへは新規・未評価・再採用の
`graph_review_batch`と、過去の全評価済みfrontierを短く表す`graph_review_ledger`を載せる。
`max_graph_candidates_per_review_batch`は意味的な省略ではなく、全候補を差分Reviewに通すための
機械的pageサイズである。`max_material_evidence_chars`はArticle本文などのEvidence本文だけに適用する。
Programは候補の関連度や法令上の優先度を計算せず、上限外候補を`reject`や`complete`へ読み替えない。
`max_solver_input_chars`は共通Prompt、構造情報、Graph review batch・ledger、Evidence本文を含む最終入力全体の安全上限である。
modelのcontext容量に合わせてProfileで変更し、超過時は候補を省略せず`context_capacity_exceeded`とする。

`max_fetched_resources_per_cycle`はCycle内で`fetch_articles`が`succeeded`にした重複なしArticle数を数える。
検索候補とGraph候補を表示しただけでは数えない。現在の`max_tool_requests_per_cycle`は
`max_tool_requests_per_step`へ改名し、1 Solver DecisionのToolRequest数だけを検証する。Cycle累計Tool数と
自動Tool数はtraceで別に数え、本文取得予算と混同しない。

初期実装では、1つのRun内でproviderを統一する。providerをまたぐ役割分担は対象外とする。

### 7.2 Promptの配置

Profileにはsystem promptの参照先とversionを持たせ、法令固有のprompt本文は
`domains/legal/prompts/`へ配置する。

- 汎用ループの制御規則: `agent_framework`の共通prompt fragment
- 法令の調査方法・注意事項: `domains/legal/prompts/`
- モデル、token、timeout、prompt参照: Profile
- API key: 環境変数またはsecret管理

Profileを切り替えても、CaseStateの意味とTool契約は変わらない。

Skillsは初期ループの必須要素にしない。必要になった場合だけ、明示的に選択されたSkillの指示を
Solver promptへ追加する。SkillによってTool権限や意味判断の責務を拡大しない。

### 7.3 LLMへ見せるstatusのPrompt契約

LLMが入出力するJSON Schemaに`enum`を載せるだけでは、値の意味、相互関係、決定主体は伝わらない。
値、基本的な意味、決定主体は5.2の説明付きstatus契約から共通Solver Prompt用語集へ生成し、
research・integrationの両方へ必ず合成する。次の契約語彙には、値の定義に加えて誤解しやすい相互関係と
使用規則を記載する。段階別Promptへ別表現で重複させない。

```text
契約語彙:
- Graph Reviewはあなた（Solver）がGraph候補の関連性と本文取得順を判断する処理モードであり、
  任意のReviewer Agentによる最終回答Reviewとは別である。Reviewer無効時もGraph Reviewを行う。
- program-owned statusは取得・実行の事実であり、あなたは値を変更しない。
- ToolResult succeededはTool正常終了だけを意味し、内容の関連性・正しさ・仮説支持を意味しない。
  failedはエラー終了、timeoutは時間切れであり、対象の不存在を意味しない。
- Graph ToolResultのgraph_projection_updated=trueは、取得したGraph情報がCaseStoreへ保存され、差分batchまたはledgerへ投影可能になったことを示す。
  evidence_countはToolが作成したEvidence件数であり、関連候補の採用件数や根拠件数を意味しない。
- Cycle plannedはgoal・探索方針・完了条件保存済み、runningは同じ方針でstep反復中、completedは
  Cycle全体の評価と終了理由適用済みを意味する。Tool 1回やGraph 1ホップをCycle完了としない。
- Step plannedはToolRequest保存済み、observedは全Tool結果保存済み・意味評価前、completedは
  その結果による仮説・作業・frontier更新適用済みを意味する。observedを評価せず次stepへ進まない。
- content not_requestedは本文未要求、pendingは要求済み・結果待ち、succeededは当該Articleについて
  OpenSearchに登録済みの全本文chunk取得済み、failed/timeoutは全chunk取得失敗・時間切れである。
  content succeededは原データのindex完全性、relevant、supportedを意味しない。
- frontier reviewのunreviewedは、現在のHypothesisに対してまだ関連性を評価していない候補である。
  selectedは、あなたが本文取得対象として選んだ状態であり、本文取得成功を意味しない。
  relevant_deferredは、あなたが関連ありと判断したが今回の本文取得枠に入れず保留した状態、
  rejectedは、あなたが現在の質問・Hypothesisの検証に不要と判断した状態である。
  content statusとfrontier review statusを同じ意味として扱わない。
- expansion not_startedはscope未要求、pendingは結果待ち、partialは未取得page/scopeあり、
  completeは当該scope取得完了、failed/timeoutは取得失敗・時間切れである。
  expansion completeは隣接Article本文の確認完了を意味しない。
- kind=start_cycleはActive Cycleがないとき、今回のgoal・strategy・完了条件と最初の行動を決める。
  kind=continue_cycleはActive Cycleの仮説・探索方針を維持して次stepへ進む。
  kind=start_next_cycleは、現方針で完了できない理由またはCycle予算境界を明示して現Cycleを閉じ、
  結果と未解決事項だけを引き継ぐ。次Cycleの計画・ToolRequestは含めず、次の呼出しでstart_cycleを選ぶ。
  kind=finalizeは必要な根拠を探し切った判断または実行上限での限定回答を、追加Toolなしで確定する。
- WorkItem openは未完了、resolvedは問いへの結論あり、droppedは前提否定・重複・無関係による除外である。
  resolved/droppedは理由をresolutionへ書く。取得失敗だけをresolvedにしない。
- WorkItemのquestionを変えずに親の問いへ引き続き寄与できるならretainする。仮説、検索語、検索先だけの誤りは
  replaceの理由にしない。不足観点は子または兄弟WorkItemとして追加する。question自体を別の意味へ変える必要が
  ある場合だけreplaceし、質問に無関係または重複と根拠から判断した場合だけdropする。
- 親WorkItemをreplaceする場合は旧部分木のopenな子も明示的にdropするかdrop_subtree=trueを返し、
  新しい部分木は別IDで作る。Programは旧子を新しい親へ自動的に付け替えない。
- Hypothesis supportedは提示されたgrounding Evidenceがstatementを支持、contradictedは否定、
  unresolvedは根拠不足・両義的・未確認である。supportedでもWorkItem全体の完了を意味しない。
- Frontier selectは今回の検証行動へ採用、deferは関連ありだが今回の取得枠外として保留、
  rejectは現在の質問・Hypothesisに無関係と理由付きで判断した状態である。
  Decisionに現れないfrontierはrejectせずunreviewedのまま残す。
- graph_review_ledgerの評価済みNodeを新しいHypothesisの検証に使う場合は、
  frontier_re_adoptionsにNode・WorkItem・Hypothesis・理由を明示する。Programに自動転用を要求しない。
- Cycle境界では、本文未取得のactiveなrelevant_deferred全件へdeferred_frontier_resolutionsを返す。
  carry_forwardは次Cycle以降への保持、no_longer_neededは回答に不要との意味判断、
  unresolved_at_limitは次Cycleを開始できない上限時の未確認を表す。Programは既知ID、全件性、
  actionと次動作の参照整合だけを検証し、どのactionが法的に妥当かは判断しない。
- impact retainはWorkItemを維持して前提を差替え、replaceは旧WorkItemをdroppedにして新IDへ置換し、
  dropは不要として閉じる。これらは新たにcontradictedとなったbasisの影響をSolverが判断する値である。
- 観察後に、元の利用者質問に対するWorkTreeの範囲、重複、反証Hypothesisの影響を監査する。局所的な
  Hypothesis・WorkItem・ToolRequestの追加で現在方針を維持できるならcontinue_cycleを選ぶ。初期分解の
  主要部分、中心仮説、検索起点または対象階層の前提を変える必要がある場合、またはCycle取得枠が尽きても
  必要と判断した未取得Evidenceが残る場合にstart_next_cycleを選ぶ。
- cycle_budget_reached=true、cycle_step_limit_reached=true、またはcycle_close_required=trueなら、
  現Cycleに新しいToolRequestを追加しない。直前までのToolResultを評価し、WorkItem・Hypothesisを更新する。
  完了できるならfinalize、未解決で残りCycle予算があるなら、現Cycleの結果、次Cycleへ渡す命題、
  再採用候補を明示してstart_next_cycleを選ぶ。次のgoal・strategyは次のstart_cycleで決める。
- cycle_step_timeoutは中間呼出しがCycle予算で時間切れになった実行事実であり、仮説の否定、
  根拠の不存在、provider全体の障害を意味しない。予約済みのCycle終了判断で手元の結果を整理する。
- finalize_only=falseなら必要な追加調査をcontinue_cycleでき、方針変更が必要ならstart_next_cycleを選べる。
  trueなら上限後のCycle終了判断なので、
  追加Toolを要求せず、確認済み範囲と未確認範囲を区別してfinalizeする。
- material_included=trueだけが本文提示済みである。falseはmanifest・探索構造だけで本文未提示である。
- grounding_evidence_idsは意味判断・引用可能な本文、navigation_evidence_idsはGraph以外の候補発見専用、
  fetchable_resource_ids（Legalではfetchable_article_ids）は本文取得Toolへ完全一致で渡せる既知Resource IDである。
```

Legal Domain Packの共通Promptには次を追加する。

```text
- max_exploration_depthはOpenSearch起点をdepth 0としてGraph関係をたどれる最大depthであり、Legal Profileでは1に固定する。
  depthが上限未満で、relation用ExplorationIntentに既知の起点Articleと明示selectorがある場合だけ、Programが
  そのscopeの1ホップ候補を取得する。上限depthのArticleは本文を取得・評価できるが、そこからGraph候補は増えない。
  Cycle変更は既存起点のdepthをリセットしない。
- 候補発見・Graph展開・本文取得の各ToolRequestは、今回検証する既知WorkItem・Hypothesisと
  ExplorationIntentへ結び付ける。具体的Hypothesisを立てる前の初回検索だけは、理由を示したWorkItem単位の
  search Intentを許可し、Graph Intentには使わない。
  OpenSearchでは未確認事項から作ったqueryと必要最小限のfilter、Graphでは既知の起点Article、mode、
  1つのpredicateまたは原文relation、1つのdirection、必要な構造filterを明示する。predicateを空やallにして
  全種別を要求しない。predicateを選べない場合は、Graphを全探索せずOpenSearchで関係を示す本文または新しい起点を探す。
- Hypothesisとpredicateの対応、検索語、filter、方向、優先度はSolverが判断する。Programへ補完を要求しない。
  検索結果は指定scope内の候補であり、Hypothesisの支持を意味しない。本文取得後に意味を判断する。
- Graph候補がない、最大depthへ達した、または現在のGraph方針を探し切ってもopen WorkItemが残る場合は、
  その問いと確認済み本文の委任・参照表現を使ってlegal_searchを要求し、新しいdepth 0起点を探す。
  Programに必要条文や検索語の推測を任せない。
- graph_review_batchは今回評価が必要な新規候補、再採用候補、新Linkが加わった既評価候補の差分である。
  `review_trigger`は`new_frontier / re_adopted / new_link`のいずれかであり、新Link差分では直前の判断を
  維持するか変更するかを追加Linkも含めて再評価する。Articleごとの法令名、条番号・見出し、content status、
  起点、WorkItem・Hypothesis、当該候補について今回までに判明した全relationが載る。
  graph_review_ledgerは過去の全評価済みfrontierのID、Article、WorkItem・Hypothesis、
  selected / relevant_deferred / rejected、短い理由を示す台帳であり、過去の全Graph Link詳細やLLM生応答ではない。
  CaseStoreの全履歴がPromptから失われたのではなく、評価済みの詳細を重複入力しないための差分投影である。
  batch内の同じArticleへ複数Linkがあれば全てを質問、WorkItem、Hypothesis、取得済み起点本文と照合する。
  表示順や末尾にあることを理由に候補を無視せず、relationだけで法的関連性を確定しない。
- content statusのnot_requestedは未要求、pendingは結果待ち、succeededは当該ArticleについてOpenSearchに
  登録済みの全本文chunk取得済み、failedは全chunk取得前のエラー終了、timeoutは時間切れである。
  succeededは原データのindex完全性、法的関連性、根拠採用を意味しない。
- 検索本文中の条番号、法令番号、documentIdからArticle IDを生成しない。必要な参照先IDが
  fetchable_article_idsになければfetch_articlesから外し、法令名・条番号・確認事項でlegal_searchする。
  Decisionを返す直前に、fetch_articlesの全IDをfetchable_article_idsと完全一致で照合する。
- 質問に関係すると判断した1ホップ候補は、Graph Reviewごとに最大3件、かつCycleの
  残り本文取得枠内でselectする。関連するが枠に収まらない候補はdeferし、
  graph_review_ledgerと次Cycleの引継ぎ候補へ残す。Graph候補だけを根拠にせず、端点Article本文を確認する。
- Graph mode `semantic_assertion`は非同期分類済みの未確認候補、`explicit_reference / explains`は
  原文またはガイド上の明示関係である。
- relation_assertionの`proposedPredicate`は候補となる意味関係であり、法的に確認済みの正式関係ではない。
  RelationAssertionとして存在すること自体が未確認を意味する。`SUBJECT / OBJECT`の両端Article本文を取得し、
  今回の質問における意味はSolverが判断してCaseStoreへ保存する。Neo4jの候補を更新・昇格しない。
- REFERENCESはfrom本文がtoを明示参照する。意味候補は、IMPLEMENTS=親規定から具体化規定、
  INCORPORATES=準用・読み替える規定から取り込まれる規定、USES_DEFINITION=利用規定から定義規定、
  EXCEPTION_TO=例外規定から一般規定、OVERRIDES=優先規定から排除・修正される規定である。
  `from_subject`は起点がSUBJECT/from側、`to_subject`は起点がOBJECT/to側である。
- `MENTIONS`はLegal Graphの関係種別ではない。単なる言及をGraph候補、本文取得対象、根拠として扱わず、
  ガイドと条文の明示的対応だけを`EXPLAINS`として扱う。
- 旧referenceKindは移行監査情報であり、意味predicateや法的結論に使用しない。RelationAssertionでは
  `basisEdgeId / supportingSpans / classificationRunId`を確認し、両端本文で今回のHypothesisとの関係を判断する。
- ClassificationRunのcoverageにuncertainまたはfailedがある場合、Assertionがないことを関係不存在と解釈しない。
- relationSource、sourceId、derivedFromEdgeId等の生成元・監査用来歴はCaseStateに保持されるが、
  SolverContextへは重複投影されない。SolverはGraph review batchに示された関係属性と取得本文で判断する。
```

Reviewer Promptには`accept=指摘なし・findings空`、`revise=具体的findingsあり`を定義する。
プログラム内部だけの`RunStatus`、`stop_reason`、trace error codeはSolverへ渡さず、Prompt語彙を増やさない。
status追加・名称変更時は5.2の説明付きstatus契約を変更する。Provider schemaと共通Promptのstatus用語集は
同契約から生成し、手作業でenumと意味を同期しない。serialized valueを変更する場合はProfile versionだけでなく
`contract_version`と既存Caseのmigrationまたは旧値読替えを同じ変更単位で追加する。

### 7.4 Graph差分Review・Cycle予算のPrompt契約

statusの値と基本定義は5.2の契約から共通Promptへ生成する。次表は自動生成するstatus用語集の重複ではなく、
各処理モードで値をどう使うかという手順・業務上の制約である。コードと状態型の変更と同じcommitで更新し、
Promptだけを先行させて現行SolverContextに存在しない値をLLMへ指示しない。

| Prompt / schema | 必須の内容 |
|---|---|
| `solver_common.md` | Graph ReviewはSolverの処理モードであり、任意のReviewer Agentとは別であることを定義する。 |
| `solver_common.md` | Cycleは最大4、1 Cycleの本文取得累計は4、Graph Review選択は最大3と定義する。`max_tool_requests_per_step`と本文取得累計を区別する。 |
| `solver_common.md` | `cycle_budget_reached`、`cycle_close_required`、`cycle_step_timeout`、`remaining_fetch_capacity`の意味と決定主体を定義する。 |
| `solver_common.md` | `unreviewed / selected / relevant_deferred / rejected`と`select / defer / reject`を定義し、content statusと混同しないよう指示する。 |
| `solver_common.md` | `fetch_articles`のcontent `succeeded`は当該Articleの全登録済みchunk取得完了を意味し、index完全性・関連性・根拠採用を意味しないと定義する。本文取得に`partial`を導入しない。 |
| `solver_common.md` | 評価済みNodeを別Hypothesisへ使う場合は`frontier_re_adoptions`で明示し、Programが自動転用しないことを定義する。 |
| `solver_common.md` | 各検索を既知WorkItem・Hypothesis・ExplorationIntentへ結び付け、OpenSearchとGraphの明示selector、候補と根拠の違い、selectorをProgramへ補完させないことを定義する。 |
| `solver_common.md` / `solver_graph_review.md` | RelationAssertionは`SUBJECT / OBJECT`で接続された未確認候補で、`proposedPredicate`は確定関係ではないと定義する。5 predicateの向き、`ClassificationRun` coverage、検索時の案件判断をNeo4jへ更新・昇格しないことも定義する。 |
| `solver_research.md` / `solver_integration.md` | 未確認事項から検証目的と最小scopeを作る。Graphの関係種別を選べなければ全種別を要求せず、OpenSearchで根拠または起点を発見する。 |
| `solver_graph_review.md` | `graph_review_batch`と`graph_review_ledger`を読む。`review_trigger`を解釈し、過去の詳細が再提示されないことを候補の不存在と解釈しない。 |
| `solver_graph_review.md` | 各batchの全候補をWorkItem・Hypothesis別に評価し、最大3件を`select`、関連する残りを`defer`、無関係と判断したものだけを`reject`する。 |
| `solver_graph_review.md` | `remaining_fetch_capacity=0`なら新たにselectせず、関連候補をdeferしてCycle終了判断へ戻す。Graph Reviewから直接次Cycleの法的方針を決めない。 |
| `solver_integration.md` | Cycle上限に達したら、直前までのToolResultを評価し、Hypothesis・WorkItem・Evidence・Graph ledgerを整理した後に、finalizeまたは次Cycleへの構造化引継ぎを返す。次Cycleのgoal・strategyは返さない。 |
| `solver_integration.md` | Cycle境界でactiveな`relevant_deferred`全件を`carry_forward / no_longer_needed / unresolved_at_limit`のいずれかへ明示し、黙って破棄しない。`start_next_cycle`では次Cycle計画やToolRequestを返さない。 |
| `relation_classifier.md` / `relation_grounder.md` | 同じ候補を1 predicateずつ専門判定し、predicate固有の二必要条件とfindingを返す。成立predicateだけを別の根拠付与応答へ渡し、方向・参照箇所・両端spanを選ぶ。Programは意味を補完せず、条件整合・既知ID・件数だけを検証する。 |
| Provider schema | Review判断対象は現在のbatch、本文取得へ選べるIDはbatchの候補とledgerの`relevant_deferred`、再試行時の`selected + failed/timeout`に制限する。選択上限は`min(3, remaining_fetch_capacity)`とする。`rejected`は新Link差分でbatchへ再提示された場合を除き同じHypothesisで再選択させず、別Hypothesisへの`frontier_re_adoptions`はledgerの既知Nodeと既知のopen WorkItem・Hypothesisだけを許可する。候補の関連性や優先度はschemaまたはProgramで補正しない。 |
| Provider schema | Deferred解消はledgerの既知IDだけを許可する。Programは全件性と次動作との矛盾だけを拒否し、関連性・必要性を補正しない。 |
| Provider schema | Graph Reviewモードで必ず空になるre-adoption、deferred解消、answerは空配列またはnullの簡易schemaとし、未使用の動的enumをコンパイルさせない。 |
| Provider schema | ExplorationIntentのWorkItem・Hypothesis・起点Articleは既知ID enum、Graph mode、predicateまたは原文relation、direction、構造filterはLegal Tool allowlistへ限定する。predicateは5種、directionは`from_subject / to_subject`だけを許可し、空・all・複数predicateの一括指定を許可しない。`APPLIED_BY / MENTIONS`をenumへ含めない。 |

Prompt契約テストでは、共通Prompt、処理モード別Prompt、Provider schemaが上表と同じCommand、status、
上限、Graph差分投影を使用することを検査する。

## 8. CaseStore

### 8.1 初期契約

```python
class CaseStore(Protocol):
    def create(self, state: CaseState) -> None: ...
    def load(self, case_id: str) -> CaseState: ...
    def save(self, state: CaseState) -> None: ...
```

初期実装は`InMemoryCaseStore`だけとする。

- Pythonプロセス内だけで有効
- プロセス終了で内容を失う
- 複数プロセス整合性を保証しない
- DB transactionやdurabilityを保証しない

Cycle開始時はgoal・strategy・completion criteriaを検証して`CycleRecord.phase=planned`を保存し、
最初のStep開始時に`running`へ変える。各action-observation Stepは次の3 checkpointで保存する。

1. SolverのToolRequestを検証して`StepRecord.phase=planned`を保存する。
2. 全ToolResult・Evidence・探索Node/Linkを保存して`StepRecord.phase=observed`にする。
3. 次のSolverによるHypothesis・WorkItem・frontier更新を検証し、`StepRecord.phase=completed`にする。

Solverが`continue_cycle`を返した場合は、同じ`CycleRecord`へ次のStepを追加する。
`start_next_cycle`または`finalize`を返した場合だけ現在Cycleを`completed`にし、
`research_cycle_count`を同時に増やす。
各`fetch_articles` ToolResultが要求した全Articleの全登録済みchunkを取得して`succeeded`になった時点で、重複なしのArticle IDを
`CycleRecord.fetched_resource_ids`へ追加する。残り本文取得枠を超えるToolRequestは実行前に契約違反とし、
ProgramがIDを切捨てない。上限、step境界、またはCycle時間境界では`budget_stop_reason`を保存し、
予算到達前にCycle終了用のSolver判断を実行する。

Stepの`planned`からの再開は未完了Toolだけを実行し、`observed`からの再開はToolを再実行せず
Solver評価へ進む。成功済みToolRequestとExpansionSliceは同じID・scopeで再実行しない。
これらはインメモリ状態更新であり、DB transactionとは呼ばない。

### 8.2 将来の永続化

プロセス再起動をまたぐ再開要求が発生した場合だけ、SQLiteまたはPostgreSQL Adapterを追加する。
その時点で実際の同時実行要件を確認し、revision、optimistic locking、migrationを設計する。
永続化Adapterは上記Step checkpointの原子性、Node・Link・request IDの一意性、`observed`からの
非再実行を同じCaseStore contract testで満たす。

現時点で将来DBを推測してRepository、Unit of Work、leaseを先行実装しない。
`CaseStore`をAgentLoopから分離しておくことだけを、切替容易性の初期保証とする。

## 9. ログとtrace

EventJournalやDB監査ログは導入しない。運用ログとAPI traceを同じ実行計測から生成する。

記録する項目は次に限定する。

- `request_id / case_id`
- cycle番号・phase・goal・strategy・focus Hypothesis件数
- step番号・phase・ToolRequest件数
- frontier件数、探索Node/Link件数、最小/最大depth、`partial` expansion件数
- 呼び出し用途: `research / integration / review`
- provider、model、Profile名・version
- `sourceSnapshotId / graphSchemaVersion / classificationRunId`と分類coverage件数
- logical call数、transport attempt数
- input/output token
- latency
- Tool名、件数、status、elapsed
- 機械的な`error_code / stop_reason`
- Reviewerが有効だったか

既定ログへ次を出さない。

- API key、credential
- 利用者質問の全文
- system prompt全文
- LLM生応答
- 法令本文、Evidence本文
- 内部例外文字列をそのまま返したAPI error

内容が必要なデバッグは明示的な開発設定に限定する。ログ出力失敗によって回答処理を失敗させない。

## 10. ディレクトリ構成

```text
agent-api/app/
├── agent_framework/                 # 検索対象に依存しない再利用基盤
│   ├── contracts.py                 # discriminator付きSolverCommand / CaseUpdate / ImpactDecision
│   ├── state_contracts.py           # 説明付きstatus契約・対象別の小さい遷移表・契約version
│   ├── state.py                     # 型付きCaseState / WorkItem / Hypothesis / Evidence / CycleRecord / StepRecord
│   ├── transitions.py               # Command適用・複数record間の構造条件・直接status更新の唯一入口
│   ├── exploration.py               # Node / Link / frontier / expansionの汎用構造
│   ├── loop.py                      # Cycle内step反復・最大4 cycle・予算終了・Reviewer分岐
│   ├── context.py                   # WorkTree・探索frontier・focus・Evidenceの機械的表示
│   ├── validation.py                # 既知ID・権限・上限等の構造検証。状態遷移規則を重複定義しない
│   ├── contract_rendering.py        # Provider schema基礎・LLM-visible status用語集の決定的生成
│   ├── profiles.py                  # Profile読込みと用途別model解決
│   ├── store.py                     # 小さいCaseStore Protocol
│   ├── observability.py             # 構造化ログとtrace計測
│   └── ports/
│       ├── model.py                 # ModelPort
│       └── tool.py                  # ToolPort / ToolDefinition
│
├── domains/
│   └── legal/                       # 法令業務ドメイン
│       ├── tools.py                 # 法令Tool登録
│       ├── evidence.py              # 法令Evidenceへの変換・表示
│       ├── graph_schema.py          # predicate・方向・selector・RelationAssertion契約
│       ├── relation_classification.py # 非同期分類の入出力・検証・Run publish
│       ├── profiles/
│       │   └── default.yaml
│       └── prompts/
│           ├── solver_common.md
│           ├── solver_research.md
│           ├── solver_integration.md
│           ├── solver_graph_review.md
│           ├── relation_classifier.md
│           ├── relation_grounder.md
│           └── reviewer.md
│
└── adapters/
    ├── models/
    │   ├── anthropic.py
    │   └── ollama.py
    ├── tools/
    │   └── legal_search.py           # OpenSearch / Neo4j / 本文取得
    └── persistence/
        └── in_memory.py

scripts/
└── classify_legal_relations.py      # 再開可能な非同期分類jobのCLI入口
```

法令名、条文、ガイド、`IMPLEMENTS`、`REFERENCES`等を`agent_framework`へ置かない。
OpenSearch、Neo4j、Anthropic等のSDKも`agent_framework`から直接importしない。

## 11. 現行コードからの切替

現行コードの3系統を次のように扱う。

| 系統 | 主な場所 | 扱い |
|---|---|---|
| 旧回答経路 | `research_case_store.py`, `llm_research_loop.py`, `llm_directed_research.py` | 切替完了まで比較対象として維持し、Phase 4合格後に削除する |
| 新Framework経路 | `agent_framework/`, `domains/legal/`, `framework_agent.py`, `adapters/persistence/simple_in_memory.py` | 本計画に沿って完成させる移行先 |
| 別CaseStore試作 | `agent_core/`, `adapters/persistence/in_memory.py` | `llm.py`等から参照中だが移行先には含めない。参照元を解消してから削除する |

10章の目標ディレクトリに`agent_core/`を載せないのは見落としではなく、EventJournal、Repository、
transaction等を初期Frameworkへ持ち込まないためである。`framework_agent.py`は現在も
`adapters/persistence/simple_in_memory.py`を使い、`agent_core/`を直接使用しない。

- 新経路はFeature Flagで有効化し、Phase 4の合格前に既定経路へしない。
- 既存OpenSearch・Neo4j・本文取得はLegal Tool Adapterで利用し、法令固有ロジックをFrameworkへコピーしない。
- 現行の自動Graph、旧Graph relation・方向・status、二重encodeされたSolver出力を互換契約として新設計へ持ち込まない。
- 新経路が合格した後、参照を確認したうえで旧回答経路と`agent_core/`試作を別変更で削除する。
- 切替中も同じ入力snapshotからOpenSearchとNeo4jを再構築し、旧Graphと新Graphを同一Caseへ混在させない。

Context Projectorは、全WorkTree案内、現Cycle、直前Step、Graph差分batch、評価済みfrontier ledger、
focusへ接続するNode・Link、直近ToolResult、新規・保持EvidenceをCaseStoreから決定的に投影する。
全Graph履歴をPromptへ重複表示せず、関連性・優先度・再採用はSolverに判断させる。

## 12. 実装Phase

### Phase 0: 契約とbaseline

本計画の「代表2問」は、`agent-ui/example_questions.py`の次の完全一致質問に固定する。
採点用のrequired evidenceとanswer pointsはSolverへ渡さず、同ファイルの定義を評価時だけ使用する。
現行fixtureには独立したquestion IDがないため、`EXAMPLE_TITLES`へ渡す完全一致titleを評価キーとする。

| `EXAMPLE_TITLES`の完全一致値 | Agent APIへ渡す質問文 |
|---|---|
| `株券を買い集める場合の公開買付け` | 上場会社の株式を、市場を通さずに多数の株主から買い集めたいのですが、公開買付けの手続が必要になるのはどのような場合ですか。対象となる株券等の範囲、主な例外、必要な手続も含めて、根拠となる条文とともに説明してください。 |
| `役職員への譲渡制限付株式の交付` | 上場会社が自社や子会社の役職員へ、譲渡を一定期間制限した自社株式を報酬として交付する場合、有価証券の募集・売出しの届出は必要ですか。届出が不要となり得る条件、対象にできる人の範囲、譲渡制限をいつまで課す必要があるかも含めて、根拠となる条文とともに説明してください。 |

初期baselineでは同ファイルの`legal_as_of=2026-07-26`と採点定義を固定する。質問文、必要根拠、回答要点、
法令時点のいずれかを変更する場合は、baselineを別revisionとして採り直し、本表と評価fixtureを同じ変更で更新する。

- 本計画を正本として確定する。
- 上記2問について、総時間、LLM呼び出し数、用途別latency、Tool時間をtraceから記録する。
- 評価データ、設定、model ID、code revisionを固定する。
- 新しいSolver、Reviewer、Tool、CaseStore契約のfixtureを作る。
- 現行のstatus、judgment、action、Command、定義箇所、決定主体、永続化有無、LLM表示有無を棚卸しし、
  同じ値・意味・変換が`state.py / contracts.py / context.py / validation.py / structured_json.py / Prompt`へ
  重複している一覧を作る。
- 対象ごとの説明付きstatus契約、discriminator付きCommand、遷移表、`contract_version`のfixtureを作る。
- Provider schemaと共通Prompt用語集を契約fixtureから生成し、手書きenum・手書き基本定義との二重管理を
  新Frameworkへ持ち込まない。
- Case→再帰WorkItem→Hypothesis→Exploration Node/Link/frontier→CycleRecord→StepRecord→ToolResult/Evidenceの参照fixtureを作る。
- 7.3の全LLM-visible statusが共通PromptまたはDomain Promptへ定義される契約テストを作る。
- Reviewerの既定値が`false`であることを設定契約へ固定する。
- Framework Profileの`max_exploration_depth`が`1`と`2`だけを受理し、Legal Profileは`1`に固定され、
  未設定、`0`、`3`以上、整数以外を拒否するfixtureを作る。
- Legal Profileの`max_material_evidence_chars`初期値を50,000文字へ固定し、本文枠とGraph review batch・ledgerが
  別に計上される契約fixtureを作る。
- `max_solver_input_chars`初期値を240,000文字とし、本文上限より大きいことをProfile検証へ追加する。
- 現行Graph向け分類baselineとして`legal_relation_classifier_fixture.jsonl` 34件を固定する。
  法律→府令、施行令→府令、定義参照、複数参照箇所をタグ別集計し、goldはLLM入力へ渡さない。
- 外部ガイド6文書を`guidance_navigation_fixture.jsonl`へ固定し、OpenSearch検索、明示`EXPLAINS`集合、
  遷移先Article全文取得を検査する。ガイド本文だけで法令関係を作る評価にはしない。

完了条件:

- 現行baselineからCycle、Step、本文取得、Graph Review、LLM呼出し、latencyの比較可能な計測値を保存する。
- 新契約で意味判断と機械的検証の境界をテストとして記述できる。
- statusまたはCommandをfixtureへ追加したとき、Provider schema、Prompt用語集、遷移網羅性のいずれかが
  未定義なら契約テストが失敗する。
- Relation分類fixtureとガイドfixtureをNeo4jへ書き込まず再実行でき、model、prompt version、
  1候補・5 predicateのWorker判定、Reviewer判定、差戻し回数、タグ別結果が記録される。
  機能fixtureを全候補の母集団精度とは表現しない。

### Phase 1: 最小Framework

- `agent_framework/state_contracts.py`を説明付きstatus契約の正本として実装し、
  `contracts.py`、`state.py`、`transitions.py`、`contract_rendering.py`、`loop.py`を接続する。
- status-bearing recordは型付きstatusを持ち、内部で生文字列を持ち回らない。LLM・JSON・永続化境界で
  PydanticがEnumとの相互変換を行い、内部処理はEnumメンバーを参照する。
- statusの直接代入を廃止し、対象ごとのCommandを`transitions.py`で適用して新しいrecordを作る。
  型付きstatusの読み取りは許可し、同じ意味を隠すだけの中間booleanを増やさない。
- `StartCycle / ContinueCycle / StartNextCycle / Finalize`をdiscriminator付きCommand unionにし、複数フィールドの
  組合せで次動作を表現しない。
- Provider schemaと共通PromptのLLM-visible status用語集を説明付き契約から生成する。
  Domain Promptには処理モード固有の使用規則だけを残す。
- `exploration.py`へNode、Link、frontier、ExpansionSlice、CycleRecord、StepRecordの汎用型を実装する。
- `CaseStore`と`InMemoryCaseStore`を実装する。
- Profile resolverを実装する。
- Case全体再生成ではなく、`CaseUpdate`の追加・更新差分を適用する。
- 全WorkTree案内、現Cycleのgoal・strategy、直前Step、frontier、Solver指定focus、直前の新規Evidence、保持Evidenceを組み立てる。
- Cycleの`planned → running → completed`と、各Stepの`planned → observed → completed`を保存し、
  Cycleの`completed`時だけcycle数を増やす。
- `max_research_cycles=4`、Cycle累計の`max_fetched_resources_per_cycle=4`、
  `max_tool_requests_per_step`を別の制約として実装する。自動Toolをstep・Cycle traceへ計上するが、
  本文取得数とは混同しない。
- Cycleの本文取得・step・時間境界前に新しいactionを止め、予約したSolver呼出しで
  観察済み結果の評価とCycle終了を行う。
- fake Modelとfake Toolで1 Cycle内の複数step、`start_cycle / continue_cycle / start_next_cycle / finalize`、
  Stepの`observed`からの再開をテストする。
- read-only ToolRequestの上限制御付き並列実行を実装する。
- 反証Hypothesisから影響WorkItemを列挙し、Solverの`retain / replace / drop`を適用する。
- Reviewer無効時にReviewer Modelが一度も呼ばれないことをテストする。
- Reviewer有効時の`accept / revise / review_failed`をテストする。
- 状態型ごとに`current status × Command`を網羅し、許可・拒否が未定義の組合せを失敗させる。
- `context.py`、`validation.py`、`loop.py`、Provider adapter、Promptに同じstatus変換表やenum一覧が
  残っていないことを契約テストで確認する。
- status recordのJSON保存・復元、未知値拒否、serialized value変更時の`contract_version`不一致と
  migration/旧値読替えをClaude APIなしでテストする。

Phase 1の主要な実装リスクは`contract_rendering.py`である。Enum、説明、owner、LLM可視性、遷移を持つ
正本から、Pydantic型、Provider schemaの基礎、日本語Prompt用語集、網羅性テストを矛盾なく派生させる
小さな生成系になるため、単なるテンプレート追加として見積もらない。最初にCycleとFrontierの2種類だけで
縦切りし、生成物のsnapshot test、未知値、全遷移組合せ、JSON round-tripを通してから他statusへ広げる。

このPhaseではClaude APIを使わない。

完了条件:

- Cycle 1で検索→起点本文→1ホップ→隣接本文を複数Stepとして継続し、必要根拠を探し切って`finalize`できる。
- Tool実行やStep完了だけではcycle数が増えず、`start_next_cycle`または`finalize`でCycleを閉じた時だけ増える。
- 1 Cycleの5件目の本文取得を実行前に拒否し、4件までの観察結果をSolverが評価して
  `finalize`または次Cycleへの引継ぎを返し、その後の`start_cycle`で次Cycleの計画を作る。
- 最大4 Cycleへ到達するfixtureと、1〜3 Cycleで早期`finalize`するfixtureがともに通る。
- Stepの`planned`では未完了Toolだけを実行し、`observed`では成功済みToolを再実行せず評価へ進む。
- 最後の許可StepのToolResultが次のSolver判断へ渡され、評価されずに残らない。
- 同じ方針を継続できるのに、Graph hopやTool終了だけを理由として`start_next_cycle`にしない。
- 同じResourceを複数Linkから発見してもNodeは1件で、Linkはすべて保持される。
- Graph navigationのArticle・Link投影がSolver向けの唯一表示となり、manifest・ToolResult・ID一覧に同じGraph Evidenceが重複しない。
- 循環Linkを保存しても、成功済みNode・scopeを再展開しない。
- 汎用fixtureの`max_exploration_depth=1 / 2`で各上限depthからGraph展開せず、
  Legal Profileの実行では深さ1本文を取得できるが深さ1からGraph展開しない。
- 次Cycleへ移っても同じ起点のdepthをリセットせず、別のOpenSearch結果だけを新しい深さ0起点にする。
- Solver Decisionに現れないfrontierが消えない。
- 50,000文字以内のEvidence本文が決定的な順序で提示され、過去・保持Evidenceの上限外本文はmanifestから
  再取得できる。新規取得Articleは全chunkを原子的な提示単位とし、途中だけを表示しない。
- Evidence本文が上限に達しても、今回のGraph review batchのArticle ID、見出し、起点、relation、depth、
  content statusと、過去の評価済みfrontier ledgerがSolverContextに残る。全候補・Link履歴はCaseStoreに残る。
- Solverはledgerの既知Nodeを新Hypothesisへ明示的に再採用できるが、Programが`rejected`を自動転用しない。
- 新規Graph候補が複数Review batchに分かれても未提示pageが消えず、評価済み詳細を
  次ReviewのPromptへ重複投影しない。
- `select`したfrontierがledgerに`selected`として残り、本文取得の`pending / succeeded / failed / timeout`と
  混同されない。取得成功済み候補は再選択できず、失敗・timeout時だけ既知IDで再試行できる。
- modelのcontext容量を超えるfixtureでGraph候補を黙って削らず`context_capacity_exceeded`になる。
- CaseUpdateに現れなかった別系統のWorkItemと未完了WorkItemが消えない。
- WorkItemの親子循環、未知basis ID、未知focus IDを拒否する。
- Hypothesis反証時に、プログラムが影響WorkItemを自動的にdropしない。
- 仮説だけが反証されたfixtureではWorkItemを維持し、新Hypothesisを追加できる。
- 不足観点のfixtureでは既存WorkItemをreplaceせず、子または兄弟WorkItemを追加できる。
- 問い自体が不適切なfixtureだけで旧部分木を閉じ、新しい部分木へreplaceできる。
- プログラムがHypothesisの意味statusを書き換えない。
- 通常の`finalize`時にopen WorkItemが残る契約違反を拒否する。上限時の限定回答だけ、limitationsと
  unresolved ID欄が全open WorkItem・対応unresolved Hypothesisを漏れなく参照する場合に保持を許す。
  Programは未確認事項の法的内容や、Graph候補が本当に不要かは判断しない。
- 未知ID、不正Tool、権限外Tool、上限超過だけを拒否する。
- Reviewerの既定値が無効である。
- 型付きstatusは他処理から読み取れるが、共通Command適用処理を迂回して直接変更できない。
- 説明付きstatus契約の変更がProvider schemaと共通Prompt用語集へ自動反映され、手修正を要求しない。

### Phase 2: 法令の薄い縦切り

Phase 2は、実装途中のschemaで全データを作り直さない。次の順序を固定する。
全件実行時の具体的な確認項目と停止条件は
[`relation_classification_rollout_checklist.md`](relation_classification_rollout_checklist.md)を使う。

1. 新Graph契約、型、Constraint、監査をfixtureへ実装する。この時点では既存の実データを更新しない。
2. `/admin/seed`を決定的処理だけにし、1つの入力manifestから同じ`sourceSnapshotId`を持つ
   OpenSearch本文とNeo4jの構造・原文Relationを再構築できるようにする。
3. Relation分類をseedから独立させ、候補JSONLの再開可能exportと、Luna判定JSONLを
   `ClassificationRun`へ取り込む再開可能importを実装する。常駐worker、queue、schedulerを必須にせず、
   HTTP seed処理内からLLMを呼ばない。
4. 構造監査済みfixture、既存14件、新規20件、代表100候補の順で、schema整合、Worker / Reviewer品質、
   差戻し率、未解消率、checkpoint、所要時間を確認する。全候補の所要時間を再見積りする前に
   全件Runを開始しない。旧34件fixtureはローカル実装の機能試験として残すが、5 predicateの受入判定に使わない。
5. 検証環境の回答処理を止め、同じmanifestからOpenSearchとNeo4jを一度だけ再構築する。
   現行の破壊的seedを実行中に検索可能な状態とは扱わず、途中失敗時は不一致snapshotを公開しない。
6. 新snapshotを対象に候補をexportし、Codex LunaのWorker / Reviewerペアで全件分類する。検証importで
   `building` RunへcheckpointとAssertionを保存し、全件監査に成功したRunだけを`published`へ遷移させる。
   Caseは開始時にその`classificationRunId`を固定する。
7. publish済みRunを新しいLegal Tool Adapterへ接続し、代表2問を検証する。非同期分類完了前でも
   OpenSearchと原文`REFERENCES / EXPLAINS`は利用できるが、`semantic_assertion`はpublish済みRunが
   ある場合だけ利用する。

OpenSearchとNeo4jの決定的seedは同じsnapshotを公開する1つの移行単位、非同期Relation分類はその後に
publishする別単位とする。Graph schema、抽出規則、入力データの変更時にNeo4jだけを再seedしたり、
旧snapshotのClassificationRunを新snapshotへ流用したりしない。初期検証環境ではversioned indexや
常駐workerを先に導入せず、メンテナンス中の再構築と再開可能CLIで整合を保つ。

- Legal Domain Packと法令Promptを実装する。
- 既存OpenSearch、Neo4j、本文取得をLegal Tool Adapterとして接続する。
- `fetch_articles`はArticleごとの件数上限で打ち切らず、OpenSearchの総件数を確認して安定順に全pageを取得する。
  内部page sizeとArticle取得上限を分離し、全件取得後だけToolResult・contentを`succeeded`にする。
  途中失敗・timeout・0件取得では部分Evidenceをcommitせず、Article単位で再試行可能にする。
- 5.1.3のNeo4j物理定義を実装する。`:GraphNode`に`Document`、`Article`、`Paragraph`、`Item`、
  `RelationAssertion`、`ClassificationRun`、`ClassificationCheckpoint`の型別labelを付け、物理Relationは`HAS_CONTENT_UNIT`、
  `REFERENCES`、`EXPLAINS`、RelationAssertion用`SUBJECT / OBJECT / CLASSIFIED_IN`だけを生成する。
  `IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`は`proposedPredicate`に保存し、
  `APPLIED_BY / MENTIONS`を生成しない。項・号をArticleへ置換せず、
  Article単位の探索投影にも正確なContent Unit IDを残す。
- `/admin/seed`はOpenSearch本文、構造、明示`REFERENCES / EXPLAINS`までを決定的に作り、LLM分類を待たず終了する。
  端点ペア・basis・全参照箇所を持つ候補1件ずつをexportし、判定結果を取り込む再開可能な非同期jobを実装する。
  候補内に閉じた両Article全文と`<articleId>::span-N`を提示し、旧`suggestedType / referenceKind`を
  正解ヒントとしてPromptへ出さない。Luna Workerは5 predicateを同時に比較し、predicate固有の二必要条件、
  finding、成立時の方向と両端根拠spanを返す。Luna ReviewerはWorker回答を見て具体的に指摘し、
  同じWorkerへの差戻しは1回だけ許す。ProgramはfindingからoutcomeとAssertionを決定的に投影する。既知decision key・
  predicate enum・端点・根拠span・snapshot・hash・件数だけを構造検証する。Programは分類結果を補正しない。
- 分類結果を`ClassificationRun`へ集計し、完了Runだけ一括publishする。RelationAssertionを
  `SUBJECT / OBJECT / CLASSIFIED_IN`各1本で接続し、`basisEdgeId / supportingSpans / classificationRunId`を
  保存する。`candidateKey`と`assertionDedupeKey`を決定的に生成し、後者の一意制約で同一Run・候補・predicateの
  二重登録を防ぐ。再開時に同じkeyと同じpayloadがあれば処理済みとして再利用し、同じkeyでpayloadが違えば
  Runを失敗させて上書きしない。旧`fromArticleId / toArticleId / suggestedType / status`を新schemaの正本にしない。
- 分類LLMは各候補の5 predicateを一度に比較し、保存checkpointは候補単位にする。
  Codexオペレーター実行では最大5候補を1 sessionへ割り当て、候補別recordを独立生成する。
  Worker / Reviewerは別sessionとし、同時実行は最大3 session、差戻しは同じWorkerへ1回だけとする。
  Reviewer差戻しは1候補につき最大1回とし、再差戻しは`unresolved`へ分離する。
- Case開始時に`sourceSnapshotId / graphSchemaVersion / classificationRunId`を固定し、検索時案件判断は
  CaseStoreだけへ保存する。分類jobと検索時Solverの責務を混同しない。
- OpenSearch・Graphの各ToolRequestを既知の`ExplorationIntent`へ結び付け、Solverが明示したHypothesis由来の
  query・filter、または起点・mode・1 predicateまたは原文relation・1 direction・構造filterだけをbackendへ渡す。
  現行Profileの固定`[REFERENCES, IMPLEMENTS, APPLIED_BY]`による無条件Graph取得は廃止する。
- Legal Tool Adapterは自由Cypherを受け付けず、modeとdirection別の固定parameterized Cypherを使う。
  materialize前の候補件数が安全上限を超えた場合は上位N件へ切り捨てず、`scope_too_broad`とfacet件数を返す。
- Graph方向の外部契約を`from_subject / to_subject`へ統一する。Tool AdapterはNeo4jのfrom/toと検索起点から
  directionを決定し、旧称をPrompt、Provider schema、ToolResult、CaseStoreの新規データへ出さない。
- `APPLIED_BY / MENTIONS`をLegal ontology、seed、Neo4j、Graph Tool allowlist、Promptから削除する。
  現行`legal_ontology.py`で`implemented=False`の`DEFINES / USES_TERM / EXCEPTION_TO`も物理Relationの
  積み残しとして実装せず、旧registryから削除する。定義利用と例外の意味候補は、それぞれ
  `RelationAssertion.proposedPredicate`の`USES_DEFINITION / EXCEPTION_TO`で表す。
  旧`referenceKind`を意味selectorから外し、原文`REFERENCES`には引用箇所と抽出来歴を保存する。
  schema versionを更新し、同じ入力snapshotからOpenSearchとNeo4jを両方再構築する。
  `EXPLAINS`以外の単なるガイド言及をGraph関係へ変換しない。
  実装時は`graph_edge_construction.md`、edge registry、Graph監査、seedテストを同じ変更単位で更新する。
- OpenSearch候補とGraph候補をLegal Resource Node・DiscoveryLink・frontierへ投影する。
- 同じDecisionで本文取得とrelation Intentが明示された場合は1ホップGraphを同じStepの観察へ入れる。
  本文から必要性が判明した場合は次Stepのrelation Intentとして実行する。隣接本文取得は現Cycleの
  残り本文取得枠内でSolverが`select`した対象に限定し、枠外の関連候補は`defer`して次Cycleへ残す。
- Profileの`max_exploration_depth`に達したArticleでは本文だけを取得し、relation IntentがあってもGraphを抑止する。
- Graphのmode・predicateまたは原文relation・direction・classification run・構造filter・cursor・policy versionを
  ExpansionSliceのscopeへ対応付ける。
- Graphの全Article・Link・Review履歴をCaseStoreに保持し、SolverContextへは
  `graph_review_batch`と`graph_review_ledger`を差分投影する。
- Graph Reviewは1回最大3件のArticle本文を選び、Cycleの残り本文取得枠を超えないようにする。
- 7.4の通り`solver_common.md`、`solver_graph_review.md`、`solver_integration.md`、Provider schemaを
  型・Profile version・契約テストと同時に更新する。
- `/answer`に新経路のFeature Flagを追加する。
- Solverのresearch/integrationで別modelを設定できるようにする。
- 法令固有型や法令関係判断がFrameworkへ漏れていないことを確認する。

完了条件:

- fake Modelを使ったAPI統合テストが通る。
- Tool結果の取得状態はプログラム、法的関連性と根拠採否はSolverが決める。
- seed後に`Document / Article / Paragraph / Item / RelationAssertion / ClassificationRun`のlabel・端点型・一意制約が
  5.1.3と一致し、項・号NodeがArticle labelを持たないことを確認する。
- publish済み全RelationAssertionが`SUBJECT / OBJECT / CLASSIFIED_IN`を各1本持ち、既知Articleと
  ClassificationRunへ接続し、5種の`proposedPredicate`、非nullな`basisEdgeId / supportingSpans`の整合が取れる。
  旧`status`を正本にせず、未確認候補を正式な物理意味Relationへ自動昇格させない。
- 同じ分類候補を中断・再開で2回保存するfixtureで、`assertionId`が異なっても
  `classificationRunId + candidateKey + proposedPredicate`の重複が一意制約とpublish監査で検出される。
  同一候補から異なるpredicateが返る場合と、別Runで再分類する場合は別Assertionとして保存できる。
- 同一source content unitに複数参照先と異なる意味があるfixtureで、非同期LLMへ全端点と引用箇所が渡り、
  候補がtargetごとの別LLM入力になり、Programが全参照先へ同じpredicateを複製しない。
  同一候補内で同じ参照文字列が複数回現れるfixtureは全出現spanを提示し、最初の一致だけに固定しない。
  分類再開・cache・Run単位publishを確認する。
- 5候補shardの順序入替え、単件実行との比較、別候補の追加を行うfixtureで、候補別の5 predicate、方向、
  groundingが変わらず、Article ID付きspanが候補間で衝突せず、候補単位checkpointから再開できる。
- Workerの誤りをReviewerが指摘するfixtureで、同じWorkerが指摘を参照して1回だけ全5 predicateを再確認し、
  同じReviewerが差分を確認する。2回目も不合格なら再実行せず`unresolved`になる。
- Provider schemaで各predicateが固有名の二必要条件とfindingを持ち、条件とfindingの不整合を拒否する。
  非成立・不確実時は方向・根拠を要求せず、成立predicateだけ根拠付与schemaへ渡ることを確認する。
  Programの決定的投影が`CLASSIFIED / REFERENCE_ONLY / UNCERTAIN`の3経路で本文の意味を補完しないことを確認する。
- 現行34件の二値fixtureは旧`IMPLEMENTS`候補の移行baselineとして扱う。新schemaの受入れでは
  5 predicateそれぞれの正例・負例、`REFERENCE_ONLY`、`UNCERTAIN`、法令・政令・府省令・ガイドを含むfixtureを
  追加する。複数候補batchを有効化する場合は、候補順の入替えと単件実行の双方で判断が変わらないことも確認する。
- Caseが固定した`classificationRunId`以外のAssertionをTool Adapterが返さず、Run coverageに
  uncertain/failedがあるとき不在を関係不存在として扱わないPrompt契約を確認する。
- 同一seed manifestのOpenSearchとNeo4jでDocument・Article ID、`sourceSnapshotId`、取得可能なsource revision、
  content hashが対応し、
  Graph schema変更時に両方が再構築される。Neo4jだけを更新した不一致状態を成功扱いしない。
- 1 pageを超えるArticle fixtureで全登録済みchunkがEvidenceとして保存され、最終pageまで取得後だけ
  `succeeded`になる。後続page失敗・timeout・0件fixtureは`succeeded`にならず、部分Evidenceが残らない。
- 新規取得Articleの全Evidence chunkが次のSolver判断へ一度提示され、ProjectorがArticle途中を切って
  全文提示済みに見せない。全文がmodel contextへ収まらないfixtureは`context_capacity_exceeded`になる。
- 5つの意味predicateと原文`REFERENCES / EXPLAINS`の意味をFrameworkが判断しない。
- 同じ起点Articleに複数predicateがあるfixtureで、Solverが指定したmode・1 predicate・1 direction・構造filter以外を
  Tool Adapterが返さず、Programが未指定predicateを追加しない。
- directionの契約テストで`from_subject / to_subject`だけが入出力可能で、旧称がPrompt・schema・ToolResultへ
  現れないことを確認する。Neo4jのfrom/toは変更せず、起点がfromなら`from_subject`、toなら`to_subject`になる。
- seed後のGraph inventory、Legal Tool allowlist、Prompt、Provider schemaに`APPLIED_BY / MENTIONS`が存在せず、
  明示的なガイド・条文対応の`EXPLAINS`は維持されることを確認する。
- 公開買付けfixtureで、`EXCEPTION_TO/to_subject`により金商法27条の2から施行令7条、
  `IMPLEMENTS/from_subject`により施行令7条から府令2条の5、金商法27条の3から府令10条を候補取得できる。
  Graph候補だけを根拠にせず、取得した両端本文をSolverが評価する。
- raw `REFERENCES/to_subject`の高fan-in fixtureは通常QA selectorとして拒否または`scope_too_broad`になり、
  候補を任意の上位N件へ切り捨てない。
- predicateを選べないfixtureでは、Solverが全Graph意味関係を要求せず、Hypothesisに沿った`legal_search`で
  新しい根拠または起点を発見できる。
- 別Hypothesisが同じGraph scopeを指定したfixtureでNeo4jを再実行せず、既存Linkから新しい
  `Node × Hypothesis` frontierを作る。
- 同じArticleへ複数経路があるfixtureで本文取得は1回、DiscoveryLinkは複数残る。
- A→B→Aの循環fixtureで再帰展開が停止する。
- `max_exploration_depth=1`で、1ホップ候補の本文取得と、その候補からのGraph非実行を確認する。
- 最大depthまたはGraph関係欠落のfixtureで、Solverがopen WorkItemに基づく`legal_search`を選び、
  結果を新しい深さ0起点として同じCaseで探索できる。
- 関係するとSolverが判断した未確認frontierが残る場合、通常の`finalize`を選ばないPrompt契約を確認する。
- Evidence本文が省略されても、当該Graph review batchにはArticle ID、法令名、条番号・見出し、
  content status、depth、起点Article・Link、mode・predicateまたは原文relation・direction・
  basisEdgeId・supportingSpans・classificationRunId・sourceKind、
  `partial / complete`が残り、
  ledgerの`relevant_deferred` frontierも既知IDとして選べる。全履歴はCaseStoreで監査できる。
- 100件以上のGraph候補fixtureで、Review入力が累積全件ではなくProfileのpage上限内に収まり、
  全pageが最終的に`selected / relevant_deferred / rejected`のいずれかになる。
- 既評価frontierへ新Linkが追加されたfixtureでは、その候補と今回までの関係情報だけが差分batchへ戻り、
  Solverの再判断後も過去DecisionとLink履歴がCaseStoreに残る。
- 同一候補Articleを複数起点から発見するfixtureで、Articleは1件、Linkは全経路分残り、
  Graph Evidence IDがmanifest、ToolResult、navigation・omitted ID一覧に現れないことを確認する。
- Legal PromptがGraph mode、RelationAssertionの5つの`proposedPredicate`、basis、classification coverage、
  sourceKind、directionを
  7.3どおり定義する。

### Phase 3: ログと性能

- logical model callとtransport retryを分けて計測する。
- 非同期Relation分類は候補ごとのmodel latency、一次`UNCERTAIN`率、再分類回数、checkpoint保存時間、
  Worker / Reviewer別の判定時間、差戻し率、未解消率、再開skip件数を記録する。最初に代表100件を
  5候補/session・最大3 active sessionのWorker / Reviewer判定で測り、全候補の所要時間を再見積りしてから
  全Runを開始する。速度のためにProgramがpredicateを補完したり、未評価候補を処理済みにしない。
- cycle phase・goal・strategy、step番号・phase、frontier、探索Node/Link/depth、model用途、Tool時間を構造化ログとAPI traceへ出す。
- 秘匿情報が通常ログへ出ないことをテストする。
- Prompt入力と構造化出力を必要最小限へ縮小する。
- 直列Tool実行が残っていないことを計測する。

完了条件:

- Reviewer無効の単純問題では、LLM呼び出しが原則2〜3回以内である。
- 公開買付けのような多段探索は、1 Cycleの本文取得を4件以内に抑え、
  通常1〜2 Cycle、必要な場合のみ最大4 Cycleで完了する。
- 最大経路でもRun全体のaction stepは`max_total_steps`、Solver判断は原則`max_total_steps + 1`を超えない。
- 各LLM・Tool実行前にCycle終了、残りCycle、最終回答の予約を確保し、予算不足の中間呼出しを
  開始せずToolResultをCycle終了判断へ渡す。
- 予算由来の中間LLM timeoutをRun全体の`provider_error`にせず、`cycle_step_timeout`として
  Cycle終了判断へ進める。
- provider障害を意味上の`unresolved`へ変換しない。
- 非同期分類を中断・再開しても、同じcandidate hash・model・prompt versionの完了候補を再呼出しせず、
  未完了候補だけを続行する。保存batchを大きくして判断済み候補を失わず、LLM入力batchを増やして
  候補間干渉を起こさない。
- 大きな1ホップでも、Graph Reviewの入力は差分batch上限内に収まり、既評価候補の詳細を
  毎回重複入力しない。未評価pageと`relevant_deferred` frontierは消えない。

### Phase 4: 実モデル評価と切替

- 最初にReviewer無効、research/integrationともOllama `gemma4:e4b`で、Phase 0に固定した自然言語2問を
  1回ずつ実行する。実装、契約、Prompt、Tool選択、検索、Cycle引継ぎ、根拠利用の動作確認を目的とし、
  失敗を直ちにモデル性能の問題としない。
- 必要根拠、回答要点、総時間、LLM呼び出し数、Tool時間をbaselineと比較する。
- 上記2問の動作確認合格後、必要なら対象環境で使用する別Profileの品質・性能比較を行う。
  登録済み自然言語12問を各3回、同一設定・逐次実行して36件のRun全体latencyを集め、
  外部provider障害を除くRunのp90を測る。Frameworkの予算timeout、protocol error、限定回答は除外せず
  性能・品質失敗として数える。外部provider障害も件数と理由を別記する。
- 必要ならReviewer有効を別試験として1回だけ実行し、品質差と時間差を測る。
- 合格後に新経路をデフォルトへ切り替える。
- `llm.py`と`adapters/persistence/in_memory.py`等の参照を解消した後、`agent_core/`試作と旧回答経路を
  それぞれ別変更で削除する。

合格条件:

- Phase 0に固定した2問で必要根拠へ到達する。
- プログラムによる法的意味判断がない。
- Reviewer無効がデフォルトである。
- 通常問題のp90を120秒以内とする。外部provider障害は別集計する。
- baselineより回答品質を落とさず、LLM呼び出し数を大幅に削減する。

`max_wall_time_sec=180`は異常な長時間実行を止め、Cycle終了と限定回答の時間を残す安全上限であり、
p90 120秒は通常問題の性能目標である。安全上限以内なら性能合格という意味ではない。p90を超えた場合は、
用途別の入力・出力token、重複投影、LLM呼出し数、Tool並列性をtraceで分解する。Evidence本文枠や
`max_output_tokens`を調整する場合も、新規Article全文、Graph差分候補、必要根拠を欠落させない契約テストと
上記2問の品質評価を再実行する。安全上限だけを延長して合格扱いにせず、120秒を変更する場合は実測根拠と
品質・latencyの比較結果を本Phaseの評価成果物へ残す。

## 13. 将来拡張

次は初期切替の合格条件へ含めない。

### 永続化

再起動を越えた案件再開が必要になった場合、CaseStore contract testを作ってから
SQLiteまたはPostgreSQL Adapterを1種類ずつ追加する。

### サブエージェント

単一Solverと並列ToolRequestで不足することが計測された場合だけ検討する。
追加する場合も、独立したread-heavy作業に限定し、結果はEvidence IDでSolverへ戻す。

### provider横断

同一Run内で複数providerを使う要求が生じた場合だけ、credential、capability、障害分離を追加設計する。

## 14. 禁止事項

- statusの値・基本定義・決定主体を、説明付きstatus契約とProvider schema、Promptへ別々に手書きする。
- アプリケーション内部でstatusを生文字列として比較する、またはCommand適用処理を迂回して直接代入する。
- 対象ごとの小さい遷移規則を、`context.py`、`validation.py`、`loop.py`等へ重複実装する。
- 型付きstatusの読み取りを一律に隠すためだけに、同義の中間booleanや第二のstatusを追加する。
- プログラムが法的関連性、十分性、重要度を語句やscoreで決めない。
- LLMの`finalize`をプログラムが意味上の理由で撤回しない。
- SolverにCaseState全体を毎回再生成させ、出力漏れを削除として扱わない。
- 全WorkTree案内から未完了WorkItemを黙って省略しない。
- Tool実行完了を、Hypothesis評価まで閉じたCycle完了として数えない。
- Tool実行回、LLM呼び出し回、Graphの1ホップをCycle境界として扱わない。
- プログラムが本文取得・step・時間の機械的境界を超えてactionを続けたり、境界で
  現Cycleを閉じる判断を飛ばして次Cycleを自動開始したりしない。
- Graph候補を再帰関数で連続展開し、Solver評価を挟まず隣接本文を次々に取得しない。
- 複数発見経路を持つ探索Graphの正本を、単一親しか持てない木へ変換しない。
- Graph候補をArticle・Linkへ正規化する際に、resource ID、depth、起点Link、取得・展開statusを隠さない。
- LLM-visible statusを、意味と決定主体を持つ説明付き契約と、そこから生成されるPrompt用語集なしに追加・変更しない。
- Hypothesis反証を理由に、プログラムが子WorkItemを自動的に維持・置換・破棄しない。
- WorkItemの問いやHypothesisのstatementを別の意味へ上書きしない。
- 構造化出力不正を`unresolved`や`revise`へ読み替えない。
- Reviewer無効時に暗黙にReviewerを呼ばない。
- Reviewerの指摘からプログラムが検索queryを生成しない。
- model ID、Reviewer有効値、DB backendをAgentLoopへハードコードしない。
- 法令固有Promptや型を`agent_framework`へ置かない。
- 未取得本文や未確認Graph関係を最終根拠として自動採用しない。
- Prompt全文、LLM生応答、法令本文、credentialを通常ログへ出さない。
- 最大回数に達したことを、根拠不足という意味判断へ変換しない。
