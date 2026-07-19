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
Claude を使う場合は `LLM_PROVIDER=anthropic` に切り替える（選択式 lawqa_jp の判定精度は Ollama の小型モデルより大きく高い。動作確認例は2節参照）。

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
PLANNER_MODEL=claude-haiku-4-5-20251001
PLANNER_MAX_TOKENS=1024
PLANNER_TIMEOUT_SEC=30
AGENT_USE_LLM_PLANNER=true
AGENT_MAX_QUERIES=4
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

利用可能なモデルIDはAPIキーの契約プランに依存するため、`ANSWER_MODEL` を決める前に直接確認する。

```bash
curl -s https://api.anthropic.com/v1/messages \
  -H 'content-type: application/json' \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":10,"messages":[{"role":"user","content":"test"}]}' \
  | jq .
```

`404 model not found` の場合は、そのキーではモデルIDが使えない。契約プランで利用可能な別のモデルIDに変える。

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

```bash
curl -s -X POST http://localhost:8000/admin/seed | jq .
```

投入内容:

- OpenSearch index: `legal-rag-content`
- Graph nodes/edges: `docs/requirements/samples/metadata/*.jsonl`
- e-Gov法令投入時のGraph edges: `HAS_CONTENT_UNIT` と、同一法令内の明示的な条文参照から生成した `REFERENCES`
- e-Gov法令は本則・附則を分けて投入する。本則は `law-<法令番号>-article-<条番号>`、附則は `law-<法令番号>-suppl-<index>-article-<条番号>`（条番号の衝突で本則が消えるのを防ぐ）。各文書に `provisionType` / `sectionKey` を付与。詳細は [id_naming_rules.md](docs/requirements/docs/id_naming_rules.md) 3.1
- MinIO bucket: `knowledge-root`
- サンプル評価データ: `knowledge-root/eval-data/samples/...`
- 原本保管用マニュアル: `knowledge-root/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md`

原本保管用マニュアルは、OpenSearch / Neo4j / 評価データには投入しない。

`/admin/seed` は OpenSearch index と Neo4j graph を作り直す。検証環境の再投入用であり、本番運用向けの差分投入ではない。

### seed中の挙動（ハングではない）

`SEED_LAWQA_EGOV=true` 込みの seed は、e-Gov法令の取得と**全チャンク（約1.6万件）の埋め込み
生成をメモリ上で完了してから** OpenSearch へ一括投入する。そのため投入完了までの数分〜
十数分間、`GET /legal-rag-content/_count` は **0 のまま**になる。これはハングではない。

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

## 5. UI 操作

1. `http://localhost:8501` を開く。
2. Sidebar の `Health check` を押す。
3. 未投入なら `Seed sample data` を押す。
4. Pattern を選び、質問を入力して `Ask` を押す。

UI では回答、検索ルート、citations、Graph paths、trace を確認できる。

## 6. 評価実行

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

評価は `docs/requirements/samples/eval/*.sample.jsonl` を使う。lawqa_jp 本体データは同梱していない。

### lawqa_jp 全問評価

lawqa_jp 本体データは同梱しない。公開データをURLから読む場合は、以下のように `LAWQA_EVAL_URL` を指定する。

全問評価でRAG検索を意味あるものにするには、先に lawqa_jp の `references` に含まれる e-Gov 法令本文を投入する。

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
未指定の場合は `docs/requirements/samples/eval/law_registry.json` を使用する。法令ID、評価用の法令ファミリー、部分投入範囲はこのファイルで一元管理する。

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

評価結果には `metricVersion` が含まれる。version 2では採点不能な参照問題をcitation系指標の分母から除外し、`candidatePoolHit`、`fusionHit`、`citationHit`を段階別に記録するため、version 1のcitation系数値とは直接比較しない。

最終回答では、RRF融合上位（既定10件）をClaudeへ渡し、各選択肢を `entailed`、`contradicted`、`insufficient` のいずれかで独立判定する。設問が正しい記述を求めるか、誤った記述を求めるかは `questionPolarity` として分離し、判定に使用した `citationIds` を優先して最終引用（既定5件）を構成する。評価結果の `choiceAssessments` で判定理由と根拠IDを確認できる。

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
  [evaluation_design.md](docs/requirements/docs/evaluation_design.md) 2.1 を参照。
- 現行の既定検索は BM25 + bge-m3 vector の Hybrid 検索。
- `SEED_LAWQA_EGOV=true` を使わない場合、RAGコーパスはサンプル1文書のみなので、全問スコアは検索基盤の完成度ではなく、現在投入済み文書に強く依存する。
- e-Gov以外のPDF等を参照する問題は、現行の自動投入対象外。`citationHit` は e-Gov 法令ID単位の部分一致も見る。

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
| 拡張思考(thinking) | 非対応 | 非対応 | **`thinking`ブロックが`max_tokens`予算を消費**。`max_tokens`不足だと本文(`text`ブロック)が出力される前に打ち切られ、空応答になる。`ANTHROPIC_MAX_TOKENS`は最低4096を確保する(既定値も4096に変更済み) |

`predictedAnswer=null` での棄権について: 当初 claude-sonnet-5 は根拠が弱い問題で棄権する割合が高く見えたが、原因はモデル固有の性質ではなく [llm.py](agent-api/app/llm.py) の `build_answer_prompt()` が「判断できない場合は null にしてください」と明示的に指示していたため。選択肢がある場合は必ずいずれかを選ぶよう指示を変更した結果、claude-sonnet-5 で20問中20問がLLM使用、うち20問中18問正解(90%)まで改善した(小サンプルにつき参考値)。根拠が薄い場合でも `answer` テキスト内でその旨と専門家確認の必要性を明記する指示は維持している。

共通の注意点:

- `agent-api` のコードは Dockerfile で `COPY` されるため、`.env` やコードを変更した後は `docker compose up -d` ではなく `docker compose up --build -d` を使う。`--build` を忘れると古いイメージのまま起動し、変更が反映されない。
- LLM応答が空文字列の場合でも `trace.llm` に診断情報(`validationError`等)が残るようにしている([agent.py](agent-api/app/agent.py)の`_compose_answer`)。空応答時に原因不明で「LLM未使用」と表示される場合は、この診断ロジックが退行していないか確認する。
- モデルIDの利用可否はAPIキーの契約プランに依存する。切り替え前に2節のcurlコマンドで動作確認する。

## 10. 実データへ差し替える箇所

Phase 0 で以下を固定する。

- embedding model / dimension。既定は Ollama `bge-m3` / 1024次元
- lawqa_jp 評価対象件数と除外条件
- planner / answer / judge LLM。初期確認は Ollama `gemma4:e4b`、Claude利用時は `LLM_PROVIDER=anthropic`
- Hybrid 検索重み
- Pattern 3 の tool call / retry 上限

実データ投入時の主な差し替え箇所:

- `agent-api/app/embeddings.py`: embedding provider を変更する場合に差し替え
- `agent-api/app/seed.py`: サンプル文書生成から e-Gov 前処理済み文書投入へ変更
- `.env`: `LLM_PROVIDER`, `ANSWER_MODEL`, provider別APIキーを変更
- `agent-api/app/agent.py`: planner / answer / judge の使い分けを拡張
- `docs/requirements/samples/eval/`: 実評価分割の JSONL に差し替え

法的判断はシステム出力だけで断定せず、必要に応じて専門家確認を行う。
