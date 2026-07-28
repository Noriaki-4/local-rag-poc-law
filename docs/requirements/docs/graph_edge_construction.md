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

## 7. vNext（法令レイヤー別探索）でのエッジ拡張

`layered_legal_evidence_retrieval_plan.md` §6 に対応する実装済みの差分。エッジ種別の定義は
`agent-api/app/legal_ontology.py` の `EDGE_REGISTRY` を機械可読な正とし、seed・検索・監査は
同じレジストリを参照する。ドキュメントとコードとNeo4jの一致は
`scripts/graph_inventory.py` と `agent-api/app/graph_audit.py` で検査する。

### 実装済みエッジ

| edgeType | 方向 | 追加プロパティ | 備考 |
|---|---|---|---|
| `HAS_CONTENT_UNIT` | container → child | - | 従来どおり |
| `REFERENCES` | citing → cited | `referenceKind` | 原文上の参照。法的意味は referenceKind で表す |
| `IMPLEMENTS` | parent → child | `derivedFromEdgeId`, `delegationWordingDetected`, `specificationWordingDetected` | 段階的confidence |
| `APPLIED_BY` | applied → applying | `derivedFromEdgeId` | 現行名・現行方向を維持 |
| `EXPLAINS` | ガイド文書 → 条 | - | 条文注釈・対応表で明示された参照だけ |
| `MENTIONS` | ガイド文書 → 条 | - | 前ページからの引き継ぎ等の単なる言及。探索拡張・根拠充足には使わない |

`referenceKind`: `article_reference` / `delegation_parent` / `application` / `definition` /
`exception` / `form_or_table`。

### IMPLEMENTS の段階的confidence

固定値 0.95 を廃止し、根拠の強さで段階化する。下位法令が親条文を参照するだけでは
IMPLEMENTS にせず、`REFERENCES` のまま残す。

| 値 | 条件 |
|---:|---|
| 1.00 | manual |
| 0.98 | 親条文の委任文言(「政令で定める」等)と下位法令の親参照の両方を確認 |
| 0.90 | 同一法令系統かつ下位法令側の具体化表現を確認 |
| 0.70 | 親条文への参照だけ。IMPLEMENTSを作らずREFERENCESのままにする |
| 0.50 | ガイド由来の未確認候補。RelationAssertion |

信頼判定は confidence の数値だけでなく、`relationSource` が許可値であること、派生エッジに
`derivedFromEdgeId` があること、委任文言が検出されていること、未確認assertionでないことを
同時に検証する(`legal_ontology.is_trusted_relation`)。

### RelationAssertion（ガイドが示唆する未確認関係）

ガイドが示唆した法令間関係は、正式なArticle間エッジにせず `RelationAssertion` ノードとして
保存する(`status=unverified`, `confidence=0.5`)。候補拡張だけに使い、根拠充足・mustInclude・
法令関係図の確定線には使わない。法令本文で確認できた場合に正式な関係を作る。

v1の抽出規則は、条文注釈で法律の条に紐づいたガイドチャンク本文に「(施行)令第N条」がある場合に、
同一法令系統の施行令の条への `IMPLEMENTS` 候補を作るところまで。府令名の明示的な言及からの
抽出は未実装。

### 未実装エッジ

`DEFINES` / `USES_TERM` / `EXCEPTION_TO` はレジストリに定義だけ置き、`implemented=false` と
している。seedされず、探索拡張のallowlistにも入らず、これらを前提にした子Requirement生成も
無効になる。実装する場合は seed・検索・監査を同時に追加し、Graph schema version を上げて
再シードする。

### 検索時の法令系統スコープ

委任先(政令・府省令)のRequirementは、親条文が属する法令系統(`law_registry.json` の
`familyRoot`)内のdocumentIdへ絞って検索する。実測で、薬機法の質問に対して「政令」レイヤーの
検索が金融商品取引法施行令へ届く事象を確認したため導入した。registryに無い法令系統では
絞り込みを行わず、従来どおり全法令から探す。
