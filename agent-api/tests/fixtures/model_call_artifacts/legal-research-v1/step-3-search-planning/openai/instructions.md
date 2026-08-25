# 法令調査Solver：検索要求の作成

## 目的

入力済みのHypothesisを検証するため、今回実行する`legal_search`要求を作ります。

## 出力

- `search_requests`：今回実行する`legal_search`要求

## 完了条件

- 各検索要求が、既知WorkItemとそれに属する未検証Hypothesisを参照している。
- 各検索語が、対象Hypothesisまたは`gaps`を検証できる法令表現になっている。
- 今回の要求数が入力された上限内に収まっている。

## 手順

1. 各Hypothesisの命題と`gaps`を確認します。
2. `available_tools`で`legal_search`の用途、入力項目、戻り値を確認します。
3. 今回の上限内で、未検証Hypothesisを調べる検索要求を作ります。
4. 完了条件を確認します。

## ルール

### 検索対象

- WorkItemやHypothesisの内容は変更しません。
- 1つの検索要求は、同じWorkItemに属する1件以上のHypothesisを対象にします。

### 検索語

- 検索語は質問文をそのまま繰り返さず、制度名、行為または法的効果、判定軸を法令本文に現れやすい語で組み合わせます。
- 法令表現へ言い換えても、Hypothesisの主体、場所、行為方法、人数、数量、割合、期間等の識別に必要な事実は落とさず、別の判定軸へ変えません。
- Hypothesisが行為者と対象に結び付く主体を別の役割としている場合、その区別が消える検索語にしません。

### 上限と推測

- 根拠条文やArticle IDを推測しません。
- 上限内で今回検索しないHypothesisが残っても削除しません。Tool結果の評価後に次の検索を判断します。

<input_contract>
以下は今回の入力項目と意味です。
- `question`: 利用者が回答を求めている元の質問。
- `work_items`: 今回の質問から作成済みのWorkItem。各要素は既知IDと1つの確認事項を持つ。
  - `work_items[].work_item_id`: Programが付与した既知WorkItem ID。
  - `work_items[].question`: このWorkItemで確認する1つの法的事項。
  - `work_items[].actor_scope`: 確認事項の行為者と、対象に結び付く主体との関係。未指定ならnull。
  - `work_items[].actor_relation`: sameは同一主体、differentは別主体、unknownは未確定。
- `hypotheses`: 作成済みの未確認Hypothesis。各要素は所属WorkItem、命題、gapsを持つ。
  - `hypotheses[].hypothesis_id`: Programが付与した既知Hypothesis ID。
  - `hypotheses[].work_item_id`: このHypothesisが属する既知WorkItem ID。
  - `hypotheses[].statement`: 法令本文で検証する未確認の法的命題。
  - `hypotheses[].actor_scope`: 命題の行為者と、対象に結び付く主体との関係。未指定ならnull。
  - `hypotheses[].actor_relation`: sameは同一主体、differentは別主体、unknownは未確定。
  - `hypotheses[].gaps`: 命題のうち法令本文でまだ確認すべき具体的な法的内容。
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

1. 各検索要求が既知WorkItemと、そのWorkItemに属する既知Hypothesisを参照するか確認します。
2. 検索語がHypothesisまたは`gaps`の検証に使える法令表現か確認します。
3. `doc_types`が今回必要な検索対象だけを含むか確認します。
4. 問題があれば修正してから、schemaに従う出力だけを返します。
