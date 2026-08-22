# 法令検索 課題管理

> 更新日: 2026-08-22
>
> 本書は、法令検索の現在地、未解決課題、優先順位、完了条件を管理する。
> 設計仕様の正本ではない。Agent契約は
> [汎用反復型Agent実装計画](generic_iterative_agent_framework_plan.md)、第二期の狙いは
> [第二期開発備忘録](second_phase_development_memo.md)、実行手順と実測値は
> [RUNBOOK](../RUNBOOK.md)を正とする。

## 1. 管理方法

課題のstatusは次の意味で使う。

| status | 意味 |
|---|---|
| `未着手` | 方針はあるが、実装または評価を開始していない |
| `対応中` | 実装、原因調査、または修正を進めている |
| `検証待ち` | 必要な実装は存在するが、要求する実モデルE2E評価に合格していない |
| `要設計` | 選択肢とトレードオフを決める必要がある |
| `停止中` | 再開条件を満たすまで意図的に作業を止めている |
| `完了` | 本書の完了条件を満たす証跡がある |

優先度は、第二期Step 1の完了に必須なものを`P0`、基盤の安定化に必要なものを`P1`、
対象拡大時に扱うものを`P2`とする。

statusを`完了`へ変更するときは、対応するテスト、評価結果、traceまたはcommitを「確認証跡」へ記載する。
設計を変更した場合は本書だけを更新せず、正本文書、契約、Prompt、テストも同じ変更で更新する。

## 2. 対象とする検索フロー

```text
利用者の質問
    ↓
WorkItemへ分解し、検証可能なHypothesisを作る
    ↓
Hypothesisを法令に現れやすい検索表現へ変換する
    ↓
OpenSearchで候補Articleを検索する
    ↓
選択したArticle全文をOpenSearchから取得して評価する
    ↓
必要ならHypothesisに沿ったGraph selectorをSolverが指定する
    ↓
Neo4jから指定条件の1ホップ候補を取得する
    ↓
候補Article全文をOpenSearchから取得して評価する
    ↓
必要なら検索語を変えてOpenSearchを再検索するか、
選択したArticleを次の1ホップGraph検索の起点にする
    ↓
根拠が揃えば回答し、分解・仮説自体が不適切なら次Cycleで仕切り直す
```

同じCycle内でもOpenSearchは複数回使用できる。ただし、同一条件を繰り返すのではなく、本文やGraphの
観察によって判明した法令名、条番号、委任事項、法令表現または未解決WorkItemを使って検索条件を更新する。
同じHypothesisと探索方針を維持できる間は同一Cycleで処理し、作業分解や前提から見直す場合だけ次Cycleへ進む。

## 3. 課題一覧

| ID | 優先度 | status | 課題 | 現在地 | 次の確認 |
|---|---|---|---|---|---|
| `LR-001` | P0 | 検証待ち | 質問から必要な検索仮説を漏れなく作る | 範囲・要件・例外・手続を分けるPromptはあるが、総合問題で必要観点を追跡できていない | 公開買付け3問のtraceで、質問が要求する全観点が独立したWorkItem・Hypothesisになったか確認する |
| `LR-002` | P0 | 検証待ち | 法令検索表現を作り、同一Cycle内でOpenSearchを適切に再検索する | 「必要な手続」を「公告・届出・通知・提出・期間・様式」等へ言い換えるPromptと複数stepは実装済み | 初回検索、本文観察後の検索語変更、同一scopeの重複防止をtraceで確認する |
| `LR-003` | P0 | 検証待ち | Graph由来Articleを起点に連続1ホップ探索する | 固定selectorでは法律→施行令→府令へ到達でき、Graph由来Articleの再起点化も実装済み | OpenSearchだけでは末端Articleを発見できない条件で、実モデルが2回の1ホップを選ぶE2E試験を通す |
| `LR-004` | P0 | 対応中 | 複合問題で必要本文を揃えてから完了する | 狭い2問は合格したが、総合問題は240秒で停止し、必要Articleは2/6だった | `tob-overview`で必要Article 6/6、回答観点、未解決ID、引用の整合を確認する |
| `LR-005` | P0 | 未着手 | `gpt-4o-mini`で新検索経路を実モデル評価する | Provider接続、Structured Outputs、model切替は実装済み。法令E2Eは未実施 | Reviewer無効で公開買付け3問を実行し、Hypothesis、Tool、Evidence、Cycle、時間を記録する |
| `LR-006` | P1 | 要設計 | 意味分類coverage不足時にも逆引き検索爆発と取りこぼしを両立させる | publish済み意味関係ならselectorで絞れるが、未分類範囲でraw `REFERENCES/to_subject`を使うと高fan-inになる | 限定fallbackの発動条件、scope上限、coverage不足の表示、限定回答条件を決める |
| `LR-007` | P1 | 未着手 | CycleとStepを再開可能な状態として保存する | WorkItem、Hypothesis、Evidence、Graph review履歴はあるが、目標の`CycleRecord / StepRecord / ExplorationState`は未実装 | Tool観察後の中断から同じStepを再開し、別Cycleとして数えないfixtureを通す |
| `LR-008` | P1 | 未着手 | status、Provider schema、Prompt、遷移検証の修正漏れを防ぐ | 現在は複数ファイルへ定義が分散している | 説明付き型契約からschemaとLLM用語集を生成し、未定義statusで契約テストが失敗するようにする |
| `LR-009` | P1 | 対応中 | Tool数、本文取得数、Cycle数の用語と設定を一致させる | 現行既定は最大4 Cycle、1 step最大5 Tool要求、1 Cycle本文3 Article。過去の「4 Article」記述が一部資料に残る | 正本、Profile、Prompt、fixture、データセット説明の値と意味を照合する |
| `LR-010` | P2 | 停止中 | 全件Relation分類を安全に再開する | 全件Runは1,615 checkpointで参照scopeと改正法構造の問題が見つかり停止中 | shadow差分監査、影響候補特定、再開条件合格後に同じsnapshot・checkpointから再開する |
| `LR-011` | P2 | 要設計 | 自治体の条例・規則・要綱を使う小規模データセットを決める | 自治体向け利用像はあるが、データセットは未決定 | 上位法令改正→条例→規則→要綱の逆引きを検証できる最小集合を利用者と決める |

## 4. P0課題の完了条件

### LR-001 検索仮説

- 質問が明示する要件、対象範囲、例外、手続が、重複しないWorkItemと検証可能なHypothesisになる。
- 不要な観点をチェックリストから機械的に追加しない。
- 取得本文から新しい委任事項が判明した場合だけ、必要なWorkItemまたはHypothesisを追加できる。
- 未取得の下位規範を学習済み知識で確認済みにしない。

### LR-002 OpenSearchの反復利用

- 1 Cycle内で、初回検索と観察後の再検索を別stepとして実行できる。
- 再検索には、変更した検索表現、判明した法令名・条番号、または対象WorkItemが記録される。
- 同一query、同一filter、同一Hypothesisの成功済みscopeを再実行しない。
- 検索候補のsnippetをHypothesisの支持根拠や回答citationに使わない。

### LR-003 連続1ホップGraph探索

- 1回の`legal_graph_neighbors`が1 mode、1 direction、意味検索では1 predicate、1ホップに限定される。
- Graph由来であることを理由に、選択Articleを次のGraph起点から除外しない。
- 法律→施行令と施行令→府令が、別々のToolRequestとしてtraceへ残る。
- Graph候補だけで回答せず、選択した両端Articleの全文をOpenSearchから取得する。
- 同じArticleへ複数経路で到達しても本文取得は1回で、DiscoveryLinkは各経路を保持する。

### LR-004 複合問題の完了判断

- `tob-overview`の必要Article 6件をすべてArticle全文として取得する。
- resolved WorkItemの根拠Hypothesisと回答citationが一致する。
- open WorkItemまたはunresolved Hypothesisが残る通常実行を完全回答としてfinalizeしない。
- 時間上限に達した場合は、未確認事項を隠さず限定回答として示す。
- 契約修復回数、入力・出力token、Tool時間、Cycleごとの取得Articleをtraceから説明できる。

### LR-005 GPT-4o mini実モデル評価

- `tob-exceptions`、`tob-announcement`、`tob-overview`をReviewer無効で各1回実行する。
- 必要Article到達数と回答観点を、過去のHaiku結果と同じ指標で比較する。
- 失敗時は、モデル判断、Prompt、共通契約、Provider輸送、Tool、データのどこで失敗したかをtraceで分離する。
- Provider疎通やmockテストだけを法令検索の合格としない。

## 5. 確認済みの前提

次は新規課題として再度設計し直さず、回帰対象として維持する。

| 項目 | 確認済み内容 |
|---|---|
| データ構造 | 保存済みe-Gov XMLを正本とし、Document、Article、Paragraph、Item、本則・附則、枝番を区別する |
| 本文取得元 | 候補発見とArticle全文取得はOpenSearch、関係候補取得はNeo4jとし、本文を二重取得しない |
| Graph単位 | 1要求1ホップとし、Programが候補を意味的に自動再帰展開しない |
| Graph経路 | 公開買付けミニsnapshotでは、固定selectorにより金商法27条の2→施行令7条→府令2条の5、金商法27条の3→府令10条へ到達できる |
| Evidence | 検索候補とGraph候補はnavigationであり、取得済みArticle本文だけを法的判断の根拠候補にする |
| 判断境界 | Hypothesis、検索語、Graph selector、候補採否、根拠十分性、完了はSolverが判断し、Programは既知ID、型、件数、予算、重複scopeだけを検証する |
| Agent構成 | 回答経路はSolverと任意Reviewerに留め、Reviewerは既定無効とする |

## 6. 推奨する実行順

```text
LR-005  GPT-4o miniで公開買付け3問を実行
    ↓
LR-001  WorkItem・Hypothesisの不足をtraceで確認
    ↓
LR-002  法令検索表現と同一Cycle内の再検索を確認
    ↓
LR-003  OpenSearchだけでは解けない連続1ホップE2Eを確認
    ↓
LR-004  総合問題を必要Article 6/6まで通す
    ↓
LR-006〜LR-009  対象拡大前に基盤を安定化
    ↓
LR-011  自治体向け小規模データセットを決定
    ↓
LR-010  必要性と費用を再評価して全件分類を再開
```

全件Relation分類は、P0の検索経路が小規模データで合格するまで再開しない。

## 7. 確認証跡

| 日付 | 対象 | 結果 | 証跡 |
|---|---|---|---|
| 2026-08-21 | 公開買付けミニGraph | 17候補を承認し、24 RelationAssertionをpublish。固定selectorで代表3経路へ到達 | [第二期開発備忘録](second_phase_development_memo.md#21-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・例外問題 | 必要Article 3/3、回答観点3/3 | [第二期開発備忘録](second_phase_development_memo.md#21-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・公告問題 | 必要Article 2/2、回答観点4/4 | [第二期開発備忘録](second_phase_development_memo.md#21-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-21 | Haiku・総合問題 | 240秒で停止し、必要Article 2/6。第二期Step 1は未合格 | [第二期開発備忘録](second_phase_development_memo.md#21-第二期step-1公開買付け3階層ミニデータセット) |
| 2026-08-22 | OpenAI Provider | `gpt-4o-mini`への切替とStructured Outputs接続を実装。法令E2Eは未実施 | [RUNBOOK](../RUNBOOK.md) |
