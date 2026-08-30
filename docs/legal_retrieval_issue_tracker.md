# 法令検索 課題管理

> 更新日: 2026-08-30
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
| `LR-001` | P0 | 対応中 | 質問から必要な検索仮説を漏れなく作る | Profile v289でHypothesisがないWorkItemを1件ずつ処理する方式へ変更した際、元質問、`action_actor`及び`gaps`をHypothesis生成契約から落としたため、主体の推測と抽象的な命題が再発した。v300で`statement`をWorkItemへの回答を構成する回答項目として定義し、追加の未確認事項がない`gaps=[]`を許可した。v301でWorkItem最大24件、Hypothesis最大4件／WorkItemという意味上の上限をProvider schemaから削除した。v302で分割単位を「法令本文の一つの規定内容で個別に支持又は否定できる命題」と明確化した。v303では1 WorkItemずつ処理するHypothesis生成入力から元質問全体を外し、WorkItemを唯一の作業範囲とした。`gpt-4o-mini`の隔離実行では総合の成立条件へ他WorkItemの論点が混入する問題は解消した。Profile v379のLuna総合問題では、質問の「必要な手続」を質問にない7内訳へ展開して未確認範囲を広げたため、v380で広い要求の抽象度を保つよう質問分解Promptを補った。v380のLuna `high`隔離実行は4 WorkItemを維持し、「必要な手続」を内訳へ展開せず合格した。`action_actor`はWorkItemだけを正本とし、Hypothesisには複製しない | 公開買付けの公告・例外・総合WorkItemを隔離実行し、WorkItem外の論点や質問にない内訳を追加せず、検索対象を選べるHypothesisが生成されることを確認する |
| `LR-002` | P0 | 対応中 | 法令検索表現を作り、同一Cycle内でOpenSearchを適切に再検索する | 検索要求作成だけの隔離診断を追加した。Profile v290で`purpose`を確認内容、`query`を短い法令用語の組合せとして分離した。例外問題では直接例外と委任を1検索にまとめて法令表現を生成できたが、総合問題の検索語はまだ抽象的である | 個別語をPromptへ追加せず、LR-001のHypothesis具体性と実際のOpenSearch候補を合わせて評価する |
| `LR-003` | P0 | 完了 | Graph由来Articleを起点に連続1ホップ探索する | `gpt-4o-mini`の実モデルtrace v7で、金商法27条の2→施行令7条、施行令7条→府令2条の5を別々の1ホップGraph要求として実行し、府令本文取得後にCycle 1で正常完了した | `lr_003_second_hop_integration_v1.json`、`lr_003_second_hop_graph_review_v1.json`、`lr_003_cycle_close_deferred_frontiers_v1.json`を回帰fixtureとして維持する |
| `LR-004` | P0 | 完了 | 複合問題の統合Decisionを成立させ、次の探索または完了へ進む | Profile v388で、逐次統合済みEvidenceをCycle Closeで再統合せず、Cycle遷移だけを判断するよう修正した。最終化はProvider共通の小型schemaを使い、処理済みGraph ledgerを除外する。Luna `high`総合問題ではCycle 2境界が約95秒から約22秒へ短縮した。v389では最終化へ70.6秒を与えてもtimeoutしたため、v390で法令アプリの最終化予約を35秒から90秒へ増やした | v390の実モデル総合問題は2 Cycle・17モデル呼出しで正常完了した。必要Article 4/4と関連する府令本文を取得し、入力28,076 token、出力6,468 tokenの最終回答を引用付きで生成した |
| `LR-005` | P1 | 検証待ち | Haikuと`gpt-4o-mini`の役割を分けて評価する | `gpt-4o-mini`は単一責務の隔離Promptでは質問分解・具体的Hypothesis生成ができたが、実際の長い入力と複数処理ではHypothesisの抽象化、論点の結合、主体・候補対応の揺れが残った。過去の`protocol_error`、重複行動、ID不整合の多くはPrompt・契約・実装の不備であり、GPT固有の失敗とは扱わない。現在はHaikuを法令検索の主評価モデル、`gpt-4o-mini`を承認済みfixture以降の安価な構造デバッグ用とする | 同じ承認済みcheckpointを両モデルで各1回再生し、意味判断差とProvider・契約不備を分離して記録する。OpenAIの429は回答品質と別に扱う |
| `LR-006` | P1 | 要設計 | 意味分類coverage不足時にも逆引き検索爆発と取りこぼしを両立させる | publish済み意味関係ならselectorで絞れるが、未分類範囲でraw `REFERENCES/to_subject`を使うと高fan-inになる | 限定fallbackの発動条件、scope上限、coverage不足の表示、限定回答条件を決める |
| `LR-007` | P1 | 未着手 | CycleとStepを再開可能な状態として保存する | WorkItem、Hypothesis、Evidence、Graph review履歴はあるが、目標の`CycleRecord / StepRecord / ExplorationState`は未実装 | Tool観察後の中断から同じStepを再開し、別Cycleとして数えないfixtureを通す |
| `LR-008` | P1 | 対応中 | status、Provider schema、Prompt、遷移検証の修正漏れを防ぐ | 主要Solver項目の`Field.description`から共通用語集を生成した。初回3 Stepは`ResearchStepInput`の実投影項目と入れ子要素から`input_contract`を生成し、基準Providerの完成成果物を本番生成結果と比較する。他Providerとの意味契約一致は重複ファイルを持たずテストする。Toolの用途・入力Schema・戻り値は`ToolDefinition`から`available_tools`へ投影する | 全statusのowner・遷移・永続化versionを説明付き正本へ集約し、未定義statusで契約テストが失敗するようにする |
| `LR-009` | P1 | 対応中 | Tool数、本文取得数、Cycle数の用語と設定を一致させる | Profile v429の現行既定は最大5 Cycle、1 step最大5 Tool要求、1 Cycle本文5 Article。設定、型制約、Compose、設計書を同じ値へ更新した | 過去資料に残る旧値を、履歴か現行仕様か区別して確認する |
| `LR-010` | P2 | 停止中 | 全件Relation分類を安全に再開する | 全件Runは1,615 checkpointで参照scopeと改正法構造の問題が見つかり停止中 | shadow差分監査、影響候補特定、再開条件合格後に同じsnapshot・checkpointから再開する |
| `LR-011` | P2 | 要設計 | 自治体の条例・規則・要綱を使う小規模データセットを決める | 自治体向け利用像はあるが、データセットは未決定 | 上位法令改正→条例→規則→要綱の逆引きを検証できる最小集合を利用者と決める |
| `LR-012` | P0 | 完了 | LLMの固定指示、実行時入力、最終契約を分離し、レビュー可能な成果物として出力する | API送信・診断・成果物出力が同じ`RenderedModelCall`を使用し、snapshotでは呼出し別ファイルを出力する。代表research fixtureはOpenAI・Anthropic・Ollamaの基準成果物を持つ | 固定指示hash、動的入力hash、両schema、実送信hashの回帰テストと全887テストに合格 |
| `LR-013` | P0 | 検証待ち | Provider共通の小さいSolver輸送契約へ統一する | v154で全Providerを同じ処理段階別schemaへ統一し、Anthropic専用sidecarを新規経路から外した。実行時IDはenumへ複製せず共通validatorで検証する。代表Cycle Close schemaは14,494文字から7,014文字へ減少し、全895テストに合格した | 同じcheckpointをHaikuで再生し、Integrationがgrammar complexityの400エラーにならないことを確認する |
| `LR-014` | P1 | 検証待ち | Haikuで承認した中間状態から安価なモデルで後続処理を再生する | checkpointの明示承認をpromotion時に記録し、指定Provider・modelで1回のSolver処理を再生する`replay_agent_checkpoint.py`を実装した。APIを使わない単体テストは合格した | Haikuの正常中間状態を承認済みfixtureへ昇格し、`gpt-4o-mini`、同じcheckpointのHaikuの順に実モデル再生する |
| `LR-015` | P0 | 検証待ち | 初回Researchを単一責務のStepへ分け、要求をWorkItemとそれ以外へ欠落なく分解する | Profile v156で同一Cycle内の要求分解、仮説立案、検索要求作成を実装した。各完成Promptは単独で読めるH1を持ち、処理順のStep番号を含めない。一時的な段階比較コードを整理した後の全900テストに合格した | 別分野fixtureと公開買付けE2Eを`gpt-4o-mini`で確認し、最終的にHaikuで品質確認する |
| `LR-016` | P0 | 完了 | Tool観察の意味統合とCycle Closeの境界を保つ | v376でObservation IntegrationとDependency AssessmentをEvidence Integrationへ統合した。全open WorkItemの一括処理は総合問題の10 Hypothesis・47 Evidenceで16,384出力tokenを使い切ったため、意味的に独立したWorkItemごとの統合判断へ戻し、最大4件を並列実行する。WorkItem内の本文評価と下位規範判断は分けず、Programは差分結合とWorkItem完了を機械処理する。Cycle Closeは更新済み状態から遷移と回答だけを扱う | Profile v379・Luna `high`の総合問題で、WorkItem別の本文部分確認、下位規範状態、WorkItem機械導出、3 Cycleの遷移が契約違反なく完了した |
| `LR-017` | P0 | 対応中 | 検索候補の規律主体と行為対象を安定して区別する | Profile v310の隔離診断で、検索抜粋と`action_actor=不明`から行為者を確定する結果が実行ごとに揺れた。Profile v311では本文取得前の専用主体照合を廃止し、内容面でHypothesisに対応する候補を本文取得可能にした。旧主体照合値は保存互換だけに残し、Solver入力と候補選択条件には使わない | 公開買付け3問で、主体未確定の必要Articleが本文取得前に除外されず、取得本文を評価するLLMが主体の一致・分岐を判断できることを確認する。利用者確認と条件付き回答はLR-020で扱う |
| `LR-018` | P0 | 完了 | Graph探索が必要な未解決事項があってもSolverがGraph検索を要求しない | 次Cycle開始時の保留OpenSearch候補を自動Search Selectionへ戻さず、Integrationで既知候補・Graph・再検索を比較するよう修正した。Graph selectorのmode・predicate・directionを分岐schemaで拘束し、同一Graph要求と本文取得要求は選択内容を保ったまま輸送時に統合する | Cycle 2 fixtureで府令10条を含む既知候補の本文取得へ進むこと、および候補を除いたGraph必須状態で`27条の2 / IMPLEMENTS / from_subject`の1要求を返し共通契約を通過することを`gpt-4o-mini-2024-07-18`で確認済み |
| `LR-019` | P0 | 検証待ち | 統合の意味的な行動選択を違反別契約からSolver loopへ戻す | v373で、Promptは処理上限内のWorkItem選択を許す一方、validatorが全`needs_action`への同時Graph要求を強制する不整合を再現した。v374で全件同時強制を削除し、LLMが今回選んだWorkItemだけを進め、残りを後続stepへ保持する契約へ統一した。Haiku総合問題でGraph要求と後続Graph Reviewが実行され、`protocol_error`は解消した | 公告・例外の回帰と、総合問題で複数の`needs_action`が次stepへ欠落せず残ることを確認する |
| `LR-020` | P1 | 要設計 | 検索又は結論を変える質問の欠落・曖昧さを一方的に補わず、利用者へ確認する | 現在は行為者が不明でも`action_actor=不明`として調査を続ける。これは一般論の質問には適切だが、確認対象となる行為や主体の欠落により複数の検索経路・法的結論が成立する場合を、検索前に止める契約がない。検索後に候補本文から異なる規律主体が判明する場合もある | まず検索前の独立した質問確認をStreamlitへ追加し、必要な場合だけ確認・質問文修正・利用者確認を行う。検索後の分岐で必要となるCaseの確認待ち・再開は次段階として設計する |
| `LR-021` | P0 | 対応中 | 検索候補の内容評価と取得選択を整合させる | v376でSearch AssessmentとReselectionを一つのSearch Selectionへ統合した。LLMは全候補を比較するが、出力は選択した最大5件の内容評価・対応Hypothesis・選択理由だけとする。Programはこの単一出力を保存用評価と本文取得選択へ分け、非選択IDを入力候補との差集合として保留する。意味上の採否はLLM、既知ID・件数・構造変換はProgramが扱う | Luna `high`の総合問題で、対応Hypothesisと選択Articleが同じ判断内で整合し、必要候補が後段で脱落しないことを確認する |
| `LR-022` | P0 | 未着手 | 後続Cycleで既存WorkItemへ代替Hypothesisを追加する | 設計ではHypothesisの`statement`を別の意味へ上書きせず、見立てを変える場合は新しいHypothesisを追加する。しかし現行の段階別経路は、Hypothesisが1件もないopen WorkItemだけを生成処理へ送り、本文評価は既存Hypothesisの更新だけを許す。そのため、初期仮説が反証された場合も、同じWorkItemへ新しい見立てを追加して次Cycleで仕切り直せない | H1が`contradicted`または新しい規律構造が判明したfixtureで、H1と根拠を履歴として保持し、Cycle 2で同じWorkItemへ新IDのH2を追加できることを確認する。検索方法だけを変える場合は不要なHypothesisを追加しない |
| `LR-023` | P0 | 完了（Profile v426） | Hypothesisに合うGraph方向と候補の対応先を安定して選ぶ | Profile v425では、具体化規定を探す4件の`IMPLEMENTS`要求が全て`to_subject`となった。直近コミットのProfile v395は後続Cycleで正方向へ回復できたが、最初の要求では同じ誤りがあった。Profile v426では、各意味関係のSUBJECT / OBJECT役割と方向の対応を共通Tool PromptとTool schemaへ直接記載し、Toolを選べるEvidence Integrationにも同じ共通Promptを合成した。法的意味の選択はLLM、既知値と構造の検証はProgramが担当する | v425の失敗fixtureをLuna `high`で隔離再生し、4要求全てが修復なしで`IMPLEMENTS / from_subject`となった。総合E2Eでも4要求全て正方向となり、府令2条の5・10条を取得して11/11に合格した。物理`REFERENCES`の探索目的変換とGraph候補のHypothesis再対応付けは別契約として維持する |
| `LR-024` | P0 | 完了 | Hypothesisが支持された内容と未確認事項を同時に保持する | Haiku・Profile v375では、H-3は府令2条の5、H-4は府令10条を未取得だった。それぞれ上位規定から一部内容を確認できたが、`judgment=supported`への更新と同時に`gaps=[]`となった。WorkItemは下位規範Dependencyにより`open`を維持したものの、Hypothesis単体では必要な具体的内容を確認済みのように見える不整合が残った | Profile v383で`judgment`をstatementの判定、`gaps`をWorkItemへの回答に必要な未確認事項として契約・Promptへ明記した。Evidence Integrationでは下位規範状態を先に判断し、`terminal_text_missing`と同時に更新したWorkItemの全Hypothesisで`gaps=[]`となる矛盾だけをProgramが拒否する。固定fixtureで誤出力の拒否と、`supported`のまま府令の具体的条件を`gaps`へ保持する更新を確認した |
| `LR-025` | P0 | 完了 | 取得済みEvidenceとHypothesisの対応付けを後続処理へ引き継ぐ | Profile v382で`HypothesisUpdate.evidence_ids`を今回新たに対応付けたEvidenceの差分とし、CaseStore保存時とCycle Close向け投影時の両方で既存対応へ追記する共通処理へ変更した。意味的な対応付けはLLM、既存対応の保持・既知ID検証・重複除去はProgramが担当する | 前CycleのEvidenceをLLMが再出力せず、現在CycleのEvidenceだけを返すfixtureで、両方の対応が後続入力とCaseStoreに保持されることを確認済み |
| `LR-026` | P0 | 完了 | Graphで選択したArticle本文を、残りのGraph候補より先に統合する | Profile v383の総合問題では、府令2条の5を本文取得した後も未処理Graph候補のレビューを続けたため、取得本文をHypothesisへ統合できないまま時間上限へ到達した。Profile v384では未統合の取得本文を機械的に検出し、Graph・検索候補をCaseStoreに保持したままEvidence Integrationを優先する。統合後は処理済みIDを除いた未処理候補を再投影する。候補順序は固定せず、未処理候補はCycle境界でも保持する | 回帰テストに加え、Luna `high`の総合問題で`graph_selection → observation_integration`を2回連続して確認した。府令2条の5・10条を含む必要Article 6/6を取得・統合した。最終回答のtimeoutはLR-004で扱う |
| `LR-027` | P0 | 回帰修正済み・実モデル再検証待ち | 同じCycleで一つのHypothesisのGraph候補を追い続けない | Graph ReviewをWorkItem・Hypothesis単位に分け、同じ単位でGraph由来本文を1件取得したら、そのCycleでは別Hypothesisを処理する。同じ単位の未処理候補はIDを保持して次Cycleへ戻す。v415ではReviewが同じ`h-1`から3件を選び、さらに`defer`候補を通常の本文取得から同一Cycle中に取得できたため、取得枠5件を`h-1`中心に消費した。v416実測では上限1件で選択できたが、取得成功からEvidence統合までの間だけ取得済み単位へ反映されず、同じObservation Integrationから後続候補を要求できる隙間が残った | Profile v417でGraph Review本文取得の成功時点を件数管理上の完了とし、Evidence統合前でも同じHypothesisの後続Graph候補を`fetchable_article_ids`から除外する。候補の採否はLLM、取得成功・Cycle・ID・件数はProgramが扱う。fixture後、Luna `high`総合問題で再検証する |
| `LR-028` | P0 | 完了 | 同じGraph Articleペアを異なる探索要求から取得してもEvidence IDが衝突しない | Graph navigation Evidence IDはArticleペアから決定する一方、Evidence本文には関係種別と向きが含まれていた。同じペアを別のGraph selectorで再取得すると、同じIDで内容が異なり、CaseStoreが`tool returned conflicting evidence ID`として拒否した。Profile v389ではArticleペアIDを共通接頭辞として保ち、関係内容のhashをEvidence IDへ加えた | 同じArticleペアをREFERENCESとRelationAssertionから取得する回帰とLuna `high`総合問題に合格した。実モデルはGraph衝突なくCycle 3の最終化まで到達した |
| `LR-029` | P0 | 実モデル確認済み・品質改善継続 | `supported`かつ`gaps`ありのHypothesisを後続探索と限定回答へ一貫して引き継ぐ | LR-024で`judgment`と`gaps`を独立させた後も、Search Planning・Search Selectionは`judgment=unresolved`だけを未確認対象としていた。Profile v415では別の回帰として、open WorkItemの判定済みHypothesisにEvidenceがあってもCycle Closeへ本文を提示せず、モデルが返した引用も解決済みWorkItemの必須IDだけで上書きした。そのため総合問題が取得済み根拠を「提示されていない」と回答した | Profile v416では判定済みHypothesisと解決済みDependencyの本文をCycle Closeへ提示し、モデルが限定回答で選んだ確認済み引用を保持した上で、解決済みWorkItemの必須引用だけを機械追記する。Luna `high`総合実測で`answerCompleteness=limited`でも33件の取得済み根拠を引用し、確認済みの条件・範囲・例外・手続を回答できた。未確認部分の法的判断はLLMに残す |
| `LR-030` | P0 | 実装済み（意味関係実モデル合格） | Hypothesisに合う意味関係をGraph探索へ使い、意味分類の未被覆時だけ明示参照へフォールバックする | Profile v392の追加Lv.3設問では、Graphに`IMPLEMENTS / EXCEPTION_TO / USES_DEFINITION / OVERRIDES`が登録済みでも、実行したGraph要求は全て`explicit_reference`だった。公告方法の設問ではGraph 4回のうち最初の府令9条発見後は取得済み条文へ戻り、データセットに存在しない準用先をOpenSearchで6回追加検索した。Graph処理自体は合計約0.05秒だが、26回のLLM呼出しに約322.5秒を要した | Profile v393で、下位規範Actionの`explicit_reference`固定schemaを廃止した。SolverはHypothesisに対応するpredicateを説明できる場合に`semantic_assertion`を先に選び、新候補がない場合だけ明示参照へ切り替える。Programは意味を選ばず、各Graph要求の返却Article ID、新規Article ID及び同一scope履歴を提示する。Luna `high`総合問題はGraph 4回を全て意味関係・正方向で行い11/11。意味分類coverageのscope別manifestは`LR-006`で扱う |
| `LR-031` | P1 | 要設計 | 新Frameworkの`explains`をガイド`Document`から法令`Article`を発見できる契約へ整合させる | `EXPLAINS`はガイドの条文注釈・対応表が明示した`Document → Article`であり、法令本文間の`REFERENCES`とは別の索引である。一方、`legal_graph_neighbors`の共通入力は起点を`article_ids`とし、`GraphClient.article_relations_touching`も両端を`Article / Paragraph / Item`へ限定するため、正規の`Document → Article`を新Frameworkの`explains`モードから取得できない。旧Guidance Laneには`document_id`起点の探索がある | `explains`の利用範囲を決め、少なくともガイド`Document`から明示対応する法令`Article`を1ホップで取得し、選択したArticle本文をOpenSearchから取得するfixtureを通す。Articleからガイドへの逆引きを提供する場合は、Article本文取得とは別の候補型・取得経路を設計する。単なる言及を`EXPLAINS`へ昇格させない |
| `LR-032` | P1 | 完了 | Graphの物理`REFERENCES`探索を名称と契約だけで理解できるようにする | 旧`explicit_reference`は、本文の実行時解析、利用者が明示した参照、物理`REFERENCES`探索のいずれにも読めた。Profile v395でGraph modeを`reference_edges`へ変更し、seed済み`REFERENCES`を1ホップたどる処理だとschema descriptionとPromptへ明記した。`follow_reference_in_text / find_articles_referencing_this`は二方向の探索目的として維持した。実装コミットは`e58e2ac` | 全1074テストに合格した。Luna `high`総合問題では`reference_edges`を2回実行して正常完了し、旧modeは完成schema、応答及びtraceに現れなかった。探索フロー上のfallback条件はmodeの意味へ混ぜず、別の手順として維持する。意味関係の方向選択は変更対象外であり、再発した誤方向は`LR-023`で継続する |
| `LR-033` | P1 | 構造修正・総合再検証中 | 本文評価と次行動選択の重複LLM呼出しを統合し、回答時間を短縮する | Profile v395のLuna `high`では、本文を`observation_integration`で評価した直後に`integration`が同じ観察を読み直していた。v409で最大3 WorkItemを一組として処理し、複数組を並列化したが、取得本文と直接関係しないWorkItemも同じ呼出しで評価するため、未取得事項の解消やEvidenceの誤対応付けを招く可能性が残った。Programは法的対応付けや優先順位を選ばず、既知ID、schema、件数、時間及び状態遷移だけを検証する | Profile v422でEvidence Integrationを1 WorkItemと1つのLLM呼出しに限定し、対象が複数ある場合は最大4呼出しを並列実行する。Profile v423では、新しいTool結果を受けた同一Hypothesisの再評価は保ち、同一WorkItemで処理済みの`load_evidence`本文だけを再提示の対象から外す。同一scopeの成功済み再要求は契約境界でも拒否し、監査は新しいTool結果がない反復だけを警告する。法的意味の判断は引き続きLLMが行い、ProgramはWorkItem、Hypothesis、Evidenceの既知IDと成功済みscopeのみを検証する。 |
| `LR-035` | P1 | 完了（Profile v429） | LLMへ送る指示・入力・出力契約を、1つのレビュー用成果物で確認できるようにする | `snapshot`の各Model呼出しへ`complete_request.json`を生成する。実送信Prompt、Providerへ別送信した出力schema、正規化後schema、model、Provider及び各hashを一つに収録し、schemaをPrompt本文とは表示しない。診断JSONLの`completeRequestPath`から直接参照できる | 生成内容が既存`request.txt`、各schema、manifestのhashと一致する回帰テストを維持する。診断出力だけの変更であり、LLM入力とtoken数は変えない |
| `LR-036` | P1 | 精度修正済み・速度改善継続（Profile v430） | 各WorkItemを専属セッションへ固定し、異なるWorkItemだけを並列処理する | `case_id + work_item_id`の論理session、turn継続、親の共通締切、完了差分の入力順merge、WorkItem別Evidence scopeを実装した。v428では本文取得・Graph・OpenSearchが同時完了した際、本文統合のため一時的に隠した探索結果まで処理済みにして、府令10条候補を失った。v430では本文統合へ実際に提示したTool結果だけを処理済みにし、同時完了した探索結果を次の専用Reviewへ残す | 最小fixtureと全1104テストに合格した。Luna `high`総合問題は府令10条をCycle 1で本文取得し11/11相当へ回復したが、2 Cycle・358.9秒でv428より123.6秒遅いため、速度改善は未完了とする |

### 3.1 LR-016 Tool観察とCycle Closeの単一責務化

新しいArticle本文を取得した後は、取得枠を使い切っていても、まず同じSolverのEvidence Integration
で1つのWorkItemに属するHypothesisと下位規範確認を同時に更新する。対象WorkItemが複数ある場合は、WorkItem単位の呼出しを最大4件並列実行する。
Adapterはその差分を入力順に結合する。AdapterはHypothesis、Evidence、Dependency状態から
WorkItem完了差分を機械導出して更新後のread modelへ投影し、続けてCycle Closeを呼ぶ。
Cycle Closeは本文評価やTool選択を兼務せず、
通常完了または次Cycleへの引継ぎだけを判断する。2つの結果は1件の`SolverDecision`へ正規化し、共通validatorを
通過した後にCaseStoreへ一度だけ適用する。

Cycle CloseのProvider入力から、`fetchable_article_ids`、検索・Graph候補、Tool定義、汎用`update`、
本文評価用の分岐を除外する。入力には本文評価を反映済みのWorkItem・Hypothesis、引継ぎ候補となる
既知Evidence IDを投影し、出力は通常完了または次Cycle開始の小さいdiscriminated contractにする。Programは既知ID、重複、
件数、分岐の排他性だけを検証する。Evidence採否とDependencyの意味はSolver、WorkItem進捗とCycle遷移は
Programが担当し、次Cycleの引継ぎ説明と最終回答はSolverが作る。

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

Profile v310の実モデル隔離診断では、`action_actor=不明`の同じ入力に対する主体照合が、ほぼ全件一致から
全件不一致まで変動した。検索抜粋だけでの主体確定を本文取得の門番にする設計が不安定だったため、Profile v311で
専用主体照合を検索経路から削除した。内容面で対応した候補は、主体が未確定でも本文取得できる。主体の一致、
別主体の規律又は質問自体の曖昧さは、取得本文を評価するLLMが判断する。

本文取得枠を5件へ変更した実モデル確認では、Reselectionが同じArticleを複数回返し、Provider schemaは通過するが
内部の一意性契約で停止した。Profile v312では同じArticle IDの輸送上の重複を提示順の1件へ正規化し、
`matched_hypothesis_ids`は和集合で保持する。これはArticleの採否を変更する意味判断ではなく、集合として扱う
選択結果の構造正規化である。Promptと完了確認にも、同じArticleを最大1回だけ返すことを明記する。

同じ確認で、Cycle全体の本文取得上限5件を、1回の`fetch_articles`上限4件としてReselectionへ提示する
不整合も判明した。Profile v313ではTool定義の`article_ids.maxItems`を正本として、現在のCycle残容量との
小さい方を`current_fetch_request_capacity`として提示し、Provider schemaも同じ値で制約した。
その後の総合問題で、4件を取得した時点ではCycleに1件分の余裕があるのに、強い手続候補が初回選別から
落ちることを確認した。Profile v357では、Cycle上限と1回の`fetch_articles`上限をともに5件へ揃えた。
Graphの1要求あたり起点Article上限4件は別の制約として維持する。

### 3.4 LR-020 曖昧な質問の確認

同じ質問から複数の法的確認事項・主体関係が合理的に成立し、その違いが検索又は回答を変える場合は、
Solverが一つへ決め打ちせず、解釈候補と相違点を利用者へ示して確認を求める。例えば
「公開買付けによらずに買い付けられる主な場合と、所有者が少数である場合の条件」は、後半を
買付けの例外条件として読むか、所有者数の条件を独立して問うかでWorkItemと主体が変わる。

確認は、値が空であることではなく、その欠落が結果へ与える影響を基準にする。単に主語が省略されている、
又は制度の一般的説明を求めているだけなら処理を続ける。確認対象となる行為や主体等が欠けており、
その補い方によって検索経路又は法的結論が変わり、元の質問から一つに決められない場合だけ確認を求める。
「法律行為」は民法上の意味に限定され得るため、本契約ではより広い「確認対象となる行為」を使う。

意味上の欠落・曖昧さ、解釈候補、確認質問及び修正後の質問はLLMが判断する。Programは意味を選ばず、
出力形式、選択肢ID、件数及び利用者の選択との対応だけを検証する。

#### 検索前の最小実装

最初はCaseの中断・再開を実装せず、質問分解より前に独立した質問確認を置く。質問がそのまま調査可能なら
原文を変更せず現行の質問分解へ渡す。確認が必要なら、Streamlitで確認質問、選択肢及び自由入力欄を表示し、
LLMが作った修正版を既存の質問入力欄へ戻す。利用者が修正版を確認してから通常の`/answer`を実行する。

```text
質問入力
  → 質問確認LLM
      → ready: 元の質問で調査開始
      → clarification_required
          → Streamlitで確認質問を表示
          → 利用者が選択又は自由入力
          → LLMが質問を修正
          → 利用者が修正版を確認
          → 調査開始
```

質問確認は新しいAgentを増やさず、Solverの独立した処理modeとする。最小出力契約は次とする。

```text
QuestionReadiness
├─ decision: ready | clarification_required
├─ reason
├─ clarification_question
└─ choices[]
   ├─ choice_id
   ├─ label
   └─ refined_question
```

この段階では検索結果、WorkItem、Hypothesis又はCaseStoreを入力しない。確認を繰り返しても調査可能な質問に
ならない場合に備え、UIは利用者が修正版を直接編集できるようにする。

#### 検索後の確認

質問時点では確定できなくても、候補本文を取得して初めて主体や法的効果の分岐が明らかになる場合がある。
この場合は、主体を質問分解時に必ず確定させず、まず行為、対象、条件から主体を限定せず検索する。
候補本文に現れた規律主体をLLMが整理し、主体の違いが法的結論を変える場合だけ、質問と既知情報で
一つへ確定できるか判断する。

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
検索後の確認では、Programが確認待ちを保存し、利用者回答を同じCaseへ追加して該当Hypothesisから再開する。
これは検索前の最小実装が安定した後に追加する。

評価時の誤差と機能課題を混同しないため、`tob-exceptions`設問は「主な適用除外」と、その一類型である
「所有者が少数の場合」を重ねて問わない。所有者が少数の場合に、公開買付けによらずに買い付けられる
具体的条件だけを尋ねる。必要根拠は金商法27条の2、施行令7条、公開買付府令2条の5のままとする。
これは評価設問の焦点を一つにしたものであり、曖昧なエンドユーザー質問への確認機能を実装したものではない。

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

Profile v314では、この最後の不整合を契約構造から除いた。Cycle CloseのProvider出力から重複した
`outcome`を削除した。v330ではさらにObservation IntegrationからWorkItem更新を外し、Hypothesisと
Dependency状態からProgramが導出したopen WorkItemの有無と次Cycle可否により、`required_transition`を
決定的に導出する。LLMは指定された遷移に応じて引継ぎ内容または回答だけを返す。

## 4. 最優先分析: 複合問題の統合

### 4.1 本書でいう統合

現行の統合は、取得済み本文を要約して最終回答を書く処理だけではない。Tool実行後の1回の
`SolverDecision`で、次をまとめて行っている。

```text
ToolResultとArticle全文
        ↓
EvidenceをHypothesisへ対応付ける
        ↓
SolverがHypothesisを更新する
        ↓
Solverが下位規範を評価し、ProgramがWorkItem進捗を導出する
        ↓
同一Cycleで追加調査 / 次Cycleへ移る / 最終回答、のいずれかを選ぶ
        ↓
選択に対応するToolRequest、引継ぎ情報または引用付き回答を返す
```

したがって「統合失敗」は、最終回答の文章生成失敗だけでなく、Evidence対応付け、Hypothesis更新、
WorkItem進捗の導出、
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

- 現行`agent-ui/example_questions.py`で定義した`tob-overview`の必要Article 4件を、
  すべてArticle全文として取得する。
- resolved WorkItemの根拠Hypothesisと回答citationが一致する。
- open WorkItemまたはunresolved Hypothesisが残る通常実行を完全回答としてfinalizeしない。
- 時間上限に達した場合は、未確認事項を隠さず限定回答として示す。
- 契約修復回数、入力・出力token、Tool時間、Cycleごとの取得Articleをtraceから説明できる。

Profile v387のLuna `high`総合問題では必要Article 4/4を取得したが、Cycle 2終了時の
`cycle_close_evidence_integration_checkpoint_timeout`が約95秒を消費した。記録上の入力は49,111 token、
出力は11,651 tokenであり、Case全体の420秒上限到達後に最終回答用Solver入力だけが作成された。
したがって、現在の調査対象は回答文生成ではなく、その直前に行うEvidence統合チェックである。

診断checkpointを再生すると、Cycle 2終了時のArticle取得ToolResultはすべて
`integrated_tool_result_request_ids`へ記録済みで、未統合Article取得は0件だった。一方、
`_solve_cycle_close`は無条件に`_solve_observation_integrations`を呼び、直近Tool結果が空の場合は
全open WorkItemを対象にする。そのため、既に逐次統合した4 WorkItemを再び4並列で評価した。
この再評価によるWorkItem状態、gap及び下位規範状態の変更はなく、Hypothesis 1件へ既知Article内の
Evidence IDを追加しただけだった。

時間配分にも次の問題がある。

- `cycle_close_reserve_sec=15`はCycle Close呼出しの上限ではなく、Cycle Closeへ切り替える時刻の判定にしか使わない。
- 実際のCycle Closeには、全体残り時間から`finalization_reserve_sec=35`を引いた約94秒が与えられ、その全時間を再統合が消費した。
- その後の最終化入力はPrompt 62,066文字、schema 10,489文字、Evidence 42件で、残り約34秒しかなかった。
- 過去のLuna実測では同種の最終化に約51秒かかった例があり、35秒固定予約は`high`設定に不足し得る。

Profile v388では、Cycle Closeを逐次統合済み状態からの遷移判断だけに変更した。未統合Articleがある場合は
既存のEvidence Integrationを先に完了させる。最終化にはProvider共通の専用小型契約を使い、空の`update`、
`frontier_re_adoptions`及び処理済みGraph ledgerを入力・schemaから除いた。これによりCycle 2境界は約22秒、
最終化入力は26,558 tokenとなり、約53秒で限定回答本文を生成した。

この最終化では、本文が引用していた府令2条の4のEvidence ID 1件を`citation_ids`から落としたため、契約修復へ
進んだ。意味判断を再実行せず、解決済みWorkItemへ既に対応付けた根拠IDをProgramが最終回答へ機械反映するよう
修正し、回帰テストを追加した。後続実行で発生したGraph Evidence ID衝突はLR-028として分離した。

Profile v389ではGraph Evidence ID衝突を解消して最終化へ到達したが、最終化入力48,090文字、schema 5,142文字に
対して70.6秒を与えてもLuna `high`がtimeoutした。Profile v390では汎用Frameworkの既定値を変えず、法令アプリの
最終化予約だけを90秒へ変更した。実モデル総合問題は最終化へ113.2秒を確保し、入力28,076 token、
出力6,468 tokenの引用付き回答を生成して正常完了した。

### LR-005 GPT-4o mini実モデル評価

- `gpt-4o-mini`は、質問分解やHypothesis生成だけを行う隔離Promptでは、具体的な法的命題を作成できた。
- 一方、実際の長い入力で複数の判断を同時に求めると、Hypothesisの抽象化、独立した論点の結合、
  行為者及び候補とHypothesisの対応付けに揺れがあった。現時点ではHaikuより法令検索全体の安定性が低い。
- 過去に発生した`protocol_error`、同一行動の再要求、既知IDの不整合、Provider schemaの肥大化は、
  Prompt、契約又は実装にも原因があった。これらを`gpt-4o-mini`固有の能力不足として扱わない。
- OpenAI APIの429は利用上限又はレート制限であり、回答品質の評価から分けて記録する。
- 当面はHaikuを法令検索の主評価モデルとする。`gpt-4o-mini`は、Haikuで承認したfixture又はcheckpoint以降を
  安価に再生し、構造、契約及び状態遷移をデバッグする用途に使う。
- 同じ入力、完成Prompt、schema、checkpoint及び評価基準で両モデルを各1回実行し、モデルの意味判断差と
  Provider、Prompt、契約及び実装の差を分離する。Provider疎通やmockテストだけを法令検索の合格としない。

現時点の比較は次のとおりである。

| 観点 | `gpt-4o-mini` | Haiku |
|---|---|---|
| 隔離した質問分解・Hypothesis生成 | 具体的な法的Hypothesisを生成できた | 実行可能 |
| 長い入力と複数処理での追従 | Hypothesisの抽象化、論点の結合、候補対応の揺れが目立った | GPTより安定して処理を継続した |
| 公開買付け総合問題の通し実行 | 過去実行では契約・実装不備も重なり、正常完了が安定しなかった | Profile v375では2 Cycleを`protocol_error`なく完了し、必要Article 4/6を取得した |
| 残る意味判断上の問題 | 実経路では具体性と対応付けが不安定 | Graph方向の誤り、府令2条の5のHypothesis誤対応、未確認なのに`gaps=[]`とする問題が残る |

以上から、現時点ではHaikuを法令検索全体の主評価モデルとする。ただし、比較した実行の間でPrompt、契約及び
実装が変更されているため、確認済みの差をすべてモデル性能差とは断定しない。`gpt-4o-mini`にも隔離Promptで
具体的なHypothesisを作る能力があり、同一checkpointによる比較が完了するまでは「Haikuの方が有望で安定している」
という暫定評価とする。

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

### LR-023 Graph方向とHypothesis対応

Haiku・Profile v375の公開買付け総合問題では、Graphの起点Article自体は概ね妥当だったが、
下位規範を逆引きする方向と、発見した候補を確認対象へ対応付ける処理が不安定だった。

- 金商法27条の2は`incoming`で探索され、公開買付府令2条の5が候補に現れた。
  ただし、これは施行令7条から府令2条の5へ進んだ結果ではなく、府令2条の5が金商法27条の2も
  直接参照していた別経路による発見だった。
- 施行令7条から府令2条の5を探す要求と、金商法27条の3から府令10条を探す要求は`outgoing`だった。
  委任先Article IDが本文に明示されず、下位規範側からの参照を探す場面では`incoming`が必要であり、
  この2要求は意図した逆引きに失敗した。
- 府令2条の5は、発見元である成立条件のH-1に対応する候補として固定され、本来対応する例外のH-3へ
  再対応付けされなかった。そのためH-1の条件確認には優先度が低い候補と判断され、`defer`された。
  府令10条はOpenSearch候補には現れたが、金商法27条の3から`outgoing`へ進んだGraph検索では回収されなかった。

同じProfile v375をLuna `low`で実行すると、主要な下位規範探索は改善した。

- 金商法27条の2を`incoming`で探索し、施行令6条・7条と府令2条の5を発見した。
- 金商法27条の3・27条の9を`incoming`で探索し、施行令8条、府令9条・10条・11条等を発見した。
- 府令10条は手続H-4へ対応し、公告掲載事項に直接関係すると評価された。しかし残り本文取得枠3件では
  施行令6条、府令2条の5、施行令8条を優先し、`relevant_deferred / not_requested`となった。
  後続で再採用されず`finalize_only`へ入ったため、本文未取得の原因はGraph方向ではない。
- 定義府令14条の2から委任元の上位規定を探す要求には`incoming`を選び、候補0件となった。
  参照を書いている定義府令から参照先へ進む場面なので、ここは`outgoing`が必要だった。

`REFERENCES`の物理方向は、参照を書いたArticleを`from`、参照先Articleを`to`とする。したがって、
上位Articleを参照する下位Articleを探す場合は`incoming`、起点Articleに書かれた参照先をたどる場合は
`outgoing`である。同じLuna実行内で両方を`incoming`としたことから、「順引き・逆引き」等の説明と
Neo4jの物理方向を混同した可能性がある。

Profile v381では、LLMには`follow_reference_in_text`又は`find_articles_referencing_this`を選ばせ、
adapterがそれぞれ`outgoing / incoming`へ機械変換する。この変換は法的関係の意味をProgramが決めるものではなく、
選択済み探索目的をGraph APIへ写す処理である。旧誤方向checkpointをLuna `high`で再生すると、金商法27条の2と
施行令7条から未知の下位規範を探す2要求はいずれも`find_articles_referencing_this`となり、修復なしで契約を通過した。

直近コミットのProfile v395とProfile v425の完成Promptを比較すると、意味関係の方向説明自体は存在したが、
`IMPLEMENTS`の「親規定がSUBJECT、具体化規定がOBJECT」と「起点をSUBJECTにする場合は`from_subject`」が
離れており、LLMが両者を組み合わせる必要があった。v425では、目的欄に「具体化規定を探す」と正しく書きながら、
4件全てで`to_subject`を選んだ。また、Profile v425で追加されたEvidence IntegrationはToolを選択できるにもかかわらず、
共通Tool Promptを合成していなかった。レビュー用のStep 4成果物も誤ってCycle Close Profileから生成され、
本番Promptとの差を見落としやすい状態だった。

Profile v426では、意味関係ごとの端点役割と方向を一続きの対応として共通Tool PromptとTool schemaへ記載し、
Evidence Integrationにも同じ共通Tool Promptを合成した。Programは法的な関係又は方向を補正せず、LLMが選んだ
既知値と構造だけを検証する。レビュー用Step 4成果物も本番と同じEvidence Integration Profileから生成する。

v425の失敗入力を固定fixtureとしてLuna `high`で隔離再生した結果、4件全てが1回目の出力で
`IMPLEMENTS / from_subject`となった。Profile v426の総合E2Eでも4件全てが同じ正方向となり、施行令7条から
府令2条の5、金商法27条の3と施行令8条から府令10条を発見・取得した。3 Cycle、359.0秒で正常完了し、
機械採点は必要根拠4/4、回答観点4/4、出典3/3の11/11だった。追加の未確認事項を残したため
`answerCompleteness=limited`であり、11/11は代表設問の評価基準に対する結果である。v425の329.3秒・10/11、
v395の345.3秒・11/11と比べ、今回の変更は精度を安定化したが速度改善ではない。

完了条件は次のとおりとする。

- SolverがHypothesisと本文中の参照表現に基づき、参照先をたどるか、参照元を探すかを選ぶ。
- Graph候補は発見元のHypothesisだけに固定せず、全open Hypothesisとの意味的対応をSolverが再評価できる。
- Programは既知ID、探索目的、件数及び参照整合だけを検証し、法的関係や候補の対応先を決めない。
- 固定fixture、Luna及びHaiku回帰で、上位規定から下位規範を探す`incoming`と、下位規範から
  明示参照先をたどる`outgoing`をそれぞれ確認する。
- 施行令7条から府令2条の5、金商法27条の3から府令10条へ到達し、対応するHypothesisの
  本文取得候補として選べることを確認する。候補発見後の取得優先順位と後続再採用は方向評価と分けて採点する。

### LR-024 Hypothesisの支持判断と未確認事項

Profile v375では、例外のH-3と手続のH-4について、上位規定の本文から命題の一部を支持できた。
一方、具体的内容を定める府令2条の5と府令10条は取得していない。それにもかかわらず、両方とも
`judgment=supported`かつ`gaps=[]`になった。

`supported`は、取得本文が`statement`を支持することを表す。WorkItemへの回答に必要な内容をすべて
確認したことまでは表さない。そのため、支持根拠があっても具体的な条件、例外又は手続が未確認なら、
その内容を`gaps`へ保持する必要がある。

完了条件は次のとおりとする。

- Evidence Integrationは、取得本文で確認できた内容と、まだ本文を取得していない内容を分けて評価する。
- `judgment=supported`への更新だけを理由に`gaps`を空にしない。
- `gaps`を解消するかはSolverが本文の意味から判断する。Programはその意味を補わず、同じ更新で
  `terminal_text_missing`としたWorkItemの全Hypothesisから`gaps`を消す構造矛盾だけを拒否する。
- 府令2条の5・10条を取得していない固定fixtureではH-3・H-4の`gaps`が残り、取得後のfixtureでは
  対応する内容を確認した場合だけ解消される。

### LR-025 取得済みEvidenceとHypothesisの対応付けの引継ぎ

取得済みEvidenceを保存するだけでは、後続Step又は次CycleでどのHypothesisの検証に使ったかを
再利用できない。対応が欠落すると、取得済み本文を未確認として扱う、同じ本文を再取得する、又は
別Hypothesisへ誤って対応付ける可能性がある。

EvidenceとHypothesisの対応はCaseStoreを正本とし、Projectorは現在の処理に必要な対応だけを
Solver入力へ投影する。対応の追加はLLMの意味判断による明示的な差分を保存する。
Programは既知ID、型及び参照整合を検証し、本文とHypothesisの意味的対応を決めない。

Profile v382では、`HypothesisUpdate.evidence_ids`を今回の評価で新たに使用したEvidence IDの差分とした。
Programは保存時とCycle Close向け仮適用時に同じ追記処理を使い、既存IDを順序どおり保持して重複だけを除く。

完了条件は次のとおりとする。

- Evidence Integrationで確定したEvidenceとHypothesisの対応が、後続Stepと次Cycleでも保持される。
- Projectorが本文を省略する場合も、Evidence IDと対応Hypothesis IDを失わない。
- 同じEvidenceを別Hypothesisへ再対応付けする場合は、LLMが明示した差分として記録する。
- Cycle境界fixtureで、取得済みEvidenceを再取得せずにHypothesis評価と次の探索判断へ利用できる。
- 未取得本文を取得済みとして扱わず、LR-024の`judgment`と`gaps`の条件を同時に満たす。

### LR-026 Graph本文取得後の逐次統合

Graph Reviewで本文未取得Articleを`select`した場合、その本文取得後は残りのGraph候補より
Evidence Integrationを優先する。統合後にCaseStoreからGraph候補を再投影し、処理済みの
`frontier_item_id`を除いた未処理候補を再びレビューする。

候補順序やページ位置は永続化しない。候補の追加やHypothesisの更新で順序が変わることを許容し、
次の不変条件だけを維持する。

- 処理済み候補と未処理候補をIDで区別する。
- 未処理候補をCycle終了時に削除せず、次Cycleへ引き継ぐ。
- 最終Cycleで未処理候補が残る場合は、回答上の未確認事項として扱う。
- Graph候補の採否と取得本文の意味評価はLLMが行い、Programは未統合結果の検出と処理順だけを管理する。

### LR-027 Hypothesis単位のGraph探索

Graph Reviewの単位をWorkItem・Hypothesisの組とする。一つの単位で本文取得対象を選んだ後は、
本文取得とEvidence Integrationを完了してから別のHypothesisへ進む。同じ単位の未処理候補は
現在Cycleで再提示せず、次Cycleへ引き継ぐ。

- Legal Profileでは、一つのHypothesisから一回に取得するGraph由来本文を一Articleとする。
- Graphが不要なHypothesisには取得を強制しない。
- 候補と処理順は固定せず、処理済み・未処理をIDで区別する。
- 次Cycleでは更新済みHypothesisを使い、残候補がまだ必要かをLLMが判断する。
- ProgramはCycle番号、Tool結果、既知IDから同一Cycleの取得済み単位を判定し、候補の法的関連性を判断しない。
- 同一単位の残候補だけを理由にProgramがCycleを閉じない。現在Cycleで別Hypothesisを処理するか、
  Cycleを閉じるかは、残り取得枠と未確認事項を見たSolverが判断する。

### LR-029 支持済みHypothesisに残る未確認事項の引継ぎ

LR-024では、`judgment`をHypothesisの`statement`に対する判定、`gaps`をWorkItemへの回答に残る
未確認事項として分離した。このため、`judgment=supported`でも`gaps`が残る状態は正常であり、後続検索の
対象になり得る。

2026-08-27の追加設問では、次の不整合を確認した。

- Search Selectionへ検索候補4件が渡された一方、`work_tree=[]`、`hypotheses=[]`となった。
- Provider schemaは`matched_hypothesis_ids=[]`を要求したが、後段validatorは選択候補に1件以上の
  対応Hypothesisを要求したため、正常な出力が存在しなかった。
- 別の実行では、`supported`かつ`gaps`ありのHypothesisによりWorkItemがopenのまま残ったが、
  最終回答契約はそのWorkItemを未解決Hypothesis、`needs_action` Dependency又は未処理Frontierの
  いずれにも対応付けられず、契約修復を繰り返して停止した。
- これらを修正した実モデル再検証ではSearch Selectionを通過したが、本文評価がHypothesis差分を返さず
  Dependencyだけを`needs_action`へ更新した際、`SolverDecision(next=continue)`が「状態更新なし」と
  誤判定した。DependencyDecisionはCaseStoreへ保存される状態差分だが、継続条件のvalidatorが判定対象から
  漏らしていた。

Programは`judgment`又は`gaps`の法的妥当性を判断しない。構造上は、未判定のHypothesisに加え、
支持済みでも`gaps`が残るHypothesisを後続探索へ投影する。最終化では`unresolved_hypothesis_ids`を
`judgment=unresolved`のIDに限定したまま、支持済みHypothesisの`gaps`もopen WorkItemの未確認理由として
認める。
DependencyDecisionだけが変化する本文評価も有効な状態更新として扱う。Programは既知IDと状態遷移を検証し、
下位規範が必要かという意味判断はLLMの出力を保持する。

### LR-030 意味関係を優先したGraph探索と明示参照fallback

現在の公開買付けミニGraphには、24件の意味関係がpublishされている。

| predicate | 件数 | 用途 |
|---|---:|---|
| `IMPLEMENTS` | 10 | 上位規定が委任した事項を下位規定が具体化する |
| `USES_DEFINITION` | 11 | 別Articleで定義された用語を使用する |
| `EXCEPTION_TO` | 2 | 原則規定に対する適用除外又は例外を定める |
| `OVERRIDES` | 1 | 明示的な優先規定で別規定の適用を変更する |
| `INCORPORATES` | 0 | 準用・読替えの対象Articleが現在の13 Articleに含まれず、分類対象ペアがない |

必要な三層関係も、次の意味関係として登録済みである。

```text
金商法27条の2
  ├─ IMPLEMENTS ──→ 施行令7条
  └─ EXCEPTION_TO ←─ 施行令7条
                           └─ IMPLEMENTS ──→ 公開買付府令2条の5

金商法27条の3
  ├─ IMPLEMENTS ──→ 施行令9条の3
  │                         └─ IMPLEMENTS ──→ 公開買付府令9条
  └─ IMPLEMENTS ──→ 公開買付府令10条
```

それにもかかわらず、Profile v392の「委任・適用除外」と「公告方法」のGraph要求は全て
`explicit_reference`だった。明示参照の関係自体は正しく取得できたが、Hypothesisに不要な隣接Articleや
取得済みArticleへの往復が発生した。「公告方法」は13 Article全件がOpenSearch候補に一度は現れ、必要4 Articleを
取得済みだったが、準用先の電子手続府令1条・2条及び施行令4条の2の4がデータセットにないことを確定できず、
追加探索を続けた。

意味関係と明示参照は、次の順序で使う。

1. Solverは現在のHypothesis、起点Article及び探索目的から、意味predicateを説明できるか判断する。
2. 説明でき、publish済み分類のcoverageがある場合は`semantic_assertion`を使う。
3. 該当predicateが未分類、対象scopeのcoverageが不足、又は明示された参照先そのものを確認する場合は、
   `reference_edges`へフォールバックする。
4. Programは意味predicateを選ばない。既知ID、件数上限、coverage、過去の探索scope及び新規Article ID数だけを
   検証・記録する。
5. 新しいArticle IDが得られなかった場合、その事実をObservationとしてSolverへ返す。別の検索表現、別の関係、
   次Cycle又は限定回答のどれを選ぶかはSolverが判断する。

完了条件は次のとおりとする。

- 例外Hypothesisでは、金商法27条の2から`EXCEPTION_TO / to_subject`で施行令7条へ進み、
  施行令7条から`IMPLEMENTS / from_subject`で府令2条の5へ進める。
- 公告方法Hypothesisでは、金商法27条の3から`IMPLEMENTS / from_subject`で施行令9条の3・府令10条へ進み、
  施行令9条の3から同じpredicateで府令9条へ進める。
- 意味分類の未被覆fixtureでは`reference_edges`へ移り、意味関係があるfixtureでは最初からraw参照全件を
  列挙しない。
- 同じHypothesisで意味関係と明示参照の双方から新規Articleを得られなかった場合、同じscopeの反復ではなく、
  Solverが探索方針の変更又は限定回答を選べる。

2026-08-27のProfile v393では、下位規範ActionでProgramがGraph Toolの入力schemaを
`explicit_reference`だけへ縮小していた処理を削除した。Programが構造上Graphを要求する場合も、
`semantic_assertion / explicit_reference / explains`のどれを使うか、predicate及び方向はSolverが選ぶ。
同時に`completed_graph_searches[]`へ、その要求が返した`candidate_article_ids`と、その時点でCaseに初めて現れた
`new_candidate_article_ids`を追加した。これはIDの集合差だけをProgramが計算し、関係の有用性は判断しない。
Promptは、Hypothesisに合う意味関係を説明できる場合は意味関係を先に使い、新規候補が得られない場合に
明示参照へ切り替え、双方で進展しなければ同じGraph探索を反復しない順序へ統一した。

現段階ではGraph Adapterが設定されたpublish済みClassificationRunだけを参照する。候補0件は関係不存在を
意味しないため、Solverは明示参照等へフォールバックする。ClassificationRunの対象scope別coverageを
AgentViewへ明示する拡張は`LR-006`の未完了事項として分離する。

同日のLuna `high`による現行の公開買付け総合設問では、3 Cycle、325.8秒で正常完了した。
Graph要求4回は全て`semantic_assertion / IMPLEMENTS / from_subject`で、`explicit_reference`は0回だった。
金商法27条の2から施行令6条・7条、施行令7条から公開買付府令2条の4・2条の5・2条の6、
金商法27条の3・27条の9から公開買付府令10条等を発見した。UIの採点では文書3/3、必須条文4/4、
回答要点4/4の計11/11である。モデル呼出し18回・試行24回に324.4秒、Tool 14回に4.2秒を要しており、
意味関係の選択は改善したが、処理時間は引き続きLLM呼出しと契約修復が支配している。

### LR-031 ガイド`EXPLAINS`のFramework契約

`reference_edges`と`explains`は統合しない。前者は法令のArticle、Paragraph又はItemに明記された
参照元から参照先Articleへ進む`REFERENCES`である。後者は、ガイドの条文注釈又は対応表で解説対象が
明示された場合に限り、ガイド`Document`から法令`Article`へ進む非規範的な索引である。

```text
法令Content Unit ── REFERENCES ──→ 法令Article
ガイドDocument   ── EXPLAINS   ──→ 法令Article
```

物理Graphとseedは後者を`Document → Article`として定義しているが、新Frameworkには次の不整合がある。

- `legal_graph_neighbors`の共通起点項目が`article_ids`であり、ガイド`Document`を指定できない。
- `GraphClient.article_relations_touching`がRelationの両端を`Article / Paragraph / Item`へ限定し、
  `Document → Article`の`EXPLAINS`を除外する。
- Graph Reviewと後続の`fetch_articles`はArticle候補を前提とするため、Articleからガイドを逆引きする場合の
  ガイド本文取得契約がない。

現状の`explains`モードはschema上選択できても、新Frameworkの本来の用途である
「OpenSearchで発見したガイドDocumentから、明示的な解説対象Articleを特定する」を実行できない。
旧Guidance Laneの`document_id`起点探索は存在するが、新Frameworkの解決とはみなさない。

修正時は、まずガイド`Document → Article`の順方向だけをArticle発見経路として成立させる。
Articleからガイドへの逆引きは、利用要件とガイド本文取得経路を確認してから追加する。Programは
Document ID、Article ID、方向、件数及び既知参照を検証するだけとし、ガイドが質問を解説するかという
意味判断はSolverへ残す。単なる言及、同じページに現れただけの条文又は前ページからの引継ぎを
`EXPLAINS`として登録しない。

### LR-032 `reference_edges`の名称と契約

旧`explicit_reference`は、起点Articleの本文を実行時に文字列検索する機能、又は利用者が質問で明示した参照と
誤読できた。Profile v395で名称を`reference_edges`へ変更した。このmodeは、seed時に原文の明示参照から
作成済みのNeo4j `REFERENCES`を、検索時に1ホップたどる。

```text
[参照元規定] ── REFERENCES ──> [参照先規定]

follow_reference_in_text
  参照元規定から、本文に記載された参照先規定を探す

find_articles_referencing_this
  参照先規定から、それを参照している参照元規定を探す
```

現在の説明は、次の異なる事項を一か所へ混在させている。

- Graphに保存された物理関係の種類: `REFERENCES`
- Graph検索modeの名前: `reference_edges`
- 検索方向を表す目的: `follow_reference_in_text / find_articles_referencing_this`
- `semantic_assertion`で候補が得られない場合に利用する、探索フロー上のfallback条件

このため、Solver、実装者及び利用者が、意味関係との違い、検索方向、OpenSearchによる本文検索との違いを
名称だけから把握しにくい。修正では、まずschema description、完成Prompt及び人間向けGraph説明で上の4点を
分離する。`outgoing / incoming`はLLM-visible契約へ戻さず、参照元・参照先という法令上の役割で説明する。

名称変更だけでfallback条件まで表さない。fallbackは探索手順、`reference_edges`は実行するGraph検索の種類として
別々に保つ。旧名称を含む過去の診断fixtureは当時の実行記録として維持し、新しい契約検証には使わない。

Profile v395では全1074テストに合格した。Luna `high`の公開買付け総合問題は3 Cycle、345.3秒で正常完了し、
資料3/3、必要条文4/4、回答要点4/4の計11/11だった。Graph要求5回のうち2回が`reference_edges`であり、
旧名称は完成schema、応答及びtraceに現れなかった。実装は`e58e2ac`でコミット済みである。

2回の`reference_edges`は正常に実行されたが、候補はいずれも0件だった。必要条文は、初回OpenSearchで取得済みの
施行令7条と金商法27条の3を起点に、後続の正しい`semantic_assertion / IMPLEMENTS / from_subject`で回収した。
最初の`IMPLEMENTS`探索で意味方向を誤った問題は名称変更で解決しておらず、`LR-023`で継続する。

### LR-033 本文評価と次行動選択の統合

Profile v395のLuna `high`による公開買付け総合問題を性能baselineとする。

| 処理 | LLM呼出し | 延べlatency | 入力token | 出力token |
|---|---:|---:|---:|---:|
| Observation Integration | 6 | 121.2秒 | 132,789 | 28,229 |
| Integration | 4 | 94.6秒 | 78,725 | 12,594 |
| Finalization | 1 | 39.5秒 | 27,825 | 6,270 |
| Cycle Close | 2 | 29.0秒 | 12,299 | 3,833 |
| 全LLM処理 | 22 | 342.1秒 | 293,489 | 57,430 |

Tool処理16回の合計は9.2秒であり、最初にOpenSearch、Neo4j又は本文取得を高速化しても全体への効果は小さい。
最優先の重複は、新しい本文をObservation Integrationで評価した直後に、通常Integrationが更新済み状態と本文を
読み直して次のToolを選ぶことである。

```text
現行
本文取得
  -> Observation Integration: Hypothesis・下位規範状態を更新
  -> Integration: 同じ観察を踏まえて次行動を選択

不採用としたProfile v397実験
本文取得
  -> Integration
       - Hypothesisを更新
       - 下位規範状態を更新
       - 次行動又は完了を判断

Profile v409
本文取得
  -> 直近本文と全open HypothesisのObservation Integrationを1回
       - Hypothesisを更新
       - 下位規範状態を更新
       - 本文取得前の対応候補が誤りなら再対応付け
       - WorkItemごとに直後のToolを最大1件選択
```

v397では既存の通常Integration契約を流用したが、公告で通常Integrationと修復だけの入力が約47万tokenとなり、
約321.0秒・26回でbaselineを改善しなかった。全Case状態を再読する方式は採用しない。

v409では入力を最大3つのopen WorkItemとそのHypothesis、直前のToolで新たに取得した本文、
既知候補、成功済みscope及び処理上限へ限定する。利用可能Toolの契約は出力schemaを正本とし、
入力へ重複掲載しない。過去の本文評価はHypothesisの判定・Evidence ID・未確認事項として引き継ぐ。
下位規範判断も既存の状態・根拠IDを入力し、新規根拠だけを出力してProgramが既存IDへ追記する。
取得前のHypothesis対応は候補として提示し、実際の本文を他のopen Hypothesisへ再対応付けできる。
出力には状態差分とWorkItemごとの直後のTool要求最大1件だけを加え、Cycle移行、Graph候補の一括評価及び最終回答は兼務させない。
Cycle境界ではTool要求を空配列へ固定する。
投影する既知Hypothesis、Evidence、Article IDは少数なので、この呼出しだけはProvider schemaにも
完全一致候補を残す。共通validatorも維持し、モデルがIDを連結・変形してから修復する往復を避ける。

Programは既知ID、Tool schema、件数、予算及び成功済みscopeとの一致だけを扱う。Hypothesis更新、次に必要な確認、
Toolの選択、Graph predicate・方向及びEvidenceとHypothesisの対応はLLMが判断する。

Graph候補0件の場合は、前回の起点、predicate、direction、候補数及び逆方向の試行有無を次の入力へ残す。
LLMが方向誤りだと判断した場合だけ逆方向を選べるようにし、Programが自動的に両方向を検索しない。
同一scopeの再実行は禁止し、意味方向の精度自体は`LR-023`で扱う。

次に、Cycle CloseではCaseStoreに保存済みのEvidence IDや状態を再列挙させず、次Cycleの焦点、意味判断が必要な未決候補の
扱い及び短い理由だけを返す方向で入力・出力を削減する。FinalizationはHypothesisに対応する取得本文を中心に投影し、
棄却済みnavigation、処理済みledger及び重複本文を除く。reasoning設定の引下げは最初の変更へ混ぜず、同じfixtureで
品質を比較できる段階になってから別に評価する。

完了条件は次とする。

Profile v409のLuna `high`実測は次のとおりである。例外問題は一括出力上限への到達を解消し、
必要根拠を維持したまま大幅に短縮した。公告問題は呼出数とtokenを削減したが、1回当たりのLuna生成時間が増え、
wall timeは改善しなかった。このため本課題は部分改善として継続する。

| 設問 | 必要根拠 | baseline | Profile v409 | LLM呼出 | 入力token |
|---|---:|---:|---:|---:|---:|
| 公告 | 4/4 | 319.7秒 | 329.7秒 | 25→17 | 275,945→135,162 |
| 例外 | 3/3 | 307.2秒 | 134.0秒 | 29→9 | 310,433→142,649 |

Profile v421では、基本処理をLunaのまま維持し、複数の意味判断をまとめた`evidence_integration`だけを
`AGENT_FRAMEWORK_EVIDENCE_INTEGRATION_MODEL`で別モデルへ切り替えられるようにした。Cycle Close、通常Integration、最終回答及びReviewerにはこの設定を適用しない。
これはモデル差を隔離するための実験である。Terra `high`を使った総合問題の初回実測は2 Cycle・19モデル呼出し、
324.4秒、必要Article 3/4、全体10/11だった。`observation_integration`5回だけがTerra、残り14回はLunaであり、
府令2条の5は取得したが府令10条を取得しなかった。直前のLunaのみのv420実測は約309.5秒・9/11だったため、
1回の比較では根拠到達が1件改善した一方、総時間は約15秒増えた。完全回答と速度改善は未達であり、
モデル切替だけでLR-033を完了扱いにしない。Profile v422で基準設定をLuna `high`へ戻し、
Evidence IntegrationをWorkItem単位に分離した状態で比較する。

Profile v423のLuna `high`総合問題は3 Cycle・310.7秒・LLM 21回・Tool 18回で完了した。
v422では同一Cycle・WorkItem・Hypothesis・Evidence IDの`load_evidence`が2回成功していたが、
v423では同一scopeの成功済み再要求は0件、新しいTool結果がない同一Hypothesisの
Observation Integration反復も0件となった。新しいTool結果ごとの再評価は通常の
action-observationとして保持する。一方、この実行は施行令7条の委任先を未確認なのに、
`h-3` を`supported / gaps=[] / terminal_text_confirmed`として終了した。そのため府令2条の5のGraph要求自体が
作られず、機械採点は10/11だった。再提示防止とは別に、本文統合Promptの確認範囲と
`gaps`削除条件を切り分ける。

Profile v424では、質問と無関係な参照先は新しい`gaps`に追加しない一方、質問された条件、
範囲又は手続を委ねる参照先は末端本文を確認するまで保持するようPromptを修正した。
既存`gaps`は対応本文を確認した場合だけ削除する。Luna `high`総合問題では`h-3`が府令の
具体的要件を`gaps`に保持し、府令2条の5をGraphが発見・選択した。ただし、そのArticleは
別HypothesisのToolで先に取得済みだったため、正しい`h-3`へ本文を再提示できず、3 Cycle・357.9秒・10/11だった。

Profile v425では、LLMがGraph候補を選択し、そのArticleが取得済みだが対象HypothesisのEvidenceでない場合、
Programが既知Evidence IDを`load_evidence`で機械的に再提示する。法的対応先はGraph ReviewのLLM選択を使い、
ProgramはArticle・Hypothesis・Evidenceの既知IDだけを照合する。v424失敗状態の固定fixtureで府令2条の5を`h-3`へ
再提示できる要求を確認した。Luna `high`総合問題では府令2条の5を`h-3`へ統合し最終引用に含めたが、
Graph方向を全4要求で`to_subject`としたため府令10条を取得できず、2 Cycle・329.3秒・10/11だった。
再提示経路とPromptの確認範囲は合格、Graph方向の安定性は`LR-023`で継続する。

公告のv409では、Cycle 1で主要根拠4件を取得済みだったが、Evidence Integrationが条文中の準用先を
質問された公告条件の範囲へ限定せず、電子届出の入力方式・様式まで新しい`gaps`へ追加した。その結果、
不要なCycle 2が発生し、追加のLLM 8回に約232.8秒を費やした。Profile v410では、確認範囲を
WorkItemの質問粒度に合わせ、提示本文で質問された規律を説明できる場合は、質問されていない様式、
入力方法又は運用手続を参照先から新しい`gaps`へ広げない。これは特定Article名による分岐ではなく、
LLMが質問範囲と法令本文を比較する一般ルールである。

v410でも公告は2 Cycle・318.9秒だった。Cycle 1終了は140.5秒だったが、最終化の予約が90秒、
次Cycleの最低予算が25秒だったため、残り279.5秒でCycle 2を開始した。実際のCycle 2は約161.7秒を使った。
Profile v411では、最大4 Cycleという回数上限は維持しつつ、最終化予約をモデルtimeoutと同じ180秒、
次Cycleの最低予算を120秒へ変更する。Programは法的な完了を決めず、残り時間だけから新Cycleを開始できるかを判定する。

v411の公告は119.8秒、LLM 12回、Tool 7回まで短縮した。一方、初回検索で金商法27条の3を発見していたが、
候補選択入力に`non_work_item_requirements`の「根拠条文を示す」がなく、直接の詳細規定4件だけを選択した。
Profile v412では候補選択へ回答全体の要件も投影し、根拠提示が求められる場合は、規律を置く規定と具体化規定を
取得枠内で選べるようにする。候補の法的役割と採否は引き続きLLMが判断する。

v412の公告は156.2秒で金商法27条の3を含む主要根拠を取得したが、時間予約へ入る直前に取得した府令9条を
Hypothesisへ統合せず最終化し、日刊新聞紙の種類・数の条件を回答へ反映できなかった。Profile v413では、
時間又はCycle上限に達していても、未統合のArticle本文がある場合はEvidence Integrationを1回先に実行する。
その後は追加Toolを許さず、統合済み状態から限定回答を作る。

v413のLuna `high`公告は147.8秒、2 Cycle、LLM 13回、Tool 9回、入力81,726 token、出力15,998 tokenで
正常完了した。主要根拠4/4に加え、最後に取得した府令9条をHypothesisへ統合し、日刊新聞紙は原則として
二種類を含む二紙以上、全国紙なら一紙以上という条件を回答し、府令9条第1項・第2項を引用した。
v395の319.7秒から約54%、v412の156.2秒から約5%短縮しており、この公告実行単体では
時間上限による根拠欠落はなかった。

ただし、v413で公告・例外以外のLv.3設問7件を連続検証すると、全実行は正常終了した一方、厳格評価は1/7だった。
合計1023.5秒、平均146.2秒、LLM 66回、Tool 47回、Graph 3回であり、総合問題では府令2条の5・10条、
社内方針・改正影響・非居住者問題では府令9条又は10条が欠けた。必要ArticleはOpenSearchに存在したが、
多くの実行が1 Cycleの途中で`finalize_only=true`となり、次の本文取得又はGraph探索へ進めなかった。

原因は、Loopが`finalization_reserve_sec + cycle_close_reserve_sec + min_next_cycle_budget_sec`を
現在Cycleの終了閾値にしていたことである。v413の設定では420−180−15−120=105秒しか探索へ使えず、
「次Cycleを安全に開始できない」と「現在Cycleを直ちに終了する」を混同していた。また、180秒は
モデル呼出しの最大timeoutに合わせた値で、実測11〜29秒程度だった最終化の通常所要時間とは別概念である。

Profile v415では、残り時間が`finalization_reserve_sec`以下の場合だけ`finalize_only=true`とする。
`min_next_cycle_budget_sec`は`can_start_next_cycle`だけに使用し、次Cycleを開始できなくても
`finalization_reserve_sec + cycle_close_reserve_sec`へ達するまでは現在Cycleの探索を継続できる。
モデルtimeoutは180秒のまま、最終化予約は過去の長い実行にも余裕を持たせて90秒とする。
`runStatus=completed`は処理の終端を示すだけなので、API traceには未解決IDから機械導出した
`answerCompleteness=complete / limited / unavailable`を別に返す。

最初のv414実モデル総合問題は2 Cycleへ進み、府令10条を含む追加本文を取得したが、Cycle Closeへ
残り時間から最終化予約を引いた119秒をそのまま与え、全時間をtimeoutで消費した。その後、保存差分がない
checkpointを`next=continue`の空Decisionへ変換し、`SolverDecision`の検証で`schema_validation`となった。
v415ではCycle Closeの最大待機を`cycle_close_reserve_sec=30`へ限定し、保存差分がないtimeoutは
checkpoint Decisionを生成せず、`cycle_step_timeout`から予約済み最終化へ移る。

v415のLuna `high`総合問題は、2 Cycle・301.7秒、LLM 15回、Tool 14回で`runStatus=completed`となり、
Cycle Closeは5.5秒と7.9秒で完了した。v414で発生した`schema_validation`は再発せず、現在Cycleの探索も
105秒では打ち切られなかった。金商法27条の2・27条の3、施行令6条・7条・8条、公開買付府令2条の5等を
本文取得したが、府令10条は取得せず、未評価Graph候補39件と各WorkItemの未確認事項が残ったため、
`answerCompleteness=limited`の限定回答となった。これは時間境界の構造修正が機能したことと、回答品質の
完了を分けて扱う実例であり、11/11達成を示すものではない。

Profile v416では、この実測で判明した二つの回帰を修正する。第一に、open WorkItemでも判定済みHypothesisへ
対応する取得本文を限定回答へ提示し、モデルが選んだ確認済み引用を空の必須IDで上書きしない。第二に、
同じHypothesisのGraph候補は1 Cycleにつき1 Articleだけを本文取得し、統合済み単位の`defer`候補を
同一Cycleの通常取得候補へ混ぜない。質問分解では、質問が上位概念だけを示す要求を、質問にない構成要素へ
展開しない指示をPromptとschema descriptionで一致させる。

v416のLuna `high`総合問題は、2 Cycle・331.8秒、LLM 18回、Tool 10回で正常終了した。金商法27条の2・
27条の3、公開買付府令2条の5・10条の必要Article 4/4を取得し、確認済み根拠を保持した限定回答として、
成立条件、対象範囲、主な適用除外、手続の4観点を説明した。適用除外の追加要件を未確認として`wi-3`を
openに保ったため`answerCompleteness=limited`であり、完全回答とは扱わない。実測ではGraph Review本文の
取得成功直後、Evidence統合前のContextだけ同じHypothesisを取得済みと扱わない境界が判明したため、v417で
取得成功時点から同一CycleのGraph取得枠へ反映する。法的候補の必要性は引き続きLLMが判断する。

- 本文評価だけの専用呼出し直後に、同じ観察を読む通常Integrationを重ねない。
- 1回のObservation Integrationが1つのopen WorkItemとそのHypothesisだけを直近本文と照合し、状態差分と直後のTool最大1件を返す。対象WorkItemが複数なら最大4回を並列実行する。
- 未統合ToolResultは同じDecisionの適用後に処理済みとなり、同じ観察だけを理由に再実行しない。
- Programが法的関連性、Graph predicate・方向、根拠十分性又は次Toolの意味を決めない。
- `fetch_articles`、5 predicate・2方向の`legal_graph_neighbors`、`reference_edges`、`legal_search`、`load_evidence`、行動なしをfixtureで検証する。
- Luna `high`の公告・例外・総合で必要根拠と総合11/11を維持し、同じsnapshot・設定に対するLLM呼出数、wall time、入出力tokenをbaselineより削減する。

### LR-034 Cycle単位の探索経路監査

最終回答が合格しても、途中のGraph方向、Evidence対応付け、未確認事項の更新に問題が残る場合がある。
最終結果だけでは、別Cycleの探索によって偶然回復した経路上の誤りを回帰対象にできない。

診断`status / snapshot`に、Cycle終了時の`cycle_checkpoint`を追加する。これはCaseStoreへ保存する新しい状態ではなく、
Cycle開始・終了時の正本から作る監査用投影である。WorkItem・Hypothesisの差分、未確認事項、Evidence ID、
Tool引数・結果・所要時間、モデル所要時間・tokenをCycle単位で記録する。本文はEvidence正本に残し、投影へ複製しない。

Programが検出するのは構造上の確認候補に限定する。たとえば、0件だった意味関係Graph検索で逆方向が未試行、
取得本文が未統合、取得Evidenceが要求元Hypothesisへ未対応、新しいEvidenceなしで`gaps`を消去、進捗なしで次Cycleへ
移行した場合である。これらを法的誤りと断定せず、Graph方向の自動反転や状態の自動修復も行わない。

完了条件は次とする。

- `snapshot`から各Cycle終了時の完全な状態・差分を再現できる。
- `status`では本文を保存せず、Cycle要約と確認事項だけを確認できる。
- 保存JSONLから追加のLLM呼出しなしでJSON・Markdown監査報告を生成できる。
- 最終回答の実行結果と探索経路の確認事項を別項目として表示する。
- 確認事項の診断sequenceから既存手順で最小fixtureを作れる。
- Programは法的関連性、Graph方向の正誤、根拠十分性を確定しない。

2026-08-29のLuna `high`・公開買付け総合問題では、3 Cycle・342.3秒で正常完了し、資料3/3、必要Article
4/4、回答要点4/4の11/11だった。Cycle監査は、Cycle 3で候補0件だった意味関係Graph探索2件を
`GRAPH_EMPTY_INVERSE_UNTRIED`として記録した。本文未統合・Evidence未対応付けの警告はなかった。
最終回答の合格を変えず、途中経路だけを別の確認対象として残せることを実モデルで確認した。

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

2026-08-26のProfile v314実モデル総合問題では、Cycle 2移行後に成功済み`legal_search`と
同じscopeを3回要求し、`already_completed`で終了した。Programの重複scopeはWorkItem、Hypothesis、
Tool引数の組であり、`request_id`と`purpose`を比較しない。一方、Promptは「完全一致する要求」とだけ
書いていたため、LLMがIDや目的を変えたToolRequestを別の要求と解釈できる不整合があった。
Profile v315では、契約description、共通Tool規則、Integration、Dependency Action、自己点検を同じ
scope定義へ統一した。さらにDependency Action入力を次のTool選択に必要なread modelへ限定し、
`action_feedback`を先頭へ投影する。棄却後もTool種類は制限せず、同じToolの別scopeを選ぶ意味判断は
Solverに残す。

同日の修正後も、例外問題ではDependency Actionが成功済み`legal_search`を再要求し続け、3回目の
`ActionRejected`がrun全体の`protocol_error`になった。原因は、重複scopeを禁止しながら専用契約が
各`needs_action`へ必ずToolRequestを要求し、別の有効な行動がない場合の出力を許していなかったことにある。
Profile v316では、Solverが重複しないToolを選ぶか、`can_start_next_cycle=true`の場合にToolRequestなしで
`start_next_cycle=true`を選ぶ契約へ変更した。Programは重複scopeとCycle上限だけを検証し、別検索の有効性や
次Cycleへ移るべきかの意味判断はSolverに残す。

v316の再検証では重複検索は発生しなかったが、Integrationが同じWorkItem IDへ異なる状態更新を2件返し、
契約修復でも同じ配列を繰り返した。Profile v317では、CaseUpdateのdescription、共通契約、Integrationの
出力前確認へ「各IDの今回の最終差分は1件だけ」を定義した。Programは競合更新から一方を選ばず、LLMが
本文評価に基づいて最終状態を選ぶ。併せて、Dependency Actionの結合後Promptに残っていた「ToolRequest必須」
という旧共通契約を、v316の`start_next_cycle`契約へ揃えた。

Profile v317・`gpt-4o-mini-2024-07-18`の例外問題再検証はCycle 2で正常完了し、必要な金商法27条の2、
施行令7条、公開買付府令2条の5をすべて本文取得した。同一scopeの再要求による`protocol_error`は発生しなかった。
一方、最終引用は施行令7条と府令2条の5だけで金商法27条の2を含まず、関連の薄いArticle取得と、表現を
短くした類似再検索も残った。これは重複scope契約とは分け、引用完全性と探索効率の評価対象とする。

同日の総合問題再検証では、Cycle 2で成功済み`legal_search`を再要求し、`ActionRejected`後も同一scopeを
3回出力して`protocol_error`となった。v317は次Cycleへの選択肢を追加しただけで、棄却済みscopeを修復schemaから
排除していなかった。Profile v318の最初の修正では、棄却後を`start_next_cycle=true`へ限定したため
重複検索による`protocol_error`は解消したが、同じ未解決状態から各Cycleで同じ検索を選び、Cycle 4まで消費した。
Profile v319では、棄却後の修復schemaから棄却されたTool種類を外し、Solverが別種のToolまたは次Cycleを選ぶ。
Programは代替行動を生成せず、同じCycle内で棄却されたTool種類の再請求だけを防ぐ。

Profile v319の実モデル総合問題は`protocol_error`なしで完了したが、各Cycleの最初に同じ検索を要求してから
棄却・修復する流れが3回残った。Profile v320では、Search Reviewが未確認Hypothesisへ対応付けた
本文未取得候補がある間、Dependency Actionの`available_tools`と出力schemaから`legal_search`を外す。
Programは候補の意味を判断せず、保存済みのLLM対応判断と本文取得状態だけを使う。
`gpt-4o-mini-2024-07-18`の総合問題再検証では、同一scopeの`action_rejected`とCycle 2以降の
`legal_search`はいずれも0件となり、既知候補の`fetch_articles`へ進んだ。その後のCycle Closeは
OpenAI TPM上限の429で停止したため、最終回答品質はこの実行では評価しない。

2026-08-26の例外問題では、下位規範Action Promptが次のToolRequestだけを要求する一方、汎用Solver schemaが
`next`、`start_next_cycle`、状態更新、最終回答も要求していた。実モデルはToolRequestを作りながら
`next=finalize`、`answer=null`を返し、同じ無効出力を再試行した。Profile v304では、この呼出しの出力を
`decision_reason`と`tool_requests`だけに限定した。Programは既存の`needs_action`判断を変更せず、
ToolRequestのWorkItem IDから`action_request_id`を決定的に対応付け、共通`SolverDecision`へ正規化する。

Graph Toolの入力schemaは、`semantic_assertion`では`from_subject / to_subject`とpredicateを、
`reference_edges`では2つの`reference_lookup`と`predicate=null`を、`explains`では
`outgoing / incoming`と`predicate=null`を許す分岐契約にした。明示参照の物理方向はadapterが探索目的から変換し、
無効なmode・探索目的の組合せをProvider出力時点で防ぐ。

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
- 2026-08-29の初回監査では、未参照fixtureと、過去のモデル出力を保存していることだけを検査する
  fixtureを合わせて14件削除した。現在のコードを実行して契約、状態遷移、ID対応又は探索経路を検査する
  fixtureは維持し、残す基準を`agent-api/tests/fixtures/framework/README.md`へ明記した。

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
LR-025  取得済みEvidenceとHypothesisの対応を後続処理へ引き継ぐ
    ↓
LR-024  支持済みHypothesisにも未確認事項を保持する
    ↓
LR-026  Graph本文取得後にEvidenceを逐次統合する
    ↓
LR-023  Graph方向と候補のHypothesis再対応付けを修正
    ↓
LR-033  本文評価と次行動選択を統合し、重複LLM呼出しを削減
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
| 2026-08-25 | Profile v275・公開買付け3問・`gpt-4o-mini` | 公告は必要Article 2/2だが回答要点不足。例外は2/3取得後、総合は0/6取得後に、いずれもCycle 2の`finalize`とTool要求の矛盾で停止。総合では27条の22の2を誤選択 | 修正前出力だけを保存した3件は2026-08-29のfixture監査で削除した。現行Dependency Action回帰に使う`tob_exceptions_cycle2_finalize_tool_conflict_v275.json`だけを維持する |
| 2026-08-25 | Profile v291・公開買付け3問・`gpt-4o-mini` | 公告だけ正常完了。3問合計で必要Article 2/11、回答要点4/12。例外と総合は`next=finalize`、`answer=null`、Tool要求ありの矛盾を修復できず`protocol_error`。候補内容評価と主体照合の対応ID不一致、および取得候補の誤選択を確認 | `eval-results/e2e-v291-gpt4omini/`、`eval-results/agent-framework-diagnostics/legal-beb78a10fd89425eb78de503e5829a93.jsonl`、`legal-07f80109b5074079851b241bccfb32ce.jsonl`、`legal-985c7b715e12438cbf2404d0257625a6.jsonl` |
| 2026-08-27 | Profile v375・公開買付け総合・Haiku | 全`needs_action`同時Graph要求の矛盾と、open WorkItemの確認済み根拠を最終化から除外する不備を修正。全1059テスト合格。実モデルは2 Cycleで正常完了し、条件・対象・例外・基本手続を引用付きで回答した。府令2条の5・10条は本文未取得のため完全回答は未達 | `eval-results/e2e-v375-haiku-overview.json`、`eval-results/agent-framework-diagnostics/legal-4d596e7ddc0e4a039b96e4be77de48eb.jsonl` |
| 2026-08-27 | Profile v379・公開買付け総合・Luna `high` | Search SelectionとWorkItem別Evidence Integrationを統合し、独立WorkItemを最大4件並列化した。Graph Reviewは現在batchだけを入出力し、理由を短文化した。3 Cycleを`protocol_error`なく完了し、必要Article 6/6を取得した。回答は確認済み範囲を引用したが、「必要な手続」を広く分解した未確認Hypothesisが残り完全回答は未達 | `eval-results/agent-framework-diagnostics/legal-4d8c773f37004c9caf86a46664a4a22e.jsonl` |
| 2026-08-27 | Profile v384・LR-026 | 未統合のArticle取得結果がある場合、Graph候補の次pageよりEvidence Integrationを優先するよう変更。候補2件を1件ずつ処理する回帰で、各本文の統合後に未処理候補へ戻ることを確認。全1065テスト合格 | `agent-api/tests/test_agent_framework.py::test_new_graph_candidates_use_dedicated_solver_profile` |
| 2026-08-27 | Profile v384・公開買付け総合・Luna `high` | 2回のGraph Reviewの各本文取得直後にEvidence Integrationを実行し、必要Article 6/6を取得・統合した。契約違反はなかった。Cycle Close開始時の残り81秒でstep timeoutとなり、Finalizationも残り35秒でtimeoutしたため最終回答は未生成 | `eval-results/e2e-v384-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-608f323c69e2403eacf995a4735d964b.jsonl` |
| 2026-08-27 | Profile v385・公開買付け総合・Luna `high` | Graph ReviewをHypothesis単位にし、Cycle 2で`h-1`の本文統合後に`h-3`へ移動した。約287秒まで短縮したが、2回目の統合が全4 WorkItemを再評価し、無関係な`h-4`出力が16,384 tokenで不完全JSONとなったため`protocol_error`。v386で直近Tool結果の参照先だけを統合するよう修正した | `eval-results/e2e-v385-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-444459581ade4ba6b3a75f2c122cfcf2.jsonl` |
| 2026-08-27 | Profile v387・LR-027・公開買付け総合・Luna `high` | 全1067テストに合格。実モデルではCycle 1の`h-1`取得後、Cycle 2で`h-3`、同じCycle内の`h-4`へ順に移り、府令2条の5・10条を本文取得した。同一Hypothesisの残候補を追い続ける問題と早期Cycle Closeを解消した。Cycle 2終了後のEvidence統合チェックが約95秒かかり、全体420秒で`model_timeout`となったため最終回答は未生成 | `eval-results/e2e-v387-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-f0a2a26cc4204d2b9a0d9555ced6b019.jsonl` |
| 2026-08-27 | Profile v388・LR-004・公開買付け総合・Luna `high` | Cycle Closeの重複Evidence統合を除き、Provider共通の小型最終化schemaを導入した。Cycle 2境界は約95秒から約22秒、最終化入力は31,267から26,558 tokenへ減り、約53秒で限定回答本文を生成した。引用ID 1件の転記漏れは機械補完する回帰を追加し、全1068テストに合格した。再検証は別のGraph Evidence ID衝突（LR-028）により最終化前に停止した | `eval-results/e2e-v388-luna-overview/response-retry.json`、`eval-results/agent-framework-diagnostics/legal-ec28fee4d92e4311a0f45ea7369492c7.jsonl`、`eval-results/e2e-v388-luna-overview/response-final.json` |
| 2026-08-27 | Profile v389・LR-028・公開買付け総合・Luna `high` | Graph navigation Evidence IDへ関係内容hashを加え、同一Articleペアを異なるselectorで取得した際の衝突を解消した。実モデルでは府令2条の5・2条の4・2条の6・10条・9条を取得し、Graph衝突なく最終化へ到達した。最終化は48,090文字・schema 5,142文字、残り70.6秒で`model_timeout`となった | `eval-results/agent-framework-diagnostics/legal-909d02f5df2b42128ac406775005bd2e.jsonl` |
| 2026-08-27 | Profile v390・LR-004・公開買付け総合・Luna `high` | 法令アプリの最終化予約を35秒から90秒へ変更した。実モデルは2 Cycle・17モデル呼出しで正常完了し、必要Article 4/4と府令2条の4・2条の5・2条の6・10条を取得した。最終化は113.2秒を確保し、入力28,076 token、出力6,468 tokenの引用付き回答を生成した。全1069テスト合格 | `eval-results/agent-framework-diagnostics/legal-368d39950a6b4dd384498172941289dd.jsonl` |
| 2026-08-27 | Profile v392・LR-029・社内方針設問・Luna `high` | `supported`かつ`gaps`ありを後続探索へ残し、限定回答のopen WorkItemを正しく検証した。DependencyDecisionだけを更新する`continue`も状態差分として認めた。全1072テスト合格。実モデルは2 Cycle・24モデル呼出しで正常完了し、必要Article 4/4を取得して引用付き回答を生成した | `eval-results/agent-framework-diagnostics/legal-15e72e6b1e1546d1afdbc9a11a96c657.jsonl` |
| 2026-08-29 | Profile v395・LR-032・公開買付け総合・Luna `high` | Graph modeを`explicit_reference`から`reference_edges`へ変更し、`e58e2ac`でコミット。全1074テスト合格。実モデルは3 Cycle、345.3秒で正常完了し11/11。旧名称は完成schema・応答・traceに現れなかった。`reference_edges`の候補は2回とも0件であり、必要条文は別起点の正しい意味関係探索で回収した。最初の`IMPLEMENTS`で`to_subject`を選んだ意味方向の再発はLR-023へ記録 | `eval-results/e2e-v395-luna-overview/response.json` |
| 2026-08-30 | Profile v426・LR-023・公開買付け総合・Luna `high` | v425では目的を「具体化規定の確認」と説明しながら、4件の`IMPLEMENTS`要求を全て`to_subject`とした。端点役割と方向を共通Tool Prompt及びschemaで直接対応させ、Toolを選べるEvidence Integrationにも同じPromptを合成した。失敗fixtureの隔離再生は4/4正方向・修復なし。総合E2Eは3 Cycle、359.0秒で府令2条の5・10条を取得し、11/11。`answerCompleteness`は追加の未確認事項により`limited`。全1096テスト合格 | `agent-api/tests/fixtures/framework/tob_overview_graph_direction_v425.json`、`eval-results/checkpoint-replay/tob-overview-graph-direction-v426.json`、`eval-results/e2e-v426-luna-overview/response.json` |
| 2026-08-30 | Profile v427・LR-036・公開買付け総合・Luna `high` | `case_id + work_item_id`の論理sessionを導入し、4 WorkItemが各6 turnで同じsession IDを維持した。親の共通締切、入力順merge、一部timeout時の完了差分checkpointを実装し、全1100テスト合格。実モデルは2 Cycle・351.9秒で、Evidence Integrationのtimeoutはなく、30秒のCycle Close timeout後も予約時間内に限定回答を生成した。一方、府令10条本文を取得できず10/11相当であり、11/11の完了条件は未達。府令10条は検索抜粋として既知だったが、専属sessionへ提示する保留候補のscopeが不十分な別課題を確認した | `eval-results/e2e-v427-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-5ddf81320ae249c0ad412325c590e5f8.jsonl`、`eval-results/cycle-audits/v427-vs-v426/cycle-audit-comparison.md` |
| 2026-08-30 | Profile v428・LR-036・公開買付け総合・Luna `high` | 省略Evidenceを全sessionへ複製せず、発見Tool要求と既存Hypothesis対応の来歴でWorkItem別に投影した。全1101テスト合格。実モデルは4 Cycle・235.3秒、Evidence Integrationのtimeoutなしで完了し、v426比で123.6秒、入力105,036 token、Tool 7回を削減した。必要Articleは府令10条を欠く3/4で10/11相当のため、性能改善の確定及びLR-036完了とは扱わない。府令10条を発見・取得する探索品質は並列時間管理から分離して継続する | `eval-results/e2e-v428-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-87397ddfdea74e7aa60bec8a07b73c4f.jsonl`、`eval-results/cycle-audits/v428-vs-v426/cycle-audit-comparison.md` |
| 2026-08-30 | Profile v430・LR-036・混在Tool結果引継ぎ・公開買付け総合・Luna `high` | 本文取得、Graph検索及びOpenSearch検索が同時完了した場合、本文統合へ提示したTool結果だけを処理済みにするよう修正した。隠した探索結果はGraph / Search Reviewへ残り、府令10条はCycle 1のSearch Reviewから本文取得され、最終回答にも引用された。全1104テスト合格。実モデルは2 Cycle・358.9秒、資料3/3・必要Article 4/4・回答観点4/4の11/11相当。v428比で123.6秒、モデル呼出し4回、Tool 11回増えたため速度改善は未完了 | `eval-results/e2e-v430-luna-overview/response.json`、`eval-results/agent-framework-diagnostics/legal-e889d4efa220422884f5dbc4cbda22ed.jsonl`、`eval-results/cycle-audits/v430-vs-v428/cycle-audit-comparison.md` |
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
