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
ANTHROPIC_MAX_TOKENS=1024
ANSWER_MODEL=claude-haiku-4-5-20251001
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

`/health` の `llm.ok` が `true` であれば、Agent API コンテナから設定した LLM provider を利用できる。

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
- MinIO bucket: `knowledge-root`
- サンプル評価データ: `knowledge-root/eval-data/samples/...`
- 原本保管用マニュアル: `knowledge-root/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md`

原本保管用マニュアルは、OpenSearch / Neo4j / 評価データには投入しない。

`/admin/seed` は OpenSearch index と Neo4j graph を作り直す。検証環境の再投入用であり、本番運用向けの差分投入ではない。

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
    "maxDepth": 1
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
    "userClearanceLevel": 2
  }' | jq .
```

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

`LAWQA_EGOV_LAW_IDS` を指定すると、lawqa_jp から自動抽出せず、指定したe-Gov法令IDだけを投入できる。

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

## 9. 実データへ差し替える箇所

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
