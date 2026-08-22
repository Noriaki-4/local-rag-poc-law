## 完了ルール

- 質問の各観点をWorkItemとHypothesisで追跡します。
- 回答と各WorkItemのresolutionを、直接対応するgrounding Evidenceと照合します。
- 特定の法令・Articleを説明する場合は、そのArticle自身のEvidenceをcitationへ含めます。
- resolved WorkItemのbasis Hypothesisが使うEvidenceを、回答のcitationから落としません。
- 質問に関係する下位規範の委任が残る場合は、末端の具体化規定を確認するまで完了にしません。
- 調査可能な未確認事項が回答へ影響する場合は`continue`します。`limitations`で代用しません。
- 通常の`finalize`では全WorkItemを`resolved / dropped`にし、limitationsと未解決IDを空にします。
- 上限により調査できない場合だけ、open WorkItemとunresolved Hypothesisを保ち、limitationsと未解決IDを対応させます。
