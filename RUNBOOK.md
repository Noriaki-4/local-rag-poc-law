# RUNBOOK

## 1. 概要

このリポジトリは、Step1 ローカル Agentic RAG / DeepSearch POC を Docker 上で起動する。

構成:

- MinIO: 原本・処理済み成果物・評価データ置き場
- OpenSearch: 法令本文の BM25 / kNN / Hybrid 検索
- OpenSearch Dashboards: インデックス確認
- Neo4j: GraphRAG 検証
- Agent API: FastAPI
- Agent UI: Streamlit
- Eval Runner: サンプル評価実行

実装はサンプルデータで起動確認できる。lawqa_jp 本体は同梱せず、全問評価時は公開データをURLまたはローカルファイルから読む。

初期 LLM は有料 API ではなく、ホストで動く Ollama の `gemma4:e4b` を使う。Docker 内の Agent API からは `http://host.docker.internal:11434` に接続する。
Claude は `LLM_PROVIDER=anthropic`、OpenAI APIの`gpt-4o-mini`は
`LLM_PROVIDER=openai`へ切り替える。`LLM_MODEL`を指定すると、探索・統合・回答・Reviewerを
1つのモデルへ一括で切り替えられる。未指定時だけ役割別のmodel設定を使う。

embedding は既定で Ollama の `bge-m3` を使う。Agent の検索は `AGENT_USE_BM25=true`, `AGENT_USE_VECTOR=true` の Hybrid 検索を既定にする。

検索候補の最終並べ替えには、ローカルの日本語 cross-encoder
`hotchpotch/japanese-reranker-base-v2` を使う。`reranker-api` は独立コンテナで動き、
モデルは `reranker_models` volume にキャッシュする。外部Rerank APIの契約やAPIキーは不要。

## 2. 初回起動

ホスト側で Ollama、回答生成用 `gemma4:e4b`、embedding 用 `bge-m3` が使えることを確認する。

```bash
ollama list
ollama pull gemma4:e4b
ollama pull bge-m3
curl -s http://localhost:11434/api/generate \
  -H 'content-type: application/json' \
  -d '{"model":"gemma4:e4b","prompt":"日本語で一文だけ返してください。","stream":false}' | jq .

curl -s http://localhost:11434/api/embed \
  -H 'content-type: application/json' \
  -d '{"model":"bge-m3","input":"金融商品取引法第2条第1項における有価証券の定義。"}' \
  | jq '.embeddings[0] | length'
```

LLM 設定は `docker-compose.yml` の `agent-api.environment` に既定値を入れている。必要に応じて `.env.example` を参考に `.env` を作る。

Claude を使う場合の `.env` 例:

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_VERSION=2023-06-01
ANTHROPIC_MAX_TOKENS=4096
ANSWER_MODEL=claude-haiku-4-5-20251001
REVIEWER_MODEL=claude-haiku-4-5-20251001
REVIEWER_MAX_TOKENS=8192
LLM_RESEARCH_STAGE_MODEL=claude-haiku-4-5-20251001
LLM_RESEARCH_INTEGRATION_MODEL=claude-sonnet-5
PLANNER_MODEL=claude-haiku-4-5-20251001
PLANNER_MAX_TOKENS=1024
PLANNER_TIMEOUT_SEC=30
AGENT_USE_LLM_PLANNER=true
AGENT_MAX_QUERIES=5
AGENT_MAX_RETRY_ROUNDS=1
AGENT_MAX_TOTAL_TOOL_CALLS=8
AGENT_MAX_GRAPH_HOP=1
AGENT_MAX_GRAPH_PATHS=10
AGENT_MAX_WALL_TIME_SEC=110
AGENT_CANDIDATE_TOP_K=20
AGENT_RERANK_TOP_K=10
AGENT_RRF_K=60
AGENT_MAX_LLM_CALLS=3
RERANK_PROVIDER=local_http
RERANK_BASE_URL=http://reranker-api:8100
RERANK_MODEL=hotchpotch/japanese-reranker-base-v2
RERANK_CANDIDATE_TOP_K=30
RERANK_TIMEOUT_SEC=30
RERANK_MAX_CHARS=3000
EVALUATOR_MODEL=claude-haiku-4-5-20251001
EVALUATOR_MAX_TOKENS=1024
EVALUATOR_TIMEOUT_SEC=20
AGENT_USE_BM25=true
AGENT_USE_VECTOR=true
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_MAX_CHARS=1000
```

利用可能なモデルIDはAPIキーの契約プランに依存するため、`ANSWER_MODEL`、
`REVIEWER_MODEL`、調査の役割別modelを決める前に直接確認する。

```bash
curl -s https://api.anthropic.com/v1/messages \
  -H 'content-type: application/json' \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":10,"messages":[{"role":"user","content":"test"}]}' \
  | jq .
```

`404 model not found` の場合は、そのキーではモデルIDが使えない。契約プランで利用可能な別のモデルIDに変える。

OpenAI APIの`gpt-4o-mini`へ全役割を一括で切り替える`.env`例:

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MAX_TOKENS_CEILING=16384
```

API keyを画面へ表示せず、上記設定を既存`.env`へ反映する場合は次を実行する。
同じコマンドを再実行すると既存値を置き換え、重複行は残さない。`.env`のpermissionは`600`になる。

```bash
./scripts/configure_openai.sh
```

`LLM_MODEL`は`ANSWER_MODEL`、`REVIEWER_MODEL`、探索・統合modelなどの役割別設定より優先する。
役割ごとに異なるモデルを使う場合は`LLM_MODEL`を空にして、従来の役割別設定を指定する。
`gpt-4o-mini`はChat CompletionsのStructured Outputsへ接続し、既存の共通JSON契約を使う。
モデルのAPI仕様は[OpenAI公式のgpt-4o-mini説明](https://developers.openai.com/api/docs/models/gpt-4o-mini)を参照する。
ChatGPTの月額サブスクリプションはOpenAI API利用料に充当されないため、API Platform側の
APIキーと支払い設定が別途必要である。

キーとmodelの疎通はAgent APIのhealthで確認する。キーをshellへ直接表示しない。

```bash
docker compose up --build -d agent-api
curl -s http://localhost:8000/health | jq '.llm'
```

`.env` を書き換えた後は、`docker compose up -d agent-api` だけでなく `docker compose up --build -d agent-api` を使うこと。`agent-api` の Python コードはイメージに `COPY` されているため、コード変更後に `--build` を省略すると古いイメージのまま起動し、変更が反映されない。

Ollama に戻す場合:

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
ANSWER_MODEL=gemma4:e4b
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_MAX_CHARS=1000
```

```bash
docker compose up --build -d
```

起動確認:

```bash
docker compose ps
curl -s http://localhost:8000/health | jq .
```

`/health` の `llm.ok` と `reranker.ok` が `true` であれば、Agent APIコンテナから
設定したLLMとローカルリランカーを利用できる。初回はHugging Faceからモデルを取得するため、
`reranker-api` がhealthyになるまで数分かかる場合がある。

リランカーを無効化して比較する場合:

```bash
RERANK_PROVIDER=none docker compose up --build -d agent-api
```

専用リランカー適用時は `/answer` の `route` に `evidence_reranker` が入り、
`trace.reranker` にモデル、処理時間、スコア、フォールバック理由が記録される。

UI:

```text
http://localhost:8501
```

OpenSearch:

```text
http://localhost:9200
```

OpenSearch Dashboards:

```text
http://localhost:5601
```

Neo4j Browser:

```text
http://localhost:7474
user: neo4j
password: password
```

MinIO Console:

```text
http://localhost:9001
user: minioadmin
password: minioadmin
```

## 3. サンプルデータ投入

Agent API から OpenSearch / Neo4j / MinIO にサンプルを投入する。

seed時は、OpenSearchへ今回投入する`processedObjectUri`を正として
`derived-artifacts/vector-documents/`を同期する。過去のseedで残った未参照vector文書は削除するが、
`source-documents/`、`eval-data/`、`derived-artifacts/preprocessed/`は削除しない。

```bash
curl -s -X POST http://localhost:8000/admin/seed | jq .
```

投入内容:

- OpenSearch index: `legal-rag-content-ja-v2`（Kuromoji＋NFKC＋bigram）
- OpenSearchの各Content Unit: `sourceSnapshotId`、`contentHash`、Article・Document集約hash
- Neo4j nodes: `Document / Article / Paragraph / Item`
- Neo4j edges: 構造`HAS_CONTENT_UNIT`、原文上の明示参照`REFERENCES`、
  ガイドが明示した対応`EXPLAINS`
- `MENTIONS / APPLIED_BY`、意味関係と同名の物理Relation、同期`RelationAssertion`は生成しない
- e-Gov法令は本則・附則を分けて投入する。本則は `law-<法令番号>-article-<条番号>`、附則は `law-<法令番号>-suppl-<index>-article-<条番号>`（条番号の衝突で本則が消えるのを防ぐ）。各文書に `provisionType` / `sectionKey` を付与。詳細は [id_naming_rules.md](docs/id_naming_rules.md) 3.1
- MinIO bucket: `knowledge-root`
- サンプル評価データ: `knowledge-root/eval-data/samples/...`
- 原本保管用マニュアル: `knowledge-root/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md`

原本保管用マニュアルは、OpenSearch / Neo4j / 評価データには投入しない。

`/admin/seed` は、書込み前に同じ入力manifestからGraphを組み立てて監査し、監査成功後に
OpenSearch index と Neo4j graph を作り直す。レスポンスの`sourceSnapshotId`は両方で共通である。
検証環境の再投入用であり、本番運用向けの差分投入や無停止切替ではないため、実行中は回答処理を止める。

### 公開買付け3階層ミニデータセット

第二期Step 1では、全件を再投入せず、保存済みe-Gov XMLから選んだ3法令13 Articleだけで
OpenSearchとNeo4jの接続を検証する。まず、ネットワークやDBを使わず固定snapshotを監査する。

```bash
python3 scripts/validate_public_tender_offer_mini_dataset.py
```

専用indexでAgent APIを起動し、seedする。

```bash
OPENSEARCH_INDEX=legal-rag-public-tender-mini-v1 \
SEED_SCENARIO_MANIFEST=/workspace/datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json \
docker compose up -d --build agent-api

curl -s -X POST http://localhost:8000/admin/seed | jq .
```

このseedは専用OpenSearch indexを再作成し、Neo4j全体をミニデータセットへ置き換える。
既存の全件OpenSearch indexは削除しない。全件Graphと切り替えて併用する仕組みではないため、
元に戻す場合は対象の環境変数を戻してAgent APIを再起動し、全件seedを改めて実行する。
ミニデータセットseedでは、MinIOの別snapshot由来vector文書を削除しない。

2026-08-21の確認値は、OpenSearch 69文書、Neo4j 82 node
（Document 3、Article 13、Paragraph 46、Item 20）、構造Relation 100件である。
意味分類はseedと分離し、監査済みRunだけをpublishする。同日の検証Run
`classification-run-public-tender-mini-v1-v23`は17候補すべて承認、24 RelationAssertionである。
回答経路でこのRunを固定する場合は、Agent API起動時に次も設定する。

```bash
LEGAL_RELATION_CLASSIFICATION_RUN_ID=classification-run-public-tender-mini-v1-v23
```

データセットの範囲、gold分離、期待経路は
[`datasets/scenarios/public_tender_offer_three_layer_v1/README.md`](datasets/scenarios/public_tender_offer_three_layer_v1/README.md)
を参照する。

### 非同期Relation分類

全件実行前後の順序と停止条件は
[非同期Relation分類 全件実行チェックリスト](docs/relation_classification_rollout_checklist.md)に従う。

`/admin/seed`は決定的に抽出できる構造と原文Relationまでを作り、LLMを呼ばない。
意味分類はseed後の独立した再開可能jobが、原文`REFERENCES`を候補化し、5種類の
`proposedPredicate`を持つ未確認`RelationAssertion`として別の`ClassificationRun`へ登録する。
完了・監査済みRunだけを`published`にし、検索はCase開始時にpublish済みRunを固定する。

現時点では、新しい型、冪等キー、Neo4j Constraint、候補単位checkpoint、再開可能CLI、
Run publish監査まで実装済みである。保存済みe-Gov XMLからOpenSearch / Neo4jを再構築し、
新しい参照構造の監査は`94/94`である。新5 predicateの全件Runとpublishはまだ実行していない。
旧`scripts/classify_graph_relations.py --apply`はschema version 7の
既存`RelationAssertion`を更新する移行用処理なので、schema version 9の再seed後には実行しない。

非同期分類後もRelationAssertionは正式なArticle間Relationへ昇格させない。検索時Solverが質問に
関係する候補だけ両端Article本文で評価し、その案件判断はCaseStoreだけへ保存する。プログラムは
既知ID、predicate enum、端点、根拠span、snapshot・hash・件数だけを検証し、法的意味を補正しない。

オフライン分類は検索・回答用のLLM providerと分離する。現行CLIのローカル動作確認用既定値は
Ollamaの`gemma4:e4b`だが、この経路を全件意味分類の品質承認済み経路とは扱わない。
全件登録用の意味判定は、Codexサブスクリプション内の`gpt-5.6-luna`を
Worker / Reviewerの両方に使うオペレーター実行とする。
[OpenAI公式モデル説明](https://developers.openai.com/api/docs/models/gpt-5.6-luna)では、
Lunaは高速・大量処理向けのモデルとされている。実行時はリポジトリ固有の
`.agents/skills/legal-relation-adjudicator` skillの入出力契約に従い、分類候補JSONLを読み、
監査可能な判定JSONLを返す。
API経由では実行せず、Agent APIへLunaのmodel IDや認証を組み込まない。

Lunaのreasoning effortはWorker、Reviewerとも`high`に固定する。これはAPIの出力token上限ではなく、
Codex sessionを開始するときに指定する推論深度である。1候補について5 predicateの二条件、意味方向、
根拠span、構造適合性をまとめて確認するため、全件Runの途中で`medium`等と混在させない。
Coordinatorは各sessionの開始時にmodelとreasoning effortを明示し、成果物manifestへ両方を保存する。
速度比較のため別のreasoning effortを試す場合は別Run・別manifestとし、`high`の成果物へ混ぜない。
reasoning effortを変えただけでも判定条件が変わったものとして代表100件の品質ゲートを再実行する。

live indexからLuna用のlabel-free packetをexportし、最大5件のshardへ分割する。
`candidateKey`にはsnapshot、schema、prompt、Worker / Reviewer model、両Article hash、
同じ有向Articleペアを結ぶ全`basisEdgeIds`と参照出現hashが含まれるため、一時的なモデル名や
物理Relationの一部だけでexportしたpacketを本番Runへ流用しない。

```bash
python3 scripts/export_relation_adjudication_packet.py \
  --output eval-results/relation-full/packet.jsonl

python3 scripts/shard_relation_adjudication_packet.py \
  --packet eval-results/relation-full/packet.jsonl \
  --output-dir eval-results/relation-full/shards \
  --max-candidates-per-shard 5 \
  --max-active-sessions 3 \
  --reasoning-effort high
```

全件のCodex subscription sessionは、再開可能queueから起動する。既定では計画だけを表示し、
`--execute`を付けた場合だけLuna sessionを開始する。`--apply`は承認済みshardをbuilding Runへ
checkpoint保存するが、publishは行わない。最初は`--max-shards`で小さく通し、安定後だけ
`--all-shards`を明示する。

```bash
# 次の3 shardを確認するだけ
python3 scripts/run_relation_adjudication_codex_queue.py \
  --run-root eval-results/relation-full-v22-pair-v7 \
  --run-id classification-run-... \
  --max-shards 3

# 次の3 shardを最大3 sessionで処理し、checkpoint保存する
python3 scripts/run_relation_adjudication_codex_queue.py \
  --run-root eval-results/relation-full-v22-pair-v7 \
  --run-id classification-run-... \
  --max-shards 3 \
  --max-active-sessions 3 \
  --execute --apply

# 残り全shardを同じRunで再開する
python3 scripts/run_relation_adjudication_codex_queue.py \
  --run-root eval-results/relation-full-v22-pair-v7 \
  --run-id classification-run-... \
  --all-shards \
  --max-active-sessions 3 \
  --max-consecutive-failures 3 \
  --execute --apply
```

queueはskill、分類契約、対象shardを1回のpromptへ展開し、Lunaには外部toolを使わせず、
strict structured outputだけを返させる。これは意味判断をProgramへ移す変更ではない。
Programは回答をJSONLへ保存し、既知ID、件数、enum、条件代数を既存binder / Pydantic契約で検証する。
WorkerとReviewerは別の新規sessionであり、thread IDを`orchestration/state.json`へ保存する。
意味差戻し時は同じWorkerを1回だけresumeし、同じReviewerが最終差分確認する。

未知ID、候補件数、occurrence hash、5 predicate網羅等でWorker JSONが機械契約を通らない場合は
同じWorkerへ、Review JSONが通らない場合は同じReviewerへ検証エラーを返し、完全なrecordを
それぞれ1回だけ再出力させる。これは意味差戻し回数を増やす処理ではなく、ProgramがIDや意味を
推測・置換しないための契約修復である。連続3 shardが失敗した場合はqueueを止め、
残りを未着手のまま保持する。成果物があるshardとimport済みcheckpointは再利用される。

どちらも既存成果物を上書きしない。中断後はWorkerの承認済みJSONLを
`--completed-jsonl`でexportへ渡し、同じ候補集合の完了済みkeyだけを除外する。
別snapshotや別modelのkeyが混じった場合は再開とせず失敗する。

分類単位は1本の物理`REFERENCES`ではなく、同じ物理方向を持つ1組のArticleペアである。
そのペアを結ぶ全`basisEdgeIds`と全`referenceOccurrences`を同じ候補へ含め、Article本文を文脈として使う。
各occurrenceは対応する`basisEdgeId / referenceKind`を持つ。`referenceKind`は抽出上の手掛かりであり、
意味predicateの正解として扱わない。成立predicateごとにLunaが根拠occurrenceを選び、Programは
その既知hashから保存対象のbasis edgeを一意に復元する。
OpenSearchの子チャンクが親チャンク本文を再掲する場合は、`parentContentUnitId`で
直接の親子と確認できた先頭部分だけを除き、重複のないArticle全文を復元する。
Article本文は決定的なspan ID付きでLLMへ渡し、LLMは判断根拠とするspan IDを選ぶ。
自由記述の引用文は求めず、プログラムは選ばれたIDが対応Articleに存在することだけを検証する。
Luna Workerは5 predicateを同じ候補について一度に比較し、それぞれ固有の二必要条件とfinding、
成立時の根拠IDを返す。複数predicateは独立に成立できる。ReviewerはWorkerの回答と同じ候補本文を受け取り、
誤りを具体的に指摘する。差戻しは1回だけとし、同じWorkerが指摘を参照して全5分類を再確認した後、
同じReviewerが差分を最終確認する。処理量は候補を複数のWorker / Reviewerペアへ分割して並列化する。
Worker出力後は
`.agents/skills/legal-relation-adjudicator/scripts/bind_single_occurrence_ids.py`を実行する。
参照箇所が1つだけの候補では、Programが入力packetの既知`occurrenceHash`をassertionへ機械的に束縛する。
複数参照箇所がある候補では選択自体が意味判断になるため補正せず、Workerが選んだ既知hashだけを許可する。
プログラムは条件とfinding、既知ID、件数の整合を検証してoutcomeと保存対象Assertionへ決定的に投影するだけで、
predicate・条件値・finding・方向・根拠を補正しない。

参照抽出では、`改正前の<法令名>第N条`または`改正前における<法令名>第N条`を現行Articleへ
接続しない。保存snapshotに旧版Articleがなく一意に解決できない場合は`REFERENCES`を生成せず、
同じ文中の`新規則第N条`等の現行参照だけを独立して接続する。この規則を変更した場合はNeo4jだけでなく、
同じsnapshot契約を持つOpenSearchも保存XMLから再seedし、代表94件の構造評価を再実行する。

現行`classify_legal_relations.py`のOllama経路は、1候補をpredicateごとの5回と根拠付与に分ける実装のままである。
これはローカル契約試験と比較baselineには使えるが、LunaのWorker / Reviewer成果をGraphへ登録するimport経路ではない。
Luna成果は`import_relation_adjudication_results.py`が、元packet・Worker回答・Reviewer承認・
差戻し回数をPydantic契約で検証してから`ClassificationRun`へ取り込む。
Worker回答単体は取り込めない。同じpayloadの再importはskipし、異なるpayloadは上書きしない。

```bash
# Neo4jを更新せず、入力契約と保存予定件数を確認
python3 scripts/import_relation_adjudication_results.py \
  --manifest eval-results/relation-full/shards/manifest.json \
  --packet eval-results/relation-full/packet.jsonl \
  --approved eval-results/relation-full/approved.jsonl \
  --unresolved eval-results/relation-full/unresolved.jsonl

# 候補別checkpointと承認済みAssertionを保存（まだpublishしない）
python3 scripts/import_relation_adjudication_results.py \
  --manifest eval-results/relation-full/shards/manifest.json \
  --packet eval-results/relation-full/packet.jsonl \
  --approved eval-results/relation-full/approved.jsonl \
  --unresolved eval-results/relation-full/unresolved.jsonl \
  --apply

# 全scopeのimport後だけ、同じrun IDを監査してpublish
python3 scripts/import_relation_adjudication_results.py \
  --manifest eval-results/relation-full/shards/manifest.json \
  --packet eval-results/relation-full/packet.jsonl \
  --approved eval-results/relation-full/approved.jsonl \
  --unresolved eval-results/relation-full/unresolved.jsonl \
  --run-id classification-run-... \
  --apply --publish
```

`RELATION_CLASSIFIER_CONTEXT_TOKENS=131072`はOllama経路で長いArticleを黙って切り捨てないための上限である。

新schemaの候補数とsnapshot整合だけを確認し、LLM・Neo4j更新を行わないdry-run:

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
python3 scripts/classify_legal_relations.py --limit 10
```

Ollama経路の小規模分類を実行してcheckpointを保存し、Runを`building`のまま検査する場合:

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
RELATION_CLASSIFIER_PROVIDER=ollama \
RELATION_CLASSIFIER_MODEL=gemma4:e4b \
RELATION_CLASSIFIER_REVIEWER_MODEL=gemma4:e4b \
python3 scripts/classify_legal_relations.py --limit 10 --apply
```

中断した`building` Runは、最初の出力・ログにあるIDを指定して再開する。同じ候補の
成功済み`ClassificationCheckpoint`は再度LLMへ送らない。`failed` checkpointはエラー段階・
メッセージ・対象predicateを保持し、再開時に再試行して同じcheckpointを置換する。

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
RELATION_CLASSIFIER_PROVIDER=ollama \
RELATION_CLASSIFIER_MODEL=gemma4:e4b \
RELATION_CLASSIFIER_REVIEWER_MODEL=gemma4:e4b \
python3 scripts/classify_legal_relations.py \
  --limit 10 \
  --run-id classification-run-... \
  --apply
```

`--apply`だけではpublishしない。新5 predicate fixtureと対象scopeの品質確認後、同じ`--run-id`へ
`--apply --publish`を明示した場合だけRunを公開する。全件Runの前に、
10件、34件の旧移行baseline、新5 predicate fixture、代表100候補の順で品質と時間を確認する。

府令を含む34件の固定fixtureで現在の分類精度を評価する場合は次を実行する。
これは旧二値分類の移行baselineであり、新5 predicateの受入れ評価ではない。
評価用Graph代理が更新をメモリに捕捉するため、Neo4jは更新されない。

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
RELATION_CLASSIFIER_PROVIDER=ollama \
RELATION_CLASSIFIER_MODEL=gemma4:e4b \
RELATION_CLASSIFIER_REVIEWER_MODEL=gemma4:e4b \
python3 scripts/evaluate_legal_relation_classifier.py
```

既定の`RELATION_CLASSIFIER_BATCH_SIZE=1`は、ローカル小型モデルが同時提示された別候補の
本文・意味判断を混同しないための精度優先値である。2以上は速度比較用の明示設定とする。
特定fixtureだけを再現する場合は`--fixture-id`を複数回指定できる。

新5 predicateの回帰fixtureを、Neo4jへ書き込まず実データで評価する場合:

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
RELATION_CLASSIFIER_PROVIDER=ollama \
RELATION_CLASSIFIER_MODEL=gemma4:e4b \
RELATION_CLASSIFIER_REVIEWER_MODEL=gemma4:e4b \
python3 scripts/evaluate_legal_relation_5predicate.py
```

2026-08-18のv19では、内部candidate keyをLLM入力から除外し、根拠spanを原文参照の物理方向で
選択する契約に改めた。民法618条→617条の`INCORPORATES`、603条→602条の
`REFERENCE_ONLY`、619条→622条の2の`USES_DEFINITION`かつ非`EXCEPTION_TO`を3/3で確認した。
これは観測済み誤分類の回帰fixtureであり、5 predicate全体の受入fixtureや全候補の精度評価を
代替しない。候補総数は参照構造修正後の再seedで確定し、旧4,323件を現行値として使わない。

手動で参照先と意味関係を確認した20件について、Graphの参照先解決とLLMの意味分類を
分離して評価する場合は次を実行する。既定では構造だけを監査し、`--classify`を付けた場合も、
参照先が正解と一致し、意味分類の正解ラベルを確定できたペアだけをLLMへ渡す。
誤接続されたArticleペアをモデル精度の評価へ混ぜない。どちらの実行もNeo4jを更新しない。

```bash
python3 scripts/evaluate_legal_relation_20_adjudicated.py

OLLAMA_BASE_URL=http://localhost:11434 \
RELATION_CLASSIFIER_PROVIDER=ollama \
RELATION_CLASSIFIER_MODEL=gemma4:e4b \
RELATION_CLASSIFIER_REVIEWER_MODEL=gemma4:e4b \
python3 scripts/evaluate_legal_relation_20_adjudicated.py --classify
```

fixtureはfine-tuning用データではなく、参照解決器と分類器を同じ20件で比較するための
正解ラベル付き評価データである。構造修正後に参照先が変わる3件は、正しいArticleペアで
本文を再確認するまで`expectedPredicates: null`として意味分類対象から除外する。

2026-08-18の初回baselineでは、現行Graphの参照先解決が14/20、参照先が正しい14ペアに
限定した`gemma4:e4b`の5 predicate完全一致が4/14だった。構造側の不一致6件は、見出しを
参照扱いした1件、解決不能な他法令・改正法令を自己参照にした2件、附則内参照を本則へ
接続した1件、法律を参照する文脈を施行令の自己参照にした2件である。意味分類側では、
`INCORPORATES`の過剰成立と`IMPLEMENTS`の見落としが主に残った。したがって、Graphの
参照先修正だけで分類精度が解決するとは扱わない。

#### GemmaからLunaへ切り替えた理由（2026-08-19）

`gemma4:e4b`を不採用にした理由は、ローカルモデル一般が法令を扱えないからではなく、
このレポで必要な「構造監査済みArticleペアについて5 predicateを独立評価し、引用出現と両端spanを
根拠として固定する」という受入条件を安定して満たさなかったためである。

- 旧34件の34/34は、明示的な委任文言を中心とした機能試験であり、5 predicateの実務的な識別能力を示さない。
- 参照先が正しかった手動監査14件では、Gemmaの5 predicate完全一致は4/14だった。
- 主な誤りは、`IMPLEMENTS`と単なる親法令カテゴリー利用の混同、`INCORPORATES`の過剰成立、
  `USES_DEFINITION`の見落とし、参照文言と根拠spanの対応不安定であった。
- 本則・附則の誤接続、親本文が子chunkへ再掲される問題、同じ引用文言の複数出現を区別できない問題は
  モデル精度と分離して構造側で修正した。これらをGemmaの失敗として数えていない。

構造修正後は、Codex `gpt-5.6-luna`を同じモデルのWorker / Reviewerペアとして使用した。
ReviewerはWorkerの回答を見たうえで誤りと根拠を指摘し、Workerの差戻し対応は1回だけに制限した。
既存14件の回帰は最終14/14、新規20件は2ペア（11件と9件）で並列実行し、初回の3件を
1回の差戻しで修正して最終20/20、未解消0件となった。新規20件はさらに人手相当の全件監査を行い、
確定fixture
`docs/samples/eval/legal_relation_parallel_20_adjudicated_fixture.jsonl`と全件一致した。

全件用の最終運用では、1 Worker sessionと1 Reviewer sessionへそれぞれ最大5候補を渡し、候補ごとに
独立したJSONL recordとcheckpointを作る。WorkerとReviewerは別contextである。差戻しは元のWorkerへ
1回だけ返し、同じReviewerが差分を最終確認する。同時に実行中のCodex sessionは最大3つとし、
完了した枠へ次shardを割り当てる。5件版で代表100件を再評価し、構造・意味・方向・groundingの
品質ゲートを通過するまでは全件分類を開始しない。

#### 代表100件の構造・意味評価データセット（2026-08-19）

法令とガイドを同じ意味分類schemaへ押し込まず、次の2レーンをmanifestで束ねた。

- 法令関係94件: 13法令系統、5 `referenceKind`、本則・附則22件を含む。
- ガイド6件: 既存のガイド検索と明示`EXPLAINS`遷移を検査する。

論理シャードは20件ずつ5本である。利用可能なagent slotがCoordinatorを含め4だったため、
物理実行は最大3並列とし、完了した枠へ残りを投入した。法令94件は意味分類前に全件を構造監査し、
最終的に`resolved=73 / unresolved=13 / not_reference=8`となった。法令94件とガイド6件は
Codex GPT-5.6 Solが全件を個別確認し、正解データの最終所有者となる。以前のLuna Worker /
Reviewer成果は作業履歴としてauditに残すが、正解の根拠にはしない。差戻し後も不一致だった
1件は、範囲指定された準用の対象に第114条の13が含まれるため、Codexの全文監査で
`INCORPORATES`と確定した。プログラムに意味ラベルを補完させていない。
また、金融商品取引法施行令第1条の7の3第5号が、同令第2条の12の2で範囲を定めた
有価証券を使う1件は、従来の成立なしを見直して`USES_DEFINITION`と確定した。
さらに、改正命令の附則が明示する「改正後の企業内容等の開示に関する内閣府令第三条」を、
改正命令自身の第三条として扱っていた1件を修正した。正しい本則第三条とのペアについて、
施行日前後の適用を分ける経過措置の全文を確認し、`INCORPORATES`、`EXCEPTION_TO`、`OVERRIDES`を確定した。
その後のskill回帰で、附則表が参照する改正当時の第十五条の三第二項と、packetに供給された現行
第十五条の三の項構造・主題が一致しない1件を検出した。構造上のArticle解決は維持するが、同一revision
の全文でないため意味判定は`needs_resolution`とし、意味分類可能な対象は72件へ訂正した。
意味分類可能な全ペアについて定義文言も横断確認し、定義条文が将来の利用条文を列挙する物理参照では、
`USES_DEFINITION`の意味方向が物理`REFERENCES`と逆になることを明示した。正解生成プログラムは
候補抽出にだけ使い、述語の追加・削除やSUBJECT / OBJECTの選択には使っていない。
その後のoccurrence-local回帰で、施行規則第十五条の十一から第一条の二第五項第十号への参照は
「健康サポート薬局」の定義ではなく基準・書類要件を指すことを再確認した。同じArticle内に定義があっても
当該occurrenceが定義scopeを橋渡ししないため、この1件の`USES_DEFINITION`を不成立へ訂正した。

次の成立predicate件数とLuna精度は、物理edgeごとに評価していた旧baselineの記録である。
Articleペア単位の現行Gate 6結果と混同しない。

成立predicateは`IMPLEMENTS=9`、`INCORPORATES=5`、`USES_DEFINITION=25`、
`EXCEPTION_TO=5`、`OVERRIDES=2`で、成立なしの負例も含む。Codexの横断監査では、構造監査が見逃した
「医療法第一条の二」を薬機法第一条の二へ接続した1件を`unresolved`へ修正した。Workerが
`needs_resolution`で停止したため、この誤接続に意味ラベルは付いていない。

確定した正解を伏せたLunaの再評価結果は次のとおりである。構造監査は`89/94`。意味分類可能な
72件では、Worker初回がstatusと成立predicateの完全一致`51/72`（70.8%）、Reviewerと1回だけの
差戻し後が`57/72`（79.2%）だった。後者では71件がReviewer承認、1件が未解消で、
SUBJECT / OBJECTまで含む完全一致は`56/72`（77.8%）である。ガイド6件は意味5分類の対象ではなく、
専用の決定的評価で`6/6`だった。この結果から、現行Luna方式を無監査で全件publishする精度には達していない。

#### Articleペア単位Gate 6の初回結果（2026-08-19）

保存XMLからOpenSearch 16,459文書、Neo4j 17,254 node / 34,206 edgeを再構築した。
`REFERENCES`は16,964件で、代表94件の構造評価は`94/94`、Graph監査違反は0だった。
全件exportは14,454 Articleペア候補、16,964 basis edge、最大5候補の2,891 shardとなった。

代表データのうち構造的に有効な73 Articleペアを15 shardへ分け、goldを見せない新規contextで
Luna `high`のWorker / Reviewerを最大3 session並列で実行した。差戻しは5候補、各1回で、
final Review後のworkflow `unresolved`は0だった。初回採点はstatus `73/73`、5 predicate
`355/365`、候補単位のpredicate完全一致`63/73`、単一gold spanとのstrict完全一致`57/73`で停止した。

差分監査により、predicate差10件にはLuna誤判定だけでなく、旧edge単位goldからArticleペアgoldへ
移した際の教師データ誤りが含まれることが分かった。またstrict grounding差には、同じ法的関係を
直接支える別spanを選んだだけの候補が含まれていた。したがって、この初回値を最終精度として使わない。

修正方針は次のとおりである。

- Worker / Reviewerは、各predicateについて選択した参照出現が両端の意味役割を結ぶことを確認する。
- goldはCodexがArticle全文と参照出現を再確認して修正し、Luna出力を機械的に正解へ採用しない。
- 複数の妥当なgroundingは、人が確認した既知IDの許容集合として評価データへ保存する。
- scorerは許容集合との一致だけを検査し、隣接・親子spanから意味的な許容範囲を推測しない。
- 修正後は差分候補だけを新規Worker / Reviewer contextで再評価し、合格までGate 7へ進まない。

`adjudicationStatus=needs_resolution`は入力Articleペアの構造・版が意味分類に適さないという判断、
workflow `unresolved`は差戻し1回後もReviewerが承認しなかった実行結果であり、別の値である。

成果物は次の3ファイルを正本とする。

- `docs/samples/eval/legal_relation_guidance_100_manifest.json`
- `docs/samples/eval/legal_relation_94_adjudicated_fixture.jsonl`
- `docs/samples/eval/legal_relation_94_adjudication_audit.jsonl`

Articleペア化後に人が再監査した意味goldは
`docs/samples/eval/legal_relation_73_pair_overrides.jsonl`で明示的に上書きする。
builderは複数edgeだけでなく単一edgeの明示overrideも旧edge goldより優先する。現在は複数edge 15件と
単一edge 3件の計18件である。このファイルはProgramが意味を生成する規則ではなく、CodexがArticle全文を
確認した監査結果である。複数の妥当なgroundingは
`docs/samples/eval/legal_relation_73_grounding_allowances.jsonl`へ候補・predicate単位で明示する。
scorerは既知occurrence/span IDとcanonical goldを含むことを検証したうえで集合との完全一致だけを調べ、
隣接spanや親子spanから許容値を自動生成しない。これらの評価成果物はWorker / Reviewerへ渡さない。

初回Luna v2成果物を、訂正したgoldと6件のgrounding許容集合で再採点した監査値は、status `73/73`、
5 predicate `356/365`、候補完全一致`63/73`、grounding `54/62`である。これは同じLuna出力の再採点であり、
v3契約による再実行結果ではない。

残る10候補は、gold・過去出力を見ない新規Luna Worker / Reviewer contextで差分再評価した。
本文監査で2件のgold誤りを訂正した後、pair-v3は`7/10`一致した。残る3件について、特定Articleの
正解ではなく、無名の法的役割、前方スコープが及ぶ正確な構造単位、無関係な逆参照を区別する一般則を
pair-v4へ追加し、`3/3`一致した。v2の一致63件、v3の一致7件、v4の一致3件を評価用途で合成した結果は、
status `73/73`、5 predicate `365/365`、方向 `62/62`、grounding `62/62`、候補完全一致`73/73`である。
差分成果物は`eval-results/relation-guidance-100-pair-v3-diff/`と
`eval-results/relation-guidance-100-pair-v4-diff/`に保存した。skill versionが異なるため、これらを
同一ClassificationRunへimportしてはならない。

2026-08-20の追加回帰で、`第X条の規定の適用については、同条中「A」とあるのは「B」とする`
という読替適用をLunaが`OVERRIDES`だけに分類し、`INCORPORATES`を落とす例を確認した。
pair-v7では、読替後も対象規律が適用される場合は両predicateを独立に成立させ、対象規律を直接
`適用しない`場合は`OVERRIDES`から`INCORPORATES`を自動成立させない一般則をWorker / Reviewer契約へ追加した。
Luna用versionはprompt `legal-relation-5predicate-v22-pair`、skill
`legal-relation-adjudicator-2026-08-20-pair-v7`である。ローカルOllama経路の旧prompt本文とversion 21は
別契約として維持し、全件Luna成果物と混在させない。

正解を除去した3候補で、読替適用、不適用、置換を伴わない直接適用を新規Luna Worker / Reviewer
contextへ渡した結果、predicate完全一致`3/3`、Reviewer承認`3/3`、差戻し0件だった。
成果物は`eval-results/relation-incorporates-regression-pair-v7-complete/`に保存した。
同時にローカル回帰は`768 passed`であり、特定の第140条をPromptへ記載していない。

gold再構築と再採点は次で行う。`--output`は既存成果物を上書きしないため、新しいパスを指定する。

```bash
agent-api/.venv/bin/python scripts/build_relation_pair_gold.py \
  --packet eval-results/relation-guidance-100-pair-v2/semantic-blind-packet.jsonl \
  --legacy-audit docs/samples/eval/legal_relation_94_adjudication_audit.jsonl \
  --pair-overrides docs/samples/eval/legal_relation_73_pair_overrides.jsonl \
  --output /tmp/semantic-pair-gold.jsonl

agent-api/.venv/bin/python scripts/score_relation_pair_output.py \
  --packet eval-results/relation-guidance-100-pair-v2/semantic-blind-packet.jsonl \
  --gold /tmp/semantic-pair-gold.jsonl \
  --actual eval-results/relation-guidance-100-pair-v2/semantic-approved/*.jsonl \
  --grounding-allowances docs/samples/eval/legal_relation_73_grounding_allowances.jsonl
```

`--actual`は複数ファイルを受け付け、別ディレクトリを加える場合はオプション自体を繰り返せる。

ガイド6件は既存の
`docs/samples/eval/guidance_navigation_fixture.jsonl`を参照する。100件fixtureの件数、
status、predicate網羅、監査契約は次で回帰する。

```bash
agent-api/.venv/bin/pytest -q \
  agent-api/tests/test_legal_relation_guidance_100_fixture.py \
  agent-api/tests/test_legal_relation_incorporates_contract.py
```

Graphの参照解決を修正した後は、次で94件の構造正解と照合する。2026-08-19の再seed後は
`94/94`である。正解上`not_reference`または`unresolved`の21件はGraph候補から除外済みである。
合格条件は引き続き`94/94`とする。このコマンドは意味分類やGraph更新を行わない。

```bash
agent-api/.venv/bin/python \
  scripts/evaluate_legal_relation_20_adjudicated.py \
  --fixture docs/samples/eval/legal_relation_94_adjudicated_fixture.jsonl
```

したがって、現時点の採用判断は次のとおりである。

- Gemma経路: ローカルの契約・速度・比較試験用。全件Runをpublishする品質経路には使わない。
- Luna経路: Codexサブスクリプション内で、確定済み正解を見せずに並列実行する評価対象。
- Luna出力は正解生成に使わず、Codexが全件確認したfixtureと照合する。
- 法令snapshotまたは分類契約が変わらない限り、確定済み`candidateKey`を再分類しない。

ガイド6文書について、OpenSearch検索、明示`EXPLAINS`、遷移先Article全文取得を
実データで検査する場合は次を実行する。LLMとClaude APIは使わず、Neo4jも更新しない。

```bash
python3 scripts/evaluate_guidance_navigation.py
```

2026-08-18の旧30件`gemma4:e4b`評価では、自由記述の引用を使った旧契約が3/30、
span ID選択へ修正した初期契約が19/30だった。参照文がspan境界をまたぐ場合も位置対応し、
長文用contextとtimeoutを適用したv7契約は24/30（80%）だった。内訳は
`implements` 13/15、`reference_only` 11/15で、残る誤りには「下位法令が親条文を
権限委任の対象として列挙すること」と「親条文自身の委任を下位法令が実施すること」の
混同がある。
v8ではspan IDをArticle ID付きで一意化し、Article本文を候補内へ閉じ、1候補ずつ判断する。
追加した府令4件は、法律→府令の正例・負例、施行令→府令、複数参照箇所を含む。
同日の`gemma4:e4b`によるv8全34件評価は34/34だった（一次`uncertain` 1件を同モデルの
Reviewerが再検討）。府令タグは法律→府令3/3、施行令→府令1/1、公開買付け3/3である。
このfixtureは明確な文言の候補を選んだ機能試験であり、参照構造修正後に確定する全候補の正確な
正答率推定には使わない。現時点で全候補の一括登録は行わない。

### 日本語Analyzer索引

既存`legal-rag-content`を削除せず、Kuromoji＋NFKCとbigramのmulti-fieldを持つ
`legal-rag-content-ja-v2`を既定で使用する。Analyzer pluginを追加するため、最初に
OpenSearchをbuildする。

```bash
docker compose build opensearch
docker compose up -d opensearch
python3 scripts/create_japanese_search_index.py
```

再索引は既存のembeddingをコピーするため、全件の埋め込みを再生成しない。target索引が既に
存在する場合、スクリプトは削除・上書きせず終了する。作り直す場合は対象名と用途を確認してから
明示的に別名を指定する。

Agent APIの既定値は次の設定である。明示指定する場合も同じ値を使う。

```bash
OPENSEARCH_INDEX=legal-rag-content-ja-v2 \
OPENSEARCH_INDEX_MAPPING=metadata/opensearch_index_mapping.japanese.sample.json \
docker compose up -d --build agent-api
```

障害切り分けのため旧Analyzerへ明示的に戻す場合:

```bash
OPENSEARCH_INDEX=legal-rag-content \
OPENSEARCH_INDEX_MAPPING=metadata/opensearch_index_mapping.sample.json \
docker compose up -d agent-api
```

検索スコア、候補上限、探索ループはこの設定では変更しない。`heading`、`text`、
`sectionPath`等の既存検索fieldを、索引時・検索時とも日本語Analyzerで解析する。

### seed中の挙動（ハングではない）

`SEED_LAWQA_EGOV=true` 込みの seed は、e-Gov法令の取得と**全チャンク（約1.6万件）の埋め込み
生成をメモリ上で完了してから** OpenSearch へ一括投入する。そのため投入完了までの数分〜
十数分間、対象索引の`_count`は **0 のまま**になる。これはハングではない。

進行中か（=正常）を確かめる目安:

```bash
# ネットワーク受信量とメモリが増え続けていれば進行中
docker stats --no-stream local-rag-poc-law-agent-api-1
# エラーが出ていないこと
docker compose logs --tail 30 agent-api | grep -iE "error|exception|traceback"
```

`/admin/seed` の HTTP レスポンス（`{"status":"seeded", ...}`）が返れば完了。docCount が
0 のままでも、レスポンスが返るまでは待つこと。埋め込みは Ollama(bge-m3) が律速で、
このフェーズが最も時間がかかる。

> seed 環境変数（`SEED_LAWQA_EGOV` / `SEED_EXTERNAL_GUIDANCE` 等）は `docker-compose.yml` の
> `${VAR:-default}` で **agent-api コンテナ起動時に読まれる**。curl 側に付けても効かないため、
> `SEED_LAWQA_EGOV=true SEED_EXTERNAL_GUIDANCE=true docker compose up -d --build agent-api` の
> ように **compose up 時**に指定してから seed する。

## 4. API 動作確認

Hybrid 検索:

```bash
curl -s http://localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{
    "query": "有価証券の定義",
    "docType": "law",
    "topK": 5,
    "userClearanceLevel": 2
  }' | jq .
```

Graph 検索:

```bash
curl -s http://localhost:8000/graph/path \
  -H 'content-type: application/json' \
  -d '{
    "fromGraphNodeId": "law-323AC0000000025",
    "edgeType": "HAS_CONTENT_UNIT",
    "maxDepth": 1,
    "userClearanceLevel": 2
  }' | jq .
```

lawqa_jp 形式:

```bash
curl -s http://localhost:8000/answer \
  -H 'content-type: application/json' \
  -d '{
    "question": "金融商品取引法第2条第1項に照らして、次の記述のうち正しいものはどれか。",
    "choices": {
      "A": "有価証券には国債証券および地方債証券が含まれる。",
      "B": "有価証券には国債証券は含まれるが、地方債証券は含まれない。",
      "C": "有価証券は株券だけを意味する。",
      "D": "有価証券の定義は金融商品取引法に置かれていない。"
    },
    "pattern": "pattern_2_rule_based_agentic_rag",
    "userClearanceLevel": 2,
    "candidateTopK": 20,
    "rerankTopK": 10,
    "topK": 5
  }' | jq .
```

最終系を確認する場合は、`pattern` に `pattern_4_deepsearch` を指定する。レスポンスの `trace` で分解クエリ、再検索理由、tool call数、Graph node/edge ID、停止理由を確認できる。探索は設定したtool call数・再検索回数・110秒のwall timeを超えない。

### 時間予算プロファイルの確認

`/health` が採用中の時間profileを公開する。eval-runnerは起動時にこれと `REQUEST_TIMEOUT_SEC` を
突き合わせ、agent wall timeより短い場合は評価を開始せず設定エラーにする。

```bash
curl -s http://localhost:8000/health | jq '.timeBudget, .layeredLegalRetrieval'
```

`timeBudget.warnings` に次が出た場合は、Phase 0 の実測後に値を明示設定する
(自動では書き換えない。`docs/layered_legal_evidence_retrieval_plan.md` §11.2)。

- `AGENT_ANSWER_RESERVE_SEC < LLM_TIMEOUT_SEC`: 回答LLMがtimeout上限まで使うとwall timeを超え得る
- componentの設定timeout×最大呼び出し回数の合計が full-answer-safe 探索予算を超えている

### 法令レイヤー別探索 vNext (shadow)

`AGENT_LAYERED_LEGAL_RETRIEVAL_SHADOW=true` にすると、現行の検索・回答を変えずに
新方式(論点→必要根拠スロット→レイヤー別探索→conclusionGroup単位のコンテキスト選抜)を
同一リクエスト内で実行し、`trace.layeredLegalRetrieval` へ記録する。

```bash
curl -s http://localhost:8000/answer -H 'content-type: application/json' \
  -d '{"question":"公開買付けの手続が必要になるのはどのような場合ですか","pattern":"pattern_4_deepsearch","userClearanceLevel":2,"topK":5}' \
  | jq '.trace.layeredLegalRetrieval | {mode, contextCoverage, expansionRounds, stopReason, shadowIncomplete}'
```

主な確認項目:

- `legalIssuePlan`: 論点分解(条番号はplannerではなく決定的パーサーの結果を使う)
- `guidanceLane`: ガイドから辿れた条文候補。`explainedArticleIdsByIssue` は EXPLAINS 由来で
  法令本文の直接取得に使い、`mentionedArticleIdsByIssue` と未確認 `guidanceRelationAssertions`
  は検索範囲の拡張だけに使う(ガイドだけで法令Requirementをresolvedにしない)
- `evidenceRequirements` / `requirementTransitions`: 必要根拠スロットの状態遷移と未解決理由
- `satisfactionByRequirement`: 候補Articleが論点語・法的役割または信頼済み直接取得によって
  Requirementを満たしたと判定した理由
- `conclusionGroups` / `contextCoverage.answerStatus`: 主論点groupを完全被覆できたか
- `graphEdgesAccepted` / `graphEdgesRejected`: どの関係を信頼して辿ったか
- `timeBudget` / `shadowIncomplete`: shadowが回答前の安全余白を侵していないか

`AGENT_LAYERED_LEGAL_RETRIEVAL=true`(active)にすると、主論点groupを被覆できた場合だけ
新方式のコンテキストを回答LLMへ渡す。主論点の根拠が被覆できない場合は、旧経路の根拠で
通常回答したように見せず、利用者へ根拠不足を表示して断定を止める。新方式の内部エラー時だけ
旧経路へ戻る。
ガイドは法令mandatory枠を満たさず、主論点groupを完全被覆できたときにだけ補助枠
(既定2件)へ入り、引用には `evidenceLane: "guidance"` と「行政解釈・実務上の取扱い」が付く。
切替はPhase 6の評価(自然言語20問・lawqa_jp 140問)の合格後に行う。

### LLM主導の法令調査

LLMへ検索語・探索順序・根拠選択の裁量を持たせる新経路の判断契約を実装している。
法令とガイドの取得元は既存のOpenSearch / Neo4jに限定し、未取得のArticle IDや
contentUnitIdはコード側で拒否する。
探索段階の構造化出力のID欄は、今回のPromptに提示したArticle ID、documentId、
contentUnitIdだけを選べるenumにする。統合段階は、同じ長いID enumが共有DAGの
各階層へ反復展開されて入力を膨らませるため、Promptに既知IDを示し、出力直後の
完全一致検証とsanitizeで未知IDを拒否する。LLMが新たな法令名・条番号を調べたい
場合は内部IDを組み立てず、`search_corpus`の検索語として要求する。

反復方式は`iterative_cycles_v8_hypothesis_testing`である。1利用者質問につきプロセス内の
`ResearchCase`を1件作り、LLM Actionを直列`ResearchTask`として実行する。検索・
Graph・本文取得の確認済み結果はツール終了直後に案件へ記録し、統合LLMが成功した
場合だけ新しいCheckpointを作る。統合タイムアウト時も最新Checkpoint以降の案件差分と
未取得Article候補Taskは残る。詳細は
[llm_research_case_store_implementation_plan.md](docs/llm_research_case_store_implementation_plan.md)
を参照する。
一般検索で発見したArticleも`search_result`候補Taskとして案件へ保存する。候補が
Prompt上限を超える場合は文書別に分散して表示ページを進め、先頭候補だけを各サイクルで
繰り返さない。統合LLMへの参照だけでは候補ページを進めず、候補を実行できる探索・
掘り下げ段階だけで進める。Article本文取得時の自動Graph展開は`IMPLEMENTS`最大12件に限定し、
参照・準用関係の広い探索はLLMの明示的な`expand_graph`へ分離する。
Anthropicが一時的な`529 Overloaded`を返した場合だけ、同一LLM呼び出しのtimeout内で
2秒・4秒・8秒の指数バックオフを行う。入力不正、利用枠不足、その他のHTTPエラーは
同じ経路では再試行しない。

`AGENT_LLM_DIRECTED_RETRIEVAL=true`にすると、旧planner、レイヤー別Requirement生成、
プログラム側の充足判定・根拠枠選抜を通らず、LLM主導調査だけで回答する。プログラムは
許可された検索・本文取得の実行、ID検証、Task・Checkpoint状態遷移、時間・件数上限、
禁止事項の検証を担当し、LLMが候補比較、追加調査、根拠選択を行う。
タイムアウトや根拠不足で終了した場合、旧方式へ黙って戻さず利用者向け回答とtraceへ明示する。
Anthropic等のクレジット・利用枠不足は`provider_quota_error`として、投入資料の根拠不足や
内部エラーと区別して表示する。

3回の各調査サイクルで`explore → deepen → integrate`を実行する。サイクル間では生の
検索履歴を引き継がず、原文ID付きの調査チェックポイントだけを継承する。全取得本文は
証拠カタログへ保持するが、次サイクルへ自動提示するのはチェックポイントの
`evidenceIds`、判断継続中の`openEvidenceIds`、未解決Article、およびそれらを結ぶ
確認済みGraph関係だけとする。
探索・掘り下げJSONは4,096トークンの物理上限に対して2,500トークン以内を目標とし、
根拠IDと未確認Article IDを説明文より優先する。上限へ近づく場合は理由・調査経緯を
要約し、JSONを途中で切らず完結させる。
各サイクルは、質問への暫定結論を`Hypothesis`として作成し、各検索・本文取得Actionを
`hypothesisIds`で検証対象へ紐づけ、取得本文による支持・反証・根拠不足を記録する。
探索では質問の重要な特徴を説明する主仮説を立て、有力な別構成がある場合だけ競合仮説を
残す。掘り下げでは、取得本文が仮説の要件・対象・例外・手続等を実際に定めるか、
質問の重要な特徴を説明できるか、反証や適用範囲の不一致がないかを比較する。
合わない仮説は周辺検索を続けず、維持・修正・棄却・追加のいずれかへ更新する。
CaseStoreは仮説ごとに実行Taskと観測Evidenceを保持し、Checkpointは
`logicalStructure.hypotheses`へ平坦な配列として保存する。各要素は
`hypothesisId / statement / status / evidenceIds / missing`だけを持つ。
`status`は`unverified | partially_supported | supported | rejected`である。
未検証または一部支持の仮説が中心的結論を変え得る場合は`ready`にせず、中心的結論へ
影響しない場合だけ未確認範囲と影響を明示して`ready`にできる。旧nested形式は読込時に
この平坦形式へ変換し、根拠IDと未確認事項を維持する。
`missing`は未確認点の説明であり、プログラムが条番号・法令種別・見出し類似を解釈して
Taskを自動生成する命令欄ではない。次に読む既知ArticleはLLMが`fetch_articles`または
Checkpointの`nextArticleIds`へ明示する。IDが不明なら、法令名・条番号・検証目的を
`search_corpus`または`unresolved(action=search)`へ明示する。プログラムは法的必要性を
推測せず、指定IDの実在性、本文取得状態、Task遷移、許可ツール、予算と終了状態だけを検証する。
各判断とサイクル統合では、`ready`の直前に、結論を支える法令本文を選択済みか、
LLM自身が本文確認を必要と判断して`nextArticleIds`または未確認事項へ残したArticleが
未取得でないかを見直す。残り予算で取得できる場合は`fetch_articles`を優先する。
これは質問された全事項の完全調査を要求するものではない。取得不能時は探索を無期限に
継続せず、中心的結論への影響に応じて`insufficient`または留保付き`ready`とする。
前サイクルで取得した未採用候補は、LLMの次サイクル入力へ再注入しない。必要になった場合は
Article ID直接取得または再検索で明示的に読み直す。形式不正や時間切れでも、
最後に検証済みのチェックポイントがあれば`partial`として限定回答へ利用する。
チェックポイントは`status / conclusion / evidenceIds / openEvidenceIds /
nextQuestions / nextArticleIds`に加え、
`logicalStructure`へIssue単位の共有根拠DAGを保持する。各Issueが`authorityNodes`を
一度だけ所有し、複数のClaimは`authorityNodeIds`で同じ根拠を共有参照する。
`nextArticleIds`は最大10件、各`authorityNode`が保持する`evidenceIds`は最大20件とする。
`authorityNodes`は`parentNodeId`と`relationFromParent`で直接根拠から委任先・定義・例外・
手続具体化・ガイドへのつながりを表す。親IDは同じIssue内だけを参照するため、
正しい法令関係をClaimごとに複製せず圧縮できる。生の検索履歴や長い根拠選択理由は
持ち越さない。
Article直接取得時は、検証済みのGraph関係だけを1hop自動取得して関係先Article IDを
提示する。Graph取得上限は起点Articleごとに適用し、複数起点の一つが候補を独占しない。
取得済みGraph関係はバックエンドのカタログへ保持するが、同一サイクルでは新規関係を
専用欄へ一度だけ提示し、ツール履歴には件数だけを残す。次サイクルではチェックポイントに
残したArticle同士を結ぶ関係だけを提示する。提示時も起点Articleごとのラウンドロビンとする。
起点Article、関係種別、到達先Article、
法令名・条見出しを一組で渡し、関係先の本文取得と最終採否はLLMが判断する。
明示的な`expand_graph`は、正式Graph経路、索引時に`implements`と分類されたナビゲーション関係、
未分類または`uncertain`の`RelationAssertion`を区別して扱う。索引時分類済み関係も正式エッジ・
法的根拠ではない。検索時LLMは質問への関連性と本文取得の要否を判断するが、関係種別を再分類
しない。結論に使う場合は対象Article本文そのものを取得して根拠にする。
各サイクルは残り時間を残サイクル数で配分し、統合LLM用の時間を先に予約する。
統合失敗時に段階判断だけで`ready`へ昇格させず、`continue`の限定状態として扱う。
統合JSONに未確認のArticle ID等が混じった場合は、IDを推測で補正せず、その項目だけを
除外して検証済み部分を`continue`状態で次サイクルへ渡す。1項目の不正だけで、
同じJSONにある確認済みArticleや未解決事項を全て失わない。
中間サイクルの統合LLMがタイムアウトした場合も、`recoverableTimeouts`へ明示したうえで、
直接取得本文を最大20件、直接取得前なら一般検索候補の文書別圧縮一覧を最大18件だけ
1サイクルの回復バッファとして次回へ渡す。正常な統合では一般検索の未採用本文を
統合入力へ含めず、直接取得本文・段階選択本文・確定根拠・判断中根拠を優先する。
統合JSONの不正IDを除去した結果チェックポイントが空になった場合も、正常な統合として
回復バッファを消さず、次サイクルの段階判断と統合判断へ再提示する。
最終サイクルの統合まで
タイムアウトした場合は従来どおり`timeout`または`partial`として利用者へ明示する。
一度取得済みのArticleをLLMが再取得した場合も、カタログ差分が0件であっても今回返した
本文を次段階で優先表示する。
複数Articleの直接取得はArticleごとに最大100chunksを取得し、長い1条だけが他Articleを
押し出さないようにする。LLMへの提示はArticle単位で分散するが、未提示本文は削除しない。
documentId確定後の法令内検索は30 Article、未確定の横断検索は8 Articleとする。
複数文書の候補は文書単位のラウンドロビンでLLMへ提示する。
文字予算で本文を切り詰めた場合は各原文へ`textTruncated`と表示文字数を付ける。
探索・統合LLMが根拠IDとして返せるのは、今回のPromptへ本文を実際に表示したIDだけとする。
最終回答とReviewerにも切り詰め・省略IDのmanifestを渡し、省略IDはcitationIds候補から除外する。
active経路では、Integrationの結論・仮説状態・`verified`判定をMainの判断材料へ流用しない。
MainとReviewerは`issue-grounding-v1`契約の`issueId / question`と利用可能な既知引用IDを共有し、
MainはIssue別の`issueDecisions`、Reviewerは同じIssue ID付きの`findings`を返す。
Reviewerには選択済み引用と未選択候補を分けて表示し、未選択候補は追加検索要否の判別にだけ使う。
プログラムはIssue ID集合、既知引用ID、件数、状態、トップレベルとの集合整合だけを検証する。
切り詰め本文は表示部分が直接支える主張には使えるが、未表示末尾に例外がないことや
列挙の完全性を推測する根拠にはしない。

`AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW=true`は回答非接続の比較用であり、activeと同時に
有効にしてもactive経路が優先される。

```bash
curl -s http://localhost:8000/health | jq '.llmDirectedLegalRetrieval'
```

主な設定:

```text
AGENT_LLM_DIRECTED_RETRIEVAL=false
AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW=false
# 互換設定。役割別設定が空の場合に探索・統合の両方へ使う。
LLM_RESEARCH_MODEL=
LLM_RESEARCH_STAGE_MODEL=
LLM_RESEARCH_INTEGRATION_MODEL=
REVIEWER_MODEL=
LLM_RESEARCH_MAX_TOKENS=4096
LLM_RESEARCH_STAGE_EFFORT=low
LLM_RESEARCH_INTEGRATION_MAX_TOKENS=8192
LLM_RESEARCH_INTEGRATION_EFFORT=low
LLM_RESEARCH_TIMEOUT_SEC=90
LLM_RESEARCH_MAX_TURNS=3
LLM_RESEARCH_MAX_ACTIONS_PER_TURN=4
LLM_RESEARCH_MAX_TOOL_CALLS=18
LLM_RESEARCH_SEARCH_TOP_K=8
LLM_RESEARCH_DOCUMENT_SEARCH_TOP_K=30
LLM_RESEARCH_MAX_CHUNKS_PER_ARTICLE=100
LLM_RESEARCH_ACTIVE_BUDGET_SEC=360
LLM_RESEARCH_SHADOW_BUDGET_SEC=180
LLM_RESEARCH_MAX_EVIDENCE_ITEMS=60
LLM_FINALIZATION_MATERIAL_MAX_ITEMS=24
LLM_RESEARCH_MAX_SELECTED_EVIDENCE=16
LLM_RESEARCH_EVIDENCE_CHARS=30000
```

設計と次の実装手順は
[llm_directed_legal_retrieval.md](docs/llm_directed_legal_retrieval.md)
を参照する。

### Graph棚卸し (Phase 0)

seed済みGraphのnode/edge種別・件数、registryとの一致、authorityTypeの分布を出す。

```bash
PYTHONPATH=agent-api agent-api/.venv/bin/python scripts/graph_inventory.py
```

`ordinance_unspecified` が残る法令は、省令か内閣府令かを人手で確認して
`docs/samples/eval/law_registry.json` の `authorityType` へ明示する。
推測で確定してはならないが、未判別を理由にレイヤー指定検索から除外もしない。

## 5. UI 操作

1. `http://localhost:8501` を開く。
2. Sidebar の `Health check` を押す。
3. 未投入なら `Seed sample data` を押す。
4. Pattern を選び、質問を入力して `Ask` を押す。

UI では回答、検索ルート、citations、Graph paths、trace を確認できる。

## 6. 評価実行

### 新Agent Frameworkの明示検証

`AGENT_FRAMEWORK_ACTIVE=false`のままでも、`/answer/framework`は新Frameworkだけを実行する。
現行経路との取り違えを避けるため、新Frameworkの移行評価ではこのendpointを使う。
Reviewerは`AGENT_FRAMEWORK_REVIEWER_ENABLED=true`を明示した場合だけ有効になり、既定値は`false`。
有効時は最終回答案、WorkItem、Hypothesis、DependencyDecision、取得済みgrounding Evidenceから
`ReviewerView`を機械投影する。Reviewerは`accept`または構造化Findingを返し、差戻し後のSolverは
全finding IDへ`addressed / disputed`を1回ずつ返す。Programは既知IDと全件性だけを検証し、
回答修正か追加調査かを決めない。既定の差戻し上限は1回で、再確認も`revise`なら`review_failed`となる。
検索時LLMの現在の基準検証はOpenAI API `gpt-4o-mini`をresearchとintegrationの両方へ設定して行う。
共通fixtureと公開買付け3問が安定するまでは、Haiku比較を同時に進めない。不具合時はモデル性能だけを原因とせず、
実装、契約の`description`、Prompt、Provider輸送、入力、`trace.agentFramework`を先に調べる。
`gpt-4o-mini`で合格後、同じfixtureと合格条件のままHaikuへ変更し、Provider差だけを確認する。
これはLunaを使う非同期Relation分類とは別の運用である。
初回の作業分解とTool選択には`AGENT_FRAMEWORK_RESEARCH_MODEL`、ToolResult取得後の意味評価・
状態統合・追加調査または終了の判断には`AGENT_FRAMEWORK_INTEGRATION_MODEL`を使う。
旧`AGENT_FRAMEWORK_FINALIZE_MODEL`も互換設定として読めるが、新規設定ではintegration名を使う。
全Solver呼出しへ短い`solver_common.md`を合成する。Toolを使える処理だけへ`solver_tools.md`、
完了判断を行う処理へ`solver_completion.md`を追加する。実行手順は`research / integration /
cycle_close / finalization / reviewer_revision / search_selection / graph_selection`別のPromptから一つだけ選ぶ。
選択条件はContextの構造値だけで、法的意味やToolの必要性はSolverが判断する。

最初の検索動作確認用設定:

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
AGENT_FRAMEWORK_RESEARCH_MODEL=gemma4:e4b
AGENT_FRAMEWORK_INTEGRATION_MODEL=gemma4:e4b
AGENT_FRAMEWORK_REVIEWER_ENABLED=false
AGENT_FRAMEWORK_DIAGNOSTICS_MODE=off
AGENT_FRAMEWORK_POST_RUN_AUDIT=off
```

```bash
curl -s http://localhost:8000/answer/framework \
  -H 'content-type: application/json' \
  -d '{"question":"公開買付けの手続が必要になるのはどのような場合ですか","pattern":"pattern_4_deepsearch","userClearanceLevel":2,"topK":5}' \
  | jq '{answer, citations, agentFramework: .trace.agentFramework}'
```

既定の`AGENT_FRAMEWORK_DIAGNOSTICS_MODE=off`では、`trace.agentFramework`へ
`reviewerEnabled`、`researchCycleCount`、`modelCalls`、`toolCalls`、`runStatus`等の小さい実行要約だけを返す。
`runStatus=completed`は実行完了だけを表し、
回答の法的十分性を意味しない。
`toolCalls`には本文を含めず、Solverが指定した検索・本文取得・1ホップGraph取得の`arguments`と
`purpose`を残す。本文取得だけではGraphを自動実行しない。

統合の状態分析が必要な実行だけ、次の診断modeへ変更する。
機能の目的、保存情報、応答フィールドの説明は
[Agent事後監査](docs/agent_post_run_audit.md)を参照する。本節は実行手順を正とする。

| `AGENT_FRAMEWORK_DIAGNOSTICS_MODE` | API trace | ローカル診断ファイル |
|---|---|---|
| `off` | 実行・model・Toolの要約 | 出力しない |
| `status` | 上記にWorkItem、Hypothesis、Graph review等の状態一覧を追加 | 本文を含まない状態・件数・契約違反をJSONLで出力 |
| `snapshot` | `status`と同じ | 統合直前の`CaseState`、実際の`SolverContext`、Model Profile、Providerへ渡したPrompt・schema、Decision、契約違反を含む |

診断ファイルは`EVAL_RESULTS_DIR/agent-framework-diagnostics/<case_id>.jsonl`へ保存する。
`eval-results/`はGit管理外である。診断出力に失敗しても回答処理は失敗させない。modeは出力・保存だけを
切り替え、SolverのPromptや`SolverContext`を増減しないため、`status`や`snapshot`を有効にしても
LLMへの入力tokenは増えない。通常は`off`を維持し、再現対象の1実行だけ`status`または`snapshot`にする。

双方向の契約は次のrecordで追跡する。`transport_input`はプログラムからLLMへ送ったPrompt・schemaの
`promptHash`、`schemaHash`、Profile名・versionと、外部Prompt assetのファイル名・使用section・hashを持つ。
`snapshot`ではPromptとschema本文も保存する。`transport_output`はLLMから返った生payloadと
`payloadHash`、輸送検証エラーを持ち、`solver_output`以降は正規化済み`SolverDecision`と
`solverDecisionHash`を持つ。同じhashにより、生応答、正規化後、契約違反、CaseState適用の境界を区別する。
Prompt assetの本文を変更した場合はProfile versionも更新し、実行時の来歴と設定上の契約versionを一致させる。

Solverへ渡る項目の基本的な意味はPydanticの`Field.description`から`contract_glossary`へ生成する。
Toolの正規名、用途、入力Schema、戻り値は実行時の`available_tools`へ投影する。
`snapshot`では、Providerへ実際に渡したPrompt内の`contract_glossary`と
`solver_context.available_tools`、構造化出力schemaが同じ正規項目名を使っていることを確認する。
OpenAI経路のTool引数は`arguments` objectであり、`arguments_json`の二重JSONを新規出力しない。
Anthropic adapterに輸送上のsidecarが必要でも、正規の契約名と意味は変更しない。

特定の`solver_input`を外部LLMなしで再現するfixtureへ固定する場合は、診断JSONLの`sequence`を指定する。
`sequence`は診断ファイル内の対象recordを`jq`等で確認してから選ぶ。次は公開買付け総合問題の、初回
OpenSearch結果を受けた統合入力を固定した実行例である。

```bash
python3 scripts/promote_agent_diagnostic_fixture.py \
  --input eval-results/agent-framework-diagnostics/legal-....jsonl \
  --sequence 6 \
  --fixture-id tob-overview-cycle1-after-search-v1 \
  --question-id tob-overview \
  --output agent-api/tests/fixtures/framework/tob_overview_cycle1_after_search_v1.json
```

生成fixtureは`CaseState`と実際の`SolverContext`を保持し、case IDだけfixture用の固定値へ置換する。
回帰テストでは同じ`CaseState`から`SolverContext`を再投影し、完全一致を確認する。元の診断JSONLや
Prompt全体はfixtureへ複製しない。現在の`after-search` fixtureは本文取得前の失敗分析用であり、
LR-004が求める「Cycle 1で3 Article取得直後」のfixtureとは区別する。

各Solver呼出しは、内部思考の逐語記録ではなく、そのStepで`continue`または`finalize`を選ぶ直接の理由を
`SolverDecision.decision_reason`へ一文で返す。構造契約を通過してCaseStateへ適用されたDecisionだけを
診断JSONLの`decision_applied`へ保存し、契約修復前の不正なDecisionと区別する。API traceの
`caseId`と、診断有効時の`appliedDecisionSequences`で対象を特定できる。

処理終了後に理由を確認するときだけ、回答実行前に次を設定する。

```bash
AGENT_FRAMEWORK_DIAGNOSTICS_MODE=snapshot
AGENT_FRAMEWORK_POST_RUN_AUDIT=on_demand
AGENT_FRAMEWORK_POST_RUN_AUDIT_MAX_TOKENS=2048
```

回答後、`trace.agentFramework.caseId`を使って最後の適用済みDecisionを説明させる。

```bash
curl -s http://localhost:8000/answer/framework/audit \
  -H 'content-type: application/json' \
  -d '{"caseId":"legal-...","inquiry":"なぜ完了と判断したのですか"}' \
  | jq
```

途中の判断を確認する場合だけ、`appliedDecisionSequences`にある値を`decisionSequence`へ指定する。
監査APIは保存済みの対象SolverContext、適用済みDecision、前後状態を別のLLM呼出しへ渡す読み取り専用処理で、
CaseStore、最終回答、検索結果を更新しない。返るのは内部思考ではなく、記録済み事実と事後的推論を分離した説明である。
`status`では本文と完全なDecision材料を保存しないため監査APIは利用できない。既定`off`では追加API呼出しも
診断ファイルも発生しない。

Legal ProfileのGraph要求は1回1ホップである。SolverがHypothesisに対応するpredicate・方向・起点を
明示して`legal_graph_neighbors`を要求する。Graph候補Articleの本文取得後、その先が必要なら、Solverは
そのArticleを新しい起点にして次の1ホップを要求できる。Programは累積depthや発見元を理由に除外しない。
`fetch_articles`1回のArticle IDは物理上限4個、1 Cycleの本文取得成功数は
`AGENT_FRAMEWORK_MAX_FETCHED_RESOURCES_PER_CYCLE`（現行既定3件）で制限する。
Legal Profileの1 Solver Decisionは検索系Toolを最大4要求、`fetch_articles`を最大1要求とし、
合計上限は5要求である。本文取得量の4 Article上限とは別の制約である。
Graph Reviewから1 stepで選ぶ候補は最大3件とし、残りの関連候補はdeferして後続stepまたは次Cycleへ残す。
プログラムは超過分を選別せず契約違反として返す。
`modelCalls[].purpose`は`research / integration / cycle_close / finalization /
reviewer_revision / search_selection / graph_selection`のいずれかになる。`cycle_close_required=true`では`cycle_close`、
`finalize_only=true`では`finalization`、Reviewer差戻し時は`reviewer_revision`を使う。新しい1ホップ候補が
投影された直後は、同一Solverを本文を再掲しない短い専用Promptの`graph_selection`モードで呼ぶ。これは任意の
Reviewer Agentとは別の処理モードである。Solverは`graph_review_batch`の全候補をselect/defer/rejectし、
最大3件かつCycle残り枠内の選択Articleを同じ順序の1件の`fetch_articles`で`continue`する。
プログラムはReviewの存在、既知ID、件数、Requestとの一致だけを
検証し、候補の関連性を判断しない。Solverが
`finalize`する際は、全WorkItemを明示的に`resolved`または`dropped`へ閉じる必要がある。
プログラムは未終了IDの有無だけを検証し、法的な十分性や終了理由はSolverが判断する。
SolverContextは、根拠・引用に使える正確なIDを`grounding_evidence_ids`、追加本文取得に使える
Article IDを`fetchable_article_ids`として別々に渡す。LLMは前者をHypothesis・citationへ、後者を
`fetch_articles`へ使う。`fetchable_article_ids`は本文取得済みという意味ではなく、検索等で発見済みの
本文取得可能な候補を表す。質問に関係する候補の本文が未取得なら、同じ検索の反復より本文取得を優先する。
`search_candidates`は各候補を発見した検索要求、発見元WorkItem・Hypothesis、検索抜粋Evidenceの既知参照を
Article単位にまとめる。発見元は来歴であり意味上の採用先を限定しない。Programは参照を対応付けるだけで、
候補の関連性と本文取得対象はSolverが判断する。Search Reviewは採用先を確定せず、選択Articleの発見元参照を
Programが本文取得要求の輸送用に転記する。全文取得後のIntegrationが意味上の採用先を判断する。
新しい検索候補が投影された直後は、同じSolverを`search_selection`用途で2段階に呼ぶ。第1段階は候補ごとに
検索抜粋をまとめた専用Viewから、全候補の条件・効果を自分の言葉で短く評価する。この一時評価はCaseStateへ
保存せず、診断snapshotと第2段階の入力だけに使う。第2段階は検索抜粋を再掲せず、短い評価一覧から本文取得候補を
選ぶ。Programは評価対象の全件性、既知ID、選択件数だけを検証し、選択外候補を機械的に保留して、選択IDを
1件の本文取得要求へ転記する。
同一WorkItem・Hypothesis・query・filterで成功済みの`legal_search`を完全一致で再要求した場合だけ、
構造契約違反として差し戻す。Programは検索語の意味的な類似や候補の関連性を判定しない。
プログラムはIDの完全一致だけを検証する。
Article本文等のEvidence本文は`AGENT_FRAMEWORK_MAX_MATERIAL_EVIDENCE_CHARS`（既定50,000文字）を
上限とする。全Graph Article・Link・判断履歴はCaseStoreに保持し、Solverへは新規・再採用・新Link差分の
`graph_review_batch`と、全評価済みfrontierの短い`graph_review_ledger`だけを載せる。同じGraph navigation情報は
`evidence_manifest`、`recent_tool_results.evidence_ids`、`navigation_evidence_ids`、`omitted_evidence_ids`へ
再掲載しない。CaseStateのEvidenceとToolResultは監査用の正本として削除しない。
差分batchは`AGENT_FRAMEWORK_MAX_GRAPH_CANDIDATES_PER_REVIEW_BATCH`（既定20）で機械的にpage分割し、
未提示候補を関連なしまたは不存在として扱わない。
差分batchの候補はArticle ID、法令名・見出し、起点、relation、取得状態を失わず、過去候補はledgerから消えない。
候補の法的関連性はSolverが判断する。Prompt全体が
`AGENT_FRAMEWORK_MAX_SOLVER_INPUT_CHARS`（既定240,000文字）を超える場合は、
候補を黙って削らず`context_capacity_exceeded`で停止する。
下位法令・委任先の未確認事項は、通常のWorkItem、Hypothesis、gapsで管理する。
`discover_source / assess_source / discover_target / fetch_target`という重複状態はLegal Profileで要求しない。
取得本文に質問へ関係する委任があれば、Solverは対応するHypothesisを`unresolved`、WorkItemを`open`の
まま追加調査する。プログラムは法的な要否を決めない。Graph候補自体は根拠にならず、関連するとSolverが
判断したArticleを`fetch_articles`で取得して初めて根拠候補にできる。
検索本文中の参照先条文を追加取得する場合も、対応Article IDが
`fetchable_article_ids`になければIDを組み立てず、法令名・条番号・確認事項で
`legal_search`を行う。SolverはDecisionを返す前に、`fetch_articles`の全IDを同一覧と完全一致で照合する。
`retain_evidence_ids`は全取得結果ではなく、Solverが次サイクルにも本文を必要と判断したEvidenceを
`max_retained_evidence`以内で選ぶ欄である。プログラムは超過分を自動選別しない。
SolverDecisionの`next`、focus、保持ID、answer、`dependency_decisions`はproviderのstructured-output schemaで直接受け取る。
複雑な`update`と`tool_requests`だけを個別のJSON文字列として輸送し、Adapterが復元する。
長い回答本文を二重エンコードせず、provider schemaも過大にしない。その後は同じSolverDecision契約で検証する。
SolverDecisionが未知ID、上限超過、未終了WorkItem等の構造契約に違反した場合、状態へ適用せず、
違反理由と直前Decisionを同じ用途のLLMへ返す。初回を含む最大3回でLLMが意味判断を保って自己修復し、
3回目も不正なら`protocol_error`で終了する。プログラムによるID補正や超過分の切捨ては行わない。
修復時にLLMへ渡す文面はPythonへ埋め込まず、次のMarkdownを正とする。

- `agent-api/app/agent_framework/prompts/solver_transport_repair.md`
- `agent-api/app/agent_framework/prompts/solver_contract_repair.md`

各`prompt-section`とPython側の適用registryはテストで完全一致を確認する。Prompt編集時はsection markerと
`${...}`形式のtemplate変数を変更せず、`agent-api/tests/test_prompt_assets.py`を実行する。
Legal Profileの全体上限は既定240秒である。通常探索・契約修復用の時間を使い切った場合も、予約済みの
最終化時間へ制御を戻し、未確認事項を限定回答として明示する。時間切れを法的完了へ読み替えない。

新FrameworkのLegal Domainは、法令検索の第1位Articleについて取得済み一致chunkを検索順位どおり
最大5000文字の範囲でまとめてSolverへ返し、他のArticle候補は代表chunkを最大400文字で返す。
同じArticle見出しが各項号へ付加した共通先頭文は1回だけ残して機械的に圧縮する。これはArticle内の委任・
例外を先頭chunkだけへ縮約して失うことを避ける機械的な検索投影であり、法的関連性は選別しない。
法令とガイドを同時検索した場合、法令候補は`top_k`件、ガイド候補は上位2件を返す。
並列検索のEvidenceは、リクエスト順による文脈上限の偏りを避けるため結果ごとにround-robinで提示する。
法令・ガイドを問わず
`legal_search`の結果は`evidenceRole=search_navigation`の発見候補であり、Hypothesisの確定根拠や
citationにはできない。Solverが関連すると判断した法令Articleは
`fetch_articles`で全項号を取得し、その結果だけを法令本文の根拠候補として評価する。
`fetch_articles`の実行時は、プログラムが同じArticle IDを`legal_graph_neighbors`へ機械的に転記し、
OpenSearchの本文取得とNeo4jの1ホップ取得を同じcycleで実行する。SolverはGraphを直接要求しない。
同じArticleのGraphは成功後に再取得せず、Graph候補のArticle本文をSolverが取得対象に選ぶと、
そのArticleが次の1ホップ起点になる。

`legal_graph_neighbors`は既知Articleに接続する入出力両方向の正式Graph関係と
RelationAssertionを候補として返す。Graph候補は条文取得用のナビゲーション情報であり、
そのまま引用根拠やHypothesisの確定根拠にはできない。Solverは関係種別と方向を質問に照らし、
関係し得る範囲の隣接Articleをすべて`fetch_articles`へまとめ、端点本文を読んで採用を判断する。
`REFERENCES`はciting→cited、`IMPLEMENTS`はparent→child、`APPLIED_BY`はapplied→applyingであり、
`outgoing`は起点がfrom側、`incoming`は起点がto側を表す。`formal_relation`と
`relation_assertion`を区別し、後者を確定関係として扱わない。項・号に付いた同一参照、逆向き派生関係、
RelationAssertionが同じ2つのArticleを結ぶ場合は、Articleペア1候補へ機械的に集約する。
これは候補枠の重複消費を防ぐ処理であり、どの候補が法的に関連するかは選別しない。

UIの自然言語例題12問は、文書・必要条文・回答要点の3段階で確認する。
採点用の条文ID・要点はAgent APIへ送信しない。

```bash
TOP_K=5 RUNS=1 CONCURRENCY=1 python3 scripts/check_example_questions.py
```

検索とLLMの揺らぎを見る場合は `RUNS=3` など複数回実行する。

この採点は登録済み12問の質問文との完全一致時だけ適用される。言い換え・追記を含む質問と
新規質問は採点対象外であり、検索・回答は実行されても合否は出ない。必須条文と回答要点は
例題ごとの手動定義で、回答要点の文字列照合は否定・例外・当てはめの正しさを保証しない。
したがって結果は開発用の回帰指標として扱い、未知質問への正答率として報告しないこと。
法令改正、投入データ、質問文を変更した場合は
[example_questions.py](agent-ui/example_questions.py) の採点基準も見直す。

サンプル評価を実行する。

```bash
docker compose --profile eval run --rm eval-runner
```

結果は `eval-results/eval-*.jsonl` に保存される。

最終系を少数問で確認する場合:

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_LIMIT=3 \
EVAL_PATTERN=pattern_4_deepsearch \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm --no-deps eval-runner
```

確認:

```bash
ls -la eval-results
tail -n 5 eval-results/*.jsonl
```

評価は `docs/samples/eval/*.sample.jsonl` を使う。lawqa_jp 本体データは同梱していない。

### lawqa_jp 全問評価

lawqa_jp 本体データは同梱しない。公開データをURLから読む場合は、以下のように `LAWQA_EVAL_URL` を指定する。

全問評価でRAG検索を意味あるものにするには、先に lawqa_jp の `references` に含まれる e-Gov 法令本文を投入する。

#### e-Gov XML原本データセット

OpenSearch / Neo4jを再構築するたびにe-Gov APIから取得し直さないよう、最初に
`law_registry.json`の対象法令をローカルの不変XML corpusへ同期する。

```bash
python3 scripts/sync_egov_law_corpus.py
```

既定の保存先は`datasets/lawqa_jp/egov_law_corpus/`である。

```text
egov_law_corpus/
├─ manifest.json
├─ manifests/
│  └─ egov-law-corpus-<dataset hash>.json
└─ documents/
   └─ <lawId>/
      └─ <XML sha256>.xml
```

XMLは内容ハッシュをファイル名として不変保存する。Manifestには法令ID、法令名、取得元URL、
取得日時、XML SHA-256、ファイルサイズ、本則・附則・Article件数、法令ファミリーを記録する。
同じコマンドの再実行では全ファイルのXML構造・タイトル・hashを検証し、正常ならe-Govへ
アクセスせず再利用する。e-Gov上の更新を明示的に確認するときだけ次を実行する。

```bash
python3 scripts/sync_egov_law_corpus.py --refresh
```

変更されたXMLは旧ファイルを上書きせず、新しい内容ハッシュとdataset snapshotとして保存する。
このディレクトリは公開データのローカル原本でサイズが大きいためGit管理外である。
`/admin/seed`は`EGOV_LAW_CORPUS_MANIFEST`（既定は上記`manifest.json`）を入力にし、
各XMLのSHA-256、法令ID、法令名を検証してからOpenSearch / Neo4jを同時に再構築する。
seed中にe-Gov APIへアクセスしない。e-Gov上の更新を取り込む場合も、先に`--refresh`で
新しい不変snapshotを作り、そのmanifestを指定してから一度だけ再seedする。

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
SEED_LAWQA_EGOV=true \
AGENT_USE_BM25=true \
AGENT_USE_VECTOR=true \
docker compose up --build -d agent-api

LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
SEED_LAWQA_EGOV=true \
curl -s -X POST http://localhost:8000/admin/seed | jq .
```

別snapshotを使う場合は、compose起動時にホスト側パスではなくコンテナ内のmanifestパスを指定する。

```bash
EGOV_LAW_CORPUS_MANIFEST=/workspace/datasets/lawqa_jp/egov_law_corpus/manifests/egov-law-corpus-<hash>.json \
SEED_LAWQA_EGOV=true \
docker compose up --build -d agent-api
```

### 非e-Govガイドラインの原本保管と投入

lawqa_jp の `selection.json` が参照する非e-Gov資料は、金融庁・厚生労働省・国土交通省のPDF 6件で、20問の根拠に使われる。原本は評価用の問題・正解・コンテキストとは別に保管し、必要なときだけ検索コーパスへ投入する。

```bash
scripts/download_lawqa_guidance.sh
```

この操作は `datasets/lawqa_jp/external-guidance/` にPDFと、取得元URL・SHA-256を記録した `manifest.json` を作る。金融庁の旧URL `250221_kaiji.pdf` は現時点で提供終了のため、lawqa_jp参照時点のWeb Archiveスナップショットを取得し、マニフェストにその事実を記録する。

#### docling前処理(任意だが推奨)

投入前にdoclingでPDFを構造分解(見出し/段落/表)しておくと、表が1表=1チャンクの
Markdownとして投入され、表中の「法第N条」自己参照が `relatedArticleContentUnitIds` に
記録される(検索時の `guidance_explains_lookup` が発火しやすくなる)。
トリガーは手動のみ(このリポジトリでは自動発火の仕組みは持たない):

```bash
docker compose --profile preprocess build preprocess-worker  # seed.py側と同様、コード変更後は必須
docker compose --profile preprocess run --rm preprocess-worker \
  python -m app.cli --sync-local
```

`seed.py` 側で `_load_guidance_artifact` 等を変更した場合も、`agent-api` を
`docker compose up --build -d agent-api` で再ビルドしないと反映されない(上記の
`--build` に関する注意と同じ)。派生JSONを正しく生成しても、agent-apiが古い
イメージのままだと黙って従来のpypdf経路にフォールバックし続けるので注意。

これは `datasets/.../documents/*.pdf` をMinIOのrawゾーン
(`source-documents/external-guidance/`)へアップロードし、変換結果JSONを派生ゾーン
(`derived-artifacts/preprocessed/external-guidance/`)へ書き込む。seedは派生JSONが
あればそれを優先し、無ければ従来のpypdfページ分割にフォールバックする。派生JSONの
`sourceSha256` がマニフェストのSHA-256と一致しない場合、seedは古い成果物での投入を
防ぐためエラーで停止する(前処理を再実行すること)。

- 1件だけ処理する場合: `python -m app.cli --sync-local --only mhlw-000761110`
- doclingはPyTorch依存でイメージが大きいため、profile `preprocess` 指定時のみビルドされる。
- 大きいPDF(監督指針479ページ等)はCPU変換に時間がかかる。

AWS移行時の対応: 手動CLIはS3イベント同形のdictを組み立てて
`preprocess-worker/app/handler.py` の `handle_s3_event()` を呼んでいる。移行後は
S3 Event Notification → Lambda(コンテナイメージ)/ECS から同じ関数を呼ぶだけで、
変換ロジック・イベント解析・成果物スキーマは無変更で引き継げる。boto3の接続先も
`AWS_ENDPOINT_URL` 環境変数を外せば本物のS3に切り替わる。

次の指定で、原本をMinIOに保管し、PDF本文をOpenSearchに投入する(docling前処理済みなら構造チャンク、未処理ならページ単位)。ガイドラインは法令の委任・準用Graphには入れない。

```bash
SEED_LAWQA_EGOV=true \
SEED_EXTERNAL_GUIDANCE=true \
curl -s -X POST http://localhost:8000/admin/seed | jq .
```

`externalGuidanceDocuments` が0より大きければ投入された。既定は `false` のため、法令だけの評価結果とガイドライン込みの評価結果は分けて記録する。

`LAWQA_EGOV_LAW_IDS` を指定すると、lawqa_jp から自動抽出せず、指定したe-Gov法令IDだけを投入できる。
未指定の場合は `docs/samples/eval/law_registry.json` を使用する。法令ID、評価用の法令ファミリー、部分投入範囲はこのファイルで一元管理する。

```bash
LAWQA_EGOV_LAW_IDS=323AC0000000025,340CO0000000321 \
SEED_LAWQA_EGOV=true \
docker compose up --build -d agent-api
```

まず少数件で疎通確認する。

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_LIMIT=10 \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

140問すべてを実行する。

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

評価結果には `metricVersion` が含まれる。version 6では従来のany-hitに加えて、
期待条文の完全到達率（`*ArticleCompleteHit`）と条文再現率（`*ArticleRecall`）を段階別に記録する。
集計では問題単位のマクロ平均に加え、全期待条文を分母にする
`*ArticleMicroRecall` も確認できる。
論点被覆型選抜をShadow modeで実行した場合は、
`shadowRerankerArticleCompleteHit/Recall` とそのマクロ・ミクロ集計も記録する。
Shadow選抜が時間切れ・再ランカー障害で完了しなかった問題は比較指標から除き、
`shadowSelectionIncomplete` に件数を出す。
法令レイヤー別探索を有効にした実行では、`layeredContextArticleCompleteHit/Recall`、
`primaryConclusionGroupCompleteRate`、`mandatoryConclusionGroupCompleteRate`、
`layeredAnswerStatusCounts`、旧・新コンテキスト別の`contextTruncation`も記録する。
また、`lawqa_known_issues.json` の既知問題を除く診断正答率を公式正答率とは別に出す。
version 5以前とは評価項目が異なるため直接比較しない。

最終回答では、RRF融合上位をClaudeへ渡し、各選択肢を `entailed`、`contradicted`、
`insufficient` のいずれかで独立判定する。Claudeは最終ラベルを出力せず、
`questionPolarity`、各判定の `verdict` と `confidence` からコードが `predictedAnswer` を決定する。
判定に使用した `citationIds` を優先して最終引用（既定5件）を構成する。

`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`とsnapshot情報はseed時に生成するため、この変更を
既存環境へ反映するにはagent-apiをbuild後、OpenSearchとNeo4jを同じ`/admin/seed`で再構築する。
Neo4jだけを更新しない。Graph schema version 9では、意味関係は後続のpublish済み
`ClassificationRun`から取得し、正式なArticle間Relationとしては保存しない。
Graph展開は上位ノードごとに少数ずつ取得した後、質問との語句被覆と接続先法令の多様性で
経路を選び、一つの条文や一つの法令の参照先だけで `AGENT_MAX_GRAPH_PATHS` を
使い切らないようにする。
このうち明示的`REFERENCES`、信頼済み`IMPLEMENTS`、派生元を持つ`APPLIED_BY`の対象だけは、
接続先法令を分散させながら最大4 Articleを再ランカー入力に保持する。同一Articleの
複数項・号は最も質問に近い1チャンクだけを強制対象とし、他のチャンクは候補プールから
削除しない。
再ランカー上位に既に入った必須候補は並べ替えず、上位外の必須候補だけを非必須の末尾と
交換する。通常のGraph候補の救済は再ランカー上限から4件下までとし、分解クエリの代表を
起点に見つかったGraph候補はこの距離制限を設けない。ただし、明示条番号・分解論点・Graph
を合わせた再ランカー上位外からの救済は合計2件までであり、低順位候補による上位枠の
置き換えを限定する。

複数論点の自然言語質問では、質問全文だけによる再ランクで短い論点の条文が落ちないよう、
プランナーが生成した分解クエリごとに検索20位以内をローカル再ランカーで再評価し、
各クエリ最大3件・重複Articleを除く代表を全体で最大12件、再ランカー入力へ保持する。
代表は1クエリが枠を独占しないようラウンドロビンで選ぶ。この情報はgoldではなく、
実行時の検索結果とローカル再ランカーだけから決める。`AGENT_MAX_QUERIES=5`は質問全文1件と
分解クエリ最大4件の合計であり、プランナーへも分解クエリの残り枠を明示する。
分解クエリ代表と資料ごとの代表を合わせても再ランカー入力の半分までとし、残り半分は
融合スコア順の候補を維持する。証拠評価器が追加した検索語についても、条番号が明示されて
いれば条・項・号を直接取得し、同検索語内のローカル再ランク代表を最大2件保持する。

再ランカー入力30件から回答コンテキスト16件を選ぶ論点被覆型方式は
[legal_issue_coverage_retrieval.md](docs/legal_issue_coverage_retrieval.md)
に従う。既定では現行16件を回答へ使いながら新16件も計算するShadow modeである。

```text
AGENT_ISSUE_COVERAGE_SELECTION=false
AGENT_ISSUE_COVERAGE_SHADOW=true
AGENT_ANSWER_RESERVE_SEC=60
```

新方式を回答へ反映する場合は `AGENT_ISSUE_COVERAGE_SELECTION=true` にしてagent-apiを
再buildする。共通保護枠は16件中最大8chunks、明示条文はそのうち最大4chunksで、
少なくとも8chunksは質問全文の再ランク順から採用する。Graph候補は検索順位を偽装せず、
接続元の論点を別状態で継承して30件確定後に再評価する。

後段論点再ランクは回答生成用の予約時間を侵食しない。`AGENT_ANSWER_RESERVE_SEC` は
`LLM_TIMEOUT_SEC`とは独立した最低予約で、論点フェーズ全体の上限は
`RERANK_TIMEOUT_SEC`である。Shadow 140問の前に次の20問確認を行う。

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection.json \
EVAL_LIMIT=20 \
EVAL_SKIP_SEED=true \
REQUEST_TIMEOUT_SEC=360 \
AGENT_ANSWER_RESERVE_SEC=60 \
docker compose --profile eval run --rm eval-runner
```

結果の `shadowSelectionComplete/Incomplete`、`aspectPhaseBudgetMs`、
`aspectPhaseElapsedMs`、`skippedAspectQueries`、`request_failed` を確認し、
後段フェーズが恒常的にスキップされる場合は140問へ進まない。

同じ検索語を法令・ガイドラインなど資料種別を変えて検索する場合、Agent APIプロセス内で
クエリ埋め込みと未絞り込みKNN結果をLRUキャッシュする。資料種別、公開状態、最新版、
clearanceの絞り込みはキャッシュ取得後にも適用するため、検索対象の境界は変えずに
Ollama埋め込みとOpenSearch KNNの重複実行だけを避ける。

最終引用でGraph経路を最大1件確保するのは、引用上限を厳守する選択式だけである。
自由入力では回答本文が参照したIDを上限外でも回収できるため、Graph候補を強制引用しない。
選択式でも、Graph pinによる救済前から再ランカー上位にあった候補だけを対象にする。
選択は経路の起点条文が再ランク後にどれだけ質問へ
関連しているかを優先し、同順位の場合だけ`IMPLEMENTS`、親法`REFERENCES`、
`APPLIED_BY`の順にする。
これはLLMが指定した直接根拠を複数押し出さず、法令間の接続根拠も利用者が確認できるように
するためである。一般のGraph候補やガイドラインは固定しない。

選択肢順を変えた560件で評価する場合:

```bash
LAWQA_EVAL_URL=https://raw.githubusercontent.com/digital-go-jp/lawqa_jp/main/data/selection_randomized.json \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

ローカルに clone / download したデータを使う場合:

```bash
mkdir -p datasets
git clone https://github.com/digital-go-jp/lawqa_jp.git datasets/lawqa_jp

LAWQA_EVAL_PATH=/workspace/datasets/lawqa_jp/data/selection.json \
EVAL_SKIP_SEED=true \
docker compose --profile eval run --rm eval-runner
```

注意:

- `コンテキスト` と `output` は Agent API には送らない。
- `output` と `references` は eval-runner の答え合わせだけに使う。
- lawqa_jp はゴールデンセット（正解＋必要な条文を持つ）だが、それらは採点専用で、
  システムには問題文と選択肢しか渡さない。gold条文の生成（コンテキスト見出しから
  条・項・号の contentUnitId を復元）と各指標の照合ロジックの詳細は
  [evaluation_design.md](docs/evaluation_design.md) 2.1 を参照。
- 現行の既定検索は BM25 + bge-m3 vector の Hybrid 検索。
- `SEED_LAWQA_EGOV=true` を使わない場合、RAGコーパスはサンプル1文書のみなので、全問スコアは検索基盤の完成度ではなく、現在投入済み文書に強く依存する。
- e-Gov以外のPDF等を参照する問題は、現行の自動投入対象外。`citationHit` は e-Gov 法令ID単位の部分一致も見る。
- `predictedAnswer` が空で不正解になった場合は、検索失敗と即断せず
  `trace.llm.validationError` とAgent APIログを確認する。接続切断・タイムアウト等で
  回答生成だけが失敗した試行は、候補・再ランカー・最終引用の段階別指標と分けて扱う。

## 7. ログ確認

```bash
docker compose logs -f agent-api
docker compose logs -f agent-ui
docker compose logs -f opensearch
docker compose logs -f neo4j
```

## 8. 停止

```bash
docker compose down
```

volume も削除して初期化する場合:

```bash
docker compose down -v
```

## 9. LLMモデル固有の癖

lawqa_jp 選択式での動作確認で見つかった、モデルごとの挙動差。モデルを切り替える際は再確認すること。

| 項目 | Ollama gemma4:e4b | Anthropic claude-haiku-4-5-20251001 | Anthropic claude-sonnet-5 |
|---|---|---|---|
| `temperature` パラメータ | 必須(0で決定的に) | 受理される | **拒否される(400: deprecated)**。Anthropic呼び出しでは送らない |
| JSON出力の形式 | 素のJSON | **Markdownコードフェンス(` ```json `)で包む**ことがある。剥がす処理が必要 | 素のJSON |
| スキーマ外フィールド | 出さない | `reasoning` 等の**追加フィールドを付与**することがある。pydanticは`extra="ignore"`にする | 同様の傾向あり |
| 拡張思考(thinking) | 非対応 | 非対応 | **`thinking`ブロックが`max_tokens`予算を消費**。`max_tokens`不足だと本文(`text`ブロック)が出力される前に打ち切られ、空応答になる。`ANTHROPIC_MAX_TOKENS`は16384を確保する(既定値も16384に変更済み) |

`ANTHROPIC_MAX_TOKENS` を8192から16384へ引き上げた根拠(2026-07-25 実測、lawqa_jp 金商法_第3章_問題番号51):

| | contentBlockTypes | 出力トークン | 本文の文字数 | stopReason |
|---|---|---|---|---|
| 成功時 | `thinking`, `text` | 6,459 | 1,565 | `end_turn` |
| 失敗時 | **`thinking` のみ** | 8,192(上限到達) | **0** | **`max_tokens`** |

失敗時は入力17,314トークン、成功時は9,146トークンで、引用候補を多く渡した回ほど思考が伸びて上限に達する。同じ設問が実行のたびに成否を変えるのはこのため。8192でも足りない場合があるため16384を既定とし、あわせて [llm.py](agent-api/app/llm.py) の `generate_answer()` に次の2点を実装している。

- `stopReason=max_tokens` で失敗した再試行は、同じ枠ではなく**枠を倍**(上限 `ANTHROPIC_MAX_TOKENS_CEILING`)にして投げ直す。同じ枠での再試行は同じ空応答になり、時間とトークンを二重に捨てるため。
- 再試行が例外(タイムアウト等)で落ちても、**1回目の結果を破棄しない**。従来は例外が伝播して1回目の判定ごと失われ、`llmUsed=false`として不正解扱いになっていた。

### タイムアウト設定はトークン枠とセットで調整する

`ANTHROPIC_MAX_TOKENS` だけを上げると、次は**時間**が制約になる。枠を広げた分だけモデルが長く生成するためで、上記設問では成功時の出力が9,971トークンに達し、1回の呼び出しが120秒を超えて `read timeout=120` で落ちた。トークン枠を上げるときは時間も併せて引き上げる。

| 設定 | 変更前 | 変更後 | 理由 |
|---|---|---|---|
| `ANTHROPIC_MAX_TOKENS` | 8192 | 16384 | 思考込みで本文まで到達させる |
| `LLM_TIMEOUT_SEC` | 120 | 180 | 1万トークン規模の生成が120秒に収まらない |
| `AGENT_MAX_WALL_TIME_SEC` | 200 | 280 | 検索約30〜40秒 + 回答生成180秒を収める(コード側の上限は300) |
| `REQUEST_TIMEOUT_SEC` | 240 | 360 | 評価クライアントがサーバの全体予算より先に諦めないようにする |

`REQUEST_TIMEOUT_SEC` は評価クライアント側([run_eval.py](eval-runner/run_eval.py))の待ち時間で、既定値は150秒。eval-runnerは起動時に `/health` の `timeBudget.agentMaxWallTimeSec` と突き合わせ、`REQUEST_TIMEOUT_SEC <= wall time + REQUEST_TIMEOUT_SAFETY_MARGIN_SEC`(既定10秒)の場合は評価を開始せず設定エラーにする。サーバの全体予算より短いと、**時間はかかったが正しく返ってきた回答をクライアントが捨てる**ため、必ず全体予算より長くする。

`predictedAnswer=null` での棄権について: 当初 claude-sonnet-5 は根拠が弱い問題で棄権する割合が高く見えたが、原因はモデル固有の性質ではなく [llm.py](agent-api/app/llm.py) の `build_answer_prompt()` が「判断できない場合は null にしてください」と明示的に指示していたため。選択肢がある場合は必ずいずれかを選ぶよう指示を変更した結果、claude-sonnet-5 で20問中20問がLLM使用、うち20問中18問正解(90%)まで改善した(小サンプルにつき参考値)。根拠が薄い場合でも `answer` テキスト内でその旨と専門家確認の必要性を明記する指示は維持している。

共通の注意点:

- `agent-api` のコードは Dockerfile で `COPY` されるため、`.env` やコードを変更した後は `docker compose up -d` ではなく `docker compose up --build -d` を使う。`--build` を忘れると古いイメージのまま起動し、変更が反映されない。
- LLM応答が空文字列の場合でも `trace.llm` に診断情報(`validationError`等)が残るようにしている([agent.py](agent-api/app/agent.py)の`_compose_answer`)。空応答時に原因不明で「LLM未使用」と表示される場合は、この診断ロジックが退行していないか確認する。
- モデルIDの利用可否はAPIキーの契約プランに依存する。切り替え前に2節のcurlコマンドで動作確認する。

## 10. 実データへ差し替える箇所

Phase 0 で以下を固定する。

- embedding model / dimension。既定は Ollama `bge-m3` / 1024次元
- lawqa_jp 評価対象件数と除外条件
- planner / answer / judge LLM。初期確認は Ollama `gemma4:e4b`。Claudeは
  `LLM_PROVIDER=anthropic`、OpenAI APIは`LLM_PROVIDER=openai`を使う
- Hybrid 検索重み
- Pattern 3 の tool call / retry 上限

実データ投入時の主な差し替え箇所:

- `agent-api/app/embeddings.py`: embedding provider を変更する場合に差し替え
- `agent-api/app/seed.py`: サンプル文書生成から e-Gov 前処理済み文書投入へ変更
- `.env`: `LLM_PROVIDER`, `LLM_MODEL`, `ANSWER_MODEL`, `REVIEWER_MODEL`,
  `LLM_RESEARCH_STAGE_MODEL`, `LLM_RESEARCH_INTEGRATION_MODEL`, provider別APIキーを変更
- `agent-api/app/agent.py`: planner / answer / judge の使い分けを拡張
- `docs/samples/eval/`: 実評価分割の JSONL に差し替え

法的判断はシステム出力だけで断定せず、必要に応じて専門家確認を行う。
