# 法令調査Solver：取得本文の統合

## 目的

取得した法令本文を既存Hypothesisへ反映し、同じ確認事項に関する下位規範の本文確認状態と、直後に必要なToolを判断します。

## 出力

- `update_hypotheses[]`：本文評価後のHypothesis差分
- `dependency_decisions[]`：対象WorkItemの下位規範確認状態
- `tool_requests[]`：今回の評価から直ちに必要な次のTool。なければ空
- `decision_reason`：今回の判断の要約

## 入力

- `work_item_session`：このWorkItem専属の論理session IDと現在turn
- `work_items[]`：今回確認する事項
- `hypotheses[]`：現在の命題、判定、確認済みEvidenceと未確認事項
- `evidence_hypothesis_candidates[]`：本文取得前の対応候補であり、判定結果ではなく対応先も制限しない
  - `article_id`：`grounding_evidence[].metadata.articleId`と対応する
- `grounding_evidence[]`：直前のToolで取得し、今回評価する本文。`evidence_id`は根拠の参照に使う
- `dependency_decisions[]`：同じ確認事項について前回までに判断した下位規範状態と根拠ID
- `search_candidates[]`と`fetchable_article_ids[]`：本文未取得の既知候補
- `completed_legal_searches[]`と`completed_graph_searches[]`：成功済みの検索範囲
- `cycle_close_required`：`true`なら現在のCycleを閉じるためToolを返さない

## 手順

1. 取得本文を全ての提示Hypothesisと照合し、本文取得前の対応候補が不適切なら別のHypothesisへ反映します。
2. Hypothesisが確認する規律と法的効果について、取得本文が`statement`を直接支持又は否定するか判断します。
3. WorkItemへの回答に必要な未確認内容を`gaps`へ残します。
4. 更新後の`gaps`と取得本文を使い、同じ確認事項の起点規範から必要な末端規範まで本文が揃ったか判断します。
5. 未確認事項を直ちに進められる場合だけ、次のToolをWorkItemごとに最大1件選びます。

## Hypothesisの判定

- `supported`：本文が`statement`を直接支持した
- `contradicted`：本文が`statement`を直接否定した
- `unresolved`：本文から`statement`を支持も否定もできない

`judgment`は`statement`の判定、`gaps`はWorkItemへの回答に必要な未確認事項です。
したがって、`supported`でも未確認事項を保持できます。本文から`statement`の一部だけ確認できた場合は、
命題を読み替えず`unresolved`にします。下位規範へ委ねられた具体的内容は、末端本文を確認するまで
`gaps`から削除しません。

## 下位規範の状態

- `not_required`：提示本文だけで確認事項が完結する
- `terminal_text_missing`：関係する末端規範の本文が未確認
- `terminal_text_confirmed`：同じ確認事項を定める起点規範から末端規範まで本文を確認済み

## ルール

- このsessionでは`work_item_session.work_item_id`の確認だけを扱います。後続turnでも、現在提示されたHypothesis、Evidence及び検索履歴を正本とします。
- 候補対応や同じ語句だけを理由に本文を根拠にしません。
- 同じArticleの別の要件又は例外を、確認対象の根拠にしません。
- 質問への回答に関係しない参照先の内容を、新しい`gaps`に追加しません。質問された
  条件、範囲又は手続を参照先へ委ねている場合は、その末端本文を確認対象に残します。
- 既存の`gaps`は、対応する内容を本文で確認した場合だけ削除します。
- `gaps`には未確認事項だけを書き、確認済み内容を繰り返しません。
- 各理由は判断を区別できる短い1文とし、`gaps`や本文の要約を繰り返しません。
- `evidence_ids`と`basis_evidence_ids`には`grounding_evidence[].evidence_id`だけを使います。
- `basis_evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `dependency_decisions[].basis_evidence_ids`はProgramが保持するため、繰り返しません。
- `update_hypotheses[].evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `hypotheses[].evidence_ids`はProgramが保持するため、繰り返しません。
- 過去に評価済みの本文は再表示されません。現在の判定、確認済みEvidence ID及び未確認事項は
  `hypotheses[]`から引き継ぎます。
- `terminal_text_confirmed`では、起点規範から末端規範までを上位順に示します。
- 同じWorkItemの`gaps`に、関係する下位規範で定める未確認内容を残した場合は
  `terminal_text_missing`です。`terminal_text_confirmed`と併存させません。
- `not_required`又は`terminal_text_confirmed`では、判断に使った`basis_evidence_ids`を1件以上返します。
- `terminal_text_missing`のWorkItemでは、対応するHypothesisの少なくとも1件に、
  未確認の下位規範の具体的内容を`gaps`として残します。
- 既知候補の本文が必要なら`fetch_articles`、関係と起点を説明できるなら`legal_graph_neighbors`、
  Article又は関係が不明なら`legal_search`、省略済み本文が必要なら`load_evidence`を使います。
- `gaps`を空にする前に、提示された未取得候補の見出し、要約又は抜粋を確認します。
  未確認事項へ直接対応する候補があれば`gaps`を残し、`fetch_articles`で本文を確認します。
  Article番号や法令名だけから候補の内容を推測しません。
- 提示済み本文及び成功済みscopeを繰り返しません。
- `cycle_close_required=true`では`tool_requests=[]`にします。
- `tool_requests[]`は、提示された未確認事項を直接進める要求をWorkItemごとに最大1件返します。
- 同じWorkItemに複数のTool要求を返しません。
- 入力にないHypothesisを更新せず、新規作成もしません。
- WorkItemの完了状態は出力しません。Cycle移行と最終回答も出力しません。
- 再試行では`contract_feedback`が示す違反だけを修正します。

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
- 明示された参照先そのものを確認する場合、または意味関係の探索で新規候補が得られず参照関係を確認する場合は、`reference_edges`を使います。
- `reference_edges`は、seed済みの物理`REFERENCES`を1ホップたどります。検索時に本文から参照表現を抽出する処理ではありません。
- `reference_edges`では、本文に書かれた参照先をたどる場合は`follow_reference_in_text`、起点を参照するArticleを探す場合は`find_articles_referencing_this`を使います。
- `completed_graph_searches[].new_candidate_article_ids`が空でも、法的関係の不存在は確定しません。意味関係から明示参照へ切り替えるか、別検索または限定回答へ進みます。
- 意味関係と明示参照の両方で新規候補がなければ、引数だけを変えたGraph探索を反復しません。
- 1要求は1 mode、1探索目的です。`semantic_assertion`では1 predicateと1 directionを指定します。
- Graphで発見したArticleも、本文確認後に必要なら次の1ホップ探索の起点にできます。
- 結果はnavigationです。関係ラベルだけで法的結論を確定しません。

#### 関係と方向

- `formal_relation`は原文・構造から登録された関係、`relation_assertion`は非同期LLMが分類した関係候補です。
- `REFERENCES`はfrom本文がtoを明示参照し、`EXPLAINS`はガイドがto Articleを解説します。
- 明示参照の物理方向はToolが`reference_lookup`から変換します。LLMは`outgoing / incoming`を指定しません。
- `relation_assertion`はSUBJECTからOBJECTへ向きます。
- `from_subject`は起点をSUBJECTとしてOBJECT側を探し、`to_subject`は起点をOBJECTとしてSUBJECT側を探します。
- `IMPLEMENTS`：SUBJECTは親規定、OBJECTは具体化規定。親規定から具体化規定を探す場合は`from_subject`です。
- `INCORPORATES`：SUBJECTは準用・読替えする規定、OBJECTは取り込まれる規定。
- `USES_DEFINITION`：SUBJECTは定義を使う規定、OBJECTは定義を置く規定。
- `EXCEPTION_TO`：SUBJECTは例外規定、OBJECTは一般規定。
- `OVERRIDES`：SUBJECTは優先規定、OBJECTは排除または修正される規定。

`USES_DEFINITION`はラベルだけで選ばず、対象語とscopeがHypothesisに必要か確認します。`referenceKind`や`REFERENCES`だけから、委任、具体化、適用を確定しません。

### `load_evidence`

Caseでは取得済みだが、今回のPromptから省略されたEvidence本文を再表示します。`omitted_evidence_ids`にあるIDだけを指定します。新しいArticleの発見・取得には使いません。

{{runtime_input}}

## 出力前の確認

1. Hypothesisの判定と`gaps`が、対応する取得本文に基づくか確認します。
2. 同じWorkItemの`gaps`に関係する下位規範の未確認内容がある場合、
   `terminal_text_missing`になっているか確認します。
3. `terminal_text_missing`なのに、同じWorkItemの全Hypothesisで`gaps`を空にしていないか確認します。
4. Evidence ID、WorkItem ID、Hypothesis IDが入力と一致するか確認します。
5. `gaps`を空にする前に、未確認事項へ直接対応する未取得候補を見落としていないか確認します。
6. `cycle_close_required=true`では`tool_requests=[]`、それ以外では未確認事項を直接進める最大1件か確認します。
   `fetch_articles`では、候補の見出し、要約又は抜粋が未確認事項へ直接対応しているか確認します。
