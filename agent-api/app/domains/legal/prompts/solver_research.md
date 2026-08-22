## Researchモード

### 実行手順

1. 元の質問をWorkItemへ分解します。
2. 各WorkItemへ検証可能なHypothesisを置きます。
3. Hypothesisごとに最初の探索方法と検索表現を選びます。
4. open WorkItem、Hypothesis、ToolRequestを含むSolverDecisionを返します。

### 判断ルール

#### 作業分解と仮説

- 初回判断では、元の質問が明示的に求める観点だけを、重複しないWorkItemへ分解します。
- WorkItem数を減らすために、適用要件、数値基準、例外、義務・手続など
  別の根拠で検証する観点を束ねないでください。
- 各WorkItemに、取得本文で独立に検証できるHypothesisを置きます。
- 法令名や条文番号がまだEvidenceにない場合、Hypothesisへ推測で記載せず、
  確認したい法的内容を記載します。

#### 最初の探索

- まだArticle IDが分からない事項はlegal_searchで発見します。
- 検索語は質問の表現をそのまま繰り返さず、制度名と確認する観点を、
  条文に現れやすい法令表現へ言い換えます。
- 例えば「必要な手続」を、観点に応じて「公告」「届出」「通知」「提出」
  「期間」「様式」などへ具体化します。ただし質問にない観点を追加しません。
- Article IDが判明し、Hypothesisに対応する関係の種類と方向を説明できる場合は、
  legal_graph_neighborsを使えます。
- 複数の独立した観点がある場合は、同じ一般検索を繰り返さず、
  観点ごとの検索語で並行して探索してください。

#### Researchモードの終了

- 初回時点の根拠が本当に十分な場合を除き、次に検証するopen WorkItemと
  ToolRequestを返してcontinueします。
