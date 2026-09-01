## 出力前の確認

- 同じ命題の見立てを直す場合は、既存IDを`revise_hypotheses[]`へ返しているか。
- 独立した未解決命題だけを`add_hypotheses[]`へ返しているか。
- 既存Hypothesisの言い換え、個別要件又は検索方針を新規追加していないか。
- 更新後の`statement`と`judgment`が提示本文と整合しているか。
- 既存gapを意図せず失っていないか。新規内容は`add_gaps[]`、確認済み又は不要な既存項目は
  `resolve_gap_ids[]`に分け、表現を直す場合は解消と追加を組み合わせたか。
- 各項目の`evidence_ids[]`を、必要性を直接示す最小限の根拠に絞ったか。
- 指定したHypothesis、WorkItem及びEvidence IDが入力に存在するか。
- 更新も追加もなければ両方を空配列にしたか。
