# AWS初期bootstrapデータの暫定運用

> status: 暫定対応（有効）
>
> 記録日: 2026-09-01
>
> 関連課題: `AWS-009`、`AWS-016`、`AWS-017`

## 1. この文書の目的

AWS移行の初期検証では、時間制約により正規seedと非同期Relation分類を再実行しない。
代わりに、現在の検索検証で利用しているOpenSearch全件データと、Neo4jの公開買付けmini Graph・
公開済み意味分類結果を固定成果物として引き継ぐ。

この構成はSearchとGraphの対象範囲およびsnapshotが一致しないため、通常のデータ更新仕様ではなく
初期AWS疎通のための暫定対応として扱う。正規seed、非同期分類、同一入力からの再構築を置き換える
恒久仕様ではない。

## 2. 暫定構成

| 用途 | 利用するデータ |
|---|---|
| Search | OpenSearch index `legal-rag-content-ja-v2` |
| Search snapshot | `snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e` |
| Search範囲 | 20文書、16,459 Content Unit |
| Search内訳 | e-Gov 14法令・14,742 Content Unit、6ガイドライン・1,717 Content Unit |
| Graph | Neo4jの公開買付け3階層mini Graph |
| Graph snapshot | `snapshot-020185f383d15088b066cfbea48ff5379db05c4e1b48d69d67f209df57f0da46` |
| Graph範囲 | 124 node、172 edge、3 Document、13 Article、schema version 9 |
| 意味分類 | `classification-run-public-tender-mini-v1-v23` |
| 意味分類状態 | `published`、17候補処理、24 RelationAssertion |

原本とscenarioの監査には次を使う。

- e-Gov corpus: `datasets/lawqa_jp/egov_law_corpus/manifest.json`
- ガイドライン: `datasets/lawqa_jp/external-guidance/manifest.json`
- 公開買付けscenario: `datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json`

「20文書」は「e-Gov 20法令」ではない。実データはe-Gov 14法令と6ガイドラインである。

## 3. snapshotが異なることによる制約

Search snapshotとGraph snapshotの一致をこのbootstrapの前提にしない。各snapshot、manifest、件数、
ClassificationRunを独立して検証し、両データ間の対応には既存の`documentId`、Article ID、
`contentUnitId`等の安定IDを使う。

このため、初期AWS環境には次の制約がある。

- OpenSearchでは20文書全体を検索できる。
- Neptune AnalyticsでGraph探索できるのはmini Graphに含まれる3 Document・13 Articleだけである。
- Search結果がmini Graphの範囲外の場合、Graph結果がないことをデータ不整合や検索失敗と断定しない。
- Graph探索が空でも、OpenSearchとS3の本文根拠を使う検索経路を継続できる必要がある。
- この構成を使った評価結果では、全件Graphを利用した結果であるかのように扱わない。

## 4. export時の安全条件

exportは`infra/aws/scripts/export-existing-bootstrap-data.py`でread-onlyに行う。

- DBを削除、更新、再seedしない。
- 非同期Relation分類を再実行しない。
- `LEGAL_RELATION_CLASSIFICATION_RUN_ID`の環境変数が空でも、それだけでRunを選択しない。
- 指定したClassificationRunをNeo4jから取得し、`phase=published`かつ指定Graph snapshot所属であることを確認する。
- OpenSearch文書IDをe-Gov・ガイドラインmanifestと照合する。
- scenarioの親dataset snapshotをe-Gov corpus manifestと照合する。
- bge-m3 embeddingはTitan Text Embeddings V2と互換性がないためexportしない。
- 出力ファイルごとのSHA-256と件数を`manifest.json`へ記録する。
- kuromoji / ICU analysisとmappingを一体のportableなindex定義として保存する。
- 原本manifestと、manifestから参照する14 XML・6 PDFを成果物内へ複製し、一時的な兄弟repo依存を残さない。

2026-09-01のP0実装後の最終read-only検証では、次の一時ディレクトリへschema version 3でexportした。

```text
/tmp/local-rag-law-p0-final.NVLtX7/export
```

この検証時の`manifest.json` SHA-256は
`60fa9dd40a6be7bb7ab6bd58d4cfa4d1da64d81c46dbd140f818f7a4e3cc2c01`である。

これはローカルの一時検証成果物であり、正本でも永続保管先でもない。リポジトリ、S3、AWS環境には
まだ配置していない。再利用時は一時パスの存在を前提にせず、稼働データから再exportしてmanifestと
hashを再検証する。

## 5. AWSへ投入するときの扱い

- export成果物をS3の環境別bootstrap prefixへ配置する。
- OpenSearch Serverlessにはkuromoji + ICU mappingを作成する。
- 16,459 Content Unitのdocument vectorをTitan Text Embeddings V2、1024次元、正規化ありで再生成する。
- Neptune AnalyticsにはGraph snapshotの124 node・172 edgeだけを投入する。
- Runtimeの`LEGAL_RELATION_CLASSIFICATION_RUN_ID`には公開済みRun IDを明示する。
- `minio://`のprocessed objectはContent Unit本文をS3 objectとして保存してURIを書き換え、ガイドラインの
  source objectは成果物内の原本PDFを指すS3 URIへ書き換える。HTTPのe-Gov source URLは維持する。
- Graph 124 node / 172 edgeは小規模なため、専用bulk CSVへ変換せず、parameterized openCypherを
  `graphNodeId` / `graphEdgeId`で冪等実行する。別snapshotが既にあるGraphへは混在投入しない。
- Neptune openCypherで保存できないlist propertyは、AWS投入adapterでversion付きJSON文字列へ変換する。
  固定成果物は変更せず、AgentCore Runtime adapterがquery結果をローカルGraph契約のlistへ復元する。
- private endpointへの投入はapplication subnetのone-off ECS taskから行う。task roleだけにwrite権限を付け、
  利用者向けAgentCore Runtimeのroleはread-onlyのままにする。
- S3配置、Titan再Embedding、OpenSearch投入、Neptune投入が終わるまで`AWS-016`を完了にしない。

## 6. 暫定対応の終了条件

次を満たした時点で、この非対称bootstrapを終了する。

1. 認証された管理taskから正規seedを安全に実行できる。
2. 同じ入力manifestからSearchとGraphを再構築し、各成果物の対応関係を監査できる。
3. 必要なRelation候補を再開可能な非同期処理で分類、監査、import、publishできる。
4. 全件Graphを使うか、対象を限定したGraphを正式仕様とするかを決定し、評価条件へ反映する。
5. 新旧データで検索・Graph探索・引用を比較し、rollback可能な状態でRuntimeの参照先を切り替える。
6. `bootstrapData`を新しいsnapshotとRunへ更新し、`AWS-009`、`AWS-016`、`AWS-017`の状態を見直す。

暫定対応を終了しても、この記録は判断経緯と移行時の監査証跡として残す。
