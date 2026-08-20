# 公開買付け3階層ミニデータセット

## 目的

金融商品取引法、同法施行令、公開買付府令をまたぐ検索を、小さな固定snapshotで検証する。
架空条文は作らず、保存済みe-Gov XMLから本則13条文をArticle単位で選ぶ。

```text
金商法27条の2 ── IMPLEMENTS ──→ 施行令7条
金商法27条の2 ← EXCEPTION_TO ── 施行令7条
施行令7条     ── IMPLEMENTS ──→ 公開買付府令2条の5
金商法27条の3 ── IMPLEMENTS ──→ 公開買付府令10条
```

1回のGraph検索は1ホップとし、Solverが選択した施行令7条を次の検索起点にできることを検証する。
金商法27条の2と施行令7条の間では、委任具体化と例外という二つの意味関係を独立に保持する。
検索時に複数predicateから同じArticleへ到達しても、本文取得はArticle IDで一度にまとめ、到達理由だけを残す。

## 検索入力とgoldの分離

検索・seedが読んでよいファイルは次だけである。

- `manifest.json`
- `article_allowlist.json`
- manifestが参照する保存済みe-Gov XML

`eval/`は採点専用であり、OpenSearch、Neo4j、Solver Promptへ投入しない。
`navigation_expectations.jsonl`は検索で最低限必要なpredicateだけを定める。5 predicateの完全な
意味分類結果やimport可能な`RelationAssertionRecord`ではない。実際の意味登録では、label-free候補を
Worker／Reviewerへ渡し、通常のClassificationRun契約で登録する。

## 対象Article

| 法令 | Article | 用途 |
|---|---|---|
| 金融商品取引法 | 27条の2、27条の3 | 検索・Graphの核心 |
| 金融商品取引法 | 27条の4 | 比較候補 |
| 施行令 | 6条、7条、8条、9条の3 | 対象、例外、期間、公告の周辺規定 |
| 公開買付府令 | 2条の5、10条 | Graphの核心 |
| 公開買付府令 | 2条の4、2条の6、9条、11条 | 隣接・周辺の比較候補 |

比較候補は固定的な不正解ではない。質問とHypothesisに応じて採否が変わる通常のArticleとして登録する。

全体問題のgoldは、質問が明示する各観点を直接支える6 Articleを要求する。金商法27条の2だけでなく、
対象範囲を定める施行令6条、適用除外を定める施行令7条と府令2条の5、公告・届出を定める
金商法27条の3と府令10条をEvidenceとして確認する。検索候補や別Articleで代用しない。
1 Cycleの本文取得上限4 Articleを超えるため、この問題は複数Cycleの引継ぎも検証する。

## snapshot

元XMLを複製せず、content-addressed pathとSHA-256を`manifest.json`で固定する。subsetの
`datasetSnapshotId`は、親snapshot、3つのXML hash、Article allowlistから計算する。
法令本文を更新する場合は元XMLを上書きせず、新しい親snapshotとsubset snapshotを作る。

対象外Articleへの参照は、誤ったArticleへ補正せず`outside_dataset_scope`として監査する。
Neo4jには両端Articleがデータセット内に存在するRelationだけを登録し、dangling edgeを作らない。

## 検証

```bash
python3 scripts/validate_public_tender_offer_mini_dataset.py
```

検証はネットワーク、LLM、OpenSearch、Neo4jを使用しない。保存済みXMLのhash、本則Articleの一意性、
Article全文、期待参照文言、Graph期待方向、gold分離、snapshot再現性を確認する。
