# Graphエッジ構築方式

## 1. 目的

GraphRAGの成否は、Graphエッジの品質に強く依存する。Phase 1では、エッジ種別ごとに抽出方式を明示し、LLM抽出に頼りすぎない。

## 2. 初期対象エッジ

Phase 1では以下に絞る。

```text
HAS_CONTENT_UNIT
REFERENCES
DEFINES
USES_TERM
EXCEPTION_TO
```

lawqa_jp向けの主対象:

```text
HAS_CONTENT_UNIT
REFERENCES
DEFINES
USES_TERM
EXCEPTION_TO
```

## 3. 抽出方式

| edgeType | 初期抽出方式 | 備考 |
|---|---|---|
| HAS_CONTENT_UNIT | XML構造からルール生成 | 信頼度1.0 |
| REFERENCES | 条文中の「第X条」「前条」「同項」「同号」等をルール抽出 | 法令XMLの構造と正規表現で生成 |
| DEFINES | 「...とは」「...をいう」「定義する」等をルール抽出 + 必要に応じLLMレビュー | 金商法第2条などで重要 |
| USES_TERM | 定義語辞書からルール抽出 | 定義語ノード生成後に実施 |
| EXCEPTION_TO | 「ただし」「除く」「この限りでない」等を条文内位置つきで抽出 | 自動エッジは低confidenceにする |

## 4. relationSource / relationConfidence

```text
relationSource:
  xml_rule       # XML構造から機械的に生成
  regex_rule     # 正規表現で抽出
  llm_candidate  # LLMが候補抽出。未レビュー
  llm_reviewed   # LLM候補を人がレビュー
  manual         # 人手定義
```

初期confidence:

```text
xml_rule:      1.0
regex_rule:    0.7〜0.9
llm_candidate: 0.5
llm_reviewed:  0.8〜0.95
manual:        1.0
```

## 5. Phase 1の作業項目

1. 法令XMLからLaw / Article / Paragraph / Itemノードを生成
2. HAS_CONTENT_UNITを生成
3. 条文番号参照を正規表現で抽出しREFERENCESを生成
4. 定義語候補を抽出しTerm / Definition / DEFINESを生成
5. 例外表現を検出しEXCEPTION_TO候補を生成
6. dangling edge検査を実施
7. 抽出結果をサンプル問題で目視確認

## 6. 注意点

Graph edgeだけで回答しない。Graphは関連条文の展開に使い、最終回答では必ずsource_fetch_toolで本文を取得して引用する。
