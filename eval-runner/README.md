# Evaluation Runner

評価ランナーは、新Agent Frameworkの汎用Answer APIを外部データセットで採点します。

## 境界

- Agent APIへ送るのは、質問と任意の回答候補だけです。
- `lawqa_jp/`がlawqa_jpの読込、候補ID正規化、正解ラベル、採点補助を担当します。
- 正解、コンテキスト、期待参照はAgent Frameworkや法令Domainへ渡しません。
- 再利用先でlawqa_jp評価が不要なら、`eval-runner/lawqa_jp/`を含める必要はありません。

Agent Frameworkは回答候補を汎用の`AnswerOption`として扱い、最終回答の
`selected_option_id`を返します。A〜Dやlawqa_jp固有の規則は持ちません。
