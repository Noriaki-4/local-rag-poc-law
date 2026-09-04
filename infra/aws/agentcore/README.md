# Legal AgentCore Runtime

GenUからBedrock AgentCore Runtimeとして既存Legal Agentを呼び出すための薄い境界である。
検索ロジックの正本は`agent-api/app`に残し、このディレクトリには次だけを置く。

- AgentCore必須endpointの`GET /ping`（`{"status":"Healthy"}`）と`POST /invocations`
- GenUのStrands requestを`operation`により現行`AnswerRequest`または
  `QuestionReadinessRequest`へ変換（省略時は後方互換のため回答生成）
- 同期回答をGenUが読めるStrands event streamへ変換する処理
- OpenSearch SigV4、Titan、Neptune Analytics、Bedrock ClaudeのAWS接続adapter
- AgentCore用ARM64 container定義

`runtime_app.py`はLegal Agentのimportをinvoke時まで遅延する。これにより`/ping`はOpenSearch、Graph、
LLM clientを初期化せずに応答する。検索中は先に開始eventを返し、既存Agentの同期処理を別threadで
実行した後、回答本文をtext eventへ、引用の見出し・本文・Content Unit ID等を`legalRagCitations` eventへ
投影する。`operation=question_readiness`では既存の質問確認Domain Serviceを呼び、判定理由と推奨質問文を
JSONのtext eventとして返す。検索・回答処理は開始しない。

GenUから渡されるmodelは診断ログにだけ残し、Legal Agentのmodel設定には使用しない。Runtimeは環境設定の
`low` / `middle` / `high`ごとに指定したJapan geo inference profileだけを使用する。処理とlevelの対応は
`agent-api/app/domains/legal/model_levels.json`を正本とし、現行PoCは`low`をClaude Haiku 4.5、
`middle`と`high`をClaude Sonnet 4.6へ割り当てる。`aws_adapters.py`は`app.main`より先にprovider境界を差し込み、
`agent-api/app`の検索・Agentロジックを複製しない。Runtime roleはOpenSearch / NeptuneのreadとBedrock invoke
だけを持ち、bootstrap write処理は公開endpointから呼び出せない。

全model呼出しはAnthropic APIではなく、東京リージョンのBedrock Runtime `Converse`を経由する。
`BEDROCK_MODEL_ID`は旧回答経路のfallbackであり、Agent Frameworkの処理別modelを上書きしない。

Bedrock Converseでは可能な限りnative JSON Schema outputを使う。Agent Frameworkの大きいsolver schemaが
選択modelのcompiled grammar上限を超えた場合だけ、同じschemaを持つ非strict tool-useへfallbackし、
返されたtool inputを既存のapplication schema validatorで検証する。他のValidationExceptionはfallbackせず失敗させる。

## テスト

```bash
PYTHONPATH=infra/aws/agentcore python -m pytest -q infra/aws/agentcore/tests
```

## Image

Docker build contextはrepository rootとする。CDKは環境設定のECR repositoryとimmutable tagを参照し、
offline synth時にDocker buildやAWS接続を行わない。

```bash
AWS_PROFILE=rag-poc-admin \
  infra/aws/scripts/build-agentcore-image.sh
```

同じtagはECRのimmutable設定により上書きできない。コードを更新するときは環境設定の`imageTag`を
新しい値へ変更してimageをpushし、CDK diff後にRuntime stackをdeployする。
