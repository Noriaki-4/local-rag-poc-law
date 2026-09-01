# Implementation Guidelines

## 目的

この文書は、このリポジトリで実装を変更するときの共通原則を定めます。詳細仕様は
`docs/generic_iterative_agent_framework_plan.md`、操作手順は`RUNBOOK.md`を正本とします。

## 基本原則

- 既存の責務、命名、ディレクトリ構成を優先します。
- 変更範囲は要求を満たす最小限にします。
- KISS、DRY、YAGNIを基本姿勢とします。
- ユーザーの未コミット変更と無関係な箇所を変更しません。
- 実装、モデル品質の評価、Promptチューニングを別工程として扱います。
- 不具合の現象だけを局所的に修正せず、原因となった不変条件とデータの流れを確認します。
- 同じ原因が別のWorkItem、Cycle、Provider又はTool経路でも発生しないか確認し、共通原因を必要最小限の範囲で修正します。

## 判断の分担

| 担当 | 判断すること |
|---|---|
| LLM | 質問分解、仮説、検索語、候補の意味的関連性、根拠評価、統合、完了判断 |
| Program | ID、型、件数、既知参照、重複、上限、状態遷移、保存、Tool実行 |

Programへ法的意味を推測するルールを追加しません。LLMの誤りを文字列一致や法令固有の条件分岐で補正せず、
Prompt、入力情報、Tool、データのどこに不足があるかを切り分けます。

## 契約とPrompt

### JSON契約の作法

- LLMが取得・返却するJSON項目の形状と基本的な意味は、Pydantic型と`Field.description`を正本にします。
- Solver、Cycle、Graph、検証で使う正規契約と状態遷移はProvider間で共通にします。
- Provider APIの制約に合わせたwire schemaはadapter内だけで変換し、共通の正規契約へ復元してから検証します。
- Provider固有の項目や分岐をAgent Loop、CaseStore、Domainの状態へ持ち込みません。
- `Field.description`には、値の意味、IDの種類、配列の要素、`null`や空配列の意味を、その項目を理解できる範囲で書きます。
- 用途別のLLM入力には、必要な項目だけを持つread modelを用意します。
- 入力契約とProvider出力schemaは、Pydantic型と`Field.description`から決定的に生成します。
- 実行時JSONは値を保持し、その項目説明は生成した`input_contract`またはProvider schemaでLLMへ伝えます。
- 同じ項目定義をDomain Promptや別の手書きschemaへ複製しません。項目の意味を変える場合は、先に正本の`Field.description`を変更します。

### Promptの作法

- Domain Promptには、その処理の目的、出力、完了条件、手順、判断ルールだけを書きます。
- Promptで使う名前は実際のJSONパスと対応させます。独自の別名を使う場合は対応を明示します。
- Toolの名前、用途、入力Schema、戻り値は`ToolDefinition`を正本にします。
- Promptを変更したら、Profile version、契約テスト、関連成果物を同じ変更で更新します。
- レビュー時は分割Promptだけでなく、結合後の指示、実行時入力、出力schemaを一組で確認します。

## 状態と永続化

- CaseStoreを案件状態の正本とし、LLMの会話履歴を正本にしません。
- 状態値と遷移は型付き契約と一元化した検証処理を通します。
- Projectorは正本から用途別の入力を決定的に作り、意味選別を行いません。
- 再計算できる投影値を第二の正本として保存しません。
- 永続化先を替えてもドメイン処理が変わらないよう、RepositoryまたはPortの境界を保ちます。

## 検索とデータ

- e-Gov XMLまたは保存済みdatasetを法令構造の正本とし、OpenSearchとNeo4jは再生成可能な索引とみなします。
- OpenSearchはArticle発見と検索抜粋、本文取得はgrounding Evidence、Neo4jは関係探索として区別します。
- 検索結果、本文、Graph候補を同じEvidence役割として扱いません。
- Article、Paragraph、Item、本則、附則、枝番を平坦化によって取り違えないようにします。
- index schemaやseed処理を変更した場合は、両索引の再構築要否とsnapshot整合性を確認します。

### WorkItem内の処理順

`LR-045`で解決した取りこぼしを再発させないため、同じWorkItemでは次の順に処理します。

1. すでに本文取得を決めたArticleがあれば、本文を取得します。
2. 取得した本文が未評価なら、Hypothesisと照合して結果を保存します。
3. 検索又はGraphで発見した候補が未評価なら、本文を取得するか判断します。
4. ここまでの情報で確認を進められない場合だけ、新しい検索又はGraph探索を行います。

つまり、選択済みArticleや未評価候補を残したまま、同じWorkItemで新しい探索を始めません。
ProgramはIDと処理状態から次に行う段階を決めます。候補の関連性、本文取得の要否、本文と
Hypothesisの対応はLLMが判断します。別のWorkItemは、この順序をそれぞれ独立して進められます。

### WorkItemの分離と並列処理

- 意味評価の入力は1つのWorkItemに限定し、別のWorkItemのHypothesisや本文を混ぜません。
- 異なるWorkItemは並列処理できますが、複数WorkItemのTool要求や意味評価を1つのLLM処理へまとめません。各結果は所属WorkItemを保ってCaseStoreへ適用します。
- WorkItem別結果は結合前に個別検証し、正常な結果を保持したまま、契約違反になったWorkItemだけを再実行します。
- 既知本文を再提示する場合も、必要とするWorkItemだけへ渡します。
- 新しいTool結果がない同一Hypothesisと本文の組合せを、通常の探索中に繰り返し評価しません。

この分離は`LR-016`、`LR-033`、`LR-036`、`LR-039`及び`LR-043`の再発防止条件です。

### Hypothesisの更新

- 同じ法的命題を修正するときはHypothesisの現在版を更新し、旧版は履歴に残します。通常のLLM入力には現在版だけを渡します。
- Evidenceとの対応は差分追加し、既存の対応を失わないようにします。
- 未確認事項は追加と解消の差分で更新し、配列全体の再生成によって既存項目を失わないようにします。
- Hypothesisの判断と未確認事項は独立して扱います。判断済みでも未確認事項が残る場合は、探索対象又は限定回答として保持します。
- Hypothesisと未確認事項を更新してから、その結果を使って依存関係とWorkItemの状態を導出します。

この更新規則は`LR-022`、`LR-024`、`LR-025`、`LR-029`及び`LR-038`に基づきます。

### 処理段階の責務

- Evidence Integrationは取得本文を評価し、Hypothesisと依存関係の差分を返します。
- Cycle Closeは評価済み状態を次Cycleへ引き継ぐことだけを扱います。
- Finalizationだけが最終回答を生成します。探索、本文評価又はCycle引継ぎを兼務させません。
- 終了済みWorkItemの保留候補をFinalizationで再評価しません。

同じ判断を複数段階で繰り返さないことは、`LR-016`、`LR-037`及び`LR-050`の再発防止条件です。

### Graph候補と回答根拠

- Graphの意味関係と方向は、Hypothesisと関係端点の役割からLLMが選びます。Programは既知ID、契約及び1ホップ制約を検証します。
- Hypothesisに対応する意味関係を説明できる場合は意味関係を使い、新しい候補が得られない場合に物理的な参照関係へ切り替えます。
- Graph候補はArticleを発見する情報であり、Article本文を取得して評価するまで回答根拠にしません。
- Graphで選んだArticle本文は、同じWorkItemの残りのGraph候補を処理する前にHypothesisへ反映します。
- Finalizationへ渡す本文は、Hypothesis又は解決済み依存関係へ対応付けたgrounding Evidenceに限定します。
- 回答中の法令名は、対応するEvidenceが持つ正式名称を使用します。

これらは`LR-023`、`LR-026`、`LR-030`、`LR-041`及び`LR-046`に基づきます。

### 索引、設定及び候補文脈

- 同じ検証シナリオで使うOpenSearchとNeo4jは、必要なArticleと関係を一貫して収録します。
- 検索一致chunkは候補発見の材料であり、Article全文の代用にしません。候補評価に追加文脈を渡す場合も、検索一致箇所と区別します。
- 同じ上限値やmodel設定をアプリ、Compose及び操作手順へ重複定義する場合は、正本と優先順位を明示し、実行時の値が一致することを検証します。

これらは`LR-042`、`LR-048`及び`LR-049`に基づきます。

## 変更手順

1. `README.md`、`RUNBOOK.md`、関連する正本文書と既存コードを確認します。
2. 変更対象の契約、Prompt、Projector、Validator、保存先、テストを特定します。
3. 修正前に、守るべき不変条件と、値の生成元から利用先までの流れを確認します。
4. 直接の発生箇所以外に、同じ不変条件を共有する経路がないか確認します。
5. 最小の変更を実装します。
6. 対象テスト、関連回帰テスト、必要に応じて全テストを実行します。
7. `git diff --check`と差分を確認します。
8. 仕様や操作が変わった場合だけ関連文書を更新します。

不具合調査は[DEBUGGING.md](DEBUGGING.md)に従います。
