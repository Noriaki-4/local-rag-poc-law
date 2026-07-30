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

seed時は、OpenSearchへ今回投入する`processedObjectUri`を正として
`derived-artifacts/vector-documents/`を同期する。過去のseedで残った未参照vector文書は削除するが、
`source-documents/`、`eval-data/`、`derived-artifacts/preprocessed/`は削除しない。

```bash
curl -s -X POST http://localhost:8000/admin/seed | jq .
```

投入内容:

- OpenSearch index: `legal-rag-content`
- Graph nodes/edges: `docs/requirements/samples/metadata/*.jsonl`
- e-Gov法令投入時のGraph edges: `HAS_CONTENT_UNIT`、同一法令内の明示的な条文参照から
  生成した `REFERENCES`、下位法令の「法第N条」参照を親法律へ、府省令等の
  「令第N条」参照を同じ法令系統の施行令へ結ぶ `REFERENCES` と、その逆向きに
  親法律・施行令側から具体化条文を引く `IMPLEMENTS`、
  準用先の規定から準用元を逆引きする `APPLIED_BY`
- e-Gov法令は本則・附則を分けて投入する。本則は `law-<法令番号>-article-<条番号>`、附則は `law-<法令番号>-suppl-<index>-article-<条番号>`（条番号の衝突で本則が消えるのを防ぐ）。各文書に `provisionType` / `sectionKey` を付与。詳細は [id_naming_rules.md](docs/requirements/docs/id_naming_rules.md) 3.1
- MinIO bucket: `knowledge-root`
- サンプル評価データ: `knowledge-root/eval-data/samples/...`
- 原本保管用マニュアル: `knowledge-root/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md`

原本保管用マニュアルは、OpenSearch / Neo4j / 評価データには投入しない。

`/admin/seed` は OpenSearch index と Neo4j graph を作り直す。検証環境の再投入用であり、本番運用向けの差分投入ではない。

### 日本語Analyzerの比較索引

既存`legal-rag-content`を削除せず、Kuromoji＋NFKCとbigramのmulti-fieldを持つ
`legal-rag-content-ja-v2`を作る。Analyzer pluginを追加するため、最初にOpenSearchをbuildする。

```bash
docker compose build opensearch
docker compose up -d opensearch
python3 scripts/create_japanese_search_index.py
```

再索引は既存のembeddingをコピーするため、全件の埋め込みを再生成しない。target索引が既に
存在する場合、スクリプトは削除・上書きせず終了する。作り直す場合は対象名と用途を確認してから
明示的に別名を指定する。

比較索引をAgent APIで使う場合:

```bash
OPENSEARCH_INDEX=legal-rag-content-ja-v2 \
OPENSEARCH_INDEX_MAPPING=metadata/opensearch_index_mapping.japanese.sample.json \
docker compose up -d --build agent-api
```

元へ戻す場合:

```bash
OPENSEARCH_INDEX=legal-rag-content \
OPENSEARCH_INDEX_MAPPING=metadata/opensearch_index_mapping.sample.json \
docker compose up -d agent-api
```

この段階では検索スコア、候補上限、探索ループは変更しない。`heading`、`text`、
`sectionPath`等の既存検索fieldが日本語Analyzerで解析される差だけを比較する。

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

### 時間予算プロファイルの確認

`/health` が採用中の時間profileを公開する。eval-runnerは起動時にこれと `REQUEST_TIMEOUT_SEC` を
突き合わせ、agent wall timeより短い場合は評価を開始せず設定エラーにする。

```bash
curl -s http://localhost:8000/health | jq '.timeBudget, .layeredLegalRetrieval'
```

`timeBudget.warnings` に次が出た場合は、Phase 0 の実測後に値を明示設定する
(自動では書き換えない。`docs/requirements/docs/layered_legal_evidence_retrieval_plan.md` §11.2)。

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

`AGENT_LLM_DIRECTED_RETRIEVAL=true`にすると、旧planner、レイヤー別Requirement生成、
プログラム側の充足判定・根拠枠選抜を通らず、LLM主導調査だけで回答する。プログラムは
検索・本文取得・ID検証・時間上限を担当し、LLMが候補比較、追加調査、根拠選択を行う。
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

`AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW=true`は回答非接続の比較用であり、activeと同時に
有効にしてもactive経路が優先される。

```bash
curl -s http://localhost:8000/health | jq '.llmDirectedLegalRetrieval'
```

主な設定:

```text
AGENT_LLM_DIRECTED_RETRIEVAL=false
AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW=false
LLM_RESEARCH_MODEL=
LLM_RESEARCH_MAX_TOKENS=4096
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
LLM_RESEARCH_MAX_SELECTED_EVIDENCE=16
LLM_RESEARCH_EVIDENCE_CHARS=30000
```

設計と次の実装手順は
[llm_directed_legal_retrieval.md](docs/requirements/docs/llm_directed_legal_retrieval.md)
を参照する。

### Graph棚卸し (Phase 0)

seed済みGraphのnode/edge種別・件数、registryとの一致、authorityTypeの分布を出す。

```bash
uv run --with neo4j --with requests python scripts/graph_inventory.py
```

`ordinance_unspecified` が残る法令は、省令か内閣府令かを人手で確認して
`docs/requirements/samples/eval/law_registry.json` の `authorityType` へ明示する。
推測で確定してはならないが、未判別を理由にレイヤー指定検索から除外もしない。

## 5. UI 操作

1. `http://localhost:8501` を開く。
2. Sidebar の `Health check` を押す。
3. 未投入なら `Seed sample data` を押す。
4. Pattern を選び、質問を入力して `Ask` を押す。

UI では回答、検索ルート、citations、Graph paths、trace を確認できる。

## 6. 評価実行

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

`IMPLEMENTS` と `APPLIED_BY` はseed時に生成するため、この変更を既存環境へ反映するには
agent-apiをbuild後、`/admin/seed` を再実行する。特に「令第N条」を施行令へ結ぶ
委任関係もseed済みGraphの内容なので、再seedせずにコードだけ更新した場合は追加されない。
Graph展開は上位ノードごとに少数ずつ取得した後、質問との語句被覆と接続先法令の多様性で
経路を選び、一つの条文や一つの法令の参照先だけで `AGENT_MAX_GRAPH_PATHS` を
使い切らないようにする。
このうち親法参照・`IMPLEMENTS`・`APPLIED_BY` で機械的に確定した対象だけは、
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
[legal_issue_coverage_retrieval.md](docs/requirements/docs/legal_issue_coverage_retrieval.md)
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
  [evaluation_design.md](docs/requirements/docs/evaluation_design.md) 2.1 を参照。
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
