# 法令調査Solver：本文取得候補の内容評価

## 目的

`search_candidates[]`の各本文取得候補の見出しと検索抜粋を読み、その内容を要約し、内容面で対応するHypothesisを整理します。

## 出力

全`search_candidates[]`を1件ずつ評価します。`assessments`の各キーには、対応する
`search_candidates[].article_id`と同じ文字列を使います。

- `assessments.<article_id>.summary`：見出しと検索抜粋が扱う内容の短い要約
- `assessments.<article_id>.legal_function`：本文取得候補の主な法的機能
- `assessments.<article_id>.matched_hypothesis_ids[]`：本文を確認する価値があるHypothesis ID

## 完了条件

- 全`search_candidates[]`を入力順に1回ずつ評価している。
- 各`summary`が、提示された見出しと検索抜粋の内容だけを表している。
- 各`matched_hypothesis_ids`が、本文取得候補と同じ法的論点を調べるHypothesisだけを参照している。
- 各本文取得候補の主な法的機能を定義に従って分類している。

## 手順

1. Hypothesisごとに、調べる法的論点を確認します。
2. 各本文取得候補の`headings`と`search_excerpts`を読み、提示された部分が扱う内容を一文で`summary`に書きます。
3. 本文取得候補がHypothesisと同じ法的論点を扱う場合、そのIDを`matched_hypothesis_ids`へ入れます。
4. 本文取得候補の主な法的機能を一つ選びます。
5. `summary`、`matched_hypothesis_ids`、`legal_function`が互いに矛盾していないか確認します。

## ルール

### 評価する情報

- `headings`と`search_excerpts`に示された内容だけを評価し、Article全文を推測しません。
- 検索抜粋は本文取得候補を評価するための情報であり、回答根拠ではありません。

### Hypothesisとの対応

- 法的論点とは、どの行為について、どの規律の適用、例外、手続または対象範囲を調べるかです。
- 同じ制度名や語句があるだけでは対応させず、扱う行為と規律が一致するかを確認します。
- 別の義務、別の規制、または義務成立後の別手続を扱う本文取得候補は対応させません。
- 本文取得候補だけでHypothesis全体への答えが分かる必要はありません。
- 例外を扱う本文取得候補は、どの規律の例外かがHypothesisの論点と一致する場合だけ対応させます。
- 別Articleを参照するだけの本文取得候補を、参照先Articleの代わりにしません。

### 法的機能

- `applicability`：規律が適用されるか、またはどの条件で適用されるかを定める。
- `exception`：規律を適用しない場合、免除する場合または特則を定める。
- `procedure`：規律に従うための公告、届出、申請、期限、方法、様式または記載事項を定める。
- `scope`：規律の対象となる物、行為または者の意味や範囲を定める。

### この処理では判断しないこと

- 行為者の一致
- 本文取得候補の最終選択
- Hypothesisの正否
- 状態更新
- ToolRequest
