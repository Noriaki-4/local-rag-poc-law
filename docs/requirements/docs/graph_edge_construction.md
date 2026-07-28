# Graphエッジ構築方式

## 1. 目的

GraphRAGの成否は、Graphエッジの品質に強く依存する。Phase 1では、エッジ種別ごとに抽出方式を明示し、LLM抽出に頼りすぎない。

## 2. 初期対象エッジ

Phase 1では以下に絞る。

```text
HAS_CONTENT_UNIT
REFERENCES
IMPLEMENTS
APPLIED_BY
DEFINES
USES_TERM
EXCEPTION_TO
```

lawqa_jp向けの主対象:

```text
HAS_CONTENT_UNIT
REFERENCES
IMPLEMENTS
APPLIED_BY
DEFINES
USES_TERM
EXCEPTION_TO
```

## 3. 抽出方式

| edgeType | 初期抽出方式 | 備考 |
|---|---|---|
| HAS_CONTENT_UNIT | XML構造からルール生成 | 信頼度1.0 |
| REFERENCES | 条文中の「第X条」「前条」「同項」「同号」等をルール抽出 | 法令XMLの構造と正規表現で生成 |
| IMPLEMENTS | 下位法令の「法第X条」または「令第X条」REFERENCESを反転し、親法律・施行令の条文から下位法令条文へ接続 | 委任規定の逆引き。下位→親はREFERENCESとして保持 |
| APPLIED_BY | 「準用」を含むREFERENCESを反転して準用先から準用元へ接続 | 準用関係の逆引き |
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
4. 下位法令の親法律・施行令参照からIMPLEMENTS、準用参照からAPPLIED_BYを逆向きに生成
5. 定義語候補を抽出しTerm / Definition / DEFINESを生成
6. 例外表現を検出しEXCEPTION_TO候補を生成
7. dangling edge検査を実施
8. 抽出結果をサンプル問題で目視確認

## 6. 注意点

Graph edgeだけで回答しない。Graphは関連条文の展開に使い、最終回答では必ずsource_fetch_toolで本文を取得して引用する。

委任関係は法令系統ごとに解決する。府省令等の「法第X条」は系統の親法律へ、
「令第X条」「同令第X条」「本令第X条」「当該政令第X条」は同じ系統でタイトルが
「施行令」で終わる政令へ接続する。単に同じ条番号を持つ別法令へは接続しない。
この関係はseed時に生成するため、抽出規則を変更した環境では `/admin/seed` による
Graph再構築が必要になる。

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
