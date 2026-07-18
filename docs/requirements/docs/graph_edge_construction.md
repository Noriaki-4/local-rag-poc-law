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
| EXPLAINS | ガイドラインPDFの対応表・条文注釈から抽出した「ガイドライン文書→法令条文」対応 | `seed.py` の `_guidance_graph_artifacts`。張り元はガイドライン Document ノード、張り先は既存の Article ノード。詳細は下記6.1 |

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

## 6.1 EXPLAINS(ガイドライン→法令条文)

抽象的な委任規定(例: 薬機法第18条の2「...体制を整備すること」)は、具体的な文言で
書かれたガイドライン解説とのスコア競争でRRF/rerankerに負け、最終引用から漏れることが
ある。ガイドラインPDFには「本ガイドラインの見出しと法令との対応表」や「(法第N条関係)」
形式で解説対象の条文が明記されているため、この対応を **EXPLAINS エッジ**としてグラフに
載せ、「羅針盤」として使う。

- **投入(`seed.py`)**: docling前処理済みのガイドライン(`preprocess-worker`)から、
  各チャンクの `relatedArticleContentUnitIds` を **文書単位で集約**し、
  `ガイドライン Document ノード -EXPLAINS-> 法令 Article ノード` を張る
  (`_guidance_graph_artifacts`)。`relationSource="guidance_article_annotation"`、
  `relationConfidence=0.9`。張り先条文が(法令の部分投入等で)グラフに無い場合は
  `_drop_dangling_explains_edges` で該当EXPLAINSだけ落とし、seed全体は止めない。
- **検索(`agent.py` `_inject_guidance_explained_articles`)**: 上位候補に入った
  ガイドラインチャンクの `documentId` からEXPLAINSを辿って解説対象条文を特定し、
  スコアに依らず `mustInclude=True` で候補プールへ投入する(羅針盤の結果を
  reranker再採点で捨てない)。ただし大きな対応表がrerank枠を埋め尽くさないよう
  `GUIDANCE_EXPLAINS_MAX_ARTICLES` 件で打ち切る。条文本文の取得はOpenSearch側
  (Vector RAG)の役割で、最終的に引用するかはLLMの判断に委ねる。
