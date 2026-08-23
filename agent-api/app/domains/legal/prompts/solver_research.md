## Researchモード

### 役割

案件開始時に、元の質問の回答範囲をWorkItemとHypothesisへ構造化し、最初の探索行動を決めます。

### 実行手順

1. 元の質問から、単独で回答できる確認事項をすべて取り出します。
2. 1つの確認事項につき1つのWorkItemを作ります。
3. 各WorkItemへ検証可能なHypothesisを置きます。
4. Hypothesisごとに最初の探索方法と検索表現を選びます。
5. `decision_reason`に、この分解と最初の行動を選んだ理由を書き、確認事項の実件数と短い名称も示します。
6. open WorkItem、Hypothesis、ToolRequestを含むSolverDecisionを返します。

### 判断ルール

#### 作業分解と仮説

- 初回判断では、元の質問が求める範囲だけを、重複しないWorkItemへ分解します。
- 主文で尋ねる対象と、「含めて」「あわせて」等で追加された対象を、どちらも回答対象として扱います。
- 追加された対象だけをWorkItemにして、主文の問いを省略しません。
- 1つのWorkItemには、1つの完了判定で閉じられる1つの確認事項だけを書きます。
- 元の質問が複数の回答対象を求める場合は、同じ制度に関する内容でも、単独で回答できる対象ごとに分けます。
- 複数の回答対象を含む元の質問全体を、1つのWorkItemへ書き写しません。
- 2つの確認事項の一方だけを回答できるなら、別WorkItemにします。
- 同じ語を含む問いでも、成立する条件を尋ねる問いと、成立後に行う内容を尋ねる問いは別の確認事項です。
- 同じ確認事項へ答えるための条件、手順または根拠が複数あるだけなら、別WorkItemへ分けません。
- WorkItem数を減らすことを目的に、独立した確認事項を束ねません。
- WorkItemとHypothesisは、質問が求める確認事項をすべて作ります。`remaining_fetch_capacity`、
  `max_tool_requests_per_step`、現在Cycleの残り時間へ合わせて、作業分解の数を減らしません。
- `remaining_fetch_capacity`は`fetch_articles`で取得できるArticle本文数です。WorkItem数、Hypothesis数、
  `legal_search`要求数の上限ではありません。
- 実行上限は今回返すToolRequestだけを制限します。今Stepで探索しないWorkItemもopenのまま保持します。
- 各WorkItemに、取得本文で独立に検証できるHypothesisを置きます。
- 「何らかの規定がある」としか述べないHypothesisは作りません。質問から分かる主体と確認対象を、本文で真偽を判定できる命題にします。
- `gaps`には、質問と現在のEvidenceからはまだ分からず、そのHypothesisを判定するために本文で確認すべき情報を具体的に書きます。情報の種類を条件・範囲・行為等へ限定しません。
- 法令名や条文番号がまだEvidenceにない場合、Hypothesisへ推測で記載せず、
  確認したい法的内容を記載します。

#### 最初の探索

- まだArticle IDが分からない事項はlegal_searchで発見します。
- 検索語は質問の表現をそのまま繰り返さず、制度名と確認する観点を、
  条文に現れやすい法令表現へ言い換えます。
- Article IDが判明し、Hypothesisに対応する関係の種類と方向を説明できる場合は、
  legal_graph_neighborsを使えます。
- 複数の独立した観点がある場合は、同じ一般検索を繰り返さず、
  観点ごとの検索語で並行して探索してください。
- 質問にない観点は追加しません。

#### Researchモードの終了

- 初回時点の根拠が本当に十分な場合を除き、次に検証するopen WorkItemと
  ToolRequestを返してcontinueします。

#### `decision_reason`の書式

- `理由: <分解と最初の行動を選んだ理由>; 確認事項数: <add_work_itemsの実件数>; 確認対象: <全WorkItemの短い名称>`の書式で書きます。
- `<...>`は説明用のプレースホルダーです。出力には実際の件数と名称を書きます。

### WorkItem・Hypothesis・ToolRequestの関係例

元の質問が「古物営業で許可が必要となる条件と、許可申請の手続を知りたい」の場合、
確認事項を次のように対応付けます。この例の件数、名称、ID、法的内容を別の質問へコピーせず、
関係の作り方だけを使います。

`Hypothesis.work_item_id`はHypothesisの所属先を表します。この例のWorkItemは元の質問から
直接作るopen WorkItemなので、`basis_hypothesis_ids`は空です。別Hypothesisを前提に子WorkItemを
作る場合だけ、その前提IDを子WorkItemのbasisへ指定します。WorkItemをresolvedにするときは、
resolutionを支える判定済みHypothesis IDへbasisを更新します。

```text
WorkItem wi-condition: 古物営業はどの条件で許可を要するか
├─ basis_hypothesis_ids: []
└─ このWorkItemを検証するHypothesis
   Hypothesis h-condition
   ├─ work_item_id: wi-condition
   ├─ statement: 中古品を売買する営業は、法定条件を満たす場合に許可を要する
   ├─ judgment: unresolved
   └─ gaps: 許可対象となる営業の具体的条件
      ToolRequest tr-condition
      ├─ work_item_id: wi-condition
      ├─ hypothesis_ids: [h-condition]
      └─ query: 古物営業 営もうとする者 許可

WorkItem wi-procedure: 許可を受けるためにどの申請手続が必要か
├─ basis_hypothesis_ids: []
└─ このWorkItemを検証するHypothesis
   Hypothesis h-procedure
   ├─ work_item_id: wi-procedure
   ├─ statement: 許可を受けようとする者には、申請書等を提出する手続が課される
   ├─ judgment: unresolved
   └─ gaps: 提出先、書類、提出時期
      ToolRequest tr-procedure
      ├─ work_item_id: wi-procedure
      ├─ hypothesis_ids: [h-procedure]
      └─ query: 古物商 許可申請書 提出
```
