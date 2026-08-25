# 法令調査Solver：検索候補の規律主体分類

## 目的

候補の自己要約は完了しています。各候補が規律する行為者を、Hypothesisの主体構造と照合します。
法的機能、条件、効果、候補の採否は判断しません。

## 出力

- 各候補の規律主体、質問内での役割、主体面で対応するHypothesis ID

## 完了条件

- 全候補を1回ずつ分類している。
- 規律される行為者と、条件中の所有者・発行者等を区別している。
- 主体面だけを判断し、内容評価や候補選択をやり直していない。

## 手順

1. 各Hypothesisの`actor_scope`と`actor_relation`を確認します。
2. 各候補の`headings`と`summary`から、義務、禁止、許可等の効果を受ける行為者を`regulated_actor`へ書きます。
   条件中に現れる所有者、発行者等を、規律される行為者と取り違えません。
   行為者を規律せず、用語、人数、割合、対象範囲だけを定める候補は主体中立とします。
3. その行為者の質問内での役割を`regulated_actor_role`へ分類します。
   呼称の文字列一致ではなく、同じ行為を行う法令上の役割かで判断します。例えば、質問で公開買付けを
   行う者は、規定中の「公開買付者」と同じ行為者です。
4. 主体だけを基準に、照合可能なHypothesis IDを`matched_hypothesis_ids`へ入れます。行為、対象、条件、効果は前段で別に判定済みです。

## 分類値

- `hypothesis_actor`：Hypothesisの行為者
- `target_associated_actor`：行為対象の所有者、発行者、所属先等
- `actor_neutral`：行為者を規律せず、用語、数、対象範囲を定める候補
- `other`：それ以外の主体
- `unknown`：要約から確定できない主体

「AのBをCしたい」で行為者とAが`different`の場合、「A自身によるC」は
`target_associated_actor`、「A以外の者によるC」は`hypothesis_actor`です。

## ルール

- `actor_scope`に書かれた行為者と比較対象を読み、`actor_relation`で両者の同一性を確認します。
- 呼称の文字列一致ではなく、同じ法的行為を行う役割かで判断します。
- `actor_neutral`では内容面で対応済みのHypothesis IDを保持します。
- `target_associated_actor`、`other`、`unknown`は主体一致として扱いません。
- この処理では候補を選びません。
