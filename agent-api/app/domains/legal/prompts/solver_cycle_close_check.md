## 出力前の完了確認

1. 元の質問が明示する各観点を、WorkItemとHypothesisで追跡できているか確認します。不足する観点は追加し、次Cycleへ引き継ぎます。
2. WorkItemを`resolved`にする場合、resolutionを支える判定済みHypothesis IDを
   `basis_hypothesis_ids`へ設定し、回答へ影響するunresolved Hypothesisを残していないか確認します。
3. DependencyDecisionを`resolved`にするのは、委任元と末端の具体化規定の本文根拠が両方ある場合だけです。
4. 未確認事項が残り`can_start_next_cycle=true`なら、`next=continue`、`start_next_cycle=true`、`answer=null`にします。
5. 不足があればDecisionを修正してから返します。確認結果の説明文は追加しません。
