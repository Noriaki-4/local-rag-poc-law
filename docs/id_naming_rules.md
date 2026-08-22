# ID命名規則と参照整合ルール

## 1. 基本方針

実装者がサンプルをそのまま雛形にできるよう、IDは機械的に生成できる形式に統一する。

## 2. documentId

### 法令

```text
law-<法令番号>
```

例:

```text
law-323AC0000000025        # 金融商品取引法
```

### マニュアル

原本保管用マニュアルは、RAG投入やGraph投入を行わない場合でも、原本ファイルの識別名として以下を使える。

```text
manual-<domain>-<連番>
```

例:

```text
manual-ordinance-001
```

## 3. contentUnitId / graphNodeId

法令の条:

```text
law-<法令番号>-article-<条番号>
```

法令の項:

```text
law-<法令番号>-article-<条番号>-paragraph-<項番号>
```

法令の号:

```text
law-<法令番号>-article-<条番号>-paragraph-<項番号>-item-<号番号>
```

例:

```text
law-323AC0000000025-article-2
law-323AC0000000025-article-2-paragraph-1
```

### 3.1 附則（Supplementary Provisions）

本則と附則は条番号を別々に振り直すため（本則第8条と附則第8条が同居する）、同じ `article-<条番号>` に集約すると衝突し、後勝ちで本則が消える。附則は本則と別の名前空間に分離する。

```text
law-<法令番号>-suppl-<附則index>-article-<条番号>
law-<法令番号>-suppl-<附則index>-article-<条番号>-paragraph-<項番号>
```

- `<附則index>` は e-Gov XML の `SupplProvision` 出現順（0始まり）。`suppl-0` が原始附則、以降は改正法附則。
- 本則は従来どおり `law-<法令番号>-article-<条番号>`（接頭辞なし）を維持する。lawqa_jp の参照や既存サンプルとの後方互換のため変更しない。
- OpenSearch文書には `provisionType`（`main` / `supplementary`）と `sectionKey`（`main` / `suppl-<index>`）を付与し、本則・附則を判別できるようにする。
- 「第N条」という参照は本則の条を指す慣行に従い、参照エッジ（`REFERENCES`）は本則の `article-<条番号>` を指す。「前条」「次条」の隣接は `sectionKey` 内で閉じる。

### 3.2 枝番（条・号の「のN」）

「第2条の12」「第二号の二」のような枝番は、アンダースコアで連結して接尾辞に保持する。int化して丸めると隣接番号と衝突するため行わない。

```text
第2条の12   -> article-2_12
第二号の二   -> ...-item-2_2
```

枝番を持つ条は、元の条の配下にある子要素ではない。法改正等で条と条の間へ規定を追加するための
独立した条であり、正式な条番号全体を使って別々の`Article` nodeとして識別する。

```text
Document
├─ Article 第2条       (article-2)
├─ Article 第2条の2    (article-2_2)
├─ ...
├─ Article 第2条の12   (article-2_12)
├─ Article 第2条の13   (article-2_13)
└─ Article 第3条       (article-3)
```

したがって、`第2条の12`と`第2条の13`を複数の「第2条」として扱わず、また
`Article 第2条`から`Article 第2条の12`への親子関係も作らない。いずれも同じDocumentに属する
同階層のArticleであり、それぞれが自身のParagraphとItemを持つ。

```text
第2条の12第1項第二号の二
-> article-2_12-paragraph-1-item-2_2
```

枝番と構造階層も混同しない。`第2条の12`は`article-2_12`、`第2条第12項`は
`article-2-paragraph-12`であり、別のContent Unitである。アンダースコアは1つの条番号・号番号の
内部にある枝番、ハイフン付きの`paragraph`・`item`は条・項・号の包含階層を表す。

MVPでは、Graph nodeの`graphNodeId`は対応する`contentUnitId`と同一でよい。文書ノードは`documentId`と同一にする。

## 4. edge ID

```text
edge-<from短縮>-<edgeType小文字>-<to短縮>
```

例:

```text
edge-law-323AC0000000025-has-content-unit-article-2
```

## 5. dangling edge禁止

`edges.jsonl` の `fromGraphNodeId` と `toGraphNodeId` は、同じGraph投入バッチ内、または既存Graph内に存在しなければならない。

Phase 1のimport前に次を検査する。

```text
all_edge.fromGraphNodeId in nodeIds
all_edge.toGraphNodeId in nodeIds
```

存在しない場合は投入エラーとし、自動補完しない。

## 6. サンプル上の確定事項

金融商品取引法第2条は以下で統一する。

```text
documentId:    law-323AC0000000025
contentUnitId: law-323AC0000000025-article-2
graphNodeId:   law-323AC0000000025-article-2
```

`financial-instruments-law-article-2` のような英語名ベースIDは使用しない。
