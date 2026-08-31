## 下位規範を確認する次の行動

### 目的

`needs_action`のWorkItemについて、未確認の下位規範を確認する次のToolRequestを選びます。現在Cycleに有効な要求が残らない場合は、次Cycleで探索方針を見直します。WorkItem、Hypothesis、DependencyDecisionの意味評価はやり直しません。
入力に出ていない他のopen WorkItemはCaseStoreに残り、この行動後に再提示されます。

### 出力

- 処理上限内で選んだ`needs_action`を進めるToolRequest
- 現在Cycleに有効なTool要求がない場合の`start_next_cycle=true`
- Toolを選んだ短い理由

### 完了条件

選んだ`needs_action`に未確認事項を進めるToolRequestが対応しているか、重複しない有効な要求がない場合に次Cycleへの見直しを選んでいることです。

### 手順

1. `action_feedback`がある場合は、棄却されたTool種類を使わず、別種のToolまたは次Cycleを選びます。
2. `basis_evidence_ids`の本文から、確認済みの規定と残る未確認事項を把握します。
3. 取得本文が、WorkItemで問われた未確認内容を下位規範へ明示的に委ねている場合は、その委任元Articleから下位規範を探します。
4. それ以外は、本文未取得の既知候補が未確認事項を直接扱うか確認します。該当する候補があれば本文取得を選びます。
5. 該当する既知候補がなければ、次の判断基準から未確認事項を最も直接進めるToolを選びます。
   - 同じ事項を扱う既知候補の本文が必要：`fetch_articles`
   - 起点Articleと調べる関係・方向を説明できる：`legal_graph_neighbors`
   - Articleまたは関係がまだ分からない：`legal_search`
   - 必要な既知Evidence本文が今回省略されている：`load_evidence`
   - Graphを使う場合は、Hypothesisに合う意味関係を説明できれば`semantic_assertion`を優先し、新規候補が得られなかった場合だけ明示参照へ切り替えます。
6. 有効な行動がある場合は、処理上限内で今回進める`needs_action` WorkItemを選び、各WorkItemにToolRequestを1件返して`start_next_cycle=false`にします。今回選ばないWorkItemは次stepへ残します。
7. 成功済みscope以外に未確認事項を進める行動がない場合は、ToolRequestを返さず`start_next_cycle=true`にします。

### ルール

#### 行動の選択

- 候補名や関係ラベルだけで下位規範を確認済みにしません。
- `material_evidence`にある本文を再取得しません。
- 成功済みの検索・Graph scopeを繰り返しません。
- `completed_graph_searches`の候補IDと新規候補IDを確認し、進展のなかった探索を引数だけ変えて反復しません。
- `action_feedback`にあるTool種類は、今回の修復では使いません。別種のToolが適切でなければ`start_next_cycle=true`にします。
- 未確認Hypothesisに対応する本文未取得の既知候補がある間は、その候補を確認してから再検索します。
- 下位規範への委任が本文で確認できる場合は、同じ階層の周辺規定を広げる前に、その委任元Articleから委任内容に合う関係をGraphで確認します。
- Graphで下位規範が見つからず`legal_search`へ切り替える場合は、委任元の条・項・号を、下位規範に現れる引用表現へ直して検索語に含めます。
- `fetch_articles`の取得枠は`remaining_fetch_capacity_by_work_item`に示すWorkItemごとの値です。1つの要求には、そのWorkItemに属するHypothesisだけを指定します。
- WorkItemが既知規定に関係する規定又は改正影響先の列挙を求める場合は、語句検索だけで範囲を
  確認済みにせず、起点ArticleからHypothesisに合う関係をGraphで確認します。
- `available_tools`が`legal_graph_neighbors`だけの場合は、処理上限内で今回進める`needs_action`についてGraph要求を返します。今回選ばないWorkItemは後続stepへ残し、Graphを使わずに次Cycleへ移りません。
- `start_next_cycle`は`can_start_next_cycle=true`の場合だけ選べます。検索語をわずかに変えただけの要求を作る代わりには使いません。

#### この処理ではしないこと

- 回答、状態更新、DependencyDecisionの再判定は行いません。Cycleについては、現在の行動を続けられない場合の次Cycle移行だけを判断できます。
