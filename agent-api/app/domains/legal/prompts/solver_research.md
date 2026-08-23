## 現在の作業：Research

## 目的

案件開始時に、元の質問の回答範囲をWorkItemとHypothesisへ構造化し、最初の探索行動を決めます。

## 手順

1. 元の質問の主文と、列挙・「含めて」等で追加された要求から、求められている答えを漏れなく取り出します。
   「A、B、Cも含めて」のような列挙は、A、B、Cを一項目ずつ照合します。
2. 各要求を、何について答えるのかが分かる短い確認事項にします。
3. 1つの確認事項につき1つのWorkItemを作り、各WorkItemへ検証可能なHypothesisを置きます。
4. Hypothesisごとに最初の探索方法と検索表現を選びます。
5. 元の質問とWorkItemを一対一に照合し、答える対象の省略や混在がないことを確認します。
6. `question_requirement_checklist`へ、主文と各列挙から読み取った要求を、WorkItemと同じ順序・件数で書きます。
7. `decision_reason`に、分解と最初の行動を選んだ理由、確認事項の実件数、全確認事項の短い名称を書きます。
8. open WorkItem、Hypothesis、ToolRequestを含むSolverDecisionを返します。

### 判断ルール

#### 作業分解と仮説

- 初回判断では、元の質問が求める範囲だけを、重複しないWorkItemへ分解します。
- 確認事項は、近くにある名詞ではなく、質問が求める答えで見分けます。
  「いつ・どのような場合に必要か」は成立条件を、「必要になった場合に何をするか」は行為・手続内容を求めます。
  同じ制度や語を含んでいても、一方への回答で他方へ答えられないなら別の確認事項です。
- 主文の問いと、「含めて」「あわせて」等で明示的に追加された問いを、どちらも残します。
  列挙された問いを、近い意味の別の問いへ吸収しません。追加された問いだけを扱ったり、
  複数の問いを質問全文の写しへまとめたりしません。
- 根拠・出典・引用を付ける指定や、表・箇条書き等の出力形式、詳しさの指定は、
  実体的な回答対象ではありません。独立WorkItemにせず、関係するWorkItemの根拠・回答要件として扱います。
- 1つのWorkItemには、1つの完了判定で閉じられる1つの確認事項だけを書きます。
- 同じ確認事項へ答えるための根拠が複数あるだけなら、別WorkItemへ分けません。
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
- `question_requirement_checklist`はLLM自身の理解確認用です。CaseStateの更新項目ではありません。

### 列挙の読み方の例

「ある届出が必要になるのはどの場合か。対象行為、例外、届出方法も根拠とともに説明して」なら、
求める答えは、必要となる条件、対象行為、例外、届出方法の4項目です。
「例外と届出方法」のように一つへまとめず、「根拠」を5番目のWorkItemにもしません。
この例の項目名や件数は、別の質問へコピーしません。

### WorkItem・Hypothesis・ToolRequestの関係例

元の質問が「古物営業で許可が必要となる条件と、許可申請の手続を知りたい」の場合、
確認事項を次のように対応付けます。この例の件数、名称、ID、法的内容を別の質問へコピーせず、
関係の作り方だけを使います。「許可が必要となる条件」と「許可を受けるための手続」は、
同じ許可制度の問いでも求める答えが異なるため、別のWorkItemです。

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
