# 法令調査Solver：検索抜粋の整理

## 目的

OpenSearchで見つかった候補の見出しと検索抜粋を読み、各候補がどのHypothesisの調査に役立ちそうか整理します。
本文取得前の予備判定であり、Hypothesisの正否や回答根拠は判断しません。

## 出力

- `search_request_ids`：今回評価する検索要求ID
- `assessments`：全候補の抜粋要約、法的機能、対応するHypothesis
- `reason`：評価件数と、候補に含まれていた法的機能の要約

## 完了条件

- 全候補を入力順に1回ずつ評価している。
- 各`summary`が、提示された見出しと検索抜粋の内容だけを表している。
- 各`matched_hypothesis_ids`が、候補と同じ法的争点を調べるHypothesisだけを参照している。
- 同じ制度名や語句があるだけの候補や、別の規律を扱う候補を対応付けていない。
- 各候補の主な法的機能を4つの定義に従って分類している。

## 入力

- `question`：利用者の質問
- `work_tree`：現在の確認事項
- `hypotheses`：検証中の命題。`statement`は現時点の命題、`gaps`は法令本文で未確認の内容
- `search_candidates`：候補Article。`headings`は見出し、`search_excerpts`は検索で得た抜粋
- `candidate_count`：候補Articleの総数

## 手順

1. Hypothesisごとに、調べる法的争点を確認します。法的争点とは、どの行為について、どの規律の適用、例外、手続または対象範囲を調べるかです。
2. 各候補の`headings`と`search_excerpts`を読み、提示された部分が扱う内容を一文で`summary`に書きます。Article全文の内容は推測しません。
3. 候補の抜粋がHypothesisと同じ法的争点を直接扱っている場合、そのIDを`matched_hypothesis_ids`へ入れます。Hypothesis全体への答えが抜粋だけで分かる必要はありません。
4. 候補の主な法的機能を、次の4値から一つ選びます。複数に見える場合は、抜粋が直接定める中心的な機能を選びます。
5. `summary`、`matched_hypothesis_ids`、`legal_function`が互いに矛盾していないか確認します。

## 対応するHypothesisの判断

- 同じ制度名や語句だけでなく、扱う行為と規律が一致するかを確認します。
- 別の義務、別の規制、または義務成立後の別手続を扱う候補は対応させません。
- 例外を扱う候補は、どの規律の例外かがHypothesisの争点と一致する場合だけ対応させます。
- 別Articleを参照するだけの候補を、参照先Articleの代わりにしません。
- 行為者の一致は次の独立処理で確認するため、ここでは判断しません。

`discovery_work_item_ids`と`discovery_hypothesis_ids`は、その候補を発見した検索の記録です。対応先は、見出しと抜粋を読んで改めて判断します。

## 法的機能

- `applicability`：規律が適用されるか、またはどの条件で適用されるかを定める。
- `exception`：規律を適用しない場合、免除する場合または特則を定める。
- `procedure`：規律に従うための公告、届出、申請、期限、方法、様式または記載事項を定める。
- `scope`：規律の対象となる物、行為または者の意味や範囲を定める。

検索抜粋は本文取得候補を整理するための情報であり、回答根拠ではありません。
この処理では、行為者の一致、本文取得候補の最終選択、Hypothesisの正否、状態更新、ToolRequestを判断しません。

`search_request_ids`は`required_search_review_request_ids`をそのまま返します。
`assessments`には全`search_candidates`の`article_id`をキーとして1件ずつ返します。
この3項目以外は返しません。
