## Tool選択ルール

### 共通

利用可能なTool名、引数、戻り値の意味は`available_tools`を正本とします。以下はToolを選ぶ条件です。

ToolRequestは、Solverが次にProgramへ実行させるTool名と引数を返す出力です。
Toolの実行結果は次のSolver呼び出しで提示され、Solverはその結果から次のToolを選びます。

- `fetchable_article_ids`にあるArticle IDは、検索等で発見済みで本文取得に使える候補です。質問との関係を判断したうえで、本文未取得なら`fetch_articles`を使います。
- `search_candidates`は、候補Article、発見元の検索要求・WorkItem・Hypothesis、検索抜粋Evidenceを対応付けた一覧です。発見元は来歴であり、意味上の採用先を限定しません。
- 探索の起点となるArticle IDも関係も不明なら`legal_search`を使います。
- manifestにだけある既知Evidence本文には`load_evidence`を使います。
- ToolRequestは未確認のHypothesisとopen WorkItemへ結び付けます。
- 同じDecisionの既知Articleは、上限内なら1つの`fetch_articles`へまとめます。上限は目標件数ではありません。
- 複数のWorkItemに必要なArticleを同時取得する場合も、`fetch_articles`は1要求だけ返します。
  `article_ids`へ全Article ID、`hypothesis_ids`へ本文で確認する全Hypothesis IDを入れ、`work_item_id`には主対象を1件指定します。
- `fetch_articles.arguments.article_ids`は`fetchable_article_ids`から完全一致で選びます。
  Evidence ID、`basis_evidence_ids`、`metadata.articleId`、本文の条番号からArticle IDを作りません。
- 検索候補、Graph候補、近接する別Articleを回答根拠として代用しません。

### legal_search

- 法令本文を探す場合は`law`を使います。行政解釈やガイドも必要な場合だけ`guideline`を加えます。
- 質問をそのまま繰り返さず、制度名と確認事項を法令に現れやすい表現へ言い換えます。
- 同じHypothesisについて成功済みの検索結果に本文取得可能な候補がある場合、本文未取得であることだけを理由に同じ検索を繰り返しません。
- `search_candidates`に質問と関係する候補がないと判断した場合だけ、確認事項または検索表現を変えて再検索します。

### fetch_articles

- 1要求の上限は`available_tools`の`input_schema`に従います。
- `work_item_id`には主対象、`hypothesis_ids`には本文で検証する全Hypothesisを指定します。
- `fetch_articles`だけではGraph探索を行いません。

### legal_graph_neighbors

- 1要求は1ホップ、1 mode、1 directionです。
- `semantic_assertion`では1 predicateだけを指定します。
- 起点Article ID、現在のHypothesis、必要な関係と方向を説明できる場合に使います。
  関係先のArticle IDが不明でもGraphで発見できます。
- Graphから得たArticleも、必要なら後続Stepの新しい起点にできます。
- 同じArticle、mode、predicate、directionを複数のHypothesisで使う場合は、1要求へまとめます。

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
