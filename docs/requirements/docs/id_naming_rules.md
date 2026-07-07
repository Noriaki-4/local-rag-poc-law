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
law-322AC0000000067        # 地方自治法
law-323AC0000000025        # 金融商品取引法
```

### マニュアル

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
law-322AC0000000067-article-16
law-323AC0000000025-article-2
law-323AC0000000025-article-2-paragraph-1
```

マニュアル:

```text
manual-ordinance-001-step-008
```

MVPでは、Graph nodeの`graphNodeId`は対応する`contentUnitId`と同一でよい。文書ノードは`documentId`と同一にする。

## 4. edge ID

```text
edge-<from短縮>-<edgeType小文字>-<to短縮>
```

例:

```text
edge-manual-ordinance-001-step-008-based-on-law-law-322AC0000000067-article-16
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

地方自治法第16条は以下で統一する。

```text
documentId:    law-322AC0000000067
contentUnitId: law-322AC0000000067-article-16
graphNodeId:   law-322AC0000000067-article-16
```

`local-autonomy-law-article-16` のような英語名ベースIDは使用しない。
