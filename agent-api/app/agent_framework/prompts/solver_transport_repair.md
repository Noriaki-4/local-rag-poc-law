<!-- prompt-section:base -->
${base_prompt}

直前の出力は輸送またはschema検証だけに失敗しました。意味上の判断を変えず、契約に適合するSolverDecisionへ修復してください。
${focused_instruction}
<validation_error>${validation_error}</validation_error>
<previous_solver_decision>${previous_solver_decision}</previous_solver_decision>

<!-- prompt-section:continue_requires_action -->
next=continueを維持するなら、追加調査に必要なlegal_search、legal_graph_neighbors、load_evidenceまたはarticle_fetchを少なくとも1件返します。調査が不要と判断するなら、未完了WorkItemを根拠に基づいて閉じ、next=finalizeとanswerを返します。どちらかを意味判断して選びます。

<!-- prompt-section:article_fetch_limit -->
本文取得は専用article_fetchだけに1件返し、article_ref_Nはエラーに示された現在の残り件数以内にします。汎用tool_requestsへarticle_fetchまたはfetch_articlesを入れません。どの候補を今回取得するかは自分で選びます。

<!-- prompt-section:hypothesis_requires_evidence -->
本文Evidenceを選んでいないHypothesisはjudgment=unresolved、evidence_ids=[]のままにします。search_navigationだけでsupportedまたはcontradictedにせず、必要な既知Articleはarticle_fetchで取得します。
