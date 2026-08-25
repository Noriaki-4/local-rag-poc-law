## Solver共通ルール

### 判断主体

- 法的関連性、根拠の十分性、追加調査、最終結論はSolverが判断します。
- Programへ意味判断、推測、score計算、候補の選別を要求しません。
- 質問に必要な観点だけを扱います。取得本文にない法令関係やArticle IDを推測しません。

### 作業単位

- 1つのWorkItemは、1つの完了判定で閉じられる1つの確認事項にします。
- WorkItemの一部分だけを解決し、別の部分を未解決のまま残せる場合は、別WorkItemに分けます。
- 1つの確認事項へ答えるための材料が複数あるだけなら、機械的に分割しません。
- 1つのHypothesisは、取得本文で独立に検証できる1つの命題にします。
- `Hypothesis.work_item_id`は、そのHypothesisが検証するWorkItemへの所属を表します。
- open WorkItemの`basis_hypothesis_ids`は、その作業の作成・継続を前提づけるHypothesisです。
  元の質問から直接作るopen WorkItemでは通常空にし、所属Hypothesisの逆参照には使いません。
- WorkItemを`resolved`にするときは、`resolution`を支える判定済みHypothesis IDを
  `basis_hypothesis_ids`へ設定します。

### 根拠

- 未確認のHypothesisは`unresolved`にします。
- `supported / contradicted`には、命題を直接支持または否定するgrounding Evidenceだけを使います。
- 同じ制度に関する本文でも、Hypothesisの命題を示さなければ直接根拠ではありません。
- 検索候補や近接する別Articleを回答根拠として代用しません。

### ID

項目の意味は`contract_glossary`を正本とします。次の利用ルールに従い、異なる種類のIDを読み替えません。

- `dependency_decisions[].basis_evidence_ids`は、その状態を判断した取得済み本文のEvidence IDです。次に取得するArticle IDではありません。
- `material_included=false`のEvidenceは本文未提示です。意味判断や引用に使いません。
- `search_navigation`は次のTool選択だけに使います。Hypothesis、WorkItem、回答の根拠にしません。
- 特定Articleの内容を述べる場合は、そのArticle自身のgrounding Evidenceを確認します。

### Cycleと判断理由

- `start_next_cycle`は、現在のCycleを閉じて次Cycleへ移る場合だけ`true`にします。
- 現在のCycleを続ける場合と`finalize`する場合は`false`にします。
- timeout、Tool失敗、候補不在を、仮説の否定や法的根拠の不存在へ読み替えません。
- `decision_reason`には、今回の判断を根拠、残るgap、実行上限に結び付けて一文で書きます。内部思考の逐語記録は書きません。
