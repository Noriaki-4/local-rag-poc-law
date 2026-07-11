# Agentic RAG ロジック 4パターン

本書は目標とする設計を示す。現行POCコードの `/answer` は全パターンで `law_search_tool` のみを実行し、`graph_search_tool` の組み込みと LLM によるクエリ分解は未実装（Graph 探索は `/graph/path` エンドポイントで個別に検証可能）。

## Pattern 1: Baseline RAG

### 位置づけ

比較基準。OpenSearchのみ。

### ロジック

```text
Input
  -> law_search_tool
  -> source_fetch_tool
  -> citation_builder
  -> answer / predictedAnswer
```

### 特徴

- Plannerなし
- GraphRAGなし
- クエリ分解なし
- 再検索なし
- 引用は必須

## Pattern 2: Rule-based Agentic RAG

### 位置づけ

初期POC本命。固定ルールで検索ルートを決める。

### ルール例

```text
lawqa_jp:
  law_search_tool

根拠・条文・法令・規定:
  law_search_tool

定義・ただし・除く・前条・同条:
  law_search_tool -> graph_search_tool -> source_fetch_tool
```

### Graph条件

lawqa_jpでは、単語の単純出現だけでGraph Expansionを発火しない。以下の条件を満たす場合に使う。

```text
1. 問題文または選択肢に、参照語が2種類以上出る
   例: 「定義」+「ただし」、「前条」+「除く」

2. 選択肢内に参照語があり、選択肢の正誤判断に直接関係する

3. 初回Vector検索で取得した条文本文に、未解決の参照表現がある
   例: 「前項」「同号」「別に定める」

4. Evidence Evaluatorが「定義条文・参照条文・例外条文が不足」と判定する
```

禁止: 「規定する」「ただし」などが本文に1語出ただけで常時Graph展開しない。

## Pattern 3: Controlled Agentic RAG

### 位置づけ

中間ロジック。LLMに提案させるが、Orchestratorが制御する。

### ロジック

```text
Input
  -> inputType判定
  -> 必要ならInstruction RAG
  -> LLM Query Decomposition
  -> LLM Search Plan Proposal
  -> Orchestrator Validation
  -> Secure Filter Injection
  -> Vector / Graph Search
  -> Evidence Evaluator
  -> Follow-up Search 最大2回
  -> Evidence Merge
  -> Citation Builder
  -> Evaluation Logger
```

### 再検索ポリシー

```text
max_retry_rounds = 2
max_graph_hop = 2
max_total_tool_calls = 8
max_docs_per_round = 5
```

再検索は根拠不足時のみ。

### 停止条件

```text
必須引用が揃った
選択肢ごとの判定根拠が揃った
追加検索しても新しいcontentUnitIdが増えない
類似度が低すぎる
tool call上限到達
```

## Pattern 4: Full DeepSearch Agent

### 位置づけ

最終系。初期実装では追いすぎない。

### ロジック

```text
User Context Resolve
Input Router
Instruction Retrieval
Query Understanding
Query Decomposition
Search Route Planning
Secure Filter Injection
Multi-Retrieval Execution
Graph Expansion
Source Fetch
Evidence Grading
Follow-up Query Generation
Failure Diagnosis / Local Repair
Evidence Merge
Answer Composer
Citation Builder
Evaluation Logger
```

### 対応モード

```text
law_deepsearch:
  law -> graph expansion -> citation

lawqa_solver:
  issue extraction -> choice decomposition -> law search -> graph expansion -> choice judgement -> citation
```

## 推奨実装順

```text
1. Pattern 1: lawqa_jp + OpenSearch
2. Pattern 2: lawqa_jp + 条件付きGraphRAG
3. Pattern 3: クエリ分解 + 最大2回再検索
4. Pattern 4の一部: Instruction RAG
```
