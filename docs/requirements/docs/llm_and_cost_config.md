# LLM選定・固定変数・コスト前提

## 1. 目的

Pattern 3以降のクエリ分解・検索計画・選択肢判定はLLM依存が大きい。比較実験では、LLMを固定してPattern差分だけを見る。

## 2. 初期設定テンプレート

```yaml
llm_config:
  provider: "ollama"
  base_url: "http://host.docker.internal:11434"
  planner_model: "gemma4:e4b"
  answer_model: "gemma4:e4b"
  judge_model: "none"
  temperature: 0
  top_p: 1
  max_output_tokens: 2048
  timeout_sec: 60
```

初期動作確認では、有料 API の想定外トークン消費を避けるため Ollama 上の `gemma4:e4b` を使う。Claudeを使う場合は `LLM_PROVIDER=anthropic`、`ANTHROPIC_API_KEY`、`ANSWER_MODEL` を `.env` で切り替える。judge は初期では `none` とし、評価はルール評価を主にする。

## 3. モデル利用パターン

| 用途 | 推奨 |
|---|---|
| Pattern 1回答 | answer_model固定 |
| Pattern 2回答 | answer_model固定 |
| Pattern 3 Planner | planner_model固定 |
| Pattern 3 Evidence Evaluator | planner_modelまたはanswer_model固定 |
| LLM-as-Judge | 初期は任意。使う場合はjudge_model固定 |

## 4. コスト計測

全tool call / LLM callで以下を記録する。

```text
model
input_tokens
output_tokens
latency_ms
estimated_cost
```

API利用時は実価格テーブルを設定ファイル化する。ローカルLLMの場合は estimated_cost=0 とし、代わりに推論時間を記録する。

Agent API の trace には以下を記録する。

```text
provider
model
inputTokens
outputTokens
latencyMs
estimatedCost = 0
```

## 5. 比較実験の統制

Pattern 1〜4比較では以下を固定する。

```text
planner_model
answer_model
embedding_model
chunk_strategy
hybrid_weight
top_k
max_total_tool_calls
max_retry_rounds
```

max_total_tool_calls はPatternごとに一意に定める。Pattern 3の初期値は `8` とする。
