# データセット設計

## 1. データセット全体

```text
datasets/
  lawqa_jp/
    input/                 # 問題文・選択肢。RAG対象外
    gold/                  # 正解・期待参照条文。RAG対象外
    egov_law_corpus/       # e-Govから事前取得・前処理した法令本文。RAG検索対象
    instruction_manual/    # lawqa_jp回答方法マニュアル。任意のInstruction RAG対象

  ordinance_manual/
    source-documents/
    vector-documents/
    graph-artifacts/
    eval/

  local_government_law/
    source-documents/
    vector-documents/
    graph-artifacts/
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

## 4. 条例制定・改正マニュアル

POC用に自作する。目的は、manual → law → citation の検証。

### 主要 contentUnit

```text
manual-ordinance-001-sec-001: 条例・規則・要綱の違い
manual-ordinance-001-step-001: 政策目的と制度化の必要性を整理する
manual-ordinance-001-step-002: 条例・規則・要綱のいずれで定めるか判断する
manual-ordinance-001-step-003: 関係法令との抵触有無を確認する
manual-ordinance-001-step-004: 条例案の素案を作成する
manual-ordinance-001-step-005: 法規担当課へ例規審査を依頼する
manual-ordinance-001-step-006: 庁内調整・パブリックコメント要否を確認する
manual-ordinance-001-step-007: 議案として議会へ提出する
manual-ordinance-001-step-008: 議決後、公布・施行手続を行う
```

### Graph例

```text
ProcedureStep: manual-ordinance-001-step-007
  -> BASED_ON_LAW
Article: law-322AC0000000067-article-96

ProcedureStep: manual-ordinance-001-step-008
  -> BASED_ON_LAW
Article: law-322AC0000000067-article-16
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

ID命名規則は `docs/id_naming_rules.md` を正とする。地方自治法は `law-322AC0000000067` をdocumentIdとし、英語名ベースIDは使わない。

## 8. OpenSearch向けメタデータ

Step1 / Step2 はOpenSearch直を前提にするため、OpenSearch投入サンプルは `samples/metadata/opensearch_document.sample.json` と `samples/metadata/opensearch_index_mapping.sample.json` を使う。Bedrock Knowledge Bases固有のsidecar形式は、本POCの実行形式ではない。
