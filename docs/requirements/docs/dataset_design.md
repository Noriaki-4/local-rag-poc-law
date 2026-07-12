# データセット設計

## 1. データセット全体

```text
datasets/
  lawqa_jp/
    input/                 # 問題文・選択肢。RAG対象外
    gold/                  # 正解・期待参照条文。RAG対象外
    egov_law_corpus/       # e-Govから事前取得・前処理した法令本文。RAG検索対象
    instruction_manual/    # lawqa_jp回答方法マニュアル。任意のInstruction RAG対象

  archived_manuals/
    source-documents/      # 原本保管のみ。RAG/Graph/eval対象外
```


## 2.0 lawqa_jp の出所・ライセンス・構成

出所:

- 配布元: デジタル庁 GitHub `digital-go-jp/lawqa_jp`
- データセット名: 日本の法令に関する多肢選択式QAデータセット
- 主なファイル:
  - `data/selection.json`: コンテキスト・設問・選択肢・正答を含む元データ
  - `data/selection.csv`: CSV形式のQ&Aデータ
  - `data/selection_randomized.json`: 選択肢順をランダマイズしたデータ
  - `data/selection_with_reference_randomized.json`: 外部法令・省令などへの参照を含む設問を抽出し、選択肢順をランダマイズしたデータ
  - `data/law_list.json`: 設問で参照されている法令・出典条文情報の一覧

ライセンス:

- 公共データ利用規約（第1.0版） `public_data_license_v1.0`
- 再配布・成果物公開の可否は、同規約および参照先資料の利用条件を確認すること。POCパッケージ内にはlawqa_jp本体データを同梱しない。

内容構成の注意:

- `selection.json` の `コンテキスト` は、問題の背景となる法令本文・準拠文書抜粋であり、RAG検索結果ではない。
- `references` はコンテキスト参照元URLであり、Retrieved Context評価では期待根拠として扱う。
- 参照元には e-Gov 法令だけでなく、金融庁・厚労省・国交省等のガイドライン、Q&A、PDF資料が含まれる可能性がある。
- 初期POCでは、e-GovからXML取得できる法令由来問題を主対象にする。PDFガイドライン由来問題を除外する場合は、件数と除外理由を `evaluation_design.md` の評価分割に記録する。

## 2. lawqa_jp の扱い

### RAG対象にしない

- 問題文
- 選択肢
- 正解ラベル
- 解説
- gold references

### RAG検索対象にする

- e-Gov から事前ダウンロードした法令XML / 法令本文
- 上記を条・項・号単位に分割した Markdown / JSON
- 上記から生成した GraphRAG 用 Article / Paragraph / REFERENCES / DEFINES / EXCEPTION_TO

補足: e-Govそのものを実行時のRAG検索先にするのではない。e-Govから取得済みの法令本文をローカルのOpenSearch / GraphDBに登録して検索対象にする。

### 任意でRAG対象にする

- lawqa_jp回答方法マニュアル
- GraphRAG利用判断マニュアル

これは Evidence RAG ではなく Instruction RAG として扱う。

## 2.1 e-Gov 由来法令本文の取得方針

1. lawqa_jp の参照法令一覧から、e-Govで取得可能な法令番号を抽出する。
2. 対象法令XMLをe-Govから事前ダウンロードする。
3. XMLを条・項・号単位に分割し、本文Markdown / JSONとmetadataを生成する。
4. 生成物をMinIOに配置し、OpenSearchとNeo4jに登録する。
5. 実行時のAgentはe-Govへ直接問い合わせず、ローカルに登録済みのインデックスを検索する。

Step1 POC では `SEED_LAWQA_EGOV=true` を指定した `/admin/seed` により、lawqa_jp の `references` から e-Gov 法令IDを抽出し、e-Gov API の XML を条・項・号単位で OpenSearch / Neo4j に投入できる。
PDF等の e-Gov 以外の参照元は、この自動投入の対象外とする。

### 本則・附則の分離

XMLの `MainProvision`（本則）と `SupplProvision`（附則）は条番号を別々に振り直すため、単純に条番号だけで `contentUnitId` を作ると衝突し、後勝ちで本則条文が失われる（本則第8条が附則第8条に上書きされる等）。投入時は次を守る。

- 本則: `law-<法令番号>-article-<条番号>`
- 附則: `law-<法令番号>-suppl-<附則index>-article-<条番号>`（[id_naming_rules.md](./id_naming_rules.md) 3.1 参照）
- 各文書に `provisionType`（`main` / `supplementary`）と `sectionKey` を付与する。
- 附則も検索対象として投入する。lawqa_jp の参照は主に本則だが、附則（経過措置・罰則等）も設問根拠になり得るため保持する。

条・号の枝番（「第2条の12」「第二号の二」）はアンダースコア連結で保持する（`article-2_12`、`item-2_2`）。


## 2.2 選択肢ラベルの正規化

lawqa_jp元データでは選択肢ラベル・正答ラベルが小文字 `a`〜`d` で表現される場合がある。POC内部の評価ログ・サンプルでは大文字 `A`〜`D` に正規化する。

前処理ルール:

```text
input label:  a / b / c / d
normalized:   A / B / C / D
```

`goldAnswer`、`predictedAnswer`、`choiceJudgements` のキーはすべて大文字 `A`〜`D` に統一する。評価スクリプトは入力時に `upper()` 正規化を行い、元データの表記揺れで照合ミスが起きないようにする。

## 3. lawqa_jp 評価モード

| Mode | 名称 | 内容 | RAG検証か |
|---|---|---|---:|
| 0 | No Context | 問題文 + 選択肢のみ | × |
| 1 | Gold Context | lawqa_jp付属コンテキストを渡す | ×。上限性能確認 |
| 2 | Retrieved Context | e-Govから事前取得・登録した法令本文インデックスを検索 | ○ |
| 3 | Agentic Retrieved Context | クエリ分解 + Vector + Graph | ○ |

## 4. 原本保管のみのマニュアル

条例制定・改正業務マニュアルの原本サンプルは、将来検討用に保管する。
現行POCでは、このマニュアルを OpenSearch / GraphDB / 評価データへ投入しない。

原本:

```text
docs/requirements/samples/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md
```

扱い:

```text
- document_registry へは登録しない
- OpenSearch へは投入しない
- Neo4j / Neptune 用 Graph artifact は作成しない
- eval item は作成しない
```

## 5. フォルダ構成

```text
knowledge-root/
  source-documents/
  derived-artifacts/
    vector-documents/
    graph-artifacts/
  document-registry/
  eval-data/
  instruction-manuals/
```

## 6. document-registry方針

`documents.jsonl` を正本とし、OpenSearch / Neo4j は再生成可能なインデックスとみなす。



## 7. ID命名規則

ID命名規則は `docs/id_naming_rules.md` を正とする。

## 8. OpenSearch向けメタデータ

Step1 / Step2 はOpenSearch直を前提にするため、OpenSearch投入サンプルは `samples/metadata/opensearch_document.sample.json` と `samples/metadata/opensearch_index_mapping.sample.json` を使う。Bedrock Knowledge Bases固有のsidecar形式は、本POCの実行形式ではない。
