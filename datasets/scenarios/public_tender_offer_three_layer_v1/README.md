# 公開買付け3階層ミニデータセット

## 目的

金融商品取引法、同法施行令、公開買付府令をまたぐ検索を、小さな固定snapshotで検証する。
少人数私募の告知根拠をたどる回帰経路も含め、架空条文は作らず、保存済みe-Gov XMLから
本則15条文をArticle単位で選ぶ。

```text
金商法27条の2 ── IMPLEMENTS ──→ 施行令7条
金商法27条の2 ← EXCEPTION_TO ── 施行令7条
施行令7条     ── IMPLEMENTS ──→ 公開買付府令2条の5
公開買付府令2条の5 ── USES_DEFINITION ──→ 金商法27条の2
金商法27条の3 ── IMPLEMENTS ──→ 公開買付府令10条
開示府令14条の15 ── REFERENCES ──→ 金商法23条の13
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
| 金融商品取引法 | 23条の13 | 少人数私募の告知・書面交付 |
| 金融商品取引法 | 27条の4 | 比較候補 |
| 施行令 | 6条、7条、8条、9条の3 | 対象、例外、期間、公告の周辺規定 |
| 公開買付府令 | 2条の5、10条 | Graphの核心 |
| 公開買付府令 | 2条の4、2条の6、9条、11条 | 隣接・周辺の比較候補 |
| 開示府令 | 14条の15 | 少人数私募の告知内容と金商法23条の13への明示参照 |

比較候補は固定的な不正解ではない。質問とHypothesisに応じて採否が変わる通常のArticleとして登録する。

全体問題のgoldは、質問が明示する各観点を直接支える6 Articleを要求する。金商法27条の2だけでなく、
対象範囲を定める施行令6条、適用除外を定める施行令7条と府令2条の5、公告・届出を定める
金商法27条の3と府令10条をEvidenceとして確認する。検索候補や別Articleで代用しない。
1 Cycleの本文取得上限4 Articleを超えるため、この問題は複数Cycleの引継ぎも検証する。

### 自治体法務の検索構造を代替する追加設問

自治体例規を入手するまでの間、同じ15 Articleだけを使い、自治体法務で必要になる検索構造を
抽象的に検証する。既存の公告・例外・総合3問はbaselineとして維持し、次の6問を追加する。

| question ID | 抽象化した検索 | 主な対象Article |
|---|---|---|
| `tob-delegation-chain` | 上位規範から下位規範への委任・具体化 | 金商法27条の2、施行令7条、府令2条の5 |
| `tob-definition-use` | 下位規範で使われる用語から定義元をたどる | 府令2条の5、金商法27条の2、施行令7条 |
| `tob-permitted-choice` | 選択可能な範囲と、選択に伴う拘束条件 | 金商法27条の3、施行令9条の3、府令9条・10条 |
| `tob-policy-compliance` | 独自方針と上位規範の整合 | 金商法27条の3、施行令9条の3、府令9条・10条 |
| `tob-amendment-impact` | 上位規範の改正から下位規範を逆引き | 金商法27条の3、施行令9条の3、府令9条・10条 |
| `tob-internal-procedure` | 上位規範から内部手順を組み立てる | 金商法27条の3、施行令9条の3、府令9条・10条・11条 |

`tob-definition-use`は定義語そのものを質問に書かず、府令2条の5を起点に
`USES_DEFINITION`をたどって金商法27条の2の定義へ到達する経路を検証対象とする。
実モデルは定義語を補って定義元を直接検索できるため、Graph経路自体の隔離テストでは
初期検索候補を府令2条の5だけに固定し、回答の正否とは別にGraph要求を確認する。

`tob-amendment-impact`が求めるのは見直し候補と関係の説明であり、仮定した改正内容がないまま
改正要否を断定することではない。追加設問もgoldを検索、Graph、Solver Promptへ投入しない。

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
