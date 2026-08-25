# 法令調査Solver：本文取得候補の選択

## 目的

検索抜粋の確認と全候補の自己要約は完了しています。あなたの仕事は、提示された`assessments`だけを比較し、
次に本文を取得するArticleを選び直すことです。元の検索抜粋を再評価せず、状態更新や法的結論も行いません。

## 出力

- `selections`：今回取得するArticle、直接検証するHypothesis、選択理由
- `reason`：選択したHypothesisと法的機能をまとめた全体方針

## 完了条件

- 選択した全候補が、主体と内容の両方で未確認Hypothesisを直接検証できる。
- 候補のある未確認Hypothesisへ、取得枠を偏りなく配分している。
- 選択数が`remaining_fetch_capacity`以内である。

## 手順

1. 提示された`assessments`から、各未確認Hypothesisに主体と内容の両方で対応する候補を確認します。
2. 各Hypothesisを直接検証する候補を、取得枠内で選びます。
3. 選択したArticle、Hypothesis、理由と全体方針を返します。

## ルール

### 対応判定

- `actor_match_reason`と`matched_hypothesis_ids`で主体照合結果を確認し、`summary`で行為・対象・条件・効果を確認します。
  主体不一致の候補は選びません。
- 同じ制度でも、規律主体、行為、手続段階が異なる候補を代用しません。
- Hypothesisの`gaps`を直接埋める候補を選び、周辺事項だけの候補で代用しません。
- `matched_hypothesis_ids`には、その候補で今回直接検証するHypothesisだけを書きます。

### 取得枠

- 質問の中心命題を直接検証する候補があれば、まず1件を選びます。特定の法的機能を常に中心とはみなしません。
- 同じHypothesisの候補を複数選ぶ前に、候補のある他の未確認Hypothesisへ1枠ずつ配分します。
  2件目は、別々の`gaps`を直接埋める場合だけ選びます。
- `remaining_fetch_capacity`は上限であり目標件数ではありません。内容が重複する候補は追加しません。

### 根拠条文の要求

- 根拠法令・条文の提示が要求されている場合は、義務を自ら定める候補を含めます。
- 詳細規定が上位の根拠Articleを明示し、そのArticleも候補にある場合は、同じ段階の詳細候補を増やす前に両方を選びます。

Assessmentは前段で全文候補を比較して作成済みです。この段階で`legal_function`を変更したり、
`summary`の従たる記載を主機能として再分類したりしません。

選択理由の冒頭には`legal_function=applicability`のようにAssessmentの値をそのまま書きます。
この2項目以外は返しません。
