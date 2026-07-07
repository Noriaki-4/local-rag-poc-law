# lawqa_jp 多肢選択法令QA 回答手順マニュアル

## 1. 基本方針

問題文と選択肢を読み、各選択肢の主張を法令本文に照らして判定する。正解選択肢を選ぶだけでなく、根拠条文を引用する。

## 2. 解法手順

1. 問題文の法的論点を抽出する。
2. 選択肢ごとに、確認すべき主張を分解する。
3. 法令名、条、項、定義語、例外表現を検索語にする。
4. 法令本文を検索する。
5. 必要に応じてGraphRAGで定義・参照・例外条文を展開する。
6. 各選択肢を supported / contradicted / not_supported に分類する。
7. 最も根拠に合う選択肢を predictedAnswer とする。
8. 法令本文の引用を付ける。

## 3. GraphRAGを使う条件

次の場合は、Vector検索結果だけで回答せず、GraphRAGで関連条文を展開する。

- 「定義」「みなす」「規定する」が出る
- 「ただし」「除く」「この限りでない」が出る
- 「前条」「次条」「同項」「同号」「別に定める」が出る
- 選択肢判断に複数条文が必要
- 取得した条文だけでは選択肢の正誤を判定できない

## 4. 出力形式

```json
{
  "predictedAnswer": "A|B|C|D",
  "choiceJudgements": {
    "A": "supported|contradicted|not_supported",
    "B": "supported|contradicted|not_supported",
    "C": "supported|contradicted|not_supported",
    "D": "supported|contradicted|not_supported"
  },
  "citations": []
}
```
