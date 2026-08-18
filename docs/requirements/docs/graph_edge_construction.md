# 法令Graph構築仕様

## 1. 目的

法令Graphでは、プログラムが決定的に確認できる構造・原文・来歴と、LLMが本文から分類する
法的意味を分離する。本書はGraph schema version 9のseed構築仕様を定める。法的意味分類と
検索時の扱いは
[`generic_iterative_agent_framework_plan.md`](generic_iterative_agent_framework_plan.md)を正とする。

## 2. seedの責務

`/admin/seed`はLLMを呼ばず、次だけをOpenSearchとNeo4jへ投入する。

| 保存先 | 投入内容 |
|---|---|
| OpenSearch | 法令・ガイドのContent Unit本文、検索フィールド、embedding、snapshot・hash |
| Neo4j Node | `Document / Article / Paragraph / Item` |
| Neo4j Relation | `HAS_CONTENT_UNIT / REFERENCES / EXPLAINS` |

`RelationAssertion / ClassificationRun / ClassificationCheckpoint / SUBJECT / OBJECT / CLASSIFIED_IN`はseed後の非同期分類jobが
作る。`IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`は物理Relationに
せず、`RelationAssertion.proposedPredicate`へ保存する。`APPLIED_BY / MENTIONS`は生成しない。

## 3. snapshotとhash

seedは破壊的な書込みへ入る前に全Content Unitを構築し、embeddingと既存の派生identityを除く
正規化内容から`contentHash`を計算する。さらに次を決定的に作る。

- Article配下のContent Unit hash集合から`articleContentHash`
- Document配下のContent Unit hash集合から`documentContentHash`
- 全Content Unit ID、hash、取得可能な`sourceRevisionId`、`graphSchemaVersion`から
  `sourceSnapshotId`

同じ入力の順序だけを変えてもsnapshotは変わらず、本文・構造・revision・Graph schemaが変われば
snapshotが変わる。取得元がrevision IDを提供しない場合は推測値を作らず、プロパティを未設定とする。
OpenSearch文書とNeo4jのNode・Relationには同じ`sourceSnapshotId`を付ける。

## 4. 決定的Relation

### 4.1 HAS_CONTENT_UNIT

XML等の構造からcontainer→childへ生成する。

```text
Document → Article → Paragraph → Item
```

項・号に`Article` labelを付けない。各Relationには`graphEdgeId`、`sourceContentUnitId`、
`sourceSnapshotId`、`sourceRevisionId`、`graphSchemaVersion`を保存する。

### 4.2 REFERENCES

原文中の明示参照だけを、参照を書いたContent Unit→参照先Content Unitの向きで保存する。

- 同一法令の「第N条」「前条」「次条」
- 下位法令の「法第N条」から同じ法令系統の親法律
- 府省令等の「令第N条」から同じ法令系統の施行令

`同法 / 同令`は先行詞から対象法令を一意に決められる場合だけ接続する。単に条番号が同じ別法令へ
接続しない。Relationには参照文字列、参照元Content Unit、取得できる位置、解決方法を保存する。
旧`referenceKind`は移行監査用に残っていても、意味predicateや検索selectorには使用しない。

### 4.3 EXPLAINS

ガイドの条文注釈・対応表が明示した、ガイド`Document`→法令`Article`だけを保存する。
同じ表の同じ行にある対応を維持し、表全体のArticle集合を直積にしない。前ページからの
`carried_forward`や単なる言及はOpenSearch本文に残すだけで、Graph Relationへ変換しない。

## 5. seed前監査

seedはNeo4j・OpenSearchを変更する前に、少なくとも次を検査し、違反があれば書込みを開始しない。

- dangling Relation、構造循環、Content Unitの複数親がない
- `graphEdgeId`が重複しない
- Node型とRelation端点が許可された組合せである
- seed対象に`RelationAssertion / ClassificationRun`が混在しない
- seed対象Relationが`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`だけである
- 全Node・RelationのsnapshotとGraph schema versionが一致する
- 全Nodeに`contentHash`、全Relationに`sourceContentUnitId`がある
- `REFERENCES`に参照文字列がある

監査成功後、検証環境をメンテナンス状態にしてOpenSearchとNeo4jを同じseedで再構築する。
Neo4jだけを再seedする経路は設けない。

## 6. 非同期分類との境界

非同期jobはpublish済みseedの`sourceSnapshotId`を固定し、原文`REFERENCES`と両端Article全文から
分類候補を作る。原文上の参照元・参照先と意味上のSUBJECT・OBJECTを区別する。LLMは1 predicateずつ
固有の二必要条件を判定し、成立predicateだけ別の根拠付与呼出しでSUBJECT・OBJECT、参照箇所、両端spanを
選ぶ。Programは条件整合と既知IDを構造検証し、
`building`の`ClassificationRun`へ保存する。プログラムはpredicateを推測・補正しない。

Assertionを作らない`reference_only / uncertain / failed`を含め、各候補の完了結果を
`ClassificationCheckpoint`へ保存する。これにより中断再開時に処理済み候補を再度LLMへ送らない。
全候補の処理と監査が完了したRunだけを`published`にする。旧snapshotの分類結果を新snapshotへ
流用せず、Graph schema、抽出規則、法令・ガイド入力の変更時は両ストアの再seed後に再分類する。
