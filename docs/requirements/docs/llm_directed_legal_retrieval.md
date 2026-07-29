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

法的役割の固定リスト、論点数、法令レイヤーごとの探索順序は、LLMの必須手順として
プロンプトへ埋め込まない。

## 3. 1ターンの契約

LLMは次のいずれかを返す。

- `continue`: 追加のツール操作が必要
- `ready`: 提示済み証拠で回答可能
- `insufficient`: 調査しても根拠が不足

使用できる操作:

- `search_corpus`: 投入済み法令・ガイドを検索
- `fetch_articles`: 既知のArticle IDから本文を取得
- `expand_graph`: 既知のArticle IDから確認済み関係を取得

OpenSearchで確認できた文書IDと題名もLLMへ提示する。対象法令を特定できる場合、
`search_corpus.documentIds`でその法令内だけを検索できる。文書ID自体をLLMに推測させず、
一覧に無いIDはコードで拒否する。

`ready`または`insufficient`の`selectedEvidence`に指定できるのは、そのターンまでにLLMへ提示した
`contentUnitId`だけである。LLMの出力がJSON schemaを満たしても、IDの出所と状態整合性は
`validate_research_turn()`で別途検証する。

最終ターンは収束専用とし、追加操作と`continue`を許可しない。LLMは確認済み本文から
`ready`または`insufficient`を選び、限定回答に使える証拠も`selectedEvidence`へ残す。
時間切れや形式不正で最終判断を完了できない場合も、直前の有効な証拠選択は破棄せず、
`partial`として回答生成へ渡し、未確認事項を明示させる。直前ターンでArticle本文を
直接取得した場合は、その本文も失わずに限定回答の候補へ加える。
最終ターンの入力では、直前に直接取得した本文、直前に選択した証拠の順で先頭へ置く。
候補一覧の重複提示をやめ、操作履歴から検索語・action詳細を除いた要約だけを残し、
本文を最大12,000文字・32件へ抑える。`selectedEvidence`は最大16件で、回答に必要な
最小限の本文を選ばせる。これらは3ターン構成を変えず、ターン間の情報欠落と
最終判断のタイムアウトを減らすための制御である。

十分性は、質問が直接求める中心的な結論を取得済み法令で説明できるかで判断する。
考え得る全例外や周辺制度の網羅を完了条件にはしない。未確認事項が中心的な結論を変えない
場合は、回答上の留保として示したうえで`ready`にできる。

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
  - 複数Articleの直接取得は、要求Article数×検索上限件数のchunk枠を確保する
  - Article直接取得時は、Graph schemaで実装済みかつconfidence・生成元・委任根拠を
    検証できる関係だけを1hop自動取得し、関係先Article IDを次ターンへ提示する
  - 自動Graph取得は5秒で打ち切り、失敗してもArticle本文の取得結果は破棄しない
  - clearance、件数、Graph深度をコード側で制限
- `app/llm_research_loop.py`
  - `判断 → ツール実行 → 証拠登録`の反復
  - ターン数、ツール回数、時間の共有予算
  - 最終ターンを証拠選択と回答可否の確定に予約
  - 直前に直接取得した本文を次ターンの証拠表示で最優先する
  - 上限到達時も最後に検証できた証拠選択と直前取得本文を保持
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
