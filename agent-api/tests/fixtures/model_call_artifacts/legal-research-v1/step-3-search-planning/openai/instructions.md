# 法令調査Solver：検索要求の作成

## 目的

入力済みのHypothesisを検証するため、今回実行する`legal_search`要求を作ります。

## 出力

- `search_requests`：今回実行する`legal_search`要求

## 完了条件

- 各検索要求について、どのHypothesisの何を確認する検索かが分かる。
- 各検索語が、質問の言い換えではなく、法令本文から候補を探せる表現になっている。

## 手順

1. 各Hypothesisの命題を確認します。`gaps`がある場合は、残る未確認事項として検索語へ反映します。
2. `available_tools`で`legal_search`の用途、入力項目、戻り値を確認します。
3. 今回の上限内で、未検証Hypothesisを調べる検索要求を作ります。
4. 完了条件を確認します。

## ルール

### 検索対象

- WorkItemやHypothesisの内容は変更しません。
- 1つの検索要求は、同じWorkItemに属する1件以上のHypothesisを対象にします。
- 複数Hypothesisで同じ検索語と候補を使える場合は、1つの検索要求にまとめられます。別々の検索にすることは強制しません。

### 検索語

- `purpose`には、この検索で確認する内容を文章で書きます。
- `query`には、検索欄へ入力する短い法令用語・法令表現の組合せだけを書きます。質問文や`purpose`を別の文章にして入れません。
- 検索語は質問文をそのまま繰り返さず、制度名と、確認する規律を表す語を組み合わせます。
- 確認する規律に応じて、法令本文に現れる表現を使います。
  - 義務・成立条件：`しなければならない`、`場合`
  - 定義・範囲：`とは`、`をいう`、`に掲げる`
  - 例外：`適用しない`、`除く`、`ただし`
  - 委任：`政令で定める`、`府令で定める`
  - 手続：対象となる`公告`、`届出`、`提出`等の行為
- `に関する法令`、`必要になる条件`、`具体的条件`、`確認する`のような説明は`purpose`に書き、`query`には入れません。
- 法令表現へ言い換えても、Hypothesisの主体、場所、行為方法、人数、数量、割合、期間等の識別に必要な事実は落とさず、別の判定軸へ変えません。
- Hypothesisが行為者と対象に結び付く主体を別の役割としている場合、その区別が消える検索語にしません。

### 例

同じ制度について「義務の例外」と「例外要件の下位法令への委任」を確認する場合、検索要求を分ける必要はありません。

- `purpose`：制度上の義務に対する例外と、委任された要件を確認する。
- `query`：`制度名 義務 適用しない 除く 政令で定める`

### 上限と推測

- 根拠条文やArticle IDを推測しません。
- 上限内で今回検索しないHypothesisが残っても削除しません。Tool結果の評価後に次の検索を判断します。

<input_contract>
以下は今回の入力項目と意味です。
- `question`: 利用者が回答を求めている元の質問。
- `work_items`: 今回の質問から作成済みのWorkItem。各要素は既知IDと1つの確認事項を持つ。
  - `work_items[].work_item_id`: Programが付与した既知WorkItem ID。
  - `work_items[].question`: このWorkItemで確認する1つの法的事項。
  - `work_items[].action_actor`: 確認事項で規制対象となる行為をする者。未指定ならnull。
- `hypotheses`: 作成済みの未確認Hypothesis。各要素は所属WorkItem、命題、gapsを持つ。
  - `hypotheses[].hypothesis_id`: Programが付与した既知Hypothesis ID。
  - `hypotheses[].work_item_id`: このHypothesisが属する既知WorkItem ID。
  - `hypotheses[].statement`: 法令本文で検証する暫定的な法的結論。
  - `hypotheses[].action_actor`: 所属WorkItemで確定した、規制対象となる行為をする者。
  - `hypotheses[].gaps`: 結論のうち法令本文で確定すべき未確認の規律要素。
- `available_tools`: 現在のStepで要求できるTool一覧。
  - `available_tools[].name`: SolverDecision.tool_requestsで使う正規のTool名。
  - `available_tools[].description`: Toolが何を行い、いつ使い、何を行わないかを説明するLLM向け契約。
  - `available_tools[].input_schema`: Tool argumentsのProvider非依存JSON Schema。
  - `available_tools[].result_description`: ToolResultとEvidenceとして返る情報および制約。
  - `available_tools[].read_only`: 外部状態を変更しないToolならtrue。
  - `available_tools[].parallel_safe`: 他のread-only Toolと安全に並列実行できるならtrue。
- `max_tool_requests_per_step`: 今回のStepで返せるTool要求総数の上限。
</input_contract>

{{runtime_input}}

## 出力前の確認

1. 各検索要求について、どのHypothesisの何を確認する検索か説明できるか確認します。
2. `purpose`は確認内容の説明、`query`は制度名を含む短い法令用語・法令表現の組合せになっているか確認します。`query`が質問又は`purpose`の言い換えなら修正します。
3. 複数Hypothesisをまとめた場合は、同じ検索語と候補を使えるか確認します。
4. `doc_types`が今回必要な検索対象だけを含むか確認します。
5. 問題があれば修正してから、schemaに従う出力だけを返します。
