<!-- prompt-section:contract_feedback_rule -->
contract_feedbackがある場合、直前Decisionは状態へ適用されていません。適合している意味判断は保ち、violationと矛盾する制御値・参照だけを修正してください。

<!-- prompt-section:base -->
直前Decisionは未適用です。適合する意味判断を保ち、次の違反だけを修正します。
${focused_instructions}
violation: ${violation}

<!-- prompt-section:unknown_evidence -->
Evidence IDを生成せず、正規契約のHypothesis.evidence_idsにはgrounding_evidence_idsの完全一致だけを選びます。Provider輸送上の対応欄はschemaの指示に従います。未取得本文ならHypothesisをunresolvedにします。

<!-- prompt-section:review_finding_resolution -->
reviewer_findingsの全finding_idをreview_finding_resolutionsへ1回ずつ返します。指摘を反映するならaddressed、提示済み本文に基づき採用しない場合だけdisputedとし、理由と使用した既知Evidence IDを示します。

<!-- prompt-section:hypothesis_requires_evidence -->
grounding_evidence_idsが空ならHypothesisをunresolved、evidence_ids=[]へ戻し、検索候補から必要なArticleをfetch_articlesで取得します。検索候補だけで完了しません。

<!-- prompt-section:navigation_only_evidence -->
search_navigationは根拠にせず、必要なArticle本文を取得するか未解決のままにします。

<!-- prompt-section:unknown_article_id -->
`fetch_articles`には`fetchable_article_ids`の完全一致だけを使います。IDが`grounding_evidence_ids`または
`material_evidence`にある場合は本文取得済みなので、再取得要求を削除して提示本文を評価します。
Paragraph・ItemのEvidence IDをArticle IDとして使いません。必要なArticleが未発見の場合だけ
Graphまたは異なる`legal_search`で発見します。`needs_action`のbasis Evidenceを再取得しません。

<!-- prompt-section:open_work_item -->
追加調査できるならopenのままcontinueします。不能時だけlimitationsと既知の未解決IDを対応させます。

<!-- prompt-section:work_item_hypothesis_alignment -->
WorkItemだけをresolvedにしません。提示本文がbasis Hypothesisを直接確認できるなら、同じDecisionで各Hypothesisを根拠付きsupportedまたはcontradictedへ更新します。確認できないbasis Hypothesisが一つでもあれば、WorkItemをopen、resolution=nullのままcontinueします。

<!-- prompt-section:cycle_boundary -->
現CycleへToolを追加せず、完了ならfinalize、未完了で次Cycle可能ならstart_next_cycle=trueにします。

<!-- prompt-section:resolved_dependency -->
同じArticleのParagraphを重ねず、委任元と末端Articleの本文Evidenceを使います。末端未取得ならneeds_actionにします。

<!-- prompt-section:dependency_decision -->
required_dependency_work_item_idsの各IDへ1件返し、各basis_evidence_idsへ判断に使ったgrounding Evidenceを1件以上指定します。needs_actionには未解決の委任を確認した本文Evidenceを使います。現Cycleを続ける場合は、対応するtool_requests[].request_idをaction_request_idへ同じ文字列のままコピーします。次Cycleへ引き継ぐ場合はnullにします。

<!-- prompt-section:retained_evidence_limit -->
retain_evidence_idsはmax_retained_evidence件以内で、後続Cycleにも本文が必要なEvidenceをLLMが選びます。

<!-- prompt-section:tool_request_limit -->
上限内で今回必要な要求をLLMが選び、超過分をProgramへ選別させません。

<!-- prompt-section:unique_tool_request_ids -->
今回返す各ToolRequestのrequest_idを同じDecision内で相互に異なる短い局所IDにします。意味判断とToolの種類・引数は変えません。request_idを変更したToolRequestを指すDependencyDecisionがあれば、action_request_idにも同じ局所IDをコピーします。

<!-- prompt-section:repeated_successful_search -->
同じWorkItem・Hypothesis・query・filterで成功済みのlegal_searchを再要求しません。search_candidatesと対応する検索抜粋を評価し、関係する候補があればfetch_articlesで本文を取得します。既存候補では検証できないと判断した場合だけ、不足する確認事項をdecision_reasonに示して検索表現を変更します。

<!-- prompt-section:article_fetch_contract -->
本文取得は1 Requestに統合し、既知Articleをremaining_fetch_capacity以内で選びます。

<!-- prompt-section:known_references -->
WorkItem・Hypothesis・Requestは既知IDへ完全一致させます。必要なToolなら対応WorkItemをstate=open、resolution=nullのまま保ちます。WorkItemが本当に完了したなら、そのWorkItemをnext_focus_work_item_idsとToolRequestから外します。どちらかは意味判断に基づいてSolverが選びます。

<!-- prompt-section:graph_review -->
表示されたGraph batch・ledgerの既知IDだけを使い、候補の必要性はSolverが判断します。

<!-- prompt-section:citation_coverage -->
回答で使うHypothesis Evidenceをcitation_idsへ含め、使わないEvidenceはHypothesisから外します。resolved DependencyDecisionのbasisに選んだ各Articleから少なくとも1つのEvidenceを引用します。直前Decisionは未適用なので、finalizeを維持するならWorkItem・Hypothesisの完了更新も正規契約のupdateへ再掲します。
