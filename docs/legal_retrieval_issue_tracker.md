# 法令検索 課題管理

> 更新日: 2026-08-23
>
> 本書は、法令検索の現在地、未解決課題、優先順位、完了条件を管理する。
> 設計仕様の正本ではない。Agent契約は
> [汎用反復型Agent実装計画](generic_iterative_agent_framework_plan.md)、第二期の狙いは
> [第二期開発備忘録](second_phase_development_memo.md)、実行手順と実測値は
> [RUNBOOK](../RUNBOOK.md)を正とする。

## 1. 管理方法

課題のstatusは次の意味で使う。

| status | 意味 |
|---|---|
| `未着手` | 方針はあるが、実装または評価を開始していない |
| `対応中` | 実装、原因調査、または修正を進めている |
| `検証待ち` | 必要な実装は存在するが、要求する実モデルE2E評価に合格していない |
| `要設計` | 選択肢とトレードオフを決める必要がある |
| `停止中` | 再開条件を満たすまで意図的に作業を止めている |
| `完了` | 本書の完了条件を満たす証跡がある |

優先度は、第二期Step 1の完了に必須なものを`P0`、基盤の安定化に必要なものを`P1`、
対象拡大時に扱うものを`P2`とする。

statusを`完了`へ変更するときは、対応するテスト、評価結果、traceまたはcommitを「確認証跡」へ記載する。
設計を変更した場合は本書だけを更新せず、正本文書、契約、Prompt、テストも同じ変更で更新する。

## 2. 対象とする検索フロー

```text
利用者の質問
    ↓
WorkItemへ分解し、検証可能なHypothesisを作る
    ↓
Hypothesisを法令に現れやすい検索表現へ変換する
    ↓
OpenSearchで候補Articleを検索する
    ↓
選択したArticle全文をOpenSearchから取得して評価する
    ↓
必要ならHypothesisに沿ったGraph selectorをSolverが指定する
    ↓
Neo4jから指定条件の1ホップ候補を取得する
    ↓
候補Article全文をOpenSearchから取得して評価する
    ↓
必要なら検索語を変えてOpenSearchを再検索するか、
選択したArticleを次の1ホップGraph検索の起点にする
    ↓
根拠が揃えば回答し、分解・仮説自体が不適切なら次Cycleで仕切り直す
```

同じCycle内でもOpenSearchは複数回使用できる。ただし、同一条件を繰り返すのではなく、本文やGraphの
観察によって判明した法令名、条番号、委任事項、法令表現または未解決WorkItemを使って検索条件を更新する。
同じHypothesisと探索方針を維持できる間は同一Cycleで処理し、作業分解や前提から見直す場合だけ次Cycleへ進む。

## 3. 課題一覧

| ID | 優先度 | status | 課題 | 現在地 | 次の確認 |
|---|---|---|---|---|---|
| `LR-001` | P0 | 対応中 | 質問から必要な検索仮説を漏れなく作る | v150〜v152で規定存在や質問の言い換えを再現した。v153の労働法fixtureではHaikuが要件・上限構造・手続行為を含む仮説を1回で生成した | 収録済みの別分野でもHypothesisと`gaps`の意味形を実モデル確認する |
| `LR-002` | P0 | 検証待ち | 法令検索表現を作り、同一Cycle内でOpenSearchを適切に再検索する | 「必要な手続」を「公告・届出・通知・提出・期間・様式」等へ言い換えるPromptと複数stepは実装済み | 初回検索、本文観察後の検索語変更、同一scopeの重複防止をtraceで確認する |
| `LR-003` | P0 | 検証待ち | Graph由来Articleを起点に連続1ホップ探索する | 固定selectorでは法律→施行令→府令へ到達でき、Graph由来Articleの再起点化も実装済み | OpenSearchだけでは末端Articleを発見できない条件で、実モデルが2回の1ホップを選ぶE2E試験を通す |
| `LR-004` | P0 | 対応中 | 複合問題の統合Decisionを成立させ、次の探索または完了へ進む | v153のHaiku実データE2EはCycle 2まで進んだが、AnthropicがIntegrationのstrict schemaを大きすぎるとして400で拒否した | IntegrationのProvider schemaを現処理に必要な項目へ縮小し、収録済み問題で再実行する |
| `LR-005` | P0 | 未着手 | `gpt-4o-mini`で新検索経路を実モデル評価する | Provider接続、Structured Outputs、model切替は実装済み。法令E2Eは未実施 | Reviewer無効で公開買付け3問を実行し、Hypothesis、Tool、Evidence、Cycle、時間を記録する |
| `LR-006` | P1 | 要設計 | 意味分類coverage不足時にも逆引き検索爆発と取りこぼしを両立させる | publish済み意味関係ならselectorで絞れるが、未分類範囲でraw `REFERENCES/to_subject`を使うと高fan-inになる | 限定fallbackの発動条件、scope上限、coverage不足の表示、限定回答条件を決める |
| `LR-007` | P1 | 未着手 | CycleとStepを再開可能な状態として保存する | WorkItem、Hypothesis、Evidence、Graph review履歴はあるが、目標の`CycleRecord / StepRecord / ExplorationState`は未実装 | Tool観察後の中断から同じStepを再開し、別Cycleとして数えないfixtureを通す |
| `LR-008` | P1 | 対応中 | status、Provider schema、Prompt、遷移検証の修正漏れを防ぐ | 主要Solver項目の`Field.description`から共通用語集を生成し、Toolの用途・入力Schema・戻り値を`ToolDefinition`から`available_tools`へ投影する縦切りを実装した。OpenAI輸送はTool `arguments`を構造化objectのまま扱う | 全statusのowner・遷移・永続化versionを説明付き正本へ集約し、未定義statusで契約テストが失敗するようにする |
| `LR-009` | P1 | 対応中 | Tool数、本文取得数、Cycle数の用語と設定を一致させる | 現行既定は最大4 Cycle、1 step最大5 Tool要求、1 Cycle本文3 Article。過去の「4 Article」記述が一部資料に残る | 正本、Profile、Prompt、fixture、データセット説明の値と意味を照合する |
| `LR-010` | P2 | 停止中 | 全件Relation分類を安全に再開する | 全件Runは1,615 checkpointで参照scopeと改正法構造の問題が見つかり停止中 | shadow差分監査、影響候補特定、再開条件合格後に同じsnapshot・checkpointから再開する |
| `LR-011` | P2 | 要設計 | 自治体の条例・規則・要綱を使う小規模データセットを決める | 自治体向け利用像はあるが、データセットは未決定 | 上位法令改正→条例→規則→要綱の逆引きを検証できる最小集合を利用者と決める |
| `LR-012` | P0 | 完了 | LLMの固定指示、実行時入力、最終契約を分離し、レビュー可能な成果物として出力する | API送信・診断・成果物出力が同じ`RenderedModelCall`を使用し、snapshotでは呼出し別ファイルを出力する。代表research fixtureはOpenAI・Anthropic・Ollamaの基準成果物を持つ | 固定指示hash、動的入力hash、両schema、実送信hashの回帰テストと全887テストに合格 |

## 4. 最優先分析: 複合問題の統合

### 4.1 本書でいう統合

現行の統合は、取得済み本文を要約して最終回答を書く処理だけではない。Tool実行後の1回の
`SolverDecision`で、次をまとめて行っている。

```text
ToolResultとArticle全文
        ↓
EvidenceをHypothesisへ対応付ける
        ↓
HypothesisとWorkItemの状態を更新する
        ↓
下位規範、未取得候補、未解決観点を監査する
        ↓
同一Cycleで追加調査 / 次Cycleへ移る / 最終回答、のいずれかを選ぶ
        ↓
選択に対応するToolRequest、引継ぎ情報または引用付き回答を返す
```

したがって「統合失敗」は、最終回答の文章生成失敗だけでなく、Evidence対応付け、状態更新、
Cycle引継ぎ、完了判断または構造化出力契約の失敗を含む。

### 4.2 現時点で確認できている事実

- 狭い例外問題と公告問題は合格している。
- 総合問題はCycle 1で3 Articleを取得した後、引用に関する契約修復中に全体上限240秒へ達した。
- 停止時点のgold必須Article到達は2/6だった。
- Reviewerは既定無効であり、この失敗経路には関与しない。
- 全Cycle、通常呼出し、輸送修復、Framework契約修復は、1回の実行に設定された同じwall timeを消費する。
- 2026-08-22の`snapshot`実行では、OpenAI用輸送schemaがToolRequest内部の必須項目を拘束していない
  不具合を検出し、構造化ToolRequestへ変更した。旧欠落payloadのfixtureは現行輸送を表さないため削除し、
  現在の`output_schema.json`基準成果物と必須項目テストで回帰を検出する。
- 同修正後の再実行では、最初の4件の`legal_search`から9 Article IDを発見したが、Solverは
  `fetch_articles`を選ばず検索を反復した。最初の統合入力を
  `tob_overview_cycle1_after_search_v1.json`へ固定した。この時点では13件すべてが
  `search_navigation`で、Article全文取得数は0、Cycle残り取得枠は3である。
- 2026-08-23の実モデル実行では、Cycle 1の本文取得枠3件を使い切った後、Solverが
  `work_item_1`を`resolved`にし、そのbasisへ`hypothesis_1`を指定した一方、
  `update_hypotheses=[]`のまま`finalize`した。状態遷移検証は
  `resolved work item retains unresolved basis hypotheses`として正しく拒否した。
- 誤認はCycle Closeだけで始まっていない。Research Promptは、質問で列挙された対象範囲・例外・手続を
  別WorkItemへ分け、別の本文で検証する観点を束ねないよう明記していた。しかし実出力は全観点を1 WorkItem、
  1つの広いHypothesisへまとめ、検索も`公開買付け 手続 条文`の1件だけだった。この初期状態の粗さにより、
  後続Solverは一部Article取得を質問全体の完了と誤認しやすい状態になっていた。
- 解決済みfixtureの状態再生と契約検証は回帰していない。関連するfixtureテスト5件は合格した。一方、
  実モデルによる初期分解は再現しない。Profile v116の保存結果は4 WorkItem・4 Hypothesisだったが、
  保存済みv116 Promptを現在の同じ`gpt-4o-mini`へ新規セッションで2回入力すると、いずれも
  1 WorkItem・4 Hypothesisになった。現行v124 Promptでは3回とも1 WorkItem・1 Hypothesisだった。
  したがって、旧fixtureが保証する決定的なコード経路と、時点により変動しうる実モデル出力を分けて扱う。
  また、現行Prompt変更にはHypothesis分解まで弱めた回帰がある可能性が高いが、WorkItem分解の差は
  旧Promptでも再現するため、Promptだけを原因とは断定しない。
- 通常のCycle Close Promptには「unresolved HypothesisをbasisにしたWorkItemをresolvedにしない」、
  契約修復Promptには「WorkItemだけをresolvedにしない」という規則が存在した。OpenAI輸送schemaも
  Hypothesis更新要素の型を持っていたが、WorkItem更新とHypothesis更新は独立配列で、両者の条件関係を
  schemaでは強制していなかった。
- AdapterがHypothesis更新を落としたのではない。Provider輸送後のpayload自体が
  `update_hypotheses=[]`であり、初回と修復2回のSolverDecision hashはすべて同一だった。
  同じLLMへ全Decisionを再生成させるだけの修復は、この失敗に対して実質的な進展を生まなかった。
- 実出力には、最初の違反を直した後に現れる第二の矛盾もあった。DependencyDecisionは「委任元と末端を確認済み」
  と述べながら、basisには金商法27条の3のEvidenceしかなく、施行令7条を含めていなかった。さらに施行令7条本文には
  「内閣府令で定める」が残るため、法的意味としても末端確認済みとはいえない。最小fixtureは第一の状態遷移違反を
  単独再現するためDependency部分を正常化している。証跡は
  `agent-api/tests/fixtures/framework/tob_cycle_close_unresolved_basis_v1.json`で固定した。
- 同fixtureと元診断ログを使った制御実験では、縮小した約5,076 input tokenのContextでは3回とも
  Hypothesisを`supported`へ更新し、元の「WorkItemだけresolved」違反は再現しなかった。一方、保存済みの
  約34,710 input tokenのPromptを再実行すると、再びHypothesisを更新せず`finalize`した。契約ルール約7,012文字の後に
  約59,323文字のSolverContextが続き、そのうち`material_evidence`が40,732文字、`evidence_manifest`が8,513文字を占める。
- 既存の完了ルールから3項目を文意を変えずSolverContextの後ろへ再掲すると、同じ実データ・model・schemaで
  `continue / start_next_cycle=true / answer=null / dependency=needs_action`となり、状態遷移契約にも合格した。
  これは、ルール自体が存在しないことよりも、長い本文より前にだけ重要な完了条件を置くPrompt構造が直接の失敗要因である
  ことを示す。現行OpenAI接続は、`system_prompt`をproviderのsystem roleへ分離せず、Contextと連結した1件のuser messageとして
  送っているため、この配置の影響を受けやすい。
- schemaはWorkItem更新とHypothesis更新の個々の形を拘束するが、「WorkItemをresolvedにするならbasis Hypothesisも
  同じDecisionで確認済みにする」という相互条件を表現していない。そのため、Prompt理解が崩れた出力をprovider段階で止められず、
  Framework検証まで到達してから違反になる。検証自体は正しく機能している。
- 修復Promptにも別の構造問題がある。違反と修復規則の後ろに、誤った`previous_decision`全文を含む大きなContextを再掲し、
  Decision全体を再生成させている。末尾へ修復規則を再掲する実験でも`continue`と`needs_action`までは直ったが、
  WorkItemだけ`resolved`のまま残った。局所的な一貫性違反に対して全Decisionを再判断させる方式が、部分修復と反復を招いている。
- Profile v125では、Research、Integration、Cycle Close、Finalization、Reviewer Revision、Search Review、Graph Reviewの
  各入力を削減せず、その入力の後ろへ用途別の短い完了確認を置いた。保存済みCycle Close入力では、約34,900 input tokenを
  保持したまま`continue / start_next_cycle=true / dependency=needs_action`となり、状態遷移契約に合格した。
  一方、保存済みResearch入力では1 WorkItem・1 Hypothesisから2〜3件へ改善したものの、明示された4観点の完全分離は
  安定して再現していない。完了確認は長い入力後の規則適用を改善するが、実モデルの作業分解を保証するものではない。
- Profile v126では、法令固有の観点名を分割条件にせず、1つのWorkItemを「1つの完了判定で閉じられる
  1つの確認事項」と定義した。一部分だけ解決して他を未解決にできる場合は分割し、同じ確認事項への回答材料が
  複数あるだけなら分割しない。実モデルでは複合質問全体を1つの完了単位と解釈し、1 WorkItemへ書き写した。
- Profile v127では、元の質問が求める回答対象を単独で回答できる単位へ分け、複数対象を含む質問全体を
  1 WorkItemへ書き写さないよう明示した。Researchの`decision_reason`にはWorkItem数と短い確認対象を記載させ、
  Prompt理解を診断可能にした。実モデルでは3 WorkItemへ改善したが、主文の「必要になる条件」を省略した。
- Profile v128では、主文の問いと追加指定を両方WorkItemへ残すようResearch Promptへ明示した。またSearch Assessmentで
  17候補中1候補を落として別候補を重複した失敗に対し、入力後へ候補Article IDの順序付きチェックリストを機械投影する。
  法的機能の判断はLLM、件数・ID集合の検証は既存Programのままで、意味契約は追加していない。
- Profile v128の再実行では主文を保持しSearch Assessmentも全候補を一意に返したが、「必要になる条件」と
  「必要になった場合の手続内容」を同じ対象とみなして後者を省略した。Search Reselectionも取得枠3件に対して
  scope候補1件だけを選び、Integrationは15件の`fetchable_article_ids`を使わず成功済み検索を反復した。
- Profile v129では、同じ語を含んでも成立条件と成立後の内容は別の確認事項と明示する。Reselectionは取得枠内で
  未確認Hypothesisごとに候補を選び、Integrationは既知候補があれば同じ検索を繰り返さないことを出力前に確認する。
  いずれもPrompt修正であり、新しい意味契約やProgramによる候補選別は追加していない。
- Profile v129の再実行では候補選択とCycle継続は改善したが、Researchが現在Cycleの本文取得枠3件に合わせて
  WorkItemも3件へ減らした。またCycle 3で、本文取得済みArticleとParagraph Evidence IDを`fetch_articles`へ
  再指定し、既存のID契約に拒否された。
- Profile v130では、質問全体のWorkItem・Hypothesis作成と今StepのTool実行上限を分離する。Integrationと
  契約修復Promptには、`fetch_articles`は`fetchable_article_ids`だけを使い、提示済み本文やParagraph・Itemの
  Evidence IDを再取得しないことを明示した。Programの意味判断や状態遷移契約は変更していない。
- Profile v130でもResearchは本文取得枠3件に合わせて3 WorkItemを返した。Profile v131では
  `remaining_fetch_capacity`が`fetch_articles`だけの上限であり、WorkItem、Hypothesis、`legal_search`の上限ではないと
  用途を明示する。また`needs_action`のbasis Evidenceは委任元の確認根拠であって再取得対象ではなく、委任先を
  既知候補、Graphまたは異なる検索表現で探すことを通常Promptと修復Promptの双方へ明示した。
- Profile v131のfixture検証では、新規9 fixture、既存5 fixtureを含む全872 testが成功した。その後の
  `gpt-4o-mini`実経路では、Research Promptが実際に渡っていたにもかかわらず3 WorkItemのまま例外観点を欠いた。
  Cycle 2のIntegrationでは、`fetchable_article_ids`限定、取得済み本文を再取得しない、Paragraph・Itemの
  Evidence IDをArticle取得へ使わない、という完了確認も実際に渡っていたが、取得済みArticle 1件と
  Paragraph Evidence ID 2件を`fetch_articles`へ指定した。同じDecisionを3回返して契約違反となり、
  `research_cycle_count=2`、`stopReason=protocol_error`で終了した。これは古いPromptの読み込みではなく、
  固定fixtureの局所条件と実入力でのモデル挙動に差が残ることを示す。Promptだけの追記を反復せず、
  次の変更前に作業分解と取得対象選択をそれぞれ独立に再現できる評価方法を見直す。
- Profile v132では、Research Promptの固定数を含む例が実モデルの3分割を誘導した可能性を踏まえ、
  指示から具体的な件数例を除いた。参考例は独立節へ分け、規則ではないことを明示する。共通Promptには
  `fetchable_article_ids`、`grounding_evidence_ids`、`material_evidence`、navigation、候補Article、
  Evidence所属Articleの意味と使用先を一つの表で定義した。`basis_evidence_ids`は状態判断の根拠であり、
  次の本文取得対象ではないことも明示した。
- Profile v133では、WorkItem分解結果の出力書式と無関係だった「必要な手続」の検索語例を削除した。
  `decision_reason`は固定件数を含む自然文例ではなく、`add_work_items`の実件数と全WorkItem名を埋める
  プレースホルダー付き書式で定義する。
- Profile v134では、`decision_reason`を単なるWorkItem一覧ではなく、分解と最初の行動を選んだ理由に
  実件数と全WorkItem名を添える診断項目として整理した。Hypothesisの`gaps`は条件・範囲・行為という
  列挙に限定せず、命題を本文で判定するために不足する具体的な情報として定義する。
- Profile v139では、用途別の現在作業を固定指示の先頭へ移し、全入れ子型を重複掲載していた契約用語集を
  `SolverContext / SolverDecision`の入口だけへ縮小した。実モデルの初回Researchはなお3 WorkItemで、
  条件と実施手続を混同した。Cycle 3では既知候補を使わず成功済み検索を反復し、契約違反で停止した。
- Profile v142では初回Researchへ用途別read modelを導入し、本文取得枠、Graph、Evidence等の未使用値を
  Provider入力から外した。Provider schemaもResearchで返す差分だけへ縮小し、入力tokenは約17,700から
  約14,300へ減ったが、要求漏れだけは解消しなかった。
- Profile v143では、主文と列挙要求をLLM自身が明示する輸送専用
  `question_requirement_checklist`を追加した。Profile v145の単独実モデル試験では、条件・対象範囲・例外・
  実施手続の4観点を保持した一方、「根拠となる条文」を余分な5件目のWorkItemにした。
  Profile v147では、同欄が`add_work_items`と意味上重複していたため削除し、`add_work_items`を質問分解の
  唯一の正本にした。LLMは出力前に元の質問と`add_work_items`を直接照合し、Programへ意味の補正を移さない。

この事実から、2/6だけを見てOpenSearchまたはGraphの取得不能と結論付けない。少なくとも直近実行では、
Cycle 1の取得結果を統合して次の探索へ進む前に停止したため、残り4 Articleを探す機会自体が失われている。
一方、保存済み資料だけでは、最初の出力が違反した契約名、修復回数、各呼出し時間の完全な内訳までは
再現できない。ここを推測で修正せず、固定fixtureと計測で確定する。

### 4.3 分析対象

| 観点 | 確認すること | 問題なら疑う場所 |
|---|---|---|
| 統合入力 | 取得した3 Article全文、対応WorkItem・Hypothesis、直前ToolResultが欠落・重複なく見えているか | `CaseStore`、`Projector`、`SolverContext` |
| 差分範囲 | 直前結果と関係しない状態まで毎回再判断・再出力させていないか | `Projector`、`CaseUpdate`契約 |
| Evidence対応 | statementと直接根拠を対応付け、未知IDやnavigationを根拠にしていないか | 統合Prompt、Evidence binding、ID輸送 |
| 状態整合 | 未解決Hypothesisを残したままWorkItemを閉じる、または根拠なしでsupportedにしていないか | `SolverDecision`、状態遷移検証 |
| Cycle引継ぎ | 1 Cycleの本文3件枠を使い切った不完全状態で、finalizeせず`start_next_cycle=true`を返せるか | 統合Prompt、Cycle境界契約 |
| 契約修復 | 違反箇所だけを直せているか。統合Decision全体を再生成して意味判断を壊していないか | Provider輸送schema、修復Prompt、Adapter |
| 時間 | 通常統合、Provider内修復、Framework契約修復のどこで時間を使ったか | trace、wall-time配分 |
| 完了判断 | 全明示観点と直接根拠が揃う前に完全回答にしていないか | 統合Prompt、finalize検証 |

Programは既知ID、型、件数、参照整合、予算を検証する。Evidenceの関連性、Hypothesisの真偽、
未解決観点、次の検索、完了可否は引き続きSolverが判断し、統合問題の修正を理由にProgramへ移さない。
また、統合専用の新Agentは追加せず、Solverの統合利用形として扱う。

### 4.4 分析手順

```text
1. Cycle 1で3 Articleを取得した直後のCaseStateをfixture化
        ↓
2. Projectorが作る統合用SolverContextを保存して内訳を監査
        ↓
3. 外部LLMなしで、次の4形を契約・状態適用テスト
   a. 同一Cycleで追加Toolを要求
   b. 取得枠を使い切り、未解決のまま次Cycleへ移る
   c. 全観点が解決し、通常回答をfinalize
   d. 実行上限で、未確認事項を示した限定回答をfinalize
        ↓
4. 違反コード、Prompt文字数、入力・出力token、各修復時間をtraceへ記録
        ↓
5. 総合問題だけをReviewer無効・設定済みモデルで1回実行
        ↓
6. 統合を通過してから、残った不足を検索仮説・OpenSearch・Graphへ分類
```

最初から公開買付け3問を連続実行しない。狭い2問はCycle引継ぎを十分に通らないため、まず
`tob-overview`のCycle 1直後を再現対象にする。全体timeoutや本文取得上限を先に増やすと、契約失敗を
隠したまま入力だけを増やす可能性があるため、計測前には変更しない。

### 4.5 統合分析の完了条件

- 同じCycle 1状態から、同じ統合用`SolverContext`を再現できる。
- 統合入力の各要素について、件数と文字量を説明できる。
- 3件取得済み・未解決あり・Cycle取得枠0のfixtureが、修復なしで次Cycleへ遷移する。
- 通常完了と上限時の限定完了を区別し、open WorkItemを契約通過だけのために閉じない。
- 初回出力違反とProvider輸送違反を別々に記録し、修復前後の時間を説明できる。
- 実モデル総合問題が、240秒を増やさずCycle 2へ到達する。

## 5. P0課題の完了条件

### LR-001 検索仮説

- 質問が明示する要件、対象範囲、例外、手続が、重複しないWorkItemと検証可能なHypothesisになる。
- 不要な観点をチェックリストから機械的に追加しない。
- 取得本文から新しい委任事項が判明した場合だけ、必要なWorkItemまたはHypothesisを追加できる。
- 未取得の下位規範を学習済み知識で確認済みにしない。

### LR-002 OpenSearchの反復利用

- 1 Cycle内で、初回検索と観察後の再検索を別stepとして実行できる。
- 再検索には、変更した検索表現、判明した法令名・条番号、または対象WorkItemが記録される。
- 同一query、同一filter、同一Hypothesisの成功済みscopeを再実行しない。
- 検索候補のsnippetをHypothesisの支持根拠や回答citationに使わない。

### LR-003 連続1ホップGraph探索

- 1回の`legal_graph_neighbors`が1 mode、1 direction、意味検索では1 predicate、1ホップに限定される。
- Graph由来であることを理由に、選択Articleを次のGraph起点から除外しない。
- 法律→施行令と施行令→府令が、別々のToolRequestとしてtraceへ残る。
- Graph候補だけで回答せず、選択した両端Articleの全文をOpenSearchから取得する。
- 同じArticleへ複数経路で到達しても本文取得は1回で、DiscoveryLinkは各経路を保持する。

### LR-004 複合問題の完了判断

- `tob-overview`の必要Article 6件をすべてArticle全文として取得する。
- resolved WorkItemの根拠Hypothesisと回答citationが一致する。
- open WorkItemまたはunresolved Hypothesisが残る通常実行を完全回答としてfinalizeしない。
- 時間上限に達した場合は、未確認事項を隠さず限定回答として示す。
- 契約修復回数、入力・出力token、Tool時間、Cycleごとの取得Articleをtraceから説明できる。

### LR-005 GPT-4o mini実モデル評価

- `tob-exceptions`、`tob-announcement`、`tob-overview`をReviewer無効で各1回実行する。
- 必要Article到達数と回答観点を、過去のHaiku結果と同じ指標で比較する。
- 失敗時は、モデル判断、Prompt、共通契約、Provider輸送、Tool、データのどこで失敗したかをtraceで分離する。
- Provider疎通やmockテストだけを法令検索の合格としない。

### LR-012 Prompt・契約の最終成果物

- 同じ処理モード、Provider輸送方式、契約versionでは、動的入力が変わっても固定指示の
  `instructionsHash`が変わらない。
- 質問、検索結果、Evidence、残り枠、許可ID、候補別名、修復情報を固定指示から分離して
  `input.json`へ出力する。
- `instructions.md`、`input.json`、Providerへ渡す`output_schema.json`、正規化後の
  `normalized_schema.json`、実送信`request.txt`、来歴とhashを持つ`manifest.json`を、LLM APIを
  呼ばずに生成できる。
- API送信処理と成果物出力処理が同じレンダリング結果を使い、fake Providerが受け取るPrompt・schemaと
  成果物が一致する。
- 通常実行の成果物はGit管理外、代表fixtureの基準成果物だけをGit管理し、再生成差分をテストで検出する。
- 既存fixtureを固有の回帰価値で監査し、古いPrompt文言または修正済み輸送形式だけを固定するものを削除する。

## 6. 確認済みの前提

次は新規課題として再度設計し直さず、回帰対象として維持する。

| 項目 | 確認済み内容 |
|---|---|
| データ構造 | 保存済みe-Gov XMLを正本とし、Document、Article、Paragraph、Item、本則・附則、枝番を区別する |
| 本文取得元 | 候補発見とArticle全文取得はOpenSearch、関係候補取得はNeo4jとし、本文を二重取得しない |
| Graph単位 | 1要求1ホップとし、Programが候補を意味的に自動再帰展開しない |
| Graph経路 | 公開買付けミニsnapshotでは、固定selectorにより金商法27条の2→施行令7条→府令2条の5、金商法27条の3→府令10条へ到達できる |
| Evidence | 検索候補とGraph候補はnavigationであり、取得済みArticle本文だけを法的判断の根拠候補にする |
| 判断境界 | Hypothesis、検索語、Graph selector、候補採否、根拠十分性、完了はSolverが判断し、Programは既知ID、型、件数、予算、重複scopeだけを検証する |
| Agent構成 | 回答経路はSolverと任意Reviewerに留め、Reviewerは既定無効とする |

## 7. 推奨する実行順

```text
LR-012  固定指示・動的入力・最終契約の成果物出力を整備
    ↓
LR-004  Cycle 1直後の統合fixtureと計測を整備
    ↓
LR-004  総合問題だけを実行し、Cycle 2遷移を確認
    ↓
LR-001  統合後も残ったWorkItem・Hypothesis不足を確認
    ↓
LR-002  法令検索表現と同一Cycle内の再検索を確認
    ↓
LR-003  OpenSearchだけでは解けない連続1ホップE2Eを確認
    ↓
LR-005  設定済みモデルで公開買付け3問を同一指標で比較
    ↓
LR-004  総合問題を必要Article 6/6まで通す
    ↓
LR-006〜LR-009  対象拡大前に基盤を安定化
    ↓
LR-011  自治体向け小規模データセットを決定
    ↓
LR-010  必要性と費用を再評価して全件分類を再開
```

全件Relation分類は、P0の検索経路が小規模データで合格するまで再開しない。

## 8. 確認証跡

| 日付 | 対象 | 結果 | 証跡 |
|---|---|---|---|
| 2026-08-21 | 公開買付けミニGraph | 17候補を承認し、24 RelationAssertionをpublish。固定selectorで代表3経路へ到達 | [第二期開発備忘録](second_phase_development_memo.md#22-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・例外問題 | 必要Article 3/3、回答観点3/3 | [第二期開発備忘録](second_phase_development_memo.md#22-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・公告問題 | 必要Article 2/2、回答観点4/4 | [第二期開発備忘録](second_phase_development_memo.md#22-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・総合問題 | 240秒で停止し、必要Article 2/6。第二期Step 1は未合格 | [第二期開発備忘録](second_phase_development_memo.md#22-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-22 | OpenAI Provider | `gpt-4o-mini`への切替とStructured Outputs接続を実装。法令E2Eは未実施 | [RUNBOOK](../RUNBOOK.md) |
| 2026-08-23 | LR-012 Prompt・契約成果物 | 固定指示、動的入力、Provider schema、正規化後schema、実送信内容を分離。3 Provider基準成果物と全887テストに合格 | [RUNBOOK](../RUNBOOK.md) |
