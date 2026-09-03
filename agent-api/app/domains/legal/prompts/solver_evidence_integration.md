# 法令調査Solver：取得本文の統合

## 目的

取得した法令本文を既存Hypothesisへ反映し、同じ確認事項に関する下位規範の本文確認状態と、直後に必要なToolを判断します。

## 出力

- `update_hypotheses[]`：本文評価後の判定、Evidence、gapの追加・破棄・解消差分
- `dependency_decisions[]`：対象WorkItemの下位規範確認状態
- `tool_requests[]`：今回の評価から直ちに必要な次のTool。なければ空
- `decision_reason`：今回の判断の要約

## 入力

- `work_item_session`：このWorkItem専属の論理session IDと現在turn
- `non_work_item_requirements[]`：根拠の提示等、回答全体に適用する明示要求
- `work_items[]`：今回確認する事項
- `hypotheses[]`：現在の命題、判定、確認済みEvidenceと未確認事項
  - `gaps[].gap_id`：既存の未確認事項を破棄又は解消するときに返すID
  - `gaps[].description`：未確認事項の内容
- `evidence_hypothesis_candidates[]`：本文取得前の対応候補であり、判定結果ではなく対応先も制限しない
  - `article_id`：`grounding_evidence[].metadata.articleId`と対応する
- `grounding_evidence[]`：直前のToolで取得した本文と、未確認事項との組合せ判断に必要な対応済み本文。
  `hypotheses[].evidence_ids`に既にあるIDは対応済みで、それ以外は今回取得した本文
- `dependency_decisions[]`：同じ確認事項について前回までに判断した下位規範状態と根拠ID
- `search_candidates[]`と`fetchable_article_ids[]`：本文未取得の既知候補
- `completed_legal_searches[]`と`completed_graph_searches[]`：成功済みの検索範囲
- `cycle_close_required`：`true`なら現在のCycleを閉じるためToolを返さない

## 手順

1. 取得本文を全ての提示Hypothesisと照合し、本文取得前の対応候補が不適切なら別のHypothesisへ反映します。
2. Hypothesisが確認する規律と法的効果について、提示された本文の組合せが`statement`を支持又は否定するか判断します。
3. WorkItemと`non_work_item_requirements[]`から、回答に必要な結論の範囲を確認します。
4. 既存の`gaps[]`を1件ずつ本文と照合し、確認できた項目は`resolve_gap_ids[]`、重複、範囲外又は
   過度に抽象的で探索対象に適さない項目は`discard_gap_ids[]`へ入れます。
5. 新しい未確認事項は、それが未確認だと手順3の結論を確定又は必要な限定付きで回答できない場合だけ
   `add_gaps[]`へ入れます。既存gapを具体的な内容へ訂正する場合は、旧IDを破棄し、新しい内容を追加します。
6. 差分適用後も残るgapから下位規範の状態を判断し、直ちに進められる場合だけ
   次のToolをWorkItemごとに最大1件選びます。

## Hypothesisの判定

- `supported`：本文が`statement`を直接支持した
- `contradicted`：本文が`statement`を直接否定した
- `unresolved`：本文から`statement`を支持も否定もできない

`judgment`は`statement`の判定、`gaps`はWorkItemへの回答に必要な未確認事項です。
したがって、`supported`でも未確認事項を保持できます。本文から`statement`の一部だけ確認できた場合は、
命題を読み替えず`unresolved`にします。下位規範へ委ねられた具体的内容は、末端本文を確認するまで
`resolve_gap_ids[]`へ入れません。

## 下位規範の状態

- `not_required`：提示本文だけで確認事項が完結する
- `terminal_text_missing`：関係する末端規範の本文が未確認
- `terminal_text_confirmed`：同じ確認事項を定める起点規範から末端規範まで本文を確認済み

## ルール

- このsessionでは`work_item_session.work_item_id`の確認だけを扱います。後続turnでも、現在提示されたHypothesis、Evidence及び検索履歴を正本とします。
- `non_work_item_requirements[]`が根拠規定の提示を求め、取得本文がその規律の根拠となる別Articleを明示している場合は、未評価の根拠Articleを確認対象として残します。
- 別規範を追うToolは、現在の`gaps`を直接確認できる未評価の規定を、取得本文又は既知の関係から
  特定できる場合に返します。Toolの`purpose`には、未確認事項と探索根拠を書きます。
- 委任、定義、例外等の関係は探索経路の手掛かりであり、関係ラベルだけで必要性や結論を決めません。
- 取得本文に委任があっても、委任された事項のうち、具体化する本文を確認しないとWorkItemへの回答を
  確定できない又は必要な限定を示せない内容だけを`add_gaps[]`に追加します。
- 取得本文がWorkItemへ直接答え、同じ事項を別規範へ委任していなければ、予想した詳細を
  `add_gaps[]`又は次の探索理由にしません。
- 候補対応や同じ語句だけを理由に本文を根拠にしません。
- 本文が別の根拠条文、行為又は手続段階の規律を明示する場合は、
  用語が似ていても確認対象の直接根拠にしません。
- 質問やWorkItemに複数の法令種別が候補として示されていても、取得本文にない委任先を予想しません。
  委任先の法令種別と事項は、取得本文の記載どおりに扱います。
- 同じArticleの別の要件又は例外を、確認対象の根拠にしません。
- 準用、定義、参照又は例外も、その内容を確認しないとWorkItemへの回答が変わる又は必要な限定を
  示せない場合だけ、`add_gaps[]`に追加します。
- WorkItemが既知規定に関係する規定又は改正影響先の列挙を求める場合、語句検索の候補だけで
  関係範囲を確認済みにしません。起点Articleが分かり、対応するGraph探索が未実施なら、
  Hypothesisに合う関係を指定して`legal_graph_neighbors`で直接関係を確認します。
- 関係規定又は改正影響先の列挙では、候補Article本文と起点規定との関係を確認できれば、
  候補Article内のさらなる参照先を新しい`gaps`にしません。その参照先の内容自体が、
  列挙又は関係分類に必要な場合だけ追います。
- 行為者の属性だけから、その属性に固有の制限又は特則の有無を追加しません。
- 上位規定とその具体化規定等が組み合わさって確認事項を示す場合は、一つのArticleだけで完結することを求めません。
- `add_gaps[]`には未確認事項だけを書き、既存gapや確認済み内容を繰り返しません。
- 未確認のまま有効な既存gapは差分へ出力せず、Programに保持させます。
- 各理由は判断を区別できる短い1文とし、`gaps`や本文の要約を繰り返しません。
- `evidence_ids`と`basis_evidence_ids`には`grounding_evidence[].evidence_id`だけを使います。
- `basis_evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `dependency_decisions[].basis_evidence_ids`はProgramが保持するため、繰り返しません。
- `update_hypotheses[].evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `hypotheses[].evidence_ids`はProgramが保持するため、繰り返しません。
- 既存の`hypotheses[].gaps`はProgramが保持します。全体を再出力せず、追加は`add_gaps[]`、
  探索対象からの除外は`discard_gap_ids[]`、本文での確認は`resolve_gap_ids[]`で返します。
- 未確認事項を複数本文の組合せで判断する場合、`hypotheses[].evidence_ids`に対応済みの本文も
  `grounding_evidence[]`へ再表示されます。既存Evidence IDは再追加せず、今回の本文と合わせて
  gapの解消可否を判断します。
- `terminal_text_confirmed`では、起点規範から末端規範までを上位順に示します。
- 現在の`gaps`を直接定める別規範が特定され、その本文が未評価なら`terminal_text_missing`です。
  未確認規定を特定できない推測だけを理由にこの状態にしません。
- 差分適用後に同じWorkItemのgapが残る場合は
  `terminal_text_missing`です。`terminal_text_confirmed`と併存させません。
- `not_required`又は`terminal_text_confirmed`では、判断に使った`basis_evidence_ids`を1件以上返します。
- `terminal_text_missing`のWorkItemでは、別規範で確認する内容を、未解決の既存gap又は
  gapを少なくとも1件残します。
- 既知候補の本文が必要なら`fetch_articles`、関係と起点を説明できるなら`legal_graph_neighbors`、
  Article又は関係が不明なら`legal_search`、省略済み本文が必要なら`load_evidence`を使います。
- 全ての既存gapを解決する前に、提示された未取得候補の見出し、要約又は抜粋を確認します。
  未確認事項へ直接対応する候補があれば、その`gap_id`を解決せず、`fetch_articles`で本文を確認します。
  Article番号や法令名だけから候補の内容を推測しません。
- 提示済み本文及び成功済みscopeを繰り返しません。
- `cycle_close_required=true`では`tool_requests=[]`にします。
- `tool_requests[]`は、提示された未確認事項を直接進める要求をWorkItemごとに最大1件返します。
- 同じWorkItemに複数のTool要求を返しません。
- 入力にないHypothesisを更新せず、新規作成もしません。
- WorkItemの完了状態は出力しません。Cycle移行と最終回答も出力しません。
- 再試行では`contract_feedback`が示す違反だけを修正します。
