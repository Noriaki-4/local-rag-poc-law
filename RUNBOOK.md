# RUNBOOK

## 1. 概要

このリポジトリは、Step1 ローカル Agentic RAG / DeepSearch POC を Docker 上で起動する。

構成:

- MinIO: 原本・処理済み成果物・評価データ置き場
- OpenSearch: 法令・マニュアル本文の BM25 / kNN / Hybrid 検索
- OpenSearch Dashboards: インデックス確認
- Neo4j: GraphRAG 検証
- Agent API: FastAPI
- Agent UI: Streamlit
- Eval Runner: サンプル評価実行

実装はサンプルデータで起動確認できる。e-Gov 由来の実法令データ、lawqa_jp 本体、実 embedding model は Phase 0 で固定後に差し替える。

初期 LLM は有料 API ではなく、ホストで動く Ollama の `gemma4:e4b` を使う。Docker 内の Agent API からは `http://host.docker.internal:11434` に接続する。

## 2. 初回起動

ホスト側で Ollama と `gemma4:e4b` が使えることを確認する。

```bash
ollama list
curl -s http://localhost:11434/api/generate \
  -H 'content-type: application/json' \
  -d '{"model":"gemma4:e4b","prompt":"日本語で一文だけ返してください。","stream":false}' | jq .
```

Ollama 設定は `docker-compose.yml` の `agent-api.environment` に既定値を入れている。必要に応じて `.env.example` を参考に `.env` を作る。

```bash
docker compose up --build -d
```

起動確認:

```bash
docker compose ps
curl -s http://localhost:8000/health | jq .
```

`/health` の `llm.ok` が `true` であれば、Agent API コンテナから Ollama の `gemma4:e4b` に接続できている。

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

`/admin/seed` は OpenSearch index と Neo4j graph を作り直す。検証環境の再投入用であり、本番運用向けの差分投入ではない。

## 4. API 動作確認

Hybrid 検索:

```bash
curl -s http://localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{
    "query": "条例案が議会で可決された後の公布施行手続",
    "docType": "manual",
    "topK": 5,
    "userClearanceLevel": 2
  }' | jq .
```

Graph 検索:

```bash
curl -s http://localhost:8000/graph/path \
  -H 'content-type: application/json' \
  -d '{
    "fromGraphNodeId": "manual-ordinance-001-step-008",
    "edgeType": "BASED_ON_LAW",
    "maxDepth": 1
  }' | jq .
```

条例 manual to law 回答:

```bash
curl -s http://localhost:8000/answer \
  -H 'content-type: application/json' \
  -d '{
    "question": "条例案が議会で可決された後、担当課は何をすべきか。根拠条文も示して。",
    "pattern": "pattern_3_controlled_agentic_rag",
    "userClearanceLevel": 2
  }' | jq .
```

lawqa_jp 形式:

```bash
curl -s http://localhost:8000/answer \
  -H 'content-type: application/json' \
  -d '{
    "question": "次の記述のうち、法令上正しいものはどれか。",
    "choices": {
      "A": "選択肢A",
      "B": "選択肢B",
      "C": "選択肢C",
      "D": "選択肢D"
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

- embedding model / dimension
- lawqa_jp 評価対象件数と除外条件
- planner / answer / judge LLM。初期確認は Ollama `gemma4:e4b`、judge は `none`
- Hybrid 検索重み
- Pattern 3 の tool call / retry 上限

実データ投入時の主な差し替え箇所:

- `agent-api/app/embeddings.py`: 決定的ローカル embedding から実 embedding provider へ変更
- `agent-api/app/seed.py`: サンプル文書生成から e-Gov 前処理済み文書投入へ変更
- `agent-api/app/llm.py`: 初期 Ollama provider から必要な LLM provider へ変更
- `agent-api/app/agent.py`: planner / answer / judge の使い分けを拡張
- `docs/requirements/samples/eval/`: 実評価分割の JSONL に差し替え

法的判断はシステム出力だけで断定せず、必要に応じて専門家確認を行う。
