## 現在の作業：Integration

## 目的

新しいTool結果と取得本文を現在の調査状態へ反映し、次の行動または調査完了を判断します。
作業分解が不足・重複しているとEvidenceから分かった場合だけ、WorkItemとHypothesisも更新します。
新しいTool結果がなく前Cycleの検索候補がある場合は、既知候補、Graph、再検索から次Cycleの行動を選びます。

## 手順

1. `required_dependency_work_item_ids`があれば、下位規範監査を最初に行います。
2. 各WorkItemの`gaps`と、それを直接確認できる`material_evidence`のArticleを対応させます。
3. 回答に影響する`gaps`の確認先Article本文がなければ、`needs_action`と次のToolRequestを返します。
   - 確認先Articleが既知候補なら`fetch_articles`を選びます。
   - 既知の上位Articleから委任先・具体化規定を探すなら、
     `legal_graph_neighbors`の`semantic_assertion / IMPLEMENTS / from_subject`を選びます。
   - 起点Articleも関係も分からない場合だけ`legal_search`を選びます。
   この場合は`continue`とし、WorkItemの解決や`finalize`は行いません。
4. 下位規範監査の後、取得本文をHypothesis・WorkItemへ反映し、次の行動または完了を判断します。

`recent_tool_results`の有無は、本文反映と次の行動の判断に使います。

- ある場合：新しいTool結果と本文を評価し、WorkItem、Hypothesis、DependencyDecisionを更新してから次の行動を選びます。
- ない場合：前Cycleの評価をやり直さず、open WorkItemと未確認事項に対する次の行動だけを選びます。
  このときの`search_candidates`は発見・評価済みで本文未取得の候補です。いずれかのgapを直接確認できる候補が
  1件でもあれば、その候補の`article_id`を`fetch_articles`で取得します。別のgapに候補がないことを理由に、
  取得可能な候補を残して`legal_search`を先に行いません。

次の行動を選ぶときは、`search_candidates`の既知候補、既知ArticleからのGraph探索、異なる検索表現による
OpenSearchの順に確認します。完了ルールを満たす場合だけ`finalize`します。

### 取得結果の評価

- `material_evidence`本文とHypothesisのstatementを一件ずつ照合します。
- `gaps`はそのHypothesisに残る未確認事項です。提示本文で確認した項目だけを解消し、
  回答に影響する`gaps`が残る間はHypothesisやWorkItemを閉じず`finalize`しません。
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
- 「政令で定める」「府令で定める」等の委任先の内容が`gaps`に残り、委任先Article本文が
  未提示なら`needs_action`です。`not_required`や`resolved`にせず、WorkItemはopen、Hypothesisはunresolvedのまま次のToolを選びます。
- 委任先Articleが既知候補なら`fetch_articles`、不明でも起点Articleと関係・方向が分かるなら
  `legal_graph_neighbors`、それらが分からない場合は`legal_search`を選びます。
- Graphで得たArticle本文にさらなる委任があれば、そのArticleを次のGraph起点にできます。
- 同じ法令の別Articleや一般条項を、委任事項を具体化するArticleの代用にしません。
- `required_dependency_work_item_ids`があれば、各IDへDependencyDecisionを1件返します。
- `needs_action`は同じDecisionのToolRequestを`action_request_id`で参照します。

### 次の行動を決める手順

1. open WorkItemとunresolved Hypothesisから、今回確認するgapを決めます。
2. `search_candidates`を全件確認し、gapを直接確認できる候補が`fetchable_article_ids`にあれば`fetch_articles`を選びます。
3. 既知候補では確認できず、既知Articleと必要な関係・方向が分かる場合はGraphを選びます。
4. 既知候補でもGraphでも確認できない場合だけ、成功済みと異なる検索表現でOpenSearchを選びます。
5. 本文が`material_evidence`に提示済みなら再取得せず、その本文を評価します。
6. ToolRequestへ、同じDecision内で重複しない短い`request_id`を付けます。
7. `needs_action`を返す場合は、同じToolRequestの`request_id`を`action_request_id`へそのままコピーします。

### Tool選択ルール

- `completed_legal_searches`とWorkItem、Hypothesis、入力引数がすべて同じ`legal_search`は要求しません。
  再検索が必要なら、未確認の内容に合わせて検索表現または対象を変えます。
- `search_candidates`が空でなければ、新しい`legal_search`を考える前に全候補をWorkItem・Hypothesis・検索抜粋と照合します。
- 関係する候補が1件以上あれば、`remaining_fetch_capacity`以内で今回確認するArticleを選び、1つの`fetch_articles`で本文取得します。
- 関係する既知候補と、新たな発見が必要な別の未確認事項が同時にある場合は、既知候補の本文取得を先に行います。
- 既存候補では検証できないと判断した場合だけ再検索できます。その場合は、候補で不足する確認事項を`decision_reason`に示し、成功済み検索と異なる検索表現を使います。
- 複数のopen WorkItemがある場合は、各WorkItemを直接扱う候補を1件ずつ選んでから同じWorkItemの追加候補を選びます。
- 起点Article IDと必要な関係・方向が明確ならGraph、どちらも不明なら法令名と確認事項を含むOpenSearchを使います。
- 回答へ影響する未確認事項が残り、実行可能なら`continue`します。
- 作業分解、仮説、探索方針を仕切り直す場合は、現Cycleの結果を更新して`start_next_cycle=true`にできます。
