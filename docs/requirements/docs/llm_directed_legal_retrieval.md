# LLM主導の法令調査

## 1. 目的

検索語、探索順序、必要な法令関係、根拠の十分性といった法的意味の判断を、高性能LLMの
裁量へ寄せる。法令本文とガイドは、これまでどおりこのリポジトリが投入・管理する
OpenSearch、Neo4j、MinIOのデータだけを使用する。

既存のルール主導方式を削除せず、feature flagで切り替えられる独立経路として追加する。

## 2. 分担

LLMへ任せるもの:

- 何を調べるか
- どの検索語を使うか
- どの順序で検索・Article取得・Graph展開を行うか
- 取得済み証拠で回答できるか
- 回答に使う証拠

コードで強制するもの:

- 検索対象を投入済みデータソースへ限定する
- 利用可能なツールと呼び出し回数
- Article ID直接取得とGraph展開は、検索結果またはGraphで確認済みのIDだけ許可する
- 回答根拠はLLMへ実際に提示したcontentUnitIdだけ許可する
- 法令とガイドの種別を保持する
- 時間・候補・文字数の上限
- LLM判断とツール実行履歴をtraceへ残す

LLM事業者のクレジット・利用枠不足は`provider_quota_error`として明示し、
「投入済みデータに根拠が無い」という意味上の失敗へ変換しない。

法的役割の固定リスト、論点数、法令レイヤーごとの探索順序は、LLMの必須手順として
プロンプトへ埋め込まない。

## 3. 反復深化型の調査契約

外側は3調査サイクルとし、各サイクルの内部で次を完了する。

1. `explore`: 前回の知見を踏まえて法令・ガイドを探索する
2. `deepen`: 探索結果を読み、Article本文・Graph関係・法令内検索を掘り下げる
3. `integrate`: 原文ID付きの調査チェックポイントへ統合する

第1サイクルの結果を第2サイクルへ、第2サイクルの結果を第3サイクルへ引き継ぐ。
各サイクルは同じ3段階を繰り返し、後続サイクルでは前回の暫定結論も再検証する。
探索・掘り下げの構造化出力は、物理上限4,096トークンに対して2,500トークン以内を
目標とする。情報が多い場合は確認済み根拠IDと未確認Article IDを優先して保持し、
理由・調査経緯・重複説明を要約して、JSONを完全に閉じることを優先する。

チェックポイントは次を保持する。

- `status`: `continue | ready | insufficient`
- `conclusion`: 現在の結論だけを1〜3文
- `evidenceIds`: 結論・最終回答に使う取得済み原文ID
- `openEvidenceIds`: 本文取得済みだが結論との関係を判断継続中の原文ID
- `nextQuestions`: 回答に必要だが未確認の事項
- `nextArticleIds`: Graphまたは検索で確認済みの、次に本文を読むArticle ID（最大10件）
- `logicalStructure`: 論点、結論、根拠階層、未確認事項

`logicalStructure`は次の階層で表す。

```text
issues
└─ issue
   ├─ authorityNodes（論点内の共有根拠レジストリ）
   │  ├─ 直接根拠
   │  └─ 委任先・定義・例外・手続具体化・ガイド等
   └─ claims
      └─ authorityNodeIds（共有ノードへの参照）
```

`authorityNodes`は`nodeId / articleId / evidenceIds / verificationStatus`を持ち、
`parentNodeId / relationFromParent / purpose`で同じIssue内の親根拠との関係を表す。
各Claimは`authorityNodeIds`で共有レジストリのノードを参照する。同じArticleが複数の
結論を支えるなど法令関係はDAGになり得るため、ノードや本文をClaimごとに複製しない。
同じ論点・結論の`issueId / claimId`は後続サイクルでも維持し、関係を追加・更新する。
Graph関係だけを確認したArticleと、本文まで確認したArticleはそれぞれ
`graph_verified | text_not_fetched`と`text_verified`で区別する。
統合JSONは探索判断より構造が大きいため、`LLM_RESEARCH_INTEGRATION_MAX_TOKENS`
（既定8,192）を独立させる。Anthropicでは圧縮処理に高い推論量を使い切らないよう
`LLM_RESEARCH_INTEGRATION_EFFORT`（既定`low`）を指定する。

調査経緯、検索語、長い根拠選択理由、条文の長い説明は保持しない。チェックポイントは
法的根拠の代替ではなく、後続サイクルと回答生成では`evidenceIds`に対応する原文を
証拠カタログから再読込する。`nextArticleIds`は存在確認済みArticleだけを許可し、
Graph経路の推測や未確認IDを次サイクルへ持ち込まない。確認済みGraph経路は
起点Article・関係種別・到達先Article・法令名・条見出しを一組で保持し、Article IDだけへ
平坦化しない。

証拠カタログは取得済み本文とGraph関係をバックエンドで保持するが、次サイクルの
プロンプトへ全候補を再投入しない。自動提示する本文はチェックポイントの`evidenceIds`、
`openEvidenceIds`、未解決Articleと今回のサイクルで直接取得した本文に限定する。
一般検索の代表本文はArticle発見に使い、正常な統合入力から除外する。Graph関係は、
同一サイクルでは今回の新規関係を専用欄へ一度だけ提示し、次サイクルでは
`authorityNodes / nextArticleIds / unresolved / evidenceIds`に残ったArticle同士を結ぶ
関係だけを提示する。未採用候補は削除せず、ID検証と明示的な再取得には利用できる。

使用できる操作:

- `search_corpus`: 投入済み法令・ガイドを検索
- `fetch_articles`: 既知のArticle IDから本文を取得
- `expand_graph`: 既知のArticle IDから確認済み関係を取得

OpenSearchで確認できた文書IDと題名もLLMへ提示する。対象法令を特定できる場合、
`search_corpus.documentIds`でその法令内だけを検索できる。文書ID自体をLLMに推測させず、
一覧に無いIDはコードで拒否する。

`ready`または`insufficient`の`evidenceIds`に指定できるのは、そのターンまでにLLMへ提示した
`contentUnitId`だけである。LLMの出力がJSON schemaを満たしても、IDの出所と状態整合性は
`validate_research_checkpoint()`で別途検証する。

サイクル間で生の検索履歴や全条文本文を累積しない。全取得本文は外部の証拠カタログへ
保持し、統合時は前回選択原文を最大6,000文字、今回取得・再取得した原文を最大12,000文字だけ
再構成する。段階判断でLLMが明示選択した原文を最優先し、残りは同一Articleの長い
項号列が他のArticleを押し出さないようラウンドロビン表示する。次サイクルの探索・
掘り下げでは前回未採用候補を再表示しないが、候補自体は削除しない。
LLMが取得済みArticleを再取得した場合は、新規件数が0でも今回返した本文を優先表示する。
時間切れや形式不正の場合は、最後に検証できたチェックポイントを
`partial`として回答生成へ渡す。各サイクルには残り時間を残サイクル数で配分し、
`integrate`用の時間を先に予約する。統合が失敗した場合、探索・掘り下げ段階の
`ready`だけでチェックポイントを`ready`へ昇格させず、`continue`として扱う。
統合JSONに未確認のArticle ID、原文ID、根拠ノード参照が混じった場合は、そのIDを
推測で補正しない。不正部分だけを除外し、残った構造を再検証できた場合に限り
`continue`へ戻して次サイクルへ渡す。除外内容はtraceの`sanitization`へ記録する。
Claimは1論点最大8件、チェックポイント全体でも最大8件とし、細分化された5件目を
形式だけを理由に統合全体から捨てない。
各根拠ノードの`evidenceIds`は最大20件とし、長いArticleの11件目だけを理由に
統合JSON全体を無効化しない。
中間サイクルの統合LLMがタイムアウトしても、残りサイクルと全体時間がある場合は
直前の検証済みチェックポイントから継続する。直接取得本文があれば最大20件を、
なければ一般検索候補を文書単位で分散した最大18件を、1サイクルだけ回復入力へ残す。
形式不正を除去した結果、根拠ID・判断中ID・次回取得Article・確認済み根拠ノードが
すべて空になった場合も統合成功とは扱わず、同じ回復入力を次サイクルの段階判断と
統合判断へ再提示する。
タイムアウトは
`recoverableTimeouts / cycles[].integrationTimeout`へ記録し、最終サイクルで回復
できなかった場合だけ調査全体を`timeout | partial`で終了する。

Graphの1hop取得上限は起点Articleごとに適用する。複数起点をまとめたglobal上限で
先頭Articleだけが候補を占有しないようにし、LLMへのGraph関係提示も起点Article単位で
ラウンドロビンする。取得上限は「候補発見のためにバックエンドへ保持する数」であり、
次サイクルへそのまま渡す数ではない。同一サイクルのツール履歴からはGraph関係本文を
除き、件数だけを残して専用Graph欄との重複を防ぐ。Graphは候補発見のための確認済み関係で
あり、本文を取得するまで最終根拠へは採用しない。

十分性は、質問が直接求める中心的な結論を取得済み法令で説明できるかで判断する。
考え得る全例外や周辺制度の網羅を完了条件にはしない。未確認事項が中心的な結論を変えない
場合は、回答上の留保として示したうえで`ready`にできる。

`ready`の直前には、結論を支える法令本文を選択済みか、LLM自身が本文確認を必要と判断して
`nextArticleIds`または未確認事項へ残したArticleが未取得でないかを見直す。残り予算で
取得できる場合は`fetch_articles`を選び、取得してから採否を判断する。この確認は、
質問された全事項や考え得る全論点の完全調査を要求するものではなく、LLM自身が必要と
判断した本文の取得漏れだけを防ぐ。取得不能時は探索を無期限に続けず、中心的結論への
影響があれば`insufficient`、影響しなければ未確認事項と影響を明示して`ready`とする。

## 4. 現在の実装範囲

以下を実装した。

- `app/llm_directed_research.py`
  - 1ターンの構造化出力
  - 最小限のプロンプト
  - 証拠カタログ
  - 未取得IDの拒否
  - Ollama / Anthropic共通schema
- `LLMClient.decide_legal_research_turn()`
  - 既存の構造化JSONトランスポートを使った1ターン判断
  - モデル・token・timeout・判断内容のtrace化
- `app/llm_research_tools.py`
  - OpenSearchの共通ハイブリッド検索・Article直接取得、Neo4jの関係展開
  - 法令内検索は日本語Analyzer、BM25/vector融合、Article単位集約を使う
  - 検索時はArticleごとに代表chunkだけを提示し、全項号はLLMが`fetch_articles`で取得する
  - 全文書を横断する検索は8 Article、documentId確定後の法令内検索は30 Articleとする
  - 複数文書の候補は文書単位のラウンドロビンで提示し、最初に検索した法律だけで
    LLMへの提示上限を使い切らない
  - 複数Articleはglobal件数枠を共有せず、Articleごとに最大100chunksを取得して
    証拠カタログへ保存する
  - Article直接取得時は、Graph schemaで実装済みかつconfidence・生成元・委任根拠を
    検証できる関係だけを1hop自動取得し、起点・関係種別・到達先・見出しを次ターンへ提示する
  - 取得済みArticleの再取得でも、返却本文IDを次の判断へ優先提示する
  - 自動Graph取得は5秒で打ち切り、失敗してもArticle本文の取得結果は破棄しない
  - clearance、件数、Graph深度をコード側で制限
- `app/llm_research_loop.py`
  - 3サイクル×`explore → deepen → integrate`の反復
  - サイクルごとに操作履歴を破棄し、結論と階層的な法的論理構造を持つ
    構造化チェックポイントだけを継承
  - ターン数、ツール回数、時間の共有予算
  - 上限到達時も最後に検証できたチェックポイントを保持
  - 未取得IDを含む判断を実行せず、次ターンでの自己修正を許可
  - LLMの時間切れを`llm_timeout`、接続障害を`llm_connection_error`として区別
- `AgentService`のactive単独回答経路
  - 旧planner、Requirement生成、充足判定、固定枠選抜を迂回
  - LLMが選んだ本文をそのまま回答LLMへ渡す
  - 回答LLMには質問で実際に求められた各事項を一つずつ確認し、根拠の無い事項を
    推測しないよう指示する
  - タイムアウト・接続障害・根拠不足を旧方式で隠さない
- 環境変数と`/health`表示

`AGENT_LLM_DIRECTED_RETRIEVAL=true`では`connectedToAnswer=true`となり、新方式だけで
調査と回答を行う。`AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW=true`は回答非接続の比較用に残す。

## 5. 検証

1. activeで自然言語問題を実行し、LLMの検索判断・候補・最終選択をtraceで確認する
2. 必要条文到達、根拠選択、回答要点、時間切れを分けて評価する
3. 検索の再現率が不足する場合は検索APIを改善し、法的な採否規則をコードへ戻さない

ループ実装時も、特定の法的役割や探索順序をコードで強制する機能は追加しない。

法令内検索で日本語が1文字単位に解析される問題と、Kuromoji・N-gram・ベクトルを組み合わせる
改善案は
[日本語法令検索のAnalyzer・ハイブリッド検索改善案](japanese_legal_search_analysis_plan.md)
を参照する。
