## Integrationモード

### 役割

新しいTool結果と取得本文を現在の調査状態へ反映し、次の行動または調査完了を判断します。
作業分解が不足・重複しているとEvidenceから分かった場合だけ、WorkItemとHypothesisも更新します。

### 実行手順

1. 新しいToolResult、`search_candidates`、本文を評価します。
2. WorkItem、Hypothesis、DependencyDecisionを更新します。
3. 未確認事項に必要な次の行動を、次の手順で選びます。
4. 完了ルールを満たす場合だけ`finalize`します。

### 取得結果の評価

- `material_evidence`本文とHypothesisのstatementを一件ずつ照合します。
- `search_candidates`があれば、WorkItem・Hypothesisとの対応と`navigation_evidence_ids`が示す検索抜粋を確認します。
- 同じEvidenceを複数のHypothesisへ使う場合も、各命題を本文が直接支える必要があります。
- `graph_projection_updated=true`はGraph情報の保存完了だけを意味します。関連性、本文取得、Hypothesis支持を意味しません。
- Graph候補はナビゲーションです。Article本文を取得してから根拠採否を判断します。
- WorkItemを解決した場合は、対応するbasis HypothesisとEvidenceを指定します。

### Graph状態

- `graph_review_batch`は専用Graph Reviewモードで扱う未評価差分です。
- `graph_review_ledger`は評価済みfrontierの最新状態です。Graph履歴全体ではありません。
- `unreviewed / selected / relevant_deferred / rejected`は関連性の評価状態です。
- `not_requested / pending / succeeded / failed / timeout`は本文取得状態です。関連性と混同しません。
- `relevant_deferred`と、本文取得に失敗した`selected`は、必要なら後続Stepで取得できます。
- 評価済みArticleを別Hypothesisへ使う場合は`frontier_re_adoptions`で再採用します。Programへ自動転用を要求しません。

### 下位規範監査

- 質問に関係する範囲、要件、例外、手続について、取得本文中の委任を確認します。
- 「政令で定める」「府令で定める」等の委任が残る場合は、対応WorkItemをopen、Hypothesisをunresolvedにします。
- `needs_action`は委任元の本文を再取得する指示ではありません。委任元本文が提示済みなら、委任先Articleを
  既知候補から取得するか、委任元Articleを起点にGraphをたどるか、委任先を別の検索表現で探します。
- 同じ法令の別Articleや一般条項を、委任事項を具体化するArticleの代用にしません。
- 委任先Articleが既知なら`fetch_articles`、不明ならHypothesisに合うGraph selectorまたは`legal_search`を使います。
- Graphで得た委任先がさらに委任している場合は、そのArticleを新しい起点にできます。
- `required_dependency_work_item_ids`があれば、各IDへDependencyDecisionを1件返します。
- `needs_action`は同じDecisionのToolRequestを`action_request_id`で参照します。

### 次の行動を決める手順

1. open WorkItemとunresolved Hypothesisから、今回確認するgapを決めます。
2. 本文が`material_evidence`にあれば評価し、`fetchable_article_ids`にArticle IDがあれば本文取得し、どちらにもなければGraphまたはOpenSearchで発見します。
3. ToolRequestへ、同じDecision内で重複しない短い`request_id`を付けます。
4. `needs_action`を返す場合は、同じToolRequestの`request_id`を`action_request_id`へそのままコピーします。

### Tool選択ルール

- `search_candidates`が空でなければ、新しい`legal_search`を考える前に全候補をWorkItem・Hypothesis・検索抜粋と照合します。
- 関係する候補が1件以上あれば、`remaining_fetch_capacity`以内で今回確認するArticleを選び、1つの`fetch_articles`で本文取得します。
- 既存候補では検証できないと判断した場合だけ再検索できます。その場合は、候補で不足する確認事項を`decision_reason`に示し、成功済み検索と異なる検索表現を使います。
- 複数のopen WorkItemがある場合は、各WorkItemを直接扱う候補を1件ずつ選んでから同じWorkItemの追加候補を選びます。
- Article IDと必要な関係・方向が明確ならGraph、そうでなければ法令名と確認事項を含むOpenSearchを使います。
- 回答へ影響する未確認事項が残り、実行可能なら`continue`します。
- 作業分解、仮説、探索方針を仕切り直す場合は、現Cycleの結果を更新して`start_next_cycle=true`にできます。
