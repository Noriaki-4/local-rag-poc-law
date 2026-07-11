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
