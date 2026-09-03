# 仮説単位の法令検索方式

## 位置づけ

本書は、新しい法令検索方式について合意した設計判断をまとめる。対象は、Hypothesisを起点に
OpenSearchとLegal Graphを使い、回答根拠となるArticle本文を集める処理である。

本書の内容は設計決定であり、未実装の項目を含む。現行実装との差は、実装と回帰テストが完了するまで
明示して管理する。

## 目的

- LLMがGraph候補とHypothesisの関係を判断しやすくする。
- Hypothesisの意味と、それに対応するEvidence・探索履歴を混在させない。
- 答えが見つからない探索を有限回で止め、入力増大と重複処理を防ぐ。

## 全体像

```text
WorkItem
└─ active Hypothesis H1
   │
   ├─ Cycle 1                            探索セット1
   │  ├─ OpenSearch                      セット内で最大1回
   │  │  └─ 検索抜粋から起点Articleを選ぶ
   │  ├─ Legal Graph（1ホップ）          セット内で最大1回
   │  │  └─ 整形した関係情報から候補を選ぶ
   │  ├─ 候補Article本文を取得           セット数に含めない
   │  ├─ 本文をHypothesisへ統合          セット数に含めない
   │  └─ 未確認事項が残ればCycleを閉じる
   │
   └─ Cycle 2                            探索セット2（設定で許可した場合だけ）
      ├─ OpenSearch                      必要な場合に最大1回
      ├─ Legal Graph（1ホップ）          起点があれば最大1回
      └─ 本文取得・統合                  セット数に含めない

H1の命題が変わる
└─ H2を新規作成
   ├─ replaces_hypothesis_id = H1
   └─ H1は履歴として保持し、通常の探索入力から外す
```

この図は標準例であり、OpenSearchとGraphを必ず両方使うという意味ではない。既知の起点Articleがあれば
Graphから開始でき、OpenSearchで起点を発見できなければGraphを実行せずにセットを終える。1 Cycleでは
同じHypothesisについて1セットだけ実行する。

探索セットを開始する前に、取得済み本文、未評価候補、選択済みArticleを処理する。新しい探索は、既知の
情報だけでは未確認事項を解消できない場合に限る。

## 候補探索から本文採用まで

候補の発見、本文の取得、Hypothesisの根拠としての採用は、それぞれ別の処理である。

```text
┌────────────── 1. 候補探索 ──────────────┐
│                                          │
│  OpenSearch                              │
│  ├─ 検索語でchunkを検索                  │
│  ├─ 結果をArticle単位にまとめる          │
│  └─ Article ID・見出し・検索抜粋を返す   │
│                                          │
│  Legal Graph                             │
│  ├─ 既知Articleを起点に1ホップ探索       │
│  └─ 候補Article・方向・関係説明・        │
│     短い根拠引用を返す                   │
└──────────────────┬───────────────────────┘
                   ▼
┌────────────── 2. 候補評価 ──────────────┐
│  LLMが候補とactive Hypothesisを照合      │
│                                          │
│  select ── 今、本文を取得する            │
│  defer  ── 関連し得るが、今は取得しない  │
│  reject ── 現Hypothesisには使用しない    │
└──────────────────┬───────────────────────┘
                   │ select
                   ▼
┌────────────── 3. 本文取得 ──────────────┐
│  CaseStoreに取得済み                     │
│  ├─ yes: 保存済み本文を再利用            │
│  └─ no : Article本文を取得して保存       │
│                                          │
│  ※ 取得しただけでは根拠採用にならない   │
└──────────────────┬───────────────────────┘
                   ▼
┌────────────── 4. 本文評価・採用 ────────┐
│  LLMが本文とHypothesisを照合              │
│                                          │
│  採用                                    │
│  ├─ HypothesisへEvidence IDを関連付ける   │
│  ├─ judgment・未確認事項を更新する        │
│  └─ 必要時だけ後続LLMへ本文を提示する     │
│                                          │
│  不採用                                  │
│  ├─ 本文と判定はCaseStoreに保持する       │
│  ├─ Hypothesisへ関連付けない              │
│  └─ 後続の通常Promptへ本文を提示しない    │
└──────────────────┬───────────────────────┘
                   ▼
┌────────────── 5. 次の処理 ──────────────┐
│  未確認事項なし ── Hypothesisの評価完了   │
│                                          │
│  未確認事項あり                          │
│  ├─ 未処理の既知候補を先に処理する       │
│  ├─ 必要なら残り上限内で新しく探索する   │
│  └─ 上限到達ならunresolvedとして残す      │
└──────────────────────────────────────────┘
```

`defer`は削除でも採用でもない。候補ID、対応Hypothesis、短い理由だけをCaseStoreへ保持し、優先候補を
統合しても未確認事項が残った場合など、判断材料が変わったときだけ再検討する。状態が変わっていない候補を
LLMへ繰り返し提示しない。

候補探索で得た検索抜粋やGraph関係は、本文取得対象を選ぶための情報であり、回答根拠ではない。
Article本文を取得し、Hypothesisとの対応が確認された時点で初めて根拠候補になる。

## 1. Graph候補をLLMが判断しやすい形にする

### 問題

Graphの生データには、predicate、方向、SUBJECT・OBJECT、両端Article、分類根拠などが含まれる。
これらをそのまま渡して、LLMへ方向の解釈とHypothesisとの対応判断を同時に求めると、候補の役割を
取り違えやすい。

### 決定

Programは、保存済みのGraph情報を意味を変えずに読みやすい関係文へ整形する。これは新しい法的判断では
なく、既存のpredicate、方向、端点、`relationExplanation`を決定的に表示する処理である。

例えば、次のGraph情報があるとする。

```text
predicate: USES_DEFINITION
direction: from_subject
subject: 公開買付府令第2条の5
object: 金融商品取引法第27条の2
```

LLMには、次のような関係として提示する。

```text
起点Article: 公開買付府令第2条の5
候補Article: 金融商品取引法第27条の2
関係: 公開買付府令第2条の5が使用する用語を、金融商品取引法第27条の2が定義している
```

実際の表示では、これがGraphに登録された未確認の候補関係であることも併記する。断定文に見せて、
本文確認済みの法的関係と誤認させない。

Graph候補の評価入力は、原則として次に限定する。

- `hypothesis_id`
- Hypothesisの`statement`と未確認事項
- 起点Article
- 候補Article
- 整形済みの関係説明
- 関係が確認済みか未確認候補かを示す状態
- 関係分類に使われた短い根拠引用
- 候補Article本文の取得状態

LLMは、この候補Articleの本文をHypothesisの確認に使う価値があるかを`select / defer / reject`で判断する。
Graphのpredicateや方向そのものを再分類しない。Graph候補だけでHypothesisを支持済みにせず、選択した
Article本文を取得してからEvidenceとして評価する。

```text
Neo4jの関係情報
        ↓ Programが決定的に整形
Hypothesis + 起点 + 候補 + 関係文 + 短い根拠引用
        ↓ LLMが関連性を評価
select / defer / reject
        ↓ selectのみ
Article本文取得 → Evidence統合
```

### 取得本文の保存とLLMへの提示

本文を取得したことと、Hypothesisの根拠として採用することを区別する。

```text
Article本文を取得
        ↓
CaseStoreへ保存
        ↓ Hypothesisとの対応を評価
        ├─ 採用
        │  ├─ HypothesisへEvidence IDを関連付ける
        │  └─ 後続処理で必要な場合だけLLMへ本文を提示する
        │
        └─ 不採用
           ├─ CaseStoreには本文と判定を保持する
           ├─ HypothesisへEvidence IDを関連付けない
           └─ 後続の通常Promptへ本文を提示しない
```

CaseStoreは取得済み情報の正本であり、LLM入力ではない。Projectorは、今回評価する本文又は対象Hypothesisへ
関連付け済みで今回の判断に必要な本文だけを選んでLLMへ渡す。不採用本文をCaseStoreから削除する必要はないが、
保存されているという理由だけで後続Promptや最終回答へ含めない。

別Hypothesisで同じArticleが必要になった場合は、CaseStoreの取得済み本文を再利用し、そのHypothesisとの対応を
改めて評価する。OpenSearchや元データから本文を再取得せず、過去の不採用判断を別Hypothesisへ自動適用しない。

## 2. 内容が変わったHypothesisは別Hypothesisにする

### 決定

Hypothesisは、一つの暫定的な法的命題を表す。`statement`の意味が変わる場合は既存Hypothesisを
上書きせず、新しいIDのHypothesisを作る。

```text
WorkItem W1
├─ H1: 当初の法的命題
│  └─ H2の置換元として履歴に残る
└─ H2: 本文から得た知見を反映した別の法的命題
   └─ replaces_hypothesis_id = H1
```

- H1とH2が同時に検証すべき独立命題なら、H2の`replaces_hypothesis_id`は`null`にする。
- 文言の修正だけで命題が変わらない場合は、新しいHypothesisを作らない。
- 旧Hypothesis、そのEvidence対応、探索履歴は監査用に保持する。
- 通常の探索Promptには、他のHypothesisから置換されていないHypothesisだけを渡す。
- 新Hypothesisへ旧HypothesisのEvidenceや判断を無条件に継承しない。既取得本文を再利用する場合も、
  新Hypothesisとの対応を改めて評価する。

`active / superseded`は保存項目にしない。ほかのHypothesisの`replaces_hypothesis_id`からProgramが導出する。

Programは、同じWorkItem内の正規化後の同一文面を重複Hypothesisとして拒否する。意味を変えない言い換えの
判定はLLMが行うが、後述する探索scopeの重複防止により、見逃しても同じ探索は再実行しない。

## 3. 同一Hypothesisの探索セットに上限を設ける

### 上限

OpenSearchと、その結果又は既知Articleを起点にするGraph 1ホップを、一つの探索セットとして扱う。
置換されていない一つのHypothesisについて、次の2つの上限を適用する。

| 上限 | セット数 | Cycle移行時 |
|---|---:|---|
| Cycle内上限 | 1 Cycleにつき1セット | 次Cycleで数え直す。 |
| 全Cycle通算上限 | 設定値。既定は1セット | リセットしない。必要なら設定を2へ変更できる。 |

いずれか先に達した上限を適用する。Case全体のCycle数、step数、本文取得数及び時間の上限は別に存在し、
それらにも従う。

一つの探索セットに含められるもの:

- OpenSearch: 最大1回
- Legal Graphの1ホップ探索: 最大1回

探索セット数に含めないもの:

- 検索抜粋の評価
- Graph候補の評価
- Article本文の取得
- Evidence統合
- Cycle終了処理と最終回答

標準的には、Cycle 1の1セットでOpenSearchにより起点Articleを発見し、同じセットでその起点からGraphを
1ホップ探索する。Graphは1要求につき1ホップとし、候補を自動的に連続展開しない。

設定を2セットへ変更した場合も、2セット目を同じCycleへ追加しない。取得済み情報を統合してCycleを閉じ、
次Cycleで検索条件又はGraph起点を見直して実行する。

設定名は`max_exploration_sets_per_hypothesis`とし、既定値を`1`とする。OpenSearchとGraphの個別呼出し回数を
上限値として設定しない。

探索セットは同じCycle内の複数stepにまたがることができるが、Cycleをまたがない。最初のOpenSearch又はGraphを
実行した時点でセットを開始し、その結果評価後に行う別種のToolを同じセットへ含める。同じセットのGraphを、
2セット目として誤って数えない。既知の起点Articleがある場合は、Graphだけで1セットを構成できる。

### 上限到達時

設定された全Cycle通算セット数で十分な根拠が得られなかった場合は、次の状態にする。

```text
judgment = unresolved
gaps = 未確認のまま残った事項
hypothesis_exploration_sets.remaining_new_sets_total = 0
```

上限到達は、規定や根拠が存在しないことを意味しない。WorkItemは他のHypothesisで回答可能かを確認し、
最終回答では確認できた範囲と未確認事項を区別する。

### 上限の迂回を防ぐ

- Hypothesis IDではなく、Tool、WorkItem、正規化した引数、Graphの起点・関係・方向から探索scopeを識別する。
- 同じscopeの成功済み探索は再実行せず、保存済み結果を再利用する。
- 同じ命題を新しいHypothesis IDで追加しても、探索上限と成功済みscopeを迂回できないようにする。
- 命題が実質的に変わった新Hypothesisには新しい探索上限を与える。ただし、過去と同じscopeは再実行しない。

## Cycleとの関係

探索上限はHypothesis単位、Cycleは処理を整理して再計画する単位であり、別の概念である。

- 同じ探索セット内で、OpenSearchで起点が判明した後にGraphを1ホップ探索できる。
- 本文を取得したら、同じHypothesisの新しい探索より先にEvidence統合を行う。
- 2セット目が設定で許可されている場合も、同じCycleへ入れず、残りセット数を次Cycleへ引き継ぐ。
- Hypothesisが上限へ達していても、別の置換されていないHypothesisは独立して探索できる。

## LLMとProgramの責務

| 担当 | 責務 |
|---|---|
| LLM | 法的命題の作成、Hypothesisの意味上の変更判断、候補とHypothesisの関連性、本文の根拠評価 |
| Program | ID、置換関係からの処理対象導出、探索セット数、探索scopeの重複、Graph関係の決定的な表示、本文保存、用途別のLLM入力投影、上限到達処理 |

Programは、候補の法的関連性やHypothesisの正否を推測しない。LLMは、ID、回数、重複、上限、保存を
管理しない。この分担により、法的意味判断を保ったまま、重複探索と状態不整合を機械的に防ぐ。

## 対応モデルとProvider境界

Luna、Haiku 4.5及びSonnet 4.6で、Hypothesis、探索セット、ToolRequest、Evidence及びCycleの正規契約を分けない。
固定Promptと用途別入力も共通にし、APIへ送るJSON Schema、推論設定及び応答形式の差だけをProvider
adapterで吸収する。

| モデル | 共通で使うもの | Provider adapterだけで扱う差 |
|---|---|---|
| `gpt-5.6-luna` | 同じPrompt、`SolverContext`、正規出力契約、検証処理 | OpenAI Structured Outputsへschemaを変換し、対応する`reasoning_effort`を送る。 |
| `claude-haiku-4-5-20251001` | 同上 | Anthropic Structured Outputs用の輸送schemaを使う。`effort`は送らず、設定時だけmanual extended thinkingの予算を送る。 |
| `claude-sonnet-4-6` | 同上 | Haikuと同じAnthropic正規化経路を使う。manual thinking予算は送らず、`thinking.type=adaptive`と`output_config.effort`を使う。 |

現行実装では、Lunaのreasoning effortとHaikuのmanual thinkingは輸送層で適用される。一方、
`StructuredJSONModelAdapter`から`LLMClient.generate_structured_json()`へ用途別のeffortを渡しておらず、
Sonnet 4.6のadaptive thinkingも有効にしていない。Structured Outputs自体は利用できるが、Frameworkの
設定としてthinkingとeffortを制御・記録できない。実装時は、
`ModelCallProfile`で解決した任意のreasoning effortを共通JSON生成入口へ渡し、次のように変換する。

```text
ModelCallProfile.reasoning_effort
        │
        ├─ OpenAI + Luna
        │    └─ reasoning_effort
        │
        ├─ Anthropic + Sonnet 4.6
        │    ├─ thinking.type = adaptive
        │    └─ output_config.effort
        │
        └─ Anthropic + Haiku 4.5
             └─ effortは送らない
                必要時だけthinking.type=enabled + budget_tokens
```

HaikuとSonnetの応答では、`content[0]`を本文と仮定せず、`type=text`のblockだけを連結する。Sonnet 4.6の
`stop_reason=refusal`は空JSON又は一般的な契約違反として再試行せず、Providerの拒否として区別する。

Anthropic用の縮小された輸送schemaは正規契約ではない。`decision_json`等を復元した後、Lunaと同じ
Pydantic契約とValidatorへ必ず通す。Hypothesis置換契約を変更するときは、正規schemaだけでなく、
Anthropic輸送schemaと復元処理も同じ変更で更新する。

一つの実行では一つの`LLM_PROVIDER`を使う。LunaとClaudeを同一実行内で混在させることは本設計の
要件にしない。Anthropic実行内でHaiku 4.5とSonnet 4.6を役割別に使い分ける場合も、状態、Prompt及び正規契約は
共通のままとする。

現行の`config.py`、`.env.example`及び一部のテストには`claude-sonnet-5`が残っている。実装時は、
実行設定の既定値、設定例及び現行挙動を固定するテストを`claude-sonnet-4-6`へ変更する。
過去の比較結果として記録されたモデル名は、実行時設定ではないため履歴のまま残す。

参考:

- [GPT-5.6 Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [Claude Sonnet 4.6](https://platform.claude.com/docs/en/models/sonnet-4-6/overview)
- [Claude extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

## 実装時の確認事項

- Hypothesisの`statement`を意味変更を伴って上書きしていない。
- 旧Hypothesisが通常の探索入力へ混入していない。
- 新Hypothesisへ旧Evidenceを無条件で移していない。
- CaseStoreに保存された不採用本文を、対象Hypothesisとの対応なしにLLMへ投影していない。
- 別Hypothesisで取得済みArticleが必要になった場合、本文を再取得せず、対応だけを改めて評価している。
- Cycle移行で探索セットの通算数がリセットされていない。
- 新しいHypothesis IDだけで同一探索を再実行できない。
- Graph候補の表示文が、保存済みpredicate・方向・端点と一致している。
- Graph候補だけを回答根拠又はHypothesis支持の根拠にしていない。
- 上限到達を「根拠なし」「規定なし」と断定していない。

## 修正対象一覧

実装時は、次の順に関連箇所を確認する。ファイル名は修正箇所を探すための入口であり、詳細な変更内容は
各節の決定に従う。

### JSON項目の変更

| JSONパス | 種別 | 現在 | 変更後 |
|---|---|---|---|
| `HypothesisRevisionDecision.revise_hypotheses[]` | 削除 | 同一IDのHypothesisを更新する差分 | 項目自体を廃止する。 |
| `HypothesisRevisionUpdate` | 削除 | `hypothesis_id`、`statement`、`judgment`、`evidence_ids[]`、`add_gaps[]`、`resolve_gap_ids[]`を持つ | 型ごと廃止する。判断とgapの通常更新は`HypothesisUpdate`へ一本化する。 |
| `HypothesisRevisionDecision.add_hypotheses[]` | 変更 | 独立した命題だけを追加する | 独立した命題と、旧Hypothesisを置き換える命題を追加する。 |
| `HypothesisRevisionProposal.replaces_hypothesis_id` | 追加 | 項目なし | 置換なら旧Hypothesis ID、新規追加なら`null`を返す。 |
| `HypothesisRevisionProposal.evidence_ids[]` | 削除 | 新命題の必要性を示した本文IDを返す | 新HypothesisはEvidence未対応で作り、別の本文評価で`Hypothesis.evidence_ids[]`へ追加する。 |
| `Hypothesis.replaces_hypothesis_id` | 追加 | 項目なし | 置換元Hypothesis IDを保存する。置換でなければ`null`とする。処理対象かどうかはこの関係から導出する。 |
| `Hypothesis.statement` | 意味変更 | 同じIDのまま内容を変更できる | 作成後は同じ法的命題を維持する。意味が変わる場合は別IDを作る。 |
| `Hypothesis.judgment` | 維持 | 本文評価により更新する | 変更なし。命題置換には使わない。 |
| `Hypothesis.evidence_ids[]` | 維持 | judgmentとgapの判断に使った本文ID | 変更なし。新Hypothesisには本文を再評価してから追加する。 |
| `Hypothesis.gaps[]` | 維持 | 未確認事項の追加・解消で更新する | 変更なし。命題自体が変わる場合は新Hypothesis側で作り直す。 |
| `CaseState.hypothesis_history[]`、`HypothesisHistoryRecord` | 削除 | 同一IDで上書きする前のsnapshotを保存する | 旧Hypothesis自体を別IDで保持するため廃止する。 |
| `CaseState.invalidated_tool_request_ids[]` | 削除 | 同一IDの旧版に対するToolRequestを無効化する | ToolRequestは置換前Hypothesisの履歴として有効なまま保持するため廃止する。 |
| `SolverContext.hypotheses[]` | 投影変更 | 保存中の現在版Hypothesisを渡す | 他のHypothesisから置換されていないHypothesisだけを通常のLLM処理へ渡す。 |
| `SolverContext.material_evidence[]` | 投影変更 | 処理profileに応じて本文を渡す | 今回評価する本文又は置換されていないHypothesisに必要な採用済み本文だけを渡す。 |
| `GraphReviewCandidate.links[].relations[]` | 入力形式変更 | Graphの関係辞書をほぼそのまま渡す | 保存値から起点、候補、predicate、方向、関係説明、確認状態、短い分類根拠を決定的に整形して渡す。 |
| `AgentLimits.max_exploration_sets_per_hypothesis` | 追加 | 項目なし | Hypothesisごとの全Cycle通算セット上限。既定値は1とし、設定で2へ変更できる。 |
| `SolverContext.hypothesis_exploration_sets[]` | 追加 | 項目なし | `hypothesis_id`、`legal_search_used_in_cycle`、`graph_used_in_cycle`、`remaining_new_sets_total`を持つ読み取り用配列を追加する。 |
| `ModelCallProfile.reasoning_effort` | 追加 | 新Frameworkの用途別Profileには推論設定がない | 任意の共通設定として保持し、Provider adapterが対応モデルのAPI表現へ変換する。Haikuでは`null`として扱う。 |

`hypothesis_exploration_sets[]`はCaseStoreの第二の正本として保存せず、ToolRequestとToolResultの履歴から作る。
既定の1セットをOpenSearchで開始した後は`remaining_new_sets_total=0`になるが、同じセットのGraphは実行できる。
そのため、OpenSearchとGraphの使用状態を別々に示す。`status`及び`lifecycle`はほかの値から導出できるため追加しない。

### 処理別の修正箇所

| 処理 | 修正する項目 | 修正内容 | 主な修正ファイル |
|---|---|---|---|
| 設定の定義 | `AgentLimits.max_exploration_sets_per_hypothesis` | `1..2`の設定値を追加し、既定値を`1`にする。個別のOpenSearch回数とGraph回数には分けない。 | `agent_framework/profiles.py` |
| 環境設定の読込み | `AGENT_FRAMEWORK_MAX_EXPLORATION_SETS_PER_HYPOTHESIS` | 環境変数を読み、Legal Profileの`AgentLimits`へ渡す。サンプル値、Composeの受け渡し及び操作説明も追加する。 | `config.py`、`domains/legal/profiles.py`、`.env.example`、`docker-compose.yml`、`RUNBOOK.md` |
| Hypothesis見直し契約 | `revise_hypotheses[]`、`HypothesisRevisionUpdate`、`HypothesisRevisionProposal` | 同一ID更新を廃止し、追加契約へ`replaces_hypothesis_id`を加える。追加契約から`evidence_ids[]`を外す。 | `agent_framework/contracts.py`、`adapters/models/structured_json.py`、`solver_hypothesis_revision.md`、`solver_hypothesis_revision_check.md` |
| Hypothesis保存 | `Hypothesis.replaces_hypothesis_id` | 新IDのHypothesisを保存し、置換元の存在、同一WorkItem所属、循環参照がないことを検証する。同一IDの`statement`上書き、snapshot作成、ToolRequest無効化を削除する。 | `agent_framework/state.py`、`agent_framework/validation.py` |
| 処理対象の導出 | `SolverContext.hypotheses[]` | ほかのHypothesisの`replaces_hypothesis_id`から参照されていないものだけを通常の探索対象にする。旧Hypothesisは監査用の状態には残す。 | `agent_framework/context.py`、`agent_framework/loop.py`、`agent_framework/cycle_audit.py` |
| Evidence投影 | `SolverContext.material_evidence[]` | 今回評価する本文又はactive Hypothesisへ採用済みの本文だけを用途別に投影する。不採用本文と置換済みHypothesisだけに対応する本文は通常入力から外す。 | `agent_framework/context.py`、`agent_framework/loop.py` |
| Graph候補投影 | `GraphReviewCandidate.links[].relations[]` | 保存済みの起点、候補、predicate、方向、関係説明、確認状態、短い分類根拠を、意味判断を加えず一定の形式へ整える。 | `agent_framework/context.py`、`domains/legal/prompts/solver_graph_review.md` |
| 探索セット集計 | `SolverContext.hypothesis_exploration_sets[]` | `ToolRequest`を`request_id`で成功した`ToolResult`へ結合し、Hypothesisごとに現在CycleのOpenSearch使用、Graph使用、通算セット数を算出する。候補0件の成功も使用済みとし、通算セット数は使用済み探索がある異なる`cycle_no`の数とする。 | `agent_framework/context.py` |
| 探索セット開始判定 | `remaining_new_sets_total` | 現在Cycleでどちらの探索も未使用なら、新規セット残数がある場合だけOpenSearch又はGraphを許可する。 | `agent_framework/validation.py` |
| 同じセットの継続判定 | `legal_search_used_in_cycle`、`graph_used_in_cycle` | 現在Cycleで片方を使用済みなら、通算残数が`0`でも未使用のもう片方を同じセットとして許可する。同種Toolの2回目とGraphの連続2ホップは拒否する。 | `agent_framework/validation.py`、`agent_framework/loop.py` |
| 成功済みscopeの再利用 | `_tool_request_scope()` | scopeから`hypothesis_ids`を外し、Tool、WorkItem、正規化引数で識別する。新しいHypothesis IDを付けても同じ検索を再実行せず、保存済み結果を再利用する。 | `agent_framework/validation.py`、`agent_framework/context.py` |
| LLMへの探索状態提示 | `hypothesis_exploration_sets[]` | active Hypothesisごとに、現在セットで使用済みのToolと新規セット残数を提示する。LLMに回数計算をさせない。 | `agent_framework/context.py`、`adapters/models/structured_json.py`、`domains/legal/prompts/solver_tools.md` |
| 次行動の選択 | 探索関連Prompt | 既知候補、本文取得、本文統合を先に処理し、その後に同じ探索セットの未使用Toolを選ぶ。新規セット残数がなければ別セットを開始しない。 | `solver_search_planning.md`、`solver_integration.md`、`solver_evidence_integration.md`、`solver_dependency_action.md`と各`*_check.md` |
| Cycle境界 | 探索セットの引継ぎ | セット途中の状態を次Cycleへ持ち越さない。設定値が`2`で通算残数があれば、本文統合後の次Cycleで新しいセットを開始できるようにする。 | `agent_framework/loop.py`、`agent_framework/validation.py` |
| 監査・診断 | 探索セットのsnapshot | Hypothesis ID、Cycle、OpenSearch使用、Graph使用、通算使用数、残数及び拒否理由を記録する。完成Promptにも同じ読み取り値を出す。 | `agent_framework/cycle_audit.py`、`agent_framework/diagnostics.py`、`agent_framework/model_call_artifacts.py` |
| Prompt索引 | 処理の役割と入力 | 探索セットの判断を行うPromptと、Programが上限を強制する境界を索引へ反映する。 | `domains/legal/prompts/README.md` |
| 方針文書の整合 | Hypothesis更新の記述 | `IMPLEMENTATION-GUIDELINES.md`には個別の置換契約や上限値を書かず、状態を上書きせず監査可能に保つ原則だけを残す。詳細仕様は本書を参照させる。 | `IMPLEMENTATION-GUIDELINES.md` |
| 共通推論設定 | `ModelCallProfile.reasoning_effort` | 用途別Profileで推論設定を解決し、共通JSON生成入口へ渡す。OpenAIとAnthropicのAPI項目名をProfileへ持ち込まない。 | `agent_framework/profiles.py`、`domains/legal/profiles.py`、`adapters/models/structured_json.py`、`llm.py` |
| Anthropic輸送schema | Hypothesis見直し、探索及び統合の縮小schema | `replaces_hypothesis_id`の追加、`revise_hypotheses[]`と提案時`evidence_ids[]`の削除をAnthropic用schema及び復元処理にも反映する。 | `adapters/models/structured_json.py` |
| Anthropic応答処理 | thinking block、`stop_reason` | Haiku 4.5のmanual thinkingとSonnet 4.6のadaptive thinkingを区別し、本文は`type=text`で抽出する。`refusal`を契約修復対象から外す。 | `llm.py`、`adapters/models/structured_json.py` |
| Provider設定・確認 | model、effort、thinking、token上限 | 許可モデルをLuna、Haiku 4.5、Sonnet 4.6とする。`config.py`のAnthropic既定値と`.env.example`の現行設定例を`claude-sonnet-4-6`へ変更し、切替方法を更新する。診断artifactへprovider、model、要求effort、実効方式を残す。 | `config.py`、`.env.example`、`docker-compose.yml`、`RUNBOOK.md`、`agent_framework/diagnostics.py`、`agent_framework/model_call_artifacts.py` |

`hypothesis_exploration_sets[]`はLLMが返す出力項目ではない。ProgramがCaseStoreの履歴から組み立てる
読み取り専用の入力である。このため、探索セット番号又は探索回数を`ToolRequest`へ追加せず、
`ToolResult.cycle_no`で同一CycleのOpenSearchとGraphを一つのセットにまとめる。
`failed`又は`timeout`は法的探索を完了したことにせず、既存のTool失敗・再試行上限で扱う。

### 現行項目の扱い

| 現行項目 | 扱い | 理由 |
|---|---|---|
| `ToolRequest.hypothesis_ids[]` | 維持 | Tool結果をどのHypothesisで評価するかを示す意味上の対応に使う。物理的な重複scopeのキーには使わない。 |
| `ToolResult.cycle_no` | 維持・集計に使用 | 同一CycleのOpenSearchとGraphを同じ探索セットとして数える。 |
| `completed_legal_searches[]` | 維持・生成条件を変更 | 成功済み検索をLLMへ知らせる。置換済みHypothesisとの一致だけを理由に履歴から消さない。 |
| `completed_graph_searches[]` | 維持・生成条件を変更 | 成功済みGraph探索と候補を再利用する。置換済みHypothesisとの一致だけを理由に履歴から消さない。 |
| `max_graph_articles_per_hypothesis_per_cycle` | 維持 | Graphから選択して本文取得するArticle数の上限であり、Graph探索セット数とは別である。 |
| `max_tool_requests_per_step` | 維持 | 1回のSolver出力が返せるToolRequest総数の上限であり、Hypothesisの探索セット数とは別である。 |
| `max_research_cycles` | 維持 | Case全体のCycle上限であり、Hypothesisごとの通算探索セット上限とは別である。 |

### テストの修正入口

| 確認対象 | 主なテスト | 確認内容 |
|---|---|---|
| Hypothesis置換 | `tests/test_hypothesis_revision.py` | 別ID追加、置換元ID、同一ID上書き拒否、Evidence非継承、処理対象の導出 |
| gap差分 | `tests/test_hypothesis_gap_diffs.py` | 通常の`HypothesisUpdate`では既存の追加・解消が維持されること |
| Context投影 | `tests/test_layered_context_assembler.py`、`tests/test_agent_framework.py` | 置換されていないHypothesisだけを渡し、不採用本文と置換済みHypothesisを通常入力へ含めないこと |
| Graph表示 | `tests/test_agent_framework.py`、Graph Review関連テスト | 整形結果が保存済みpredicate、方向、端点、説明及び根拠と一致すること |
| 探索セット集計 | `tests/test_agent_framework.py` | 同じHypothesis・同じCycleのOpenSearchとGraphが1セットになり、異なるCycleだけが通算セット数を増やすこと |
| 探索結果status | `tests/test_agent_framework.py`、`tests/test_llm_research_loop.py` | 候補0件の成功はセットを消費し、`failed`又は`timeout`は探索完了として数えないこと |
| Cycle内上限 | `tests/test_agent_framework.py`、`tests/test_llm_research_loop.py` | OpenSearch後のGraphは許可し、同じCycleの2回目のOpenSearch、2回目のGraph及びGraph連続2ホップを拒否すること |
| 通算上限 | `tests/test_llm_research_loop.py` | 既定値`1`では次Cycleの新規セットを止め、設定値`2`では2セット目を次Cycleだけで実行すること |
| 上限に数えない処理 | `tests/test_llm_research_loop.py` | 候補評価、本文取得、Evidence統合、Cycle終了が探索セット数を増やさないこと |
| scope再利用 | `tests/test_agent_framework.py`、`tests/test_llm_research_loop.py` | Hypothesis IDを変えても同じTool、WorkItem、引数の成功済み探索を再実行せず、保存済み結果を参照できること |
| 既知起点からの開始 | `tests/test_llm_research_loop.py` | 起点Articleが既知ならGraphだけで1セットを構成でき、OpenSearchを強制しないこと |
| 起点未発見 | `tests/test_llm_research_loop.py` | OpenSearchで起点Articleを選べなかった場合にGraphを強制せず、1セットとして終了できること |
| Cycle境界 | `tests/test_llm_research_loop.py`、`tests/test_cycle_audit.py` | 未使用Toolを次Cycleへ同じセットとして持ち越さず、各Cycleの使用状況と通算残数を監査できること |
| Provider共通契約 | `tests/test_openai_transport.py`、`tests/test_anthropic_transport.py`、Structured JSON関連テスト | Luna、Haiku 4.5、Sonnet 4.6の輸送表現が同じ正規契約へ復元され、同じValidator結果になること |
| 推論設定 | `tests/test_openai_transport.py`、`tests/test_anthropic_transport.py` | Lunaへreasoning effort、Sonnet 4.6へadaptive thinkingと`output_config.effort`、Haiku 4.5へmanual thinkingだけが送られること |
| 許可モデル設定 | `tests/test_llm_config.py`、`tests/test_anthropic_transport.py` | Anthropic既定値と役割別の上位モデル例が`claude-sonnet-4-6`であり、`claude-sonnet-5`を現行設定として使用しないこと |
| Anthropic schema回帰 | `tests/test_llm_parse.py`、Structured JSON関連テスト | Anthropic用schemaがAPI対応形で、Hypothesis置換の追加・削除項目を欠落させないこと |
| Anthropic応答block | `tests/test_anthropic_transport.py` | thinking blockが先頭でもtext JSONを取得でき、Sonnetの`refusal`を契約修復として再試行しないこと |
| 診断artifact | `tests/test_model_call_artifacts.py` | provider、model、要求effort及び実効thinking方式を実行結果と照合できること |
