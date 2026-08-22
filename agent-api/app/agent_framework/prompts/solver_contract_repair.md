<!-- prompt-section:contract_feedback_rule -->
contract_feedbackがある場合、直前Decisionは状態へ適用されていません。適合している意味判断は保ち、violationと矛盾する制御値・参照だけを修正してください。

<!-- prompt-section:base -->
直前Decisionは未適用です。適合する意味判断を保ち、次の違反だけを修正します。
${focused_instructions}
violation: ${violation}

<!-- prompt-section:unknown_evidence -->
Evidence IDを生成せず、grounding_evidence_idsの完全一致だけをhypothesis_evidence_bindingsで選びます。未取得本文ならHypothesisをunresolvedにします。

<!-- prompt-section:hypothesis_requires_evidence -->
grounding_evidence_idsが空ならHypothesisをunresolved、evidence_ids=[]へ戻し、検索候補から必要なArticleをarticle_fetchで取得します。検索候補だけで完了しません。

<!-- prompt-section:navigation_only_evidence -->
search_navigationは根拠にせず、必要なArticle本文を取得するか未解決のままにします。

<!-- prompt-section:unknown_article_id -->
fetchable_article_idsの完全一致だけを使い、未知ならlegal_searchで発見し直します。

<!-- prompt-section:open_work_item -->
追加調査できるならopenのままcontinueします。不能時だけlimitationsと既知の未解決IDを対応させます。

<!-- prompt-section:cycle_boundary -->
現CycleへToolを追加せず、完了ならfinalize、未完了で次Cycle可能ならstart_next_cycle=trueにします。

<!-- prompt-section:resolved_dependency -->
同じArticleのParagraphを重ねず、委任元と末端Articleの本文Evidenceを使います。末端未取得ならneeds_actionにします。

<!-- prompt-section:dependency_decision -->
required_dependency_work_item_idsの各IDへ1件返します。needs_actionのaction_request_idは、同じDecisionで実際に返すToolRequestまたはarticle_fetchのrequest_idと完全一致させます。

<!-- prompt-section:retained_evidence_limit -->
retain_evidence_idsはmax_retained_evidence件以内で、後続Cycleにも本文が必要なEvidenceをLLMが選びます。

<!-- prompt-section:tool_request_limit -->
上限内で今回必要な要求をLLMが選び、超過分をProgramへ選別させません。

<!-- prompt-section:article_fetch_contract -->
本文取得は1 Requestに統合し、既知Articleをremaining_fetch_capacity以内で選びます。

<!-- prompt-section:known_references -->
WorkItem・Hypothesis・Requestは既知IDへ完全一致させます。必要なToolなら対応WorkItemをstate=open、resolution=nullのまま保ちます。WorkItemが本当に完了したなら、そのWorkItemをnext_focus_work_item_idsとToolRequestから外します。どちらかは意味判断に基づいてSolverが選びます。

<!-- prompt-section:graph_review -->
表示されたGraph batch・ledgerの既知IDだけを使い、候補の必要性はSolverが判断します。

<!-- prompt-section:citation_coverage -->
回答で使うHypothesis Evidenceをcitation_idsへ含め、使わないEvidenceはHypothesisから外します。resolved DependencyDecisionのbasisに選んだ各Articleから少なくとも1つのEvidenceを引用します。直前Decisionは未適用なので、finalizeを維持するならWorkItem・Hypothesisの完了更新もupdate_jsonへ再掲します。
