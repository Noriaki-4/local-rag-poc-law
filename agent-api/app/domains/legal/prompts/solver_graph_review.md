# 法令調査Solver：Graph候補の評価

## 目的

`graph_review_batch`の各候補を質問とHypothesisに照らし、次のいずれかに分けます。

- `select`：現在の検証で使う
- `defer`：関係するが、今回は使わない
- `reject`：質問とHypothesisに関係しない

このモードでは法的結論、Hypothesisの判定、CaseStateの更新、調査完了の判断を行いません。

## 出力

- 全候補の`select / defer / reject`判断と理由
- 処理したGraph要求IDとLink ID

## 完了条件

- 全候補と全Linkを1回ずつ評価している。
- 各判断が、未確認事項とRelationの意味・方向に基づいている。
- 本文未取得候補の`select`数が取得枠以内である。

## 入力

- `graph_review_batch`：今回判断する候補です。全件を評価します。
- `graph_review_ledger`：過去の評価結果です。再表示されない過去のLinkもCaseStoreには残っています。
- `review_trigger`：`new_frontier`は新規候補、`re_adopted`は別Hypothesisへの再採用、`new_link`は既存候補への新しい経路です。
- `content_status`：本文取得の状態です。`not_requested`は未要求、`pending`は処理中、`succeeded`は取得済み、`failed`は失敗、`timeout`は時間切れです。関連性を表す値ではありません。
- `graph_review_selection_limit`：今回新たに本文取得できる残りArticle数です。取得済みArticleはこの数に含みません。

## 手順

1. 質問、候補に対応するWorkItem・Hypothesis、Article情報、全`links`と`relations`を読みます。
2. 各候補を`select / defer / reject`のいずれかにします。Relationは手掛かりであり、それだけで関連性を確定しません。
3. 全候補を1回ずつ評価したこと、本文未取得候補の選択数、出力IDを確認して返します。

`new_link`では以前の判断に固定せず、新しいLinkを含む表示済みの関係から判断し直します。

## ルール

### Relationの読み方

- `formal_relation`：`REFERENCES`はfromがtoを明示参照し、`EXPLAINS`はガイドがto Articleを解説します。`outgoing`は起点がfrom、`incoming`は起点がtoです。
- `relation_assertion`：SUBJECTからOBJECTへ向きます。`from_subject`は起点がSUBJECT、`to_subject`は起点がOBJECTです。`predicate`だけでなく、`relationExplanation`と両端の`supportingQuote`も読みます。

| predicate | SUBJECTからOBJECTへの意味 |
|---|---|
| `IMPLEMENTS` | 抽象的な親規定を具体化する |
| `INCORPORATES` | 準用・読替えにより規定を取り込む |
| `USES_DEFINITION` | 定義を利用する |
| `EXCEPTION_TO` | 一般規定に対する例外を定める |
| `OVERRIDES` | 別の規定を優先的に排除・修正する |

`USES_DEFINITION`は名前だけで選びません。Hypothesisがその語の意味・範囲に依存する場合だけ定義側を選び、定義側を起点にする場合は、その定義の適用先を調べる必要がある場合だけ利用側を選びます。説明が足りなければ`defer`し、後続のIntegrationに追加検索の判断を残します。

### 候補の選択

1. WorkItem・Hypothesis・`gaps`から、まだ本文で確認できていない事項とその法的な役割（条件、対象範囲、例外、手続、期間等）を特定します。
2. 各候補が何を定めるArticleかを、見出しとRelationの両端の引用・説明から特定します。候補ごとの`reason`にも、その内容を具体的に書きます。
3. 未確認事項と同じ役割を直接扱う候補から順に並べます。制度名が共通するだけで、異なる役割を扱う候補を優先しません。「規制によらずにできる場合」「適用されない場合」は、適用除外・例外を扱う候補と照合します。
4. 本文未取得の関連候補は、先頭から`graph_review_selection_limit`件を`select`し、残りを`defer`にします。本文取得済みの関連候補は上限に数えず`select`できます。
5. 現在のWorkItem・Hypothesisに関係しない候補だけを`reject`にします。

`select`は現在の検証で使う判断であり、法的内容の確定ではありません。`content_status`が`not_requested`、`failed`、`timeout`の`select`だけをProgramが本文取得します。`pending`または`succeeded`でも、現在の検証で使うなら`select`にします。同じArticleの重複は1件と数えます。

本文未取得の関連候補A・Bがあり、実効上限が1なら、優先する1件を`select`、残りを`defer`にします。実効上限が0の場合、本文未取得の関連候補はすべて`defer`にします。`graph_review_ledger`の取得可能な保留候補も、必要なら同じ上限内で`select`できます。

### 出力ID

- `frontier_decisions`に、`graph_review_batch`の全`frontier_item_id`を1回ずつ含めます。
- `graph_request_ids`は`required_graph_review_request_ids`、`reviewed_link_ids`はbatch内の全`link_id`をそのまま返します。IDを生成・修正しません。
- 候補ごとの`reason`には、候補が扱う法的な役割と未確認事項に一致するかを一文で書きます。「関連性が高い」「優先度が低い」だけでは不十分です。
- そのほかの項目はProvider schemaに従います。Tool要求や状態更新は返しません。選択した本文未取得Articleの取得はAgentLoopが行います。
