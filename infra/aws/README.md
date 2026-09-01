# AWS infrastructure

このディレクトリは、ローカルDocker版の機能をGenUの法令検索バックエンドとしてAWS上で
検証するためのAWS固有リソースを管理する。GenUがUI、ユーザー認証、会話操作を担当し、
このリポジトリは法令質問の処理、検索、Graph探索、根拠付き回答を担当する。

検索ロジック、Agent契約、法令Domainの実装はここへ複製せず、既存のアプリケーションコードを
GenUとAWS data serviceへ接続するための構成だけを置く。

```text
GenU
  -> Bedrock AgentCore Runtime（Legal Agent backend）
      -> OpenSearch Serverless / Neptune Analytics LPG / S3
      -> Bedrock Claude Haiku 4.5 / Titan Text Embeddings V2
```

GenUからOpenSearch等を直接呼ばせない。GenU向けのrequest / streaming responseと既存Agent契約の
変換はAgentCore adapterへ閉じ込め、検索・回答ロジックへGenU固有型を持ち込まない。AWSへ独自の
`agent-ui`はdeployせず、既存Streamlit UIはローカル診断用として扱う。

利用者向けLegal AgentはBedrock AgentCore Runtimeで実行する。ECS clusterはAgentの公開API基盤ではなく、
前処理、seed、評価など、AgentCore Runtimeから分離する必要がある補助workloadの候補とする。
現行の有効な検索・回答経路はrerankerを呼ばないため、reranker APIとそのECR repositoryはAWS移行対象にしない。
Gemma 4はAWSへ移行せず、Agentの生成LLMはOllamaではなくAmazon Bedrockへ接続する。
AgentCore Runtimeでは`RERANK_PROVIDER=none`と`LLM_PROVIDER=bedrock`を明示し、旧ローカル既定値への
暗黙のfallbackを許可しない。生成modelは環境設定のJapan geo inference profileへ固定し、GenU requestの
model値はログへ残すだけでRuntimeの許可modelを変更しない。
Embedding providerは生成LLMと分け、`AWS-004`でTitan Text Embeddings V2へ置き換える。

初期データサービスは、OpenSearch Serverlessのprivate `VECTORSEARCH` collection、Neptune Analyticsの
LPG + openCypher、Bedrock Titan Text Embeddings V2（`amazon.titan-embed-text-v2:0`、1024次元）とする。
日本語全文検索ではJapanese（kuromoji）AnalysisとICU Analysisを必須とし、現行の日本語mappingを
実collectionで互換性確認する。
`poc`は初期OCUを抑えるため`standbyReplicas: DISABLED`とする。この値の変更は既存collectionへ
反映できないため、変更時は新collectionの作成、Titan V2での再Embedding・再投入、検索比較、
endpoint切替、旧collection保持の順で扱う。

## 初期データの扱い

この非対称な初期データ構成は暫定対応である。理由、既知制約、安全条件、終了条件は
[AWS初期bootstrapデータの暫定運用](BOOTSTRAP-DATA.md)を正本とする。

時間制約のある初期検証では、正規seedと非同期Relation分類の再実行を保留し、ローカルで公開済みの
検索全件データと公開買付けmini Graphを使う。両者は別snapshotである。対象snapshot、source index、
原本・ガイドライン・scenario manifest、ClassificationRun、S3 prefixは
`config/environments/poc.json`の`bootstrapData`を正本とする。

稼働中のローカルOpenSearchとNeo4jから、指定した検索snapshotとGraph snapshotだけを固定成果物へ
read-onlyでexportする。`LEGAL_RELATION_CLASSIFICATION_RUN_ID`の現在値には依存せず、指定RunがNeo4j上で
`published`かつ指定Graph snapshotに属することを確認する。

```bash
python3 infra/aws/scripts/export-existing-bootstrap-data.py \
  --output-dir /tmp/local-rag-law-bootstrap \
  --opensearch-index legal-rag-content-ja-v2 \
  --search-snapshot-id snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e \
  --graph-snapshot-id snapshot-020185f383d15088b066cfbea48ff5379db05c4e1b48d69d67f209df57f0da46 \
  --source-corpus-manifest ../local-rag-poc-law/datasets/lawqa_jp/egov_law_corpus/manifest.json \
  --source-guidance-manifest ../local-rag-poc-law/datasets/lawqa_jp/external-guidance/manifest.json \
  --scenario-manifest ../local-rag-poc-law/datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json \
  --classification-run-id classification-run-public-tender-mini-v1-v23
```

出力は`manifest.json`、kuromoji / ICU analysisを含む`opensearch-index.json`、embeddingを除いた文書JSONL、
Graph node / edge JSONL、原本manifestとその原本ファイルである。
既存のbge-m3 vectorはTitan V2と互換性がないためexportせず、AWSへの投入時だけ再生成する。
exportは既存データを変更しない。出力先は誤上書きを防ぐため空ディレクトリに限定する。
確認済みの検索側は20文書（e-Gov 14法令＋6ガイドライン）、16,459 Content Unit、Graph側は
124 node / 172 edgeである。検索は全件を対象にできるが、Graph探索できる範囲はmini Graph内に限られる。

export後はAWSへ接続せずに、全file hash、snapshot、Run、件数、embedding除外を再検証できる。

```bash
python3 infra/aws/scripts/bootstrap_aws_data.py \
  --config infra/aws/config/environments/poc.json \
  --artifact-dir /tmp/local-rag-law-bootstrap
```

AWS投入では、成果物をS3へ置いた後、private OpenSearch / Neptune endpointへ到達できるapplication subnetで
一度だけECS bootstrap taskを起動する。taskはAgentCoreとは別image・別roleで、Titan再Embedding、index投入、
Graph投入、件数検証だけを行う。正規seedやRelation分類の入口は持たない。

```bash
AWS_PROFILE=rag-poc-admin aws s3 sync \
  /tmp/local-rag-law-bootstrap \
  's3://<KnowledgeBucketName output>/knowledge-root/bootstrap/current-search-validation/artifact/'

AWS_PROFILE=rag-poc-admin infra/aws/scripts/build-bootstrap-image.sh
AWS_PROFILE=rag-poc-admin infra/aws/scripts/run-bootstrap-task.sh
```

OpenSearch投入はmanifest hashを含むcheckpointをS3へbatchごとに保存し、同じIDを`index`するため、one-off
ECS taskを作り直しても完了batchの次から再開できる。Serverless vector collectionは明示的な文書`_id`を
受け付けないため、AWS側の文書IDは自動生成し、安定IDは`contentUnitId`としてsourceへ保持する。
再実行時はS3 checkpoint件数と実index件数が一致しない場合に停止し、重複投入を許さない。
ローカルsnapshotのvector engineは`lucene`だが、Serverlessへのindex作成時だけ`faiss`へ変換する。
元のbge-m3 vectorは成果物に含まずTitanで再生成するため、snapshot本文やsource metadataは変更しない。
task実行スクリプトは16,459件のTitan再Embeddingを考慮して最大12時間待つ。環境ごとに変更する場合は
`BOOTSTRAP_WAIT_TIMEOUT_SECONDS`を正の秒数で指定する。待機がtimeoutしてもtaskを停止・削除はしないため、
表示されたtask ARNの状態を確認してから再実行する。
Embeddingは既定2 workerで並列実行し、Titanのthrottling時は指数backoffする。実行環境のquotaを確認して
変更する場合だけ`BOOTSTRAP_EMBEDDING_WORKERS`を1〜32で指定する。bulk投入とcheckpoint更新は直列である。
Graphは`graphNodeId` / `graphEdgeId`で`MERGE`し、別snapshotが存在するGraphには混在投入せず停止する。
正規seedやRelation分類はこのコマンドから起動できない。

正規seedと非同期処理は削除しない。既存のRUNBOOKとCLIを保ち、AWS用adapterと管理taskが揃った後に
同じmanifest・checkpoint・publish監査を使って再開可能にする。初期bootstrapは正規seedの代替正本ではなく、
固定snapshotを短時間でAWSへ再現するための経路である。

## 管理対象

- IaCとその環境別設定
- IAM policy、コンテナ実行設定、AWSサービス固有の設定
- build、deploy、seed、疎通確認に必要なAWS向けスクリプト
- AWS構成の前提と手動運用手順

次は管理対象に含めない。

- `agent-api`の検索・Agentロジック
- 法令データセットや評価結果の複製
- AWS認証情報、API key、秘密値
- GenU本体のUI、Cognito、会話履歴
- Terraform state、生成済みplan、CDKの生成物

## 文書

- AWS移行全体の構想: [Step2 AWS実現イメージ・移行計画](../../docs/step2_transition_plan.md)
- 初期データの暫定運用: [BOOTSTRAP-DATA.md](BOOTSTRAP-DATA.md)
- AWS移行課題: [ISSUES.md](ISSUES.md)
- 法令検索側の課題: [法令検索 課題管理](../../docs/legal_retrieval_issue_tracker.md)

AWS移行課題は`ISSUES.md`を正本とし、このREADMEには進捗一覧を重複して持たない。
法令検索の意味、Prompt、Agent状態遷移に関する課題は既存の法令検索課題へ残し、AWS側では
接続方式、実行基盤、永続化、運用上の依存だけを管理する。

## 初期方針

- 最初は`poc`の1環境だけを作るが、環境名をIaCやアプリケーションコードへ固定しない。
  必要になった環境は、既存定義の複製ではなく環境別設定の追加で作れるようにする。
- IaCは採用方式を決定してから追加し、複数方式を並行して管理しない。
- AWSリソースはサービス名ではなく、network、data、compute、observabilityなど
  更新ライフサイクルが近い単位で分割する。
- リージョン、環境名、命名prefix、共通tagは一か所の設定を正本にする。
- `/admin/seed`のような管理処理は通常のAgent API公開経路から分離する。

## 実装済みのIaC基盤

IaCは、同じaccountで利用実績のあるAWS CDK v2とTypeScriptを採用した。現在は次の5 stackを
CloudFormationへ合成できる。

| stack | 現在のresource |
|---|---|
| `network` | VPC、public / application / data subnet、NAT Gateway、S3 Gateway Endpoint |
| `data` | S3 knowledge bucket、private OpenSearch Serverless `VECTORSEARCH` collection、Neptune Analytics LPG、両private endpoint、data policy、bootstrap write role |
| `compute` | ECS cluster、各componentのECR repository、CloudWatch Logs group |
| `management` | 固定snapshot専用one-off ECS task definition、task role、Security Group、AOSS data policy |
| `runtime` | VPC接続のBedrock AgentCore Runtime、実行role、OpenSearch read-only data access policy、保持対象Security Group |

Neptune Analytics graph・private endpoint、OpenSearchのindex投入用管理role、Runtime用のread-only接続権限を
実装済みである。固定snapshot投入はVPC内のone-off ECS taskで行い、通常の利用者向けRuntimeへwrite権限は
付与しない。
AgentCore Runtimeは、事前に環境設定と一致するtagのARM64 imageをECRへpushしてからdeployする。
現在の`poc`設定でdeployするとNAT Gatewayを
1台作成するため、deploy前に必要性と継続costを確認する。application subnet上のFargate taskが
ECR、CloudWatch Logs、Bedrockへ到達できるよう、各VPC endpointを実装するまでは
`network.natGateways`を`1`以上に限定する。`0`へ変更すると設定validationで停止する。

```text
infra/aws/
├─ config/
│  └─ environments/
│     └─ poc.json
├─ cdk/
│  ├─ bin/
│  ├─ lib/
│  └─ test/
├─ agentcore/
├─ bootstrap/
├─ scripts/
├─ ISSUES.md
└─ README.md
```

### ローカル検証

AWS認証を使わず、環境設定のvalidation、型チェック、単体テスト、CloudFormation合成を実行できる。
offline synthはAWS側のCloudFormation validationを行わない。

```bash
cd infra/aws/cdk
npm ci
npm run format:check
npm test
npm run synth -- -c environment=poc
```

GenU / AgentCore wire contractはrepository rootから確認する。

```bash
PYTHONPATH=infra/aws/agentcore:infra/aws/bootstrap:infra/aws/scripts \
  python -m pytest -q infra/aws/agentcore/tests infra/aws/bootstrap/tests
```

### AWS接続を伴う確認とdeploy

AWSへ接続するコマンドでは、選択したprofileのaccount・regionと環境設定が一致しなければ処理を
停止する。`rag-poc-admin`のSSO loginとCDK bootstrapが済んだ後だけ実行する。

最初にRuntime以外の基盤をdeployする。

```bash
cd infra/aws/cdk
AWS_PROFILE=rag-poc-admin npm run synth:aws -- -c environment=poc
AWS_PROFILE=rag-poc-admin npm run diff -- -c environment=poc
AWS_PROFILE=rag-poc-admin npm run deploy -- -c environment=poc \
  local-rag-law-poc-network \
  local-rag-law-poc-data \
  local-rag-law-poc-compute
```

read-only exportとdry-run検証後、成果物をdata stack outputのS3 bucketへ配置する。次にbootstrap imageを
pushしてmanagement stackをdeployし、one-off taskを実行する。

```bash
cd ../../..
AWS_PROFILE=rag-poc-admin aws s3 sync \
  /tmp/local-rag-law-bootstrap \
  's3://<KnowledgeBucketName output>/knowledge-root/bootstrap/current-search-validation/artifact/'
AWS_PROFILE=rag-poc-admin infra/aws/scripts/build-bootstrap-image.sh

cd infra/aws/cdk
AWS_PROFILE=rag-poc-admin npm run deploy -- -c environment=poc \
  --exclusively local-rag-law-poc-management

cd ../../..
AWS_PROFILE=rag-poc-admin infra/aws/scripts/run-bootstrap-task.sh
```

bootstrapの件数検証に合格した後、AgentCore用imageをbuild・pushし、Runtimeをdeployする。ECRはimmutableなので、更新時は
`config/environments/poc.json`の`agentCore.imageTag`も変更する。

```bash
cd ../../..
AWS_PROFILE=rag-poc-admin infra/aws/scripts/build-agentcore-image.sh

cd infra/aws/cdk
AWS_PROFILE=rag-poc-admin npm run deploy -- -c environment=poc \
  --exclusively local-rag-law-poc-runtime
```

Runtime ARNは`AgentCoreRuntimeArn` outputへ出力される。この値をGenU側の
`agentCoreExternalRuntimes[].arn`へ設定する。このリポジトリからAWSへのdeployとimage pushは
`poc`環境で実施している。実resourceと投入状況は[ISSUES.md](ISSUES.md)の確認証跡を正本とする。

現行の`ConfigurationHash`は環境設定全体から生成され、全stackのresource tagへ伝播する。
OpenSearch Serverless collectionはtag変更でも置換扱いになるため、bootstrap image tagだけの変更でも
data stackを依存deployへ含めるとcollection置換を要求される。`AWS-013`でstack別fingerprintと既存resourceの
移行手順を実装するまでは、既存data stackを更新対象に含めずmanagement / runtimeを`--exclusively`でdeployする。
data stackの変更が必要な場合は、新collectionへの再投入・比較・endpoint切替・rollbackを先に計画する。

## AWS環境の変更へ対応する原則

初期AWS環境は検証中に変更される前提とする。次の値をIaC本体、コンテナimage、
アプリケーションコードへ直接埋め込まない。

- AWS account ID、region、environment名
- VPC、subnet、security group、DNS zone、certificateのID
- bucket、OpenSearch、Neptune、ECS cluster等の物理resource名とendpoint
- instance、task、storageのsize、台数、timeout
- Agent実行基盤やOpenSearch方式など、比較後に置き換える可能性がある選択

構成は次の3層へ分離する。

1. 複数環境で再利用するIaC定義
2. account、region、size、機能選択を持つ環境別設定
3. deploy後に確定するresource ID、endpoint等のoutput

アプリケーションは環境別設定を直接読まず、deploy時に渡される環境変数、Secrets、IaC outputから
接続情報を受け取る。AWSサービス固有の違いはadapter境界へ閉じ込め、検索・Agent契約を変更しない。
各stackには設定内容から生成した`ConfigurationHash` tagを付け、どの環境設定から作られたresourceかを
CloudFormationとAWS resourceの両方で追跡できるようにする。

環境別設定にはschemaまたはvalidationを用意し、未定義値、許可しない組合せ、別account向けの
誤deployをplan前に検出する。秘密値は環境別設定へ保存せず、Secrets Manager等への参照だけを持つ。

既存resourceの置換が必要な変更では、IaC差分だけで完了としない。次を変更単位として記録する。

- 置換対象と影響する依存resource
- OpenSearch、Graph、S3等のデータ再投入または移行方法
- 切替前後の疎通確認と比較評価
- rollback方法と、旧resourceを削除できる条件

変更中の課題と完了条件は[ISSUES.md](ISSUES.md)で管理し、採用済み構成を変更した場合は
確認証跡に対象環境、構成version、IaC planまたはdiff、検証結果を残す。

環境を追加するときは`config/environments/poc.json`をコピーし、別の`environmentName`で保存する。
同じ環境のaccountやregionを変更する場合もこのファイルだけを変更し、CDK本体は変更しない。
`network.availabilityZoneIds`には、対象regionでAgentCoreが対応するAZ IDを2個または3個指定する。
AZ IDはAWSアカウント間で一貫するため、アカウント固有の`ap-northeast-1a`等は指定しない。
`bucketName: null`はCloudFormationに物理名の生成を任せる指定であり、確定名はstack outputから
アプリケーションへ渡す。外部連携上、作成するbucketの物理名を固定する必要がある場合だけ、
環境設定へ明示名を指定する。既存bucketをimportする構成はまだ実装していない。
