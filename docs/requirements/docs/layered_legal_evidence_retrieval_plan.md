# 法令レイヤー・法的役割別の根拠探索 vNext 設計・実装計画

## 1. 文書の位置づけ

本書は、自然言語の法令質問に対して、質問と似た条文を平面的に検索・再ランキングする方式から、
論点ごとに必要な法令レイヤーと法的役割をたどり、根拠構造を完成させる方式へ移行するための
設計・実装計画である。

既存の `legal_issue_coverage_retrieval.md` は、再ランカー入力30chunksから回答コンテキスト
16chunksを選ぶ段階を改善する移行設計である。本書のvNextは、その前段にあるクエリ分解、
候補生成、Graph展開、追加検索ループ、再ランキング単位そのものを変更する。

```text
現行の論点被覆方式
  候補プール → 30chunks → 16chunks の選抜改善

本書のvNext
  質問
    → 論点・必要根拠スロット
      → 法令レイヤー別探索
        → Article単位の候補管理
          → 委任・準用・定義・例外の反復展開
            → 根拠構造の充足確認
              → 必要chunkの選択
```

本書は実装前レビュー用である。記載した上限値は初期値案であり、20問の自然言語質問、
lawqa_jp 140問、関係エッジのサンプル監査によって調整する。

本システムは法的判断の正しさを保証しない。回答は検索された法令・行政資料に基づく参考情報とし、
具体的な事案では必要に応じて専門家による確認を求める。

## 1.1 実装状況（2026-07-28時点）

本書の設計に対する実装の到達点。コード・テストの所在を含めて記録する。

| 範囲 | 状況 | 主なモジュール |
|---|---|---|
| §5-6 オントロジー(authorityType / role / edge registry / schema version) | 実装済み | `agent-api/app/legal_ontology.py` |
| §6.1 referenceKind・derivedFromEdgeId・IMPLEMENTS段階confidence・MENTIONS・RelationAssertion | 実装済み(再シード要) | `agent-api/app/seed.py`, `legal_relation_resolver.py` |
| §6.3 Graph監査 | 実装済み(seed時に自動実行、違反はmanifestとログへ) | `agent-api/app/graph_audit.py` |
| §7 論点・EvidenceRequirement・conclusionGroup | 実装済み | `evidence_requirements.py`, `legal_issue_planner.py` |
| §8 ラウンド単位の反復探索・子Requirement生成・停止条件 | 実装済み | `layered_retriever.py` |
| §8.5 複数edge type batch traversal・Neo4j timeout | 実装済み | `graph_client.py` |
| §9 レイヤー・役割別BM25/Vector/直接取得・Requirement別Cross-Encoder・充足判定 | 実装済み | `opensearch_client.py`, `requirement_reranker.py`, `requirement_satisfaction.py` |
| §6.3-7/§9.1 委任先探索の法令系統スコープ | 実装済み(law_registryのfamilyRootで同一系統へ限定) | `law_family.py` |
| §10 ガイドレーン(ガイド検索→EXPLAINS→法令本文→補助枠) | 実装済み | `agent-api/app/guidance_lane.py` |
| §11 上限・時間予算・shadow予算 | 実装済み(初期値案のまま。Phase 0実測で確定) | `retrieval_budget.py`, `config.py` |
| §11.6 conclusionGroup単位の原子的コンテキスト配分・answerStatus | 実装済み | `layered_context_assembler.py` |
| §8.7 条件付きLLM再計画(最大1回) | **未実装** | - |
| §10-7 ガイドの位置づけ表示(行政解釈として明示) | 実装済み(`Citation.evidenceLane` / 回答プロンプト) | `models.py`, `llm.py` |
| §13 trace | 実装済み(`trace.layeredLegalRetrieval`) | `layered_shadow.py` |
| §11.2 /healthのwall time公開とeval-runnerの整合検証 | 実装済み | `agent-api/app/main.py`, `eval-runner/run_eval.py` |
| §15 Phase 0の実測(旧方式baseline・Cross-Encoderスループット・切り詰め計測) | **未実施**。稼働環境が必要 | `scripts/graph_inventory.py` |
| §15 Phase 6の切替判断(20問・140問評価) | **未実施** | - |
| §17 group被覆・新コンテキスト条文到達・answerStatusの評価ランナー反映 | 実装済み(metricVersion 6) | `eval-runner/run_eval.py` |

### 再シード後の実測 (2026-07-28, 280秒運用評価profile)

`SEED_LAWQA_EGOV=true SEED_EXTERNAL_GUIDANCE=true` でフル再シード(21分)し、shadowで確認した結果。

| 項目 | 結果 |
|---|---|
| Graph監査(§6.3) | 違反0件。schema version 3。Document / Article / Paragraph / Itemの全ノードへversionを付与 |
| authorityType | act 2,502 / cabinet_office_ordinance 1,620 / ministerial_ordinance 968 / cabinet_order 635 / guidance 6(Documentノード)。`ordinance_unspecified`と`unknown`は0件 |
| authoritySource | registry人手確認 7,291件 / lawId自動判定 7,450件 / docType 1,717件 |
| IMPLEMENTS | 4,475 → 4,415件。confidence 0.98が3,594件、0.90が821件。固定0.95を廃止し、単純参照だけの60件はREFERENCESのままにした |
| MENTIONS / RelationAssertion | 0件。現コーパスでは安全に抽出できる根拠が無いことを確認済み。全6ガイドのDocumentノードは登録し、関係を推測で生成しない |
| MinIO同期 | OpenSearchが参照する16,458 vector文書と同期。過去seedの未参照5,606件を削除し、以後のseedでも管理prefix内を自動同期 |
| §2の失敗ケース(公開買付府令10条) | 到達。最終16枠に法律27条の3・施行令9条の3・府令10条の3レイヤーが揃い`answerStatus=complete` |
| shadow phase | 13〜16秒 / 予算20秒。現行回答への影響なし |

判明した設計上の問題と対応:

1. cue由来の子Requirement(準用・ただし書等)は仮説であり、候補が1件も見つからないことがある。
   初期実装は候補ゼロのmandatory memberをgroupの被覆対象から外していたため、不足した根拠を
   隠して`complete`になり得た。mandatory memberは候補ゼロでもgroupに残し、group全体を
   `omitted_context_budget` / `unresolvedForAnswer`へ送るよう修正した(§11.6-3, §8.8)。
2. 委任先の検索が別の法令系統(薬機法の質問に対する金融商品取引法施行令)へ届いていた。
   親条文の`familyRoot`内へ限定する`law_family.py`を追加した(§6.3-7)。

未確定のまま残っている点:

- 1論点あたりのRequirement数がcue検出で増えやすい(実測で1論点→17件)。上限内には収まるが、
  Article候補枠の希釈度合いはPhase 0/6で計測して`MAX_REQUIREMENTS_TOTAL`等を再設定する。
- Phase 0のCross-Encoderスループット計測と旧方式baselineは未実施。

feature flagは既定で無効。`AGENT_LAYERED_LEGAL_RETRIEVAL_SHADOW=true` でshadow、
`AGENT_LAYERED_LEGAL_RETRIEVAL=true` でactive(主論点groupを被覆できた場合だけ新コンテキストを
回答へ渡す)。主論点の意味上の根拠不足時は旧経路の通常回答へ戻さず、利用者へ根拠不足を
明示して断定を止める。新方式の内部障害時だけ旧経路へ戻る。

Phase 0の実測値が入るまで、§11.1の上限とprofile選択は暫定値である。20秒(110秒profile)を
合格ゲートとして採用済みという意味ではない。

## 2. 背景と確認済みの問題

20回の自然言語質問では、期待した根拠条文61件に対して次の結果だった。

```text
候補プール到達             60 / 61  98.4%
再ランカー入力30件到達     55 / 61  90.2%
回答根拠16件到達           51 / 61  83.6%
回答要点                   58 / 64  90.6%
全採点項目の厳格合格        9 / 20  45.0%
```

主な失敗は次のとおりである。

1. plannerは質問を話題別には分解できるが、法律、政令、府令という法令レイヤーを分解しない。
2. 親法律を明記したクエリが多く、法令内検索も親法律へ集中する。
3. Graphに必要な委任関係が存在しても、起点数・経路数・資料数の制限で接続先が候補に入らない。
4. 同じArticleの項・号chunksが候補枠を消費する。
5. 質問全文によるCross-Encoderは、法的に必要な具体化条文を低く評価することがある。
6. 現行の追加検索は「未解決の法的役割」ではなく、候補件数や自由文クエリを基準に停止する。
7. ガイドラインは自然言語と法令を結ぶ有力な索引だが、法令と同じ候補枠で競争する場合がある。
8. Graphエッジの文書上の定義と、実際の検索ループで利用するエッジに差がある。

公開買付け質問では、公開買付府令10条が質問全文とのCross-Encoderで26位、
手続論点とのCross-Encoderで3位となった。これはモデルだけの問題ではなく、複数論点を含む
質問全文と、単一の具体化条文を比較する単位の問題である。

## 3. 設計目標

1. Articleと論点を区別し、多対多の対応を明示する。
2. 法令の階層と、質問に対する法的役割を別軸で管理する。
3. 法律だけでなく、政令・府令のArticleについても委任・準用・定義・例外を反復展開する。
4. 必要な下位法令を質問全文との類似度だけで候補から除外しない。
5. Cross-Encoderを、同一の論点・役割・レイヤー内の順位付けへ限定する。
6. ガイドを法令の代替ではなく、法令と法令間関係を発見する羅針盤として使う。
7. 検索・Graph・再ランキングの上限をchunk数中心から、論点、根拠スロット、Article、ホップ中心へ変更する。
8. 初期plannerが必要役割を完全に予測できなくても、探索ループで不足を追加できるようにする。
9. goldの条文、正答、評価要点を検索・再ランキング・回答生成へ渡さない。
10. 旧方式と新方式をshadow modeで比較し、段階的に切り替える。

## 4. 対象外

初期実装では次を行わない。

- 問題ID、質問文、gold条番号ごとの固定ルール
- LLMだけで法令間関係を確定すること
- ガイドだけを法的結論の直接根拠にすること
- 全法令レイヤーを毎回強制的に検索すること
- 全候補を無制限にGraph展開すること
- Cross-Encoderのスコアだけで法的含意・矛盾・根拠完全性を確定すること
- 判例、通達、自治体例規を初期リリースの必須レイヤーに加えること
- 法令時点の差異や既知gold問題を検索ロジックで吸収すること

### 4.1 実装着手前のBlocking

次が解消するまで、Phase 3以降の検索実装へ着手しない。

1. `authorityType`の生成元、registry schema、OpenSearch mapping、Graph propertyを確定する。
2. 現在seedされる5種のエッジと、未実装の`DEFINES`、`USES_TERM`、`EXCEPTION_TO`、
   `MENTIONS`を区別し、新規実装範囲を確定する。
3. 旧方式を110秒互換profileと280秒運用評価profileで実測し、採用する対応profileを確定したうえで、
   ローカルCross-Encoderの実測スループットと処理可能なquery-Articleペア数を確定する。
4. `AGENT_MAX_LLM_CALLS`のハード上限3と、再計画1回を含む論理LLM呼び出し設計を整合させる。
5. `LLM_MAX_CONTEXT_CHARS=12000`で切り詰めずに渡せるchunk数・文字数を計測し、
   最終コンテキスト上限を確定する。
6. 最大16chunksに対するmandatory Requirementの最低配分、超過時の優先順位、
   `unresolvedForAnswer`への引継ぎを確定する。

ここでいう「既定20秒」はコード既定値から得た安全残余の候補値であり、性能ゲートとして
採用済みという意味ではない。旧方式が同じprofileで成立することをPhase 0の最初に確認し、
成立しなければ運用profileを再定義してからPhase 3以降へ進む。

## 5. 基本概念

### 5.1 4つの独立した軸

次を同じenumや同じフラグへ混在させない。

| 軸 | 問い | 例 |
|---|---|---|
| ノード種別 | 何であるか | Law、Article、Paragraph、Item、Term、Guidance |
| Graph関係 | 何とどうつながるか | REFERENCES、IMPLEMENTS、EXPLAINS |
| 法的役割 | 質問に対して何を担うか | 原則、定義、例外、手続 |
| 証拠状態 | 根拠としてどこまで確認できたか | unresolved、candidate、resolved |

Articleは論点ではない。1つの論点が複数Articleを必要とし、1つのArticleが複数論点を支える。

```text
LegalIssue
  └─ EvidenceRequirement
       └─ ArticleCandidate
            └─ ContextChunk
```

### 5.2 法令レイヤー

初期対象は次とする。

```text
act                       法律
cabinet_order             政令
ministerial_ordinance     省令
cabinet_office_ordinance  内閣府令
ordinance_unspecified     省令・内閣府令を未判別
guidance                  ガイド・監督指針・Q&A
```

`guidance`は規範的法令レイヤーへ含めず、補助資料レーンとして別管理する。

#### authorityTypeの生成と保存

現行のOpenSearch documentは `docType=law|guideline` しか持たず、`authorityType`は存在しない。
e-Gov lawIdの`AC`は法律、`CO`は政令と判定できるが、`M`だけでは省令と内閣府令を区別できない。
したがって、lawIdの文字列だけから全レイヤーを推測してはならない。

`law_registry.json`へ次を追加し、法令レイヤーの正とする。

```json
{
  "documentId": "law-402M50000040038",
  "authorityType": "cabinet_office_ordinance",
  "authoritySource": "registry_manual_verified"
}
```

生成元の優先順位:

1. 人が確認したlaw registryの明示値
2. lawIdまたはe-Gov `LawType`から一意に判定できる法律・政令
3. e-Gov `LawType=MinisterialOrdinance`は `ordinance_unspecified` とする
4. `LawNum`、タイトル、所管情報から作る省令・内閣府令の候補値。確定値にせず監査対象とする
5. 判定できない場合は `unknown`

e-Gov `LawType`は内閣府令も`MinisterialOrdinance`に含めるため、M系法令の省令・内閣府令を
区別できない。`LawNum`には発令主体が含まれるが、発令時と現在の所管が異なる法令があるため
自動確定に使わない。M系法令を`ministerial_ordinance`または
`cabinet_office_ordinance`へ確定する方法は、実質的にlaw registryの人手確認値を正とする。

保存先:

- `law_registry.json`
- OpenSearch document mappingのkeyword field
- Neo4j Law/Article node property
- seed manifest
- traceのauthority resolution

OpenSearch mappingとGraph propertyの変更を伴うため、Phase 2で一度に再シードする。
`ordinance_unspecified`または`unknown`を省令・内閣府令のどちらかと推測して
確定してはならない。一方、未判別であることを理由にレイヤー指定検索から除外してもならない。

検索時の包含規則:

```text
Requirement = ministerial_ordinance
  → ministerial_ordinance + ordinance_unspecified + unknown

Requirement = cabinet_office_ordinance
  → cabinet_office_ordinance + ordinance_unspecified + unknown

Requirement = ordinance_unspecified
  → ministerial_ordinance + cabinet_office_ordinance
    + ordinance_unspecified + unknown
```

完全一致する`authorityType`を順位上は優先するが、未判別候補を構造的に落とさない。
Graph接続先または明示法令名から`documentId`が確定している場合は、未判別でもその法令内を
直接検索する。

### 5.3 法的役割

役割は階層型かつ複数選択とする。

| roleFamily | roleSubtype |
|---|---|
| `normative_rule` | `general_rule`, `obligation`, `prohibition`, `permission`, `entitlement` |
| `qualification` | `requirement`, `condition`, `exception`, `exclusion`, `special_rule` |
| `meaning_scope` | `definition`, `scope`, `deeming`, `presumption` |
| `procedure` | `filing`, `notice`, `publication`, `approval`, `deadline`, `form` |
| `consequence` | `legal_effect`, `invalidity`, `liability`, `remedy`, `administrative_action`, `penalty` |
| `linkage` | `delegation`, `implementation`, `reference`, `application` |
| `temporal` | `effective_date`, `transitional_measure` |
| `interpretive` | `interpretation`, `supervisory_expectation`, `practice_example` |

例:

```text
公開買付府令10条
  roleFamily: procedure
  roleSubtype:
    - publication
  enteredBy: IMPLEMENTS
```

初期plannerは質問に明示された役割だけを仮説として設定する。取得Articleの本文とGraph関係から、
必要な追加役割を動的に生成する。

`delegated_detail`は法的役割として定義しない。具体化先であることは、`parent_article_id`、
`entered_by=IMPLEMENTS`、relation pathで表す。同じ委任先Articleでも、質問に対する役割は
`definition`、`exception`、`procedure`など異なるためである。

## 6. Graphオントロジーの整理

### 6.1 エッジの分類

現行`seed.py`が生成するのは次の5種である。

```text
HAS_CONTENT_UNIT
REFERENCES
IMPLEMENTS
APPLIED_BY
EXPLAINS
```

以下に記載する`DEFINES`、`USES_TERM`、`EXCEPTION_TO`、`MENTIONS`、
`RelationAssertion`はvNextの新規実装対象であり、現時点で検索できる既存エッジとして扱わない。
実装・再シードが終わるまでは、それらを前提にした子Requirement生成を無効にする。

#### 構造関係

| edgeType | 正規方向 | 意味 |
|---|---|---|
| `HAS_CONTENT_UNIT` | container → child | Law、Article、Paragraph、Itemの包含 |

`HAS_ARTICLE`、`HAS_PARAGRAPH`などの重複エッジは保存しない。始点・終点のnodeTypeで意味を判定する。

#### 原文上の参照

| edgeType | 正規方向 | 意味 |
|---|---|---|
| `REFERENCES` | citing → cited | 条文本文に現れる条文参照 |

`REFERENCES`には `referenceKind` を追加する。

```text
article_reference
delegation_parent
application
definition
exception
form_or_table
```

#### 法的意味を付けた派生関係

| edgeType | 正規方向 | 意味 |
|---|---|---|
| `IMPLEMENTS` | parent/delegating → child/implementing | 下位法令による具体化 |
| `APPLIED_BY` | applied provision → applying provision | 準用の逆引き |
| `DEFINES` | Article → Term | 用語の定義 |
| `USES_TERM` | Article → Term | 定義語の利用 |
| `EXCEPTION_TO` | exception provision → base provision | 原則に対する例外 |

`IMPLEMENTS`と`APPLIED_BY`は検索効率のための派生エッジとして維持できるが、元となる
`REFERENCES`の `graphEdgeId` を `derivedFromEdgeId` に保存する。同一意味の順逆エッジを
独立した事実として管理しない。

派生エッジは現行互換性とGraph traversalの単純さを優先して物理保存する。
`APPLIED_BY`も現行名・現行方向を維持し、`APPLIES`への改名は行わない。

`IMPLEMENTS`は、下位法令が親条文を参照するだけで機械的に確定してはならない。
法令系統、参照表現、委任・具体化の手掛かりを検証し、単純参照しか確認できない場合は
`REFERENCES`のままにする。

現行`IMPLEMENTS`の `relationConfidence=0.95`固定値を廃止し、少なくとも次へ段階化する。

```text
1.00  manual
0.98  親条文の明示的委任文言と、下位法令の親参照の両方を確認
0.90  法令系統と具体化表現を確認した高信頼ルール
0.70  下位法令から親条文への参照だけを確認。REFERENCESのまま
0.50  LLMまたはガイドによる未確認候補。RelationAssertion
```

`minimumTrustedConfidence`は固定値を通すだけでなく、`relationSource`、
委任文言の検出結果、`derivedFromEdgeId`の存在も同時に検証する。

#### ガイド関係

| edgeType / assertion | 正規方向 | 意味 |
|---|---|---|
| `EXPLAINS` | GuidanceDocument/Chunk → Article | 明示的な解説対象 |
| `MENTIONS` | GuidanceChunk → Article | 単なる言及 |
| `RelationAssertion` | assertion node → from/to Article | ガイドが示唆した未確認の法令間関係 |

前案の `SUGGESTS_RELATION` は正式なArticle間エッジにしない。次のような
`RelationAssertion`として保存する。

```json
{
  "assertionId": "assertion-001",
  "fromArticleId": "law-...-article-27_3",
  "toArticleId": "law-...-article-10",
  "suggestedType": "IMPLEMENTS",
  "assertedByDocumentId": "guidance-...",
  "sourceText": "府令第十条に定める...",
  "confidence": 0.6,
  "status": "unverified"
}
```

`unverified`は候補拡張だけに使い、根拠充足、mustInclude、法令関係図の確定線には使わない。
法令本文で確認後、正式な関係を生成して `law_text_verified` とする。

### 6.2 エッジレジストリ

エッジ種別ごとに次を機械可読なレジストリへ定義する。

```yaml
IMPLEMENTS:
  fromNodeTypes: [Article]
  toNodeTypes: [Article]
  direction: parent_to_child
  normative: true
  canExpandSearch: true
  canSatisfyEvidence: false
  requiresFetchedTargetText: true
  allowedSources:
    - regex_rule
    - llm_reviewed
    - manual
  minimumTrustedConfidence: 0.9
```

必要項目:

- 許可される始点・終点nodeType
- 正規方向と逆引き表示名
- 規範的関係か補助関係か
- 検索拡張に使えるか
- 関係だけで根拠充足にできるか
- 本文取得が必要か
- 許可される `relationSource`
- 信頼扱いする最低confidence
- 法令時点

### 6.3 Graph監査

seed後に次を自動検査する。

1. dangling edgeが0件である。
2. `HAS_CONTENT_UNIT`に循環がない。
3. ParagraphとItemの親が一意である。
4. `parentContentUnitId`と`HAS_CONTENT_UNIT`が一致する。
5. 派生エッジの `derivedFromEdgeId` が存在する。
6. 同じ意味の重複エッジがない。
7. `IMPLEMENTS`が同一法令系統の親へ接続している。
8. `EXPLAINS`の始点がガイド、終点が法令Articleである。
9. `RelationAssertion(unverified)`が確定関係として利用されていない。
10. `legalAsOf`または法令バージョンが追跡可能である。
11. 全Law/Articleに`authorityType`があるか、`unknown`として明示されている。
12. `authorityType`がregistry、OpenSearch、Neo4jで一致する。
13. 現在seedされるエッジ種別と検索可能なエッジ種別が一致する。

オントロジーまたは抽出規則を変更した場合はGraph再シードを必要とする。Graph schema versionを
seed metadataとtraceへ保存する。

## 7. 論点と必要根拠スロット

### 7.1 データモデル

```python
@dataclass
class LegalIssue:
    issue_id: str
    label: str
    question_span: str
    key_terms: list[str]
    requested_role_families: list[str]
    explicit_references: list[str]
    confidence: float


@dataclass
class EvidenceRequirement:
    requirement_id: str
    issue_id: str
    conclusion_group_ids: list[str]
    role_family: str
    role_subtypes: list[str]
    authority_type: str | None
    parent_article_id: str | None
    entered_by: str
    mandatory: bool
    retrieval_status: str
    context_status: str
    attempts: int
    candidate_article_ids: list[str]
    accepted_article_ids: list[str]
    unresolved_reference_ids: list[str]
```

`retrieval_status`は次とする。

```text
unresolved
searching
candidate_found
resolved
ambiguous
exhausted
not_applicable
```

`context_status`は検索中は`pending`とし、最終コンテキスト選抜後に
`included`、`shared_coverage`、`omitted_context_budget`のいずれかへ更新する。
検索上の解決と、回答LLMへ根拠を渡せた状態を混同しない。

`conclusion_group_ids`は、原則と例外など一体で回答根拠に含めるRequirementを束ねる。
同じ定義Articleが複数の結論を支える場合があるため多対多とする。一体性を必要としない
Requirementには、自身だけを含む安定したgroup IDを割り当てる。

### 7.2 初期論点抽出

plannerは次のJSONを返す。

```json
{
  "issues": [
    {
      "label": "公開買付けの適用要件",
      "questionSpan": "手続が必要になるのはどのような場合",
      "keyTerms": ["市場外買付け", "株券等所有割合"],
      "requestedRoleFamilies": ["normative_rule", "qualification"],
      "explicitReferences": [],
      "confidence": 0.9
    }
  ],
  "graphPotentiallyRequired": true
}
```

plannerには正式法令名や条番号を断定させない。質問に明示された法令名・条番号は既存の
決定的パーサーで抽出し、planner結果へ統合する。

### 7.3 ルール補正

質問中の表現からrole仮説を補う。

| 表現 | roleFamily / subtype |
|---|---|
| 「とは」「意味」「対象」 | `meaning_scope` / `definition`, `scope` |
| 「どのような場合」「要件」 | `qualification` / `requirement` |
| 「例外」「除外」「ただし」 | `qualification` / `exception` |
| 「手続」「提出」「公告」「届出」 | `procedure` |
| 「違反」「罰則」「責任」 | `consequence` |
| 「期間」「いつまで」 | `procedure/deadline`または`temporal` |
| 「準用」「同じ扱い」 | `linkage/application` |

LLMとルールの結果が競合した場合は両方を仮説として残し、Article取得後に解消する。

### 7.4 論点数

論点数を4件に固定しない。

```text
最小:              1
soft limit:         6
hard limit:         8
同時処理batch:      4
```

hard limitを超える論点は削除せず、複数batchへ分けるか、回答範囲を利用者へ明示する。
下位法令で発見した具体化・例外・定義は新しい主論点ではなく、既存論点の
`EvidenceRequirement`として数える。

### 7.5 必要役割は仮説として開始する

初期plannerが全役割を正しく予測する前提を置かない。

```text
質問から明示された役割
  ↓
起点Articleを取得
  ↓
Article本文とGraph関係を解析
  ↓
必要な子Requirementを追加
```

例:

```text
issue: 公開買付けの適用除外

初期Requirement
  exception / act

金商法27条の2に「政令で定める」
  ↓
追加Requirement
  exception / cabinet_order
  enteredBy: IMPLEMENTS

施行令7条に「内閣府令で定める」
  ↓
追加Requirement
  condition / cabinet_office_ordinance
  enteredBy: IMPLEMENTS
```

### 7.6 conclusionGroupの生成

plannerに最終group構造を断定させない。初期質問とRequirementの依存関係から決定的に生成する。

1. 質問が直接求める独立した法的結論ごとに安定したgroup IDを作る。
   利用者の明示条項を含むgroup、または質問が直接求める結論・定義・要件・手続・期限等の
   groupを`isPrimary=true`とする。P0/P1は必ずprimaryだが、primaryをP0/P1だけに限定しない。
2. その結論の原則・義務・禁止と、結論を変え得る例外・除外・適用範囲を同じgroupへ入れる。
3. 親Requirementを完成させる委任先、準用先、必須定義は親のgroup IDを継承する。
4. 単なる補足説明、別個に回答できる手続・期限は独立groupとする。
5. 1 Requirementが複数の結論に必要なら、複数group IDへ所属させる。
6. Article取得後に依存関係が判明した場合はgroup memberを追加できるが、既存groupを黙って
   分割・削除せずtransitionをtraceへ残す。

groupの完全性は、所属するmandatory Requirementがすべて回答コンテキストで被覆されたかで判定する。

## 8. 反復探索ループ

### 8.1 現行ループの問題

現行は最大5クエリを一括検索し、Evaluatorが不足と判断した場合に最大1回の自由文追加検索を
行う。停止条件には候補件数が含まれ、委任先や必要役割が未解決でも停止し得る。

### 8.2 ラウンド単位の探索キュー

全体を最初から何度も実行せず、未解決Requirementだけをラウンド単位でbatch処理する。
1件ずつ `pop` するとOpenSearch multi-searchと両立しないため、同じラウンドで処理可能な
Requirementを最大`ACTIVE_ISSUE_BATCH_SIZE`論点ぶん取り出す。

起点法律を取得する初期検索はround 0として別枠にする。round 0は1 batchだけではなく、
初期Requirementを最大`ACTIVE_ISSUE_BATCH_SIZE`論点ずつ、時間・件数予算内で枯渇するまで処理する。
複数batchになってもround indexは0のままであり、展開ラウンドを消費しない。
`MAX_EXPANSION_ROUNDS=3`は、round 0で取得したArticleから子Requirementを展開する回数である。

```python
while initial_requirements:
    if (
        not budget.can_continue()
        or article_budget.exhausted()
        or requirement_budget.exhausted()
    ):
        mark_remaining_initial_as_unresolved("initial_round_budget_exhausted")
        break

    initial_batch = initial_requirements.pop_priority_batch(
        max_active_issues=ACTIVE_ISSUE_BATCH_SIZE,
    )
    process_requirement_batch(initial_batch, round_index=0)

expansion_stopped = False
for expansion_round in range(1, MAX_EXPANSION_ROUNDS + 1):
    if expansion_stopped:
        break
    round_frontier = frontier.freeze_current_round()
    while round_frontier:
        if (
            not budget.can_continue()
            or article_budget.exhausted()
            or requirement_budget.exhausted()
        ):
            mark_remaining_as_unresolved("expansion_budget_exhausted")
            expansion_stopped = True
            break

        batch = round_frontier.pop_priority_batch(
            max_active_issues=ACTIVE_ISSUE_BATCH_SIZE,
            remaining_budget=budget,
        )
        process_requirement_batch(batch, round_index=expansion_round)
```

batch内の処理順と結果のmerge順は決定的にする。あるRequirementの結果から生成された
子Requirementは、同じラウンドの`round_frontier`へ割り込ませず次ラウンドで処理する。
これにより5〜8番目の主論点が子Requirementと競合することなく、全初期論点の起点取得後に
法律→政令→府令の展開を開始できる。

### 8.3 優先順位

1. 利用者が明示した条・項・号
2. 質問に明示された主要論点の直接根拠
3. 直接根拠から高信頼Graphで要求された下位法令
4. 定義、例外、準用など、結論を変え得る未解決Requirement
5. ガイドが示唆し、法令本文で未確認の候補関係
6. 補足的な実務説明

### 8.4 子Requirementの生成

Article本文またはGraphから次を検出した場合に追加する。

| 検出 | 追加Requirement |
|---|---|
| 「政令で定める」 | `cabinet_order`の具体化 |
| 「内閣府令・省令で定める」 | 対応する府省令の具体化 |
| 「第X条を準用」 | 準用先Article |
| 「ただし」「除く」 | 例外・除外 |
| 定義語の利用 | 定義Article。`DEFINES`/`USES_TERM`実装前は本文ルールと直接検索を使う |
| 「別表・様式」 | 手続・様式 |
| 高信頼`IMPLEMENTS` | 接続先Article本文 |
| 高信頼`EXCEPTION_TO` | 例外Article本文。Phase 2の新規実装後だけ有効 |

### 8.5 Neo4jのbatch展開

現行`paths_from_many()`は複数start IDを受け取れるが、`edge_type`は1種だけである。
vNextではGraphClientへ、registryで許可された複数edge typeを1回で問い合わせるAPIを追加する。

```python
paths_from_many(
    from_graph_node_ids=[...],
    edge_types=["IMPLEMENTS", "APPLIED_BY", "EXCEPTION_TO"],
    max_depth=1,
    timeout_sec=remaining_graph_budget_sec,
    ...
)
```

Cypherのrelationship type unionを使用し、返却結果にはedge typeを保持する。文字列を直接
Cypherへ埋め込まず、edge registryのallowlistを通す。`timeout_sec`はNeo4j driverが提供する
queryまたはtransaction timeoutへ変換し、単なるCypher parameterとして渡さない。

`MAX_LEGAL_HOPS=3`はrequest全体で追跡する論理ホップ上限であり、1回のNeo4j queryを
3ホップにする設定ではない。既定は1 query=1 hopとし、

```text
round 0: 起点となる法律Articleを取得
round 1: 法律 → 政令
round 2: 政令 → 府令
round 3: 府令 → 参照条文
```

と進める。これにより、現行`AGENT_MAX_GRAPH_HOP=1`を無条件に3へ上げてノイズを増やすことを
避ける。Phase 0では1ホップ反復と複数ホップ一括の到達率・経路数・時間を比較する。

### 8.6 重複排除と循環防止

Requirementの一意キー:

```text
(issueId, roleFamily, roleSubtypes, authorityType, parentArticleId)
```

Article探索の一意キー:

```text
(requirementId, articleContentUnitId, relationPath)
```

同じArticle・同じ関係方向を再探索しない。準用や相互参照の循環を検出し、既取得Articleを
再利用する。

### 8.7 再計画

LLMによる論点再計画は次の場合だけ最大1回実施する。

- 初期Requirementの候補がすべて0件
- 取得Articleが想定roleと一致しない
- 時間予算内で同じRequirementを2回検索しても進展しない
- 取得Articleから質問上の新しい主要論点が明確に現れた
- 複数の法令系統が競合し、ルールで解決できない

2回目を実行する時間がない場合は再計画を発動せず、`exhausted`または`unresolved`として
記録する。通常の委任・準用展開はルールとGraphで行い、毎ラウンドLLMに全計画を作り直させない。

### 8.8 停止条件

```text
全mandatory Requirementがresolved
または
最大法令ホップ到達
または
最大探索ラウンド到達
または
Requirement総数上限到達
または
Article候補総数到達
または
回答生成予約時間を除いた時間予算終了
```

候補件数が多いことだけを `resolved` の条件にしない。

`MAX_REQUIREMENTS_TOTAL`へ達した場合は、既存Requirementを削除しない。優先度順に処理を続け、
上限後に新しく発見したRequirementは `unresolved`、理由
`requirement_limit_exhausted`として記録する。利用者明示Requirementとmandatory Requirementを
補助的・解釈的Requirementより優先する。

`MAX_ARTICLE_CANDIDATES_TOTAL`も先着順で消費させない。

1. 各mandatory Requirementへ最低1 Article候補の機会をround-robinで与える。
2. 可能なら各mandatory Requirementへ2件目までの候補枠を与える。
3. 残余枠をRequirement優先度とRequirement内順位で配分し、1 Requirementあたり最大8件とする。
4. 後から高優先度mandatory Requirementが生じ、全体64件が埋まっている場合は、
   未採用のoptional候補または同一Requirementの低順位余剰候補を退避して枠を作る。
5. mandatory Requirement数自体が全体候補枠を超える場合は、利用者明示、結論を変え得る
   定義・例外、直接根拠、具体化根拠の順で処理し、残りを
   `article_candidate_budget_exhausted`として記録する。

候補の退避はtraceへ記録し、すでに回答根拠として採用したArticleは自動退避しない。

予算切れ時は、未解決Requirementを回答生成へ渡し、「下位法令の具体化規定を確認できていない」
などの限定を表示できるようにする。

## 9. レイヤー・役割別検索

### 9.1 検索範囲

Requirementごとに検索範囲を決める。

```text
authorityTypeが未確定
  → 全法令から起点Articleを検索

authorityTypeとdocumentIdが確定
  → その法令内だけを検索

parentArticleIdとGraph接続先が確定
  → 接続先Articleを直接取得し、同じ法令内の補助検索を追加
```

`authorityType`だけが省令または内閣府令として指定され、`documentId`が未確定の場合は、
§5.2の包含規則に従って`ordinance_unspecified`と`unknown`も検索対象へ含める。

### 9.2 クエリ

元の質問全文ではなく、Requirement専用クエリを作る。

```text
論点: 公開買付開始公告
法的役割: 公告事項・公告方法の具体化
親条文: 金商法27条の3
探索レイヤー: 内閣府令
検索語: 公開買付開始公告、公告事項、公告方法
```

### 9.3 候補生成

候補生成は次を併用する。

1. 条・項・号の直接取得
2. 対象法令内BM25
3. 対象法令内ベクトル検索
4. 高信頼Graph接続先の直接取得
5. 同一Articleの定義・例外・準用関係
6. ガイドから得た法令候補

Article単位で重複排除し、Paragraph・ItemはArticle確定後に選ぶ。

### 9.4 Cross-Encoder

Cross-Encoderは必須条件ではなく、同じRequirement内のArticle候補を並べ替えるソフトスコアとする。

```text
ハード条件:
  明示参照
  高信頼の委任・準用先
  mandatory Requirementを満たすArticle

ソフトスコア:
  Requirement専用query × Article本文
```

Cross-Encoderが低スコアでも、構造上mandatoryなArticleを候補から削除しない。
質問全文による全法令一括リランクは、補助的なglobal scoreとしてのみ残すか、ablationで不要性を確認する。

### 9.5 Requirementの充足

例:

```text
definition:
  定義本文を含むArticleを取得済み

exception:
  原則Articleと例外Articleを取得済み
  mandatoryな再委任が未解決でない

procedure:
  手続の根拠Articleを取得済み
  質問が具体的方法を求める場合、委任された公告事項・様式等も取得済み

Graphで具体化先へ入ったRequirement:
  親Articleとの信頼可能な関係がある
  接続先Article本文を取得済み
```

roleごとの充足規則はレジストリ化し、コード中の分散したif文にしない。

## 10. ガイドの扱い

ガイドは法令と同じ根拠枠で競争させない。

```text
法令レーン
  法律・政令・府令
  → 法的結論、義務、禁止、要件、例外、手続の直接根拠

ガイドレーン
  ガイド、監督指針、Q&A
  → 自然言語と法令用語の橋渡し
  → 関連Article・法令間関係の候補発見
  → 行政解釈・実務運用の補足
```

処理:

1. 自然言語質問でガイドを検索する。
2. `EXPLAINS`、明示条文注記、対応表からArticle候補を得る。
3. `RelationAssertion`から法令間関係候補を得る。
4. 法令本文をOpenSearchから取得する。
5. 法令本文と高信頼Graphで関係を確認する。
6. 法的結論は法令本文で支える。
7. ガイドを使用する場合は「行政解釈」「実務上の取扱い」として明示する。

法令とガイドが矛盾する場合は、法令本文を優先し、ガイドの法令時点・発行主体・位置づけを表示する。

## 11. 上限と予算

現行の固定 `maxQueries=5`、`maxGraphPaths=10`、`rerankCandidates=30chunks` をそのまま
各レイヤーへ掛け合わせない。

### 11.1 初期値案

| 設定 | 初期値案 | 単位 |
|---|---:|---|
| `MAX_PRIMARY_ISSUES` | 8 | 主論点 |
| `ACTIVE_ISSUE_BATCH_SIZE` | 4 | 同時処理する主論点 |
| `MAX_REQUIREMENTS_TOTAL` | 24 | 必要根拠スロット |
| `MAX_LEGAL_HOPS` | 3 | 法令関係ホップ |
| `MAX_EXPANSION_ROUNDS` | 3 | round 0後の子Requirement展開 |
| `MAX_ARTICLES_PER_REQUIREMENT` | 8 | Article候補 |
| `MAX_ACCEPTED_ARTICLES_PER_REQUIREMENT` | 2 | 採用Article |
| `MAX_CHILD_RELATIONS_PER_ARTICLE` | 6 | 子関係 |
| `MAX_ARTICLE_CANDIDATES_TOTAL` | 64 | request全体 |
| `MAX_RERANK_PAIRS_TOTAL` | 未確定。Phase 0で計測 | query-Articleペア |
| `MAX_RERANK_PAIRS_PER_CALL` | 未確定。Phase 0で計測 | 1回のCross-Encoder呼び出し |
| `MAX_RERANK_CALLS_PER_ROUND` | 2 | round 0を含む1ラウンド |
| `MAX_RERANK_CALLS_TOTAL` | 8 | request全体 |
| `MAX_EMBEDDING_BATCH_CALLS_PER_ROUND` | 1 | query embedding batch |
| `MAX_EMBEDDING_BATCH_CALLS_TOTAL` | 4 | request全体 |
| `MAX_SEARCH_BATCH_CALLS_PER_ROUND` | 2 | OpenSearch multi-search |
| `MAX_SEARCH_BATCH_CALLS_TOTAL` | 8 | request全体 |
| `MAX_GRAPH_BATCH_CALLS_PER_ROUND` | 2 | Neo4j batch traversal |
| `MAX_GRAPH_BATCH_CALLS_TOTAL` | 8 | request全体 |
| `MAX_CHUNKS_PER_ARTICLE` | 3 | 最終chunk |
| `FINAL_CONTEXT_CHUNKS` | 16 | 既定の回答コンテキスト |
| `FINAL_CONTEXT_CHUNKS_MAX` | 初期は16。文字予算拡張後だけ24を検討 | 入力予算内の上限 |
| `MAX_AUXILIARY_CONTEXT_CHUNKS` | 2 | optional・ガイドの合計 |
| `MAX_GUIDANCE_PER_ISSUE` | 5 | ガイド候補 |
| `MAX_GUIDE_DERIVED_ARTICLES` | 6 | ガイド由来Article |
| `MAX_REPLAN_CALLS` | 1 | LLM再計画 |

これらはレビュー・評価前の暫定値である。

### 11.2 グローバル安全予算

個別上限に加えて次を維持する。

- wall time
- 回答生成予約時間
- OpenSearch/Neo4jの外部呼び出し時間
- Cross-Encoderフェーズ全体時間
- LLM論理呼び出し回数
- Article候補総数
- 最終コンテキスト文字数・token数

回答予約には2つの値を区別する。

```text
minimumAnswerReserve
  = AGENT_ANSWER_RESERVE_SEC
  探索スケジューラが最低限残す時間

fullAnswerSafeReserve
  = max(AGENT_ANSWER_RESERVE_SEC, LLM_TIMEOUT_SEC)
  回答LLMがtimeout上限まで使ってもwall time内に収めるための時間

fullAnswerSafeExplorationBudget
  = wall time - fullAnswerSafeReserve
```

`LLM_TIMEOUT_SEC`は期待所要時間ではないため、個々の検索フェーズのreserve値として流用しない。
一方、実装着手の性能判定では、回答がtimeout上限を使う場合も壊れない
`fullAnswerSafeExplorationBudget`を使う。

現在の時間設定は環境によって異なる。

| profile | wall | minimum reserve | LLM timeout | 最低予約だけの余り | full-answer-safe探索予算 |
|---|---:|---:|---:|---:|---:|
| コード・Compose既定 | 110秒 | 60秒 | 90秒 | 50秒 | 20秒 |
| 現在の評価環境 | 280秒 | 60秒 | 180秒 | 220秒 | 100秒 |

110秒profileはコード上の互換profile、280秒profileはRUNBOOKでAnthropic評価に使用している
運用評価profileと呼ぶ。どちらをvNextの対応profile・性能ゲートにするかは、Phase 0で旧方式を
同一条件で測ってから確定する。旧方式が110秒profileで安全に完了しない場合、20秒をvNextの
合格ゲートにせず、280秒profileを当面の運用profileとして明示する。単にvNextだけのtimeoutを
短くして見かけ上20秒へ合わせない。

`AGENT_ANSWER_RESERVE_SEC < LLM_TIMEOUT_SEC`の場合は起動時とhealth/traceに警告を出す。
両値を自動的に同一値へ書き換えず、性能測定後に明示設定する。Agent wall timeはクライアント側の
request timeoutより安全マージンを持って短くする。

`REQUEST_TIMEOUT_SEC`はeval-runner側の設定であり、agent-api単独では比較できない。
agent-apiの`/health`に`AGENT_MAX_WALL_TIME_SEC`と採用profile名を公開し、eval-runnerが実行開始時に
自身の`REQUEST_TIMEOUT_SEC`と比較する。`REQUEST_TIMEOUT_SEC <= agent wall time + safety margin`
の場合は評価を開始せず設定エラーとする。通常のAPIクライアントにも同じ値を公開するが、
クライアント固有timeoutの検証責任は各クライアント側に置く。

探索phaseにはwall timeとは別の共有deadlineを設け、planner、embedding、OpenSearch、Neo4j、
Cross-Encoder、Evaluatorまたはreplanの各呼び出しへ次の実効timeoutを渡す。

```text
componentEffectiveTimeout
  = min(componentConfiguredTimeout, explorationDeadline - now)
```

採用profileでは、同一requestで順番に実行され得るcomponentの**呼び出し単位の割当時間合計**を
`fullAnswerSafeExplorationBudget`以下にする。`PLANNER_TIMEOUT_SEC`、
`RERANK_TIMEOUT_SEC`、`EVALUATOR_TIMEOUT_SEC`等の設定上限の単純合計が探索予算を超える場合は、
起動時とhealth/traceへ警告する。共有deadlineで物理的な超過を防ぐだけでなく、Phase 0の実測後に
各componentの割当値または採用profileを再設定する。

現行の`EMBEDDING_TIMEOUT_SEC=120`、OpenSearch client内の固定10〜20秒timeoutに加え、
Neo4j `GraphClient.paths_from_many()`の`session.run(...)`にはtimeout自体がない。
request単位の残時間を渡せない場合、共有deadlineだけを追加しても外部呼び出し中に予算を超える。
GraphClientのtimeout overrideは複数edge type batch APIと同時にPhase 2で実装し、
OpenSearch・embeddingはPhase 3までにtimeout overrideを受け取れるAPIへ変更する。

```text
各有効な逐次実行経路について:
  Σ (componentPerInvocationBudget × maxInvocationsOnPath)
    <= fullAnswerSafeExplorationBudget

各呼び出しについて:
  componentPerInvocationBudget <= componentConfiguredTimeout
  actualInvocations <= componentMaxInvocations
```

shadow期間は旧Evaluatorと新replanの両方が走り得るため、別々の予算として合計する。
active移行後に両者を統合し、同時に実行しないことを状態遷移とテストで保証できた場合だけ、
同じ予算枠を共有できる。

Phase 0で `RERANK_MAX_CHARS=3000`、現在のローカル
`hotchpotch/japanese-reranker-base-v2`について次を計測する。

- 8、16、32、64、96 query-Articleペアのp50/p90/p99
- Article文字数別のスループット
- batch size別のスループット
- component別の1 requestあたり呼び出し回数、1回あたり件数、時間
- round 0と各展開ラウンドのOpenSearch・Neo4j・Cross-Encoder呼び出し回数
- 同時にOpenSearch・Neo4jを使用した場合の実効時間
- Cross-Encoder timeout・fallback率

`MAX_RERANK_PAIRS_TOTAL`と`MAX_RERANK_PAIRS_PER_CALL`はこの計測後に確定する。
per-call上限とrequest全体上限の両方を満たさなければ呼び出さず、mandatory優先でペアを削減する。
110秒互換profileでは96ペアを初期案にせず、
mandatory Requirement優先、候補数削減、BM25/RRF fallbackを主案として測定する。
280秒運用評価profileでは96ペアも比較対象にする。110秒profileが旧方式を含めて成立しない場合は、
280秒profileで成立する設定を当面の運用値として明示し、110秒対応を別の最適化目標として残す。

### 11.3 shadow専用予算

shadowは現行検索・回答を守るため、独立したphase budgetを持つ。

```text
LAYERED_SHADOW_PHASE_BUDGET_SEC
  = min(configured shadow budget,
        max(0,
            deadline - now - fullAnswerSafeReserve)
          * LAYERED_SHADOW_REMAINING_FRACTION)
```

`LAYERED_SHADOW_REMAINING_FRACTION`の初期値は0.5とし、shadowが回答前の安全余白を
使い切らないようにする。

初期値はPhase 0の計測後に確定する。shadow budgetを超えた場合は新方式だけを打ち切り、
`shadowIncomplete=true`と未処理Requirement数を記録して現行回答を継続する。
shadowのために現行全文再ランクまたは回答LLMがfallbackしてはならない。

### 11.4 LLM呼び出し予算

現行`AGENT_MAX_LLM_CALLS`はコード上3へハードキャップされ、planner、Evaluator、answerで
使い切る。shadow中に再計画を1回追加するには最大4 logical callsが必要である。

初期移行ではshadow期間だけ上限を4へ引き上げ、
planner + 旧Evaluator + 新replan + answerを許可する。configのhard capを4へ変更する。
ただしreplanは条件成立時だけ
実行し、traceと評価結果に論理・実API呼び出し数を保存する。新方式の本稼働前に、旧Evaluatorを
廃止または再計画と統合し、planner + replan/Evaluator + answerの3回へ戻せるかをPhase 0から
Phase 4にかけて評価する。

### 11.5 最終コンテキスト文字予算

現行`LLM_MAX_CONTEXT_CHARS=12000`では、24chunksへ増やすと1件あたりの平均文字予算が減り、
条文本文の途中切り詰めが増える。初期実装は16chunksを維持する。

24chunksは次を同時に満たす場合だけ有効化する。

- `LLM_MAX_CONTEXT_CHARS`をモデル入力上限と回答token予算に合わせて再設定する。
- Article/Paragraph/Item単位で意味を壊さず切り詰める。
- 全文が入らないchunkを検知する。
- 旧・新コンテキスト別の`contextTruncation`をtraceへ保存する。
- 20問・140問で根拠Article再現率と回答品質が改善する。

chunk数より文字・token予算を優先し、法令タイトルと見出しだけ残って本文が欠ける候補を
根拠充足済みとして扱わない。

### 11.6 最終16chunksの配分

`resolved`は検索上必要なArticle本文を取得できた状態であり、そのArticleが回答コンテキストへ
入ったことを意味しない。Requirementに次の2状態を分けて保存する。

```text
retrievalStatus:
  §7.1で定義した検索状態

contextStatus:
  pending | included | shared_coverage | omitted_context_budget
```

最終選抜はArticle候補数やRequirementの処理順ではなく、mandatoryな
`conclusionGroup`の完全被覆を優先する。原則・例外など一体でなければ結論を支えられない
Requirementを同じgroupにし、group指定のないmandatory Requirementは1件だけのgroupとして扱う。
各mandatory Requirementの最低配分目標は、そのgroupを完成できる根拠chunk集合への包含である。

1. Article重複を除き、各chunkが被覆できるRequirement IDを計算する。
2. 利用者が明示した条・項・号を、所属groupの代表chunk候補として優先する。
3. groupごとに、すべてのmandatory memberを被覆する最小chunk bundleをset coverで求める。
4. すでに選択済みのshared chunkを差し引いた増分bundleが残枠へ全て収まるgroupだけを
   `eligible`とする。
5. group優先度、被覆するmandatory数、増分chunk数の少なさ、Requirement内Article順位、
   項・号完全一致、文字切り詰めの少なさの順でgroupを決定的に選ぶ。
6. 選択したgroupの増分bundleを原子的に追加する。groupの一部だけを先に追加しない。
7. 1chunkが複数groupまたはRequirementを支える場合は`shared_coverage`とし、
   1枠で複数を被覆する。
8. 残枠へgroup全体が収まらない場合、そのgroupを丸ごと
   `omitted_context_budget` / `unresolvedForAnswer`へ送り、部分bundleに枠を使わない。
9. mandatory group被覆後に空きがあり、少なくとも1つのprimary groupを完全被覆できた場合だけ、
   同一Requirementの補完項号、optional Requirement、ガイド補足の順で追加する。
10. optional Requirementとガイド補足の合計は`MAX_AUXILIARY_CONTEXT_CHUNKS=2`を超えない。
    枠を16件まで埋めること自体を目標にしない。
11. primary groupを1つも完全被覆できなかった場合はoptional・ガイドを0件とし、
    通常の法的回答を生成しない。
12. ガイドは法令mandatory枠を満たさず、法令根拠で16枠が埋まる場合は0件でもよい。

Requirement優先度:

```text
P0  利用者が明示した条・項・号
P1  質問が直接求めた結論の原則・禁止・義務・権利
P2  結論を変え得る定義・適用範囲・例外・除外・条件
P3  高信頼Graphで要求された具体化・準用先
P4  質問が直接求めた手続・期限・様式
P5  補助的解釈・実務説明・ガイド
```

group優先度は、group内mandatory memberの最上位優先度とする。同順位では、質問が直接求めた
結論を支えるgroup、結論を変え得る例外・定義を含むgroupの順を維持し、最後は安定した
`groupId`で決定する。

同じ主論点内では、原則だけを残して例外を落とすなど結論を歪める選抜を避けるため、
P1とそれに対応するP2を一つの`conclusionGroup`として扱う。配分段階でgroupを原子的に扱い、
一部だけが最終コンテキストへ入る状態を作らない。

16chunksで全mandatory Requirementを被覆できない場合:

1. 上記優先度とgroup単位のset coverageで16件まで選ぶ。
2. 入らなかったgroup内のRequirementは検索上`resolved`でも、
   `contextStatus=omitted_context_budget`とする。
3. 回答生成には`unresolvedForAnswer`として渡し、そのRequirementに関する断定を避ける。
4. traceへ`context_budget_exhausted`、除外Requirement ID、必要追加枠数を記録する。
5. UIでは「検索では確認したが、回答コンテキスト上限により回答根拠へ含められなかった」
   ことを開発者向けtraceで区別する。

primary groupの被覆状態は利用者向け回答制御にも使う。

```text
全primary groupを完全被覆:
  answerStatus = complete

一部のprimary groupだけ完全被覆:
  answerStatus = partial_primary_evidence
  回答冒頭に、回答できない主論点と理由を表示する

primary groupを1つも完全被覆できない:
  answerStatus = insufficient_primary_evidence
  通常の法的結論を生成しない
  「主たる論点の根拠を回答コンテキストへ収められなかったため、
   この質問には根拠付きで回答できません」と利用者へ表示する
```

`partial_primary_evidence`では完全被覆できたprimary groupだけを回答し、除外された主論点を
周辺論点から推測して補わない。`insufficient_primary_evidence`で周辺法令を表示する場合は
「参考として確認できた資料」と明記し、質問への法的結論として提示しない。

`MAX_CHUNKS_PER_ARTICLE=3`は保証数ではなく上限である。採用するmandatory groupの最小bundleを
全て配分した後に限り、同一Articleの追加Paragraph・Itemを最大3chunksまで選べる。

### 11.7 バッチ化

論理クエリ数と外部API呼び出し数を分離する。

```text
4つの法律レイヤーRequirement
  → OpenSearchのmulti-search 1回

3つの政令Requirement
  → documentId別にまとめたmulti-search
```

`AGENT_MAX_TOTAL_TOOL_CALLS`を単に増やすのではなく、探索キューをbatch処理して呼び出し数を抑える。
同一ラウンドの論理Requirement batchを可能な限り1つの外部batchへ集約し、payload・ペア数上限で
分割しても各component最大2回とする。round 0と最大3展開ラウンドを合わせ、OpenSearch、
Neo4j、Cross-Encoderはそれぞれrequest全体最大8回を初期hard limitとする。
query embeddingはラウンド単位で1回にまとめ、request全体最大4回とする。
Cross-Encoder APIが複数query-Articleペアを1回で処理できない場合はPhase 3でbatch endpointを
追加し、Requirementごとの逐次HTTP呼び出しへ戻さない。

### 11.8 chunk化のタイミング

```text
検索・Graph・役割判定・リランク:
  Article単位

回答コンテキスト選択:
  Paragraph・Item chunk単位
```

同一Articleの多数の項・号が候補探索予算を消費しないようにする。

## 12. フォールバック

| 障害 | フォールバック |
|---|---|
| planner失敗 | 質問全文を1主論点とし、ルールでrole仮説を生成 |
| Graph unavailable | 明示参照・法令内BM25/Vector検索を継続し、関係はunresolved |
| Cross-Encoder unavailable | Requirement内のBM25/Vector/RRF順を使用。構造的保護は維持 |
| ガイド関係未確認 | 候補拡張だけに使い、法的根拠にしない |
| 時間不足 | 未解決Requirementを記録して回答生成へ進む |
| 新方式内部エラー | feature flagで現行経路へ戻す |

フォールバック時に、低信頼候補を「解決済み」と扱わない。

## 13. トレース

新しいtraceに次を保存する。

```json
{
  "legalIssuePlan": [],
  "evidenceRequirements": [],
  "conclusionGroups": [],
  "requirementTransitions": [],
  "searchesByRequirement": {},
  "rerankByRequirement": {},
  "graphEdgesConsidered": [],
  "graphEdgesAccepted": [],
  "graphEdgesRejected": [],
  "guidanceRelationAssertions": [],
  "authorityTypeResolutions": [],
  "unresolvedRequirements": [],
  "unresolvedForAnswerRequirements": [],
  "articleCandidateCount": 0,
  "articleCandidateBudget": {
    "limitReached": false,
    "evictedCandidateIds": [],
    "exhaustedRequirementIds": []
  },
  "rerankPairCount": 0,
  "expansionRounds": 0,
  "requirementLimitReached": false,
  "timeBudget": {
    "profileName": "",
    "wallTimeMs": 0,
    "minimumAnswerReserveMs": 0,
    "llmTimeoutMs": 0,
    "fullAnswerSafeReserveMs": 0,
    "fullAnswerSafeExplorationBudgetMs": 0,
    "componentConfiguredTimeoutMs": {},
    "componentAllocatedBudgetMs": {},
    "componentEffectiveTimeoutMs": {},
    "componentMaxInvocations": {},
    "componentActualInvocations": {},
    "componentItemsPerInvocation": {}
  },
  "shadowPhaseBudgetMs": 0,
  "shadowPhaseElapsedMs": 0,
  "shadowIncomplete": false,
  "contextTruncation": {
    "oldContext": {
      "occurred": false,
      "truncatedChunkCount": 0,
      "droppedChunkCount": 0,
      "originalChars": 0,
      "includedChars": 0
    },
    "newContext": {
      "computedInShadow": true,
      "occurred": false,
      "truncatedChunkCount": 0,
      "droppedChunkCount": 0,
      "originalChars": 0,
      "includedChars": 0
    }
  },
  "contextCoverage": {
    "answerStatus": "complete",
    "primaryConclusionGroupIds": [],
    "includedPrimaryConclusionGroupIds": [],
    "omittedPrimaryConclusionGroupIds": [],
    "includedConclusionGroupIds": [],
    "omittedConclusionGroupIds": [],
    "includedRequirementIds": [],
    "sharedCoverage": {},
    "omittedRequirementIds": [],
    "additionalChunksNeeded": 0,
    "additionalChunksNeededByGroup": {}
  },
  "budgetUsage": {},
  "fallbacks": []
}
```

各Requirementについて次を追跡できなければならない。

- どの質問断片から生成されたか
- planner、ルール、Article本文、Graphのどれが生成したか
- どの法令レイヤーを検索したか
- どのArticleが候補になり、なぜ採用・却下されたか
- どの関係をたどったか
- resolvedまたはunresolvedとした理由

各`conclusionGroup`について、`isPrimary`、mandatory member、最小bundle、必要chunk数、
採用・除外理由、`answerStatus`への影響を追跡できなければならない。

## 14. 実装構成

`agent.py`へ処理を追加し続けず、責務を分離する。

```text
agent-api/app/
  legal_ontology.py
    node type、edge registry、role registry

  legal_issue_planner.py
    structured issue plan、ルール補正、再計画

  evidence_requirements.py
    Requirement状態、重複排除、充足判定

  legal_relation_resolver.py
    Graph関係の検証、子Requirement生成

  layered_retriever.py
    Requirement別の検索、batch化、探索キュー

  requirement_reranker.py
    Requirement内Cross-Encoder、fallback

  layered_context_assembler.py
    Articleからchunkへの変換、最終コンテキスト選択

  agent.py
    全体オーケストレーションと旧方式fallback
```

既存クラス・関数との互換アダプターを設け、一度に全面置換しない。
既存の `evidence_selector.py` は旧論点被覆方式が利用中のため、Phase 5までは変更・同居させず、
新方式の選抜は `layered_context_assembler.py` に分離する。

## 15. 実装フェーズ

### Phase 0: 旧方式baseline・時間予算・オントロジー棚卸し

1. コード既定の110/60/90秒profileで旧方式を実行し、探索phase・全体のp50/p90/p99、
   component別時間、timeout率、request失敗率を測る。同じ対象をRUNBOOKの
   280/60/180秒profileでも測る。
2. 旧方式が110秒profileのfull-answer-safe探索予算に収まらない場合は、このprofileを
   vNextの合格ゲートから外し、採用する運用profileと各componentの時間割当を確定する。
3. planner、embedding、OpenSearch、Neo4j、Cross-Encoder、Evaluator/replanの設定timeout、
   1回あたり割当、最大・実呼び出し回数、1回あたり件数、実効timeout、実測時間を一覧化し、
   呼び出し回数込みの割当時間合計を採用profileの探索予算内にする。
4. 現在seedされるnode/edge種別を件数付きで出力する。
5. 現行エッジが5種だけであり、`DEFINES`、`USES_TERM`、`EXCEPTION_TO`、
   `MENTIONS`が未実装であることをコード・Neo4jの両方で確認する。
6. seedされるが検索されないエッジを特定する。
7. 重複、dangling、不正方向、法令系統誤接続を検査する。
8. 現行Graph起点ID、候補経路、除外理由をtraceへ追加する。
9. e-Gov `LawType`ではM系を分離できないことを確認し、`LawNum`、タイトル、所管情報から
   監査候補を作る補助処理の可否だけを調査する。
10. `M`を省令と内閣府令へ分けるために、人手確認してregistryへ明示すべき法令を一覧化する。
11. Cross-Encoderの1呼び出し8〜96ペアに対するp50/p90/p99・文字数別スループットと、
    同一requestで最大回数を逐次実行した累積時間を計測する。
12. 採用profileと比較用profileのfull-answer-safe探索予算を分けて計測する。
13. 代表問題について、round 0の初期取得batch数と各子Requirement展開が消費したラウンド数を記録する。
14. 16chunks/12000文字で発生している切り詰め件数と、24chunks時の見積りを取得する。
15. Graph schema versionを導入する。

完了条件:

- エッジインベントリがコード・ドキュメント・Neo4jで一致する。
- 公開買付府令10条の脱落地点をGraph起点または経路選抜まで自動判別できる。
- `authorityType`をregistryへ人手で明示する対象と、法律・政令だけ自動判定できる範囲が確定している。
- 旧方式の実測に基づいてvNextの対応profileが決まり、そのprofileの探索予算と
  planner・検索・Graph・reranker・Evaluator/replanの呼び出し回数込み割当時間が矛盾していない。
- 採用profileのfull-answer-safe探索予算内で使えるCross-Encoderのper-callペア数、
  最大呼び出し回数、request全体ペア数が確定している。
- round 0を除く3展開ラウンドで到達できる法令連鎖と、未到達になる経路が確認できている。
- shadow phase budgetと最終コンテキスト文字予算の初期値を決定できる。

### Phase 1: 論点・役割・Requirementのshadow生成

1. structured planner schemaを追加する。
2. 既存文字列クエリも同時生成して現行検索を維持する。
3. roleルール補正を追加する。
4. Requirement状態モデルを追加する。
5. Requirement依存関係から`conclusionGroup`を生成する。
6. 現行回答には使わずtraceへ保存する。

完了条件:

- 既存質問例で明示された要求が主論点から欠落しない。
- 4件を超える質問でも論点を黙って切り捨てない。
- planner失敗時のfallbackがある。

### Phase 2: Graphオントロジー整備

1. edge registryを実装する。
2. `law_registry.json`へ`authorityType`と`authoritySource`を追加する。
3. OpenSearch mapping、seed document、Neo4j Law/Article propertyへ`authorityType`を追加する。
4. `REFERENCES.referenceKind`と`derivedFromEdgeId`を導入する。
5. `IMPLEMENTS`へ委任文言に基づく段階的confidenceを導入する。
6. `DEFINES`、`USES_TERM`、`EXCEPTION_TO`を新規実装するか、初期vNextの対象外にするかを
   edge typeごとに決定する。実装対象はseed・検索・監査を同時に追加する。
7. `MENTIONS`と`RelationAssertion`を新規実装する。
8. GraphClientへallowlist付き複数edge typeのbatch traversalと、Neo4j driverの
   query/transaction timeoutへ接続するrequest単位の`timeout_sec` overrideを同時に追加する。
9. seedとGraph監査テストを更新する。
10. すべてのschema・edge変更をまとめて必要な環境で一度再シードする。

完了条件:

- すべての関係に方向、信頼度、由来、検索利用可否がある。
- 未確認ガイド関係が確定法令関係として扱われない。
- registry、OpenSearch、Neo4jの`authorityType`が一致する。
- `M`の省令・内閣府令を法令内検索で区別できる。
- 単純な親条文参照だけの候補が高信頼`IMPLEMENTS`として通らない。
- 複数edge typeをRequirement batchごとに1回のGraph APIで取得できる。
- Graph queryが共有探索deadlineの残時間を超えて実行されない。

### Phase 3: Requirement別検索

1. Phase 2で投入した`authorityType`を使って法令レイヤー別documentId解決を実装する。
2. Article単位のBM25/Vector候補を生成する。
3. 明示Articleと高信頼Graph接続先を直接取得する。
4. Requirement別Cross-Encoderを実装する。
5. 検索・リランクをbatch化する。
6. 現行候補プールと新方式候補をshadow比較する。

完了条件:

- 必要Articleがchunk重複によって候補上限から落ちない。
- Cross-Encoderを無効化しても構造的必須Articleは保持される。
- Cross-Encoder総ペア数と時間がPhase 0で確定した上限内に収まる。
- Cross-Encoderの1回あたりペア数と呼び出し回数がPhase 0で確定した上限内に収まる。

### Phase 4: 反復探索

1. unresolved Requirementをラウンド単位で取り出すpriority batchを実装する。
2. Article本文・Graphから子Requirementを生成する。
3. 重複排除と循環検出を実装する。
4. 1Graph query=1 hopを基本とし、request全体の最大3論理ホップ・最大3展開ラウンドを実装する。
5. 条件付きLLM再計画を最大1回で実装する。
6. `MAX_REQUIREMENTS_TOTAL`到達時の優先処理と未解決記録を実装する。
7. 未解決理由をtraceへ保存する。

完了条件:

- 法律→政令→府令の連鎖を、質問別の固定条番号なしで到達できる。
- 候補件数だけを理由に未解決Requirementをresolvedにしない。

### Phase 5: 最終コンテキスト

1. Articleとchunkが被覆するRequirement IDを計算する。
2. mandatory Requirementを`conclusionGroup`へまとめ、各groupを完全被覆する最小chunk bundleを求める。
3. 残枠へ全bundleが収まるgroupだけを原子的に16chunksへ配分する。
4. Article内から質問に必要なParagraph・Itemを最大1〜3chunks選ぶ。
5. 16件へgroup全体が入らないresolved Requirementを`omitted_context_budget`として記録する。
6. primary group被覆から`answerStatus`を決定し、利用者向け未回答表示を生成する。
7. primary groupが0件の場合はoptional・ガイドを追加せず、通常の法的回答を抑止する。
8. ガイド枠と法令根拠枠を分離し、補助枠を最大2chunksに制限する。
9. 未解決および`unresolvedForAnswer` Requirementを回答生成へ渡す。
10. 法令関係図に確定関係と補助関係を区別して表示する。
11. 初期は現行16chunksと新方式16chunksをshadow比較する。
12. shadowでも旧・新コンテキスト別の`contextTruncation`を計測し、
    文字予算拡張後だけ24chunksを別実験する。

完了条件:

- 回答で断定する各法的結論に法令本文の根拠chunkがある。
- 同じ`conclusionGroup`の一部だけをコンテキストへ入れない。
- 16chunksに入らないRequirementを回答可能と誤判定しない。
- primary groupが入らない場合に、正常な法的回答として表示しない。
- mandatory法令根拠0件でoptional・ガイドだけのコンテキストを作らない。
- ガイドだけでmandatory Requirementを充足しない。
- 未確認RelationAssertionを確定線で表示しない。

### Phase 6: 段階的切替

1. 開発環境でfeature flagを有効化する。
2. 自然言語20問を再実行する。
3. lawqa_jp 140問をshadow modeで比較する。
4. 指標が合格後、新コンテキストを回答LLMへ渡す。
5. 引用、回答要点、正答率を再評価する。
6. 問題があれば旧方式へ即時fallbackできる状態を維持する。

## 16. テスト計画

### 16.1 オントロジー単体テスト

- nodeTypeごとの許可エッジ
- 正規方向
- `derivedFromEdgeId`
- 重複エッジ排除
- dangling edge検出
- `HAS_CONTENT_UNIT`循環検出
- 未確認RelationAssertionの使用禁止
- 法令系統をまたぐ誤った`IMPLEMENTS`の拒否
- 省令と内閣府令の`authorityType`判別
- registry、OpenSearch、Neo4jの`authorityType`一致
- `authorityType=ordinance_unspecified|unknown`を省令・内閣府令のどちらかとして確定しない
- 省令または内閣府令Requirementの検索から`ordinance_unspecified|unknown`を除外しない
- `documentId`確定時は`authorityType`未判別でも法令内を直接検索する
- `LawType=MinisterialOrdinance`だけで内閣府令・省令を確定しない
- 委任文言のない単純参照を高信頼`IMPLEMENTS`にしない
- 現行5種と新規エッジのschema version別許可

### 16.2 論点・役割テスト

- 1論点の簡単な質問
- 4論点を超える質問
- 定義、要件、例外、手続、効果を含む質問
- 1Articleが複数論点を支える質問
- 1論点が複数レイヤーを必要とする質問
- 同じ結論の原則・例外を同一`conclusionGroup`へまとめる
- 独立した結論を誤って同一`conclusionGroup`へまとめない
- 複数結論で共有する定義Requirementを複数`conclusionGroup`へ所属させる
- 質問が直接求める手続・期限groupも`isPrimary=true`にする
- planner形式エラー
- plannerが不必要な役割を提案する場合

### 16.3 探索ループテスト

- 法律だけで完結し、下位レイヤーを作らない
- 法律→政令
- 法律→府令
- 法律→政令→府令
- 準用の循環
- 同じ子Requirementの重複生成
- Graph unavailable
- Cross-Encoder unavailable
- 時間予算切れ
- 最大ホップ到達
- 5〜8主論点のround 0を複数batchで全件処理してから子展開を開始する
- round 0を除く3展開ラウンドで法律→政令→府令→参照条文へ到達する
- 複数RequirementのOpenSearch batch
- 複数edge typeのNeo4j batch
- Neo4j query/transaction timeoutで遅いGraph探索を中断する
- 子Requirementを同一ラウンドへ割り込ませない
- Requirement総数上限到達時にmandatoryを優先する
- 上限超過Requirementを削除せずunresolvedに記録する
- Article候補総数上限で先着Requirementが枠を独占しない
- 後から生成されたmandatoryのためoptional低順位候補を退避する
- shadow phase budget超過時に現行回答を継続する
- shadowがfull-answer-safe残余の設定割合を超えて使わない
- `AGENT_ANSWER_RESERVE_SEC < LLM_TIMEOUT_SEC`をhealthとtraceで警告する
- 各componentの実効timeoutを共有探索deadlineの残時間以下に切り詰める
- embedding・OpenSearch・Neo4j呼び出しもrequest残時間をtimeout overrideとして受け取る
- 同一componentの複数回呼び出しを`componentActualInvocations`へ記録する
- 1回あたりのCross-Encoderペア数とOpenSearch・Neo4j batch数が上限を超えない
- 最大呼び出し回数込みのcomponent割当時間合計がfull-answer-safe探索予算を超えない
- agent-apiのhealthがwall timeを公開し、eval-runnerが短すぎる`REQUEST_TIMEOUT_SEC`で評価を開始しない
- LLM再計画を含むlogical call数が上限を超えない

### 16.4 ガイドテスト

- `EXPLAINS`から法令本文を取得する
- `MENTIONS`だけではmustIncludeにしない
- 未確認RelationAssertionを候補拡張にだけ使う
- ガイドだけでは法令Requirementをresolvedにしない
- ガイドと法令の法令時点が異なる場合を表示する

### 16.5 回帰テスト

- 現行の明示条番号直接取得
- 選択肢問題のpredictedAnswer
- 既存のGraph表示
- 再ランカー障害時fallback
- ガイドラインの最終根拠混入防止
- datasetIssue、法令時点差、誤goldの別集計
- 既存`evidence_selector.py`と新`layered_context_assembler.py`の独立
- 12000文字予算でのcontext切り詰めtrace
- shadowで旧16chunksと新16chunksの切り詰め量を別々に計算する
- 同一chunkリストと同一文字予算を整形した場合、shadow・activeで切り詰め結果が一致する
- chunk数を増やして本文が失われる場合の採用拒否
- 16chunksでmandatoryな`conclusionGroup`を優先度順に原子的に被覆する
- 1chunkによる複数Requirementのshared coverage
- 16件から溢れたresolved Requirementを`omitted_context_budget`にする
- mandatory Requirementが16件を超える場合も優先度順が決定的で、超過分を`unresolvedForAnswer`へ渡す
- `conclusionGroup`全体が残枠に収まらない場合、片方だけを追加せずgroup全体を除外する
- 大きい未完了groupを除外した残枠で、次順位の完結可能なgroupを採用する
- primary groupが除外された場合、利用者向け回答に未回答の主論点と理由を表示する
- 全primary groupが除外された場合、`insufficient_primary_evidence`となり通常の法的結論を生成しない
- 全mandatory groupが除外された場合、optional・ガイドだけで16枠を埋めない
- optional・ガイドの合計が`MAX_AUXILIARY_CONTEXT_CHUNKS`を超えない
- contextへ入らないRequirementについて回答が断定しない

## 17. 評価指標

既存のcitation評価だけでなく、探索状態を評価する。

| 段階 | 指標 |
|---|---|
| 初期論点 | 明示要求被覆率、論点数、重複率 |
| Requirement | mandatory Requirement完全充足率 |
| conclusionGroup | 主group完全被覆率、全mandatory group完全被覆率、除外group数、必要追加chunk数 |
| 候補生成 | 必要Article候補再現率 |
| Graph | 必要関係到達率、誤関係率、未確認関係利用数 |
| リランク | Requirement別Article再現率 |
| コンテキスト | Article完全到達率、Article再現率 |
| 回答 | 根拠引用率、回答要点完全率、正答率、answerStatus別件数 |
| ループ | 展開ラウンド数、ホップ数、未解決理由 |
| 性能 | 全体とshadow phaseのp50/p90/p99、timeout率、Cross-Encoderペア数・ペア/秒 |
| コンテキスト予算 | 切り詰めchunk数、drop数、original/included文字数 |

group指標は、評価データ側で必要Articleをprimary groupとその他のmandatory groupへ対応付け、
旧16chunksと新16chunksの両方へ同じ事後評価を適用する。group情報がない評価項目は分母へ
混ぜず、group評価可能件数を併記する。評価用group・goldは検索、Graph展開、再ランキング、
回答生成へ渡さない。

### 17.1 shadow比較

同じ実行で次を保存する。

```text
旧候補プール
旧30chunks
旧16chunks

新Article候補
新resolved Requirements
新16chunks
```

第一段階では回答LLMへ旧コンテキストを渡し、候補・Article到達率を決定的に比較する。
この段階でも新16chunksを旧方式と同じprompt整形関数へ通すが、LLMには送信しない。
これにより`contextTruncation.oldContext`と`contextTruncation.newContext`を同一実行で測る。
第二段階で新コンテキストを渡し、引用・回答要点・正答率を評価する。

shadowは現行処理に新方式を追加するため計算時間は無料ではない。shadow専用phase budgetを超えた
行は正常な旧新比較へ混ぜず、`shadowIncomplete`として別集計する。

### 17.2 合格条件

初期合格条件:

1. 既知問題を除く必要Article候補再現率が旧方式以上。
2. 主group（`isPrimary=true`）完全被覆率が、同じgroup定義で測った旧16chunks以上。
3. 全mandatory group完全被覆率が、同じgroup定義で測った旧16chunks以上。
4. primary groupを除外した行で`answerStatus=complete`となる件数が0件。
5. 必要Article完全到達率が旧方式以上。
6. 必要な法律→政令→府令の接続を固定問題ルールなしで到達できる。
7. 現行で正答した問題の重大な回帰がない。
8. 未確認ガイド関係を直接根拠にした回答が0件。
9. 全評価行の`timeBudget.profileName`が採用profileと一致し、component呼び出し回数・時間が
   traceへ記録されている。欠落・不一致行は性能集計から黙って除外せず失敗として扱う。
10. shadow mode・active modeともtimeout率が、同じ採用profileの旧方式から悪化しない。
11. **shadow mode**では、旧回答を使う全体p90が同じprofileの旧方式p90の120%以下である。
12. **shadow mode**では、shadow phaseのp90が
   `LAYERED_SHADOW_PHASE_BUDGET_SEC`以内である。
13. **active mode（shadow無効）**では、探索phaseのp90が採用profileの
    full-answer-safe探索予算以内、かつ同じprofileの旧方式探索p90の120%以下である。
14. **active mode（shadow無効）**では、全体p90が同じprofileの旧方式p90の120%以下かつ
    agent wall time未満である。
15. **shadow比較**で整形した新16chunksの途中切り詰め率が旧16chunksより悪化しない。
16. gold情報が検索・再ランキング・回答生成へ渡っていない。

必要Article完全到達率の具体的な改善閾値は、lawqa_jp 140問におけるArticle評価の分母を
確定してから設定する。20問・61件だけの差分で閾値を固定しない。

法的な正答は自動指標だけで確定せず、代表問題を専門知識のあるレビュー担当者が確認する。

## 18. 過学習防止

- 問題文やgold条番号をコードへ埋め込まない。
- 法令名別の特別扱いではなく、authorityType、role、edge registryで一般化する。
- plannerにはgoldや期待Articleを渡さない。
- 条文到達判定はArticle IDで行うが、検索ロジックは評価IDを参照しない。
- 既知問題、法令時点差、誤gold、複数正解を別集計する。
- 自然言語例、lawqa_jp、未見質問で評価する。
- Cross-Encoder有無、Graph有無、ガイド有無のablationを行う。
- 同じ質問の反復だけでなく、表現を変えた未見質問で確認する。

## 19. 既存方式との互換性

feature flag:

```text
AGENT_LAYERED_LEGAL_RETRIEVAL=false
AGENT_LAYERED_LEGAL_RETRIEVAL_SHADOW=true
```

移行中は次を維持する。

- 現行planner queries
- 現行Hybrid検索
- 現行Graph展開
- 現行30→16選抜
- 現行回答・引用

新方式はshadowでIssue、Requirement、Article候補、関係、コンテキスト候補を計算する。
新方式の内部障害が現行回答へ影響してはならない。

旧 `legal_issue_coverage_retrieval.md` のfeature flagは、新方式のPhase 5が安定するまで維持する。
新方式切替後に、旧論点被覆セレクタを削除するか、fallbackとして残すかを別途決定する。

## 20. 実装順序

1. 旧方式を110秒・280秒profileで実測し、対応profile候補を決める
2. planner・検索・Graph・reranker・Evaluator/replanのper-call時間と最大呼び出し回数を決める
3. Graphエッジ・nodeTypeの実データ棚卸し
4. authorityTypeの導出調査とregistry入力一覧
5. Cross-Encoderスループットと旧新コンテキスト切り詰めの計測
6. 時間・LLM・shadow phase予算の確定
7. エッジレジストリと監査テスト
8. Graph起点・除外理由のtrace
9. authorityTypeのschema・mapping追加
10. IMPLEMENTS confidence、未実装エッジ、Graph batch API・Neo4j timeout
11. schema変更をまとめた再シード
12. roleレジストリ
13. structured issue plannerのshadow出力
14. EvidenceRequirement状態モデル
15. Article単位の候補集約
16. Requirement別の法令内検索
17. Requirement別Cross-Encoder
18. 子Requirement生成
19. priority batchと停止条件
20. ガイドRelationAssertion
21. Articleからchunkへの`conclusionGroup`単位の選択とprimary欠落時の回答制御
22. shadow評価ランナー
23. 自然言語20問
24. lawqa_jp 140問のshadow比較
25. 新コンテキストでの回答評価
26. feature flag切替判断

各工程でテストを先に追加し、後続工程までテストを延期しない。

## 21. 他AIレビューで確認してほしい事項

### オントロジー

1. `REFERENCES`を原文上の事実、`IMPLEMENTS`等を派生意味として分ける設計は妥当か。
2. 派生エッジを物理保存し、`derivedFromEdgeId`で原文参照へ結ぶ設計は妥当か。
3. `APPLIED_BY`の現行名・方向を維持する判断に問題はないか。
4. IMPLEMENTSの段階的confidenceと信頼判定条件は十分か。
5. `DEFINES`、`USES_TERM`、`EXCEPTION_TO`の始点・終点は十分明確か。
6. ガイド由来関係をRelationAssertionノードにする設計は過剰でないか。
7. 法令時点・改正前後の関係をどの粒度で保持するか。
8. authorityTypeのregistry明示と`ordinance_unspecified|unknown` fallbackで、
   Mの省令・内閣府令を安全に区別できるか。

### 論点・役割

9. roleFamilyとroleSubtypeの粒度は過不足ないか。
10. `delegated_detail`をroleSubtypeにせず、`enteredBy`とrelation pathで表す判断は妥当か。
11. 初期plannerとルール補正の責務分担は妥当か。
12. plannerが完全な必要役割を予測しない前提は適切か。
13. Requirementの充足規則をどこまで決定的ルールにできるか。

### 探索ループ

14. priority batchをラウンド単位で処理する方式は実装・検証しやすいか。
15. 1 query=1 hop、最大3論理ホップ・3展開ラウンドで十分か。
16. 複数edge typeのNeo4j batch traversalは、関係別の診断可能性を維持できるか。
17. LLM再計画を最大1回に限定してよいか。
18. 循環・重複排除キーは十分か。
19. 未解決Requirementを回答へどう表現するか。

### 上限

20. 主論点soft 6 / hard 8は妥当か。
21. Requirement総数24、Article総数64は適切か。
22. Phase 0実測からCross-Encoderペア上限を決める手順は妥当か。
    per-call、per-round、request全体の3段階上限で十分か。
23. 初期16chunksを維持し、文字予算拡張後だけ24を検討する判断は妥当か。
24. tool call上限ではなくbatch数と時間で制御する方針は妥当か。
25. Requirement上限到達時の優先処理・unresolved記録は十分か。
26. Article候補総数をmandatoryへround-robin配分し、optional候補を退避する設計は妥当か。
27. 最終16chunksを`conclusionGroup`単位の原子的set coverageで配る設計は妥当か。
28. 大きい`conclusionGroup`を丸ごと除外し、次の完結可能なgroupへ枠を回す判断は妥当か。
    primary groupが0件の場合に通常回答と補助資料充填を止める判断は妥当か。

### 評価・移行

29. shadow modeで旧新を同一実行比較できる範囲はどこまでか。
30. full-answer-safe残余の50%とするshadow上限は現行回答を十分保護するか。
31. 必要Article完全到達率を主指標にすることは妥当か。
    主group・全mandatory group完全被覆率を併用することで安全側への過剰な脱落を検出できるか。
32. 旧方式実測後に110秒互換profileまたは280秒運用評価profileを選ぶ条件と、
    component時間割当は妥当か。
33. Graph関係精度の人手サンプル監査件数はどの程度必要か。
34. authorityTypeとedge変更を一度の再シードへまとめられているか。
35. 旧論点被覆方式をいつ削除できるか。

## 22. 着手前の決定事項

次をレビュー後に確定してからコード変更へ進む。

1. Graphの正規エッジ方向と派生エッジ保存方針
2. authorityTypeの生成元、registry schema、index mapping、未判別府省令の検索包含規則
3. 現行5エッジと新規実装エッジの境界
4. node/edge/role registryのスキーマ
5. IMPLEMENTSの段階的confidence
6. roleFamily / roleSubtypeの最終enum
7. structured planner JSON schema
8. EvidenceRequirementの一意キー、充足規則、`conclusionGroup`生成・継承規則、
   総数上限到達時の挙動
9. Article候補総数をRequirement間で公平に配分する規則
10. 旧方式実測後に採用する対応profile、full-answer-safe探索予算、
    `PLANNER_TIMEOUT_SEC`、`EMBEDDING_TIMEOUT_SEC`、OpenSearch・Neo4j timeout、
    `RERANK_TIMEOUT_SEC`、`EVALUATOR_TIMEOUT_SEC`を含むcomponentのper-call割当、
    最大呼び出し回数、Cross-Encoderのper-call・request全体ペア上限
11. 最終16chunksの`conclusionGroup`原子的配分、mandatory最低保証、優先度、
    shared coverage、primary欠落時の`answerStatus`、補助枠上限、利用者向け表示
12. `resolved`と`contextStatus`の分離、`unresolvedForAnswer`の回答制御
13. LLM呼び出しhard capを4へ変更する期間と旧Evaluatorの統合方針
14. 最終コンテキスト文字予算、chunk数、切り詰めtrace
15. RelationAssertionの保存方式
16. GraphClientの複数edge type batch APIとNeo4j query/transaction timeout
17. Graph schema versionと一度の再シード手順
18. shadow専用phase budget、残余割合、group被覆指標を含む評価metricVersion
19. round 0の全初期batch処理と、round 0を除く最大3展開ラウンド
20. agent-api healthとeval-runnerによるrequest timeout整合性検証
21. feature flag名と旧方式fallback期間
