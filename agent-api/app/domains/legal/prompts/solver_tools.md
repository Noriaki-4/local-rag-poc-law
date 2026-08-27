## Tool選択ルール

`available_tools`が、今回使えるTool名、用途、入力schema、戻り値の正本です。以下は選択時の判断基準であり、実行順ではありません。

### 共通原則

- ToolRequestは、未確認のHypothesisとopen WorkItemへ結び付けます。
- `search_candidates`とGraph候補は発見情報です。本文確認前に根拠へ使いません。
- 同一Decisionで複数Articleの本文を取得する場合は、上限内で1つの`fetch_articles`へまとめます。
- 本文取得済みArticleと、成功済みの検索・Graph scopeは繰り返しません。
- 検索・Graph scopeは`work_item_id`、`hypothesis_ids`、Tool引数の組です。
  `request_id`や`purpose`だけを変えても別scopeにはなりません。

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

起点Articleと、Hypothesisに必要な関係・探索目的を説明できる場合に1ホップ先を発見します。

- Hypothesisに必要な意味関係を説明できる場合は、まず`semantic_assertion`を使います。
- `semantic_assertion`では、Hypothesisに対応する1 predicateと、起点Articleから見た1 directionを指定します。
- 明示された参照先そのものを確認する場合、または意味関係の探索で新規候補が得られず参照関係を確認する場合は、`explicit_reference`を使います。
- `explicit_reference`では、本文に書かれた参照先をたどる場合は`follow_reference_in_text`、起点を参照するArticleを探す場合は`find_articles_referencing_this`を使います。
- `completed_graph_searches[].new_candidate_article_ids`が空でも、法的関係の不存在は確定しません。意味関係から明示参照へ切り替えるか、別検索または限定回答へ進みます。
- 意味関係と明示参照の両方で新規候補がなければ、引数だけを変えたGraph探索を反復しません。
- 1要求は1 mode、1探索目的です。`semantic_assertion`では1 predicateと1 directionを指定します。
- Graphで発見したArticleも、本文確認後に必要なら次の1ホップ探索の起点にできます。
- 結果はnavigationです。関係ラベルだけで法的結論を確定しません。

#### 関係と方向

- `formal_relation`は原文・構造から登録された関係、`relation_assertion`は非同期LLMが分類した関係候補です。
- `REFERENCES`はfrom本文がtoを明示参照し、`EXPLAINS`はガイドがto Articleを解説します。
- 明示参照の物理方向はToolが`reference_lookup`から変換します。LLMは`outgoing / incoming`を指定しません。
- `relation_assertion`はSUBJECTからOBJECTへ向きます。`from_subject`は起点がSUBJECT、`to_subject`は起点がOBJECTです。
- `IMPLEMENTS`：親規定から具体化規定へ向く。
- `INCORPORATES`：準用・読替えする規定から、取り込まれる規定へ向く。
- `USES_DEFINITION`：定義を使う規定から、定義を置く規定へ向く。
- `EXCEPTION_TO`：例外規定から一般規定へ向く。
- `OVERRIDES`：優先規定から、排除または修正される規定へ向く。

`USES_DEFINITION`はラベルだけで選ばず、対象語とscopeがHypothesisに必要か確認します。`referenceKind`や`REFERENCES`だけから、委任、具体化、適用を確定しません。

### `load_evidence`

Caseでは取得済みだが、今回のPromptから省略されたEvidence本文を再表示します。`omitted_evidence_ids`にあるIDだけを指定します。新しいArticleの発見・取得には使いません。
