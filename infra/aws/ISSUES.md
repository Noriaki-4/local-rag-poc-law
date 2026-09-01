# AWS移行 課題管理

> 更新日: 2026-09-01
>
> 本書は、AWS移行の未解決課題、優先順位、完了条件を管理する。
> AWS構成の仕様書ではない。全体構想は
> [Step2 AWS実現イメージ・移行計画](../../docs/step2_transition_plan.md)、
> 現行実装と操作手順は[RUNBOOK](../../RUNBOOK.md)を参照する。

## 1. 管理方法

課題のstatusは次の意味で使う。

| status | 意味 |
|---|---|
| `未着手` | 方針または完了条件はあるが、実装を開始していない |
| `対応中` | 設計、実装または検証を進めている |
| `検証待ち` | 実装済みだが、AWS上の要求する確認に合格していない |
| `要設計` | 選択肢とトレードオフを決める必要がある |
| `停止中` | 再開条件を満たすまで意図的に作業を止めている |
| `完了` | 完了条件を満たす証跡がある |

優先度は、最初のAWS環境で検索・回答を成立させるために必須なものを`P0`、
比較評価と安定運用に必要なものを`P1`、PoC後の対象拡大で扱うものを`P2`とする。

statusを`完了`へ変更するときは、対応するIaC差分、テスト、AWS上の確認結果または
CloudWatch等の証跡を「確認証跡」へ記載する。設計を変更した場合は本書だけで完結させず、
対応する構成文書、IaC、アプリケーション設定、テストも更新する。

初期AWS環境のaccount、region、network、採用サービス、resource sizeは変更される前提とする。
採用済み構成を変更するときは既存課題の「現在地」を上書きするだけでなく、変更理由、対象環境、
データ移行、rollback、旧resourceの削除条件を該当課題または新しい課題へ残す。

## 2. 課題一覧

| ID | 優先度 | status | 課題 | 現在地 | 完了条件 |
|---|---|---|---|---|---|
| `AWS-001` | P0 | 対応中 | 初期AWS構成とIaC方式を決定する | IaCはTypeScript CDK、初期accountは`035351467732`、regionは`ap-northeast-1`とした。UIと認証の上流をGenU、利用者向けLegal Agentの実行先をBedrock AgentCore Runtimeに固定した。データサービスはOpenSearch Serverless、Neptune Analytics LPG、Titan Text Embeddings V2を採用する。未使用のrerankerと生成LLMのGemma 4はAWSへ移行しない。固定snapshot投入はone-off ECS taskへ決定し、正規seed・PDF前処理・evalの最終実行先は保留している | PoCで採用する実行基盤、データサービス、IaC、リージョン、GenU連携境界と採用理由が一つの構成として記録されている |
| `AWS-014` | P0 | 検証待ち | GenUとAgentCore Runtimeの連携契約を決める | GenUから外部Bedrock AgentCore Runtimeとして呼び出す方針と、Strands request / event streamのadapterを実装した。質問抽出、開始通知、回答・引用投影、安全なエラーの9テストに合格した。GenU実環境からのinvoke、session、表示は未確認 | GenUの認証済みユーザーが法令質問をAgentCore Runtimeへ送信し、回答、引用、進捗または完了、利用者向けエラーを受信できる契約テストがあり、管理処理はRuntimeの利用者向けinvokeから実行できない |
| `AWS-015` | P0 | 検証待ち | GenUのmodel選択とLegal AgentのLLM providerを対応付ける | Bedrock ConverseをJapan geo inference profileのClaude Haiku 4.5へRuntime固定し、実invokeを完走した。大きいsolver schemaはnative compiled grammar上限を超えるため、同じschemaの非strict tool-useとapplication validationへfallbackする。GenU requestのmodel値は監査ログだけに使う。GenU実環境からのmodel選択表示は未確認 | main側のLLM契約を壊さずBedrockを選択でき、許可model・regionをbackendで制約し、GenUの選択を採用するかRuntime固定にするかが契約とテストで明示されている |
| `AWS-013` | P0 | 対応中 | AWS環境の差分と構成変更を安全に扱う | schema version 8の環境JSONとaccount / region / CIDR / AgentCore対応AZ ID検証を実AWSで使用した。stack別`ConfigurationHash`を実装し、bootstrap / Runtime image tag変更をdata hashから分離した。既存`poc`のdata tagは実resourceと同じhashへ移行用pinを設定済み。AWSへの反映、置換なしchange set確認、data resourceのblue/green手順は未実施であり、それまではmanagement / runtimeを`--exclusively`で更新する | 再利用IaC、環境別設定、deploy outputが分離され、別設定のplan検証、resource置換時のデータ移行・疎通確認・rollbackを追跡できる |
| `AWS-002` | P0 | 完了 | AWS移行計画を現行Graph schemaへ合わせる | `step2_transition_plan.md`をschema version 9の`GraphNode`派生label、`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`、`RelationAssertion`と分類Runへ修正した | `HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`と`RelationAssertion`の現行責務を前提に、AWSでの保存・検索方式が定義されている |
| `AWS-003` | P0 | 検証待ち | AWS基盤の最小IaCを作る | 東京の実accountへ5 stackをdeployし、VPC、S3、ECS / ECR、private OpenSearch Serverless、Neptune Analytics、one-off管理task、AgentCore Runtimeを作成した。Runtimeは`READY`、各stackは`CREATE_COMPLETE`または`UPDATE_COMPLETE`である。削除・再作成とdata stackの安全なtag移行は未確認 | network、data、compute、management、runtimeに必要な最小リソースを再現可能に作成・更新・削除でき、命名と共通tagが統一されている |
| `AWS-004` | P0 | 検証待ち | Legal AgentをAWSのOpenSearchへ安全に接続する | 実collectionでSigV4、Faiss 1024次元mapping、kuromoji / ICU `_analyze`、Titan embedding、Serverless自動文書IDのbulk投入を確認した。S3 checkpointから全16,459件を投入中である。Runtimeから検索を含むLegal Agent invokeは完走したが、全件投入後のBM25 / vector / multi-searchと`bge-m3`比較は未確認 | AgentCore Runtimeの実行roleからTLSとIAM SigV4（`aoss`）を使用してhealth、BM25、vector、multi-searchが確認でき、管理taskからkuromoji + ICU mappingのindex作成・日本語token解析・投入ができ、全vectorがTitan V2で再生成され、同じ日本語datasetで`bge-m3`との差が評価されている |
| `AWS-005` | P0 | 検証待ち | Neo4j依存をNeptune Analytics接続へ分離する | Neptune Analytics LPG、graph・private endpoint、parameterized openCypher Runtime adapterと、124 node / 172 edgeをIDで冪等投入する管理処理を実装した。Neo4j固有の`all` / `any`をNeptune互換list comprehensionへadapterで変換し、探索timeoutをserverへ渡す。実Graphでのquery互換性は未確認 | 現行Graph契約を変えずにNeptune Analytics用adapterからLPG投入、1ホップ探索、RelationAssertion探索、再投入ができる |
| `AWS-006` | P0 | 対応中 | Legal AgentのMinIO依存をS3対応へ置き換える | 固定bootstrapではprocessed本文をS3へ保存し、ガイドライン原本PDFをS3へ配置して`minio://` URIを書き換える。正規seedのObject Storage adapterは保留中 | ローカルMinIOとAWS S3を同じObject Storage境界から利用でき、引用に必要なsource URIとmetadataが維持される |
| `AWS-007` | P0 | 対応中 | 実行単位と長時間処理の境界を決める | UIはGenU、Legal AgentはAgentCore Runtime、固定snapshot投入はVPC内one-off ECS taskへ分離した。rerankerは移行対象外。PDF前処理・evalの最終実行形態とGenU timeout実測は未確認 | AgentCore Runtime、前処理、seed、evalの実行先、timeout、CPU・memory、streaming、再試行責務が定義される |
| `AWS-008` | P0 | 検証待ち | IAMと秘密情報管理を実装する | Runtime read-only role、one-off bootstrap task write role、手動用assume roleを分離し、S3・AOSS・Titan・Neptune権限をCDKへ実装した。bootstrapのS3削除権限を除外し、RuntimeのBedrock invoke対象をTitan V2とJP Claude Haiku 4.5 profile・東京/大阪の基盤modelへ限定した。固定secretは使わない。実AWSでの権限過不足は未確認 | 各コンポーネントが最小権限のroleを使い、秘密値をリポジトリ、image、平文ログへ保存せず起動できる |
| `AWS-009` | P0 | 停止中 | 破壊的な正規seed処理をAgentCore Runtimeから分離する | 正規seed再実行は保留した。VPC内one-off ECS taskは固定snapshot専用で、正規seed・分類を起動せず、AgentCore Runtimeとは別roleを使う | seedを認証された管理taskからだけ再実行でき、AgentCore Runtimeの利用者向けinvokeから実行できない。`AWS-016`の疎通完了後、データ更新が必要になった時点で再開する |
| `AWS-010` | P1 | 要設計 | Case状態と評価結果の永続化先を決める | CaseStoreは単一プロセスのin-memory、評価結果はローカルfilesystemを使う | 再起動と複数taskを考慮した保存・排他・保持期間が定義され、必要な再開と評価結果取得をAWS上で確認できる |
| `AWS-011` | P1 | 未着手 | ローカル版とAWS版の比較評価を自動化する | 共通の評価形式はあるが、AWS環境への実行と成果物回収手順がない | 同じdataset、設定、snapshotを使い、回答精度、引用、検索hit、latency、costを比較できる |
| `AWS-012` | P1 | 未着手 | 監視、監査ログ、コスト確認を整備する | CloudWatchへの構造化ログ、alarm、利用量tag、PoC予算の確認方法がない | requestとrunを追跡でき、主要な失敗、latency、利用量と概算costを確認できる |
| `AWS-016` | P0 | 対応中 | 公開済みローカルsnapshotをAWS初期データとして再利用する | hash検証済みschema v3成果物27ファイルとmanifestをS3へ配置した。実bootstrap taskでFaiss + kuromoji indexを作成し、Titan再Embedding・bulk投入をS3 checkpointから継続中である。完了後にNeptuneへ124 node / 172 edgeを投入する。正規seedとRelation分類は実行していない | manifest hashを検証してS3から固定成果物を読み、Titan vectorを生成してkuromoji indexへ16,459件、Neptuneへ124 node / 172 edgeを投入し、各snapshot・Run・件数一致、全件検索とmini Graph範囲の疎通を確認できる |
| `AWS-017` | P1 | 停止中 | 非同期Relation分類をAWSデータ更新経路として再実行可能にする | 初期AWS検証では公開済みClassificationRunを再利用し、14,454候補の全件成果物を含む新規分類は行わない。ローカルのpacket、Worker / Reviewer、checkpoint、import / publish経路は維持する | AWSのGraph snapshotから候補をexportし、既存の再開可能処理で分類、監査、import、publishできる。`AWS-016`完了後、新snapshotの意味分類が必要になった時点で再開する |

## 3. 推奨する実行順

1. `AWS-014`で対象GenUと連携契約を決める。
2. `AWS-015`でGenUとLegal Agentのmodel設定を対応付ける。
3. `AWS-001`で連携契約を満たす初期構成とIaC方式を決める。
4. `AWS-013`で再利用IaC、環境別設定、outputの境界と変更手順を決める。
5. `AWS-002`で移行対象のGraph・データ契約を現行仕様へ合わせる。
6. `AWS-003`で最小基盤を作り、`AWS-008`のIAM境界を同時に固定する。
7. `AWS-006`、`AWS-004`、`AWS-005`の順にデータ接続を置き換える。
8. `AWS-016`で固定snapshotを投入し、初期検索を成立させる。
9. `AWS-007`で実行単位を確定し、必要になった時点で`AWS-009`、`AWS-017`を再開する。
10. `AWS-010`以降で永続化、比較評価、監視を追加する。

法令検索の意味品質に関する変更はこの順序へ含めない。兄弟worktree側で変更された検索契約を
取り込む場合は、AWS adapterが同じ契約を維持できることを回帰テストで確認する。

## 4. 確認証跡

| 日付 | 課題 | 証跡 |
|---|---|---|
| 2026-09-01 | `AWS-003`, `AWS-013` | TypeScript build、Jest 13件、`environment=poc`のoffline CDK synthに合格。AWSへの接続・deployは未実施 |
| 2026-09-01 | `AWS-014` | GenU / AgentCore contractのPythonテスト9件に合格。実GenUとAWS Runtimeでのinvokeは未実施 |
| 2026-09-01 | `AWS-013`, `AWS-014` | `/ping`をAgentCore HTTP契約の`Healthy`へ修正。東京の非対応AZ IDを拒否し、全6 subnetを`apne1-az1` / `apne1-az2`へ固定。Jest 15件、Python 9件、offline synthと生成template確認に合格 |
| 2026-09-01 | `AWS-001`, `AWS-007`, `AWS-015` | 現行有効経路の静的確認によりrerankerが未接続であることを確認。rerankerと生成LLMのGemma 4をAWS移行対象から除外し、`poc`のreranker用ECR定義を削除。Runtimeへ`RERANK_PROVIDER=none`、`LLM_PROVIDER=bedrock`を固定。Embedding providerは`AWS-004`へ分離 |
| 2026-09-01 | `AWS-001`, `AWS-004`, `AWS-005` | 当初のProvisioned方針を変更し、初期OpenSearchをprivate Serverless `VECTORSEARCH` collectionに決定。GraphはNeptune Analytics LPG + openCypher、Embeddingは東京対応のTitan Text Embeddings V2（1024次元）とし、日本語BM25にkuromoji + ICUを必須化した。schema version 4の環境設定、collection、VPC endpoint、暗号化・network・Runtime read-only data access policyをCDKへ実装 |
| 2026-09-01 | `AWS-003`, `AWS-004`, `AWS-013` | OpenSearch Serverless追加後のTypeScript build、format check、Jest 19件、AgentCore contract 9件、`environment=poc`のoffline CDK synth、生成templateのprivate network・`aoss:APIAccessAll`・read-only data access policy・Runtime環境変数確認に合格。AWS接続・deploy・実collectionのkuromoji解析確認は未実施 |
| 2026-09-01 | `AWS-009`, `AWS-013`, `AWS-016`, `AWS-017` | 初期bootstrapを再確認し、Search全件snapshotとmini Graph snapshotが異なる構成へ訂正。OpenSearch 20文書（e-Gov 14法令＋6ガイドライン）/ 16,459 Content Unit、Graph 124 node / 172 edge、Neo4j上のpublished Run 17候補 / 24 Assertionをread-onlyで特定した。schema version 6とsnapshot分離exportへ修正し、原本manifest・embedding除外・件数・hashを実データで検証。Jest 21件に合格。AWS接続・deploy・seedは未実施 |
| 2026-09-01 | `AWS-002`〜`AWS-009`, `AWS-015`, `AWS-016` | P0実装としてNeptune Analytics CDK、one-off ECS bootstrap taskとwrite role、AWS Runtime adapter、schema v3 export、S3・Titan・AOSS・Neptune投入コマンドを追加。read-only再exportで16,459件、124 node、172 edge、原本込み27ファイルのhashを検証し、投入コマンドのdry-runに合格。TypeScript build、Jest 23件、AgentCore Python 11件、5 stackのoffline synthに合格。AWS接続・deploy・実投入は未実施 |
| 2026-09-01 | `AWS-003`〜`AWS-005`, `AWS-008`, `AWS-013`, `AWS-016` | P0完了前の再監査で、bootstrap roleのS3削除権限を除去、RuntimeのBedrock権限を利用modelへ限定、NAT 0の不成立構成をvalidationで拒否、task設定をimage外の環境変数へ分離、16,459件投入に短すぎるCLI waiterを最大12時間の明示pollingへ変更した。OpenSearch Serverless非対応のrefreshを収束待ちへ変更し、multi-searchを対応するGETへ変換した。Neptune非対応のNeo4j `all` / `any`をlist comprehensionへ変換し、探索timeoutを`queryTimeoutMilliseconds`へ渡した。TypeScript build・format check、Jest 24件、Python 14件、5 stack offline synth、固定成果物dry-run（16,459 document / 124 node / 172 edge）、生成templateのIAM・AZ・Runtime環境変数確認、AgentCore / bootstrapのLinux ARM64 image buildと`/ping` smoke testに合格。AWS接続・deploy・実service確認は未実施 |
| 2026-09-01 | `AWS-003`, `AWS-004`, `AWS-008`, `AWS-013`, `AWS-016` | `rag-poc-admin`でaccount `035351467732` / `ap-northeast-1`へnetwork、data、compute、managementをdeploy。private VECTORSEARCH collection `780xd3xfa5346ecv5z8l`、Neptune Analytics graph `g-d5pasthxl2`、knowledge bucket、VPC / endpoint、ECS / ECRを作成した。成果物28 S3 object / 142,600,697 bytesを配置。実bootstrapでAOSS署名payload hash、Lucene→Faiss、index可視化待ち、自動文書ID、Titan throttling差異を検出・修正し、manifest hash付きcheckpointから投入を継続中。data stackの全体ConfigurationHash tag更新はcollection置換になるためrollbackし、resourceがACTIVE / AVAILABLEのまま保持されたことを確認した |
| 2026-09-01 | `AWS-014`, `AWS-015` | AgentCore Runtime `arn:aws:bedrock-agentcore:ap-northeast-1:035351467732:runtime/LocalRagLawPoc-9vW35wDaXG`をdeployし`READY`を確認。実invokeはHTTP 200のStrands event streamで完走した。Haiku 4.5のnative JSON Schema compiled grammar上限を実測し、非strict tool-use fallbackとapplication schema validationへ修正。GenU実環境からの設定・invokeは未確認 |
| 2026-09-01 | `AWS-013` | stack別`ConfigurationHash`と既存data tagの移行用pinを実装。bootstrap / Runtime image tag変更でdata fingerprintが変わらず、data設定変更ではdesired fingerprintが変わる契約を含むJest 28件、TypeScript buildに合格。AWSへのdeployとchange set確認はbootstrap完了後まで保留 |
| 2026-09-01 | `AWS-014` | Streamlitの質問整理をGenUから利用するため、AgentCore wire contractへ`question_readiness` operationを追加。既存の質問確認Domain Serviceへ振り分け、構造化結果をStrands text eventで返すcontractテスト20件に合格。Runtime imageのbuild・push・再deployとGenU実通信は未実施 |
