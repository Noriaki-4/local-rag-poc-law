# LLM主導の法令調査

> 状態: 新基盤への切替完了までは現行経路の挙動仕様として有効である。将来構造と実装順序は
> `generic_iterative_agent_framework_plan.md`を正とする。

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
- 探索段階のArticle ID、documentId、contentUnitIdは、今回のPromptに提示した
  既知IDをenumとして渡す。統合段階は、共有DAGの複数階層へ長いenumを反復展開
  せず、Promptの既知IDと出力直後の完全一致検証・sanitizeで未知IDを拒否する
- 候補にない法令名・条番号は内部IDを推測させず、`search_corpus`への
  検索要求として表現する
- 法令とガイドの種別を保持する
- 時間・候補・文字数の上限
- LLM判断とツール実行履歴をtraceへ残す

LLM事業者のクレジット・利用枠不足は`provider_quota_error`として明示し、
「投入済みデータに根拠が無い」という意味上の失敗へ変換しない。

法的役割の固定リスト、論点数、法令レイヤーごとの探索順序は、LLMの必須手順として
プロンプトへ埋め込まない。

## 3. 反復深化型の調査契約

外側は3調査サイクルとし、各サイクルの内部で次を完了する。現行アルゴリズム識別子は
`iterative_cycles_v8_hypothesis_testing`である。

1. `explore`: 前回の知見を踏まえて法令・ガイドを探索する
2. `deepen`: 探索結果を読み、Article本文・Graph関係・法令内検索を掘り下げる
3. `integrate`: 原文ID付きの調査チェックポイントへ統合する

第1サイクルの結果を第2サイクルへ、第2サイクルの結果を第3サイクルへ引き継ぐ。
各サイクルは同じ3段階を繰り返し、後続サイクルでは前回の暫定結論も再検証する。

確認済み事実の正本はCheckpointではなく、質問ごとに作る`ResearchCase`とする。
LLM Actionは直列`ResearchTask`として実行し、ツール結果は統合を待たず案件へ記録する。
Checkpointは検証済み統合判断の安全地点であり、統合タイムアウト時には増やさない。
次サイクルは最新Checkpointに加え、それ以降の案件イベントと未完了・候補Taskを読む。
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
- `logicalStructure`: 仮説、検証結果、論点、結論、根拠階層、未確認事項

`logicalStructure`は次の階層で表す。

```text
 hypotheses
 └─ hypothesis
    ├─ hypothesisId / statement
    ├─ status
    ├─ evidenceIds
    └─ missing

issues
└─ issue
   ├─ authorityNodes（論点内の共有根拠レジストリ）
   │  ├─ 直接根拠
   │  └─ 委任先・定義・例外・手続具体化・ガイド等
   └─ claims
      └─ authorityNodeIds（共有ノードへの参照）
```

各サイクルは`質問の重要な特徴を確認 → 仮説作成 → 仮説を識別できる検索・本文取得
→ 本文による支持・反証の比較 → 仮説の維持・修正・棄却・追加`を行う。
仮説は平坦な配列とし、各要素は`hypothesisId / statement / status /
evidenceIds / missing`の5項目だけを持つ。状態は
`unverified | partially_supported | supported | rejected`とする。同じ仮説の
`hypothesisId`はサイクル間で維持する。各`ResearchAction`と候補Taskは
`hypothesisIds`を持ち、検索結果がどの暫定結論の検証材料かを失わない。
一律に複数仮説を要求せず、質問を同程度に説明し得る有力な別構成がある場合だけ
競合仮説を残す。取得本文が関連するというだけで支持とはせず、その仮説が予測する
要件・対象・例外・手続等を本文が定めるか、質問の重要な特徴を説明できるか、
反証や適用範囲の不一致がないかを比較する。合わない情報が得られた場合は、同じ仮説を
詳しくするだけで済ませず、仮説を修正・棄却し、必要なら新しい仮説を追加する。
`unverified`以外の判定には確認済み`evidenceIds`が必要である。`unverified`または
`partially_supported`が中心的結論を変え得るかはLLMが判断し、変え得る場合だけ
`ready`にしない。影響しない未確認範囲は`missing`と
`unresolved(affectsCoreConclusion=false)`へ明示できる。サイクル番号やTask履歴は
CaseStoreのversion・Eventで管理し、仮説要素へ重複して持たせない。

`missing`は仮説の未確認点を説明する記録とし、プログラムが自由文を意味解釈して
候補Taskを昇格する命令欄にはしない。LLMが次に読むべきArticleを既知候補から選び、
`fetch_articles`またはCheckpointの`nextArticleIds`へ明示する。IDが不明なら、
法令名・条番号・検証目的を`search_corpus`または`unresolved(action=search)`へ残す。
プログラムは、条番号、法令レイヤー、見出し類似、検索順位から法的必要性を推測しない。
指定されたIDが実在し、Promptで許可され、本文取得状態やTask状態と矛盾せず、
許可ツール・権限・件数・時間の範囲内であることだけを検証する。

旧形式の`requiredLegalRoles / verification`から新しい仮説statusをプログラムが
推測変換しない。現在の構造化schemaに適合しない判断は受理せず、LLMへ形式修正を
求める。

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

一般検索で発見したArticleは、代表本文がPrompt表示上限から外れても失われないよう、
`ResearchCase`へ`search_result`由来の本文取得候補Taskとして記録する。候補Taskは
文書単位で分散し、同じ先頭候補を毎回表示せずページを進める。今回取得した本文IDも
検索Actionの実行順ではなく、文書→Articleの順に分散してPromptへ配置する。候補を
直接実行できない統合段階のPromptViewではページ位置を進めず、次の探索判断が未確認
候補を飛ばさないようにする。本文を全量表示した証拠は、本文なし候補一覧へ重複掲載しない。
文字予算で本文が途中までしか表示されない場合は、原文ごとに`textTruncated / originalTextChars /
displayedTextChars`を付ける。LLMは表示部分が直接支える主張だけに利用でき、未表示末尾に
例外がないことや列挙が完結したことを推測してはならない。探索・統合の構造化出力で
根拠として参照できるIDも、Catalog全体ではなく今回本文を実際に表示したIDへ限定する。
検索またはGraph展開から作られた候補Taskは、起点Actionの`hypothesisIds`を継承する。

使用できる操作:

- `search_corpus`: 投入済み法令・ガイドを検索
- `fetch_articles`: 既知のArticle IDから本文を取得
- `expand_graph`: 既知Articleから正式Graph関係と、未確認のRelationAssertion候補を区別して取得

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

Article本文取得を理由に、プログラムが特定の関係種別を自動選択してGraph展開しない。
関係種別を含むGraph探索の要否はLLMが判断し、`expand_graph`として明示する。
明示的なGraph探索の上限は起点ごと50件とする。この上限と1hop制約は安全・予算上の
制約であり、関係の法的重要性をプログラムが判断するものではない。

十分性は、質問が明示して求める各事項を取得済み法令で説明できるかで判断する。
考え得る全例外や、質問にない周辺制度の網羅を完了条件にはしない。明示事項へ影響しない
周辺的な未確認事項は、回答上の留保として示したうえで`ready`にできる。

`ready`の直前には、結論を支える法令本文を選択済みか、LLM自身が本文確認を必要と判断して
`nextArticleIds`または未確認事項へ残したArticleが未取得でないかを見直す。残り予算で
取得できる場合は`fetch_articles`を選び、取得してから採否を判断する。この確認は、
質問に明示されていない全論点の完全調査を要求するものではなく、質問の明示事項とLLM自身が
必要と判断した本文の取得漏れを防ぐ。明示事項の根拠が取得不能なら探索を無期限に続けず
`insufficient`とし、限定回答に使える根拠は保持する。

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
  - Graph展開はLLMが`expand_graph`を明示した場合だけ実行し、正式関係と未確認候補を混同しない
  - RelationAssertionの両端Article IDは直接取得候補として登録するが、候補を確定関係・
    根拠充足・mustIncludeへ自動昇格しない
  - 探索LLMは質問との関連性と本文取得の要否を判断する。統合LLMは両端本文を取得済みの候補だけを
    `confirmed / rejected / uncertain`として案件内で判断し、`relationDecisions`へ保存する
  - プログラムは既知ID、候補との一致、両端本文の存在だけを検証し、関係の意味を決めない
  - 取得済みArticleの再取得でも、返却本文IDを次の判断へ優先提示する
  - clearance、件数、Graph深度をコード側で制限
- `app/llm_research_loop.py`
  - 3サイクル×`explore → deepen → integrate`の反復
  - サイクルごとに操作履歴を破棄し、結論と階層的な法的論理構造を持つ
    構造化チェックポイントだけを継承
  - 前サイクルでLLMが作ったIssue IDは後続Checkpointでも保持する。`ready`では各Issueを
    `verified`にし、そのIssueの確認済み根拠を少なくとも1件トップレベル`evidenceIds`へ
    選ぶ。プログラムはIssueの意味を判断せず、このLLM生成済み対応の保持だけを検証する
  - Checkpointの件数・既知ID・Article IDと本文IDの区別に違反した場合は、
    プログラムでIDを推測変換せず、検証エラーを統合LLMへ返して1回だけ再出力させる
  - 最終サイクルでは`continue`をschema上許可せず、中間サイクルの`continue`には
    `nextQuestions / nextArticleIds / unresolved`のいずれかを要求する
  - ターン数、ツール回数、時間の共有予算
  - 上限到達時も最後に検証できたチェックポイントを保持
  - 未取得IDを含む判断を実行せず、次ターンでの自己修正を許可
  - LLMの時間切れを`llm_timeout`、接続障害を`llm_connection_error`として区別
- `AgentService`のactive単独回答経路
  - 旧planner、Requirement生成、充足判定、固定枠選抜を迂回
  - 調査LLMがCheckpointの`evidenceIds`で選んだ本文だけを、Core Projectorが
    順序を変えずMaterial表示へ展開する。`openEvidenceIds`や根拠DAGから
    プログラムが回答候補を補充しない
    Projectorは法的関連性による採否を行わず、件数・文字数・省略数・cursorのmanifestだけを
    決定的に生成する
  - IntegrationからMainへは中間結論、仮説の判定、`verified`状態を渡さない。
    `issue-grounding-v1`共有回答契約として、`issueId / question`、利用可能な既知
    `contentUnitId`、選択上限だけを渡す。Mainは引用本文を読んで各Issueを独立に再判断する
  - Main LLMが最終回答、`ready | partial | insufficient`、最大`topK`件の
    `citationIds`、`missing`に加え、Issueごとの`issueDecisions`（`status / conclusion /
    citationIds / missing`）を同じ構造化判断として返す。回答本文へ内部`contentUnitId`を
    重複記載せず、根拠選択の正本を構造化`citationIds`へ一本化する
  - 複合質問では、明示された発生条件、対象、例外、手続等をそれぞれ回答するか`missing`へ
    明示する。Reviewer差戻し時は、前回の回答本文だけでなく`answerStatus / citationIds /
    missing / issueDecisions`とReviewerの判定をMainへ戻し、構造化判断全体を再判断させる
  - ReviewerはMainと同じ`issueId`を使い、引用本文との不整合を`findings`として批評し、
    質問が明示した各事項の回答・`missing`・欠落も独立に確認して、
    `supported | needs_revision | needs_research | insufficient`から次動作を判断する。
    `supported`以外は`findings`必須、`needs_research`だけは`researchQueries`も必須とする。
    ReviewerにはMainの選択済み引用と未選択の利用可能候補を区別して渡し、後者は現在の
    回答を支持する根拠には使わせず、引用再選択で直るか追加検索が必要かの判別だけに使う
  - `needs_revision`は利用可能候補の範囲でMain LLMへ再判断させる。`needs_research`ではReviewerが
    最大2件の`researchQueries`を決め、プログラムはその検索語を既存検索ツールへそのまま渡す。
    新旧候補の意味上の採否は再度Main LLMが`citationIds`で決める。再判断時は追加検索前の
    Checkpoint状態を履歴として区別し、Reviewer検索で追加された本文IDと検索語を明示する
  - 追加調査または再判断後のReviewでも解消しなければ断定回答を止める。
    `insufficient`を同じ材料で機械的に書き直させない
  - Reviewerが`needs_revision`または`needs_research`を続けて返した場合は、最大2回の
    remediation roundで指定動作を反復する。上限は実行境界であり、動作種別はReviewerが決める
  - MainまたはReviewerの構造化出力が契約に違反した場合、プログラムは意味や次動作を
    補正せず、エラー理由を同じLLMへ返して1回だけJSON全体を再判断・再出力させる。
    再試行の補足指示はMain、Reviewer、Integrationの現在役割だけを含める
  - プログラムは既知ID、件数、Issue ID集合、状態、Issue別とトップレベルの
    `citationIds / missing`の集合整合だけを検証し、法的な採否や回答文の修正は行わない
  - タイムアウト・接続障害・根拠不足を旧方式で隠さない
- 環境変数と`/health`表示
  - 同一`LLM_PROVIDER`内で、Mainは`ANSWER_MODEL`、Reviewerは`REVIEWER_MODEL`、
    探索・掘り下げは`LLM_RESEARCH_STAGE_MODEL`、Checkpoint統合は
    `LLM_RESEARCH_INTEGRATION_MODEL`として個別に切り替えられる
  - `REVIEWER_MODEL`未設定時は`ANSWER_MODEL`へ戻す。調査の役割別model未設定時は
    互換用`LLM_RESEARCH_MODEL`、さらに未設定なら`ANSWER_MODEL`へ戻す

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
