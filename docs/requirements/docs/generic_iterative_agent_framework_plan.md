# シンプルな汎用反復型エージェント基盤 実装計画

> 更新日: 2026-08-17
>
> 本書を新しい実装ロードマップの正本とする。
> `llm_research_case_store_implementation_plan.md`は現行経路の移行元を説明する資料、
> `llm_directed_legal_retrieval.md`は現行実装の挙動を説明する資料としてのみ参照する。
>
> 本計画の作成時点では、現行回答経路は変更していない。未接続の`agent_core`試作を
> Phase完了とは数えず、新しいPhase 0から評価と移行を開始する。

## 実装状況（2026-08-17）

- Phase 0: 新契約のfixtureと意味判断境界の単体テストは完了。現行2問の実測baseline採取は未実施。
- Phase 1: `agent_framework`、薄い`InMemoryCaseStore`、用途別Profile、read-only Toolの上限制御付き
  並列実行、保持Evidence、反証時の明示的な影響判断、任意Reviewerは実装済み。ただし現行コードの
  `research_cycle_count`は初回ToolでCycle 1となり、Solverが`start_next_cycle=true`を返した時だけ
  後続Cycleへ進む。一方、`CycleRecord`・`StepRecord`、Cycle累計の本文取得数・Tool数・時間の
  境界は未実装で、`max_tool_requests_per_cycle`も1 Decisionの上限として検証されている。
  本書4.1・5.1.2の最大4 Cycle、1 Cycle本文4件、予算到達前のCycle終了契約へ直すまで
  Phase 1のサイクル部分は未完了とする。
- Phase 2: 法令Domain Pack、Article単位OpenSearch Tool、入出力双方向のNeo4j候補Tool、
  条文本文取得Tool、既存provider共通structured JSON Model Adapter、明示検証用
  `/answer/framework`、`/answer`用Feature Flagを実装済み。Legal Profile v10では、Solverが
  `fetch_articles`に選んだArticle IDだけを1ホップGraphへ自動転記し、成功済み起点を重複展開しない。
  Graph ToolはSolverから隠し、関係種別・方向の意味評価と次の本文取得先選択はSolverへ残す。
  ただし案件内の探索Node、複数の発見Link、frontier、本文取得状態、Graph展開範囲、hopを保存する
  構造は未実装である。Feature Flagの既定値はOFF。
- Phase 3以降: 未完了。新Frameworkの最小traceはあるが、ログ・性能の完了条件は未評価。
- Phase 4予備評価: Article候補の先頭chunkだけを返して施行令7条1項9号を隠していた検索投影を修正。
  第1位Articleは取得済み一致箇所を重複圧縮して最大5000文字、他候補は最大400文字、ガイドは
  上位2件とし、並列Tool結果をround-robinでSolverへ渡す。Reviewer OFF・全Haikuの公開買付け問題は、
  修正途中の2回がprotocol_error、最終回は2 cycleで8/11。法27条の2・施行令7条と4回答要点は
  到達したが、Graph呼出しは0回で、公開買付府令2条の5・10条は未到達。検索候補には施行令7条
  1項9号と府令への委任が表示されており、残課題はSolverが明示された委任をGraph探索へ結び付けず、
  府令未確認をlimitationsへ残したままfinalizeしたことである。
- Phase 4追加評価: Legal Profile v3で`lower_law` DependencyDecisionを導入した。最初の実測は
  従来同様8/11で、委任元本文を委任先確認済みとする粗い`resolved`が原因だったため、依存元Evidence、
  `discover_source / assess_source / discover_target / fetch_target`、対象Article、別法令本文の来歴を契約へ追加した。
  その後の実測では、一括fetchの検証が厳しすぎる問題と、内包JSONの必須欄欠落が契約修復を迂回して
  `model_protocol`になる問題を検出した。一括Request中の委任先ArticleをDecision側で明示する方式へ直し、
  内包JSONの構造不足はAgentLoopの契約修復へ渡すよう修正済み。API実測3回の上限後に直したため、
  最終修正版による品質点の再計測は未実施。非APIテストは618件成功している。
- Phase 4再評価: Legal Profile v5ではDependencyDecisionをProviderの直接構造化配列へ変更し、
  必要種別・対象WorkItem・必要件数をSolverContextとschemaへ明示した。未知Article IDの契約修復も追加。
  Reviewer OFF・全Haikuの公開買付け問題は2 cycleで完走し8/11。法律・施行令と主要4回答要点は取得したが、
  Graph呼出しは0回で、公開買付府令2条の5・10条は未取得だった。Solverは対象範囲を`not_required`、
  手続を施行令本文で`resolved`とする一方、limitationsへ内閣府令確認の必要性を記載しており、
  残課題は輸送・参照契約ではなく同一SolverDecision内の意味的一貫性である。非APIテストは620件成功。
- Phase 4 Prompt再評価: Legal Profile v6以降でIntegrationを「取得評価、下位規範監査、追加調査、
  終了整合監査、終了」の順序付き手順へ変更し、明示的に求められた観点を「概要には上位法令で十分」
  として`not_required`にすること、目的・総則条項を委任先本文の代理にすることを禁止した。v6は2 cycleで
  完走したが8/11、Graph 0回、府令文書の第1条だけを取得し府令2条の5・10条は未取得。v7/v8では
  DependencyDecisionのsource/target重複違反で停止した。多段委任では中間Articleが前段のtargetかつ
  後段のsourceになり得るが、現行のWorkItem単位1レコード契約はこれを表現できない。残課題はPromptではなく、
  依存関係を1 WorkItem 1件へ集約し、source/target重複を一律拒否する契約設計である。
- Phase 4 v9/v10再評価: `fetch_articles`に1ホップGraphを自動連動し、下位法令の未確認状態を
  WorkItem・Hypothesis・gapsへ統合して、Legal Profileから重複するDependencyDecision要求を外した。
  非APIテストは623件成功。Reviewer OFF・全Haikuの公開買付け問題を3回実行したが、1回目と3回目は
  `tool_requests_json`内でHaikuが検索本文の条番号から未知Article IDを生成し、契約修復後も残したため
  cycle 1でprotocol_error、2回目は旧DependencyDecisionのsource不足でcycle 2に停止した。
  3回とも本文取得前に停止したため、自動Graphの実API確認と9/11形式の品質採点には未到達。
  検索結果自体には施行令7条、公開買付府令2条の5・10条、法27条の3が含まれていた。
  次の課題はPrompt追加ではなく、`tool_requests_json`内のArticle ID選択をProvider schemaの既知ID enumへ
  移し、LLMが一覧外IDを構造上返せない輸送契約にすることである。
- Legal Profile v11: LLMへ入出力される契約値を監査し、WorkItem、Hypothesis、反証影響、
  ToolResult、最終化、Evidence区分、任意DependencyDecisionの語彙をProvider共通Solver契約へ集約した。
  法務PromptにはGraph候補の`kind`、`status`、`referenceKind`、来歴項目の意味を追加し、
  `llm_classified_implements`を正式関係と誤認せず両端本文で判断することを明記した。
  `run_status`等の実行制御値はSolverContextへ渡らず、Reviewerの`accept / revise`は既存Promptで定義済み。
  非APIテストは623件成功。
- Legal Profile v14: CaseStateのGraph navigation EvidenceとToolResultを監査用の正本として維持したまま、
  SolverContextではGraph端点をArticle IDごと1件の`graph_candidate_catalog.articles`、全発見経路を
  `graph_candidate_catalog.links`へ正規化した。Graph navigationのJSON・Evidence ID・監査来歴は
  manifest、ToolResultのEvidence ID、navigation・omitted ID一覧へ重複表示しない。
  同一Articleの重複表示だけを除き、起点、関係種別、方向、status、WorkItem・Hypothesisとの対応は
  Linkごとに保持する。候補の関連性と本文取得対象はSolverが判断する。
  実データ投影で153件のGraph navigationを139 Article・153 Linkとして保持し、非APIテスト627件に成功した。
- Legal Profile v15: 正規化Articleの`content_status`全値を共通Promptに定義した。検索本文中の条番号や
  documentIdからArticle IDを組み立てることを禁止し、対応IDが`fetchable_article_ids`にない参照先は
  法令名・条番号・確認事項をqueryにした`legal_search`で発見し直す手順を明記した。
  通常Decisionと契約修復Decisionの両方で、`fetch_articles`の全IDを一覧と完全一致で自己確認する。
- Legal Profile v16: `search_navigation`の本文抜粋を次のTool選択専用とし、Hypothesis判定、
  WorkItemの解決、最終回答の根拠に使わないことを明記した。別の根拠で検証する適用要件、
  数値基準、例外、義務・手続を束ねず、各主張を直接支えるgrounding Evidenceと照合してから
  finalizeする。特定条文の内容はそのArticle本文で確認し、別ArticleやGraph候補で代用しない。
- Legal Profile v17: 同じDecisionで取得する既知Article IDは、WorkItemが異なっても12個以内なら
  1つのfetch_articlesへ統合する。論点別にRequestを分けて自動1ホップGraphの候補投影を重複させず、
  Requestのwork_item_idに主対象、hypothesis_idsに検証対象全件を指定する。
- Legal Profile v18: Graph探索の上限を1ホップに固定した。起点Articleの本文取得にだけ
  自動Graph取得を連動し、`neighborArticleId`として発見済みのArticleは本文取得のみを行う。
  Graph候補Articleからは再展開せず、その先が必要な場合はSolverが`legal_search`で別起点を発見する。
- Legal Profile v19: 1回の`fetch_articles.article_ids`を最大4個にした。プログラムは5個以上を
  選別・切捨てせず契約違反とし、Solverが未確認の命題に直接必要な4個以下を選び直す。
  4個は取得目標ではなく上限であり、残りの候補はCaseStateへ保持する。
- Legal Profile v20: 新しいGraph候補が投影された場合、通常の統合・完了判断より先に同一Solverの
  `graph_selection`モードを呼び、`GraphCandidateReview`へ対象Graph ToolResult、選択Article、理由を保存する。
  プログラムはReviewの存在、既知ID、件数、1件の`fetch_articles`との完全一致だけを検証し、
  法的関連性を判断しない。現行Neo4jデータでも法令名を提示できるよう、Articleの`title`がない場合は
  Documentノードの`title`をGraph queryで補い、将来のseedではArticleにも`title`を保存する。
- Legal Profile v20実測: Reviewer OFF・全Haikuの公開買付け問題は3 cycleで完走したが8/11。
  Graph選別は施行令3件だけを選び、公開買付府令2条の5・10条を選ばなかった。一括本文取得の自動Graphが
  主対象WorkItem IDを全Linkへ付けていたため、Solverが適用要件の作業だけを優先したことが原因だった。
- Legal Profile v21: Graph LinkのWorkItemは、集約Requestの全Hypothesisから所属WorkItemを機械的に復元する。
  本文を再掲しない選別用Contextで、Solverは全Article見出しとLinkをopen WorkItemごとに評価し、
  `work_item_assessments`を各WorkItemへ1件必須で返す。選択Articleの和集合だけを最大4件取得する。
  一覧外Article ID等の契約違反は、プログラムが補正せずSolverへ最大2回差し戻す。
- Legal Profile v22: v21実測では各WorkItemの評価欄があっても、Solverが一般条項を1つのWorkItemへ
  選んだ後、他の範囲・例外・手続もその条項から足りると推測して8/11で終了した。各WorkItemについて
  本文が回答に影響し得る全候補`relevant_article_ids`と今回取得する`selected_article_ids`を分離し、
  直接具体化する下位規範と一般条項を比較してから最大4件を選ぶ。プログラムは既知候補か、選択が
  関連候補に含まれるか、全体の選択との集合整合、取得件数だけを検証し、関連性は判定しない。
- Legal Profile v23: 同じArticleがOpenSearch候補とGraph候補の両方で発見された場合、検索による
  depth 0起点をGraph由来depth 1で上書きしない。Graph候補選別後の本文取得は新Cycleではなく同じ
  Cycleの後続stepとして実行し、新たな1ホップ候補を同じCycleで再評価できるようにする。また、
  resolved WorkItemのbasis HypothesisにSolver自身が指定したEvidenceを最終回答のcitationから落とす
  参照不整合を契約違反として同じSolverへ差し戻す。プログラムは法的十分性を判定しない。
- Legal Profile v24: v23実測は2 cycleで10/11となり、残る府令10条をSolver自身がWI-004の理由で
  次回確認対象と明記しながら`relevant_article_ids`へ入れず、取得枠を3/4で空けた。理由に完全一致の
  既知・未取得Article IDを書いた場合の関連候補欄との参照整合と、Solver宣言済み関連候補が残る場合に
  取得枠を空けないことを検証する。関連性の判定や候補追加はプログラムでは行わない。
- Legal Profile v25: v24実測で、理由欄のArticle ID照合が`article-27_2`を短い
  `article-27`への言及とも解釈する前方一致バグを検出した。IDの後続文字境界まで照合し、完全な
  既知IDへの言及だけを関連候補欄との参照整合対象にする。
- Legal Profile v26: Graph Articleの法的関連性と本文取得状態を分離する。`relevant_article_ids`は
  取得済みを含む既知Graph Articleを許可し、`selected_article_ids`と未使用取得枠の検証対象だけを
  `content_status=not_requested`に限定する。取得済みの関連条文を再掲しただけで停止または再取得しない。
- Legal Profile v27: v26実測では府令2条の5・10条をSolverが関連候補へ入れたが、先に別の4件を
  取得した後で未取得frontierが再投影されず9/11で終了した。Graph ToolResultごとの最新Reviewを正本にし、
  Solver宣言済みの関連候補に`not_requested`が残る間は同じCycleの後続`graph_selection` stepを要求する。
  新しい本文を見たSolverが関連性を再評価して候補を外すことは許し、過去Reviewを固定判断にしない。
- Legal Profile v28: v27実測では未取得frontierによる2回目の`graph_selection`へ進んだが、Haikuが
  3回とも`graph_candidate_review=null`を返した。Provider structured-output schemaを呼出しモードに
  合わせ、required Graph reviewがある場合はReview object、ない場合はnullだけを許可する。これは
  候補の意味判断ではなく、呼出しモードと出力形式の構造整合である。
- Legal Profile v29: v28実測では検索、絞り込み検索、本文取得を各Cycleとして数え、Graph候補到着時に
  3 Cycle上限へ達した。`SolverDecision.start_next_cycle`を追加し、初回ToolでCycle 1を開始した後、通常の
  action-observationは同じCycleで反復する。作業分解・仮説・探索方針を仕切り直すとSolverが明示した時だけ
  次Cycleを開始する。Graph選別は常に同じCycleのstepであり、プログラムは再計画の要否を判定しない。
- Legal Profile v30: v29実測では同じDecisionに4つの`fetch_articles`が分かれ、同じArticleの重複取得と
  Requestごとの自動Graphが候補・入力・所要時間を増幅した。1 DecisionのArticle本文取得を重複なしの
  1 Requestへ限定し、違反時はSolverへ差し戻す。4件超をプログラムが統合・選別・切捨てしない。
- Legal Profile v31: v30実測ではHaikuが取得済みArticleを`selected_article_ids`へ入れ、2回の修復でも
  直らなかった。Graph ReviewのProvider schemaで、`relevant_article_ids`は全既知Graph ID、
  `selected_article_ids`は`content_status=not_requested`の既知IDだけを列挙可能にする。関連性の意味判断は
  Solverのまま、プログラムは取得状態から許容ID集合を投影する。
- Legal Profile v32: v31ログで、残り58秒から最終回答用45秒を除いた13秒のGraph選別がAnthropic
  read timeoutになったことを確認した。open WorkItemが取得上限4件以内なら、Solverが未取得関連候補を
  宣言した各WorkItemから最低1件を選ぶ構造整合を追加する。関連候補は各WorkItem最大12件、理由は短文化し、
  Graph選別出力を最大4096 tokenとして、1観点への偏りと後続step数を抑える。候補の意味選択はSolverが行う。
- Legal Profile v33: v32実測で、複数WorkItemに共通するArticleが全体では選択済みでも、各
  `work_item_assessments[].selected_article_ids`へ重複記載されないことを未代表と誤判定した。各WorkItemの
  `relevant_article_ids`と全体`selected_article_ids`の交差でbatch内の代表性を検証する。
- Legal Profile v34: v33実測では自由文reasonに現れたIDと`relevant_article_ids`の照合が2回の修復後も
  停止原因になった。自由文を構造データとして再解釈する暫定検証を削除し、関連・選択・frontierのID判断は
  structured fieldだけを正本とする。reasonは監査説明であり制御入力にしない。
- Legal Profile v35: v34実測では4 WorkItemを1 batchで最低1件ずつ代表させる制約をHaikuがWI-004だけ
  満たせず、修復反復の停止原因になった。この配分制約は意味選択へ踏み込みすぎるため削除し、偏りは
  Solverが宣言した未取得関連候補を同じCycleの後続stepへ残すfrontierで扱う。
- Legal Profile v36: v35では最初のGraph選別で府令2条の5を取得した後、再選別がProvider schema違反で
  停止した。関連候補12件・reason文字数の出力schema制限を撤回し、短文化はPrompt指示へ戻す。
  取得対象4件上限、未取得ID enum、frontier継続は維持する。
- Legal Profile v37: v36実測ではGraph Reviewの選択IDと、JSON文字列内に重複記述するfetch requestの
  IDが一致せず停止した。`selected_article_ids`を唯一の正本にし、Graph選別は`tool_requests=[]`を返す。
  AgentLoopは選択ID・順序を変更せず本文取得Toolへ機械転記し、法的な候補選択は行わない。
- Legal Profile v38: v37実測ではfrontier再選別が同じGraph request IDを再度Reviewした際、旧状態検証の
  全履歴一意制約で停止した。Review単体内のID一意性は維持し、時系列Review間の同一request参照を許可する。
  履歴は全件保持し、frontier制御にはrequestごとの最新Reviewを使う。
- Legal Profile v38実測: Reviewer OFF・全HaikuでCycle 1のままTool 12回、本文16条を取得し、
  4回目のGraph候補選別呼出しが残り約15秒でprovider timeoutとなった。
  `max_tool_requests_per_cycle=4`は実際には1 Solver Decisionあたりの上限で、自動Graph Toolと
  後続Decisionを含むCycle累計を制限していなかった。Graph選別も、本文取得対象は最大4件だが、
  累積したGraph catalogと過去の未取得frontierを毎回再入力し、入力が約5万から約14.9万tokenへ
  増えた。未来のSolver時間を予約しても、中間呼出しの予算由来timeoutをCycle終了へ変換せず
  Run全体を`provider_error`にした。この結果から、Cycle累計の本文取得上限、4 Cycle上限、
  予算到達前のCycle終了判断、Graph差分Reviewを同じ修正単位で導入する。
- Legal Profile v39実装（未検証）: `max_research_cycles=4`、Cycle累計の本文取得4件、Graph Reviewの選択3件、
  新規・未評価・再採用・新Link差分の`graph_review_batch`、評価済みfrontierの短い`graph_review_ledger`を導入する。
  全Node・Link・Review履歴はCaseStoreに保持し、過去の全詳細をLLMへ再送しない。Cycle境界前に
  終了判断時間を予約し、予算由来の中間timeoutは`cycle_step_timeout`としてCycle終了判断へ渡す。
  状態型・Context・Provider schema・Prompt・Profile version・設定は実装へ反映済み。検証は利用者指示により
  未実施で、既存fixtureの新契約への更新と非APIテスト・実モデル評価は次の作業とする。
- Legal Profile v45: v44の公開買付け実測では、Cycle 1の本文取得枠消費後に未評価Graph候補が残ったが、
  Solverが全WorkItemをclosedへ修復し、limitationsでは関連府令未確認と記載したまま8/11でfinalizeした。
  契約修復Promptから「全open WorkItemを閉じる」という近道を除き、未確認事項・gaps・limitationsを保って
  通常判断へ戻すよう変更した。Cycle境界の未評価Graph候補群には`UnreviewedGraphResolution`を必須とし、
  `review_next_cycle / no_longer_needed / unresolved_at_limit`と次動作の構造整合だけをProgramが検証する。
  `answer.limitations`は未確認事項専用とし、対応するopen WorkItemとunresolved Hypothesisの既知IDを
  `unresolved_work_item_ids / unresolved_hypothesis_ids`へ必須化した。次Cycleを開始可能な通常finalizeでは
  未解決scopeを許さず、上限時はWorkItemを偽ってclosedにせず限定回答として保持できる。
- Legal Profile v46: v45実測は、未知Evidence ID、未評価Graph方針欠落、open WorkItem finalizeが
  fail-fast検証で順番に1件ずつ現れ、3回の契約試行を使い切って停止した。Cycle境界で
  `remaining_unreviewed_count > 0`ならProvider schemaから`unreviewed_graph_resolution=null`を除外する。
  同一Decisionの未知Article ID、未知Evidence ID、未評価Graph方針欠落、open WorkItem finalizeを
  構造preflightでまとめて差し戻す。open-finalize違反の修復時に次Cycleを開始可能なら、Provider schemaを
  `next=continue / start_next_cycle=true / answer=null / tool_requests=[]`へ限定し、未評価Graph候補があれば
  `review_next_cycle`を必須にする。Programは法的必要性を判定せず、既知IDと状態遷移の矛盾だけを扱う。
- Legal Profile v47: v46実測では、open WorkItemの修復時に、一度修復済みだった未知Evidence IDを
  Solverが再生成した。open-finalize違反まで到達した直前Decisionの`CaseUpdate`は、それ以前の検査を
  通過済みなので、修復呼出しのProvider schemaで単一候補として維持する。修復対象はCycle遷移と
  未評価Graph候補の扱いに限定し、ProgramはEvidenceの意味やWorkItemの完了状態を変更しない。
- Legal Profile v48: v47実測では、初回Decisionが追加したWorkItem・HypothesisのIDと、同じDecisionの
  focus・ToolRequestが参照したIDに表記ずれが生じた。focus、ToolのWorkItem、ToolのHypothesisを
  構造preflightで同時に検査して一括差し戻しし、修復呼出しのfocus候補を直前Decisionに実在する
  open WorkItemへschema制限する。Programは参照先を選ばず、Solverが既知候補から選択する。
- Legal Profile v49: v48実測では、open-finalize修復で検証済み`CaseUpdate`全体を長いJSON文字列enumとして
  再出力させた結果、Anthropic輸送が`invalid_json`になった。修復呼出しは`update_json={}`だけを返し、
  Adapterが直前Solver Decisionの`CaseUpdate`を同一内容のまま復元する。LLMの意味判断を変更せず、
  冗長な再出力と再生成による参照破損を防ぐ。

Phase 1の契約テストは`agent-api/tests/test_agent_framework.py`を正本とする。

## 1. 決定事項

本計画では、次を確定事項とする。

1. Codex型の反復ループを維持する。
2. 仮説検証を小さく繰り返す。
3. 汎用基盤と法令検索ドメインを分離する。
4. 意味判断はLLMが行い、プログラムは実行と機械的検証だけを行う。
5. 判断主体は`Solver`と、任意の`Reviewer`だけにする。
6. `Projector`、`Integrator`、`Explorer`、`Answerer`を独立した登場人物にしない。
7. 調査サイクルは固定回数を必ず実行せず、通常1〜2回、最大4回とする。
8. 1 Cycleで本文取得できるResourceは累計4件までとし、上限または時間境界で
   Cycleを閉じ、そのCycleの結果と次の探索方針をSolverが評価する。
9. Graph候補はCaseStoreに全件保持するが、Graph ReviewのLLM入力は新規・未評価候補、
   仮説変更で再採用した候補、既評価候補へ追加された新Linkの詳細差分と、全評価済み候補の
   短い台帳に限る。
10. Reviewerはオプションとし、**デフォルトでは無効**にする。
11. 現在はDBを使わない。初期実装は単一プロセス用の`InMemoryCaseStore`とする。
12. SQL生成は別プロジェクトでの応用例であり、このリポジトリでは実装しない。
13. 案件内の作業分解は、最上位から下位まで`WorkItem`という名称で統一する。
14. SolverはCaseState全体を再生成せず、安定IDを使った変更差分だけを返す。
15. 1サイクル内で、仮説に必要な探索を複数のaction-observation stepとして反復し、
    全結果を評価してからサイクルを閉じる。
16. Legal Profileでは、Solverが本文取得に選んだArticleだけを起点として、同じcycleで1ホップGraphを自動取得する。
17. 1サイクルはTool実行回や1ホップではなく、1つの仮説・探索方針に対する
    上限付きの仮説検証単位とする。根拠を探し切るか、本文取得・step・時間のいずれかの
    Cycle上限に達した時点で必ず結果を評価し、完了または更新した方針で次Cycleへ進む。
18. 本書の`Graph Review`は独立Agentや任意の`Reviewer`ではなく、SolverがGraph候補の
    関連性と本文取得順を判断するモードを指す。Reviewerを無効にしてもGraph Reviewは実行できる。
19. 再帰探索は純粋な木にせず、案件内探索Graph、frontier、展開済み集合、CycleRecordで管理する。
20. LLMへ提示または出力させるstatus・judgment・actionの全値は、許容値だけでなく意味と決定主体をPromptに定義する。
21. Legal ProfileのGraph最大hopは`1`に固定し、OpenSearch起点を深さ0として数える。

## 2. 目的と非目的

### 2.1 目的

検索対象に依存しない、次のエージェントループを実装する。

```text
作業を分解する
  → 仮説を立てる
  → 仮説を検証する行動を選ぶ
  → ツール結果を観察する
  → 仮説・作業構造を更新する
  → 必要な範囲だけ繰り返す
  → 根拠付きの成果を返す
```

法令検索は、このループを最初に接続して評価する業務ドメインである。

現行実装で1問に3分以上かかる主因である、固定3サイクルと
`explore → deepen → integrate`の直列LLM呼び出しを廃止する。

### 2.2 非目的

初期実装では、次を行わない。

- DB永続化
- Repositoryのエンティティ別分割
- Unit of Workや疑似DB transaction
- EventJournalを正本にするイベントソーシング
- Projector、Scheduler、Integrator等のサービス分割
- サブエージェント
- 書込みを含む並列実行
- SQL生成
- 自動的な法的判断

将来必要になった機能は、実測された要求が出た時点で追加する。

## 3. 全体構成

```text
利用者
  ↓
AgentLoop（プログラム）
  ↓
Solver（LLM）
  ├─ researchモード: 作業分解、仮説、次のToolRequest
  └─ finalizeモード: 根拠を統合した最終回答
  ↓
Reviewer（LLM、既定無効）
  ├─ accept
  └─ revise → Solverへ具体的な指摘を返す
```

`AgentLoop`は判断主体ではない。状態を読み、適切なProfileでLLMを呼び、
出力を機械的に検証し、ツールを実行して結果を保存する。

### 3.1 責務

| 主体 | 担当すること | 担当しないこと |
|---|---|---|
| Solver | 作業分解、仮説、意味評価、根拠選択、追加調査、完了判断、回答 | ツールの直接実行、存在しないIDの生成 |
| Reviewer | 回答と根拠の整合確認、具体的な修正指摘 | ツール実行、後続経路の直接制御 |
| AgentLoop | LLM呼び出し、状態更新、上限管理、ツール実行、再試行制御 | 法的関連性、十分性、重要度の判断 |
| Tool Adapter | 検索・本文取得・Graph取得と実行結果の正規化 | 取得物の法的評価 |
| CaseStore | CaseStateの保存と読出し | Prompt編集、意味判断、重要度選択 |
| Domain Pack | 法令用Prompt、ツール定義、根拠表示形式 | 汎用ループの制御 |

## 4. 反復ループ

### 4.1 Solver判断と1サイクル

1サイクルは、1件または明示的に束ねた少数のHypothesisについて、1つの仮説・探索方針を立て、
その方針に必要な根拠を予算内で探し、結果全体を評価するまでの試行単位である。
根拠を探し切った場合だけでなく、本文取得数・step・Cycle時間の上限に達した場合も、
未評価のToolResultを残さずSolverの終了評価を通してCycleを閉じる。1回のToolRequest、
1回のLLM呼び出し、1ホップのGraph展開をサイクルとは呼ばない。

```text
1サイクル
  1. FocusするWorkItem・Hypothesisを選ぶ              Solver
  2. Cycle goal、探索方針、完了・失敗条件を決める      Solver
  3. 必要な間、action-observation stepを小さく反復する
       a. 次に確かめることとToolRequestを決める        Solver
       b. Step開始状態を保存する                       Program
       c. Toolを実行しToolResult・Evidenceを保存する   Program
       d. 結果を観察し仮説・frontierを更新する          Solver
       e. 同じ方針を続けるか、Cycleを閉じるか判断する  Solver
  4. 本文取得・step・Cycle時間のいずれかの上限前に新しいactionを止める Program
  5. Cycle全体の結果をWorkItem・Hypothesisへ反映する   Solver
  6. finalizeまたは次Cycleのgoal・strategy・引継ぎを決める Solver
  7. CycleRecordを閉じ、cycle数を加算する              Program
```

各stepのSolver呼び出しは、現在のCycle goal、WorkTree、Hypothesis、探索frontier、直前の
ToolResultとEvidenceを読み、直前stepの評価と次のいずれかを返す。

- `continue_cycle`: 同じ仮説・探索方針のまま、次の検証用ToolRequestを返す。
- `start_next_cycle`: 今回の方針では完了できない理由とCaseStateの変更差分を返し、
  更新した仮説・方針で別Cycleを開始する。
- `finalize`: 必要な根拠を探し切ったと判断し、CaseStateの変更差分と根拠付き最終回答を返す。

同じCycleの間は、OpenSearchで起点を発見し、起点本文と1ホップを取得し、LLMが選んだ隣接本文を取得する。
隣接本文からGraphは再展開しない。新しいGraph候補の関連性と本文取得対象は
各stepでSolverが判断し、プログラムが再帰的に自動選択しない。

```text
Cycle 1: 仮説H1・探索方針P1・本文取得累計0/4
  Solver判断1 → OpenSearch
  Solver判断2 → 結果を評価し、起点Article本文を予算内で取得
  Solver判断3 → 新規Graph差分を評価し、関係する隣接Articleを選ぶ
  Solver判断N
    ├─ 根拠を探し切った                → finalize
    ├─ 同じ方針で次の検証が必要で予算が残る → continue_cycle
    └─ 方針変更またはCycle予算上限         → Cycleを評価しstart_next_cycle

Cycle 2: 更新した仮説H2・探索方針P2
  前CycleのEvidence・frontier・失敗理由を引き継いで再試行
```

公開買付け問題では、法27条の2・27条の3、施行令7条、公開買付府令2条の5・10条等の
候補を1 Cycleで無理に一括取得しない。Cycle 1は本文取得累計4件以内で仮説と起点を評価し、
未取得で関連ありと判断したfrontierは、Solverが引継ぎ対象と理由を選んでCycle 2以降へ渡す。
初期分解・中心仮説・検索起点が誤っていれば置換し、方針が妥当でも本文取得上限へ達したら、
同じ仮説を漫然と延長せず、取得結果と次Cycleで確かめる命題を明示してCycleを閉じる。

`research_cycle_count`はTool終了時やstep終了時には増やさない。Cycle全体の評価差分が契約検証を通り、
`CycleRecord`が`completed`になった時点で増やす。Tool成功後・評価前に停止した場合は同じCycleの
同じ`StepRecord.observed`から再開し、別Cycleとして数えない。

1サイクルで質問全体の候補を無制限に広げない。Cycle累計の本文取得数、各stepで選択できる
frontier件数、Graph depth、step数、Tool数、Cycle時間をProfileで機械的に制限する。
本文取得に付随する1ホップGraphは同じstepの
観察結果に含め、隣接Article本文は同じCycleの次stepでSolverが選んで取得する。

### 4.2 サイクル数と時間確保

- 通常: 1〜2 research cyclesで完了する
- 多段探索または再計画が必要な場合: 3〜4 research cycles
- 上限: 4 research cycles
- 1 Cycleの本文取得累計上限: Profileの`max_fetched_resources_per_cycle`。Legal初期値は4
- 1 Cycle内のaction-observation step上限: Profileの`max_steps_per_cycle`
- Run全体のstep上限: Profileの`max_total_steps`
- Solverが`finalize`を返した時点で即終了
- Cycle内step上限、Cycle回数上限、Run全体step上限のいずれかに達した場合は、現在の上限条件で
  許されないToolRequestを実行せず、手元の根拠と探索方針を評価する

Reviewer無効時のSolver呼び出しは次の範囲になる。

| ケース | LLM呼び出し |
|---|---:|
| 取得済み情報だけで回答可能 | 1回 |
| 1 Cycle内で2 action stepを実行 | 3回程度 |
| 1 Cycle内で4 action stepを実行 | 5回程度 |
| 別Cycleへ再計画 | 前Cycleのstep数に次Cycleの呼び出しを加える |

プログラムは各LLM・Tool実行前に、現Cycleを評価して閉じる時間、残りCycleの最小実行時間、
最終回答時間を別々予約する。残り時間が現actionの実行予算と予約の合計を下回る場合、
新しいGraph ReviewやToolを開始せず、`cycle_close_required=true`をSolverへ渡す。
Solverは手元の結果を評価し、`finalize`または次Cycleのgoal・strategy・引継ぎfrontierを返す。
予算によって短縮された中間LLM呼出しがtimeoutした場合は、実際のprovider障害と区別して
`cycle_step_timeout`を保存し、予約時間でCycle終了判断へ進む。

4 Cycleを必ず実行するループにはせず、Tool結果を未評価のまま次Cycleへ進むことも許さない。

### 4.3 ツールの並列実行

Solverは1回の`continue_cycle`で複数のToolRequestを返せる。

初期の法令検索ツールはread-onlyなので、同じSolver stepで返された要求を上限内で並列実行する。
プログラムが要求の意味的な独立性を推測してはならない。並列化できるのは、Tool定義が
`read_only=true`かつ`parallel_safe=true`と明示している場合だけとする。

並列実行後は、全ToolResultをまとめて次のSolverへ渡す。独立したExplorerエージェントは作らない。

### 4.4 上限到達

回数・時間・provider障害は、意味上の根拠不足とは区別する。

- 回数上限: `max_research_cycles`
- Cycle内の本文取得累計上限: `max_fetched_resources_per_cycle`
- Cycle内step上限: `max_steps_per_cycle`
- Cycle時間上限: `max_cycle_wall_time_sec`
- Run全体step上限: `max_total_steps`
- 全体時間上限: `max_wall_time_sec`
- LLM timeout: `model_timeout`
- Tool timeout: ToolResultの`timeout`
- provider障害: `provider_error`
- 構造化出力不正: `protocol_error`

上限到達時、プログラムはHypothesisを`unresolved`へ変更しない。
停止理由と手元の根拠をSolverへ渡し、限定付き回答を含む最終判断をLLMへ求める。

`cycle_budget_reached=true`または`cycle_step_limit_reached=true`でも残りCycle・総step・時間がある場合、
Solverは現方針の結果と
次Cycleで確かめる命題を示して`start_next_cycle`を選べる。仮説・探索方針の仕切り直しに加え、
Cycleの本文取得枠が尽きても必要と判断した未取得Evidenceが残る場合も対象とする。単なるTool終了や
Graph 1ホップ完了だけを理由にせず、取得済みEvidenceの評価と引き継ぐfrontierを明示する。
Cycle回数、総step、全体時間のいずれかで新しいToolを実行できない場合だけ
`finalize_only=true`とし、`continue_cycle / start_next_cycle`のToolRequestを禁止する。

## 5. 状態と契約

### 5.1 最小CaseState

初期実装は、次を1つの`CaseState`として保持する。

```python
class CaseState:
    case_id: str
    question: str
    run_status: RunStatus
    research_cycle_count: int
    work_items: list[WorkItem]
    hypotheses: list[Hypothesis]
    exploration: ExplorationState
    cycle_records: list[CycleRecord]
    tool_requests: list[ToolRequest]
    evidence: list[Evidence]
    tool_results: list[ToolResult]
    dependency_decisions: list[DependencyDecision]
    focus_work_item_ids: list[str]
    retained_evidence_ids: list[str]
    final_answer: FinalAnswer | None
    review: ReviewResult | None
    stop_reason: str | None
```

初期実装では、Case、WorkItem、Hypothesis、ExplorationState等を別Repositoryへ分割しない。
安定IDを持たせるが、単一プロセスで不要なrecord revisionやleaseは持たせない。

### 5.1.1 CaseとWorkItemの関係

`Case`は利用者の案件全体である。`parent_work_item_id=None`のWorkItemは案件直下の作業、
子WorkItemは分解された下位作業、確認作業、個別の問いを表す。階層によって型や名称を変えず、
固定階層数も設けない。

```text
Case
├─ WorkItem W1
│  ├─ WorkItem W1-1
│  │  ├─ Hypothesis H1
│  │  └─ Hypothesis H2
│  └─ WorkItem W1-2
└─ WorkItem W2
   └─ WorkItem W2-1
      └─ Hypothesis H3
```

```python
class WorkItem:
    work_item_id: str
    parent_work_item_id: str | None
    question: str
    state: Literal["open", "resolved", "dropped"]
    resolution: str | None
    basis_hypothesis_ids: list[str]
    replaces_work_item_id: str | None

class Hypothesis:
    hypothesis_id: str
    work_item_id: str
    statement: str
    judgment: Literal["supported", "contradicted", "unresolved"]
    evidence_ids: list[str]
    gaps: list[str]
```

- Hypothesisは必ず1つのWorkItemへ所属する。
- Evidenceは複数HypothesisからIDで共有参照し、WorkItemごとに複製しない。
- Hypothesisのstatementを別の意味へ上書きしない。見立てを変更する場合は新しいHypothesisを作る。
- WorkItemのquestionを別の問いへ上書きしない。問いを変更する場合は旧WorkItemを`dropped`にし、
  `replaces_work_item_id`を持つ新しいWorkItemを作る。
- 親子関係は作業分解を表す。WorkItem間に別の依存関係Graphを作らず、次の対象は
  Solverが`focus_work_item_ids`で指定する。

### 5.1.2 再帰探索に強い案件内データ構造

WorkItemの親子構造と、検索対象の探索構造を混在させない。WorkItemは問いの分解を表す木、
ExplorationStateは情報源の発見・取得・展開を表す案件内Graphとする。

探索対象は純粋な木にしない。同じResourceが検索と複数のGraph関係から発見されること、相互参照で
循環することがあるため、Resourceは案件内で1 Nodeに正規化し、発見経路は複数Linkとしてすべて残す。

```text
Case
├─ WorkTree                         問いの分解
│  ├─ WorkItem W1
│  │  ├─ Hypothesis H1
│  │  └─ Hypothesis H2
│  └─ WorkItem W2
│     └─ Hypothesis H3
│
├─ ExplorationState                情報探索の正本
│  ├─ nodes[resource_id]            同じ対象は1 Node
│  ├─ links[link_id]                発見経路は複数保存
│  ├─ frontier                     次にLLMが選べる未処理候補
│  ├─ fetched_node_ids             nodesから導出する本文取得済み集合
│  └─ expanded_scope_keys          expansionsから導出するGraph展開済み集合
│
└─ CycleRecord[]                   仮説・探索方針ごとの試行
   └─ StepRecord[]                 action-observationごとのcheckpoint
```

同じArticleが複数経路から発見される例:

```text
OpenSearch ──L1──→ A ──L2──→ B ──L4──→ D
                       └L3─→ C ──L5──→ D
                                      └L6──→ A  （循環Linkは保存）

nodes = {A, B, C, D}                 DとAを複製しない
links = {L1, L2, L3, L4, L5, L6}    別経路と循環は失わない
展開 = 各Node・各scopeを成功後1回       Link保存と再展開防止を分離する
```

最初に発見した親や最小hopから表示用の探索木を派生できるが、その木を正本にしない。
法令GraphのLinkが既存Nodeへ戻ってもLinkは保存し、本文取得と同一scopeのGraph展開は重複実行しない。

Framework側はResourceとLinkの一般形だけを持ち、`Article`、`REFERENCES`等はLegal Domain Packの
`resource_kind`と`relation_metadata`へ置く。

設計根拠は、一般的なGraph探索の`frontier + explored set`、Neo4jのnode/path単位のuniqueness、
状態ful agentのstep checkpoint、データ来歴のEntityとDerivationの分離である。外部Frameworkへの
依存は追加せず、この案件に必要な最小型だけを実装する。

- [Generic graph search: frontier](https://artint.info/3e/html/ArtInt3e.Ch3.S4.html)
- [Multiple-path pruning: explored set](https://artint.info/3e/html/ArtInt3e.Ch3.S7.html)
- [Neo4j APOC: BFS/DFS、depth、uniqueness](https://neo4j.com/docs/apoc/current/graph-querying/expand-paths-config/)
- [LangGraph persistence: step checkpoint](https://docs.langchain.com/oss/python/langgraph/persistence)
- [W3C PROV: entity、activity、derivation](https://www.w3.org/TR/prov-primer/)

```python
class ExplorationState:
    nodes: list[ExplorationNode]
    links: list[DiscoveryLink]
    frontier: list[FrontierItem]

class ExplorationNode:
    exploration_node_id: str
    resource_id: str
    resource_kind: str
    minimum_depth: int
    discovered_cycle: int
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]
    evidence_ids: list[str]
    related_hypothesis_ids: list[str]
    expansions: list[ExpansionSlice]

class DiscoveryLink:
    link_id: str
    from_node_id: str | None
    to_node_id: str
    discovery_kind: Literal["search", "relation"]
    navigation_evidence_id: str
    relation_metadata: dict
    discovered_cycle: int

class ExpansionSlice:
    scope_key: str
    policy_version: str
    relation_types: list[str]
    directions: list[str]
    status: Literal["not_started", "pending", "partial", "complete", "failed", "timeout"]
    page_request_ids: list[str]
    next_cursor: str | None
    discovered_link_ids: list[str]

class FrontierItem:
    frontier_item_id: str
    node_id: str
    via_link_ids: list[str]
    work_item_id: str
    hypothesis_id: str
    minimum_depth: int
    review_status: Literal["unreviewed", "selected", "relevant_deferred", "rejected"]
    last_reviewed_cycle: int | None
    last_reviewed_step: int | None

class FrontierDecision:
    frontier_item_id: str
    action: Literal["select", "defer", "reject"]
    reason: str

class GraphReviewItem:
    frontier_item_id: str
    review_trigger: Literal["new_frontier", "re_adopted", "new_link"]
    prior_review_status: Literal["selected", "relevant_deferred", "rejected"] | None
    link_ids: list[str]

class GraphReviewLedgerItem:
    frontier_item_id: str
    node_id: str
    article_id: str
    work_item_id: str
    hypothesis_id: str
    review_status: Literal["selected", "relevant_deferred", "rejected"]
    reason: str
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]
    last_reviewed_cycle: int
    last_reviewed_step: int

class CycleRecord:
    cycle_no: int
    phase: Literal["planned", "running", "completed"]
    goal: str
    strategy: str
    completion_criteria: list[str]
    focus_work_item_ids: list[str]
    focus_hypothesis_ids: list[str]
    frontier_before_ids: list[str]
    steps: list[StepRecord]
    fetched_resource_ids: list[str]
    budget_stop_reason: Literal["resource_limit", "step_limit", "time_limit"] | None
    completion_reason: str | None
    frontier_after_ids: list[str]

class StepRecord:
    step_no: int
    phase: Literal["planned", "observed", "completed"]
    tool_request_ids: list[str]
    observed_evidence_ids: list[str]
    applied_update: CaseUpdate | None
    frontier_decisions: list[FrontierDecision]
```

探索規則:

1. OpenSearch候補を深さ0のNodeとfrontierへ追加する。
2. Solverは既知frontier IDから、1 stepで検証する少数の`select`、関連するが今回の
   本文取得枠に入れない`defer`、現在のHypothesisに不要な`reject`を返す。
3. Decisionに現れないfrontierは削除せず`unreviewed`のまま残す。`defer`は
   `relevant_deferred`として同じCycleの後続stepまたは次Cycleへ残す。
4. ProgramはID、件数、depth、Toolの成功済み重複だけを検証し、関連度や優先度を計算しない。
5. 本文取得に付随する1ホップGraphは同じstepの観察へ追加する。新しい隣接Node本文は同じCycleの
   次step以降に取得する。
6. 同じNodeへ別Linkが追加された場合はLinkとHypothesisの関連だけを追加し、成功済み本文を再取得しない。
   frontierは`Node × Hypothesis`単位にし、あるHypothesisでの`reject`を別Hypothesisへ波及させない。
7. Graph展開済み判定はNode全体ではなく`scope_key=(resource_id, relation types, directions, policy version)`単位にする。
   page cursorとrequest IDは同じExpansionSliceへ蓄積し、pageごとに別scopeを作らない。
8. `partial`と`next_cursor`があるscopeを`complete`として扱わない。未提示候補の不存在を推測しない。
9. `max_exploration_depth`はProfileで`1`または`2`だけを許可する。OpenSearch起点を深さ0、Graph関係を
   1辺たどるごとに深さを1増やす。最大depthのNodeは本文取得とSolverの意味評価を許可するが、そこを
   起点とするGraph展開は実行しない。Programは`minimum_depth < max_exploration_depth`の場合だけ
   自動1ホップGraphを連動する。
10. 後から短い経路が見つかった場合、ProgramはNodeと対応frontierの`minimum_depth`だけを小さく更新し、
    過去LinkやCycleRecordを削除しない。
11. `max_exploration_depth`はCase全体に適用する。同じOpenSearch起点からの探索を次Cycleへ引き継いでも
    depthを0へ戻さない。次Cycleの異なる検索で新たに発見したOpenSearch候補だけを新しい深さ0の起点にする。
12. 1 stepの選択件数と1回のGraph取得件数はProfileの機械的上限とし、上限超過候補は削除せず
    `partial`なExpansionSliceの未取得page、または未処理frontierとして残す。Neo4jから取得済みの
    未処理Graph frontierは決定的に分割し、未提示pageを不存在と扱わない。
13. Graph Reviewは全履歴を毎回再評価せず、新しい`unreviewed`候補、新Hypothesisが既存Nodeを
    再採用したことで新たに作られた`Node × Hypothesis` frontier、既評価frontierへ新しいLinkが
    追加された差分を詳細入力とする。過去の評価済みfrontierは短い台帳で参照する。
14. 一度`reject`したfrontierをProgramが別Hypothesisへ自動転用しない。Solverが別Hypothesisの
    検証に再採用した場合は、同じNodeを参照する新しい`unreviewed` FrontierItemを作る。

設定値ごとの到達範囲は次のとおり。

| `max_exploration_depth` | 取得・評価できる範囲 | Graph展開できる起点 |
|---|---|---|
| `1` | 深さ0、1の本文 | 深さ0だけ |
| `2` | 深さ0、1、2の本文 | 深さ0、1 |

案件内探索GraphをNeo4jへ書き戻さない。Neo4jは共有法令Graph、ExplorationStateはCaseStoreに属する
案件固有の探索履歴である。CaseStoreには全Node・Link・FrontierDecisionを保持するが、
Graph ReviewのPromptに過去の評価済み候補と全Linkを毎回再提示しない。次の2投影を分ける。

- `graph_review_batch`: 新規`unreviewed`、新Hypothesisで再採用した候補、既評価候補へ新たに
  取得したLinkの差分。Article ID、法令名、条番号・見出し、起点、対応WorkItem・Hypothesis、
  当該候補について今回までに判明した全relation属性、`review_trigger`、直前のreview statusを含める。
- `graph_review_ledger`: 過去のSolver判断の短い台帳。評価済みFrontier ID、Node ID、Article ID、
  対応WorkItem・Hypothesis、`selected / relevant_deferred / rejected`、短い前回理由、content status、
  最終Review cycle/stepを含める。全Link詳細と過去のLLM生応答は含めない。

```text
CaseStore（正本: 全Node・Link・判断履歴）
             |
             +-- graph_review_batch
             |     新規候補 / 再採用候補 / 新Link差分 + 判断に必要な関係情報
             |
             +-- graph_review_ledger
                   全評価済み候補の短い最新状態
                              |
                              v
                    SolverのGraph Review
                    select <= 3 / defer / reject
                              |
                              v
                 DecisionをCaseStoreへ追記し最新状態を更新
```

同じfrontierを再評価した場合も過去のFrontierDecisionは削除せず、ledgerには最新Decisionだけを投影する。
`selected`はSolverが本文取得対象に選んだ意味判断であり、本文取得が成功した意味ではない。
取得の成否は別の`content_status`で表す。

`graph_review_batch`がProfile上限を超える場合は、Programが安定順で機械的にpage分割し、
全pageの未評価状態とcursorを保持する。Programは関連度で候補を選ばず、未提示候補を
`reject`または不存在と扱わない。同じArticleに複数Linkがある場合は、当該Review batchでは
全Linkの短い情報を併記する。ハッシュ化Evidence IDだけを示してLLMが候補を識別できない状態を禁止する。

### 5.2 statusを少数に保ち、意味と決定主体を固定する

statusは「実行事実」と「意味判断」を分離する。同じ文字列を別の軸へ流用せず、LLMへ見せる値は
7.3の共通Prompt語彙を必ず合成する。JSON Schemaの`enum`は形式制約であり、意味定義の代わりにしない。

| 対象 | 値 | 決定者 |
|---|---|---|
| Run | `running / completed / failed / cancelled` | プログラム |
| ToolResult | `succeeded / failed / timeout` | プログラム |
| Cycle phase | `planned / running / completed` | プログラム |
| Step phase | `planned / observed / completed` | プログラム |
| Resource本文 | `not_requested / pending / succeeded / failed / timeout` | プログラム |
| Graph expansion | `not_started / pending / partial / complete / failed / timeout` | プログラム |
| Frontier review | `unreviewed` | 新規`Node × Hypothesis`からプログラムが初期化 |
| Frontier review | `selected / relevant_deferred / rejected` | Solver |
| Cycle budget flag | `cycle_budget_reached / cycle_close_required / cycle_step_timeout` | プログラム |
| Solverの次動作 | `continue_cycle / start_next_cycle / finalize` | Solver |
| WorkItem | `open / resolved / dropped` | Solver |
| Hypothesis | `supported / contradicted / unresolved` | Solver |
| Frontier action | `select / defer / reject` | Solver |
| Deferred Frontier resolution | `fetch_next_cycle / carry_forward / no_longer_needed / unresolved_at_limit` | Solver |
| 外部依存の確認 | `not_required / needs_action / resolved` | Solver |
| Review | `accept / revise` | Reviewer |

機械的statusの意味:

| 対象・値 | 意味 |
|---|---|
| ToolResult `succeeded` | Tool呼出しが正常終了した。内容の関連性、正しさ、仮説支持を意味しない |
| ToolResult `failed` | Toolがエラー終了した。対象の不存在を意味しない |
| ToolResult `timeout` | 制限時間内に完了しなかった。対象の不存在を意味しない |
| Cycle `planned` | goal、探索方針、完了・失敗条件を保存済みで、最初のstep開始前 |
| Cycle `running` | 同じ仮説・探索方針でaction-observation stepを反復中 |
| Cycle `completed` | Cycle全体の評価差分と終了理由を適用済み。次Cycle開始または最終化が可能 |
| Step `planned` | ToolRequestを保存済みで、全結果の観察前 |
| Step `observed` | 当該stepの全ToolResultを保存済みで、Solverの意味評価前 |
| Step `completed` | Solverの評価差分とfrontier更新を適用済み |
| content `not_requested` | 本文取得をまだ要求していない |
| content `pending` | 本文ToolRequestを保存済みで、終端ToolResultが未保存 |
| content `succeeded` | 本文取得に成功した。質問との関連性や根拠採用を意味しない |
| content `failed / timeout` | 本文取得が失敗または時間切れ。Article不存在を意味しない |
| expansion `not_started` | 当該scopeのGraphをまだ要求していない |
| expansion `pending` | Graph ToolRequestを保存済みで、終端ToolResultが未保存 |
| expansion `partial` | 一部候補だけ取得し、`next_cursor`または未取得範囲が残る |
| expansion `complete` | 当該scopeの取得を完了した。隣接本文の確認完了を意味しない |
| expansion `failed / timeout` | 当該scopeのGraph取得が失敗または時間切れ。関係不存在を意味しない |
| `cycle_budget_reached` | Cycleの本文取得数または他の機械的上限に達し、新しいactionを追加できない |
| `cycle_close_required` | 予約時間を保護するため新しいactionを始めず、現Cycleの終了評価が必要 |
| `cycle_step_timeout` | Cycle予算で短縮された中間呼出しが時間切れ。仮説の否定やprovider障害は意味しない |

Solverが決める意味status・action:

| 対象・値 | 意味 |
|---|---|
| `continue_cycle` | 同じCycleの仮説・探索方針を維持し、次のaction-observation stepへ進む |
| `start_next_cycle` | 現Cycleの方針を変更する必要、またはCycle予算境界を理由付きで評価して閉じ、次に検証する命題・方針で次Cycleを始める |
| `finalize` | 必要な根拠を探し切ったと判断し、新しいToolを実行せず回答を確定する |
| WorkItem `open` | 問いが未完了で、追加作業が必要 |
| WorkItem `resolved` | 問いへ結論が出ており、`resolution`に結論を持つ |
| WorkItem `dropped` | 前提否定、重複、質問との無関係により対象外とし、`resolution`に理由を持つ |
| Hypothesis `supported` | 今回提示されたgrounding Evidenceがstatementを支持する |
| Hypothesis `contradicted` | 今回提示されたgrounding Evidenceがstatementを否定する |
| Hypothesis `unresolved` | 根拠不足、両義的、未取得で真偽を確定していない |
| Frontier `select` | この候補を次の仮説検証行動へ採用する |
| Frontier `defer` | 現在の質問・Hypothesisに関連するが、今回の本文取得枠に入れず後続Cycle候補として保留する |
| Frontier `reject` | 現在の質問・Hypothesisには関係しないと判断し、理由付きで候補から外す |
| `fetch_next_cycle` | 保留候補を次Cycle最初の本文取得に含める |
| `carry_forward` | 取得上限等によりactive候補のまま次Cycle以降へ保持する |
| `no_longer_needed` | 後続Evidenceを踏まえ、質問への回答には不要と判断する |
| `unresolved_at_limit` | 新しいCycleを開始できない上限時に未確認として残し、limitationsへ示す |

`supported`はWorkItem全体の完了を意味せず、`content=succeeded`や`expansion=complete`から
プログラムが自動生成してはならない。Decisionに現れないfrontierは`reject`と解釈しない。

次は導入しない。

- ClaimとHypothesisに別々の類似statusを持たせること
- `partial`を複数の意味で使うこと
- 実行timeoutを`insufficient`へ変換すること
- Reviewerの`needs_research`
- プログラムがLLMの`finalize`を意味上の理由で`continue_cycle`へ変更すること
- `content=succeeded`を`Hypothesis=supported`へ読み替えること
- `expansion=complete`を隣接Article本文の確認済みへ読み替えること

部分的にしか確認できていない場合は、対象を複数のHypothesisへ分け、
確認できたものを`supported`、未確認のものを`unresolved`として明示する。

### 5.3 Solver契約

SolverはCaseState全体を再出力しない。追加と更新の差分だけを返し、出力に現れなかった
WorkItem、Hypothesis、EvidenceをCaseStateから削除しない。

```python
class CaseUpdate:
    add_work_items: list[WorkItem]
    update_work_items: list[WorkItemUpdate]
    add_hypotheses: list[Hypothesis]
    update_hypotheses: list[HypothesisUpdate]
    impact_decisions: list[WorkItemImpactDecision]

class WorkItemImpactDecision:
    work_item_id: str
    action: Literal["retain", "replace", "drop"]
    reason: str
    new_basis_hypothesis_ids: list[str]
    replacement_work_item_id: str | None
    drop_subtree: bool = False

class DependencyDecision:
    dependency_kind: str
    work_item_id: str
    status: Literal["not_required", "needs_action", "resolved"]
    reason: str
    source_evidence_ids: list[str]
    action: Literal["discover_source", "assess_source", "discover_target", "fetch_target"] | None
    action_request_id: str | None
    target_article_ids: list[str]
    evidence_ids: list[str]

class CyclePlan:
    goal: str
    strategy: str
    completion_criteria: list[str]
    focus_work_item_ids: list[str]
    focus_hypothesis_ids: list[str]

class FrontierReAdoption:
    node_id: str
    work_item_id: str
    hypothesis_id: str
    reason: str

class DeferredFrontierResolution:
    frontier_item_id: str
    article_id: str
    work_item_id: str
    hypothesis_id: str | None
    action: Literal["fetch_next_cycle", "carry_forward", "no_longer_needed", "unresolved_at_limit"]
    reason: str

class SolverDecision:
    next: Literal["continue_cycle", "start_next_cycle", "finalize"]
    cycle_plan: CyclePlan | None
    cycle_completion_reason: str | None
    update: CaseUpdate
    next_focus_work_item_ids: list[str]
    retain_evidence_ids: list[str]
    frontier_decisions: list[FrontierDecision]
    frontier_re_adoptions: list[FrontierReAdoption]
    deferred_frontier_resolutions: list[DeferredFrontierResolution]
    tool_requests: list[ToolRequest]
    dependency_decisions: list[DependencyDecision]
    answer: str | None
    citation_ids: list[str]
    limitations: list[str]
```

主な整合条件は次のとおり。

- `CycleRecord`は同一Caseで同時に1件だけ`planned`または`running`になり、それより後のCycleを開始しない。
- Active Cycleがない状態の`continue_cycle`では`cycle_plan`を必須とする。Active Cycleがない状態の
  `start_next_cycle`は拒否し、Active Cycle内の`continue_cycle`では現在のgoal・strategyを別の意味へ上書きしない。
- `StepRecord planned → observed`は全ToolRequestに終端ToolResultが保存された場合だけ許可する。
- `StepRecord observed → completed`はSolverのCaseUpdateとFrontierDecisionを適用した場合だけ許可する。
- Active Cycleがある状態の`start_next_cycle`と`finalize`では`cycle_completion_reason`を必須とし、Cycle全体の
  評価差分適用と`research_cycle_count`更新を同時に行う。
- FrontierDecisionは、そのCycle開始時または各stepの観察結果で追加された既知frontierと、対応する
  WorkItem・Hypothesisだけを参照する。未言及frontierは状態を変えない。
- FrontierDecisionの`select / defer / reject`は、frontierのreview statusをそれぞれ
  `selected / relevant_deferred / rejected`へ更新する。`select`後の本文取得成否は`content_status`だけで更新し、
  review statusへ成功・失敗を混在させない。`selected + succeeded/pending`の重複取得は拒否する。
- `graph_review_batch`内の新規・再採用・新Link差分は全件にFrontierDecisionを必須とする。
  ledgerだけにある`relevant_deferred`、または本文取得の再試行が必要な`selected + failed/timeout`は
  `select`できるが、再度`defer/reject`して過去判断を上書きする対象にはしない。
- FrontierReAdoptionは`graph_review_ledger`に示された既知Nodeと、既知のopen WorkItem・Hypothesisを
  Solverが理由付きで結び直す。Programはその参照整合だけを検証し、新しい`unreviewed` FrontierItemを作る。
  `rejected`の候補を別Hypothesisへ自動転用しない。
- `content_status`とExpansionSliceのstatusは対応するToolRequest・ToolResultからだけ更新し、
  SolverDecisionから変更させない。
- `continue_cycle`では1件以上のToolRequestまたは、Profileが決定的なToolRequestへ変換できる
  1件以上の`select` FrontierDecisionを持つ。Graph Reviewで選択がなく、未提示pageもない場合は
  通常integrationのCycle終了・追加検索・完了判断へ戻す。
- `start_next_cycle`は現在Cycleを閉じる評価と次の`cycle_plan`を持つ。次Cycleの最初のToolRequestを
  同じDecisionに含めてもよいが、保存上は前Cycleを閉じてから次Cycleと最初のStepを作る。
- Cycle境界で未評価Graph候補だけを引き継ぐ場合は、ToolRequestなしの`start_next_cycle`を許す。
  次Cycleの取得枠を確立してから差分Graph Reviewを行い、枠0の状態で全候補へdeferを強制しない。
  `remaining_unreviewed_count > 0`のCycle境界では`UnreviewedGraphResolution`を必須とする。
  `review_next_cycle`は`continue + start_next_cycle`、`no_longer_needed`は通常finalize、
  `unresolved_at_limit`は次Cycle不能の限定finalizeだけに対応させる。候補の必要性はSolverが判断し、
  Programは候補数が残る事実、actionと次動作の組合せだけを検証する。
- `start_next_cycle`または`finalize`では、本文未取得のactiveな`relevant_deferred`全件に
  `DeferredFrontierResolution`をちょうど1件ずつ持つ。Programは既知frontier・Article・WorkItem・Hypothesis
  の完全一致、全件性、actionと次動作の参照整合だけを検証し、法的な必要性や理由の妥当性は検証しない。
- `fetch_next_cycle`は`start_next_cycle`と同じDecisionで選び、Programが既知Article IDを
  次Cycle最初の一括本文取得へ機械転記する。Solverは同じToolRequestを二重指定しない。
  `carry_forward`は`start_next_cycle`を必要とするが、同じDecisionの本文取得枠には含めずactive候補として残す。
  `unresolved_at_limit`は新しいCycleを開始できない最終化時だけ許可し、limitationsを必須とする。
- `finalize`ではToolRequestを持たず、回答を持つ。通常finalizeでは全WorkItemをclosedにし、
  `limitations / unresolved_work_item_ids / unresolved_hypothesis_ids`を空にする。上限等により次Cycleを
  開始できない限定finalizeでは、未完了WorkItemをopen、Hypothesisをunresolvedのまま保ち、
  limitationsと両ID欄を相互参照させる。一般的な注意書きはlimitationsではなく回答本文へ記載する。
- 取得済み情報だけで最初から`finalize`する場合はCycleを作らず、`research_cycle_count`も増やさない。
- 新規IDはCase内で一意、更新IDは既知でなければならない。
- WorkItemの親IDは同じCaseに存在し、親子関係を循環させない。
- HypothesisのWorkItem ID、WorkItemのbasis Hypothesis IDは同じCaseに存在する。
- ToolRequestは既知WorkItemと、当該CaseのHypothesisを参照する。
- `supported`または`contradicted`のHypothesisは既知Evidence IDを1件以上参照する。
- `next_focus_work_item_ids`は既知の`open` WorkItemだけを参照する。
- `retain_evidence_ids`は既知Evidenceだけを参照し、Profileの件数上限を超えない。
- Profileが外部依存種別を要求する場合、Tool実行後の各判断では、判断開始時に`open`だった
  WorkItemごとに同種別のDependencyDecisionをちょうど1件持つ。
- 全状態で、判断対象になった依存元の提示済みEvidence IDを`source_evidence_ids`へ持つ。
- `needs_action`は`discover_source / assess_source / discover_target / fetch_target`と同じSolverDecision内の
  ToolRequest IDを参照する。`fetch_target`は一括Request中の委任先を`target_article_ids`へ明示する。
  `resolved`は確認済みの委任先Articleと、今回利用可能なgrounding Evidence IDを参照する。
  `not_required`は依存元と不要理由だけを持つ。
- LLMが参照できるのは、当該呼び出しへ提示されたIDだけとする。
- 検証違反は`protocol_error`であり、プログラムが意味statusを書き換えて補正しない。

子WorkItemがすべて終了しても、プログラムは親を自動的に`resolved`へしない。Solverが親を
`resolved`へする場合、残る子を同じCaseUpdateで`resolved`または`dropped`へし、resolutionを返す。
プログラムは親が終了しているのに`open`な子が残る構造を契約違反として拒否するだけで、
子の結論や破棄を決めない。

### 5.3.1 仮説が反証された場合

Hypothesisが`contradicted`になったとき、プログラムは`basis_hypothesis_ids`の完全一致から、
影響を受けるWorkItemとその子孫IDを列挙してSolverへ返す。プログラムはそれらを自動的に
終了・変更しない。

Solverは「そのWorkItemの`question`を変えずに、引き続き親の問いを解くために使えるか」で判断する。
仮説、検索語、検索先、根拠候補が誤っていただけならWorkItemを置き換えず、新しいHypothesisや
ToolRequestを追加する。観点が不足していた場合も、既存WorkItemを置き換えず子または兄弟WorkItemを追加する。
`replace`は、WorkItemの`question`自体を別の意味へ変えなければ親の問いに寄与できない場合だけに使う。
質問との無関係または重複が根拠から判明した場合だけ`drop`する。

```text
観察結果を評価する
  ├─ 問いは有効で、仮説だけが外れた       → WorkItemをretainし、新Hypothesisを追加
  ├─ 問いは有効で、探索方法だけが外れた   → WorkItemをretainし、ToolRequestを変更
  ├─ 必要な観点が不足していた             → 子または兄弟WorkItemを追加
  ├─ 問い自体を別の意味へ変える必要がある → 旧WorkItemをreplace
  └─ 問いが無関係または重複               → WorkItemをdrop
```

親WorkItemを`replace`する場合、初期実装では旧部分木の子を新しい親へ付け替えない。Solverが旧部分木の
各open WorkItemを明示的に`drop`するか、`drop_subtree=true`を返し、新しい親子WorkItemを別IDで作る。
これにより旧分解を履歴として保持しながら、親が閉じているのにopenな子が残る状態を避ける。

局所的なHypothesis追加、検索語変更、WorkItem追加で現在のCycle goal・strategyを維持できるなら
`continue_cycle`を選ぶ。初期分解の主要部分が質問を覆っていない、中心Hypothesisの反証で現在の
作業構造が成立しない、検索起点・法令階層の前提を変える必要がある、またはCycle取得枠が尽きても
必要と判断した未取得Evidenceが残る場合に`start_next_cycle`を選ぶ。Tool終了やGraphの1ホップ完了
だけを理由にしない。

Solverは影響を受けるWorkItemごとに、次を明示する。

| action | 意味 |
|---|---|
| `retain` | 作業は依然必要。反証された前提を外すか、別Hypothesisへ付け替える |
| `replace` | 旧WorkItemを`dropped`にし、別IDの新WorkItemへ置き換える |
| `drop` | 作業が不要。必要ならSolverの`drop_subtree=true`指示で子孫も終了する |

`replace`ではWorkItemのquestionを上書きしない。新WorkItemを`add_work_items`へ含め、
旧IDを`replaces_work_item_id`で参照する。`drop_subtree`もSolverの明示指示であり、
プログラムは指定された部分木を機械的に更新するだけである。

同じCaseUpdateで新たに`contradicted`となるHypothesisを前提にした`open` WorkItemがある場合、
Solverは各WorkItemの`impact_decisions`を必ず返す。プログラムは全IDが処理対象になっているか、
`retain`後のbasisから反証Hypothesisが外れているか、`replace`先が新規WorkItemとして存在するか、
`drop`対象が終了状態になるかだけを検証する。action自体は選ばない。

反証されたHypothesis、反証Evidence、droppedになったWorkItemはCaseStateから削除しない。
同じ誤った見立てを後のサイクルで繰り返さないため、全体案内に簡潔に残す。

### 5.4 Tool契約

```python
class ToolRequest:
    request_id: str
    work_item_id: str
    tool_name: str
    arguments: dict
    purpose: str
    hypothesis_ids: list[str]

class ToolResult:
    request_id: str
    status: Literal["succeeded", "failed", "timeout"]
    evidence_ids: list[str]
    error_code: str | None
    elapsed_ms: int
```

ToolRequestは実行前にCaseStateへ保存し、ToolResultの`request_id`は既知のToolRequestと完全一致させる。
これによりToolResultから検証対象WorkItemとHypothesisを必ず逆引きできる。
ToolResultは実行事実だけを表す。検索候補が法的に重要か、条文間関係が成立するかはSolverが判断する。

### 5.5 別サイクルへの引継ぎ

CaseStoreへの保存と、次のPromptへ本文を載せることを分ける。CaseStoreには全WorkItem、
Hypothesis、ExplorationState、CycleRecord、StepRecord、ToolResult、Evidenceを残す。次のSolverへは、次の4層を渡す。

| 層 | 引き継ぐ内容 | 選択方法 |
|---|---|---|
| Case | 質問、制約、cycle・step・Tool・時間の残量 | 常に全部 |
| WorkTree | 全WorkItemのID、親ID、question、state、Hypothesis・Evidence件数 | 常に全部を簡潔に表示 |
| Exploration | 現Cycleのgoal・strategy、直前Step、新規・再採用・新Link Graph候補の差分batch、過去の全評価済みfrontierの短いledger、focusへ接続するNode・Link、depth、本文・展開status | 全履歴はCaseStoreに保持し、Promptには未評価・再評価差分と、再採用に必要な短い評価台帳だけを決定的に投影 |
| Focus detail | Solver指定WorkItem、反証の影響を受けるWorkItem、そのHypothesis、直前の全ToolResultと新規Evidence本文、保持Evidence本文 | focusと保持対象はSolver、影響対象はbasis ID完全一致で展開 |

直前のTool実行で新しく得たToolResultの実行状態とEvidence本文は、次のSolver判断へ必ず一度渡す。
そこでSolverが`retain_evidence_ids`へ選んだEvidenceは、その後のサイクルでも本文を渡す。
その他の非Graph Evidenceは削除せず、ID、出典、見出し、サイズ、取得cycleをmanifestとして毎回示す。
Graph navigation Evidenceはmanifestへ重複表示せず、候補情報は後述のArticle・LinkだけでSolverへ示す。
元のEvidence IDと来歴を必要とする監査はCaseStateを参照する。
Evidence本文に使える文字数はProfileの`max_material_evidence_chars`で制御し、Legal Profileの初期値は
50,000文字とする。この本文枠だけをGraph候補の可視性保証には使わない。

Graph navigation EvidenceはArticle IDごと1件の`graph_candidate_catalog.articles`と、
起点から候補への全発見経路を保持する`graph_candidate_catalog.links`へ正規化し、
CaseStoreで全件を正本として保持する。SolverContextはそこから`graph_review_batch`と
`graph_review_ledger`を決定的に差分投影する。同じ候補を複数起点から発見した場合、
Article情報は1件に正規化し、LinkはCaseStoreにすべて保持する。Review batchで評価対象になった
Articleについては、その判断に必要な全Linkを同じbatchへ投影する。
Graph navigation EvidenceのJSON・Evidence IDは`material_evidence`、`evidence_manifest`、
`recent_tool_results.evidence_ids`、`navigation_evidence_ids`、`omitted_evidence_ids`へ重複掲載しない。
Graph ToolResultは実行status、件数、catalog投影済みであることだけをSolverへ示す。
CaseStore上のEvidence、ToolResult、生成元・監査用の関係来歴は正本として完全に残す。

```python
class GraphCandidateCatalog:
    articles: list[GraphCandidateArticle]
    links: list[GraphCandidateLink]

class GraphCandidateArticle:
    article_id: str
    document_id: str | None
    title: str | None
    heading: str | None
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]

class GraphCandidateLink:
    seed_article_id: str
    candidate_article_id: str
    work_item_ids: list[str]
    hypothesis_ids: list[str]
    relations: list[dict]  # kind, edgeType, direction, status, referenceKind

class SolverToolResult:
    request_id: str
    status: Literal["succeeded", "failed", "timeout"]
    evidence_ids: list[str]       # Graph navigation Evidence IDは載せない
    evidence_count: int
    graph_projection_updated: bool
    error_code: str | None
    elapsed_ms: int
```

Articleの同一性は`article_id`、Linkの同一発見経路は
`(seed_article_id, candidate_article_id)`を決定的な正規化単位とする。
同じ組に複数のrelationがある場合は、`kind`、`edgeType`、`direction`、`status`、`referenceKind`の
異なる値を失わず`relations`へ保持する。この正規化は同一IDと関係属性の機械的統合であり、
どのLinkが質問に関係するかをプログラムが判断する処理ではない。

Graphの次pageがまだ取得されていない場合は候補を推測せず、ExpansionSliceの`partial`と`next_cursor`を示す。
探索用Evidence本文を文字数上限で省略しても、今回の`graph_review_batch`と
`graph_review_ledger`に含むArticle ID、法令名、条番号・見出し、minimum depth、content status、review status、
起点Article・Link、relation type・direction・status、ExpansionSliceの`partial / complete`は
Exploration投影へ残す。未提示pageと過去の全詳細はCaseStoreに保持し、件数、cursor、
各Review statusの集計をContextに示す。LLMが当該batchの内容を識別できないハッシュIDだけを
示すことは禁止する。

本文量上限で省略するとき、プログラムは関連度や法令上の重要度で選ばない。新規、Solver保持指定、
固定の文字数上限という決定的な規則だけを使い、省略した非Graph Evidence IDを明示する。Solverは既知IDを
指定する`load_evidence`で本文の再提示を要求できる。

共通Prompt、WorkTree、Graph review batch・ledger、Evidence本文、出力予約を合計した入力が選択modelの
context容量を超える場合、Context BuilderはReview batchを意味的に間引かず、より小さい機械的pageへ
分割する。1候補または1候補の必要Linkだけでも収まらない場合は
`context_capacity_exceeded`で実行を止める。候補を隠してSolverに完了判断させるfallbackは設けない。
通常運用ではGraphのpage上限、step上限、
50,000文字の本文上限によりこの状態を避け、model変更時はProfile読込み時または実行前に入力・出力予約を検証する。

`load_evidence`はCaseStoreに既にあるEvidenceを読む汎用read-only ToolRequestである。
新しい法的判断やEvidenceを生成せず、指定された既知IDの本文を次のSolver判断へ戻すだけとする。

次のサイクルへPrompt全文、過去のLLM生応答、運用ログを引き継がない。構造化された現在の
CaseStateと変更理由を正本にする。CaseUpdateに現れなかった別系統のWorkItemや未完了WorkItemも、
WorkTree案内とCaseStoreには残る。

各StepのToolResultは直後のSolver判断へ必ず渡す。最後の許可StepでもHypothesis、WorkTree、frontierを
更新してからCycleを閉じ、未評価のToolResultを残したまま次Cycleまたは回答へ進まない。

最初のCycle計画はresearch profileを使う。1回でもToolを実行した後のstep判断、次Cycleへの再計画、
Reviewer差戻し後の判断はintegration profileを使い、直前結果の意味評価、状態更新、
`continue_cycle / start_next_cycle / finalize`の選択を同じLLM呼び出しで行う。通常終了のためだけの
独立Integrator呼び出しは設けない。上限到達時は同じintegration profileへ`finalize_only=true`を渡し、
追加ToolRequestだけを禁止する。

`finalize`時は、Solver自身がすべてのWorkItemを`resolved`または`dropped`へ更新する。
プログラムはopen WorkItemが残っていないことだけを検証し、どちらの状態にするか、未確認のまま
終了してよいかは判断しない。Hypothesisは`unresolved`のままでもよく、そのgapsと回答への影響を
Solverがresolutionおよびlimitationsへ明示する。

下位法令・委任先の未確認事項は、通常のWorkItem、Hypothesis、gapsで管理する。
Legal Profileでは`discover_source / assess_source / discover_target / fetch_target`の重複状態を要求しない。
取得本文に質問へ関係する委任があれば、Solverは対応するHypothesisを`unresolved`、WorkItemを`open`の
まま追加調査する。Graph結果だけでは根拠にせず、端点Articleを`fetch_articles`で取得して評価する。
どの委任が質問に関係するか、どの本文で確認できたかはSolverが判断し、プログラムは既知ID、
ToolRequest、grounding Evidenceの参照整合だけを検証する。

Legal Profileは`fetch_articles`を起点とするautomatic read-only Toolとして
`legal_graph_neighbors`を登録する。プログラムはSolverが選んだArticle IDをそのまま転記し、
選択Nodeの`minimum_depth < max_exploration_depth`ならOpenSearch本文取得とNeo4jの1ホップ取得を並列実行する。
選択Nodeが最大depthなら本文だけを取得する。同じArticle・scopeのGraphは成功後に重複実行しない。
取得した1ホップは現在Stepの観察へ加え、各隣接ArticleをExplorationStateのNode・Link・frontierへ保存する。
Solverがfrontierから選んだArticle本文を同じCycleの次stepで取得しても、そのArticleからGraphを再展開しない。
これは探索先の意味判断ではなく、Profileで宣言した決定的なTool連動である。

Solverは`REFERENCES`、`IMPLEMENTS`、`APPLIED_BY`の格納方向と、起点から見た
`incoming / outgoing`を共通Promptの定義どおりに解釈する。候補表示だけで法的結論を出さず、
質問と現在のHypothesisに関係すると判断した既知frontier IDだけを、Profileの少数上限内で次の本文取得へ
`select`する。Graph Reviewの初期選択上限は3件とし、関連するが今回の取得枠に入れない候補は
`defer`として短いledgerへ残す。同じhopの未評価候補や別枝も削除せず、機械的pageまたは
次Cycleへ残す。候補の関連性、取得順、
`reject`はSolver、Node・Linkの重複排除、depth、取得済み判定、Tool実行はプログラムが担当する。

Graphは発見経路の1つであり、必要条文到達の唯一の経路にしない。質問で明示された観点に対応する
open WorkItemが残り、関連するGraph候補がない、最大depthへ達した、または既存のGraph方針を探し切った場合、
SolverはそのWorkItem、確認済み本文の委任・参照表現、法令名、条番号等を基に`legal_search`を要求できる。
その検索結果は新しい深さ0の起点となる。検索語の作成と検索へ切り替える判断はSolverが行い、
プログラムは未解決WorkItemから検索語や必要条文を生成しない。

Solverは`graph_review_batch`に提示された新規・再評価差分と、`graph_review_ledger`の
`relevant_deferred`候補について、質問と現在のHypothesisとの関係を判断する。ledgerの
`selected + failed/timeout`は関連性を再判定せず、取得を再試行するかを判断する。
関係する候補はGraph Reviewの選択上限3件とCycleの残り本文取得枠の小さい方まで`select`し、
上限外は`defer`として次Cycle候補へ残す。関連判断と優先順はSolverが行い、Programは本文取得済み件数と
残り枠を機械的に検証する。関係すると判断した
未確認候補や、明示された質問観点に対応するopen WorkItemを残したまま、通常の`finalize`を選ばない。
実行上限に到達した場合だけ、未確認範囲と回答への影響をlimitationsへ明示して終了する。

Evidence IDとArticle IDは別の名前空間として扱う。Contextは、今回本文を提示し根拠・引用に使える
完全一致IDを`grounding_evidence_ids`、Graph以外の発見用・引用不可Evidence IDを`navigation_evidence_ids`、
本文取得に使えるIDを`fetchable_article_ids`として明示する。SolverはArticle IDからEvidence IDを
組み立てない。プログラムは一覧をmetadataから決定的に展開するだけで、採用対象はSolverが選ぶ。
`legal_search`が返す法令・ガイドの代表chunkも発見用として`navigation_evidence_ids`へ置き、元の
contentUnitIdとは別の`search-nav-*` Evidence IDを付ける。`fetch_articles`で取得した本文だけが
`grounding_evidence_ids`へ入る。これは検索結果の法的関連性をコードが判断する規則ではなく、
「候補検索」と「指定Article本文取得」というToolの取得段階を表す構造契約である。
Graph navigationはEvidence IDをSolverへ再掲せず、`graph_candidate_catalog.articles[].article_id`と
`links[].seed_article_id / candidate_article_id`から`fetchable_article_ids`を決定的に作る。

Model出力は`next`、focus、保持ID、answerをproviderのstructured-output schemaへ直接載せる。
複雑な`update`と`tool_requests`だけを別々のJSON文字列として輸送する。`dependency_decisions`は
Provider structured-output schemaへ直接載せる。Integrationでは判断開始時にopenなWorkItem数を
`minItems / maxItems`へ設定し、空配列や件数不足をProvider段階で拒否する。SolverContextにも
`required_dependency_kind`と`required_dependency_work_item_ids`を明示する。
SolverDecision全体、特に長いanswer本文は二重エンコードしない。Adapterが2項目を復元してからPydanticとAgentLoopの
参照整合検証を適用する。これによりprovider grammarを小さく保ちつつ、長文のescape破損を避ける。

SolverDecisionが参照・件数・状態等の構造契約に違反した場合、そのDecisionはCaseStateへ適用せず、
違反理由と直前Decisionを同じSolver profileへ1回だけ返す。Solverが意味判断を保ったまま構造を
自己修復する。2回目も違反した場合は`protocol_error`とする。プログラムは未知IDを推測補正せず、
上限超過分の根拠やToolRequestを選別せず、WorkItemの終了理由も生成しない。

## 6. Reviewer

### 6.1 既定値

Reviewerはデフォルトで無効にする。

```yaml
reviewer:
  enabled: false
  max_revisions: 1
```

プログラムは回答内容からReviewerの要否を推測しない。Run開始時に解決したProfileの
`reviewer.enabled`だけで経路を決める。

### 6.2 Reviewer契約

Reviewerへ渡すものは次に限定する。

- 利用者の質問
- Solverの最終回答
- Solverが実際に選んだ引用Evidence
- Solverが明示したlimitations

```python
class ReviewResult:
    verdict: Literal["accept", "revise"]
    findings: list[ReviewFinding]
```

Reviewerは追加調査の実行経路を直接選ばない。`revise`では、誤り、根拠不足、引用との不一致を
具体的に返す。Solverがその指摘を読み、回答修正か追加調査かを判断する。

1回の修正後に再確認する場合、Reviewerをもう1回呼ぶ。2回目も`revise`なら
`review_failed`として未承認を明示し、それ以上繰り返さない。

Reviewer有効時にReviewer自体がtimeoutまたは契約違反になった場合も、勝手に`accept`へしない。

## 7. Model ProfileとPrompt

### 7.1 役割ではなく呼び出し用途でモデルを選ぶ

同一provider内で、research、integration、reviewのモデルを別々に設定できるようにする。

```yaml
name: legal-default
provider: anthropic

solver:
  common_system_prompt: domains/legal/prompts/solver_common.md
  research:
    model: claude-haiku-4-5-20251001
    max_output_tokens: 4096
    system_prompt: domains/legal/prompts/solver_research.md
  integration:
    model: claude-haiku-4-5-20251001
    max_output_tokens: 4096
    system_prompt: domains/legal/prompts/solver_integration.md

reviewer:
  enabled: false
  model: claude-haiku-4-5-20251001
  max_output_tokens: 4096
  system_prompt: domains/legal/prompts/reviewer.md
  max_revisions: 1

limits:
  max_research_cycles: 4
  max_fetched_resources_per_cycle: 4
  max_steps_per_cycle: 4
  max_total_steps: 8
  max_tool_requests_per_step: 4
  max_parallel_tools: 4
  max_selected_frontier_per_step: 3
  max_graph_candidates_per_scope_page: 20
  max_graph_candidates_per_review_batch: 20
  max_exploration_depth: 1
  max_material_evidence_chars: 50000
  max_solver_input_chars: 240000
  max_retained_evidence: 12
  cycle_close_reserve_sec: 15
  min_next_cycle_budget_sec: 25
  finalization_reserve_sec: 35
  max_wall_time_sec: 180
```

`solver_common.md`はresearchとintegrationの両方へ合成する。質問観点、法令階層・委任先追跡、
Evidence利用、Cycle・Stepの意味、完了条件のようにサイクル間で変わらない規則を段階別Promptへ重複記載しない。
`solver_research.md`は初回の作業分解と発見、`solver_integration.md`は観測結果の反映と次の
行動または完了の選択だけを追加する。

モデルID、token上限、timeout、Reviewer有効・無効はProfileだけで変更する。
AgentLoopや法令ツールへmodel IDをハードコードしない。

`limits.max_exploration_depth`はFrameworkのProfile契約として整数`1`または`2`だけを受け付けるが、
Legal Profileは`1`に固定する。未設定、`0`、`3`以上、整数以外はProfile読込み時の設定エラーとし、実行中に丸めたり既定値へ
補正したりしない。設定値はRun開始時に解決してCaseへ固定し、途中のProfile変更で進行中Caseの
到達範囲を変えない。

Graph pageの上限は意味的な枝刈りではなく、Neo4jから1回に取得する機械的な件数上限である。Programは
`minimum_depth`、`discovered_cycle`、`frontier_item_id`の安定順で候補を保存する。Graph page上限に
達したExpansionSliceは`partial`として`next_cursor`を残し、まだ取得していない候補の不存在を推測しない。
取得済みGraph候補はCaseStoreから落とさない。SolverContextへは新規・未評価・再採用の
`graph_review_batch`と、過去の全評価済みfrontierを短く表す`graph_review_ledger`を載せる。
`max_graph_candidates_per_review_batch`は意味的な省略ではなく、全候補を差分Reviewに通すための
機械的pageサイズである。`max_material_evidence_chars`はArticle本文などのEvidence本文だけに適用する。
Programは候補の関連度や法令上の優先度を計算せず、上限外候補を`reject`や`complete`へ読み替えない。
`max_solver_input_chars`は共通Prompt、構造情報、Graph review batch・ledger、Evidence本文を含む最終入力全体の安全上限である。
modelのcontext容量に合わせてProfileで変更し、超過時は候補を省略せず`context_capacity_exceeded`とする。

`max_fetched_resources_per_cycle`はCycle内で`fetch_articles`が`succeeded`にした重複なしArticle数を数える。
検索候補とGraph候補を表示しただけでは数えない。現在の`max_tool_requests_per_cycle`は
`max_tool_requests_per_step`へ改名し、1 Solver DecisionのToolRequest数だけを検証する。Cycle累計Tool数と
自動Tool数はtraceで別に数え、本文取得予算と混同しない。

初期実装では、1つのRun内でproviderを統一する。providerをまたぐ役割分担は対象外とする。

### 7.2 Promptの配置

Profileにはsystem promptの参照先とversionを持たせ、法令固有のprompt本文は
`domains/legal/prompts/`へ配置する。

- 汎用ループの制御規則: `agent_framework`の共通prompt fragment
- 法令の調査方法・注意事項: `domains/legal/prompts/`
- モデル、token、timeout、prompt参照: Profile
- API key: 環境変数またはsecret管理

Profileを切り替えても、CaseStateの意味とTool契約は変わらない。

Skillsは初期ループの必須要素にしない。必要になった場合だけ、明示的に選択されたSkillの指示を
Solver promptへ追加する。SkillによってTool権限や意味判断の責務を拡大しない。

### 7.3 LLMへ見せるstatusのPrompt契約

LLMが入出力するJSON Schemaに`enum`を載せるだけでは、値の意味、相互関係、決定主体は伝わらない。
次の語彙を`agent_framework`の共通Solver Promptへ一度だけ定義し、research・integrationの両方へ
必ず合成する。段階別Promptへ別表現で重複させない。

```text
契約語彙:
- Graph Reviewはあなた（Solver）がGraph候補の関連性と本文取得順を判断する処理モードであり、
  任意のReviewer Agentによる最終回答Reviewとは別である。Reviewer無効時もGraph Reviewを行う。
- program-owned statusは取得・実行の事実であり、あなたは値を変更しない。
- ToolResult succeededはTool正常終了だけを意味し、内容の関連性・正しさ・仮説支持を意味しない。
  failedはエラー終了、timeoutは時間切れであり、対象の不存在を意味しない。
- Graph ToolResultのgraph_projection_updated=trueは、取得したGraph情報がCaseStoreへ保存され、差分batchまたはledgerへ投影可能になったことを示す。
  evidence_countはToolが作成したEvidence件数であり、関連候補の採用件数や根拠件数を意味しない。
- Cycle plannedはgoal・探索方針・完了条件保存済み、runningは同じ方針でstep反復中、completedは
  Cycle全体の評価と終了理由適用済みを意味する。Tool 1回やGraph 1ホップをCycle完了としない。
- Step plannedはToolRequest保存済み、observedは全Tool結果保存済み・意味評価前、completedは
  その結果による仮説・作業・frontier更新適用済みを意味する。observedを評価せず次stepへ進まない。
- content not_requestedは本文未要求、pendingは要求済み・結果待ち、succeededは本文取得成功、
  failed/timeoutは取得失敗・時間切れである。content succeededはrelevantやsupportedを意味しない。
- frontier reviewのunreviewedは、現在のHypothesisに対してまだ関連性を評価していない候補である。
  selectedは、あなたが本文取得対象として選んだ状態であり、本文取得成功を意味しない。
  relevant_deferredは、あなたが関連ありと判断したが今回の本文取得枠に入れず保留した状態、
  rejectedは、あなたが現在の質問・Hypothesisの検証に不要と判断した状態である。
  content statusとfrontier review statusを同じ意味として扱わない。
- expansion not_startedはscope未要求、pendingは結果待ち、partialは未取得page/scopeあり、
  completeは当該scope取得完了、failed/timeoutは取得失敗・時間切れである。
  expansion completeは隣接Article本文の確認完了を意味しない。
- next=continue_cycleはActive Cycleがなければcycle_planでCycle 1を開始し、Active Cycleがあれば
  同じ仮説・探索方針で次stepへ進む。next=start_next_cycleは、現方針で完了できない理由または
  Cycle予算境界を明示してCycleを閉じ、取得済み結果を評価した上で次に検証する命題・方針で
  別Cycleを始める。
  next=finalizeは必要な根拠を探し切ったと判断し、追加Toolなしで回答を確定する。
- WorkItem openは未完了、resolvedは問いへの結論あり、droppedは前提否定・重複・無関係による除外である。
  resolved/droppedは理由をresolutionへ書く。取得失敗だけをresolvedにしない。
- WorkItemのquestionを変えずに親の問いへ引き続き寄与できるならretainする。仮説、検索語、検索先だけの誤りは
  replaceの理由にしない。不足観点は子または兄弟WorkItemとして追加する。question自体を別の意味へ変える必要が
  ある場合だけreplaceし、質問に無関係または重複と根拠から判断した場合だけdropする。
- 親WorkItemをreplaceする場合は旧部分木のopenな子も明示的にdropするかdrop_subtree=trueを返し、
  新しい部分木は別IDで作る。Programは旧子を新しい親へ自動的に付け替えない。
- Hypothesis supportedは提示されたgrounding Evidenceがstatementを支持、contradictedは否定、
  unresolvedは根拠不足・両義的・未確認である。supportedでもWorkItem全体の完了を意味しない。
- Frontier selectは今回の検証行動へ採用、deferは関連ありだが今回の取得枠外として保留、
  rejectは現在の質問・Hypothesisに無関係と理由付きで判断した状態である。
  Decisionに現れないfrontierはrejectせずunreviewedのまま残す。
- graph_review_ledgerの評価済みNodeを新しいHypothesisの検証に使う場合は、
  frontier_re_adoptionsにNode・WorkItem・Hypothesis・理由を明示する。Programに自動転用を要求しない。
- Cycle境界では、本文未取得のactiveなrelevant_deferred全件へdeferred_frontier_resolutionsを返す。
  fetch_next_cycleは次Cycle最初の本文取得、carry_forwardは次Cycle以降への保持、no_longer_neededは回答に不要との意味判断、
  unresolved_at_limitは次Cycleを開始できない上限時の未確認を表す。Programは既知ID、全件性、
  actionと次動作の参照整合だけを検証し、どのactionが法的に妥当かは判断しない。
- impact retainはWorkItemを維持して前提を差替え、replaceは旧WorkItemをdroppedにして新IDへ置換し、
  dropは不要として閉じる。これらは新たにcontradictedとなったbasisの影響をSolverが判断する値である。
- 観察後に、元の利用者質問に対するWorkTreeの範囲、重複、反証Hypothesisの影響を監査する。局所的な
  Hypothesis・WorkItem・ToolRequestの追加で現在方針を維持できるならcontinue_cycleを選ぶ。初期分解の
  主要部分、中心仮説、検索起点または対象階層の前提を変える必要がある場合、またはCycle取得枠が尽きても
  必要と判断した未取得Evidenceが残る場合にstart_next_cycleを選ぶ。
- cycle_budget_reached=true、cycle_step_limit_reached=true、またはcycle_close_required=trueなら、
  現Cycleに新しいToolRequestを追加しない。直前までのToolResultを評価し、WorkItem・Hypothesisを更新する。
  完了できるならfinalize、未解決で残りCycle予算があるなら、次Cycleで確かめる命題、
  更新したgoal・strategy、再採用するfrontierを明示してstart_next_cycleを選ぶ。
- cycle_step_timeoutは中間呼出しがCycle予算で時間切れになった実行事実であり、仮説の否定、
  根拠の不存在、provider全体の障害を意味しない。予約済みのCycle終了判断で手元の結果を整理する。
- finalize_only=falseなら必要な追加調査をcontinue_cycleでき、方針変更が必要ならstart_next_cycleを選べる。
  trueなら上限後のCycle終了判断なので、
  追加Toolを要求せず、確認済み範囲と未確認範囲を区別してfinalizeする。
- material_included=trueだけが本文提示済みである。falseはmanifest・探索構造だけで本文未提示である。
- grounding_evidence_idsは意味判断・引用可能な本文、navigation_evidence_idsはGraph以外の候補発見専用、
  fetchable_resource_ids（Legalではfetchable_article_ids）は本文取得Toolへ完全一致で渡せる既知Resource IDである。
```

Profileが`required_dependency_kind`を設定した場合だけ、次の語彙を追加する。`null`のProfileへ
未使用のDependency status・action説明を見せない。

```text
- dependency not_requiredは回答に不要、needs_actionは必要だが追加Toolが必要、resolvedは依存先本文まで確認済み。
- discover_sourceは依存元候補の発見、assess_sourceは依存元本文の確認、discover_targetは依存先候補の発見、
  fetch_targetは既知依存先本文の取得である。
```

Legal Domain Packの共通Promptには次を追加する。

```text
- max_exploration_depthはOpenSearch起点をdepth 0としてGraph関係をたどれる最大depthであり、Legal Profileでは1に固定する。
  depthが上限未満のArticle本文を取得するとProgramが同じstepで1ホップ候補を取得する。上限depthのArticleは
  本文を取得・評価できるが、そこからGraph候補は増えない。Cycle変更は既存起点のdepthをリセットしない。
- Graph候補がない、最大depthへ達した、または現在のGraph方針を探し切ってもopen WorkItemが残る場合は、
  その問いと確認済み本文の委任・参照表現を使ってlegal_searchを要求し、新しいdepth 0起点を探す。
  Programに必要条文や検索語の推測を任せない。
- graph_review_batchは今回評価が必要な新規候補、再採用候補、新Linkが加わった既評価候補の差分である。
  `review_trigger`は`new_frontier / re_adopted / new_link`のいずれかであり、新Link差分では直前の判断を
  維持するか変更するかを追加Linkも含めて再評価する。Articleごとの法令名、条番号・見出し、content status、
  起点、WorkItem・Hypothesis、当該候補について今回までに判明した全relationが載る。
  graph_review_ledgerは過去の全評価済みfrontierのID、Article、WorkItem・Hypothesis、
  selected / relevant_deferred / rejected、短い理由を示す台帳であり、過去の全Graph Link詳細やLLM生応答ではない。
  CaseStoreの全履歴がPromptから失われたのではなく、評価済みの詳細を重複入力しないための差分投影である。
  batch内の同じArticleへ複数Linkがあれば全てを質問、WorkItem、Hypothesis、取得済み起点本文と照合する。
  表示順や末尾にあることを理由に候補を無視せず、relationだけで法的関連性を確定しない。
- content statusのnot_requestedは未要求、pendingは結果待ち、succeededは本文取得成功、
  failedはエラー終了、timeoutは時間切れである。succeededは法的関連性や根拠採用を意味しない。
- 検索本文中の条番号、法令番号、documentIdからArticle IDを生成しない。必要な参照先IDが
  fetchable_article_idsになければfetch_articlesから外し、法令名・条番号・確認事項でlegal_searchする。
  Decisionを返す直前に、fetch_articlesの全IDをfetchable_article_idsと完全一致で照合する。
- 質問に関係すると判断した1ホップ候補は、Graph Reviewごとに最大3件、かつCycleの
  残り本文取得枠内でselectする。関連するが枠に収まらない候補はdeferし、
  graph_review_ledgerと次Cycleの引継ぎ候補へ残す。Graph候補だけを根拠にせず、端点Article本文を確認する。
- Graph kind formal_relationは登録済み関係、relation_assertionは未確認候補である。
- relation_assertion statusのunverifiedは未分類、llm_classified_implementsは別処理LLMの具体化判定、
  llm_classified_reference_onlyは参照だけとの判定、llm_classified_uncertainは判定不能である。
  どのstatusも正式関係への昇格を意味せず、今回取得した両端本文で判断する。未知statusも確定根拠にしない。
- REFERENCESはfrom本文がtoを参照、IMPLEMENTSはfrom親規定からto具体化規定、APPLIED_BYは
  from準用される規定からto準用する規定である。outgoingは起点がfrom、incomingは起点がtoである。
- referenceKind article_referenceは一般参照、parent_law_referenceは下位法令から親法令への明示参照、
  applicationは適用・準用、definitionは定義、exceptionは例外、form_or_tableは様式・表への参照である。
  delegation_parentは旧schema互換値であり、名前だけで委任と確定しない。
- relationSource、sourceId、derivedFromEdgeId等の生成元・監査用来歴はCaseStateに保持されるが、
  SolverContextへは重複投影されない。Solverはcatalogに示された関係属性と取得本文で判断する。
```

Reviewer Promptには`accept=指摘なし・findings空`、`revise=具体的findingsあり`を定義する。
プログラム内部だけの`RunStatus`、`stop_reason`、trace error codeはSolverへ渡さず、Prompt語彙を増やさない。
status追加・名称変更時は、型、Prompt語彙、Profile version、schema、契約テストを同じ変更単位で更新する。

### 7.4 Graph差分Review・Cycle予算に伴うPrompt変更

コードと状態型の変更と同じcommitで、次を更新する。Promptだけを先行させ、
現行SolverContextに存在しない値をLLMへ指示しない。

| Prompt / schema | 必須変更 |
|---|---|
| `solver_common.md` | Graph ReviewはSolverの処理モードであり、任意のReviewer Agentとは別であることを定義する。 |
| `solver_common.md` | Cycleは最大4、1 Cycleの本文取得累計は4、Graph Review選択は最大3と定義する。`max_tool_requests_per_step`と本文取得累計を区別する。 |
| `solver_common.md` | `cycle_budget_reached`、`cycle_close_required`、`cycle_step_timeout`、`remaining_fetch_capacity`の意味と決定主体を定義する。 |
| `solver_common.md` | `unreviewed / selected / relevant_deferred / rejected`と`select / defer / reject`を定義し、content statusと混同しないよう指示する。 |
| `solver_common.md` | 評価済みNodeを別Hypothesisへ使う場合は`frontier_re_adoptions`で明示し、Programが自動転用しないことを定義する。 |
| `solver_graph_review.md` | 累積`graph_candidate_catalog`全件ではなく、`graph_review_batch`と`graph_review_ledger`だけを読む。`review_trigger`を解釈し、過去の詳細が再提示されないことを候補の不存在と解釈しない。 |
| `solver_graph_review.md` | 各batchの全候補をWorkItem・Hypothesis別に評価し、最大3件を`select`、関連する残りを`defer`、無関係と判断したものだけを`reject`する。 |
| `solver_graph_review.md` | `remaining_fetch_capacity=0`なら新たにselectせず、関連候補をdeferしてCycle終了判断へ戻す。Graph Reviewから直接次Cycleの法的方針を決めない。 |
| `solver_integration.md` | Cycle上限に達したら、直前までのToolResultを評価し、Hypothesis・WorkItem・Evidence・Graph ledgerを整理した後に、finalizeまたは次Cycleのgoal・strategy・再採用frontierを返す。 |
| `solver_integration.md` | Cycle境界でactiveな`relevant_deferred`全件を`fetch_next_cycle / carry_forward / no_longer_needed / unresolved_at_limit`のいずれかへ明示し、黙って破棄しない。 |
| Provider schema | Review判断対象は現在のbatch、本文取得へ選べるIDはbatchの候補とledgerの`relevant_deferred`、再試行時の`selected + failed/timeout`に制限する。選択上限は`min(3, remaining_fetch_capacity)`とする。`rejected`は新Link差分でbatchへ再提示された場合を除き同じHypothesisで再選択させず、別Hypothesisへの`frontier_re_adoptions`はledgerの既知Nodeと既知のopen WorkItem・Hypothesisだけを許可する。候補の関連性や優先度はschemaまたはProgramで補正しない。 |
| Provider schema | Deferred解消はledgerの既知IDだけを許可する。Programは全件性と次動作との矛盾だけを拒否し、関連性・必要性を補正しない。 |
| Provider schema | Graph Reviewモードで必ず空になるdependency、re-adoption、deferred解消、answerは空配列またはnullの簡易schemaとし、未使用の動的enumをコンパイルさせない。 |

Prompt契約テストでは、旧の「累積catalogを毎回全件読む」「未取得関連候補がある限り
同じCycleでReviewを繰り返す」「Graph Reviewごとに最大4件選ぶ」という指示が残っていないことも検査する。

## 8. CaseStore

### 8.1 初期契約

```python
class CaseStore(Protocol):
    def create(self, state: CaseState) -> None: ...
    def load(self, case_id: str) -> CaseState: ...
    def save(self, state: CaseState) -> None: ...
```

初期実装は`InMemoryCaseStore`だけとする。

- Pythonプロセス内だけで有効
- プロセス終了で内容を失う
- 複数プロセス整合性を保証しない
- DB transactionやdurabilityを保証しない

Cycle開始時はgoal・strategy・completion criteriaを検証して`CycleRecord.phase=planned`を保存し、
最初のStep開始時に`running`へ変える。各action-observation Stepは次の3 checkpointで保存する。

1. SolverのToolRequestを検証して`StepRecord.phase=planned`を保存する。
2. 全ToolResult・Evidence・探索Node/Linkを保存して`StepRecord.phase=observed`にする。
3. 次のSolverによるHypothesis・WorkItem・frontier更新を検証し、`StepRecord.phase=completed`にする。

Solverが`continue_cycle`を返した場合は、同じ`CycleRecord`へ次のStepを追加する。
`start_next_cycle`または`finalize`を返した場合だけ現在Cycleを`completed`にし、
`research_cycle_count`を同時に増やす。
各`fetch_articles` ToolResultが`succeeded`になった時点で、重複なしのArticle IDを
`CycleRecord.fetched_resource_ids`へ追加する。残り本文取得枠を超えるToolRequestは実行前に契約違反とし、
ProgramがIDを切捨てない。上限、step境界、またはCycle時間境界では`budget_stop_reason`を保存し、
予算到達前にCycle終了用のSolver判断を実行する。

Stepの`planned`からの再開は未完了Toolだけを実行し、`observed`からの再開はToolを再実行せず
Solver評価へ進む。成功済みToolRequestとExpansionSliceは同じID・scopeで再実行しない。
これらはインメモリ状態更新であり、DB transactionとは呼ばない。

### 8.2 将来の永続化

プロセス再起動をまたぐ再開要求が発生した場合だけ、SQLiteまたはPostgreSQL Adapterを追加する。
その時点で実際の同時実行要件を確認し、revision、optimistic locking、migrationを設計する。
永続化Adapterは上記Step checkpointの原子性、Node・Link・request IDの一意性、`observed`からの
非再実行を同じCaseStore contract testで満たす。

現時点で将来DBを推測してRepository、Unit of Work、leaseを先行実装しない。
`CaseStore`をAgentLoopから分離しておくことだけを、切替容易性の初期保証とする。

## 9. ログとtrace

EventJournalやDB監査ログは導入しない。運用ログとAPI traceを同じ実行計測から生成する。

記録する項目は次に限定する。

- `request_id / case_id`
- cycle番号・phase・goal・strategy・focus Hypothesis件数
- step番号・phase・ToolRequest件数
- frontier件数、探索Node/Link件数、最小/最大depth、`partial` expansion件数
- 呼び出し用途: `research / integration / review`
- provider、model、Profile名・version
- logical call数、transport attempt数
- input/output token
- latency
- Tool名、件数、status、elapsed
- 機械的な`error_code / stop_reason`
- Reviewerが有効だったか

既定ログへ次を出さない。

- API key、credential
- 利用者質問の全文
- system prompt全文
- LLM生応答
- 法令本文、Evidence本文
- 内部例外文字列をそのまま返したAPI error

内容が必要なデバッグは明示的な開発設定に限定する。ログ出力失敗によって回答処理を失敗させない。

## 10. ディレクトリ構成

```text
agent-api/app/
├── agent_framework/                 # 検索対象に依存しない再利用基盤
│   ├── contracts.py                 # SolverDecision / CaseUpdate / ImpactDecision
│   ├── state.py                     # CaseState / WorkItem / Hypothesis / Evidence / CycleRecord / StepRecord
│   ├── exploration.py               # Node / Link / frontier / expansionの汎用構造
│   ├── loop.py                      # Cycle内step反復・最大4 cycle・予算終了・Reviewer分岐
│   ├── context.py                   # WorkTree・探索frontier・focus・Evidenceの機械的表示
│   ├── validation.py                # 型・既知ID・権限・上限の機械的検証
│   ├── profiles.py                  # Profile読込みと用途別model解決
│   ├── store.py                     # 小さいCaseStore Protocol
│   ├── observability.py             # 構造化ログとtrace計測
│   └── ports/
│       ├── model.py                 # ModelPort
│       └── tool.py                  # ToolPort / ToolDefinition
│
├── domains/
│   └── legal/                       # 法令業務ドメイン
│       ├── tools.py                 # 法令Tool登録
│       ├── evidence.py              # 法令Evidenceへの変換・表示
│       ├── profiles/
│       │   └── default.yaml
│       └── prompts/
│           ├── solver_common.md
│           ├── solver_research.md
│           ├── solver_integration.md
│           └── reviewer.md
│
└── adapters/
    ├── models/
    │   ├── anthropic.py
    │   └── ollama.py
    ├── tools/
    │   └── legal_search.py           # OpenSearch / Neo4j / 本文取得
    └── persistence/
        └── in_memory.py
```

法令名、条文、ガイド、`IMPLEMENTS`、`REFERENCES`等を`agent_framework`へ置かない。
OpenSearch、Neo4j、Anthropic等のSDKも`agent_framework`から直接importしない。

## 11. 現行実装からの移行

### 11.1 扱い

- `llm_research_loop.py`と`research_case_store.py`は、切替完了まで現行経路として保守する。
- 未接続の`agent_core/`試作は拡張しない。
- 新しい薄い縦切りが合格した後、参照がないことを確認して過剰な試作を削除する。
- 現行経路を一度に書き換えず、Feature Flagで新旧を切り替える。
- 移行中に法令検索ロジックを汎用基盤へコピーしない。Legal Tool Adapterで既存機能を包む。

### 11.2 名称の整理

| 現行名称 | 新しい扱い |
|---|---|
| Main / Answerer / Integrator | `Solver`のresearch/integrationモードへ統合 |
| Explorer | Solverが返す複数ToolRequestへ統合 |
| Reviewer | 任意のReviewerとして残す。既定無効 |
| Projector | 独立主体として廃止。`context.py`がSolver指定IDと固定上限を機械的に展開 |
| Scheduler | 廃止。SolverのToolRequestを上限内で実行 |
| ResearchCheckpoint | `CycleRecord.steps`へ、action-observation単位の`StepRecord`として統合 |
| ResearchCaseStore | 小さい`CaseStore`へ置換 |

context組立ては、全WorkTree案内、現Cycleのgoal・strategy、直前Step、Graphの新規・再採用・新Link差分batch、
評価済みfrontierの短いledger、focusへ接続するNode・Link、直近ToolResultの実行状態、
新規Evidence、`retain_evidence_ids`を直列化する。Evidence本文は50,000文字を上限とする。
全Graph Article・Link・Review履歴はCaseStoreに保持し、Promptに過去の詳細を重複入力しない。
差分batchのpage分割は安定順の機械処理とし、関連性・優先度・再採用はSolverに選ばせる。

## 12. 実装Phase

### Phase 0: 契約とbaseline

- 本計画を正本として確定する。
- 現行の代表2問について、総時間、LLM呼び出し数、用途別latency、Tool時間をtraceから記録する。
- 評価データ、設定、model ID、code revisionを固定する。
- 新しいSolver、Reviewer、Tool、CaseStore契約のfixtureを作る。
- Case→再帰WorkItem→Hypothesis→Exploration Node/Link/frontier→CycleRecord→StepRecord→ToolResult/Evidenceの参照fixtureを作る。
- 7.3の全LLM-visible statusが共通PromptまたはDomain Promptへ定義される契約テストを作る。
- Reviewerの既定値が`false`であることを設定契約へ固定する。
- Framework Profileの`max_exploration_depth`が`1`と`2`だけを受理し、Legal Profileは`1`に固定され、
  未設定、`0`、`3`以上、整数以外を拒否するfixtureを作る。
- Legal Profileの`max_material_evidence_chars`初期値を50,000文字へ固定し、本文枠とGraph review batch・ledgerが
  別に計上される契約fixtureを作る。
- `max_solver_input_chars`初期値を240,000文字とし、本文上限より大きいことをProfile検証へ追加する。

完了条件:

- v38で上限3 Cycleのう1 CycleにTool 12回・本文16条・累積Graph Reviewが集中した内訳を再現できる。
- 新契約で意味判断と機械的検証の境界をテストとして記述できる。

### Phase 1: 最小Framework

- `agent_framework/contracts.py`、`state.py`、`loop.py`を実装する。
- `exploration.py`へNode、Link、frontier、ExpansionSlice、CycleRecord、StepRecordの汎用型を実装する。
- `CaseStore`と`InMemoryCaseStore`を実装する。
- Profile resolverを実装する。
- Case全体再生成ではなく、`CaseUpdate`の追加・更新差分を適用する。
- 全WorkTree案内、現Cycleのgoal・strategy、直前Step、frontier、Solver指定focus、直前の新規Evidence、保持Evidenceを組み立てる。
- Cycleの`planned → running → completed`と、各Stepの`planned → observed → completed`を保存し、
  Cycleの`completed`時だけcycle数を増やす。
- `max_research_cycles=4`、Cycle累計の`max_fetched_resources_per_cycle=4`、
  `max_tool_requests_per_step`を別の制約として実装する。自動Toolをstep・Cycle traceへ計上するが、
  本文取得数とは混同しない。
- Cycleの本文取得・step・時間境界前に新しいactionを止め、予約したSolver呼出しで
  観察済み結果の評価とCycle終了を行う。
- fake Modelとfake Toolで1 Cycle内の複数step、`continue_cycle / start_next_cycle / finalize`、
  Stepの`observed`からの再開をテストする。
- read-only ToolRequestの上限制御付き並列実行を実装する。
- 反証Hypothesisから影響WorkItemを列挙し、Solverの`retain / replace / drop`を適用する。
- Reviewer無効時にReviewer Modelが一度も呼ばれないことをテストする。
- Reviewer有効時の`accept / revise / review_failed`をテストする。

このPhaseではClaude APIを使わない。

完了条件:

- Cycle 1で検索→起点本文→1ホップ→隣接本文を複数Stepとして継続し、必要根拠を探し切って`finalize`できる。
- Tool実行やStep完了だけではcycle数が増えず、`start_next_cycle`または`finalize`でCycleを閉じた時だけ増える。
- 1 Cycleの5件目の本文取得を実行前に拒否し、4件までの観察結果をSolverが評価して
  `finalize`または次Cycleの計画を返す。
- 最大4 Cycleへ到達するfixtureと、1〜3 Cycleで早期`finalize`するfixtureがともに通る。
- Stepの`planned`では未完了Toolだけを実行し、`observed`では成功済みToolを再実行せず評価へ進む。
- 最後の許可StepのToolResultが次のSolver判断へ渡され、評価されずに残らない。
- 同じ方針を継続できるのに、Graph hopやTool終了だけを理由として`start_next_cycle`にしない。
- 同じResourceを複数Linkから発見してもNodeは1件で、Linkはすべて保持される。
- Graph navigationのArticle・Link投影がSolver向けの唯一表示となり、manifest・ToolResult・ID一覧に同じGraph Evidenceが重複しない。
- 循環Linkを保存しても、成功済みNode・scopeを再展開しない。
- 汎用fixtureの`max_exploration_depth=1 / 2`で各上限depthからGraph展開せず、
  Legal Profileの実行では深さ1本文を取得できるが深さ1からGraph展開しない。
- 次Cycleへ移っても同じ起点のdepthをリセットせず、別のOpenSearch結果だけを新しい深さ0起点にする。
- Solver Decisionに現れないfrontierが消えない。
- 50,000文字以内のEvidence本文が決定的な順序で提示され、上限外本文はmanifestから再取得できる。
- Evidence本文が上限に達しても、今回のGraph review batchのArticle ID、見出し、起点、relation、depth、
  content statusと、過去の評価済みfrontier ledgerがSolverContextに残る。全候補・Link履歴はCaseStoreに残る。
- Solverはledgerの既知Nodeを新Hypothesisへ明示的に再採用できるが、Programが`rejected`を自動転用しない。
- 新規Graph候補が複数Review batchに分かれても未提示pageが消えず、評価済み詳細を
  次ReviewのPromptへ重複投影しない。
- `select`したfrontierがledgerに`selected`として残り、本文取得の`pending / succeeded / failed / timeout`と
  混同されない。取得成功済み候補は再選択できず、失敗・timeout時だけ既知IDで再試行できる。
- modelのcontext容量を超えるfixtureでGraph候補を黙って削らず`context_capacity_exceeded`になる。
- CaseUpdateに現れなかった別系統のWorkItemと未完了WorkItemが消えない。
- WorkItemの親子循環、未知basis ID、未知focus IDを拒否する。
- Hypothesis反証時に、プログラムが影響WorkItemを自動的にdropしない。
- 仮説だけが反証されたfixtureではWorkItemを維持し、新Hypothesisを追加できる。
- 不足観点のfixtureでは既存WorkItemをreplaceせず、子または兄弟WorkItemを追加できる。
- 問い自体が不適切なfixtureだけで旧部分木を閉じ、新しい部分木へreplaceできる。
- プログラムがHypothesisの意味statusを書き換えない。
- 通常の`finalize`時にopen WorkItemが残る契約違反を拒否する。上限時の限定回答だけ、limitationsと
  unresolved ID欄が全open WorkItem・対応unresolved Hypothesisを漏れなく参照する場合に保持を許す。
  Programは未確認事項の法的内容や、Graph候補が本当に不要かは判断しない。
- 未知ID、不正Tool、権限外Tool、上限超過だけを拒否する。
- Reviewerの既定値が無効である。

### Phase 2: 法令の薄い縦切り

- Legal Domain Packと法令Promptを実装する。
- 既存OpenSearch、Neo4j、本文取得をLegal Tool Adapterとして接続する。
- OpenSearch候補とGraph候補をLegal Resource Node・DiscoveryLink・frontierへ投影する。
- Article本文取得と1ホップGraphを同じStepの観察へ入れ、隣接本文取得は現Cycleの
  残り本文取得枠内でSolverが`select`した対象に限定する。枠外の関連候補は`defer`して次Cycleへ残す。
- Profileの`max_exploration_depth`に達したArticleでは本文だけを取得し、自動1ホップGraphを抑止する。
- Graphのrelation type・direction・cursor・policy versionをExpansionSliceのscopeへ対応付ける。
- Graphの全Article・Link・Review履歴をCaseStoreに保持し、SolverContextへは
  `graph_review_batch`と`graph_review_ledger`を差分投影する。
- Graph Reviewは1回最大3件のArticle本文を選び、Cycleの残り本文取得枠を超えないようにする。
- 7.4の通り`solver_common.md`、`solver_graph_review.md`、`solver_integration.md`、Provider schemaを
  型・Profile version・契約テストと同時に更新する。
- `/answer`に新経路のFeature Flagを追加する。
- Solverのresearch/integrationで別modelを設定できるようにする。
- 法令固有型や法令関係判断がFrameworkへ漏れていないことを確認する。

完了条件:

- fake Modelを使ったAPI統合テストが通る。
- Tool結果の取得状態はプログラム、法的関連性と根拠採否はSolverが決める。
- `IMPLEMENTS`と`REFERENCES`の意味をFrameworkが判断しない。
- 同じArticleへ複数経路があるfixtureで本文取得は1回、DiscoveryLinkは複数残る。
- A→B→Aの循環fixtureで再帰展開が停止する。
- `max_exploration_depth=1`で、1ホップ候補の本文取得と、その候補からのGraph非実行を確認する。
- 最大depthまたはGraph関係欠落のfixtureで、Solverがopen WorkItemに基づく`legal_search`を選び、
  結果を新しい深さ0起点として同じCaseで探索できる。
- 関係するとSolverが判断した未確認frontierが残る場合、通常の`finalize`を選ばないPrompt契約を確認する。
- Evidence本文が省略されても、当該Graph review batchにはArticle ID、法令名、条番号・見出し、
  content status、depth、起点Article・Link、relation type・direction・status、`partial / complete`が残り、
  ledgerの`relevant_deferred` frontierも既知IDとして選べる。全履歴はCaseStoreで監査できる。
- 100件以上のGraph候補fixtureで、Review入力が累積全件ではなくProfileのpage上限内に収まり、
  全pageが最終的に`selected / relevant_deferred / rejected`のいずれかになる。
- 既評価frontierへ新Linkが追加されたfixtureでは、その候補と今回までの関係情報だけが差分batchへ戻り、
  Solverの再判断後も過去DecisionとLink履歴がCaseStoreに残る。
- 同一候補Articleを複数起点から発見するfixtureで、Articleは1件、Linkは全経路分残り、
  Graph Evidence IDがmanifest、ToolResult、navigation・omitted ID一覧に現れないことを確認する。
- Legal PromptがGraph kind、relation status、referenceKind、directionを7.3どおり定義する。

### Phase 3: ログと性能

- logical model callとtransport retryを分けて計測する。
- cycle phase・goal・strategy、step番号・phase、frontier、探索Node/Link/depth、model用途、Tool時間を構造化ログとAPI traceへ出す。
- 秘匿情報が通常ログへ出ないことをテストする。
- Prompt入力と構造化出力を必要最小限へ縮小する。
- 直列Tool実行が残っていないことを計測する。

完了条件:

- Reviewer無効の単純問題では、LLM呼び出しが原則2〜3回以内である。
- 公開買付けのような多段探索は、1 Cycleの本文取得を4件以内に抑え、
  通常1〜2 Cycle、必要な場合のみ最大4 Cycleで完了する。
- 最大経路でもRun全体のaction stepは`max_total_steps`、Solver判断は原則`max_total_steps + 1`を超えない。
- 各LLM・Tool実行前にCycle終了、残りCycle、最終回答の予約を確保し、予算不足の中間呼出しを
  開始せずToolResultをCycle終了判断へ渡す。
- 予算由来の中間LLM timeoutをRun全体の`provider_error`にせず、`cycle_step_timeout`として
  Cycle終了判断へ進める。
- provider障害を意味上の`unresolved`へ変換しない。
- 大きな1ホップでも、Graph Reviewの入力は差分batch上限内に収まり、既評価候補の詳細を
  毎回重複入力しない。未評価pageと`relevant_deferred` frontierは消えない。

### Phase 4: 実モデル評価と切替

- Reviewer無効、research/integrationともHaikuで代表自然言語2問を1回ずつ実行する。
- 必要根拠、回答要点、総時間、LLM呼び出し数、Tool時間をbaselineと比較する。
- 必要ならReviewer有効を別試験として1回だけ実行し、品質差と時間差を測る。
- 合格後に新経路をデフォルトへ切り替える。
- 参照されなくなった過剰な`agent_core`試作と旧経路を、別変更で削除する。

合格条件:

- 代表2問で必要根拠へ到達する。
- プログラムによる法的意味判断がない。
- Reviewer無効がデフォルトである。
- 通常問題のp90を120秒以内とする。外部provider障害は別集計する。
- baselineより回答品質を落とさず、LLM呼び出し数を大幅に削減する。

## 13. 将来拡張

次は初期切替の合格条件へ含めない。

### 永続化

再起動を越えた案件再開が必要になった場合、CaseStore contract testを作ってから
SQLiteまたはPostgreSQL Adapterを1種類ずつ追加する。

### サブエージェント

単一Solverと並列ToolRequestで不足することが計測された場合だけ検討する。
追加する場合も、独立したread-heavy作業に限定し、結果はEvidence IDでSolverへ戻す。

### provider横断

同一Run内で複数providerを使う要求が生じた場合だけ、credential、capability、障害分離を追加設計する。

## 14. 禁止事項

- プログラムが法的関連性、十分性、重要度を語句やscoreで決めない。
- LLMの`finalize`をプログラムが意味上の理由で撤回しない。
- SolverにCaseState全体を毎回再生成させ、出力漏れを削除として扱わない。
- 全WorkTree案内から未完了WorkItemを黙って省略しない。
- Tool実行完了を、Hypothesis評価まで閉じたCycle完了として数えない。
- Tool実行回、LLM呼び出し回、Graphの1ホップをCycle境界として扱わない。
- プログラムが本文取得・step・時間の機械的境界を超えてactionを続けたり、境界で
  現Cycleを閉じる判断を飛ばして次Cycleを自動開始したりしない。
- Graph候補を再帰関数で連続展開し、Solver評価を挟まず隣接本文を次々に取得しない。
- 複数発見経路を持つ探索Graphの正本を、単一親しか持てない木へ変換しない。
- Graph候補をArticle・Linkへ正規化する際に、resource ID、depth、起点Link、取得・展開statusを隠さない。
- LLM-visible statusを、意味と決定主体のPrompt定義なしに追加・変更しない。
- Hypothesis反証を理由に、プログラムが子WorkItemを自動的に維持・置換・破棄しない。
- WorkItemの問いやHypothesisのstatementを別の意味へ上書きしない。
- 構造化出力不正を`unresolved`や`revise`へ読み替えない。
- Reviewer無効時に暗黙にReviewerを呼ばない。
- Reviewerの指摘からプログラムが検索queryを生成しない。
- model ID、Reviewer有効値、DB backendをAgentLoopへハードコードしない。
- 法令固有Promptや型を`agent_framework`へ置かない。
- 未取得本文や未確認Graph関係を最終根拠として自動採用しない。
- Prompt全文、LLM生応答、法令本文、credentialを通常ログへ出さない。
- 最大回数に達したことを、根拠不足という意味判断へ変換しない。

## 15. 次の修正単位

現行の新Frameworkを本計画へ戻す修正は、次の順で行う。

### 修正A: 汎用Cycle・探索契約

1. `ExplorationState`、Node、Link、frontier、ExpansionSlice、CycleRecord、StepRecordを追加する。
2. Tool終了時のcycle加算をやめ、Cycleの`planned → running → completed`と
   Stepの`planned → observed → completed`を実装する。
3. `continue_cycle`では同じCycleへStepを追加し、`start_next_cycle / finalize`で閉じた時だけ
   `research_cycle_count`を増やす。
4. `max_research_cycles`を4へ広げ、`max_fetched_resources_per_cycle=4`を追加する。
   `max_tool_requests_per_cycle`は実際の検証単位に合わせ`max_tool_requests_per_step`へ改名する。
5. `CycleRecord.fetched_resource_ids`で本文取得成功数を重複なしで数え、残り枠を超えるRequestは
   実行前に拒否する。ProgramはArticle IDを切捨てない。
6. Cycle終了、残りCycle、最終回答の時間を別々予約し、新しい中間呼出しを始める前に
   `cycle_close_required`を判定する。予算由来timeoutは`cycle_step_timeout`としてCycle終了判断へ渡す。
7. Stepの`observed`からToolを再実行せず評価へ進める。
8. WorkTree、探索構造、現Cycleのgoal・strategy、直前Step、Evidence、Cycle予算をSolverContextへ投影する。
9. 7.3の汎用status語彙と7.4のCycle予算語彙をresearch・integration Promptへ共通注入する。
10. fake Model / fake Toolで1 Cycle内の分岐、合流、循環、再開、本文4件でのCycle終了、
    4 Cycle上限、予算timeout後のCycle終了をテストする。

### 修正B: Legal探索への接続

1. OpenSearch候補を深さ0のNode・Link・frontierへ変換する。
2. `fetch_articles`と自動1ホップGraphを同一Cycleの観察へ接続し、Profileの
   `max_exploration_depth=1`に達したGraph候補Articleからの再展開を止める。
3. Graph候補をArticle Node・DiscoveryLink・ExpansionSliceへ変換し、全件をCaseStoreに保持する。
4. 複数経路の同一Articleを1 Nodeへ統合し、Linkは失わない。
5. `graph_review_batch`に新規`unreviewed`・再採用候補・既評価候補の新Link差分と必要Link、`graph_review_ledger`に
   過去の全評価済みfrontierの短い台帳を投影する。過去の全Link詳細とLLM生応答を再投影しない。
6. Review batch上限を超える候補を安定順でpage分割し、未提示page・cursor・件数を保持する。
   Programは関連度で並べ替えず、未提示候補をrejectしない。
7. Graph ReviewのSolver選択上限を3件とし、Cycleの残り本文取得枠を超えない。選択外の
   関連候補はdeferし、同じHypothesisではledgerから後続stepまたは次Cycleで選べるようにする。
   別Hypothesisへ結び直す場合だけSolverが`frontier_re_adoptions`を返す。
8. Graphで到達できないopen WorkItemから、Solverが`legal_search`で新しい深さ0起点を作れるようにする。
9. 7.3・7.4のWorkItem監査、Graph差分Review、Cycle予算、検索fallback語彙を各Legal Promptへ反映し、
   Profile version・Provider schema・契約テストを同時に更新する。
10. `max_material_evidence_chars=50000`を適用しても、当該Review batchのArticle ID、見出し、起点、
    relation、depth、statusとdeferred ledgerが残ることをテストする。全履歴はCaseStoreで監査できる。
11. 1候補と必要Linkだけでもmodel context容量を超える場合は明示的エラーにする。

### 修正C: 実モデル検証

非APIテスト通過後に、Reviewer OFF・全Haikuで公開買付け問題を1回実行する。traceでは品質だけでなく、
各Cycleのgoal・strategy、本文取得数、各Stepのphase遷移・ToolRequest・観察結果、focus Hypothesis、
frontier before/after、select・defer・reject、Review batch・ledger件数、Node/Link総数、depth、
本文取得・Graph展開の重複有無を確認する。各Cycleが本文取得4件以内で閉じ、
未解決ならSolverが前Cycleの結果と再採用frontierを明示して次Cycleへ進むことを確認する。
また、Graph Review入力が累積catalogに比例して増えず、予算境界前にCycle終了判断を行うことを確認する。
公開買付け例題の合格条件は、評価用正解をSolver Promptへ渡さず、想定資料3/3、必要条文4/4、
回答要点4/4の合計11/11とする。11/11へ到達しない場合はPhase 4の品質確認を完了扱いにせず、
traceから検索起点、Graph Link、frontier選択、本文取得、WorkItem完了判断のどこで欠落したかを分類する。

この修正に新しい登場人物、Claude以外のprovider、DB Adapter、現行経路削除を混在させない。
