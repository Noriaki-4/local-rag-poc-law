# 検索要求の作成

入力済みのHypothesisを検証するため、今回実行する`legal_search`要求を作ります。

## 手順

1. 各Hypothesisの命題と`gaps`を確認します。
2. `available_tools`で`legal_search`の用途、入力項目、戻り値を確認します。
3. 今回の上限内で、未検証Hypothesisを調べる検索要求を作ります。

## ルール

- WorkItemやHypothesisの内容は変更しません。
- 1つの検索要求は、同じWorkItemに属する1件以上のHypothesisを対象にします。
- 検索語は質問文をそのまま繰り返さず、制度名、行為または法的効果、判定軸を法令本文に現れやすい語で組み合わせます。
- 根拠条文やArticle IDを推測しません。
- 上限内で今回検索しないHypothesisが残っても削除しません。Tool結果の評価後に次の検索を判断します。
