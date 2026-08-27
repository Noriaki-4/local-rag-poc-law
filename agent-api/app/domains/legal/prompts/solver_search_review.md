# 法令調査Solver：本文取得候補の内容整理

## 目的

各`search_candidates[]`の見出しと検索抜粋から、その候補自身が定める内容を整理します。

## 出力

`assessments`の各キーには、対応する`search_candidates[].article_id`と同じ文字列を使います。

- `assessments.<article_id>.summary`：候補が扱う規律の短い要約
- `assessments.<article_id>.legal_function`：候補の主な法的機能

## 完了条件

- 全`search_candidates[]`を入力順に1回ずつ評価している。
- 各`summary`が、提示された見出しと検索抜粋の内容だけを表している。
- 各候補の主な法的機能を定義に従って分類している。

## 手順

1. 各候補の`headings`と`search_excerpts`を読みます。
2. その候補が、どの規律について何を定めるかを一文で`summary`に書きます。
3. 候補の主な法的機能を一つ選びます。

## ルール

- Article全文を推測せず、提示された内容だけを評価します。
- 明示された対象条文番号と法的効果を`summary`に残します。
- 同じ行為を表す語があっても、その行為自体の規律と、その行為を事実・情報として扱う別の規律を区別します。
- 検索抜粋は本文取得候補を整理するための情報であり、回答根拠ではありません。
- `legal_function`は次から一つ選びます。
  - `applicability`：規律の適用条件
  - `exception`：適用除外、免除または特則
  - `procedure`：規律に従うための手続
  - `scope`：規律の対象の意味または範囲
