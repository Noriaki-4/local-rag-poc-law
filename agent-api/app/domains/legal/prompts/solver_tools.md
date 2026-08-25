## Tool選択ルール

`available_tools`が、今回使えるTool名、用途、入力schema、戻り値の正本です。以下は選択時の判断基準であり、実行順ではありません。

### 共通原則

- ToolRequestは、未確認のHypothesisとopen WorkItemへ結び付けます。
- `search_candidates`とGraph候補は発見情報です。本文確認前に根拠へ使いません。
- 同一Decisionで複数Articleの本文を取得する場合は、上限内で1つの`fetch_articles`へまとめます。
- 本文取得済みArticleと、成功済みと完全一致する検索・Graph要求は繰り返しません。

### `legal_search`

Article IDまたは探索すべき関係がまだ分からない場合に、OpenSearchで候補を発見します。

- 制度名と未確認事項を、法令本文に現れやすい表現へ言い換えます。
- 法令本文は`law`、行政解釈やガイドも必要な場合は`guideline`を対象にします。
- 結果はnavigationです。候補本文は別途取得します。

### `fetch_articles`

質問との関係を説明できる既知候補の本文を取得します。

- `article_ids`は`fetchable_article_ids`から選びます。
- Article IDとEvidence IDを混同しません。
- 取得した本文は、次のSolver呼び出しで`material_evidence`に提示されます。

### `legal_graph_neighbors`

起点Articleと、Hypothesisに必要な関係・方向を説明できる場合に1ホップ先を発見します。

- 1要求は1 mode、1 directionです。`semantic_assertion`では1 predicateを指定します。
- Graphで発見したArticleも、本文確認後に必要なら次の1ホップ探索の起点にできます。
- 結果はnavigationです。関係ラベルだけで法的結論を確定しません。

#### 関係と方向

- `formal_relation`は原文・構造から登録された関係、`relation_assertion`は非同期LLMが分類した関係候補です。
- `REFERENCES`はfrom本文がtoを明示参照し、`EXPLAINS`はガイドがto Articleを解説します。
- `outgoing`は起点がfrom、`incoming`は起点がtoです。
- `relation_assertion`はSUBJECTからOBJECTへ向きます。`from_subject`は起点がSUBJECT、`to_subject`は起点がOBJECTです。
- `IMPLEMENTS`：親規定から具体化規定へ向く。
- `INCORPORATES`：準用・読替えする規定から、取り込まれる規定へ向く。
- `USES_DEFINITION`：定義を使う規定から、定義を置く規定へ向く。
- `EXCEPTION_TO`：例外規定から一般規定へ向く。
- `OVERRIDES`：優先規定から、排除または修正される規定へ向く。

`USES_DEFINITION`はラベルだけで選ばず、対象語とscopeがHypothesisに必要か確認します。`referenceKind`や`REFERENCES`だけから、委任、具体化、適用を確定しません。

### `load_evidence`

Caseでは取得済みだが、今回のPromptから省略されたEvidence本文を再表示します。`omitted_evidence_ids`にあるIDだけを指定します。新しいArticleの発見・取得には使いません。
