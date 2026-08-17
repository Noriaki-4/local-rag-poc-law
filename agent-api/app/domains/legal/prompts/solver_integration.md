次の順序を変えずに1回のSolverDecisionを作成してください。

1. **取得結果の評価**
   - 直前の全ToolResultとmaterial_evidenceを読み、対応するWorkItemとHypothesisを更新します。
   - search_navigationの本文抜粋は次のTool選択にだけ使い、judgmentやresolutionの根拠にしません。
   - 取得本文に付随する1ホップGraph候補も確認します。検索候補やGraph候補だけを根拠にせず、質問に関係し得るArticle本文が未取得なら取得対象にします。

2. **下位規範監査**
   - 元の質問が求める観点と、取得本文中の「政令で定める」「内閣府令で定める」「省令で定める」等を照合します。
   - 範囲・要件・例外・手続を委任先が具体化する場合、その本文を確認するまで対応するHypothesisをunresolved、WorkItemをopenのままにします。
   - 目的条項・総則条項・無関係な条項を委任先本文の代用にしません。

3. **追加調査の編成**
   - finalize_only=falseで回答に影響する未確認事項があり、残りサイクルで実行可能ならcontinueします。
   - 既知Article本文にはfetch_articles、Article IDが不明ならlegal_searchを使います。検索本文に参照先の条番号があっても、そのArticle IDがfetchable_article_idsにない場合はfetch_articlesではなくlegal_searchを使います。
   - 複数のlegal_search結果から最初の本文を選ぶときは、候補を各open WorkItemへ対応付けます。同じArticleが複数観点を直接扱う場合を除き、利用者が明示した観点の一部だけへ取得枠を偏らせず、各観点を直接定めるArticleを優先します。見出しや近接性だけで、発動要件のArticleを公告・届出のArticleへ置き換えません。
   - Graph候補として発見されていない起点Articleのfetch_articlesにだけ1ホップGraph取得が自動で伴います。Graph候補Articleの本文は取得しますが、そこからGraphを再展開しません。
   - 新規・再採用・新Link差分のGraph候補は専用モードで評価されます。通常Integrationでは取得済み本文とgraph_review_ledgerを評価し、relevant_deferredの既知Articleまたはselectedかつfailed/timeoutの再試行を、Cycleのremaining_fetch_capacity内で本文取得できます。
   - Cycle境界では、本文未取得のactiveなrelevant_deferredを1件も黙って捨てず、全件へdeferred_frontier_resolutionsを返します。次Cycle最初に取得する候補はfetch_next_cycleにしてstart_next_cycle=trueとします。そのArticleはProgramが1つのfetch_articlesへ機械転記するため、同じToolRequestを重ねて返しません。取得上限等で後続へ残す候補はcarry_forwardにします。この選択に作業分解や仮説の大幅変更は不要です。不要と判断した候補はno_longer_needed、次Cycleを開始できない上限時の未確認候補はunresolved_at_limitにします。
   - graph_review_batch.candidates=[]かつremaining_unreviewed_count>0なら、詳細未提示のGraph候補が保持されています。Cycle境界ではunreviewed_graph_resolutionを必ず返します。意味評価が回答に必要ならreview_next_cycleとToolRequestなしのstart_next_cycle=trueで引き継ぎ、不要と判断してfinalizeする場合だけno_longer_needed、次Cycle不能の限定回答だけunresolved_at_limitにします。
   - ledgerの評価済みArticleを別Hypothesisへ使う場合はfrontier_re_adoptionsへArticle、open WorkItem、所属Hypothesis、理由を明示します。Programへrejected候補の自動転用を要求しません。
   - 同じDecisionの既知ArticleはWorkItemごとに分けず、4個以内なら1つのfetch_articlesへ統合します。4個は上限であり、空きを埋めるために関係の薄いArticleを追加しません。候補確認だけで最後の探索可能サイクルを消費しません。

4. **終了整合監査**
   - finalizeを選ぶ直前に、WorkItem、Hypothesis、answer、limitationsを相互照合します。
   - 適用要件、数値基準、例外、義務・手続のうち回答する観点が、それぞれ独立に検証されているか確認します。
   - answerとresolutionの各法的主張に直接対応するgrounding Evidenceがなければ確認済みにしません。
   - resolved WorkItemのbasis Hypothesisが宣言したEvidenceを、最終回答のcitation_idsから落としません。
   - 回答、resolution、gaps、limitationsに未確認の別法令が回答へ影響すると書く場合、finalize_only=falseならそのWorkItemをopenのままcontinueします。
   - limitationsは未確認事項専用です。通常のfinalizeではlimitationsとunresolved ID欄を空にします。次Cycle不能の限定回答では、未完了WorkItemとHypothesisをopen/unresolvedのまま保ち、その既知IDをanswer.unresolved_work_item_idsとunresolved_hypothesis_idsへ指定します。
   - cycle_budget_reachedまたはcycle_close_requiredがtrueなら現CycleへToolを追加しません。現CycleのWorkItem・Hypothesis・Evidence・Graph ledgerを評価し、完了ならfinalize、方針変更または次の取得枠が必要なら次に検証する命題と方針を明示してstart_next_cycle=trueにします。start_next_cycleと同じDecisionのToolRequestは次Cycleの最初の行動です。

5. **終了または回答**
   - すべての明示的観点と質問に関係する委任連鎖を確認できた場合だけ早期finalizeします。
   - finalize_only=trueまたは取得不能の場合は、確認済み事項と未確認事項を区別した限定付き回答にします。
   - 回答では観点を分け、取得本文が示す条文番号と条件だけを正確に説明します。
