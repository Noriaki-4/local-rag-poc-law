# 埋め込み・チャンク・Hybrid検索設計

## 1. 目的

Vector / Hybrid RAGの比較実験で、実装者が最初に踏む問題を明示する。特に、金商法第2条のような長大な定義条文を想定する。

## 2. 埋め込みモデル候補

Step1ではモデルを固定して比較する。初期候補は以下。

| 候補 | 用途 | 備考 |
|---|---|---|
| multilingual-e5-large 系 | 日本語・多言語の一般的な検索ベースライン | クエリに `query:`、本文に `passage:` prefixを付ける運用が一般的 |
| bge-m3 | Step1 POC の既定モデル | Ollamaでローカル実行。1024次元、多言語、長文対応 |
| ruri 系日本語埋め込み | 日本語検索の比較候補 | POCで日本語精度を確認する場合の比較対象 |
| Bedrock embedding相当 | Step2 AWS移行の比較候補 | Step1ではAPI利用可否・コストに応じて任意 |

初期POCでは、**1モデルに固定**する。Pattern比較時にembeddingモデルを変えない。

### Phase 0で確定する項目

埋め込みモデルを確定した時点で、OpenSearch mapping の `embedding.dimension` も同時に確定する。

```text
embedding_provider = ollama
embedding_model = bge-m3
embedding_dimension = 1024
embedding_max_chars = 1000
```

`samples/metadata/opensearch_index_mapping.sample.json` の `dimension: 1024` は、bge-m3 の出力次元に合わせている。ruri系やBedrock系など別モデルへ変更する場合は、OpenSearch mapping と `EMBEDDING_DIMENSION` を同時に変更して再indexする。
長大条文をそのまま投入すると初回embeddingが重くなるため、POCでは `EMBEDDING_MAX_CHARS=1000` で embedding 入力を正規化・切り詰める。

## 3. チャンク戦略

### 基本単位

法令は以下の階層を保持する。

```text
Law -> Article -> Paragraph -> Item
```

原則:

- Articleが短い場合: Article単位で1チャンク
- Articleが長い場合: Paragraph単位へ分割
- Paragraphも長い場合: sentence / clause単位へ追加分割
- `parentContentUnitId` で上位Articleへ戻れるようにする

### 長大条文対策

金商法第2条のような定義条文では、Article単位の埋め込みは長すぎる可能性が高い。

推奨:

```text
article-2                 # 親Articleノード。本文全量ではなく見出し・要約中心
article-2-paragraph-1     # 実検索チャンク
article-2-paragraph-2
article-2-paragraph-2-part-001  # 長すぎる項の分割
```

各チャンクに以下を持たせる。

```text
documentId
contentUnitId
parentContentUnitId
articleNumber
paragraphNumber
itemNumber
sectionPath
heading
text
```

## 4. OpenSearch向けドキュメント形式

Step1 / Step2 は OpenSearch 直を前提にするため、Bedrock KB固有の `metadataAttributes` / `includeForEmbedding` 形式は使わない。

OpenSearchに投入する1ドキュメント例は `samples/metadata/opensearch_document.sample.json` を参照。


### OpenSearch投入サンプルの注意

`samples/metadata/opensearch_document.sample.json` の `embedding` は、mapping確認・smoke test用の **非ゼロの1024次元ダミーベクトル**である。`/admin/seed` 実行時は `agent-api/app/embeddings.py` が Ollama `bge-m3` から実ベクトルを生成して上書きする。

別embedding providerへ変更する場合は、`embedding_model` と `embedding_dimension` に合わせて実ベクトルへ置換する。サンプル用メモや `_note` などの説明フィールドはOpenSearch投入前に除去する。投入スクリプトは、許可フィールド以外をstripしてからPOSTする。

## 5. OpenSearch index mapping例

`samples/metadata/opensearch_index_mapping.sample.json` を参照。

主なフィールド:

- `text`: 検索本文。BM25対象
- `embedding`: kNN vector
- `documentId`, `contentUnitId`, `docType`, `contentDomain`: filter / join用
- `publishStatus`, `isLatest`, `deptCode`, `clearanceLevel`: 必須フィルタ用
- `sourceObjectUri`, `sourcePage`: citation用

## 6. Hybrid検索の初期設定

Pattern比較では重みを固定する。

初期値:

```text
bm25_weight = 0.4
knn_weight  = 0.6
top_k_vector = 20
top_k_bm25   = 20
rerank_top_k = 10
final_top_k  = 5
```

OpenSearchの実装上は、BM25とkNNを別々に検索し、アプリ側で正規化・加重和する方式から始める。

補足: OpenSearch 2.19系では、search pipeline の normalization processor を使ったネイティブ hybrid query も比較候補にする。初期実装はアプリ側正規化・加重和で始めるが、Step2でOpenSearch Service / Serverlessへ寄せる際は、ネイティブhybrid方式との挙動差も確認する。

## 7. 比較実験で固定する変数

Pattern 1〜4を比較する間、以下は固定する。

```text
embedding_model
chunk_strategy
hybrid_weight
top_k
rerank有無
LLMモデル
```

これらを変える場合は、別実験として扱う。


補足: `confidentiality` と `clearanceLevel` の対応は `docs/clearance_policy.md` を正とする。
