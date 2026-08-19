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

- [ ] 同じsnapshotのNeo4j `REFERENCES`とOpenSearch Article全文から候補をexportできる
- [ ] 各候補にcandidate key、basis edge、両Article全文、全参照出現、offset、span、content hashがある
- [ ] 1つのcandidate keyとbasis edgeが複数shardへ重複しない
- [ ] shardは決定的な順序で最大5件、最後だけ5件未満になる
- [ ] manifestの候補集合と全shardの和集合が一致する
- [ ] 中断後に完了済み候補を除外して再開できる
- [ ] gold、expected predicate、旧heuristicをblind packetへ入れない

合格条件: exportを2回実行して、manifest・候補key・shard割当が同一になる。

## Gate 3: Worker・Reviewer・差戻し IFを完成させる

- [ ] Worker出力は入力候補ごとに1 recordあり、過不足・重複がない
- [ ] 全recordに5 predicateの二条件とfindingがある
- [ ] 二条件とfindingの代数が一致する
- [ ] 成立predicateだけがAssertionを持つ
- [ ] Assertionの端点、occurrence hash、source/target spanが入力の既知IDである
- [ ] Reviewer出力はWorker全recordに対応し、全5 predicateを検査する
- [ ] `approve`はissuesなし、`request_change`は具体的issueありという代数を満たす
- [ ] revision packetには`request_change`候補だけが含まれる
- [ ] revisionは同じcandidate keyの完全な置換recordである
- [ ] final Reviewで再度`request_change`なら`unresolved`へ分離し、2回目の差戻しを行わない
- [ ] 単件、5件、候補順入替えで候補別判断・ID対応が変わらないfixtureが通る

合格条件: fake成果物による正常系・差戻し・未解消・不正ID・欠落・重複のIFテストがすべて通る。

## Gate 4: import・checkpoint・publish IFを完成させる

- [ ] Luna JSONLをリポジトリのPydantic契約で検証してから取り込む
- [ ] candidate key、snapshot、schema version、prompt/skill version、modelがRunと一致する
- [ ] 候補単位でcheckpointとAssertionを同一transactionに保存する
- [ ] 同じkey・同じpayloadの再importをskipする
- [ ] 同じkey・異なるpayloadを上書きせずRun失敗にする
- [ ] 同じRun・候補・predicateの重複Assertionを拒否する
- [ ] `failed` checkpointが1件でもあればpublishを拒否する
- [ ] 未承認recordをAssertionへ変換しない
- [ ] `unresolved`件数とcoverageをRunへ保存し、関係不存在として扱わない
- [ ] dry-runではNeo4jを更新しない
- [ ] 中断後の再importで完了済みcheckpointを再保存しない

合格条件: export→fake Worker→fake Reviewer→revision→import→中断再開→監査→publishの統合テストが通る。

## Gate 5: 保存済みsnapshotから再構築する

- [ ] e-Gov APIを再取得せず、保存済みXML snapshotを使用する
- [ ] 検証環境の回答処理を停止している
- [ ] 同じmanifestからOpenSearchとNeo4jを再構築した
- [ ] 両方の`sourceSnapshotId`、Article ID、revision、content hashが対応する
- [ ] Graph schema inventoryに旧`MENTIONS / APPLIED_BY`がない
- [ ] 代表94件の構造評価を再実行して`94/94`を確認した
- [ ] 再構築後の正式な候補総数とshard数を記録した

合格条件: 不一致snapshotを公開せず、再構築後の構造監査がすべて成功する。

## Gate 6: 代表100件を最大3並列で再評価する

- [ ] goldをWorker・Reviewerへ渡していない
- [ ] 新しいWorker / Reviewer sessionを使い、過去評価のcontextを引き継いでいない
- [ ] 1 shard最大5候補、同時に実行中のsession最大3を守った
- [ ] 構造レーン94件が`94/94`である
- [ ] 意味分類可能な全候補でstatus・5 predicate・SUBJECT / OBJECT・groundingが正解と一致する
- [ ] ガイドレーン6件が`6/6`である
- [ ] final Review後の`unresolved`が0件である
- [ ] 全JSONLがGate 3・4のIF検証を通る
- [ ] Worker / Reviewer / revision別の時間、差戻し率、context使用量を記録した

合格条件: 上記をすべて満たす。1件でも意味不一致またはIF違反があれば全件実行を開始せず、原因を修正して誤答対象だけを再確認する。goldへ合わせる個別例外は追加しない。

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
