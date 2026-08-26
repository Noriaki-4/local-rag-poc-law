# 法令検索 課題管理

> 更新日: 2026-08-26
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
| `LR-001` | P0 | 対応中 | 質問から必要な検索仮説を漏れなく作る | Profile v289でHypothesisがないWorkItemを1件ずつ処理する方式へ変更した際、元質問、`action_actor`及び`gaps`をHypothesis生成契約から落としたため、主体の推測と抽象的な命題が再発した。v300で`statement`をWorkItemへの回答を構成する回答項目として定義し、追加の未確認事項がない`gaps=[]`を許可した。v301でWorkItem最大24件、Hypothesis最大4件／WorkItemという意味上の上限をProvider schemaから削除した。v302で分割単位を「法令本文の一つの規定内容で個別に支持又は否定できる命題」と明確化した。v303では1 WorkItemずつ処理するHypothesis生成入力から元質問全体を外し、WorkItemを唯一の作業範囲とした。`gpt-4o-mini`の隔離実行では総合の成立条件へ他WorkItemの論点が混入する問題は解消した。一方、公告は4件に分かれたが一部`statement`に複数事項が残り、広い例外は抽象的な1件に留まった。`action_actor`はWorkItemだけを正本とし、Hypothesisには複製しない | 公開買付けの公告・例外・総合WorkItemを隔離実行し、WorkItem外の論点を混ぜず、検索対象を選べる具体的なHypothesisが生成されることを確認する |
| `LR-002` | P0 | 対応中 | 法令検索表現を作り、同一Cycle内でOpenSearchを適切に再検索する | 検索要求作成だけの隔離診断を追加した。Profile v290で`purpose`を確認内容、`query`を短い法令用語の組合せとして分離した。例外問題では直接例外と委任を1検索にまとめて法令表現を生成できたが、総合問題の検索語はまだ抽象的である | 個別語をPromptへ追加せず、LR-001のHypothesis具体性と実際のOpenSearch候補を合わせて評価する |
| `LR-003` | P0 | 完了 | Graph由来Articleを起点に連続1ホップ探索する | `gpt-4o-mini`の実モデルtrace v7で、金商法27条の2→施行令7条、施行令7条→府令2条の5を別々の1ホップGraph要求として実行し、府令本文取得後にCycle 1で正常完了した | `lr_003_second_hop_integration_v1.json`、`lr_003_second_hop_graph_review_v1.json`、`lr_003_cycle_close_deferred_frontiers_v1.json`を回帰fixtureとして維持する |
| `LR-004` | P0 | 対応中 | 複合問題の統合Decisionを成立させ、次の探索または完了へ進む | Observation Integration、Dependency Assessment、Cycle Closeを分離した。Profile v260では投影後に未解決事項がなければCycle Close schemaが`finalize`だけを許し、矛盾した次Cycle開始を防ぐ | 公開買付け総合問題で複数Cycle後も同じ境界条件を満たすことを確認する |
| `LR-005` | P0 | 対応中 | `gpt-4o-mini`で新検索経路を実モデル評価する | Profile v291で公告・例外・総合を各1回実行した。正常完了は公告だけで、必要Articleは合計2/11、回答要点は4/12。例外と総合はCycle終了後の`finalize`矛盾で`protocol_error`となった | LR-021の候補理解・対応判断を単一責務化した後、LR-019のCycle切替を修正し、同じ3問を再実行する |
| `LR-006` | P1 | 要設計 | 意味分類coverage不足時にも逆引き検索爆発と取りこぼしを両立させる | publish済み意味関係ならselectorで絞れるが、未分類範囲でraw `REFERENCES/to_subject`を使うと高fan-inになる | 限定fallbackの発動条件、scope上限、coverage不足の表示、限定回答条件を決める |
| `LR-007` | P1 | 未着手 | CycleとStepを再開可能な状態として保存する | WorkItem、Hypothesis、Evidence、Graph review履歴はあるが、目標の`CycleRecord / StepRecord / ExplorationState`は未実装 | Tool観察後の中断から同じStepを再開し、別Cycleとして数えないfixtureを通す |
| `LR-008` | P1 | 対応中 | status、Provider schema、Prompt、遷移検証の修正漏れを防ぐ | 主要Solver項目の`Field.description`から共通用語集を生成した。初回3 Stepは`ResearchStepInput`の実投影項目と入れ子要素から`input_contract`を生成し、基準Providerの完成成果物を本番生成結果と比較する。他Providerとの意味契約一致は重複ファイルを持たずテストする。Toolの用途・入力Schema・戻り値は`ToolDefinition`から`available_tools`へ投影する | 全statusのowner・遷移・永続化versionを説明付き正本へ集約し、未定義statusで契約テストが失敗するようにする |
| `LR-009` | P1 | 対応中 | Tool数、本文取得数、Cycle数の用語と設定を一致させる | 現行既定は最大4 Cycle、1 step最大5 Tool要求、1 Cycle本文3 Article。過去の「4 Article」記述が一部資料に残る | 正本、Profile、Prompt、fixture、データセット説明の値と意味を照合する |
| `LR-010` | P2 | 停止中 | 全件Relation分類を安全に再開する | 全件Runは1,615 checkpointで参照scopeと改正法構造の問題が見つかり停止中 | shadow差分監査、影響候補特定、再開条件合格後に同じsnapshot・checkpointから再開する |
| `LR-011` | P2 | 要設計 | 自治体の条例・規則・要綱を使う小規模データセットを決める | 自治体向け利用像はあるが、データセットは未決定 | 上位法令改正→条例→規則→要綱の逆引きを検証できる最小集合を利用者と決める |
| `LR-012` | P0 | 完了 | LLMの固定指示、実行時入力、最終契約を分離し、レビュー可能な成果物として出力する | API送信・診断・成果物出力が同じ`RenderedModelCall`を使用し、snapshotでは呼出し別ファイルを出力する。代表research fixtureはOpenAI・Anthropic・Ollamaの基準成果物を持つ | 固定指示hash、動的入力hash、両schema、実送信hashの回帰テストと全887テストに合格 |
| `LR-013` | P0 | 検証待ち | Provider共通の小さいSolver輸送契約へ統一する | v154で全Providerを同じ処理段階別schemaへ統一し、Anthropic専用sidecarを新規経路から外した。実行時IDはenumへ複製せず共通validatorで検証する。代表Cycle Close schemaは14,494文字から7,014文字へ減少し、全895テストに合格した | 同じcheckpointをHaikuで再生し、Integrationがgrammar complexityの400エラーにならないことを確認する |
| `LR-014` | P1 | 検証待ち | Haikuで承認した中間状態から安価なモデルで後続処理を再生する | checkpointの明示承認をpromotion時に記録し、指定Provider・modelで1回のSolver処理を再生する`replay_agent_checkpoint.py`を実装した。APIを使わない単体テストは合格した | Haikuの正常中間状態を承認済みfixtureへ昇格し、`gpt-4o-mini`、同じcheckpointのHaikuの順に実モデル再生する |
| `LR-015` | P0 | 検証待ち | 初回Researchを単一責務のStepへ分け、要求をWorkItemとそれ以外へ欠落なく分解する | Profile v156で同一Cycle内の要求分解、仮説立案、検索要求作成を実装した。各完成Promptは単独で読めるH1を持ち、処理順のStep番号を含めない。一時的な段階比較コードを整理した後の全900テストに合格した | 別分野fixtureと公開買付けE2Eを`gpt-4o-mini`で確認し、最終的にHaikuで品質確認する |
| `LR-016` | P0 | 検証待ち | Tool観察の意味統合とCycle Closeを単一責務のStepへ分ける | v183でArticle→Hypothesis対応をObservationへ渡し、GPT-4o mini実モデルで4 Hypothesisへの本文Evidence保存を確認した。後続Stepの時間切れで成功済みObservationを失わないcheckpoint保存を追加した。対象WorkItemがない依存判定は呼び出さず、OpenAI schemaへ`null` enumを出す経路も除去した | 同じcheckpointをHaikuで再生し、本文の部分確認、意味更新、Cycle判断を確認する |
| `LR-017` | P0 | 対応中 | 検索候補の規律主体と行為対象を安定して区別する | Profile v304で主体照合LLMによるArticle・Hypothesis IDの再出力を廃止し、入力順の照合結果をProgramが既知組へ結合するようにした。候補選択には内容対応があり、主体照合が`mismatched`でない組だけを投影し、Provider schemaも同じ組だけを許可する | 公開買付け3問を再実行し、主体照合の転記漏れと、前段で不一致とした組の再選択によるprotocol errorがないことを確認する |
| `LR-018` | P0 | 完了 | Graph探索が必要な未解決事項があってもSolverがGraph検索を要求しない | 次Cycle開始時の保留OpenSearch候補を自動Search Selectionへ戻さず、Integrationで既知候補・Graph・再検索を比較するよう修正した。Graph selectorのmode・predicate・directionを分岐schemaで拘束し、同一Graph要求と本文取得要求は選択内容を保ったまま輸送時に統合する | Cycle 2 fixtureで府令10条を含む既知候補の本文取得へ進むこと、および候補を除いたGraph必須状態で`27条の2 / IMPLEMENTS / from_subject`の1要求を返し共通契約を通過することを`gpt-4o-mini-2024-07-18`で確認済み |
| `LR-019` | P0 | 対応中 | 統合の意味的な行動選択を違反別契約からSolver loopへ戻す | 下位規範Action PromptはTool選択だけを求めていたが、汎用Solver schemaが`finalize`と`answer`も要求できる不整合があり、実モデルが`finalize + answer=null + ToolRequest`を返して停止した。Profile v304で同処理を`decision_reason + tool_requests`だけの専用契約へ分離し、既存DependencyDecisionとの対応はProgramがWorkItem IDから復元する | 例外問題を再実行し、下位規範Actionが汎用完了分岐へ入らず次Toolを実行できることを確認する |
| `LR-020` | P1 | 要設計 | 複数の解釈や規律主体が成立する質問を一方的に確定せず、利用者へ確認する | 質問分解時に主体を確定させると誤った候補を早期に除外する。主体を限定せず検索すれば、発行者自身と発行者以外等の異なる規律主体が候補本文から判明する | LLMが検索後に結論を変える主体分岐を検出し、既知情報で確定できなければ確認を求める。Programが確認待ちを保存し、回答後に同じCaseを再開する最小契約を設計する |
| `LR-021` | P0 | 対応中 | 検索候補の内容評価を単一責務にする | Profile v292でArticle・Hypothesis組と主体照合を分離した。v293では、見出しと検索抜粋だけでArticle全体やHypothesisの正否を判断させず、同じ法的争点の本文取得候補を整理する予備判定へ責務を限定した。`run_search_assessment_debug.py`は本番`solver_search_review.md`だけを1回呼び、完成Prompt・入力・schema・生応答を保存できる。v293の単独候補診断でも別規制の府令63条と義務成立後の金商法27条の13が`h-1`へ誤対応し、候補数ではなく、入力Hypothesisが確認する法的結論を特定できないことが残因と分かった | 検索抜粋評価へ規則を追加する前に、Hypothesis生成が確認対象の法的結論を具体化できるかを最小fixtureで確認する。その後、公告、例外、総合の順に内容評価、主体照合、候補選択を別々に検証する |
| `LR-022` | P0 | 未着手 | 後続Cycleで既存WorkItemへ代替Hypothesisを追加する | 設計ではHypothesisの`statement`を別の意味へ上書きせず、見立てを変える場合は新しいHypothesisを追加する。しかし現行の段階別経路は、Hypothesisが1件もないopen WorkItemだけを生成処理へ送り、本文評価は既存Hypothesisの更新だけを許す。そのため、初期仮説が反証された場合も、同じWorkItemへ新しい見立てを追加して次Cycleで仕切り直せない | H1が`contradicted`または新しい規律構造が判明したfixtureで、H1と根拠を履歴として保持し、Cycle 2で同じWorkItemへ新IDのH2を追加できることを確認する。検索方法だけを変える場合は不要なHypothesisを追加しない |

### 3.1 LR-016 Tool観察とCycle Closeの単一責務化

新しいArticle本文を取得した後は、取得枠を使い切っていても、まず同じSolverのObservation Integration
で本文を評価し、WorkItem・Hypothesis・下位規範確認の意味状態を更新する。Adapterはその差分を機械的に
更新後のread modelへ投影し、続けてCycle Closeを呼ぶ。Cycle Closeは本文評価やTool選択を兼務せず、
通常完了または次Cycleへの引継ぎだけを判断する。2つの結果は1件の`SolverDecision`へ正規化し、共通validatorを
通過した後にCaseStoreへ一度だけ適用する。

Cycle CloseのProvider入力から、`fetchable_article_ids`、検索・Graph候補、Tool定義、汎用`update`、
本文評価用の分岐を除外する。入力には本文評価を反映済みのWorkItem・Hypothesis、引継ぎ候補となる
既知Evidence IDを投影し、出力は通常完了または次Cycle開始の小さいdiscriminated contractにする。Programは既知ID、重複、
件数、分岐の排他性だけを検証し、Evidenceの採否、完了可否、次Cycleの焦点はSolverが判断する。

NoSQLはこの課題の解決条件にしない。永続化方式はCaseStore Adapterの責務であり、Providerがコンパイルする
出力schemaの大きさとは独立している。まず専用View、専用Decision、Projectorによる入力投影で解決する。

各処理の代表fixtureから、少なくとも`instructions.md`、`input.json`、`output_schema.json`、
`normalized_schema.json`、`request.txt`を生成する。実行時とレビュー用成果物は同じ生成経路を使い、
Cycle Closeの完成Promptに本文取得候補やTool契約が混入していないことを回帰テストする。

### 3.2 LR-015 初回Researchの単一責務化で得た知見

2026-08-24に、公開買付け総合質問と`gpt-4o-mini`を使い、初回ResearchのPrompt、入力、
Provider schemaを段階的に変更した。各呼出しは新規API要求であり、過去応答の会話履歴を引き継いでいない。

- 現行本番の一括Researchを3回実行すると、質問が求める4 WorkItemは毎回作成できたが、
  Hypothesisは「特定の条件」「法令に基づく手続」等へ抽象化し、検索語も質問の言い換えに寄った。
- WorkItemを入力済みにしてHypothesisだけを作る呼出しは、3回すべて人数、割合、証券種類、公告、
  届出期限等の検証可能な判定軸を含んだ。WorkItemを固定したHypothesisと検索要求の同時生成も3回とも
  同じ具体性を維持した。したがってモデルに仮説立案能力がないのではなく、質問分解を確定しながら
  後続責務も同じ応答で行う負荷が主因である。
- WorkItemだけを返すschemaでは、Promptで禁止しても「根拠となる条文」を5件目のWorkItemへ3回とも追加した。
  これはユーザーの明示要求を格納する場所がWorkItem以外にない契約上の不足だった。
- `non_work_item_requirements`相当の格納先を設けると、3回すべて法的確認事項4件と
  「各回答に根拠条文を示す」という残りの要求へ分離できた。
- 実際の前段出力を次段へ渡す「要求分解 → Hypothesis立案 → 検索要求作成」を3回連結すると、
  毎回4 WorkItem、4 Hypothesis、4検索要求となった。3呼出しの合計input tokenは3,467〜3,496で、
  同じ質問に対する現行本番一括Researchの6,582 input tokenより少なかった。
- 旧完成Promptは、抽象的な役割説明、「現在の作業：Research」、初回には実行しないTool結果受領後の処理、
  個別ルール、共通ルールを同じ呼出しへ連結していた。これでは今回実行する責務と将来の責務を区別しにくい。
  新経路では各呼出しを`Step 1`、`Step 2`、`Step 3`として直接示し、そのStepの入力・手順・ルール・
  出力確認だけを渡す。初回3 Stepへ全体用`solver_common.md`は連結しない。

要求分解の正規契約は次の不変条件を持つ。

```text
質問が明示する要求全体
= work_items
+ non_work_item_requirements

work_items
  独立して法令調査し、完了判定する要求

non_work_item_requirements
  明示要求のうち、独立したWorkItemとして完了判定しないもの
```

`non_work_item_requirements`は「重要でない要求」や「全WorkItemに必ず共通する要求」を意味しない。
根拠提示や比較・表示方法等、質問が明示したが独立調査単位ではない要求を保持する。
対象時点、地域、主体、対象が法的結論を左右する場合は、該当WorkItemに残す。
元質問は引き続きCaseStoreの正本とし、この欄で置き換えない。質問にない形式や詳しさを追加しない。

Profile v156では同じSolver・同じResearch Cycleの中で3 Stepを順番に実行する。ProgramはCaseStoreへの保存、
ID付与、既知ID・件数・参照整合の検証だけを担当する。要求の仕分け、WorkItemの意味、Hypothesis、
検索語はLLMが判断する。別Agent、別Cycle、Providerの会話履歴またはProgramによる意味補正は導入しない。

### 3.3 LR-017 候補選別の主体不一致

検索候補の法的機能が同じでも、規律する主体、行為、対象または条件が異なれば、同じHypothesisの
検証候補として代用できない。公開買付け総合問題では、27条の2と27条の22の2がどちらも
`applicability`候補になり得るが、前者は発行者以外の者による買付け、後者は発行者自身による買付けを扱う。

一括した候補評価へ`matched_hypothesis_ids`を追加しただけでは、候補要約で主体を正しく記述しても、
同じ出力内の主体対応で取り違えることがあった。Profile v197では処理を、候補の内容評価、候補見出しと
要約を使う主体分類、両結果を使う候補選択の3段階へ分けた。その後、二つのLLM処理が
`matched_hypothesis_ids`を独立生成し、Programが積集合を取ると、片方のID欠落だけで候補が理由なく消える
構造問題が判明した。Profile v292では内容評価が作ったArticle・Hypothesis組を主体照合の固定入力とし、
主体照合は各組のstatusと理由だけを返す。Programは組の全件性、既知ID、重複と出力間の矛盾だけを検証し、
条文の意味や正しい主体は判定しない。

2026-08-26の通し実行では、主体照合が既知IDを再出力する契約のため、組の転記漏れで総合問題が停止した。
また、候補選択schemaは主体不一致の組を出力可能なのに、後段validatorは禁止していたため、公告問題も停止した。
Profile v304では主体照合を入力順の結果配列にし、Article・Hypothesis IDはProgramが既知組から復元する。
候補選択へは内容対応があり、主体照合が`matched / unknown`の組だけを`selectable_hypothesis_ids`として投影し、
同じ組だけをProvider schemaで許可する。これは前段LLM判断の機械的な結合であり、Programによる法的意味判断ではない。

再発時点の状態は
[主体不一致候補fixture](../agent-api/tests/fixtures/framework/tob_overview_search_actor_mismatch_v1.json)へ保存した。
公開買付け固有のPromptへ寄せない回帰確認として、土地所有者と開発行為者を区別する
[別分野fixture](../agent-api/tests/fixtures/framework/cross_domain_actor_object_selection_v1.json)も使用する。
Profile v269では、質問の行為者をWorkItemの`action_actor`、対象側主体を`target_actor`へ分離し、
`actor_relation`で両者の関係を保持する。主体情報の正本はWorkItemだけとし、Hypothesisには重複保存しない。
後続の検索計画と候補主体分類には、ProgramがHypothesisの所属WorkItemから三項目を決定的に結合する。
Profile v270では、対象関連主体が存在しない状態を`actor_relation=not_applicable`で表せるようにし、
物・文書・公告・手続自体を主体にしないルールを追加した。またHypothesisの`statement`が
「特定の条件」「法令により異なる」だけで終わらず、質問に関係する法的判定軸を一つ以上示すよう明確化した。
[主体関係checkpoint](../agent-api/tests/fixtures/framework/tob_actor_relation_search_v191.json)を
`gpt-4o-mini-2024-07-18`の新規呼出しで2回再生し、両方で27条の2を選択し、27条の22の2を
deferredとした。結果要約は
[v197実モデル回帰fixture](../agent-api/tests/fixtures/framework/tob_actor_relation_selection_regression_v197.json)
へ固定した。これにより本項の完了条件を満たした。

Profile v275では、その後の切り分けで`target_actor`と`actor_relation`がなくても、WorkItemの質問、
`action_actor`、Hypothesisを候補要約と直接比較すれば、公開買付けと別分野の主体不一致を識別できることを確認した。
そのため三項目への事前分類は廃止し、WorkItemは`action_actor`だけを正本とする。行為対象は`question`に残し、
候補の主体対応はLLMが、内容評価済みのArticle・Hypothesis組ごとにstatusと理由を返す。
Programは組を追加・削除せず結合し、最終的な本文取得候補は後段のLLMが選択する。
旧fixtureと保存状態の`target_actor`、`actor_relation`、`regulated_actor_role`は読込み時に破棄する。

### 3.4 LR-020 曖昧な質問の確認

同じ質問から複数の法的確認事項・主体関係が合理的に成立し、その違いが検索仮説や回答を変える場合は、
Solverが一つへ決め打ちせず、解釈候補と相違点を利用者へ示して確認を求める必要がある。例えば
「公開買付けによらずに買い付けられる主な場合と、所有者が少数である場合の条件」は、後半を
買付けの例外条件として読むか、所有者数の条件を独立して問うかでWorkItemと主体が変わる。

意味上の曖昧さと解釈候補はLLMが判断する。Programは候補の意味を選ばず、確認待ちとしての保存、利用者回答との
対応、同じCaseからの再開だけを担当する。表現差にすぎず検索・回答が変わらない場合には確認を求めない。
本項は未実装の設計課題として記録し、今回の主体分離実装には追加しない。

主体は質問分解時に必ず確定させない。まず行為、対象、条件から主体を限定せず検索し、候補本文に現れた
規律主体をLLMが整理する。主体の違いが法的結論を変える場合だけ、質問と既知情報で一つへ確定できるか判断する。

```text
主体を限定せず検索
  → 候補本文の規律主体を整理
  → 主体の違いで結論が変わらない: 処理を継続
  → 主体の違いで結論が変わる
      → 既知情報で確定できる: 該当するHypothesisで継続
      → 確定できない: 利用者へ確認して確認待ちにする
          → 回答をCaseへ追加し、該当するHypothesisから再開
```

利用者へ確認できない実行形態では、Solverが一方を選ばず、主体ごとの条件付き結果と未確定事項を返す。
確認は主語の省略だけを理由に要求せず、主体の違いが検索経路または法的結論を実際に変える場合に限る。

評価時の誤差と機能課題を混同しないため、`tob-exceptions`設問は、所有者が少数である場合を
「公開買付けによらずに買い付けられる条件」と明示する文へ変更した。`gpt-4o-mini`の隔離実行では、
後半のWorkItemとHypothesisが買付け条件、所有者数の基準、合意条件を維持した。これは評価設問の明確化であり、
曖昧なエンドユーザー質問への確認機能を実装したものではない。

### 3.5 2026-08-25 回帰確認

Profile v250以降の統合・下位規範探索変更をfixture、全テスト、`gpt-4o-mini`実モデルで確認し、次の
構造的なデグレードを修正した。

- Tool観察後にDependency Assessmentを再実行せず、古い`needs_action`から検索やGraphを反復する経路。
- 候補0件で成功したGraph要求が後続Viewから消え、同じ1ホップ検索を再要求できる経路。
- 「府令で定める」等の本文文字列をProgramが解釈し、LLMの依存判断を上書きする経路。Programは既知ID、
  Article数、根拠Articleの相異等の構造矛盾だけを検査する。
- 重複検索の契約修復で過去の違反に含まれるTool種別をすべてschemaから除外し、検索とGraphの代替を
  相互に失わせる経路。直前に重複した種別だけを修復時に外す。
- Observation反映後に全WorkItemが解決済みでも、Cycle Closeが`start_next_cycle`を選べる契約。

再現状態は`tests/fixtures/framework`のdependency actionおよびCycle Close fixtureへ固定した。Profile v261で
全991テストが合格した。v260の公告問題は法27条の3・府令10条を取得して正常完了した。例外問題は構造上正常終了するが、
施行令7条を選べない意味精度の課題が残るため、構造回帰の解消と法的回答の合格を区別して管理する。
総合問題で重複Graphになったcheckpointをv261で再生すると、同じGraphを反復せず、法令表現を変えた
`legal_search`を選び、共通契約を修復なしで通過した。

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
- 2026-08-24に本番Promptを合成せず、質問分解、WorkItem、入れ子のHypothesisだけを返す最小診断を追加した。
  公開買付け質問への`gpt-4o-mini`の入力は380 tokenで、条件、対象範囲、例外、必要手続の4観点すべてに
  質問の言い換えではない具体的な暫定回答を生成した。Haikuも同じ最小Promptで同じ4観点へ具体的な
  暫定回答を生成した。両モデルとも出典指定を5件目のWorkItemにしたが、最小Promptには出典を独立作業から
  除外する本番規則を意図的に含めていない。この結果から、`gpt-4o-mini`に一般的な仮説立案能力がないとは
  いえず、本番での抽象化は法令Research Prompt・契約との相互作用として扱う。暫定回答の法的正確性は
  この診断の合格条件ではなく、検索後に本文で支持、反証または修正する。

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

Profile v289の`gpt-4o-mini`隔離診断では、適用除外を主題とする設問から、直接の例外規定と
下位法令への委任を別Hypothesisとして出力し、検索前の具体的な除外事由の推測を避けられた。
総合設問内の例外WorkItemでは下位法令への委任を安定して出せておらず、完全解消ではない。
例外用ルールは現在のWorkItem自身が適用除外又は例外を問う場合だけ適用し、成立条件、対象範囲、
手続のWorkItemへ波及させない。通常論点では抽象的なHypothesisが残るため、検索計画での有効性は未確認である。

### LR-002 OpenSearchの反復利用

- 1 Cycle内で、初回検索と観察後の再検索を別stepとして実行できる。
- 再検索には、変更した検索表現、判明した法令名・条番号、または対象WorkItemが記録される。
- 同一query、同一filter、同一Hypothesisの成功済みscopeを再実行しない。
- 検索候補のsnippetをHypothesisの支持根拠や回答citationに使わない。

Profile v289の隔離診断では、公告、適用除外、総合問題の全出力がschema検証を通過した一方、
`に関する法令`や`必要条件`等の質問に近い語が残った。直接の例外規定と下位法令への委任を
別検索にする固定ルールは設けない。複数Hypothesisを同じ検索へまとめるかは、検索語と取得したい候補が
共通するかをSolverが判断する。Programによる意味分割は追加しない。

Profile v290の初回隔離診断では、直接の例外規定と下位法令への委任を同じ検索要求へまとめられ、
固定分割が不要であることを確認した。一方、総合問題では禁止した`に関する法令`等の質問要約が残ったため、
義務、定義、例外、委任、手続に対応する一般的な法令表現を検索語の判断材料としてPromptへ追加する。

続くProfile v290の隔離診断では、`purpose`を確認内容の文章、`query`を検索欄へ入れる短い法令用語の
組合せとして分けた。公告は`公開買付け 公告 掲載内容`、例外は
`公開買付け 適用除外 除く 政令で定める`となり、例外と委任も1検索へまとめられた。
総合問題は文章型のqueryを解消したが、4件中3件は抽象語が中心で、1件には不自然な語も残った。
検索計画Promptへ公開買付け固有の語を追加せず、前段Hypothesisの具体性と実際の検索候補を分けて評価する。

### LR-003 連続1ホップGraph探索

- 1回の`legal_graph_neighbors`が1 mode、1 direction、意味検索では1 predicate、1ホップに限定される。
- Graph由来であることを理由に、選択Articleを次のGraph起点から除外しない。
- 法律→施行令と施行令→府令が、別々のToolRequestとしてtraceへ残る。
- Graph候補だけで回答せず、選択した両端Articleの全文をOpenSearchから取得する。
- 同じArticleへ複数経路で到達しても本文取得は1回で、DiscoveryLinkは各経路を保持する。

実モデルtrace `lr-003-live-loop-v7`では、`semantic_assertion / IMPLEMENTS / from_subject`を使い、
金商法27条の2と施行令7条をそれぞれ起点にした2件のGraph要求が別々に記録された。2回目のGraph Reviewは
府令2条の5を選び、AgentLoopが同Articleの全5項をOpenSearchから取得した。Graph候補の選別時に取得枠を
空けたまま全件保留する矛盾と、Cycle境界で保留候補を未処理にする矛盾はfixture化し、Promptと共通遷移検証の
双方で回帰防止している。候補の関連性と優先順位はLLMが判断し、Programは既知ID、取得枠、LLM自身が宣言した
状態間の整合だけを検証する。

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

### LR-022 後続CycleでのHypothesis追加

- Cycleを進めるだけではHypothesisを増やさない。
- 検索語や検索先だけを変更する場合は、既存Hypothesisを維持する。
- 既存Hypothesisが反証された場合や、取得本文から別の見立てが必要だと判明した場合は、
  既存Hypothesisを履歴として保持し、同じWorkItemへ新IDのHypothesisを追加できる。
- Observation Integrationは取得本文による既存状態の更新に限定し、新しい見立ての作成は
  次CycleのHypothesis見直し処理で行う。
- Programは既知WorkItem ID、新規Hypothesis ID、重複及び参照整合だけを検証し、
  新しい見立てが必要か、その内容は何かを決めない。
- 「初期仮説の反証後に代替仮説を追加する場合」と「既存仮説のまま検索だけを変更する場合」を
  別fixtureで確認する。

### LR-018 Graph探索の開始判断

- Profile v185の公開買付け実モデルtraceでは、初回検索の19候補に対し、Cycle 1、2、3で
  `Search Selection -> 3 Article取得 -> Cycle Close`を繰り返し、候補数は19、16、13、10と推移した。
  Cycle 4は上限到達によりFinalizationへ進み、IntegrationとGraph要求は一度も実行されなかった。
- Cycle 2、3のDependency Assessmentは下位規範未確認を`needs_action`として認識していた。
  したがって、主因はLLMが下位規範の必要性を理解しなかったことではなく、次の行動を選ぶ
  Integrationがスケジュールされなかったことである。
- `build_solver_context`が、直近Tool結果がなく取得枠が回復した次Cycle開始時に保留候補を
  `required_search_review_request_ids`へ自動設定する。LoopはSearch SelectionをIntegrationより
  優先するため、候補が残る限り再計画が後回しになる。既存のCycle 2回帰テストもこの動作を
  期待値として固定している。
- この実行ではOpenSearchが府令第二条の五等も候補として発見していたため、個別Articleの発見に
  Graphが必須だったとは断定しない。ただし、Graphを使うか、既知候補を取得するか、検索を
  変更するかをSolverが比較する機会がなかった点は、検索経路に依存しない構造的不具合である。
- OpenSearchで得たArticle本文から下位規範または関連規定の確認が必要だとSolverが判断した場合、
  対応するHypothesis、起点Article、関係の種類、方向を指定した`legal_graph_neighbors`を要求できる。
- Graphが必要な状態のfixtureで、実際にLLMへ渡した完成Prompt、Tool説明、未解決Hypothesis、起点候補を確認できる。
- Graphを使わない場合も、Solverが未解決事項に対する別の探索方法または不要と判断した理由を示す。
- ProgramはGraph探索を一律に強制せず、既知ID、selectorの型、件数、重複scopeだけを検証する。
- Graph要求が作られた後の複数1ホップ探索は`LR-003`の完了条件で検証する。

### LR-019 統合契約と意味的行動選択の分離

- 構造契約は、JSON形状、既知ID、型、件数・予算、参照整合、同一成功済み要求の二重実行防止を検証する。
- 検索、本文取得、Graph探索、Cycle切替、回答のどれを次に選ぶかはSolverが判断し、違反別schemaで代替行動を強制しない。
- 意味的に不適切または進捗しない行動は、違反種別ごとの修復経路を増やさず、通常のObservationとしてSolverへ返す。
- 用途別の`*_check.md`は、未解決事項を前進させるか、入力との対応があるか、成功済み要求を反復していないかを確認する短い自己点検にする。Tool引数制約や特定Toolの強制を混在させない。
- Reviewerは既定無効を維持する。採用する場合も全Stepへ挿入せず、総合問題の最終回答前または停滞時だけ実行し、検索や状態更新を指示しない。
- 公開買付け総合問題の固定fixtureで、修復schemaの切替による検索とGraphの往復が発生せず、Solverが通常の次行動または次Cycleを選べる。
- Reviewer無効・有効を同じ代表evalで比較し、必要Article到達、未確認事項の断定、契約修復回数、同一Tool反復、protocol error、時間・tokenを記録する。

2026-08-25の第一段階では、成功済みと完全一致する`legal_search`と
`legal_graph_neighbors`を`ActionRejected`として実行前に止める一方、構造契約の
`contract_feedback`から分離した。次のSolver入力には`action_feedback`として理由と未実行Requestを渡し、
Provider schemaからToolを削除しない。統合と下位規範行動のPromptは、候補取得、Graph、再検索という
固定順をやめ、未確認事項、既知候補、既知起点、関係・方向の説明可能性に応じてSolverが選ぶ形にした。
自己点検は成功条件に直結する3〜5項目へ削減した。Reviewerは既存どおり既定無効・最終回答後だけとし、
停滞時起動は代表evalで必要性を確認するまで追加しない。
APIを使わない全回帰992件で、構造違反の修復、重複行動の実行前棄却、全Toolの継続提示、
既存のReviewer既定無効を確認した。実モデルの総合問題による効果測定は未実施である。

2026-08-25の修正では、次Cycleへ持ち越した検索候補を`search_candidates`と
`fetchable_article_ids`へ残しつつ、`required_search_review_request_ids`へ自動設定しないようにした。
新規の検索結果だけを専用Search Selectionへ送り、次Cycle開始時はIntegrationが次の行動を選ぶ。
保存済みCycle 2 fixtureでは、専用Selectionへ戻らず府令10条を含む3 Articleの本文取得へ進んだ。

同fixtureから検索候補だけを除き、取得済みの金商法27条の2、施行令6条・7条を起点候補として残した
Graph必須状態も`gpt-4o-mini-2024-07-18`で再生した。成功済みOpenSearchの再要求は既存契約が拒否し、
次の判断で`legal_graph_neighbors(article-27_2, semantic_assertion, IMPLEMENTS, from_subject)`を返した。
複数Hypothesisに対する同一Tool実行は1要求へ機械的に統合し、全Hypothesisとの対応を保持した。
ProgramはGraph要否、predicate、方向または候補の法的関連性を決めていない。

2026-08-25に、`non_work_item_requirements`の責務も分離した。「簡潔に」「根拠条文とともに」等は
検索候補本文だけで充足を判定できないため、Search Assessment、候補再選択、本文評価へは投影しない。
検索対象はWorkItem・Hypothesisで決め、同要求はCycle Close、上限時Finalization、最終回答チェックで
回答全体へ適用する。Programは要求の意味的な充足を判定しない。

2026-08-26の例外問題では、下位規範Action Promptが次のToolRequestだけを要求する一方、汎用Solver schemaが
`next`、`start_next_cycle`、状態更新、最終回答も要求していた。実モデルはToolRequestを作りながら
`next=finalize`、`answer=null`を返し、同じ無効出力を再試行した。Profile v304では、この呼出しの出力を
`decision_reason`と`tool_requests`だけに限定した。Programは既存の`needs_action`判断を変更せず、
ToolRequestのWorkItem IDから`action_request_id`を決定的に対応付け、共通`SolverDecision`へ正規化する。

Graph Toolの入力schemaは、`semantic_assertion`では`from_subject / to_subject`とpredicateを、
`explicit_reference / explains`では`outgoing / incoming`と`predicate=null`を許す分岐契約にした。
これにより、LLMが選んだ意味をProgramが補正せず、無効なmode・directionの組合せをProvider出力時点で防ぐ。

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

### LR-013 Provider共通の小型Solver輸送契約

- 同じ処理段階と`SolverContext`から、OpenAI、Anthropic、Ollamaへ同じ意味項目を持つ
  `output_schema.json`を生成する。Provider差で正規契約の項目名や意味を変えない。
- `update_json`、`tool_request_N_json`、Article別名、Evidence対応sidecarを新規出力に使わない。
- schemaへ実行時IDの全件を`enum`として埋め込まず、LLMが返したIDの既知性、重複、件数、参照整合は
  共通validatorで検証する。
- 現在の処理段階で使用できない`SolverDecision`欄をProvider schemaから除外し、欠落欄は正規契約の
  既定値へ復元する。意味上必要な欄を入力容量のために黙って除外しない。
- 代表fixtureで、Provider schemaの文字量、固定指示の文字量、正規化後Decision、契約違反を比較できる。
- HaikuのIntegration呼出しがgrammar complexityの400エラーにならず、共通契約として検証される。

### LR-014 承認済みcheckpointからのモデル差替え再生

- 診断fixtureに、再生開始境界、元Provider・model、承認状態を記録できる。
- fixtureの`SolverContext`を変更せず、指定したProvider・modelで1回の後続Solver処理を実行できる。
- 通常のpytestと成果物生成は外部APIを呼ばず、実モデル再生は明示コマンドだけで実行する。
- `gpt-4o-mini`の成功を正解とはみなさず、Prompt・契約・プログラム境界の切り分けに使う。
  修正の最終確認は同じfixtureをHaikuで再生し、最後にHaikuのE2Eを行う。

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
LR-013  Provider共通の小型Solver輸送契約へ統一
    ↓
LR-014  Haiku承認済みcheckpointをgpt-4o-miniで再生可能にする
    ↓
LR-019  統合の構造契約と意味的な行動選択を分離
    ↓
LR-004  Cycle 1直後の統合fixtureと計測を整備
    ↓
LR-004  総合問題だけを実行し、Cycle 2遷移を確認
    ↓
LR-022  Cycle 2で必要な代替Hypothesisを追加できるようにする
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
| 2026-08-24 | LR-013 / LR-014 共通小型契約・checkpoint再生 | Legal Profile v154。Provider共通schema、実行時IDの事後検証、承認済みcheckpoint再生コマンドを実装。代表Cycle Close schemaは14,494→7,014文字、全895テスト合格。実モデル再生は未実施 | [RUNBOOK](../RUNBOOK.md) |
| 2026-08-25 | Profile v275・公開買付け3問・`gpt-4o-mini` | 公告は必要Article 2/2だが回答要点不足。例外は2/3取得後、総合は0/6取得後に、いずれもCycle 2の`finalize`とTool要求の矛盾で停止。総合では27条の22の2を誤選択 | `tob_announcement_final_answer_incomplete_v275.json`、`tob_exceptions_cycle2_finalize_tool_conflict_v275.json`、`tob_overview_issuer_actor_mismatch_v275.json`、`tob_overview_cycle2_finalize_tool_conflict_v275.json` |
| 2026-08-25 | Profile v291・公開買付け3問・`gpt-4o-mini` | 公告だけ正常完了。3問合計で必要Article 2/11、回答要点4/12。例外と総合は`next=finalize`、`answer=null`、Tool要求ありの矛盾を修復できず`protocol_error`。候補内容評価と主体照合の対応ID不一致、および取得候補の誤選択を確認 | `eval-results/e2e-v291-gpt4omini/`、`eval-results/agent-framework-diagnostics/legal-beb78a10fd89425eb78de503e5829a93.jsonl`、`legal-07f80109b5074079851b241bccfb32ce.jsonl`、`legal-985c7b715e12438cbf2404d0257625a6.jsonl` |
### 2026-08-25: 質問分解と仮説立案の主体表現を分離

- WorkItemの主体情報を`action_actor`、`target_actor`、`actor_relation`へ分離し、Hypothesisには重複保存しない。
- 対象関連主体がない場合を`not_applicable`で表せるようにし、`unknown`と区別する。
- 一般的な制度質問で質問者を行為者と推測せず、文書・届出・通知・公告を主体として扱わない。
- 仮説は質問の言い換えで終わらず、対象に応じた法的判定軸、例外類型、手続行為を少なくとも一つ示す。
- v271の隔離実モデル診断では、根拠提示要求のWorkItem化と、主たる行為者・付随手続実施者の誤分離が残った。
  v272では意味契約を増やさず、両方を質問分解の手順と完了確認へ移し、仮説の抽象表現には短い対比例を追加した。
- v272の例外設問では、人数条件として登場する所有者を行為者と誤認した。v273では、分割後も主たる行為者を保持し、
  人数・割合・属性の条件として登場するだけの者は対象関連主体とする一般ルールを追加した。
- 上記の対象関連主体と関係ラベルはv275で廃止した。質問へ既に残る対象情報を再分類するより、
  `question`、`action_actor`、Hypothesisを候補と直接照合する方が小さく、2分野の隔離診断で同じ選別結果を得たためである。
