# 非同期Relation分類 全件実行チェックリスト

> 本書は、全件意味分類を開始する前後の実行順と停止条件を示す運用チェックリストである。
> 設計判断は[`generic_iterative_agent_framework_plan.md`](generic_iterative_agent_framework_plan.md)、
> コマンドは[`RUNBOOK.md`](../../../RUNBOOK.md)を正とする。

## 全件実行を開始してよい条件

次のGate 0〜4がすべて完了するまで、全件のLuna Workerを開始しない。

## Gate 0: 契約を固定する

- [x] WorkerとReviewerは`gpt-5.6-luna`、reasoning effortは`high`で固定した
- [x] Worker / Reviewerの各session開始時にreasoning effortを明示し、異なる深度の成果物を同じRunへ混在させない
- [x] WorkerとReviewerを別session・別contextにした
- [x] 1 sessionの候補上限を5、同時に実行中のsession上限を3にした
- [x] 各候補について5 predicateを一度に比較する契約にした
- [x] ReviewerはWorkerの全回答を見て`approve / request_change`を返す契約にした
- [x] 差戻しは元のWorkerへ1回だけ返し、元のReviewerが差分を最終確認する契約にした
- [x] Programがpredicate、condition、finding、意味方向、根拠を補正しないことを契約テストへ固定した
- [x] skill version、model、reasoning effort、shard size、session上限、snapshotをmanifestへ記録する契約にした

合格条件: 実装計画、RUNBOOK、skill、Pydantic schema、テストfixtureに矛盾がない。

## Gate 1: 参照構造を正しくする

- [x] own Article title・見出しを`REFERENCES`として登録しない
- [x] 明示された法令名、`法 / 令 / 規則`、表の列scopeを先に解決する
- [x] 本則、附則、改正法令のscopeを区別する
- [x] 親法令参照を同じ下位法令の同番号Articleへ接続しない
- [x] `wrong_target`を既知catalog内の一意なArticleへだけ修正する
- [x] 正しい候補がcatalogにない参照は`unresolved`として除外する
- [x] 代表94件の構造評価が`94/94`になった
- [x] 直近20件比較で見つかった誤った教師targetを正本fixtureで修正した

2026-08-19に保存済みe-Gov XML snapshot（14法令、16,949 Content Unit）からshadow構築し、
正解73組を全て保持し、`not_reference / unresolved`の21組を全て除外したことを確認した。
Gate 5でOpenSearch / Neo4jを実際に再構築した後、同じ`94/94`を再確認する。

合格条件: 意味分類へ渡す全候補が、構造的に検証済みのArticleペアである。

## Gate 2: export・shard IFを完成させる

- [x] 同じsnapshotのNeo4j `REFERENCES`とOpenSearch Article全文から候補をexportできる
- [x] 各候補にcandidate key、全basis edge、両Article全文、全参照出現、offset、span、content hashがある
- [x] 1つのcandidate keyと各basis edgeが複数shardへ重複しない
- [x] shardは決定的な順序で最大5件、最後だけ5件未満になる
- [x] manifestの候補集合と全shardの和集合が一致する
- [x] 中断後に完了済み候補を除外して再開できる
- [x] gold、expected predicate、旧heuristicをblind packetへ入れない

exportは`codex_subscription / gpt-5.6-luna`を候補keyの一部として固定し、
入力順を変えた2回の生成で同一JSONLとなる契約テストを追加した。
Gate 5の再構築後にlive indexの実成果物でhashを再確認する。

合格条件: exportを2回実行して、manifest・候補key・shard割当が同一になる。

## Gate 3: Worker・Reviewer・差戻し IFを完成させる

- [x] Worker出力は入力候補ごとに1 recordあり、過不足・重複がない
- [x] 全recordに5 predicateの二条件とfindingがある
- [x] 二条件とfindingの代数が一致する
- [x] 成立predicateだけがAssertionを持つ
- [x] Assertionの端点、occurrence hash、source/target spanが入力の既知IDである
- [x] Reviewer出力はWorker全recordに対応し、全5 predicateを検査する
- [x] `approve`はissuesなし、`request_change`は具体的issueありという代数を満たす
- [x] revision packetには`request_change`候補だけが含まれる
- [x] revisionは同じcandidate keyの完全な置換recordである
- [x] final Reviewで再度`request_change`なら`unresolved`へ分離し、2回目の差戻しを行わない
- [x] 単件、5件、候補順入替えで候補別判断・ID対応が変わらないfixtureが通る

`prepare_adjudication_revisions`と`merge_once_revised_adjudications`はReviewerの状態に従って
成果物を振り分けるだけで、predicate、condition、finding、方向、根拠を修正しない。

合格条件: fake成果物による正常系・差戻し・未解消・不正ID・欠落・重複のIFテストがすべて通る。

## Gate 4: import・checkpoint・publish IFを完成させる

- [x] Luna JSONLをリポジトリのPydantic契約で検証してから取り込む
- [x] candidate key、snapshot、schema version、prompt/skill version、modelがRunと一致する
- [x] 候補単位でcheckpointとAssertionを同一transactionに保存する
- [x] 同じkey・同じpayloadの再importをskipする
- [x] 同じkey・異なるpayloadを上書きせずRun失敗にする
- [x] 同じRun・候補・predicateの重複Assertionを拒否する
- [x] `failed` checkpointが1件でもあればpublishを拒否する
- [x] 未承認recordをAssertionへ変換しない
- [x] `unresolved`件数とcoverageをRunへ保存し、関係不存在として扱わない
- [x] dry-runではNeo4jを更新しない
- [x] 中断後の再importで完了済みcheckpointを再保存しない

import入力はWorker回答単体ではなく、元packet・Worker回答・Reviewerの`approve`・
差戻し回数を一体化した`ApprovedAdjudicationRecord`とする。Reviewerを経ていない
Worker JSONLはPydantic契約を通らず、Assertionへ変換できない。

合格条件: export→fake Worker→fake Reviewer→revision→import→中断再開→監査→publishの統合テストが通る。

## Gate 5: 保存済みsnapshotから再構築する

- [x] e-Gov APIを再取得せず、保存済みXML snapshotを使用する
- [x] 検証環境の回答処理を停止している
- [x] 同じmanifestからOpenSearchとNeo4jを再構築した
- [x] 両方の`sourceSnapshotId`、Article ID、revision、content hashが対応する
- [x] Graph schema inventoryに旧`MENTIONS / APPLIED_BY`がない
- [x] 代表94件の構造評価を再実行して`94/94`を確認した
- [x] 再構築後の正式な候補総数とshard数を記録した

合格条件: 不一致snapshotを公開せず、再構築後の構造監査がすべて成功する。

2026-08-19に保存manifest
`egov-law-corpus-4458d52586f9a2a4233e05ffc7e06f07c9c5429a4916043ad233908a4d911e1c`
から再構築した。結果はOpenSearch 16,459文書、Neo4j 17,254 node / 34,206 edge、
共通`sourceSnapshotId=snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e`、
schema version 9、Graph監査違反0、構造評価`94/94`だった。

初回再構築後の差分監査で、`改正前の<法令名>第N条`を現行Articleへ接続する誤りを検出した。
明示法令名が`改正前の / 改正前における`で修飾される参照は、旧版Articleを一意に解決できない限り
現行Articleへ接続しない。保存済みsnapshotから再seedした結果、誤った8 occurrenceを除外し、
`REFERENCES`は16,964件となった。構造評価は引き続き`94/94`、Graph監査違反は0である。

旧exportは`REFERENCES`出現をそのまま同数の候補へ投影する不具合があった。
2026-08-19に候補単位を有向Articleペアへ修正し、候補の`basisEdgeIds`と各
`referenceOccurrence.basisEdgeId / referenceKind`で物理Relationとの対応を保持する契約へ変更した。
旧版Article誤接続の除外後に再exportした結果は14,454候補、16,964 basis edge、
最大5候補の2,891 shardである。
複数basis edgeを持つ候補は1,556件、1候補の最大basis edge数は31件だった。
manifest schemaは2、promptは`legal-relation-5predicate-v20-pair`、skillは
`legal-relation-adjudicator-2026-08-19-pair-v2`で固定した。全basis edgeはmanifestで一度ずつ被覆され、
最大shard入力は210,366文字だった。誤った旧packetは全件Lunaへ渡していない。

上記v2は初回Gate 6成果物の再現用versionである。その差分監査後に契約を更新し、正本skillを
`.agents/skills/legal-relation-adjudicator`へ移した。次の差分再評価からは
`legal-relation-adjudicator-2026-08-19-pair-v3`を使用し、v2成果物と同じRunへ混在させない。
差分再評価で判明した無名の法的役割、前方スコープ、無関係な逆参照の境界は
`legal-relation-adjudicator-2026-08-19-pair-v4`へ一般則として反映した。

## Gate 6: 代表100件を最大3並列で再評価する

- [x] goldをWorker・Reviewerへ渡していない
- [x] 新しいWorker / Reviewer sessionを使い、過去評価のcontextを引き継いでいない
- [x] 1 shard最大5候補、同時に実行中のsession最大3を守った
- [x] 構造レーン94件が`94/94`である
- [x] 意味分類可能な全候補でstatus・5 predicate・SUBJECT / OBJECT・groundingが正解と一致する
- [x] ガイドレーン6件が`6/6`である
- [x] final Review後のworkflow上の`unresolved`が0件である
- [x] 全JSONLがGate 3・4のIF検証を通る
- [ ] Worker / Reviewer / revision別の時間、差戻し率、context使用量を記録した

合格条件: 上記をすべて満たす。1件でも意味不一致またはIF違反があれば全件実行を開始せず、原因を修正して誤答対象だけを再確認する。goldへ合わせる個別例外は追加しない。

### Gate 6初回実行の停止記録（2026-08-19）

構造的に有効な73 Articleペアを15 shardへ分け、Luna `high`のWorker / Reviewerを最大3 sessionで
ブラインド実行した。Reviewerは5候補を1回だけ差し戻し、同じWorkerとReviewerで再判定した。
workflow上の最終`unresolved`は0、statusは`73/73`一致、5 predicateは`355/365`一致、
候補単位のpredicate完全一致は`63/73`だったためGate 6を停止した。

差分を人間が再監査した結果、初回スコアには次が混在していた。

- Lunaの意味誤判定: 参照出現と意味役割の結び付け不足、読替表・明示的不適用の見落とし
- edge単位goldからArticleペアgoldへ移した際の教師データ誤り
- 複数の直接根拠spanが成立し得る候補を、単一spanとの完全一致だけで不正解にする採点不備

対処は、特定Articleの正解をPromptへ加えることではない。各predicateについて、選択した
`referenceOccurrence`が両端の意味役割を実際に橋渡しすることをWorker / Reviewerへ要求する。
goldはCodexによる本文再監査で修正し、複数の妥当なgroundingは人が明示した許容集合として保存する。
Programは許容集合とのID一致だけを検査し、新しい意味や許容spanを推測しない。

評価IFは修正済みである。単一edge 3件を含む計18件の人手overrideを旧goldより優先し、
6 candidate/predicateのgrounding許容集合を別成果物として検証する。同じv2 Luna成果物を再採点した
監査値はstatus `73/73`、5 predicate `356/365`、候補完全一致`63/73`、grounding `54/62`となった。
これは過去出力の再採点であり、v3の新規実行ではない。

残る10候補をgold・過去出力なしの新規Luna Worker / Reviewer contextで再評価した。本文監査により、
第140条2項の期間を再利用可能な定義としたgoldと、対象Articleを直接`適用しない`のにOVERRIDESを
否定したgoldの2件を訂正した。pair-v3は訂正goldに`7/10`、残る3件へ一般化した境界規則を加えた
pair-v4は`3/3`だった。v2の一致63件、v3の一致7件、v4の一致3件を評価用に合成した最終採点は、
status `73/73`、5 predicate `365/365`、方向 `62/62`、grounding `62/62`、候補完全一致`73/73`である。
成果物は`eval-results/relation-guidance-100-pair-v3-diff/`と
`eval-results/relation-guidance-100-pair-v4-diff/`に保存する。異なるskill versionの結果は
同一ClassificationRunへimportしない。

`adjudicationStatus=needs_resolution`は「入力Articleペアを意味分類できない」というLLM判断であり、
final Review後のworkflow `unresolved`は「差戻し1回後もReviewerが承認しなかった」という実行結果である。
両者を同じ0件条件として扱わない。

## Gate 7: 全件分類を最大3並列で実行する

- [ ] `phase=building`のClassificationRunを作成した
- [ ] 5件shardをキューへ登録した
- [ ] 最大3 active sessionでWorker→Reviewer→必要時revision→final Reviewを実行した
- [ ] 各shard完了時に候補別成果物を検証し、checkpointへimportした
- [ ] 中断時は新しいRunを作らず同じRunを再開した
- [ ] `failed / unresolved / request_change`を隠して処理済みにしていない
- [ ] 全候補完了後に件数、hash、重複、端点、span、coverageを監査した
- [ ] `failed=0`を確認した
- [ ] 抜き取り監査と高リスク候補監査を完了した
- [ ] 明示的なpublish操作でだけ`published`へ遷移した

## Gate 8: 検索へ接続する

- [ ] Case開始時に1つのpublished `classificationRunId`を固定する
- [ ] Tool Adapterが固定Run以外のAssertionを返さない
- [ ] SolverがHypothesisに沿った1 predicate・1 direction・構造filterを明示する
- [ ] Programが別predicateを追加しない
- [ ] raw `REFERENCES`と`semantic_assertion`を混同しない
- [ ] 最初の検索動作確認はReviewer無効、research/integrationともOllama `gemma4:e4b`で行う
- [ ] 検索失敗時は別モデルへ切り替える前に、実装、契約、Prompt、入力、traceを確認する
- [ ] 公開買付けを含む代表2問で必要条文へ到達する
- [ ] 問題があればClassificationRunを上書きせず、新しいRunとして再分類する
