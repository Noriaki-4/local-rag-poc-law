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
  現行ProfileはGraph取得時に`REFERENCES / IMPLEMENTS / APPLIED_BY`を固定指定しており、
  本書4.0のHypothesis別`ExplorationIntent`で検索scope自体を絞る機能は未実装である。
  本書5.1.3のNeo4j物理定義も未実装で、現行RelationAssertionは端点IDと旧statusをプロパティに持ち、
  `SUBJECT / OBJECT`接続、`sourceSnapshotId`、`MENTIONS`排除、OpenSearchとのsnapshot整合監査は未対応である。
  非同期LLM分類、`ClassificationRun`、5種の`proposedPredicate`、分類Runを固定したGraph検索も未実装である。
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
16. Legal Profileでは、Solverが`ExplorationIntent`で明示した既知Articleだけを起点として、
    同じcycleで最大1ホップのGraph検索を行う。本文取得だけを理由に全predicate・全modeを自動取得しない。
17. 1サイクルはTool実行回や1ホップではなく、1つの仮説・探索方針に対する
    上限付きの仮説検証単位とする。根拠を探し切るか、本文取得・step・時間のいずれかの
    Cycle上限に達した時点で必ず結果を評価し、完了または更新した方針で次Cycleへ進む。
18. 本書の`Graph Review`は独立Agentや任意の`Reviewer`ではなく、SolverがGraph候補の
    関連性と本文取得順を判断するモードを指す。Reviewerを無効にしてもGraph Reviewは実行できる。
19. 再帰探索は純粋な木にせず、案件内探索Graph、frontier、展開済み集合、CycleRecordで管理する。
20. LLMへ提示または出力させるstatus・judgment・actionの全値は、許容値だけでなく意味と決定主体をPromptに定義する。
21. Legal ProfileのGraph最大hopは`1`に固定し、OpenSearch起点を深さ0として数える。
22. 各検索は対象WorkItemと、原則として1件以上のHypothesis、その検証に必要な検索範囲を表す
    `ExplorationIntent`へ結び付ける。具体的Hypothesisをまだ立てられない初回候補発見だけは、
    理由を明示したWorkItem単位のIntentを許可する。
    Legal ProfileのGraph検索は、Solverが明示した起点、検索mode、意味predicateまたは原文関係、方向、
    必要な構造filterだけを取得し、Programが未指定の意味predicateを追加しない。
23. LegalRuleMLはNeo4jの物理schemaとして採用せず、法的な意味仮説と原文、Context、出典、時間を
    追跡可能に分離するための参照モデルとして利用する。
24. Legal ProfileがLLMへ示すGraph方向は`from_subject / to_subject`の2値に統一する。
    `from_subject`は関係のSUBJECT（Neo4jのfrom側）からOBJECT（to側）へ辿る方向、
    `to_subject`はOBJECT（to側）からSUBJECT（from側）へ辿る方向である。旧称の
    `outgoing / incoming`はPrompt、Provider schema、ToolResult、CaseStoreの新規データで使用しない。
    Neo4jには方向表示値を保存せず、既存のfrom/toと検索起点からTool Adapterが決定する。
25. `MENTIONS`はLegal Graphから排除する。単に文中へ登場しただけの法令・条文をNeo4jのRelation、
    Graph探索selector、直接本文取得、根拠充足に使用しない。ガイドと条文の明示的な対応は
    `EXPLAINS`として扱い、通常の言及本文は特別なGraph関係へ変換せずOpenSearchの原文として検索する。
26. Legal Profileの`fetch_articles`は、指定した各ArticleについてOpenSearchに登録済みの全本文チャンクを
    取得する。Articleあたりの取得件数上限を契約に設けず、内部page sizeで安定順に反復取得する。
    全pageを取得できた場合だけcontentとToolResultを`succeeded`にし、途中失敗・timeout・対象0件を
    部分成功へ変換しない。1 ToolRequestと1 Cycleで選べるArticle数の上限は維持する。
27. Neo4jは共有される法令構造・原文上の関係・未確認RelationAssertionだけを保持する。
    案件固有のWorkItem、Hypothesis、ExplorationIntent、DiscoveryLink、frontier、Evidence採否は
    CaseStoreに保持し、Neo4jへ書き戻さない。RelationAssertionは`SUBJECT / OBJECT`で両端Articleへ、
    `CLASSIFIED_IN`でClassificationRunへ接続し、
    存在自体を未確認候補として扱う。検索時Solverの案件判断で正式Relationへ自動昇格させない。
28. Graph schemaまたは法令・ガイドの入力を変更した場合は、同一の入力snapshotからOpenSearchとNeo4jを
    両方再構築する。Neo4jだけを再seedして本文とGraphのrevisionをずらさない。seed manifestと監査で
    schema version、`sourceSnapshotId`、取得できる場合のsource revision、Document・Article ID、
    content hashの対応を確認する。
29. `/admin/seed`は本文、構造、原文上の明示参照までを決定的に投入し、LLM意味分類の完了を待たない。
    意味分類はsnapshot単位の再開可能な非同期jobとし、完了した`ClassificationRun`を一括publishする。
30. 意味関係は物理Edgeとして重複生成せず、`RelationAssertion.proposedPredicate`へ
    `IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`のいずれか1つを保存する。
    `APPLIED_BY`は新Graphへ生成せず、準用・読み替えは`INCORPORATES`で表す。
31. `REFERENCES.referenceKind`の旧ヒューリスティック値を法的意味の検索条件に使用しない。
    原文Edgeは引用箇所と抽出来歴を保持し、意味分類は引用元・引用先本文を読む非同期LLMが行う。
32. Caseは`sourceSnapshotId / graphSchemaVersion / classificationRunId`を固定する。検索途中に新しい分類Runが
    publishされても同一Caseへ混在させず、分類漏れと「意味関係なし」を同一視しない。
33. 通常のGraph検索は1件の意味predicateと1方向を指定したRelationAssertion検索を基本とする。
    生の`REFERENCES/to_subject`は高fan-inになりやすいため通常QAの既定経路にせず、明示参照の
    `from_subject`または十分に限定された監査用途だけで使用する。

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
CaseStore（Data Store）
    │
    ▼
Projector ── AgentView（Input）──→ Solver（LLM）
    ▲                                  │
    │                                  ▼
    │                         SolverDecision（Output）
    │                                  │
    │                                  ▼
    │                              AgentLoop
    │                         ┌────────┴────────┐
    │                         │                 │
    │                    状態差分を適用    検証済みToolRequest
    │                         │                 │
    └─────────────────────────┘                 ▼
                                          Search Logic
                                           │         │
                                      検索実行   ToolResult
                                           ▼         │
                                OpenSearch / Neo4j   │
                                   （Data Store）     │
                                                     │
                         CaseStore ◀─────────────────┘
```

`AgentLoop`は判断主体ではない。状態を読み、適切なProfileでLLMを呼び、
出力を機械的に検証し、ツールを実行して結果を保存する。

この図は検索時の中心経路を示す。`Input`と`Output`は登場人物ではなく、LLM呼出し境界の契約である。
`Projector`も独立Agentや独立サービスではなく、`context.py`がCaseStoreから`AgentView`を組み立てる
決定的な処理の呼称とする。任意の`Reviewer`は中心経路の後段で最終結果を検査し、既定では呼び出さない。

### 3.1 5分類による整理

| 分類 | 具体要素 | 一言で表す責務 |
|---|---|---|
| Data Store | `CaseStore` | 案件の作業・仮説・探索・Evidence・判断履歴を保存する案件内正本 |
| Data Store | `OpenSearch` | 法令本文の検索と、Article全文取得に用いる共有データストア |
| Data Store | `Neo4j` | 法令構造、原文上の関係、未確認RelationAssertionを保持する共有Graph |
| Input | `Projector` → `AgentView` | CaseStoreから現在のLLM呼出しに必要な情報を決定的に投影する |
| LLM | `Solver` | 作業分解、仮説、検索方針、候補の意味評価、完了判断、回答統合を行う |
| Output | `SolverDecision` | Solverが決めた状態差分と次の行動を構造化して返す |
| Search Logic | Legal Tool Adapter / `ToolResult` | Solverが指定した検索条件で固定検索を実行し、実行状態と取得物をCaseStoreへ戻す |

境界を次のように固定する。

- `Projector`は重要度・関連性を判断しない。保存済み情報を固定規則でjoin、重複排除、整形、page分割する。
- `AgentView`は永続化しないread modelであり、CaseStoreの正本を置き換えない。
- `Solver`だけが仮説、法的意味、候補の関連性、Evidence採否、調査完了を判断する。
- Search Logicは未指定条件を補完せず、法的意味に基づく絞込みや順位付けを行わない。
- `ToolResult`は取得結果であり、それだけでEvidence採用済みまたは仮説確認済みとは扱わない。
- `AgentLoop`は`SolverDecision`を構造検証し、許可された処理を実行してCaseStoreへ適用する。
- `AgentLoop`は5分類を接続する実行制御であり、LLMとは別の判断主体として数えない。
- `Projector`はNeo4jやOpenSearchを直接読まない。検索結果は必ず`ToolResult`としてCaseStoreへ保存してから投影する。

データの流れは次の一方向を基本とする。

```text
OpenSearch / Neo4j
        ↓ Search Logic
     ToolResult
        ↓
     CaseStore ──→ Projector ──→ AgentView ──→ Solver
        ▲                                      │
        └──── AgentLoop ←── SolverDecision ────┘
```

### 3.2 責務

| 主体 | 担当すること | 担当しないこと |
|---|---|---|
| Solver | 作業分解、仮説、意味評価、根拠選択、追加調査、完了判断、回答 | ツールの直接実行、存在しないIDの生成 |
| Reviewer | 回答と根拠の整合確認、具体的な修正指摘 | ツール実行、後続経路の直接制御 |
| AgentLoop | LLM呼び出し、状態更新、上限管理、ツール実行、再試行制御 | 法的関連性、十分性、重要度の判断 |
| Tool Adapter | 検索・本文取得・Graph取得と実行結果の正規化 | 取得物の法的評価 |
| CaseStore | CaseStateの保存と読出し | Prompt編集、意味判断、重要度選択 |
| Domain Pack | 法令用Prompt、ツール定義、根拠表示形式 | 汎用ループの制御 |

### 3.3 Data Store・Input・Outputの対応

英語の型名・field名は実装上の識別子として残すが、人間向けの図、表、Prompt仕様では名前だけを並べない。
初出箇所で「何を表すか」と「誰が決めるか」を日本語で併記する。同じfieldを後続の型定義で再掲するときは、
本節またはstatus語彙への参照を付ける。

#### 検索時

```text
Data Store: CaseStore
案件の質問、作業、仮説、探索履歴、Evidence、過去のSolver判断を保存
        │
        ▼
Input: AgentView
Projectorが今回のSolver判断に必要な案件状態を読み取り専用で組み立てる
        │
        ▼
LLM: Solver
仮説、検索方針、候補の関連性、Evidence採否、完了を判断
        │
        ▼
Output: SolverDecision
案件状態の変更差分と、次に実行する検索要求を返す
        │
        ▼
Search Logic: Legal Tool Adapter
既知ID・allowlist・件数・depthを検証し、固定検索を実行
        │
        ├── OpenSearch: 内容検索とArticle全文取得
        └── Neo4j: 既知Articleから関係候補を1ホップ取得
        │
        ▼
Output: ToolResult
検索の実行事実と取得候補を返す。関連性や根拠採用は確定しない
        │
        ▼
Data Store: CaseStore
ToolResult、候補catalog、探索Linkを保存し、次のAgentViewの正本にする
```

`AgentView`は次を意味する。

```text
AgentView（Solverへ渡す入力）
│
├─ question
│   利用者の元の質問
│
├─ work_tree
│   現在の階層WorkItemと、未解決・解決済み等の状態
│
├─ hypotheses
│   Solverが立てた検証可能な仮説と、支持・反証・未解決の状態
│
├─ recent_tool_results
│   直前に実行した検索の成功・失敗・タイムアウトと取得件数
│
├─ graph_review_batch
│   今回Solverが関連性を評価する、新規または再評価対象のGraph候補
│
├─ material_evidence
│   Solverが内容を読んで根拠採否を判断できるArticle本文
│
└─ budget
    残りCycle、本文取得件数、step、時間の機械的な上限
```

`SolverDecision`は次を意味する。

```text
SolverDecision（Solverが返す出力）
│
├─ case_update
│   WorkItem、Hypothesis、gap、Evidence採否等に対する変更差分
│   内容はSolverが判断し、Programは既知IDと参照整合だけを検証する
│
├─ tool_requests
│   次に実行するOpenSearch、Graph検索、Article全文取得の要求
│   検索語、predicate、direction、構造filterはSolverが選ぶ
│
├─ frontier_decisions
│   Graph候補を今回取得する、後へ回す、現在の仮説では不要とする判断
│
├─ next_action
│   同じCycleを続ける、次Cycleで仕切り直す、回答を確定する、の選択
│
└─ final_answer
    調査完了時の根拠付き回答。調査継続時は空にする
```

Graph検索の`ToolResult`は次を意味する。候補の詳細はCaseStoreのGraph catalogへ正規化し、
Projectorは同じ内容を`recent_tool_results`と`graph_review_batch`へ重複表示しない。

```text
ToolResult（Search Logicが返す検索結果）
│
├─ status
│   検索が成功・失敗・タイムアウトのどれだったか
│   Search Logicが実行事実から決める
│
├─ candidate_article_ids
│   指定された検索scope内で見つかった候補ArticleのID
│   候補であって、質問との関連性やEvidence採用を意味しない
│
├─ relation_assertions
│   Neo4jから取得した未確認の意味関係候補
│
│   ├─ proposed_predicate
│   │   具体化、準用、定義利用、例外、優先関係のどの候補か
│   │   非同期分類LLMが判断し、検索時Solverが両端本文で再評価する
│   │
│   ├─ subject_article_id / object_article_id
│   │   関係の向きを構成するSUBJECT側とOBJECT側のArticle ID
│   │
│   ├─ basis_edge_id
│   │   この意味候補の根拠となった原文上のREFERENCESのID
│   │
│   └─ supporting_quote
│       非同期分類LLMが分類根拠として示したsource本文中の引用箇所
│
├─ classification_run_id
│   どのpublish済み非同期分類Runから取得した候補かを示す版ID
│   Case開始時にProgramが固定し、検索途中で切り替えない
│
└─ coverage
    当該ClassificationRunの処理件数、判断不能件数、失敗件数
    不完全な場合、候補がないことを意味関係の不存在と解釈しない
```

#### 非同期意味分類時

```text
Data Store: OpenSearch・Neo4j
OpenSearchから両端Article本文、Neo4jから原文REFERENCESと端点・来歴を読む
        │
        ▼
Input: Relation Classification Input
1つのsource content unitと、そこから参照された全targetを一組でLLMへ渡す
        │
        ▼
LLM: Relation Classifier
引用箇所と両端本文を対応付け、意味predicateを判断
        │
        ▼
Output: Relation Classification Output
0件以上のRelationAssertion候補と、意味候補を作らない参照の分類結果を返す
        │
        ▼
Program Validation
既知端点、predicate enum、引用の存在、snapshot・hash・件数だけを検証
        │
        ▼
Data Store: Neo4j
RelationAssertionとClassificationRunを保存し、完了Runだけ一括publish
```

非同期分類のInputとOutputは次を意味する。

```text
Relation Classification Input（非同期分類LLMへの入力）
│
├─ source_article_text
│   参照を書いたArticleの全文
│
├─ source_content_unit
│   実際に参照表現が書かれた条・項・号とその本文
│
├─ references
│   同じsource content unitから解決された全参照先
│   各要素にbasis edge、引用文字列、target Article ID・全文を含む
│
├─ source_snapshot_id / content_hash
│   どの原文版を分類したかを固定する識別情報
│
└─ allowed_predicates
    LLMが選択できる5つの意味関係と、その向きの日本語定義

Relation Classification Output（非同期分類LLMの出力）
│
├─ assertions
│   意味関係がある可能性があるとLLMが判断した候補
│   subject、predicate、object、basis edge、根拠引用を含む
│
└─ outcomes
    意味関係を作らない一般参照、判断不能、分類失敗の結果
    Programはこれを意味Relationへ変換せず、ClassificationRunのcoverageへ集計する
```

Data Storeごとの入出力責務を次に固定する。

| Data Store | 保存する入力 | 読み出す出力 | 保存しないもの |
|---|---|---|---|
| `OpenSearch` | 同一snapshotのArticle全chunkと検索用field | keyword・semantic検索候補、Article全文 | WorkItem、仮説、案件判断 |
| `Neo4j` | 法令構造、原文REFERENCES、EXPLAINS、RelationAssertion、ClassificationRun | 既知Articleとの1ホップ関係候補と分類coverage | 質問ごとの関連性、Evidence採否、回答 |
| `CaseStore` | WorkItem、Hypothesis、ToolResult、候補catalog、Evidence、SolverDecision | ProjectorがAgentViewを作るための案件状態 | 共有法令本文・共有Graphの正本 |

## 4. 反復ループ

### 4.0 人間向けの探索全体像

本節は、後続の状態型・契約・上限を読む前に、探索がどのようにつながるかを理解するための概念図である。
型や状態遷移の正確な仕様は4.1以降を正本とする。

探索の中心は、質問全体を一度に検索することではない。SolverがWorkItemの未確認事項を選び、
それを確かめる行動を実行し、取得本文をEvidenceとしてWorkItemとHypothesisを逐次更新する。

```text
┌─────────────────────────┐
│ 利用者の質問             │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Cycle開始: Solver        │
│                         │
│ ・元の質問を確認         │
│ ・WorkItemを確認・分解   │
│ ・Hypothesisを確認       │
│ ・今回のfocusを決める    │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 未確認事項・gapを選ぶ    │
│ 「次に何を確かめるか」   │
└────────────┬────────────┘
             ▼
       どう発見するか
             │
   ┌─────────┼─────────────────┐
   │         │                 │
   ▼         ▼                 ▼
具体的な候補がある   内容から探す      関係から探す
   │                 │                 │
   │          ┌──────┴──────┐   ┌──────┴────────┐
   │          │ OpenSearch  │   │ Neo4j Graph  │
   │          │ keyword     │   │ 既知の起点    │
   │          │ semantic    │   │ 関係種別・方向│
   │          └──────┬──────┘   │ 1ホップ       │
   │                 │          └──────┬────────┘
   │                 ▼                 ▼
   │          OpenSearch候補       Graph候補
   │                 │                 │
   └─────────────────┴────────┬────────┘
                              ▼
                    Solverが候補を選ぶ
                              │
                              ▼
                 ┌──────────────────────┐
                 │ OpenSearchから本文取得│
                 └───────────┬──────────┘
                             ▼
                         本文Evidence
                             │
                             ▼
                 ┌──────────────────────┐
                 │ Solverが意味を評価   │
                 │ ・Hypothesis更新     │
                 │ ・WorkItem更新       │
                 │ ・gaps更新           │
                 │ ・frontier更新       │
                 └───────────┬──────────┘
                             ▼
                      調査は完了したか
                             │
                 ┌───────────┴───────────┐
                 │                       │
                完了                    未完了
                 │                       │
                 ▼                       ▼
          根拠付き回答を作成      同じ方針を続けられるか
                                         │
                             ┌───────────┴───────────┐
                             │                       │
                        続けられる              仕切り直す
                        予算もある              または予算上限
                             │                       │
                             ▼                       ▼
                      同じCycleで              現Cycleを閉じる
                      次のgapを選ぶ                  │
                             │                       ▼
                             └──────────────→ 次Cycleを開始
                                                   │
                                                   ▼
                                        引継ぎ済みCaseStateを読み、
                                        分解・仮説・方針を再確認
```

3つの探索経路は、次のように使い分ける。

| 状況 | 行動 |
|---|---|
| OpenSearchまたはGraphに具体的な候補Articleが既にある | その候補本文を取得する |
| 制度名、用語、確認したい内容から新しい起点を探す | OpenSearchのkeyword・semantic検索を使う |
| 取得済みArticleから委任先、参照先、準用先等の構造的な関係をたどる | 既知Articleを起点にNeo4jを1ホップ検索する |

Article IDの既知・未知は、探索方法を決める意味判断ではない。本文取得では候補Article ID、
Graph検索では起点Article IDが既知であることをProgramが実行前に検証する。OpenSearchはArticle IDが
分からない状態から候補を発見できる。本文中に現れた条番号を、ProgramやSolverがArticle IDへ組み立てない。

OpenSearchとNeo4jは本文の取得元を分担するものではない。OpenSearchは内容による候補発見とArticle本文取得、
Neo4jは既知Article間の関係による候補発見を担当する。どちらから発見した候補も、本文はOpenSearchから取得する。

```text
                         Articleを発見する経路
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
              OpenSearch                     Neo4j
        内容・用語から候補を探す       既知Articleとの関係から探す
                    │                           │
                    ▼                           ▼
              Article候補                 Article候補
                    └─────────────┬─────────────┘
                                  ▼
                            Solverが選択
                                  ▼
                      OpenSearchから本文取得
                                  ▼
                         grounding Evidence
```

検索候補とGraph候補はナビゲーション情報であり、それだけではHypothesisの支持・反証やWorkItemの解決に
使わない。候補Article本文を取得し、Solverが内容を評価して初めてgrounding Evidenceとして利用できる。

```text
WorkItem: 解く必要がある個別の問い
   │
   └─ Hypothesis: 本文で検証できる命題
          │
          └─ 未確認のgap
                 │
                 ├─ OpenSearch / Graphで候補を発見
                 ├─ Solverが候補を選択
                 └─ 本文EvidenceでHypothesisを更新
                                      │
                                      └─ SolverがWorkItemを解決できるか判断
```

初期時点で検証可能な具体的命題がなければ、法令名・条文番号を推測したHypothesisを無理に作らず、
WorkItemを起点にOpenSearchで候補を発見する。取得本文から委任・参照等が判明した時点で、
その本文に基づくHypothesisとGraph探索を追加する。Graphでは起点、関係種別、方向をHypothesisに沿って扱い、
到達先Articleやその内容を事前に決め打ちしない。

Legal Profileの方向selectorは`from_subject / to_subject`だけを許可する。両方向を必要とする場合は
この2値を明示的に列挙し、`all`や意味の曖昧な別名を使わない。`MENTIONS`はGraph候補発見に使用せず、
ガイドとArticleの明示対応である`EXPLAINS`だけをLegal Graphの関係として扱う。

#### 仮説スコープによる候補発見

検索後に大量の候補を一括で選別するだけでなく、検索要求そのものを現在のWorkItem・Hypothesisへ限定する。
Solverは「何を確かめるための検索か」と、使用する検索経路・selectorを`ExplorationIntent`として返す。
Programは既知ID、許可されたselector、件数、権限、上限を検証してそのまま実行し、Hypothesisの文面から
検索語、関係種別、方向、優先度を補完しない。

```text
WorkItem W1
  └─ Hypothesis H1: 本文で検証する命題
       └─ ExplorationIntent I1
            ├─ objective: 今回確認する不足事項
            ├─ OpenSearch
            │    └─ query / keyword・semantic / 文書範囲
            └─ Neo4j
                 └─ 起点Article / mode / predicateまたは原文関係 / direction / 構造filter
                              │
                              ▼
                    指定scope内の候補だけ取得
                              │
                              ▼
                 Solverが本文取得対象を選び、意味を評価
```

役割と判断境界を含めた探索イメージは次のとおりである。

```text
┌──────────────────────────────────────────────────────────────┐
│ Solver（LLM）                                                │
│                                                              │
│ WorkItem W1                                                  │
│   └─ Hypothesis H1                                           │
│        └─ gap: まだ本文で確認できていないこと                │
│             │                                                │
│             ▼                                                │
│        ExplorationIntent I1                                  │
│        ・何を確認するか                                      │
│        ・OpenSearchかGraphか                                  │
│        ・query / filter または relation selector             │
└───────────────────────────┬──────────────────────────────────┘
                            │ 意味判断済みの検索要求
                            ▼
======================= Program境界 ============================
                            │
             既知ID・allowlist・件数・depth・権限だけ検証
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│ OpenSearch             │   │ Neo4j                       │
│ query / mode / filter  │   │ seed Article                │
│                        │   │ mode / predicate / direction│
│                        │   │ classification run / filter │
└────────────┬───────────┘   └──────────────┬───────────────┘
             │                               │
             └─────────────┬─────────────────┘
                           ▼
                 scope内の候補Node・Link
                           │
                  Frontier(Node × H1)
                           │
======================= Program境界 ============================
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ Solver（LLM）                                                │
│                                                              │
│ 候補をselect / defer / reject                                │
│        │                                                     │
│        └─ selectしたArticle本文を取得                         │
│                    │                                         │
│                    ▼                                         │
│             本文EvidenceでH1を評価                           │
│               ├─ supported   → WorkItemの解決を検討          │
│               ├─ contradicted→ H1前提の作業を見直す          │
│               └─ unresolved  → 同Cycleで追加Intent、          │
│                                  または次Cycleで仮説を再構成   │
└──────────────────────────────────────────────────────────────┘
```

上段と下段のSolverが意味判断を担当し、中段のProgramはSolverが指定したscopeを変更せず実行する。
Program境界をまたいだ時点でHypothesisが正しいと確定したわけではない。検索scopeは候補数を抑えるための
仮説由来の制約であり、Hypothesisの支持・反証は取得したArticle本文を読んだ後にだけ決める。

Legal Profileでは、Graphの意味predicateを空や`all`にして全種別を取得しない。
Solverが現在のHypothesisから必要なpredicateを特定できない場合は、まずOpenSearchで起点または関係を示す
本文を取得する。複数predicateが同じHypothesisの検証に必要なら、Solverが別々のselectorとして理由とともに
明示する。例えば、委任・具体化は`IMPLEMENTS`、準用・読み替えは`INCORPORATES`、定義利用は
`USES_DEFINITION`、例外は`EXCEPTION_TO`、優先関係は`OVERRIDES`を候補にできるが、この対応をProgramの
固定ルールにはしない。明示参照そのものをたどる場合だけ`mode=explicit_reference`を使用する。

OpenSearchも質問全文を毎回無条件に検索せず、SolverがHypothesisの未確認部分から作ったqueryと、必要な場合だけ
文書・法令系統・authority type等のfilterを使う。ただしBM25、vector検索、Graph検索が返す各候補の法的関連性を
検索エンジンが保証するわけではない。「仮説に沿った内容のみ検索する」とは、backendへ渡す探索範囲を
Hypothesis由来の明示scopeへ限定することであり、返却候補を確認なしに根拠採用することではない。

Hypothesisが反証された場合、その`ExplorationIntent`と取得結果は履歴として残すが、新Hypothesisへ自動転用しない。
Solverが既存Nodeを新Hypothesisへ再採用するか、新しいselectorで検索し直す。これにより、最初の仮説が大きく
外れた場合も、次Cycleで探索範囲を仕切り直せる。

同じArticleがOpenSearchとGraph、または複数Hypothesisから発見されても、案件内では1つのNodeへ正規化する。
発見経路はLinkとして全て残し、候補の意味判断は`Node × Hypothesis`のfrontierで分ける。本文は一度だけ取得し、
複数Hypothesisから同じEvidenceを参照できる。

```text
OpenSearch ──────────────┐
                         ├─→ 同じArticle Node ─→ 本文取得は1回
Graph ───────────────────┘             │
                                       ├─ Frontier(Node × H1)
                                       └─ Frontier(Node × H2)
```

Graph最大hopが1の場合、深さ1の候補本文からさらに別規定の確認が必要になっても、そのNodeからGraphを
再展開しない。Solverが本文中の法令名、条番号、確認事項を使ってOpenSearch検索を行い、発見したArticleを
新しい深さ0の起点にする。Graph候補がないことだけを、関係する規定の不存在とは判断しない。

各ToolResultは直後のSolver判断で評価し、WorkItem、Hypothesis、gap、frontierを差分更新する。
Cycle末尾で全結果を初めて要約し直す構造にはしない。現Cycleを閉じた後、次Cycleの最初に保存済みの
CaseStateを読み、元の質問、作業分解、仮説、未確認frontierを再確認して探索計画を立てる。

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
Solverが同じHypothesisについて明示した1ホップGraph検索は同じstepの観察結果に含め、
隣接Article本文は同じCycleの次stepでSolverが選んで取得する。

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
    contract_version: str
    source_snapshot_id: str
    graph_schema_version: int
    classification_run_id: str | None
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

Legal ProfileはCase作成時に同じsnapshotのOpenSearch・Neo4jと、publish済みClassificationRunを固定する。
publish済みRunがない場合は`classification_run_id=None`とし、意味Assertionが利用できないことをAgentViewへ
明示する。実行途中に新Runへ自動切替しない。`contract_version`もCase作成時に固定し、load時に現行契約と
異なる場合は登録済みmigrationまたは旧値読替えを通す。対応経路がなければ実行を再開しない。

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
状態ful agentのstep checkpoint、データ来歴のEntityとDerivationの分離である。法令ドメインについては
LegalRuleMLの、形式化した法的Statementを原文のLegal Sourceへ対応付けるisomorphism、Context、Authority、
Temporal Characteristicの考え方も参考にする。本計画ではLegalRuleMLのXMLや全メタモデルをNeo4jの物理schemaへ
移植せず、Hypothesis・RelationAssertion等の意味仮説と、取得本文、出典、判断時点を混同しないために利用する。
取得元に版・施行期間がない場合は推測せず`unknown`として扱う。外部Frameworkへの依存は追加せず、
この案件に必要な最小型だけを実装する。

- [Generic graph search: frontier](https://artint.info/3e/html/ArtInt3e.Ch3.S4.html)
- [Multiple-path pruning: explored set](https://artint.info/3e/html/ArtInt3e.Ch3.S7.html)
- [Neo4j APOC: BFS/DFS、depth、uniqueness](https://neo4j.com/docs/apoc/current/graph-querying/expand-paths-config/)
- [LangGraph persistence: step checkpoint](https://docs.langchain.com/oss/python/langgraph/persistence)
- [W3C PROV: entity、activity、derivation](https://www.w3.org/TR/prov-primer/)
- [OASIS LegalRuleML Core Specification 1.0](https://docs.oasis-open.org/legalruleml/legalruleml-core-spec/v1.0/os/legalruleml-core-spec-v1.0-os.html)

```python
class ExplorationState:
    nodes: list[ExplorationNode]
    links: list[DiscoveryLink]
    frontier: list[FrontierItem]
    intents: list[ExplorationIntent]

class ExplorationIntent:
    intent_id: str
    work_item_id: str
    hypothesis_ids: list[str]
    objective: str
    discovery_kind: Literal["search", "relation"]
    selectors: dict
    reason: str
    created_cycle: int

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
    intent_ids: list[str]
    relation_types: list[str]
    directions: list[Literal["from_subject", "to_subject"]]
    reference_kinds: list[str]
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

1. Solverは各検索の`ExplorationIntent`へ既知WorkItem・Hypothesis、検証目的、検索経路、selectorを指定する。
   Legal Profileのsearch selectorはquery・検索mode・任意filter、relation selectorは起点Article、
   Graph mode、1つのpredicateまたは原文relation、1つのdirection、分類Run、任意の構造filterを持つ。
   Programは意味的なselectorを追加・削除しない。
2. OpenSearch候補を深さ0のNodeと、IntentのHypothesisに属するfrontierへ追加する。
3. Solverは既知frontier IDから、1 stepで検証する少数の`select`、関連するが今回の
   本文取得枠に入れない`defer`、現在のHypothesisに不要な`reject`を返す。
4. Decisionに現れないfrontierは削除せず`unreviewed`のまま残す。`defer`は
   `relevant_deferred`として同じCycleの後続stepまたは次Cycleへ残す。
5. ProgramはID、selector allowlist、件数、depth、Toolの成功済み重複だけを検証し、
   関連度、優先度、Hypothesisと関係種別の対応を計算しない。
6. 1ホップGraphは、対応Intentが明示したmode・predicateまたは原文relation・direction・構造filterだけを取得して
   同じstepの観察へ追加する。新しい隣接Node本文は同じCycleの
   次step以降に取得する。
7. 同じNodeへ別Linkが追加された場合はLinkとHypothesisの関連だけを追加し、成功済み本文を再取得しない。
   frontierは`Node × Hypothesis`単位にし、あるHypothesisでの`reject`を別Hypothesisへ波及させない。
8. Graph展開済み判定はNode全体ではなく
   `scope_key=(resource_id, mode, predicate_or_relation, direction, structural_filters,
   classification_run_id, policy_version)`単位にする。
   別Hypothesisが同じ物理scopeを要求した場合は既存Linkを再利用して新しいfrontierを作り、Neo4jを再実行しない。
   page cursorとrequest IDは同じExpansionSliceへ蓄積し、pageごとに別scopeを作らない。
9. `partial`と`next_cursor`があるscopeを`complete`として扱わない。未提示候補の不存在を推測しない。
10. `max_exploration_depth`はProfileで`1`または`2`だけを許可する。OpenSearch起点を深さ0、Graph関係を
   1辺たどるごとに深さを1増やす。最大depthのNodeは本文取得とSolverの意味評価を許可するが、そこを
   起点とするGraph展開は実行しない。Programは`minimum_depth < max_exploration_depth`であり、
   かつ既知のrelation用ExplorationIntentがある場合だけ1ホップGraphを実行する。
11. 後から短い経路が見つかった場合、ProgramはNodeと対応frontierの`minimum_depth`だけを小さく更新し、
    過去LinkやCycleRecordを削除しない。
12. `max_exploration_depth`はCase全体に適用する。同じOpenSearch起点からの探索を次Cycleへ引き継いでも
    depthを0へ戻さない。次Cycleの異なる検索で新たに発見したOpenSearch候補だけを新しい深さ0の起点にする。
13. 1 stepの選択件数と1回のGraph取得件数はProfileの機械的上限とし、上限超過候補は削除せず
    `partial`なExpansionSliceの未取得page、または未処理frontierとして残す。Neo4jから取得済みの
    未処理Graph frontierは決定的に分割し、未提示pageを不存在と扱わない。
14. Graph Reviewは全履歴を毎回再評価せず、新しい`unreviewed`候補、新Hypothesisが既存Nodeを
    再採用したことで新たに作られた`Node × Hypothesis` frontier、既評価frontierへ新しいLinkが
    追加された差分を詳細入力とする。過去の評価済みfrontierは短い台帳で参照する。
15. 一度`reject`したfrontierをProgramが別Hypothesisへ自動転用しない。Solverが別Hypothesisの
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
  当該候補について今回までに判明した全relation属性、basis quote、classificationRunIdとcoverage、
  `review_trigger`、直前のreview statusを含める。
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

### 5.1.3 共有法令Graph（Neo4j）の物理定義

Neo4j、OpenSearch、CaseStoreの責務を次のように固定する。

```text
OpenSearch                         Neo4j
法令・ガイド本文の正本             共有法令構造・原文明示参照
Article全chunk取得                 非同期分類済みRelationAssertion
        │                                │
        └──────── Article ID ────────────┘
                         │
                         ▼
CaseStore
案件ごとのWorkItem / Hypothesis / ExplorationIntent /
DiscoveryLink / frontier / Evidence / Solver判断
```

CaseStoreの探索履歴や案件判断をNeo4jへ保存しない。同じArticleを複数案件で共有しても、ある質問での
関連・不関連、仮説支持・反証、RelationAssertionの案件内評価によって共有Graphを変更しない。

#### Node

全Nodeは`:GraphNode`と次の型別labelを持ち、`graphNodeId`を共通の一意IDとする。

| label | 粒度 | 主なプロパティ |
|---|---|---|
| `Document` | 法令またはガイド文書 | `documentId`, `title`, `docType`, `authorityType`, `sourceSnapshotId`, `sourceRevisionId`, `contentHash`, `graphSchemaVersion` |
| `Article` | 条 | `contentUnitId`, `documentId`, `heading`, `articleNumber`, `sourceSnapshotId`, `sourceRevisionId`, `contentHash` |
| `Paragraph` | 項 | `contentUnitId`, `documentId`, `parentContentUnitId`, `paragraphNumber`, `sourceSnapshotId`, `contentHash` |
| `Item` | 号 | `contentUnitId`, `documentId`, `parentContentUnitId`, `itemNumber`, `sourceSnapshotId`, `contentHash` |
| `RelationAssertion` | 非同期LLMが生成した未確認の意味関係候補 | `assertionId`, `proposedPredicate`, `basisEdgeId`, `supportingQuote`, `sourceContentUnitId`, `sourceSnapshotId`, `sourceRevisionId`, `classificationRunId`, `classifiedAt`, `graphSchemaVersion` |
| `ClassificationRun` | snapshot単位の非同期意味分類Run | `classificationRunId`, `phase`, `sourceSnapshotId`, `graphSchemaVersion`, `model`, `promptVersion`, `inputCount`, `processedCount`, `assertionCount`, `referenceOnlyCount`, `uncertainCount`, `failedCount`, `scopeHash`, `publishedAt` |

`Article`を項・号の代用labelにしない。Graph探索をArticle単位へ投影する場合も、元の
`Paragraph / Item.contentUnitId`と親`Article.contentUnitId`を両方保持する。本文はOpenSearchを正本とし、
Neo4jには識別・検索・監査に必要な見出し、revision、hashだけを置く。現在版だけを扱う初期実装では
`Law / LawRevision / Term`等を追加せず、履歴検索を実装するときに別Phaseで導入する。

#### 物理Relation

Neo4jの物理Relationは、決定的に確認できる構造・原文・来歴だけに限定する。法的意味predicateを
同名の物理Edgeとして重複生成しない。

| relation | from | to | 用途 |
|---|---|---|---|
| `HAS_CONTENT_UNIT` | `Document / Article / Paragraph` | `Article / Paragraph / Item` | 文書構造。containerからchild |
| `REFERENCES` | 参照を書いた`Article / Paragraph / Item` | 参照先`Article / Paragraph / Item` | 原文上の明示参照 |
| `EXPLAINS` | ガイド`Document` | 明示的な解説対象`Article` | 対応表・条文注釈等で明示された対応だけ |
| `SUBJECT` | `RelationAssertion` | `Article` | 意味候補のSUBJECT端点 |
| `OBJECT` | `RelationAssertion` | `Article` | 意味候補のOBJECT端点 |
| `CLASSIFIED_IN` | `RelationAssertion` | `ClassificationRun` | 候補を生成・publishした分類Run |

`IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`は物理Relationにせず、
`RelationAssertion.proposedPredicate`の値とする。`APPLIED_BY`と`MENTIONS`は新Graphへ生成しない。
単なる言及はOpenSearch本文として検索し、ガイドとArticleの明示対応だけを`EXPLAINS`にする。

原文Relationは最低限、`graphEdgeId`, `relationSource`, `sourceContentUnitId`, `sourceRevisionId`,
`sourceSnapshotId`, `graphSchemaVersion`を持つ。`REFERENCES`はさらに`citationText`、取得可能なら
`sourceSpanStart / sourceSpanEnd`、`targetResolutionMethod`を持つ。旧`referenceKind`は移行監査用に
読み取ってもよいが、新schemaの意味selectorには使用しない。特にsource content unit全体のキーワードから
付けた`application / definition / exception`等を、個々の参照先の法的意味として扱わない。

#### RelationAssertion

RelationAssertionは「法令間にこの意味関係があり得る」という共有の未確認候補であり、正式Edgeではない。
1 Nodeは1つのpredicate、1組の端点、1つの根拠参照、1つの分類Runに対応する。

```text
(subject:Article)<-[:SUBJECT]-(assertion:RelationAssertion)-[:OBJECT]->(object:Article)

意味上の候補:
subject ── proposedPredicate ──> object
```

下位府令が親法律を明示参照した例は次のように分ける。

```text
原文上の事実:
下位Article ──REFERENCES──> 親Article

未確認の意味候補:
RelationAssertion
├─ SUBJECT → 親Article
├─ proposedPredicate = IMPLEMENTS
├─ OBJECT  → 下位Article
└─ basisEdgeId → 上のREFERENCES
```

`SUBJECT / OBJECT`は端点の役割であり、契約当事者や法律上の主体・客体を意味しない。
RelationAssertionに汎用`status=unverified`を重複保存せず、Nodeとして存在すること自体を未確認候補とする。
同じ端点間でもpredicateまたは根拠箇所が異なれば別Assertionにできる。推移関係をProgramが推論して
新Assertionを書かず、LLMが根拠本文から明示的に分類した直接関係だけを保存する。

predicateと向きを次に固定する。

| `proposedPredicate` | SUBJECT | OBJECT | 例 |
|---|---|---|---|
| `IMPLEMENTS` | 抽象的な親規定 | 具体化する下位規定 | 金商法27条の3 → 公開買付府令10条 |
| `INCORPORATES` | 他規定を準用・読み替える規定 | 取り込まれる規定 | BがAを準用する場合のB → A |
| `USES_DEFINITION` | 定義を利用する規定 | 定義を置く規定 | 利用条文 → 定義条文 |
| `EXCEPTION_TO` | 例外・適用除外を定める規定 | 一般規定 | 施行令7条 → 金商法27条の2 |
| `OVERRIDES` | 優先して適用される規定 | 排除・修正される規定 | 特則 → 一般則 |

公開買付けの適用除外では、法的効果と具体化を別Assertionで表現できる。

```text
施行令7条 ── EXCEPTION_TO ──> 金商法27条の2
施行令7条 ── IMPLEMENTS ────> 公開買付府令2条の5
金商法27条の3 ─ IMPLEMENTS ─> 公開買付府令10条
```

非同期分類結果は共有候補にすぎない。検索時Solverが質問に関係する候補だけ両端Article全文で評価し、
その案件判断をHypothesis・EvidenceとともにCaseStoreへ保存する。Neo4jのRelationAssertionを更新・削除したり、
同名の正式Relationへ自動昇格させたりしない。

現行の`fromArticleId / toArticleId / suggestedType / status`だけを持つNodeは新schemaの正本にしない。
再seedと非同期再分類で`SUBJECT / OBJECT / CLASSIFIED_IN`接続と`proposedPredicate`へ生成し直すため、
旧Graph内でのin-place migrationは行わない。

#### 非同期意味分類とpublish

`/admin/seed`はOpenSearch本文、構造Node、`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`までを作り、
LLM分類の完了を待たず終了する。その後の非同期jobはsource content unitごとに、次を1つの入力として扱う。

- source Article全文と参照を含むcontent unit
- そのcontent unitから解決された全`REFERENCES`と各target Article全文
- 各参照の`citationText`と端点ID
- law family、authority type、snapshot・content hash

参照先ごとにsource content unit全体のキーワードを一律適用せず、LLMが引用箇所と両端本文を対応付けて
0件以上のRelationAssertionを返す。Programは既知ID、predicate enum、端点が入力内にあること、
`supportingQuote`がsource本文に存在すること、snapshot・hash・件数だけを検証し、predicateを補正しない。
`REFERENCE_ONLY / UNCERTAIN / FAILED`はRelationAssertionに変換せず、ClassificationRunの監査件数へ記録する。

分類jobは`source/target content hash + promptVersion + model + graphSchemaVersion`で再開・cache可能にする。
結果は`phase=building`のRunへ書き、入力scopeを処理し終えたRunだけ`phase=published`へ一括publishする。
継続不能なRunは`phase=failed`とする。このphaseはProgram内部の実行事実でありSolverへ判断させない。
Case開始時に最新のpublish済み
`classificationRunId`を固定し、そのCaseの全Graph検索へ渡す。`uncertainCount`または`failedCount`が0でない場合は
Graph ToolResultにcoverageを示し、Assertionがないことを「関係なし」と断定しない。

#### 検索時のArticle投影

Graph Tool Adapterは、正確な端点とArticle単位の探索候補を同時に保持する。

```text
relation metadata
├─ subjectContentUnitId / objectContentUnitId  正確な条・項・号
├─ subjectArticleId / objectArticleId          親Article
├─ mode / proposedPredicateまたは原文relation
├─ basisEdgeId / supportingQuote / classificationRunId
└─ direction: from_subject / to_subject
```

frontierと本文取得はArticle単位にまとめても、どの項・号に記載された関係から発見したかを
DiscoveryLinkのrelation metadataから失わない。Graph候補だけではEvidenceとせず、Article本文はOpenSearchの
`fetch_articles`で全chunkを取得してSolverが評価する。

#### Graph検索selector

Legal ToolはLLM生成Cypherを受け付けず、次の固定modeをparameterized Cypherへ対応させる。

| mode | 必須scope | 用途 |
|---|---|---|
| `semantic_assertion` | 起点Article、`proposedPredicate` 1件、direction 1件、`classificationRunId` | 仮説に沿った意味候補検索。通常経路 |
| `explicit_reference` | 起点Article、direction 1件 | 原文上の明示参照をたどる。通常は`from_subject`だけ |
| `explains` | 起点DocumentまたはArticle、direction 1件 | ガイドの明示対応をたどる |

`semantic_assertion`は必要な場合だけsame law family、target authority type、document ID等の構造filterを追加できる。
意味predicate、direction、構造filterはSolverがHypothesisから選び、Programは補完しない。同じIntentで複数predicate、
両方向、全modeを一括指定せず、必要なら別selectorに分ける。`explicit_reference/to_subject`は高fan-inになるため
通常QAの既定経路にせず、十分限定された監査目的だけ許可する。

Tool Adapterは結果をmaterializeする前に候補件数を確認する。安全上限を超える場合は任意の上位N件へ切り捨てず、
`scope_too_broad`と構造facet別件数を返す。scopeを変更するかOpenSearchへ戻るかはSolverが判断する。
検索は1ホップだけとし、同じscope keyを重複実行しない。

#### Constraint・監査・再構築

少なくとも次を一意制約・indexとして作成する。

```text
UNIQUE GraphNode.graphNodeId
UNIQUE RelationAssertion.assertionId
UNIQUE ClassificationRun.classificationRunId
INDEX  GraphNode.documentId
INDEX  Document.authorityType
INDEX  RelationAssertion.proposedPredicate
INDEX  RelationAssertion.classificationRunId
INDEX  ClassificationRun.sourceSnapshotId
```

seed監査では、Node/Relationの端点型、dangling relation、重複`graphEdgeId`、`MENTIONS / APPLIED_BY`が0件、
許可した物理Relation以外が0件であることを検査する。分類publish監査では、RelationAssertionごとの
`SUBJECT / OBJECT / CLASSIFIED_IN`各1件、predicate enum、非nullな`basisEdgeId`、`supportingQuote`、
同一snapshotの端点・ClassificationRunとの参照整合、重複`assertionId`、Run集計件数を検査する。
さらに、同じ入力snapshotから作ったOpenSearchとNeo4jについてDocument・Article ID、`sourceRevisionId`、
`sourceSnapshotId`、`contentHash`の対応を検査する。入力元がrevision IDを提供しない場合は推測値を作らず
`sourceRevisionId=null`とし、seed runで固定した`sourceSnapshotId`とcontent hashで同時生成を確認する。

Graph schema、抽出規則、法令・ガイド入力のいずれかを変更した場合は`graphSchemaVersion`を更新し、
現行`/admin/seed`でOpenSearchとNeo4jの構造・原文Relationを両方再構築した後、新snapshot用の
ClassificationRunを非同期実行する。旧Runは監査用に参照可能でも新snapshotへ流用しない。
Neo4jだけの再seed経路は設けない。

### 5.2 statusを少数に保ち、意味と決定主体を固定する

statusは「実行事実」と「意味判断」を分離する。同じ文字列を別の軸へ流用せず、LLMへ見せる値は
7.3の共通Prompt語彙を必ず合成する。JSON Schemaの`enum`は形式制約であり、意味定義の代わりにしない。

| 対象 | 値 | 決定者 |
|---|---|---|
| Run | `running / completed / failed / cancelled` | プログラム |
| ToolResult | `succeeded / failed / timeout` | プログラム |
| ClassificationRun phase | `building / published / failed` | プログラム |
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

#### status契約の保守性

保守性向上の目的は、status追加・名称変更・意味変更のたびに、状態型、Provider schema、Prompt、Projector、
validator、Loopを人手で同期する構造をなくすことである。すべてのstatusを1個の巨大Enumや1個の巨大状態機械へ
まとめるのではなく、Run、Tool、Cycle、Step、WorkItem、Hypothesis、Frontier、Graph expansionごとに
所有者を固定した小さい契約として定義する。

```text
対象ごとの説明付きstatus契約（正本）
├─ code                  JSON・永続化で使う値
├─ description           人間とLLMに示す意味
├─ owner                 Program / Solver / Reviewer
├─ persisted             CaseStoreへ保存するか
├─ llm_visible           LLMへ見せるか
└─ allowed transitions   当該対象の機械的な状態変更規則
          │
          ├─ Pydantic型・JSON Schema
          ├─ 共通Promptのstatus用語集
          ├─ 遷移検証
          └─ 契約・網羅性テスト
```

実装では通常の型付きEnumを使い、アプリケーション内部のstatusを生文字列として持ち回らない。
LLM応答、JSON、永続化backendの文字列は境界のPydanticモデルでEnumへ復元し、JSON・DBへ出すAdapterだけが
値へ直列化する。文字列としても比較できる`str, Enum`へ暗黙依存せず、Pydanticモデルで
`use_enum_values=True`を指定して内部表現を文字列へ戻さない。

```python
class FrontierReviewStatus(Enum):
    UNREVIEWED = "unreviewed"
    SELECTED = "selected"
    RELEVANT_DEFERRED = "relevant_deferred"
    REJECTED = "rejected"

class Frontier(BaseModel):
    review_status: FrontierReviewStatus

# 内部の参照
if frontier.review_status is FrontierReviewStatus.RELEVANT_DEFERRED:
    ...

# JSON・DB境界だけで直列化
record = frontier.model_dump(mode="json")
```

statusの型付き読み取りは許可する。別の処理が`FrontierReviewStatus.RELEVANT_DEFERRED`を直接読むことを
一律に隠すため、同じ意味の`has_unresolved_frontier`等を無条件に追加しない。複数箇所で本当に同じ複合条件を
使う場合だけ、名前付きpolicy関数へ一度定義する。禁止するのは、生文字列比較、状態更新の直接代入、
同じ変換・複合条件を複数箇所へコピーすることである。

statusを持つrecordは読み取り専用として扱い、変更は対象ごとのCommandと共通適用関数を通す。
単純な`current status × command → next status`は対象ごとの小さい遷移表を正本にし、既知ID、親子関係、
Cycle上限等の複数recordにまたがる構造条件はCommand適用処理へ一度だけ記述する。Pydantic validatorは
入力形状、適用処理は状態変更規則、Projectorはread model生成を担当し、同じ条件を重複実装しない。

```text
SolverCommand
      │
      ▼
apply_case_command
├─ 対象IDと現在statusを確認
├─ 対象ごとの遷移表を参照
├─ 複数record間の構造条件を検証
├─ 新しいrecordを生成
└─ CaseStoreへ適用
```

`next`と`start_next_cycle`のように、1つの操作を複数フィールドの組合せで表さない。
`ContinueCycle / StartNextCycle / Finalize`等のdiscriminator付きCommand unionを使い、Commandごとの必須欄を
型で分ける。プログラムはCommandの形式、既知ID、上限、許可された状態変更だけを検証し、どのCommandや
意味statusを選ぶべきかは判断しない。

Projectorが元のToolRequest、ToolResult、Evidence等から再計算できる値は、独立して変更可能な第二の正本にしない。
検索効率のためCaseStoreへ最新値をmaterializeする場合も、共通適用関数だけが元の事実と同時に更新し、
破棄して再生成可能な値として扱う。AgentViewは常にCaseStoreから再生成し、AgentViewのstatusをCaseStoreへ
書き戻さない。

status契約の生成と検証は次を満たす。

- Pydanticの`model_json_schema()`をProvider schemaの基礎にし、別の手書きenum一覧を正本にしない。
- 共通Solver Promptのstatus用語集は、`llm_visible=true`の説明付き契約から決定的に生成する。
- Domain Promptはstatusの業務上の使い方を記述してよいが、値と基本定義を別表現で再定義しない。
- 新しいstatusまたはCommandを追加すると、全状態との許可・拒否が未定義なら網羅性テストを失敗させる。
- serialized valueの変更は`contract_version`を上げ、既存Caseのmigrationまたは旧値読替えを明示する。
- Case開始時の`contract_version`とProfile versionを固定し、実行途中で新契約へ切り替えない。
- statusの意味を変更して他の処理の判断条件も実際に変わる場合は、その依存処理を明示的に修正する。
  不要な中間booleanで影響を隠さず、契約テストで見直し漏れを検出する。

機械的statusの意味:

| 対象・値 | 意味 |
|---|---|
| ToolResult `succeeded` | Tool呼出しが正常終了した。`fetch_articles`では要求した全Articleの全登録済みチャンク取得完了も意味するが、内容の関連性、正しさ、仮説支持を意味しない |
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
| content `succeeded` | 当該ArticleについてOpenSearchに登録済みの全本文チャンクを取得した。質問との関連性、根拠採用、元データのインデックス完全性を意味しない |
| content `failed / timeout` | 全本文チャンク取得を完了できず、失敗または時間切れになった。途中pageを部分成功として扱わず、Article不存在も意味しない |
| expansion `not_started` | 当該scopeのGraphをまだ要求していない |
| expansion `pending` | Graph ToolRequestを保存済みで、終端ToolResultが未保存 |
| expansion `partial` | 一部候補だけ取得し、`next_cursor`または未取得範囲が残る |
| expansion `complete` | 当該scopeの取得を完了した。隣接本文の確認完了を意味しない |
| expansion `failed / timeout` | 当該scopeのGraph取得が失敗または時間切れ。関係不存在を意味しない |
| `cycle_budget_reached` | Cycleの本文取得数または他の機械的上限に達し、新しいactionを追加できない |
| `cycle_close_required` | 予約時間を保護するため新しいactionを始めず、現Cycleの終了評価が必要 |
| `cycle_step_timeout` | Cycle予算で短縮された中間呼出しが時間切れ。仮説の否定やprovider障害は意味しない |

#### Article本文取得の完全性

`fetch_articles`の入力単位はArticle、保存するEvidenceの単位はOpenSearch上のArticle・Paragraph・Item
チャンクである。Tool Adapterは各Article IDについて`contentUnitId`の安定順で総件数を確認し、内部page sizeで
全pageを取得する。page sizeは1回のOpenSearch応答件数であり、Article本文の取得上限ではない。

```text
fetch_articles([A, B])
  ├─ Aに属する全chunkをpage取得
  └─ Bに属する全chunkをpage取得
          ↓
全Articleの全chunkが揃った場合だけsucceeded
```

Articleあたりの`max_chunks`を公開Tool契約・Profile上限として設けない。既存の
`LLM_RESEARCH_MAX_CHUNKS_PER_ARTICLE`は新Frameworkの`fetch_articles`取得上限には使用しない。
1回に選択できるArticle数、Cycle内で取得できる重複なしArticle数、Tool・Cycle時間、model context容量は
別の上限として維持する。

Tool Adapterはpage取得中のチャンクを一時バッファへ置き、要求した全Articleの取得が完了してから
ToolResultとEvidenceをCaseStoreへ同じStepの観察結果として適用する。後続pageの失敗・timeout、総件数との
不一致、対象Articleの0件取得があれば`succeeded`にせず、途中チャンクをgrounding Evidenceとして部分commitしない。
本文取得には`partial` statusを追加せず、`partial`はGraph expansionの未取得pageがある状態だけに使用する。

`content=succeeded`は「OpenSearchに現在登録されている全チャンクを取得した」という実行事実である。
元のe-Gov等からOpenSearchへの投入漏れがないことはseed・index監査の責務であり、本文の質問への関連性、
法的意味、Hypothesisの支持はSolverが判断する。

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
    exploration_intent_id: str | None

class ToolResult:
    request_id: str
    status: Literal["succeeded", "failed", "timeout"]
    evidence_ids: list[str]
    error_code: str | None
    elapsed_ms: int
```

ToolRequestは実行前にCaseStateへ保存し、ToolResultの`request_id`は既知のToolRequestと完全一致させる。
候補発見・Graph展開・本文取得を行うToolRequestは`exploration_intent_id`を必須とし、同じWorkItem・Hypothesisに
属する既知Intentと完全一致させる。CaseStore内の既知Evidenceを再読込するだけの`load_evidence`等はnullを許可する。
これによりToolResultから検証対象WorkItem、Hypothesis、検索scopeを必ず逆引きできる。
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

`fetch_articles`で新しく取得したArticleは、そのArticleに属する全Evidence chunkを次のSolver判断へ
一度まとめて提示する。ProjectorはArticleの一部chunkだけを表示して全文提示済みに見せず、Article途中で
文字数上限へ達する場合は`context_capacity_exceeded`を明示する。過去Evidenceをmanifestへ退避する処理と、
新規Article本文の完全提示を混同しない。Solverが法的関連性を評価する前にProgramが項・号を選別しない。

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
    articles: list[GraphCandidateArticle]  # 重複排除した候補Article一覧
    links: list[GraphCandidateLink]        # 起点から候補を発見した全経路

class GraphCandidateArticle:
    article_id: str        # 候補Articleの既知ID
    document_id: str | None  # 所属する法令・ガイド文書のID
    title: str | None      # 文書名
    heading: str | None    # 条文見出し
    content_status: Literal["not_requested", "pending", "succeeded", "failed", "timeout"]  # Article全文の取得状態

class GraphCandidateLink:
    seed_article_id: str       # Graph検索の起点Article ID
    candidate_article_id: str  # 発見した候補Article ID
    work_item_ids: list[str]    # この発見経路を必要としたWorkItem
    hypothesis_ids: list[str]   # この発見経路で検証するHypothesis
    relations: list[dict]       # 関係mode、意味predicateまたは原文関係、向き、根拠、分類Run、来歴

class SolverToolResult:
    request_id: str       # 対応する既知ToolRequestのID
    status: Literal["succeeded", "failed", "timeout"]  # Toolの実行結果
    evidence_ids: list[str]       # 本文EvidenceのID。Graph navigation Evidence IDは載せない
    evidence_count: int           # 生成した本文Evidenceの件数
    graph_projection_updated: bool  # Graph候補がCaseStoreのcatalogへ反映されたか
    error_code: str | None        # 失敗・timeout等の機械的な理由コード
    elapsed_ms: int               # Tool実行時間
```

Articleの同一性は`article_id`、Linkの同一発見経路は
`(seed_article_id, candidate_article_id)`を決定的な正規化単位とする。
同じ組に複数のrelationがある場合は、`mode`、`proposedPredicate / rawRelation`、`direction`、
`basisEdgeId`、`classificationRunId`、`sourceKind`の
異なる値を失わず`relations`へ保持する。この正規化は同一IDと関係属性の機械的統合であり、
どのLinkが質問に関係するかをプログラムが判断する処理ではない。

Graphの次pageがまだ取得されていない場合は候補を推測せず、ExpansionSliceの`partial`と`next_cursor`を示す。
探索用Evidence本文を文字数上限で省略しても、今回の`graph_review_batch`と
`graph_review_ledger`に含むArticle ID、法令名、条番号・見出し、minimum depth、content status、review status、
起点Article・Link、mode・predicateまたは原文relation・direction・classificationRunId・sourceKind、
ExpansionSliceの`partial / complete`は
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

Legal Profileは`legal_graph_neighbors`をread-only Toolとして登録する。Solverはrelation用
`ExplorationIntent`へ、対象Hypothesis、既知の起点Article、Graph mode、1つのpredicateまたは原文relation、
1つのdirection、必要な構造filterを明示する。ProgramはCaseに固定された`classificationRunId`を加えて
Tool引数へ機械的に投影し、本文取得を選んだという理由だけで全predicateを自動取得しない。
起点本文を取得済みの場合はGraphを直ちに実行でき、同じDecisionで本文取得と
relation Intentが明示された場合は同じStepの観察へまとめる。本文を読んで初めて関係探索が必要と判明した場合は、
直後のSolver判断で新しいIntentを作り、同じCycleの次Stepで実行する。

選択Nodeが最大depthならGraphを実行しない。同じArticle・scopeのGraphは成功後に重複実行せず、別Hypothesisが
同一scopeを要求した場合は保存済みLinkを再利用する。取得した1ホップは各隣接ArticleをExplorationStateの
Node・Link・frontierへ保存する。Solverがfrontierから選んだArticle本文を同じCycleの次stepで取得しても、
relation Intentがなく、または最大depthなら、そのArticleからGraphを再展開しない。

Solverは5つの`proposedPredicate`、原文`REFERENCES / EXPLAINS`、起点から見た
`from_subject / to_subject`を共通Promptの定義どおりに解釈する。候補表示だけで法的結論を出さず、
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
値、基本的な意味、決定主体は5.2の説明付きstatus契約から共通Solver Prompt用語集へ生成し、
research・integrationの両方へ必ず合成する。次の契約語彙には、値の定義に加えて誤解しやすい相互関係と
使用規則を記載する。段階別Promptへ別表現で重複させない。

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
- content not_requestedは本文未要求、pendingは要求済み・結果待ち、succeededは当該Articleについて
  OpenSearchに登録済みの全本文chunk取得済み、failed/timeoutは全chunk取得失敗・時間切れである。
  content succeededは原データのindex完全性、relevant、supportedを意味しない。
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
  depthが上限未満で、relation用ExplorationIntentに既知の起点Articleと明示selectorがある場合だけ、Programが
  そのscopeの1ホップ候補を取得する。上限depthのArticleは本文を取得・評価できるが、そこからGraph候補は増えない。
  Cycle変更は既存起点のdepthをリセットしない。
- 候補発見・Graph展開・本文取得の各ToolRequestは、今回検証する既知WorkItem・Hypothesisと
  ExplorationIntentへ結び付ける。具体的Hypothesisを立てる前の初回検索だけは、理由を示したWorkItem単位の
  search Intentを許可し、Graph Intentには使わない。
  OpenSearchでは未確認事項から作ったqueryと必要最小限のfilter、Graphでは既知の起点Article、mode、
  1つのpredicateまたは原文relation、1つのdirection、必要な構造filterを明示する。predicateを空やallにして
  全種別を要求しない。predicateを選べない場合は、Graphを全探索せずOpenSearchで関係を示す本文または新しい起点を探す。
- Hypothesisとpredicateの対応、検索語、filter、方向、優先度はSolverが判断する。Programへ補完を要求しない。
  検索結果は指定scope内の候補であり、Hypothesisの支持を意味しない。本文取得後に意味を判断する。
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
- content statusのnot_requestedは未要求、pendingは結果待ち、succeededは当該ArticleについてOpenSearchに
  登録済みの全本文chunk取得済み、failedは全chunk取得前のエラー終了、timeoutは時間切れである。
  succeededは原データのindex完全性、法的関連性、根拠採用を意味しない。
- 検索本文中の条番号、法令番号、documentIdからArticle IDを生成しない。必要な参照先IDが
  fetchable_article_idsになければfetch_articlesから外し、法令名・条番号・確認事項でlegal_searchする。
  Decisionを返す直前に、fetch_articlesの全IDをfetchable_article_idsと完全一致で照合する。
- 質問に関係すると判断した1ホップ候補は、Graph Reviewごとに最大3件、かつCycleの
  残り本文取得枠内でselectする。関連するが枠に収まらない候補はdeferし、
  graph_review_ledgerと次Cycleの引継ぎ候補へ残す。Graph候補だけを根拠にせず、端点Article本文を確認する。
- Graph mode `semantic_assertion`は非同期分類済みの未確認候補、`explicit_reference / explains`は
  原文またはガイド上の明示関係である。
- relation_assertionの`proposedPredicate`は候補となる意味関係であり、法的に確認済みの正式関係ではない。
  RelationAssertionとして存在すること自体が未確認を意味する。`SUBJECT / OBJECT`の両端Article本文を取得し、
  今回の質問における意味はSolverが判断してCaseStoreへ保存する。Neo4jの候補を更新・昇格しない。
- REFERENCESはfrom本文がtoを明示参照する。意味候補は、IMPLEMENTS=親規定から具体化規定、
  INCORPORATES=準用・読み替える規定から取り込まれる規定、USES_DEFINITION=利用規定から定義規定、
  EXCEPTION_TO=例外規定から一般規定、OVERRIDES=優先規定から排除・修正される規定である。
  `from_subject`は起点がSUBJECT/from側、`to_subject`は起点がOBJECT/to側である。
- `MENTIONS`はLegal Graphの関係種別ではない。単なる言及をGraph候補、本文取得対象、根拠として扱わず、
  ガイドと条文の明示的対応だけを`EXPLAINS`として扱う。
- 旧referenceKindは移行監査情報であり、意味predicateや法的結論に使用しない。RelationAssertionでは
  `basisEdgeId / supportingQuote / classificationRunId`を確認し、両端本文で今回のHypothesisとの関係を判断する。
- ClassificationRunのcoverageにuncertainまたはfailedがある場合、Assertionがないことを関係不存在と解釈しない。
- relationSource、sourceId、derivedFromEdgeId等の生成元・監査用来歴はCaseStateに保持されるが、
  SolverContextへは重複投影されない。Solverはcatalogに示された関係属性と取得本文で判断する。
```

Reviewer Promptには`accept=指摘なし・findings空`、`revise=具体的findingsあり`を定義する。
プログラム内部だけの`RunStatus`、`stop_reason`、trace error codeはSolverへ渡さず、Prompt語彙を増やさない。
status追加・名称変更時は5.2の説明付きstatus契約を変更する。Provider schemaと共通Promptのstatus用語集は
同契約から生成し、手作業でenumと意味を同期しない。serialized valueを変更する場合はProfile versionだけでなく
`contract_version`と既存Caseのmigrationまたは旧値読替えを同じ変更単位で追加する。

### 7.4 Graph差分Review・Cycle予算に伴うPrompt変更

statusの値と基本定義は5.2の契約から共通Promptへ生成する。次表は自動生成するstatus用語集の重複ではなく、
各処理モードで値をどう使うかという手順・業務上の制約である。コードと状態型の変更と同じcommitで更新し、
Promptだけを先行させて現行SolverContextに存在しない値をLLMへ指示しない。

| Prompt / schema | 必須変更 |
|---|---|
| `solver_common.md` | Graph ReviewはSolverの処理モードであり、任意のReviewer Agentとは別であることを定義する。 |
| `solver_common.md` | Cycleは最大4、1 Cycleの本文取得累計は4、Graph Review選択は最大3と定義する。`max_tool_requests_per_step`と本文取得累計を区別する。 |
| `solver_common.md` | `cycle_budget_reached`、`cycle_close_required`、`cycle_step_timeout`、`remaining_fetch_capacity`の意味と決定主体を定義する。 |
| `solver_common.md` | `unreviewed / selected / relevant_deferred / rejected`と`select / defer / reject`を定義し、content statusと混同しないよう指示する。 |
| `solver_common.md` | `fetch_articles`のcontent `succeeded`は当該Articleの全登録済みchunk取得完了を意味し、index完全性・関連性・根拠採用を意味しないと定義する。本文取得に`partial`を導入しない。 |
| `solver_common.md` | 評価済みNodeを別Hypothesisへ使う場合は`frontier_re_adoptions`で明示し、Programが自動転用しないことを定義する。 |
| `solver_common.md` | 各検索を既知WorkItem・Hypothesis・ExplorationIntentへ結び付け、OpenSearchとGraphの明示selector、候補と根拠の違い、selectorをProgramへ補完させないことを定義する。 |
| `solver_common.md` / `solver_graph_review.md` | RelationAssertionは`SUBJECT / OBJECT`で接続された未確認候補で、`proposedPredicate`は確定関係ではないと定義する。5 predicateの向き、`ClassificationRun` coverage、検索時の案件判断をNeo4jへ更新・昇格しないことも定義する。 |
| `solver_research.md` / `solver_integration.md` | 未確認事項から検証目的と最小scopeを作る。Graphの関係種別を選べなければ全種別を要求せず、OpenSearchで根拠または起点を発見する。 |
| `solver_graph_review.md` | 累積`graph_candidate_catalog`全件ではなく、`graph_review_batch`と`graph_review_ledger`だけを読む。`review_trigger`を解釈し、過去の詳細が再提示されないことを候補の不存在と解釈しない。 |
| `solver_graph_review.md` | 各batchの全候補をWorkItem・Hypothesis別に評価し、最大3件を`select`、関連する残りを`defer`、無関係と判断したものだけを`reject`する。 |
| `solver_graph_review.md` | `remaining_fetch_capacity=0`なら新たにselectせず、関連候補をdeferしてCycle終了判断へ戻す。Graph Reviewから直接次Cycleの法的方針を決めない。 |
| `solver_integration.md` | Cycle上限に達したら、直前までのToolResultを評価し、Hypothesis・WorkItem・Evidence・Graph ledgerを整理した後に、finalizeまたは次Cycleのgoal・strategy・再採用frontierを返す。 |
| `solver_integration.md` | Cycle境界でactiveな`relevant_deferred`全件を`fetch_next_cycle / carry_forward / no_longer_needed / unresolved_at_limit`のいずれかへ明示し、黙って破棄しない。 |
| Provider schema | Review判断対象は現在のbatch、本文取得へ選べるIDはbatchの候補とledgerの`relevant_deferred`、再試行時の`selected + failed/timeout`に制限する。選択上限は`min(3, remaining_fetch_capacity)`とする。`rejected`は新Link差分でbatchへ再提示された場合を除き同じHypothesisで再選択させず、別Hypothesisへの`frontier_re_adoptions`はledgerの既知Nodeと既知のopen WorkItem・Hypothesisだけを許可する。候補の関連性や優先度はschemaまたはProgramで補正しない。 |
| Provider schema | Deferred解消はledgerの既知IDだけを許可する。Programは全件性と次動作との矛盾だけを拒否し、関連性・必要性を補正しない。 |
| Provider schema | Graph Reviewモードで必ず空になるdependency、re-adoption、deferred解消、answerは空配列またはnullの簡易schemaとし、未使用の動的enumをコンパイルさせない。 |
| Provider schema | ExplorationIntentのWorkItem・Hypothesis・起点Articleは既知ID enum、Graph mode、predicateまたは原文relation、direction、構造filterはLegal Tool allowlistへ限定する。predicateは5種、directionは`from_subject / to_subject`だけを許可し、空・all・複数predicateの一括指定を許可しない。`APPLIED_BY / MENTIONS`をenumへ含めない。 |

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
各`fetch_articles` ToolResultが要求した全Articleの全登録済みchunkを取得して`succeeded`になった時点で、重複なしのArticle IDを
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
- `sourceSnapshotId / graphSchemaVersion / classificationRunId`と分類coverage件数
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
│   ├── contracts.py                 # discriminator付きSolverCommand / CaseUpdate / ImpactDecision
│   ├── state_contracts.py           # 説明付きstatus契約・対象別の小さい遷移表・契約version
│   ├── state.py                     # 型付きCaseState / WorkItem / Hypothesis / Evidence / CycleRecord / StepRecord
│   ├── transitions.py               # Command適用・複数record間の構造条件・直接status更新の唯一入口
│   ├── exploration.py               # Node / Link / frontier / expansionの汎用構造
│   ├── loop.py                      # Cycle内step反復・最大4 cycle・予算終了・Reviewer分岐
│   ├── context.py                   # WorkTree・探索frontier・focus・Evidenceの機械的表示
│   ├── validation.py                # 既知ID・権限・上限等の構造検証。状態遷移規則を重複定義しない
│   ├── contract_rendering.py        # Provider schema基礎・LLM-visible status用語集の決定的生成
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
│       ├── graph_schema.py          # predicate・方向・selector・RelationAssertion契約
│       ├── relation_classification.py # 非同期分類の入出力・検証・Run publish
│       ├── profiles/
│       │   └── default.yaml
│       └── prompts/
│           ├── solver_common.md
│           ├── solver_research.md
│           ├── solver_integration.md
│           ├── solver_graph_review.md
│           ├── relation_classifier.md
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

scripts/
└── classify_legal_relations.py      # 再開可能な非同期分類jobのCLI入口
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
| Projector | 独立Agent・独立サービスにはしない。`context.py`の決定的なAgentView投影処理として残す |
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
- 現行のstatus、judgment、action、Command、定義箇所、決定主体、永続化有無、LLM表示有無を棚卸しし、
  同じ値・意味・変換が`state.py / contracts.py / context.py / validation.py / structured_json.py / Prompt`へ
  重複している一覧を作る。
- 対象ごとの説明付きstatus契約、discriminator付きCommand、遷移表、`contract_version`のfixtureを作る。
- Provider schemaと共通Prompt用語集を契約fixtureから生成し、手書きenum・手書き基本定義との二重管理を
  新Frameworkへ持ち込まない。
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
- statusまたはCommandをfixtureへ追加したとき、Provider schema、Prompt用語集、遷移網羅性のいずれかが
  未定義なら契約テストが失敗する。

### Phase 1: 最小Framework

- `agent_framework/state_contracts.py`を説明付きstatus契約の正本として実装し、
  `contracts.py`、`state.py`、`transitions.py`、`contract_rendering.py`、`loop.py`を接続する。
- status-bearing recordは型付きstatusを持ち、内部で生文字列を持ち回らない。LLM・JSON・永続化境界で
  PydanticがEnumとの相互変換を行い、内部処理はEnumメンバーを参照する。
- statusの直接代入を廃止し、対象ごとのCommandを`transitions.py`で適用して新しいrecordを作る。
  型付きstatusの読み取りは許可し、同じ意味を隠すだけの中間booleanを増やさない。
- `ContinueCycle / StartNextCycle / Finalize`をdiscriminator付きCommand unionにし、複数フィールドの
  組合せで次動作を表現しない。
- Provider schemaと共通PromptのLLM-visible status用語集を説明付き契約から生成する。
  Domain Promptには処理モード固有の使用規則だけを残す。
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
- 状態型ごとに`current status × Command`を網羅し、許可・拒否が未定義の組合せを失敗させる。
- `context.py`、`validation.py`、`loop.py`、Provider adapter、Promptに同じstatus変換表やenum一覧が
  残っていないことを契約テストで確認する。
- status recordのJSON保存・復元、未知値拒否、serialized value変更時の`contract_version`不一致と
  migration/旧値読替えをClaude APIなしでテストする。

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
- 50,000文字以内のEvidence本文が決定的な順序で提示され、過去・保持Evidenceの上限外本文はmanifestから
  再取得できる。新規取得Articleは全chunkを原子的な提示単位とし、途中だけを表示しない。
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
- 型付きstatusは他処理から読み取れるが、共通Command適用処理を迂回して直接変更できない。
- 説明付きstatus契約の変更がProvider schemaと共通Prompt用語集へ自動反映され、手修正を要求しない。

### Phase 2: 法令の薄い縦切り

- Legal Domain Packと法令Promptを実装する。
- 既存OpenSearch、Neo4j、本文取得をLegal Tool Adapterとして接続する。
- `fetch_articles`はArticleごとの件数上限で打ち切らず、OpenSearchの総件数を確認して安定順に全pageを取得する。
  内部page sizeとArticle取得上限を分離し、全件取得後だけToolResult・contentを`succeeded`にする。
  途中失敗・timeout・0件取得では部分Evidenceをcommitせず、Article単位で再試行可能にする。
- 5.1.3のNeo4j物理定義を実装する。`:GraphNode`に`Document`、`Article`、`Paragraph`、`Item`、
  `RelationAssertion`、`ClassificationRun`の型別labelを付け、物理Relationは`HAS_CONTENT_UNIT`、
  `REFERENCES`、`EXPLAINS`、RelationAssertion用`SUBJECT / OBJECT / CLASSIFIED_IN`だけを生成する。
  `IMPLEMENTS / INCORPORATES / USES_DEFINITION / EXCEPTION_TO / OVERRIDES`は`proposedPredicate`に保存し、
  `APPLIED_BY / MENTIONS`を生成しない。項・号をArticleへ置換せず、
  Article単位の探索投影にも正確なContent Unit IDを残す。
- `/admin/seed`はOpenSearch本文、構造、明示`REFERENCES / EXPLAINS`までを決定的に作り、LLM分類を待たず終了する。
  source content unitと全参照先Articleを一組で分類する再開可能な非同期jobを実装し、既知ID・predicate enum・
  quote・snapshot・hashの構造検証後にRelationAssertionを作る。Programは分類結果を補正しない。
- 分類結果を`ClassificationRun`へ集計し、完了Runだけ一括publishする。RelationAssertionを
  `SUBJECT / OBJECT / CLASSIFIED_IN`各1本で接続し、`basisEdgeId / supportingQuote / classificationRunId`を
  保存する。旧`fromArticleId / toArticleId / suggestedType / status`を新schemaの正本にしない。
- Case開始時に`sourceSnapshotId / graphSchemaVersion / classificationRunId`を固定し、検索時案件判断は
  CaseStoreだけへ保存する。分類jobと検索時Solverの責務を混同しない。
- OpenSearch・Graphの各ToolRequestを既知の`ExplorationIntent`へ結び付け、Solverが明示したHypothesis由来の
  query・filter、または起点・mode・1 predicateまたは原文relation・1 direction・構造filterだけをbackendへ渡す。
  現行Profileの固定`[REFERENCES, IMPLEMENTS, APPLIED_BY]`による無条件Graph取得は廃止する。
- Legal Tool Adapterは自由Cypherを受け付けず、modeとdirection別の固定parameterized Cypherを使う。
  materialize前の候補件数が安全上限を超えた場合は上位N件へ切り捨てず、`scope_too_broad`とfacet件数を返す。
- Graph方向の外部契約を`from_subject / to_subject`へ統一する。Tool AdapterはNeo4jのfrom/toと検索起点から
  directionを決定し、旧称をPrompt、Provider schema、ToolResult、CaseStoreの新規データへ出さない。
- `APPLIED_BY / MENTIONS`をLegal ontology、seed、Neo4j、Graph Tool allowlist、Promptから削除する。
  旧`referenceKind`を意味selectorから外し、原文`REFERENCES`には引用箇所と抽出来歴を保存する。
  schema versionを更新し、同じ入力snapshotからOpenSearchとNeo4jを両方再構築する。
  `EXPLAINS`以外の単なるガイド言及をGraph関係へ変換しない。
  実装時は`graph_edge_construction.md`、edge registry、Graph監査、seedテストを同じ変更単位で更新する。
- OpenSearch候補とGraph候補をLegal Resource Node・DiscoveryLink・frontierへ投影する。
- 同じDecisionで本文取得とrelation Intentが明示された場合は1ホップGraphを同じStepの観察へ入れる。
  本文から必要性が判明した場合は次Stepのrelation Intentとして実行する。隣接本文取得は現Cycleの
  残り本文取得枠内でSolverが`select`した対象に限定し、枠外の関連候補は`defer`して次Cycleへ残す。
- Profileの`max_exploration_depth`に達したArticleでは本文だけを取得し、relation IntentがあってもGraphを抑止する。
- Graphのmode・predicateまたは原文relation・direction・classification run・構造filter・cursor・policy versionを
  ExpansionSliceのscopeへ対応付ける。
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
- seed後に`Document / Article / Paragraph / Item / RelationAssertion / ClassificationRun`のlabel・端点型・一意制約が
  5.1.3と一致し、項・号NodeがArticle labelを持たないことを確認する。
- publish済み全RelationAssertionが`SUBJECT / OBJECT / CLASSIFIED_IN`を各1本持ち、既知Articleと
  ClassificationRunへ接続し、5種の`proposedPredicate`、非nullな`basisEdgeId / supportingQuote`の整合が取れる。
  旧`status`を正本にせず、未確認候補を正式な物理意味Relationへ自動昇格させない。
- 同一source content unitに複数参照先と異なる意味があるfixtureで、非同期LLMへ全端点と引用箇所が渡り、
  Programが全参照先へ同じpredicateを複製しない。分類再開・cache・Run単位publishを確認する。
- Caseが固定した`classificationRunId`以外のAssertionをTool Adapterが返さず、Run coverageに
  uncertain/failedがあるとき不在を関係不存在として扱わないPrompt契約を確認する。
- 同一seed manifestのOpenSearchとNeo4jでDocument・Article ID、`sourceSnapshotId`、取得可能なsource revision、
  content hashが対応し、
  Graph schema変更時に両方が再構築される。Neo4jだけを更新した不一致状態を成功扱いしない。
- 1 pageを超えるArticle fixtureで全登録済みchunkがEvidenceとして保存され、最終pageまで取得後だけ
  `succeeded`になる。後続page失敗・timeout・0件fixtureは`succeeded`にならず、部分Evidenceが残らない。
- 新規取得Articleの全Evidence chunkが次のSolver判断へ一度提示され、ProjectorがArticle途中を切って
  全文提示済みに見せない。全文がmodel contextへ収まらないfixtureは`context_capacity_exceeded`になる。
- 5つの意味predicateと原文`REFERENCES / EXPLAINS`の意味をFrameworkが判断しない。
- 同じ起点Articleに複数predicateがあるfixtureで、Solverが指定したmode・1 predicate・1 direction・構造filter以外を
  Tool Adapterが返さず、Programが未指定predicateを追加しない。
- directionの契約テストで`from_subject / to_subject`だけが入出力可能で、旧称がPrompt・schema・ToolResultへ
  現れないことを確認する。Neo4jのfrom/toは変更せず、起点がfromなら`from_subject`、toなら`to_subject`になる。
- seed後のGraph inventory、Legal Tool allowlist、Prompt、Provider schemaに`APPLIED_BY / MENTIONS`が存在せず、
  明示的なガイド・条文対応の`EXPLAINS`は維持されることを確認する。
- 公開買付けfixtureで、`EXCEPTION_TO/to_subject`により金商法27条の2から施行令7条、
  `IMPLEMENTS/from_subject`により施行令7条から府令2条の5、金商法27条の3から府令10条を候補取得できる。
  Graph候補だけを根拠にせず、取得した両端本文をSolverが評価する。
- raw `REFERENCES/to_subject`の高fan-in fixtureは通常QA selectorとして拒否または`scope_too_broad`になり、
  候補を任意の上位N件へ切り捨てない。
- predicateを選べないfixtureでは、Solverが全Graph意味関係を要求せず、Hypothesisに沿った`legal_search`で
  新しい根拠または起点を発見できる。
- 別Hypothesisが同じGraph scopeを指定したfixtureでNeo4jを再実行せず、既存Linkから新しい
  `Node × Hypothesis` frontierを作る。
- 同じArticleへ複数経路があるfixtureで本文取得は1回、DiscoveryLinkは複数残る。
- A→B→Aの循環fixtureで再帰展開が停止する。
- `max_exploration_depth=1`で、1ホップ候補の本文取得と、その候補からのGraph非実行を確認する。
- 最大depthまたはGraph関係欠落のfixtureで、Solverがopen WorkItemに基づく`legal_search`を選び、
  結果を新しい深さ0起点として同じCaseで探索できる。
- 関係するとSolverが判断した未確認frontierが残る場合、通常の`finalize`を選ばないPrompt契約を確認する。
- Evidence本文が省略されても、当該Graph review batchにはArticle ID、法令名、条番号・見出し、
  content status、depth、起点Article・Link、mode・predicateまたは原文relation・direction・
  basisEdgeId・supportingQuote・classificationRunId・sourceKind、
  `partial / complete`が残り、
  ledgerの`relevant_deferred` frontierも既知IDとして選べる。全履歴はCaseStoreで監査できる。
- 100件以上のGraph候補fixtureで、Review入力が累積全件ではなくProfileのpage上限内に収まり、
  全pageが最終的に`selected / relevant_deferred / rejected`のいずれかになる。
- 既評価frontierへ新Linkが追加されたfixtureでは、その候補と今回までの関係情報だけが差分batchへ戻り、
  Solverの再判断後も過去DecisionとLink履歴がCaseStoreに残る。
- 同一候補Articleを複数起点から発見するfixtureで、Articleは1件、Linkは全経路分残り、
  Graph Evidence IDがmanifest、ToolResult、navigation・omitted ID一覧に現れないことを確認する。
- Legal PromptがGraph mode、RelationAssertionの5つの`proposedPredicate`、basis、classification coverage、
  sourceKind、directionを
  7.3どおり定義する。

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

- statusの値・基本定義・決定主体を、説明付きstatus契約とProvider schema、Promptへ別々に手書きする。
- アプリケーション内部でstatusを生文字列として比較する、またはCommand適用処理を迂回して直接代入する。
- 対象ごとの小さい遷移規則を、`context.py`、`validation.py`、`loop.py`等へ重複実装する。
- 型付きstatusの読み取りを一律に隠すためだけに、同義の中間booleanや第二のstatusを追加する。
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
- LLM-visible statusを、意味と決定主体を持つ説明付き契約と、そこから生成されるPrompt用語集なしに追加・変更しない。
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

### 修正0: status契約の保守性

1. 現行のstatus、judgment、action、Commandと、型・Provider schema・Prompt・Projector・validator・Loopの
   重複定義を棚卸しする。旧経路の互換処理と新Frameworkの正本を区別する。
2. `state_contracts.py`へ対象別の説明付きstatus契約と小さい遷移表を定義する。巨大な共通Enumや
   法令固有statusを汎用Frameworkへ持ち込まない。
3. LLM出力をdiscriminator付きCommand unionへ変更し、`next + start_next_cycle`等の矛盾可能な組合せを廃止する。
4. status-bearing recordを型付きにし、LLM・JSON・永続化境界でだけ文字列とEnumを変換する。
5. statusの更新を`transitions.py`へ集約する。型付き読み取りは許可し、生文字列比較、直接代入、同じ条件の
   複数実装を新Frameworkから除く。
6. Pydantic型からProvider schemaの基礎を生成し、`llm_visible`な説明付き契約から共通Prompt用語集を生成する。
   Domain Promptには各モードの使用方法だけを残す。
7. status・Command追加時の遷移網羅性、Schema、Prompt用語集、JSON round-trip、未知値拒否、
   `contract_version`とmigrationをClaude APIなしの契約テストへ追加する。
8. 旧status文字列と手書きenumの参照がなくなったことを確認してから、互換変換と重複validatorを削除する。

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

1. `fetch_articles`をArticle全chunkの安定順page取得へ変更する。Articleあたりの取得件数上限を廃止し、
   全page取得後だけToolResult・contentを`succeeded`にする。途中失敗・timeout・0件では部分Evidenceを
   CaseStoreへcommitしない。新規取得Articleの全chunkを次のSolver判断へ一度提示する。
2. 5.1.3のNeo4j schema、Relation、制約、監査を実装する。RelationAssertionを`SUBJECT / OBJECT`で
   Articleへ、`CLASSIFIED_IN`でClassificationRunへ接続し、旧端点ID・statusだけのNodeを生成しない。
   `APPLIED_BY / MENTIONS`を削除し、5つの意味predicateは物理Edgeでなく`proposedPredicate`へ保存する。
   Graph schema versionを更新した`/admin/seed`でOpenSearchとNeo4jの構造・原文Relationを同じ入力snapshotから
   両方再構築し、その後に再開可能な非同期LLM分類をRun単位でpublishする。
3. OpenSearch候補を深さ0のNode・Link・frontierへ変換する。
4. `ExplorationIntent`をLegal Tool契約へ接続し、OpenSearchはquery・filter、Graphは起点Article・
   mode・1 predicateまたは原文relation・1 direction・構造filterをSolverの明示scopeどおり実行する。固定全種別の
   自動Graph取得を廃止し、同じDecisionの本文取得とrelation Intentは同一Cycleの観察へまとめる。
   Profileの`max_exploration_depth=1`に達したGraph候補Articleからの再展開を止める。
   directionは`from_subject / to_subject`だけを許可し、Neo4jのfrom/toと起点から決定する。
   `APPLIED_BY / MENTIONS`と旧`referenceKind`はLegal Toolの意味selectorに含めない。
5. Graph候補をArticle Node・DiscoveryLink・ExpansionSliceへ変換し、全件をCaseStoreに保持する。
   正確なsubject/object Content Unit IDと親Article IDをrelation metadataへ残す。
6. 複数経路の同一Articleを1 Nodeへ統合し、Linkは失わない。
7. `graph_review_batch`に新規`unreviewed`・再採用候補・既評価候補の新Link差分と必要Link、`graph_review_ledger`に
   過去の全評価済みfrontierの短い台帳を投影する。過去の全Link詳細とLLM生応答を再投影しない。
8. Review batch上限を超える候補を安定順でpage分割し、未提示page・cursor・件数を保持する。
   Programは関連度で並べ替えず、未提示候補をrejectしない。
9. Graph ReviewのSolver選択上限を3件とし、Cycleの残り本文取得枠を超えない。選択外の
   関連候補はdeferし、同じHypothesisではledgerから後続stepまたは次Cycleで選べるようにする。
   別Hypothesisへ結び直す場合だけSolverが`frontier_re_adoptions`を返す。
10. Graphで到達できないopen WorkItemから、Solverが`legal_search`で新しい深さ0起点を作れるようにする。
11. 7.3・7.4のWorkItem監査、Graph差分Review、Cycle予算、検索fallback語彙を各Legal Promptへ反映し、
   Profile version・Provider schema・契約テストを同時に更新する。
   directionの旧称を残さず、`MENTIONS`をPromptとschemaの候補から除外する。
12. `max_material_evidence_chars=50000`を適用しても、当該Review batchのArticle ID、見出し、起点、
    mode・predicateまたは原文relation、direction、basis・classificationRunId、depth、sourceKindと
    deferred ledgerが残ることをテストする。
    全履歴はCaseStoreで監査できる。
13. 1候補と必要Link、または新規取得した1 Articleの全文でもmodel context容量を超える場合は明示的エラーにする。

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
