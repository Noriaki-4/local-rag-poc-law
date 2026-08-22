# 法令向け論点被覆型根拠検索設計

## 1. 文書の位置づけ

本書は、自然言語の法令質問に対し、質問文との類似度だけで条文を選ぶのではなく、
質問を構成する論点ごとに必要な法令根拠をLLMの回答コンテキストへ残すための設計を定める。

本書の方式はfeature flag付きで実装されている。現行のHybrid検索、Graph展開、再ランカー、
最終引用処理は維持しながら、再ランカー入力30件から回答コンテキスト16件を選ぶ段階を
Shadow比較または新方式へ切り替える。

法的判断の正しさをシステムだけで保証するものではない。回答は検索された法令本文に基づく
参考情報として扱い、具体的な事案では必要に応じて専門家による確認を求める。

## 2. 背景

通常のRAGは、質問文と意味・語句が似た文書を上位へ配置する。法令検索では、類似度と
法的な根拠性が一致しないことがある。

- 質問と直接根拠条文の表現が異なる。
- 「50名未満」と「50名以上」のように、質問と条文が反対方向から同じ境界を表す。
- 結論に必要な規定が法律、政令、府令へ分散する。
- 定義、委任、準用、例外、適用除外を複数条文から組み立てる必要がある。
- 長い条文が質問中の多くの語と一致し、短い直接根拠を押し出す。
- 条文を1件ずつ独立評価すると、複数条文で成立する根拠全体を評価できない。

確認済みの失敗例では、少人数私募の50名基準を定める金融商品取引法施行令第一条の五は、
候補プールと再ランカー入力30件には存在したが、質問全文による再ランクで18位となった。
現行の回答コンテキスト上限16件から外れたため、回答LLMにも最終引用処理にも渡らなかった。

したがって、この事例の変更対象は最終引用5件の選択ではなく、その前段にある
**再ランカー入力30件から回答コンテキスト16件への絞り込み**である。

## 3. 目的

1. 質問全文との類似度だけでなく、分解された各論点の根拠候補を16件へ残す。
2. 16件の少なくとも半分は質問全文による再ランカー順位から採用する。
3. 明示条文や論点候補が無制限に回答コンテキストを占有しないようにする。
4. Graphで取得した条文も、接続元の論点との関連性を再評価する。
5. 再ランカー障害や時間不足時は、現行経路へ安全にフォールバックする。
6. 旧方式と新方式を同一候補・同一再ランク結果から決定的に比較する。
7. 評価用goldを検索、再ランキング、回答生成へ渡さない。

## 4. 今回の対象外

初期移行では、次の変更を行わない。

- Anthropicなど外部LLMの呼び出し回数追加
- OpenSearchやNeo4jのツール呼び出し回数追加
- 選択式問題の最終引用上限変更
- 問題、法令名、条番号ごとの固定ルール
- 複数指標を重み付けした目的関数
- 条文の長さに対するペナルティ
- 未実装の `DEFINES` / `EXCEPTION_TO` エッジを前提とした展開
- Graph関係種別による直接根拠・補助根拠の機械的な分類
- cross-encoderによる法的含意・矛盾の確定
- Evidence Evaluatorの即時廃止
- planner出力スキーマへの正式な `issues` 追加

BM25、ベクトル検索、RRF、Graph検索、ローカルcross-encoderは候補生成・順位付けの部品として
維持する。

## 5. 用語と単位

### 5.1 論点

初期実装では、質問全文を除くplannerの分解クエリを論点として扱う。文字列の重複を除去した
planner出力順の先頭4件を採用する。plannerが失敗した場合は、既存のルールベース分解結果に
同じ規則を適用する。

### 5.2 chunk

OpenSearchとLLMコンテキストへ渡す `contentUnitId` 単位。項・号が別々に投入されている場合、
同じArticleでも複数chunkになる。

### 5.3 Article

`articleContentUnitId` 単位。論点を何件被覆したか、同じ条文が重複していないかを判定する
単位として使う。

### 5.4 枠の消費と論点被覆

- 回答コンテキスト16件の枠消費は**chunk単位**で数える。
- 論点被覆は**Article単位**で数える。

同じArticleの第一項、第三項、第四項を個別に保護する場合、回答コンテキストは3枠消費する。
一方、論点被覆では同じArticleとして扱う。1つのArticleが複数論点を支える場合は、
そのArticleで複数論点を被覆できる。

## 6. 処理フロー

```text
自然言語の質問
  ↓
plannerによる分解クエリ生成（最大4論点を採用）
  ↓
質問全文・分解クエリによる既存Hybrid検索
  ↓
初回の論点別ローカル再ランク
  ↓
明示条番号の直接取得・ガイドライン対応法令取得
  ↓
必要時のGraph展開・follow-up検索
  ↓
再ランカー入力30chunksを確定
  ↓
質問全文による再ランク
  ↓
回答生成時間を予約
  ↓
30chunksを各論点で後段再ランク
  ↓
共通保護予算を使って16chunksを選択
  ↓
LLM回答生成
  ↓
現行の最終引用選択・根拠図表示
```

質問全文による再ランクは、後段の論点別再ランクより必ず先に実行する。新しい処理が既存の
全文再ランクや回答生成の時間を奪わないよう、後段論点フェーズには集約上限を設ける。

## 7. 論点別状態

論点別状態は、既存の `_mark_aspect_representatives()` がevidenceへ書く
`aspectQueries`から独立させる。この関数を将来無効化しても、Graph候補の論点継承と
新しい選抜が動作しなければならない。

想定するデータ構造:

```python
@dataclass
class AspectEvidence:
    query: str
    searched_content_ids: list[str]
    ordered_content_ids: list[str]
    scores: dict[str, float]
    inherited_content_ids: set[str]


@dataclass
class AspectEvidenceMatrix:
    aspects: list[AspectEvidence]
```

`_rerank_aspect_queries()` は、現在返している論点内の順序に加えて、cross-encoderの
`contentUnitId`別スコアを保持する。異なる論点間で生スコアを直接比較せず、初期実装の
選抜には論点内順位を使う。

## 8. 初回の論点別再ランク

既存の初回パスは維持する。

目的:

- 各分解クエリの検索上位をその論点で評価する。
- 全文RRFだけでは埋もれる論点候補を、再ランカー入力30件へ届ける。
- Graph展開の起点となる候補を作る。

対象は最大4論点、各クエリの検索順位20位以内とする。この段階の情報は30件を作るために
使うが、16件を選ぶ正式な論点順位には後段再ランクの結果を使う。

## 9. Graph候補への論点継承

Graph候補には検索結果としての `queryRanks` がない。Graphで取得した候補へ架空の
検索順位を書き込まず、継承した論点を別に保持する。

```python
inherited_aspects_by_content_id: dict[str, set[str]]
```

継承手順:

1. Graph経路の起点Articleを特定する。
2. 起点Articleが属する論点を論点別状態から取得する。
3. Graph接続先へ論点を継承する。
4. Graph接続先を後段の論点別再ランク対象へ追加する。

後段再ランクの候補条件は次のOR条件とする。

```text
queryRanks[query] <= 20
または
Graph経由で当該queryを継承した
```

初期移行で使うGraph関係は、現行実装に存在するものへ限定する。

- `REFERENCES`
- `IMPLEMENTS`
- `APPLIED_BY`
- `HAS_CONTENT_UNIT`
- `EXPLAINS`

Graph関係種別はリンクの性質であり、その論点に対する根拠の直接性を表すものではない。
例えば、`IMPLEMENTS` で到達した政令・府令が、その論点の直接根拠になる場合がある。

## 10. 30件確定後の論点別再ランク

Graph、明示条番号、ガイドライン対応法令、follow-upを含む `rerank_candidates` 30chunksを
確定し、質問全文による再ランクが成功した後に実施する。

```text
最大30chunks × 最大4論点
```

この後段結果を、16件選抜の正式な論点別順位とする。ローカル再ランカー呼び出しは増えるが、
LLMと検索ツールの呼び出し回数は増やさない。

## 11. 30件から16件の選抜

想定インターフェース:

```python
select_issue_covered_context(
    globally_reranked,
    aspect_matrix,
    top_k=16,
    max_aspects=4,
    protected_chunk_limit=8,
    explicit_chunk_limit=4,
    rounds=2,
)
```

### 11.1 不変条件

```text
回答コンテキスト:        16chunks
共通保護枠:              最大8chunks
  ├─ 明示条文の保護:     最大4chunks
  └─ 論点候補の保護:     共通保護枠の残り
全文再ランク順から追加:  最低8chunks
```

共通保護枠はchunk単位で数える。明示条文と論点候補が同じchunkなら1枠として数える。
同じArticleの異なる項・号を別々に保護する場合は、それぞれ1枠を消費する。

### 11.2 選抜順序

1. 元の質問・選択肢で明示された条・項・号を、最大4chunks保護する。
2. 各論点の最上位法令Articleから代表chunkを1件ずつ選ぶ。
3. 各論点の2番目の法令Articleから代表chunkを1件ずつ選ぶ。
4. 共通保護枠が8chunksに達したら論点保護を終了する。
5. 残りを質問全文による再ランク順で埋める。
6. 16chunksに達したら終了する。

明示条文の候補が4chunksを超える場合、全文再ランク順の上位4chunksだけを明示保護枠へ
入れる。超過候補は削除せず、論点候補としても通常どおり評価する。論点上位なら共通保護枠の
残りから採用でき、それ以外でも全文再ランク順から採用できる。条文を明示したことによって、
その候補が論点選抜から不利になってはならない。

### 11.3 重複論点

複数論点の最上位が同じArticleの場合、そのArticleで複数論点の1巡目を被覆したと扱う。
重複した論点のために1巡目の追加枠を消費せず、その論点の2番目のArticleを1巡目から
昇格させない。2番目は2巡目で初めて検討する。

### 11.4 ガイドライン

- ガイドラインchunkは論点の法令根拠枠を充足したことにしない。
- `EXPLAINS`で取得した法令本文は法令根拠として扱う。
- ガイドライン自体は、全文再ランク順の枠から採用できる。

### 11.5 Article内の代表chunk

論点の代表としてLLMへ渡すchunkは次の順で選ぶ。

1. 質問で明示された項・号の完全一致chunk
2. 論点別cross-encoderで最上位のchunk
3. 質問全文の再ランカーで最上位のchunk
4. 同順位なら既存の `_text_coverage` が高いchunk

同じArticleの別の項・号は候補プールから削除しない。必要な別chunkは、共通保護枠または
全文再ランク順から16件へ入ることができる。

## 12. 明示条文の出所

新しい出所分類は追加せず、既存の `introducedBy` を使う。

| `introducedBy` | 取得元 | 選抜時の扱い |
|---|---|---|
| `article_reference` | 元の質問・選択肢 | 明示保護の対象。最大4chunks |
| `follow_up_article_reference` | Evidence Evaluatorの追加検索 | 対応論点内で評価。無条件保護しない |

plannerの分解クエリは、現行実装では条番号直接取得へ渡されていない。

## 13. 時間予算

### 13.1 独立した回答生成予約

`LLM_TIMEOUT_SEC` は1回のLLM呼び出しを待つ上限であり、回答生成の期待所要時間ではない。
後段論点フェーズの判定には流用しない。

独立設定を追加する。

```text
AGENT_ANSWER_RESERVE_SEC=60
```

これは回答生成の完了を保証する時間ではなく、後段再ランクが侵食してはいけない最低予約時間
である。

### 13.2 後段論点フェーズの集約上限

```python
available_for_aspects = (
    deadline
    - perf_counter()
    - settings.agent_answer_reserve_sec
)

aspect_phase_budget = min(
    settings.rerank_timeout_sec,
    max(0, available_for_aspects),
)
```

`RERANK_TIMEOUT_SEC=30` の場合、論点ごとに最大30秒ではなく、後段論点フェーズ全体で
最大30秒とする。各論点のtimeoutは、フェーズの残り時間以下に制限する。

最初の論点で再ランカーが上限まで停止した場合、残りの論点はスキップし、回答生成用の予約を
消費しない。

## 14. 障害・時間切れ時のフォールバック

### 14.1 質問全文の再ランカーが失敗

新しい論点被覆選抜を使用せず、現行の `fusion_ranked` へ戻る。

### 14.2 一部の論点別再ランカーが失敗または時間切れ

失敗・未実行の論点には強制保護枠を作らない。成功した論点と質問全文の再ランク順で
16件を構成する。

### 14.3 全論点が失敗または時間切れ

新しい選抜を使用せず、現行の16件を使用する。

BM25、ベクトル、RRF順位をcross-encoderの代用として論点保護へ強制利用しない。

## 15. 最終引用

16件以降の処理は初期移行では変更しない。

- 選択式は既定の引用上限を厳守する。
- 自由記述は回答本文が使用した候補IDを回収できる。
- `GRAPH_CITATION_CLOSURE_MAX=1` を維持する。
- 法律、政令、府令の接続図を維持する。

まず必要条文を16件の回答コンテキストへ残し、その後に別の引用欠落があるかを測定する。

## 16. Shadow mode

feature flag:

```text
AGENT_ISSUE_COVERAGE_SELECTION=false
AGENT_ISSUE_COVERAGE_SHADOW=true
```

Shadow modeでは、現行方式の16件をLLMへ渡し、新方式の16件は比較用にだけ計算する。
旧16件と新16件は、同一の30件・同一の質問全文再ランク結果から選ばれるため、差は
30件から16件を選ぶ方式だけになる。

### 16.1 Shadowで比較できるもの

- 旧・新16件の必要条文完全到達率
- 旧・新16件の条文再現率
- 論点別被覆率
- 30件の壁による欠落数
- 旧方式から改善・悪化した問題

### 16.2 Shadowで比較できないもの

- 最終引用到達率
- 回答正答率
- 回答要点到達率

これらは新方式を実際に有効化し、新16件をLLMへ渡す第2段階で測定する。

一部論点の再ランクが失敗・時間切れとなった行は、完全な旧新比較へ混ぜず、別件数として
集計する。

## 17. Trace設計

Agent APIのtraceへ少なくとも次を追加する。

```json
{
  "selectedAspectQueries": [],
  "ignoredAspectQueries": [
    {
      "query": "...",
      "reason": "max_aspects"
    }
  ],
  "aspectEvidenceMatrix": {},
  "graphInheritedAspects": {},
  "rerankCandidateContentUnitIds": [],
  "bestAspectCandidateIdsBefore30": {},
  "bestAspectCandidateMissingFrom30": [],
  "graphInheritedCandidateMissingFrom30": [],
  "oldContextContentUnitIds": [],
  "newContextContentUnitIds": [],
  "explicitReferenceCandidateCount": 0,
  "explicitProtectedChunkCount": 0,
  "aspectProtectedChunkCount": 0,
  "protectedChunkCount": 0,
  "globalRankChunkCount": 0,
  "coveredArticleCount": 0,
  "answerReserveMs": 60000,
  "availableForAspectPhaseMs": 0,
  "aspectPhaseBudgetMs": 0,
  "aspectPhaseElapsedMs": 0,
  "skippedAspectQueries": [],
  "selectorFallbackReason": null
}
```

このtraceにより、失敗地点を次の5段階へ分ける。

1. 候補プールにない。
2. 候補プールにはあるが、再ランカー入力30件にない。
3. 30件にはあるが、回答コンテキスト16件にない。
4. 16件にはあるが、回答LLMが使用しない。
5. 回答LLMは使用したが、最終引用に残らない。

## 18. 評価設計

### 18.1 metricVersion 5

現行eval-runnerはAgent APIのtrace全体を評価JSONLへ保存しない。Shadow比較を永続化するため、
`eval-runner/run_eval.py` を変更し、metricVersionを5へ更新する。

新16件に対しても既存の条文到達計算と同等の処理を行い、次を保存する。

```json
{
  "metricVersion": 5,
  "scores": {
    "rerankerArticleCompleteHit": 0,
    "rerankerArticleRecall": 0.75,
    "shadowRerankerArticleCompleteHit": 1,
    "shadowRerankerArticleRecall": 1.0
  },
  "articleCoverage": {
    "expected": 4,
    "rerankerMatched": 3,
    "shadowRerankerMatched": 4
  },
  "shadowSelection": {
    "complete": true,
    "skippedAspectCount": 0
  }
}
```

gold条文はeval-runner内の採点だけで使い、Agent APIへ送らない。

### 18.2 既知問題

Shadow指標は次の2系統を出す。

- 全問
- `diagnosticScorable=true` の問題だけ

`temporal_version_mismatch`、`suspected_gold_error` などの既知問題は全問集計には残すが、
診断用集計から除外する。

### 18.3 20問の事前確認

140問の前に20問を実行し、時間予算と記録方式を確認する。

```bash
EVAL_LIMIT=20 \
REQUEST_TIMEOUT_SEC=360 \
AGENT_ANSWER_RESERVE_SEC=60 \
...
```

確認項目:

- `aspectPhaseBudgetMs`
- `aspectPhaseElapsedMs`
- `skippedAspectQueries`
- Shadow選抜完了件数
- `request_failed`
- 全体レイテンシ
- 回答LLMの実測レイテンシ

後段フェーズが恒常的にスキップされる場合は140問へ進まず、wall time、回答予約、
フェーズ集約上限の関係を見直す。

### 18.4 第1段階: Shadow 140問

同一実行内で旧16件と新16件を比較する。

主な合格条件:

- 新方式の必要条文完全到達数が旧方式以上
- 新方式の条文ミクロ再現率が旧方式以上
- 悪化問題を個別に確認できる
- 候補プールと再ランカー入力30件までの結果が変わっていない
- Shadow比較が完了しなかった行数を別に報告する
- 問題・法令・条番号固有のルールを追加していない

### 18.5 第2段階: 新方式を有効化した140問

新16件を実際にLLMへ渡し、次を測定する。

- 正答率
- `citationArticleCompleteHit`
- `citationArticleRecall`
- 回答形式エラー
- LLM使用率
- レイテンシ
- ローカル再ランカー障害率

### 18.6 自然言語例題

既存の自然言語例題は回帰確認に使い、パラメータ調整には使わない。検索とLLMに揺らぎが
ある問題は最低3回実行する。

少人数私募では次を段階別に確認する。

- 施行令第一条の五が候補プールに存在する。
- 再ランカー入力30件に存在する。
- 新方式の16件に残る。
- LLMへ渡される。
- 最終回答で直接根拠として引用される。
- 他の必要条文を押し出さない。

## 19. テスト方針

各工程をテスト先行で実装する。最低限、次を固定する。

### 19.1 論点別状態

- `_mark_aspect_representatives()`を無効化しても論点別状態が作られる。
- `_mark_aspect_representatives()`を無効化してもGraph候補が論点を継承する。
- planner出力が5件以上でも先頭4件だけを決定的に採用する。

### 19.2 Graph継承

- `queryRanks`がないGraph候補も継承論点によって後段再ランクへ参加する。
- Graph関係が `IMPLEMENTS` であることを理由に候補を降格しない。
- 高信頼Graph候補と一般Graph候補の30件欠落を区別して記録する。

### 19.3 共通保護予算

- 保護枠はchunk数で8件を超えない。
- 明示条文の保護は4chunksを超えない。
- 同じArticleの3項を保護すると3枠消費する。
- 同じArticleの複数chunkを論点被覆で重複計上しない。
- 明示条文と論点候補の同じchunkを二重計上しない。
- 全文再ランク順から最低8chunksを追加する。
- 全文順位18位の論点候補を、共通保護予算内なら16件へ残す。
- 明示保護上限から漏れた候補でも、論点上位なら論点枠へ入れる。
- 重複論点が1巡目の枠を浪費しない。
- ガイドラインだけで法令根拠枠を充足しない。

### 19.4 順序・障害・時間

- 質問全文の再ランクを後段論点再ランクより先に実行する。
- 質問全文の再ランカー失敗時は現行経路へ戻る。
- 一部論点の失敗・時間切れでは、その論点の強制枠を作らない。
- 全論点の失敗・時間切れでは現行16件を使用する。
- 後段論点フェーズ全体が集約上限を超えない。
- 後段処理が回答生成予約へ侵入しない。
- `LLM_TIMEOUT_SEC`を変更しても回答予約時間が変わらない。

### 19.5 Shadow・評価

- Feature flag無効時に現行結果が変わらない。
- 同一30件・同一全文再ランク結果から旧新16件を決定的に比較する。
- Shadow指標がmetricVersion 5のJSONLへ保存される。
- Shadowのマクロ・ミクロ指標を集計できる。
- 全問と `diagnosticScorable=true` を分けて集計できる。
- 選択式の最終引用上限が変わらない。
- `GRAPH_CITATION_CLOSURE_MAX=1` が維持される。

## 20. 実装順序

各項目でテストを先に追加し、そのテストを満たす最小実装を行う。

1. 論点別状態を旧代表確保処理から独立させる。
2. 初回論点別順位・スコアを保持する。
3. Graph論点継承を別管理する。
4. 共通保護予算と明示条文超過時の扱いを実装する。
5. 質問全文再ランク後の論点別再評価を実装する。
6. 独立した回答予約とフェーズ集約上限を実装する。
7. 障害・時間切れ時のフォールバックを実装する。
8. Shadowの新16件選抜とtraceを実装する。
9. eval-runnerをmetricVersion 5へ更新する。
10. 20問の事前確認を行う。
11. Shadow modeで140問を評価する。
12. 改善確認後、新方式を有効化して140問を評価する。
13. 自然言語例題を複数回実行する。
14. 旧ヒューリスティックをfeature flag単位で一つずつ撤去する。

## 21. 旧ヒューリスティックの撤去

新方式が安定した後、次の順で一つずつ撤去する。

1. `FINAL_LAW_DIVERSITY_*`
2. `FINAL_RERANK_MAX_ADDITIONS` のaspect救済部分
3. `_mark_aspect_representatives()` による旧代表確保

撤去前に、論点別状態とGraph継承が旧代表確保から完全に独立していることをテストする。

次は維持する。

- 元の質問・選択肢で明示された条番号の直接取得
- 高信頼Graph関係
- `GRAPH_CITATION_CLOSURE_MAX=1`
- 選択式の引用上限
- 再ランカー障害時の現行フォールバック

## 22. 将来の別ワーク

初期移行の評価後、必要性を実測してから次を検討する。

- planner出力への正式な `issues` 追加
- 結論と根拠を対応付ける構造化出力
- 回答命題と引用条文の法的含意・矛盾検証
- `DEFINES` / `EXCEPTION_TO` の実装、検証、再シード
- Evidence Evaluatorの廃止または置換
- 自由記述の動的コンテキスト上限
- 法令時点を考慮した検索・評価

## 23. 実装配置案

責務を `agent.py` へ集中させないため、論点別状態と選抜ロジックは独立モジュールへ置く。

```text
agent-api/app/evidence_selector.py
agent-api/tests/test_evidence_selector.py
```

既存ファイルの主な変更対象:

- `agent-api/app/agent.py`
- `agent-api/app/config.py`
- `eval-runner/run_eval.py`
- `.env.example`
- `docker-compose.yml`
- `RUNBOOK.md`
- `docs/evaluation_design.md`

## 24. 実装・検証状況

2026-07-27時点で、実装順序1〜9を完了している。既定はShadow modeであり、新方式の
16件を回答へ渡す切替はまだ有効にしていない。

- Agent API、UI、eval-runnerの単体・回帰テストを実施済み。
- Agent APIとローカル再ランカーを再buildし、各依存先を含むhealth checkを確認済み。
- 登録済み自然言語例題12問を各1回、高難度8問を追加で各1回、合計20実行した。
- 厳格な全項目到達は9/20、想定資料は46/46、最終引用の必要条文は51/61、
  回答要点は58/64へ到達した。HTTP失敗とLLM未使用は0件だった。
- 必要条文は候補プール60/61、再ランカー入力30件55/61、旧16件51/61、
  新16件52/61だった。新方式の完全到達は旧12/20から新13/20へ1件改善した。
- Shadow比較は19/20件で完了した。後段論点フェーズは中央値約9.8秒、最大約16.9秒で、
  30秒の集約上限内だった。

lawqa_jp選択式20問の事前確認、Shadow 140問、新方式を有効にした140問は未実施である。
これらは外部LLMのクレジットと実行時間を使うため、段階ごとに結果を確認してから次へ進む。
