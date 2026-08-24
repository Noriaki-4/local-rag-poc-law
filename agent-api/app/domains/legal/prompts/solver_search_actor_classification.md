# 法令調査Solver：候補の規律主体分類

候補の自己要約は完了しています。各候補が規律する行為者を、Hypothesisの主体構造と照合します。
法的機能、条件、効果、候補の採否は判断しません。

## 手順

1. 各Hypothesisの`actor_scope`と`actor_relation`を確認します。
2. 各候補の`headings`と`summary`から、規律される行為者を`regulated_actor`へ書きます。見出しが「Aによる」「A以外の者による」のように主体を限定する場合、その限定を優先します。
3. その行為者の質問内での役割を`regulated_actor_role`へ分類します。
4. 主体だけを基準に、照合可能なHypothesis IDを`matched_hypothesis_ids`へ入れます。行為、対象、条件、効果は前段で別に判定済みです。

## 分類値

- `hypothesis_actor`：Hypothesisの行為者
- `target_associated_actor`：行為対象の所有者、発行者、所属先等
- `other`：それ以外の主体
- `unknown`：要約から確定できない主体

「AのBをCしたい」で行為者とAが`different`の場合、「A自身によるC」は
`target_associated_actor`、「A以外の者によるC」は`hypothesis_actor`です。

この処理では候補を選びません。
