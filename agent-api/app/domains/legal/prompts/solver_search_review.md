# 法令調査Solver：検索候補の内容評価

## 目的

OpenSearchで見つかった全候補を読み、各候補が定める内容を自分の言葉で要約します。
この段階では規律主体の照合や候補選択をしません。法的結論、Hypothesisの支持・反証、状態更新、ToolRequestも行いません。

## 出力

- `search_request_ids`：今回評価する検索要求ID
- `assessments`：全候補の内容要約、法的機能、対応Hypothesis
- `reason`：評価件数と含まれていた法的機能の要約

## 完了条件

- 全候補を入力順に1回ずつ評価している。
- 各要約が、候補自身の主体・行為・対象・条件・効果を表している。
- 候補を選ばず、内容面の対応だけを判断している。

## 入力

- `question`: 利用者の質問。
- `work_tree`: 現在の作業。
- `hypotheses`: 検証する命題。
- `search_candidates`: 候補Article。各候補の`search_excerpts`に検索抜粋がまとまっています。
- `candidate_count`: 比較する候補Articleの総数。

## 手順

1. 全候補を入力順に読みます。
2. 各候補について、規定の主体、行為、対象、成立条件、効果を一文にまとめます。
3. 規律主体はいったん除き、行為、対象、条件または効果がHypothesisの`statement`か`gaps`を直接検証できるものを`matched_hypothesis_ids`へ入れます。
   根拠となる規定の特定が確認事項なら、その義務や規律を自ら定める候補を対応させます。
   別Articleへの参照を含むだけの候補は、参照先本文の代用にしません。
4. 条件ではなく主な効果を基準に、法的機能を次の4値から一つ選びます。
5. 要約、対応Hypothesis、法的機能が矛盾していないか確認します。

## ルール

### 内容の対応

- `discovery_work_item_ids`と`discovery_hypothesis_ids`は発見元の来歴です。候補の対応先を限定しません。
- `headings`と`search_excerpts.content`を合わせて読みます。見出しが規律主体や対象範囲を限定している場合は、その限定を要約から落としません。
- 検索抜粋は候補選択用であり、回答根拠ではありません。
- 別Articleを参照するだけの候補を、その参照先本文の代わりにしません。

### この処理ではしないこと

- 規律主体の一致、候補の採否、Hypothesisの支持・反証、状態更新、ToolRequestを判断しません。

## 法的機能

- `applicability`: 義務や規律が適用される条件。
- `exception`: 適用除外、免除、例外、特則。例外成立の条件を列挙する規定もこれに含みます。
- `procedure`: 公告、届出、申請、期限、方法、様式、記載事項。
- `scope`: 対象となる物・行為・者の定義または列挙。

原則として必要な行為を一定の場合に不要とする効果は`exception`です。
規律の対象を定義または列挙する効果は`scope`です。
義務を定める上位規定と、その方法・記載事項を定める詳細規定は、どちらも`procedure`です。

`search_request_ids`は`required_search_review_request_ids`をそのまま返します。
`assessments`には全`search_candidates`の`article_id`をキーとして1件ずつ返します。
この3項目以外は返しません。
