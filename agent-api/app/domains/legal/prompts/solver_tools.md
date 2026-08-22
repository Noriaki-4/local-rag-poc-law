## Tool選択ルール

### 共通

- 既知Articleの本文取得には`fetch_articles`、Article IDが不明なら`legal_search`を使います。
- manifestにだけある既知Evidence本文には`load_evidence`を使います。
- ToolRequestは未確認のHypothesisとopen WorkItemへ結び付けます。
- 同じDecisionの既知Articleは、上限内なら1つの`fetch_articles`へまとめます。上限は目標件数ではありません。
- `fetch_articles.arguments.article_ids`は`fetchable_article_ids`から完全一致で選びます。本文の条番号からIDを作りません。

### legal_search

```json
{"query":"検索語","doc_types":["law"],"document_ids":["既知documentId"]}
```

- 法令本文を探す場合は`law`を使います。行政解釈やガイドも必要な場合だけ`guideline`を加えます。
- 質問をそのまま繰り返さず、制度名と確認事項を法令に現れやすい表現へ言い換えます。

### fetch_articles

```json
{"article_ids":["既知articleId"]}
```

- 1要求は最大4 Articleです。
- `work_item_id`には主対象、`hypothesis_ids`には本文で検証する全Hypothesisを指定します。
- `fetch_articles`だけではGraph探索を行いません。

### legal_graph_neighbors

- 1要求は1ホップ、1 mode、1 directionです。
- `semantic_assertion`では1 predicateだけを指定します。
- Article ID、現在のHypothesis、必要な関係と方向を説明できる場合だけ使います。
- Graphから得たArticleも、必要なら後続Stepの新しい起点にできます。

```json
{"article_ids":["既知articleId"],"mode":"semantic_assertion","predicate":"IMPLEMENTS","direction":"from_subject","max_relations":20}
```

```json
{"article_ids":["既知articleId"],"mode":"explicit_reference","direction":"incoming","max_relations":20}
```

```json
{"article_ids":["既知articleId"],"mode":"explains","direction":"incoming","max_relations":20}
```

#### 関係と方向

- `formal_relation`は原文・構造から登録された関係、`relation_assertion`は非同期LLMが分類した未確認候補です。
- `REFERENCES`はfrom本文がtoを明示参照します。`EXPLAINS`はガイドがto Articleを解説します。
- `outgoing`は起点がfrom、`incoming`は起点がtoです。
- `relation_assertion`はSUBJECTからOBJECTへ向きます。`from_subject`は起点がSUBJECT、`to_subject`は起点がOBJECTです。
- `IMPLEMENTS`: 親規定から具体化規定へ向きます。
- `INCORPORATES`: 準用・読替えする規定から、取り込まれる規定へ向きます。
- `USES_DEFINITION`: 定義を使う規定から、定義を置く規定へ向きます。
- `EXCEPTION_TO`: 例外規定から一般規定へ向きます。
- `OVERRIDES`: 優先規定から、排除または修正される規定へ向きます。

関係ラベルは回答根拠ではありません。今回のHypothesisに関係する候補だけを選び、必要なArticle本文を取得して確認します。

`USES_DEFINITION`はラベルだけで判断しません。`relationExplanation`と両端の`supportingQuote`から、対象の語、法的役割、地位、scopeを確認します。Hypothesisがその意味に依存する場合だけ定義側をたどります。定義の適用先を問う場合だけ逆方向をたどります。

`referenceKind`は抽出時の分類であり、本文確認の代わりではありません。`REFERENCES`だけから委任、具体化、適用を確定しません。
