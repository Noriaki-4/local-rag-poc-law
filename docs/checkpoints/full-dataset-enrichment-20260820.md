# 大規模法令関係分類の凍結チェックポイント（2026-08-20）

## 目的

14法令・14,454候補を対象にした全件意味分類は、費用と処理時間が個人で継続するには大きく、
参照先Articleの構造判定にも未解決の問題が見つかったため凍結した。本書は、将来同じdatasetへ
戻る際に、XML取得や完了済みshardを無駄にせず再開するための正本である。

小規模datasetの内容は本チェックポイントでは決めない。別途相談して決定する。

## Git上の再開地点

- 再開ブランチ: `archive/full-dataset-enrichment-wip-20260820`
- 再開タグ: `checkpoint/full-dataset-enrichment-20260820`
- 凍結前の基点commit: `26dea8479b174dfca3b9d5e618f35a699bc13da9`
- 状態: Graph参照抽出はWIP。Neo4j/OpenSearchへ反映してはならない

タグは本書を含むWIP commitへ付ける。実行結果そのものはGitで管理しない。

## 固定入力

| 項目 | 値 |
|---|---|
| e-Gov dataset snapshot | `egov-law-corpus-4458d52586f9a2a4233e05ffc7e06f07c9c5429a4916043ad233908a4d911e1c` |
| source snapshot | `snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e` |
| Graph schema | `9` |
| prompt | `legal-relation-5predicate-v22-pair` |
| classification run | `classification-run-31d3e444232c594230ce1306232d750a` |
| 候補数 | 14,454 |
| shard数 | 2,891 |
| 1 shard上限 | 5候補 |

実行profileはWorker・Reviewerとも`gpt-5.6-luna`、reasoning effortは`high`、最大3 session、
意味差戻しは1回である。詳細はアーカイブ内`input/manifest.json`を正とする。

## 停止時点

`orchestration/state.json`には359 shardが記録されている。

| stage | shard数 | 承認レコード | 未解決レコード |
|---|---:|---:|---:|
| imported | 323 | 1,596 | 19 |
| failed | 33 | 0 | 0 |
| worker_complete | 2 | 0 | 0 |
| initial_review_complete | 1 | 0 | 0 |

分類処理は停止済みであり、再開コマンドを実行してはならない。まず後述の構造判定Gateを完了する。

## Git外アーカイブ

保存先:

`/Users/nt/project-artifacts/local-rag-poc-law/2026-08-20/`

| 内容 | ファイル | SHA-256 |
|---|---|---|
| 分類packet・shard・Worker/Reviewer出力・queue state | `relation-full-v22-pair-v7.tar.zst` | `f17d9399ea94631490cf1a86dcbbb6e6b60154a8925d3bb1388d5abfe8dea90f` |
| 固定e-Gov XML corpus | `egov-law-corpus-4458d52586f9a2a4233e05ffc7e06f07c9c5429a4916043ad233908a4d911e1c.tar.zst` | `3bb2f8a6a73f85795172211a88e244e589904a081423df8ef35ca0a45689db4a` |

アーカイブは`zstd -t`で検査済み。`.sha256`ファイルも同じディレクトリに保存している。
これは同一ディスク上の退避であり、長期保存する場合は別媒体にも複製する。

## 復元

リポジトリrootから次を実行する。

```bash
mkdir -p eval-results
zstd -dc /Users/nt/project-artifacts/local-rag-poc-law/2026-08-20/relation-full-v22-pair-v7.tar.zst \
  | tar -xf - -C eval-results

mkdir -p datasets/lawqa_jp
zstd -dc /Users/nt/project-artifacts/local-rag-poc-law/2026-08-20/egov-law-corpus-4458d52586f9a2a4233e05ffc7e06f07c9c5429a4916043ad233908a4d911e1c.tar.zst \
  | tar -xf - -C datasets/lawqa_jp
```

復元後、実行前に次を照合する。

- アーカイブのSHA-256
- `input/manifest.json`のsource snapshot、Graph schema、prompt、model
- `orchestration/state.json`と成果物の整合
- Git tagと作業treeのcode revision

## 再開前の必須Gate

意味分類LLMへ渡すArticleペアが構造的に正しい、という前提が全件Runで崩れた。少なくとも次を
解消するまで、既存結果をNeo4jへ反映したり、残りshardを実行したりしない。

- e-Gov XMLの条・項・号・Sentence・表行・表セルを平文化前に保持する
- `同法`、外部法令、旧法、改正法、本則・附則、前条・次条を参照先判定LLMで解決する
- LLMは既知候補Article IDからだけ選択し、候補がなければ`unresolved`とする
- 構造判定済みArticleペアだけを5 predicateの意味分類へ渡す
- 停止済み1,615件を構造判定し直し、誤ったbasis edgeを含む結果を再利用しない
- シャドー差分と代表fixtureが合格してから、明示的な承認を得て再開する

## 現時点で再利用できるもの

- 固定e-Gov XML snapshotとmanifest
- shard分割、checkpoint、Worker/Reviewer分離、1回だけの差戻しという実行方式
- 5 predicateの意味分類契約
- 構造判定用`legal-reference-structure-auditor`
- 完了済み出力のうち、将来の構造判定Gateを再通過したレコード

現在の1,596承認レコードは、そのまま正解や公開可能結果とは扱わない。構造判定後に再利用可否を
candidate単位で決定する。
