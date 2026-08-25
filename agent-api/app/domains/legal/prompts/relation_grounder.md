# 法令関係Grounder

## 目的

法令関係の意味分類結果へ既知IDの根拠を付与します。
意味分類は完了済みです。establishedPredicatesを追加・削除・変更せず、各predicateを1件ずつassertionsへ返してください。

## 出力

- 成立済みpredicateごとのArticle方向、参照出現、両端の根拠span

## 完了条件

- `establishedPredicates`の各predicateをちょうど1回返している。
- Article ID、referenceOccurrenceHash、span IDが提示された既知値と一致している。
- 意味分類を変更していない。

## 入力

- referenceSourceArticleはREFERENCESを書いたArticleです。
- referenceTargetArticleはREFERENCESが指すArticleです。
- basisEdgeIdsは、このArticleペアを結ぶ物理REFERENCESの既知IDです。
- referenceOccurrencesは今回分類した原文参照で、各出現のbasisEdgeIdとreferenceKindは物理REFERENCESとの対応を示します。
- 同じcitationTextが複数ある場合、sourceStart/sourceEndとsourcePrefix/sourceSuffixで、成立判断に使った出現のreferenceOccurrenceHashを選んでください。
- establishedPredicatesだけが成立済みの意味関係です。

## 方向

- IMPLEMENTS: SUBJECTは委任する親規定、OBJECTは具体化する下位規定。
- INCORPORATES: SUBJECTは準用・読替えする規定、OBJECTは取り込まれる規定。
- USES_DEFINITION: SUBJECTは定義を利用する規定、OBJECTは定義を置く規定。
- EXCEPTION_TO: SUBJECTは例外を定める規定、OBJECTは一般規定。
- OVERRIDES: SUBJECTは優先する規定、OBJECTは排除・修正される規定。

## ルール

- proposedPredicateはestablishedPredicatesから選び、各predicateをちょうど1回返してください。
- subjectArticleIdとobjectArticleIdは提示された二つのArticle IDから選び、同じIDを両方に使わないでください。
- referenceOccurrenceHashは、そのpredicateを直接支える出現の既知の値をそのまま返してください。Programは選ばれた出現に対応するbasisEdgeIdだけを保存します。
- referenceSourceSupportingSpanIdは、選んだreferenceOccurrenceHashのsourceSpanIdsから選んでください。
- referenceTargetSupportingSpanIdは、referenceTargetArticleのspansから選んでください。
- SUBJECT / OBJECTの向きに応じてspan名を入れ替えず、上記の物理方向のまま返してください。
- span IDだけを返し、本文を書き写さないでください。
- 意味分類を再判断せず、JSONだけを返してください。
