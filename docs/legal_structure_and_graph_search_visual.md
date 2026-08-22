# 法令構造・OpenSearch・Graph検索の視覚ガイド

本書は、e-Gov XMLの法令・条・項・号構造を`Document / Article / Paragraph / Item`として
OpenSearchとNeo4jでどう扱い、Solverがどの単位で検索するかを人間向けに図解する。IDの正本は
[id_naming_rules.md](id_naming_rules.md)、現行Graph seedの正本は
[graph_edge_construction.md](graph_edge_construction.md)、新Agentの実装計画は
[generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md)とする。

本書の「再計画案」は2026-08-20の再検討内容であり、現行実装との差を明示する。

## 1. 図の表記

同じ図にNeo4j、OpenSearch、Solverを置く場合は、箱の種類と領域を次のように分ける。

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Neo4j Graph領域              ┃
┃                              ┃
┃  ╔════════════════════════╗  ┃
┃  ║ Neo4j Node            ║  ┃
┃  ╚════════════════════════╝  ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

┌──────────────────────────────┐
│ OpenSearch Document          │
└──────────────────────────────┘

┌──────────────────────────────┐
│ Solver / Toolの処理          │
└──────────────────────────────┘
```

## 2. e-Gov XMLを正本とする

条・項・号が別の単位なのは、e-Gov XMLがこの構造を持つためである。平坦化テキストは検索・表示用の
派生物であり、参照先解決や本則・附則判定の正本にはしない。

### 2.1 保存済みe-Gov XMLの実例

次は、データセットに保存した「金融商品取引法施行令」（法令ID `340CO0000000321`）のXMLから、
第7条第1項と第2号を抜粋したものである。長い本文と同階層の要素だけを`<!-- 省略 -->`としている。
タグの階層、`Num`属性、表示用の番号、本文は保存済みXMLに基づく。

```xml
<DataRoot>
  <Result>
    <Code>0</Code>
    <Message />
  </Result>
  <ApplData>
    <LawId>340CO0000000321</LawId>
    <LawFullText>
      <Law Lang="ja" Era="Showa" Year="40" Num="321"
           PromulgateMonth="09" PromulgateDay="30"
           LawType="CabinetOrder">
        <LawNum>昭和四十年政令第三百二十一号</LawNum>
        <LawBody>
          <LawTitle
            Kana="きんゆうしょうひんとりひきほうせこうれい"
            Abbrev="証取法施行令,金商法施行令">
            金融商品取引法施行令
          </LawTitle>

          <!-- 目次等を省略 -->

          <MainProvision>
            <Chapter Num="3">
              <ChapterTitle>第三章　公開買付けに関する開示</ChapterTitle>
              <Section Num="1">
                <SectionTitle>第一節　発行者以外の者による株券等の公開買付け</SectionTitle>

                <Article Num="7">
                  <ArticleCaption>（公開買付けの適用除外となる買付け等）</ArticleCaption>
                  <ArticleTitle>第七条</ArticleTitle>
                  <Paragraph Num="1">
                    <ParagraphNum />
                    <ParagraphSentence>
                      <Sentence Function="main" Num="1" WritingMode="vertical">
                        法第二十七条の二第一項ただし書に規定する政令で定める
                        株券等の買付け等は、次に掲げる株券等の買付け等…とする。
                      </Sentence>
                      <Sentence Function="proviso" Num="2" WritingMode="vertical">
                        ただし、第七号及び第八号に掲げる株券等の買付け等に
                        あつては、…特定市場外買付け等を除く。
                      </Sentence>
                    </ParagraphSentence>

                    <!-- 第1号を省略 -->
                    <Item Num="2">
                      <ItemTitle>二</ItemTitle>
                      <ItemSentence>
                        <Sentence Num="1" WritingMode="vertical">
                          株式の割当てを受ける権利を有する者が当該権利を
                          行使することにより行う株券等の買付け等
                        </Sentence>
                      </ItemSentence>
                    </Item>
                    <!-- 第3号以降を省略 -->
                  </Paragraph>
                  <!-- 第2項以降を省略 -->
                </Article>
              </Section>
            </Chapter>
          </MainProvision>

          <SupplProvision>
            <SupplProvisionLabel>附　則</SupplProvisionLabel>
            <Paragraph Num="1">
              <ParagraphNum>１</ParagraphNum>
              <ParagraphSentence>
                <Sentence Num="1" WritingMode="vertical">
                  この政令は、昭和四十年十月一日から施行する。
                </Sentence>
              </ParagraphSentence>
            </Paragraph>
            <!-- 省略 -->
          </SupplProvision>
        </LawBody>
      </Law>
    </LawFullText>
  </ApplData>
</DataRoot>
```

参照した保存ファイルは
[金融商品取引法施行令のXML snapshot](../datasets/lawqa_jp/egov_law_corpus/documents/340CO0000000321/575982afba368dd8750149e6852934f69206a3ae93b1516e49e891d124bd1123.xml)
である。上のXMLは説明用の抜粋であり、原文全体を再掲したものではない。

各要素の役割は次のとおり。

| e-Gov XML要素 | 実例 | このレポでの扱い |
|---|---|---|
| `DataRoot / Result / ApplData` | API結果、`LawId` | e-Gov API応答の外枠。法令本文の階層ではない |
| `LawFullText / Law` | 政令、昭和40年、321号 | 取得した法令版の本文と基本属性を包む |
| `LawBody / LawTitle` | 金融商品取引法施行令 | `Document`の名称・メタデータになる |
| `MainProvision` | 本則 | 本則の構造スコープ。`provisionType=main`として本則・附則を区別する |
| `Chapter / Section` | 第3章・第1節 | Articleの所属を表す構造。検索用の見出しにも使う |
| `Article Num="7"` | 第7条 | 独立した`Article`。Graph検索と意味関係の基本単位になる |
| `Paragraph Num="1"` | 第1項 | Articleの一部分。項番号が本文上省略される場合も`Num`で識別する |
| `Item Num="2"` | 第2号 | Paragraphの一部分。`ItemTitle`の「二」は表示用表記である |
| `ParagraphSentence / ItemSentence` | 項本文・号本文の入れ物 | Sentenceを親の構造位置へ結び付けるXML上のラッパー。独立Nodeにはしない |
| `Sentence Num="1"` | 号本文 | 実際の文章。Nodeを増やさず、本文と根拠spanの位置として保持する |
| `Subitem1`以下 | イ、ロ、ハ等 | Itemより深い細目。親階層と位置を失わず、本文・根拠spanへ投影する |
| `TableStruct`以下 | 表、行、セル | 表の行列・セル位置を保ったまま本文・根拠spanへ投影する |
| `SupplProvision` | 附則 | 本則とは別の構造スコープ。同じ法令内でも本則Articleと混同しない |

重要なのは、XML要素名と画面上の表記を別々に保持することである。たとえば`Article Num="7"`が
機械識別用の番号を持ち、`ArticleTitle`が「第七条」という表示を持つ。枝番の`第2条の12`は
`Article Num="2_12"`であり、`第2条`の子ではなく、同じ親に属する別Articleである。

また、附則が必ず`Article`を持つとは限らない。上の実例では`SupplProvision`の直下に`Paragraph`がある。
したがって、`MainProvision / SupplProvision`というXML上の祖先を見ずに条番号や平坦化本文だけで登録すると、
本則・附則・改正附則を取り違える。参照解決とGraph登録では、この構造スコープを必ず引き継ぐ。

### 2.2 正本から検索用データへ投影する

```text
┌─────────────────────────────────────┐
│ e-Gov XML：構造を保持する正本       │
│                                     │
│ Law / LawBody                       │
│ ├─ MainProvision                    │
│ │  └─ Chapter / Section             │
│ │     └─ Article                    │
│ │        └─ Paragraph               │
│ │           └─ Item                 │
│ │              └─ Sentence / Table  │
│ └─ SupplProvision                   │
│    ├─ Article → Paragraph → Item    │
│    └─ Paragraph → Item              │
└──────────────────┬──────────────────┘
                   │ 同じsnapshotから投影
          ┌────────┴────────┐
          ▼                 ▼
┌──────────────────┐  ┏━━━━━━━━━━━━━━━━━━━━━━┓
│ OpenSearch       │  ┃ Neo4j Graph         ┃
│ 検索用本文       │  ┃ 構造・参照・意味候補┃
└──────────────────┘  ┗━━━━━━━━━━━━━━━━━━━━━━┛
```

枝番を持つArticleも独立した同階層のArticleであり、`第2条の12`を`第2条`の子にしない。

```text
Document
├─ Article 第2条       article-2
├─ Article 第2条の12   article-2_12
├─ Article 第2条の13   article-2_13
└─ Article 第3条       article-3
```

## 3. OpenSearchでの表現

OpenSearchは検索しやすい単位へ本文を平坦化する。複数項を持つArticleはParagraph単位、長いParagraphに
Itemがある場合はItem単位の文書も作る。短いArticleはArticle全体が1文書になる場合がある。

```text
┌────────────────────────────────────────────┐
│ OpenSearch Document：施行令7条1項9号      │
│                                            │
│ contentUnitId                              │
│   law-...-article-7-paragraph-1-item-9     │
│                                            │
│ parentContentUnitId                        │
│   law-...-article-7-paragraph-1            │
│                                            │
│ articleContentUnitId                       │
│   law-...-article-7                        │
│                                            │
│ documentId                                 │
│   law-340CO0000000321                      │
│                                            │
│ text                                       │
│   第7条1項の導入文＋第9号本文              │
└────────────────────────────────────────────┘
```

OpenSearch内の文書はGraphの親子Edgeを持たない。`parentContentUnitId`と`articleContentUnitId`で
所属を表し、検索結果をArticle単位へ集約する。

```text
┌────────────────────────────┐
│ OpenSearch：Itemへのhit    │
│ articleContentUnitId=A7    │
└──────────────┬─────────────┘
               │ Article IDへ集約
               ▼
┌────────────────────────────┐
│ OpenSearch：Article全文取得│
│ A7に属する全chunk          │
└────────────────────────────┘
```

## 4. Neo4jでの構造表現

`Document / Article / Paragraph / Item`は現行Graphでも別Nodeである。新しく分離するものではない。

```text
┏━━━━━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━━━━┓
┃                                                 ┃
┃  ╔══════════════════════════════════════════╗   ┃
┃  ║ Document：金融商品取引法施行令          ║   ┃
┃  ║ graphNodeId=law-340CO0000000321          ║   ┃
┃  ╚════════════════════╤═════════════════════╝   ┃
┃                       │ HAS_CONTENT_UNIT         ┃
┃                       ▼                          ┃
┃  ╔══════════════════════════════════════════╗   ┃
┃  ║ Article：第7条                           ║   ┃
┃  ║ graphNodeId=law-...-article-7            ║   ┃
┃  ╚════════════════════╤═════════════════════╝   ┃
┃                       │ HAS_CONTENT_UNIT         ┃
┃                       ▼                          ┃
┃  ╔══════════════════════════════════════════╗   ┃
┃  ║ Paragraph：第1項                         ║   ┃
┃  ║ graphNodeId=law-...-paragraph-1          ║   ┃
┃  ╚════════════════════╤═════════════════════╝   ┃
┃                       │ HAS_CONTENT_UNIT         ┃
┃                       ▼                          ┃
┃  ╔══════════════════════════════════════════╗   ┃
┃  ║ Item：第9号                              ║   ┃
┃  ║ graphNodeId=law-...-item-9               ║   ┃
┃  ╚══════════════════════════════════════════╝   ┃
┃                                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

役割は次のように分ける。

| 単位 | Graph・意味解釈での役割 |
|---|---|
| `Document` | 法令名、法律・政令・府省令等の種別、snapshot、Articleの所属 |
| `Article` | Graph検索の起点・候補、条文間の意味関係の端点 |
| `Paragraph` | Article内の項構造、参照が書かれた位置 |
| `Item` | Paragraph内の号構造、参照が書かれた位置 |
| `Sentence / Table cell` | 意味判断に使った具体的な原文位置。初期実装ではNodeを増やさずspanとして保持する |

## 5. REFERENCESと構造位置

### 5.1 現行実データ

現行schema version 9では、公開買付府令2条の5から施行令7条への`REFERENCES`はArticle間にあり、
実際に参照が書かれた府令2条の5第1項をRelationプロパティで保持している。

```text
┏━━━━━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━━━━┓
┃                                                 ┃
┃  ╔══════════════════════╗                       ┃
┃  ║ Article              ║                       ┃
┃  ║ 公開買付府令2条の5   ║                       ┃
┃  ╚═══════════╤══════════╝                       ┃
┃              │ REFERENCES                       ┃
┃              │ sourceContentUnitId=第1項        ┃
┃              │ citationText=「令第7条第1項…」  ┃
┃              ▼                                  ┃
┃  ╔══════════════════════╗                       ┃
┃  ║ Article              ║                       ┃
┃  ║ 施行令7条            ║                       ┃
┃  ╚══════════════════════╝                       ┃
┃                                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

### 5.2 再計画で推奨する形

構造Nodeは細かく保持するが、Graph検索と意味分類を単純にするため、`REFERENCES`の検索上の端点は
Articleへ統一する。参照が書かれた項・号・Sentence・表セルはRelationの構造情報として失わない。

```text
┏━━━━━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━━━━┓
┃                                                 ┃
┃  ╔═══════════╗  REFERENCES   ╔═══════════╗      ┃
┃  ║ Article A ║───────────────▶║ Article B ║      ┃
┃  ╚═══════════╝                ╚═══════════╝      ┃
┃                                                 ┃
┃  REFERENCES                                     ┃
┃  ├─ sourceContentUnitId：Paragraph / Item ID    ┃
┃  ├─ targetContentUnitId：参照先の正確な位置     ┃
┃  ├─ citationText                                ┃
┃  ├─ sourceSpan                                  ┃
┃  ├─ targetResolutionMethod                      ┃
┃  └─ basisEdgeId                                 ┃
┃                                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

同じArticleペアに複数の参照出現がある場合は、各出現と`basisEdgeId`の対応を維持する。Articleペアへ
まとめるために参照箇所を消したり、別の参照出現の意味を流用したりしない。

現行の`generic_iterative_agent_framework_plan.md`にはParagraph／Itemも`REFERENCES`端点にできる記述が
残っている。上記のArticle端点＋構造位置方式を正本へ反映する変更は、再計画確定時に行う。

## 6. 意味分類結果をNeo4jへ保存する流れ

意味分類の当事者はArticleであり、Paragraph／Itemは参照の構造位置、根拠抜粋は意味判断に使った本文の
一部分である。ただし、プログラム上の
データオブジェクトとNeo4j Nodeには似た名前がある。次の3つを区別する。

### 6.1 根拠抜粋とは何か

根拠抜粋とは、条文本文を短く切った一部分である。内部実装では`ArticleSpan`または`span`と呼ぶが、
法令固有の概念、e-Gov XML要素、Neo4j Nodeのいずれでもない。Article全文の中からLLMが根拠を既知IDで
指名できるよう、プログラムが各抜粋へ番号を付ける。現在の実装は平坦化したArticle本文を
改行と`。！？`で原則1文ずつに分け、400文字を超える文だけさらに分割する。

```text
Article 第7条の全文
├─ article-7::span-1
│    「法第二十七条の二第一項ただし書に規定する…」
├─ article-7::span-2
│    「ただし、第七号及び第八号に掲げる…」
└─ article-7::span-3
     「金融商品取引業者のうち…」
```

LLMは自由な文字列を根拠IDとして作らず、入力されたspan IDから、意味関係を支える参照元側と参照先側の
抜粋を1つずつ選ぶ。Programは選択されたIDが入力Articleに存在することだけを検証し、どの抜粋が法的な
根拠かは判断しない。

`span`はXMLの`Sentence`と一致するとは限らず、構造上の参照先解決には使わない。役割の違いは次のとおり。

| 情報 | 表すもの |
|---|---|
| Article ID | 意味関係を結ぶ条文 |
| `sourceContentUnitId` | 参照が書かれたParagraph／ItemというXML上の構造位置 |
| 根拠抜粋ID（内部名：supporting span ID） | LLMが意味判断の根拠として選んだArticle本文中の番号付き抜粋 |

### 6.2 プログラム上の型とNeo4j Node

```text
┌────────────── LLM・プログラム領域 ──────────────┐
│                                                │
│  ProposedRelationAssertion                    │
│  LLMが返す「この意味関係が成立する」という出力 │
│                    │                           │
│                    │ 既知ID・型・根拠整合を検証│
│                    ▼                           │
│  RelationAssertionRecord                      │
│  Neo4jへ書き込むためのPythonデータオブジェクト │
│                                                │
└────────────────────┬───────────────────────────┘
                     │ GraphClientがNodeへ変換
                     ▼
┏━━━━━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━━━━┓
┃                                                 ┃
┃     ╔════════════════════════════════════╗      ┃
┃     ║ (:RelationAssertion)              ║      ┃
┃     ║ 意味関係候補を表すNeo4j Node      ║      ┃
┃     ║ proposedPredicate=IMPLEMENTS等     ║      ┃
┃     ╚═══════════╤══════════════╤═════════╝      ┃
┃          SUBJECT│              │OBJECT           ┃
┃                 ▼              ▼                 ┃
┃       ╔═════════════╗    ╔═════════════╗         ┃
┃       ║ (:Article A)║    ║ (:Article B)║         ┃
┃       ╚═════════════╝    ╚═════════════╝         ┃
┃                                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

`RelationAssertionRecord`というPythonオブジェクト自体をNeo4jへ保存するわけではない。GraphClientがその
フィールドをNeo4j Nodeのプロパティへ写し、Node label `RelationAssertion`を付ける。

人間向けには、上のNeo4j表現は次の意味関係候補を表している。

```text
Article A ──「IMPLEMENTSかもしれない」──▶ Article B
```

これを直接`IMPLEMENTS` Relationにしないのは、確定した法的事実ではなく、LLMが提示した根拠付き候補だからである。
候補自体をNodeにすることで、分類Run、根拠となった原文`REFERENCES`、参照記載箇所、両Articleの根拠抜粋を
それぞれ結び付けられる。

```text
(:RelationAssertion)
├─ proposedPredicate：提案された意味関係
├─ basisEdgeId：根拠になった原文REFERENCES
├─ sourceContentUnitId：参照が書かれた項・号
├─ subjectSupportingSpanId / Quote：SUBJECT側の根拠位置・原文
├─ objectSupportingSpanId / Quote：OBJECT側の根拠位置・原文
└─ classificationRunId：どの分類実行で生成したか
```

同じ参照元条文と参照先条文でも、根拠箇所または意味が異なれば別の`(:RelationAssertion)` Nodeを保存できる。

## 7. OpenSearchとGraphをつなぐArticle ID

OpenSearchとNeo4jは、同じsnapshotとArticle IDを使う。

```text
┌────────────────────────────────┐
│ OpenSearch Document            │
│ 第7条1項9号                    │
│ articleContentUnitId=A7        │
└───────────────┬────────────────┘
                │ 共通Article ID
                ▼
┏━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━┓
┃                                          ┃
┃  ╔════════════════════════════════════╗  ┃
┃  ║ Article：施行令7条                ║  ┃
┃  ║ graphNodeId=A7                    ║  ┃
┃  ╚════════════════════════════════════╝  ┃
┃                                          ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

## 8. 現行のGraph検索

現行Frameworkでは、SolverはGraph検索条件を指定しない。Article本文を取得するとProgramが固定条件で
Graphを自動検索し、起点に接続する順方向・逆方向をまとめて返す。LLMの判断は取得後の候補選別である。

```text
┌──────────────────────────┐
│ Solver                   │
│ fetch_articles(A7)       │
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ Program                  │
│ 固定条件でGraphを自動要求│
└────────────┬─────────────┘
             ▼
┏━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━┓
┃                                     ┃
┃  ╔════════════╗                     ┃
┃  ║Article A7  ║                     ┃
┃  ╚══════╤═════╝                     ┃
┃         ├─ 出ていくRelation         ┃
┃         └─ 入ってくるRelation       ┃
┃                                     ┃
┗━━━━━━━━━━━━━━┯━━━━━━━━━━━━━━━━━━━━━━┛
               │ 候補をまとめて返す
               ▼
┌──────────────────────────┐
│ SolverのGraph Review     │
│ 仮説に関係する候補を選択 │
└──────────────────────────┘
```

現行実装はGraph結果として発見したArticleを、次のGraph検索起点から除外する。そのため、
`金商法27条の2 → 施行令7条 → 公開買付府令2条の5`のような連続探索が1ホップ目で止まる。

## 9. 再計画するOpenSearch・Graph探索

### 9.1 OpenSearch検索語を法令表現へ寄せる

元の質問とWorkItemを上書きせず、LLMがHypothesisから検索用の法令表現を作る。

```text
┌────────────────────────────┐
│ 利用者の表現               │
│ 「必要な手続」             │
└──────────────┬─────────────┘
               ▼
┌────────────────────────────┐
│ SolverのHypothesis         │
│ 開始前の公示・書面提出等が │
│ 必要ではないか             │
└──────────────┬─────────────┘
               ▼
┌────────────────────────────┐
│ OpenSearch query           │
│ 公開買付開始公告           │
│ 公開買付届出書・提出       │
└────────────────────────────┘
```

### 9.2 仮説に沿ったGraph selector

逆引きは参照元が多いため、全`REFERENCES`を取得してからLLMへ分類させない。Solverが検索前に、
起点Article、方向、仮説に必要な意味、Document範囲を指定し、Neo4j側で絞る。

```text
┌──────────────────────────────┐
│ Solver：Graph探索要求        │
│                              │
│ 起点Article                  │
│ 対応WorkItem・Hypothesis     │
│ 順引き／逆引き               │
│ 探す関係の意味               │
│ 法律・政令・府省令等の範囲   │
└───────────────┬──────────────┘
                ▼
┏━━━━━━━━━━━━━━ Neo4j Graph ━━━━━━━━━━━━━━━┓
┃                                         ┃
┃  指定された条件に一致するRelationだけ   ┃
┃  1ホップ検索                            ┃
┃                                         ┃
┃  ╔═══════════╗          ╔═══════════╗   ┃
┃  ║起点Article║─────────▶║候補Article║   ┃
┃  ╚═══════════╝          ╚═══════════╝   ┃
┃       ▲                         │        ┃
┃       │所属                    │所属    ┃
┃  ╔════╧═════╗             ╔════▼═════╗  ┃
┃  ║Document A║             ║Document B║  ┃
┃  ╚══════════╝             ╚══════════╝  ┃
┃                                         ┃
┗━━━━━━━━━━━━━━┯━━━━━━━━━━━━━━━━━━━━━━━━━━┛
               │ 絞り込み済み候補
               ▼
┌──────────────────────────────┐
│ Solver                       │
│ 候補を選び本文取得を要求     │
└──────────────────────────────┘
```

順引きの原文参照は比較的候補が少ないため、仮説に必要なら`REFERENCES`を使用できる。逆引きは高fan-inに
なりやすいため、publish済み意味関係を仮説に合わせて先に絞る。意味分類coverageが不足している場合は、
0件を関係不存在と断定せず、法令表現へ寄せたOpenSearch検索または限定した原文参照検索へ切り替える。

### 9.3 選択した候補を次の起点にできる

Graph呼び出しは常に1回1ホップとするが、累積深度では禁止しない。OpenSearch由来かGraph由来かに
かかわらず、LLMが選択したArticleを次の1ホップ検索の起点にできる。

```text
┌──────────────────────────┐
│ Solver                   │
│ 金商法27条の2を起点選択  │
└────────────┬─────────────┘
             ▼
┏━━━━━━ Neo4j：1ホップ検索（1回目）━━━━━┓
┃ ╔══════════════╗      ╔═══════════╗ ┃
┃ ║金商法27条の2 ║─────▶║施行令7条  ║ ┃
┃ ╚══════════════╝      ╚═══════════╝ ┃
┗━━━━━━━━━━━━━━┯━━━━━━━━━━━━━━━━━━━━━━┛
               ▼
┌──────────────────────────┐
│ Solver                   │
│ 施行令7条を選択・本文確認│
│ 次のGraph起点として要求  │
└────────────┬─────────────┘
             ▼
┏━━━━━━ Neo4j：1ホップ検索（2回目）━━━━━┓
┃ ╔═══════════╗       ╔══════════════╗┃
┃ ║施行令7条  ║──────▶║府令2条の5   ║┃
┃ ╚═══════════╝       ╚══════════════╝┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

2回目も独立した1ホップ検索である。検索爆発は累積深度ではなく、次で抑える。

- Graph候補をProgramが自動再帰展開しない。
- 次の起点と関係の意味はLLMが選ぶ。
- 1回の要求は1方向・1種類の関係に限定する。
- 同一`Article ID + 関係 + 方向 + 構造filter`の成功済み要求を重複実行しない。
- 1 stepの候補選択数、Tool要求数、Cycle時間を機械的に制限する。

## 10. 候補と回答根拠を区別する

OpenSearch hitとGraph候補はナビゲーション情報であり、回答根拠ではない。

```text
┌────────────────────────┐
│ OpenSearch hit         │
│ またはGraph候補        │
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ SolverがArticleを選択  │
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ OpenSearchから         │
│ Article全文を取得      │
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ Solverが本文を評価     │
│ Hypothesisを更新       │
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ 確認済みEvidenceとして │
│ 回答・引用に使用       │
└────────────────────────┘
```

## 11. 現行と再計画の差分

| 観点 | 現行実装 | 再計画案 |
|---|---|---|
| e-Gov構造 | Document・Article・Paragraph・Item Nodeあり | 正本XMLのSentence・表scopeを参照先解決まで維持する |
| OpenSearch | Paragraph／Item等の平坦な検索文書 | 同じ。平坦化データだけで参照先を決めない |
| Graph検索起点 | Article | Articleのまま |
| Graph実行条件 | `fetch_articles`後にProgramが固定条件で自動実行 | SolverがHypothesisに沿ったselectorを指定 |
| 方向 | 順引き・逆引きをまとめて取得 | 1要求につき必要な1方向 |
| 逆引き | 広く取得してLLMが後段選別 | 意味関係でDB側を事前絞り込み |
| 次の起点 | Graph由来Articleは再展開禁止 | LLMが選択したArticleは起点にできる |
| ホップ | 実質最大1ホップ | 各要求1ホップ。累積深度では禁止しない |
| 検索語 | 制度名・観点中心だが法令表現への変換責務が弱い | LLMがHypothesisから法令検索表現を生成 |
| 意味関係 | Articleペア | Articleペアのまま、項・号・spanを根拠位置として保持 |
