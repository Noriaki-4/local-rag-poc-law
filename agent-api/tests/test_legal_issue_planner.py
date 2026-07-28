"""structured issue plannerとルール補正の単体テスト (計画書 §7.2-7.4, §16.2)。"""

from app.evidence_requirements import ORIGIN_PLANNER, ORIGIN_RULE
from app.legal_issue_planner import (
    HARD_MAX_PRIMARY_ISSUES,
    SOFT_MAX_PRIMARY_ISSUES,
    build_issue_plan_prompt,
    fallback_issue_plan,
    issue_plan_json_schema,
    merge_explicit_references,
    parse_issue_plan,
    role_families_from_text,
)


class TestRoleRules:
    def test_definition_cues(self) -> None:
        assert "meaning_scope" in role_families_from_text("公開買付けとは何を意味しますか")

    def test_requirement_cues(self) -> None:
        assert "qualification" in role_families_from_text("どのような場合に手続が必要ですか")

    def test_exception_cues(self) -> None:
        assert "qualification" in role_families_from_text("適用が除外される場合はありますか")

    def test_procedure_cues(self) -> None:
        assert "procedure" in role_families_from_text("公告はどのように行いますか")

    def test_consequence_cues(self) -> None:
        assert "consequence" in role_families_from_text("違反した場合の罰則は")

    def test_deadline_cues(self) -> None:
        families = role_families_from_text("いつまでに提出する必要がありますか")
        assert "procedure" in families

    def test_application_cues(self) -> None:
        assert "linkage" in role_families_from_text("第27条の規定を準用しますか")

    def test_no_cue_defaults_to_normative_rule(self) -> None:
        assert role_families_from_text("株券等の取扱い") == ("normative_rule",)


class TestParseIssuePlan:
    def test_parses_structured_plan(self) -> None:
        raw = """{
          "issues": [
            {
              "label": "公開買付けの適用要件",
              "questionSpan": "手続が必要になるのはどのような場合",
              "keyTerms": ["市場外買付け", "株券等所有割合"],
              "requestedRoleFamilies": ["normative_rule", "qualification"],
              "confidence": 0.9
            }
          ],
          "graphPotentiallyRequired": true
        }"""
        plan = parse_issue_plan(raw, question="公開買付けの手続が必要になるのはどのような場合ですか")
        assert plan.validation_error is None
        assert plan.graph_potentially_required is True
        assert len(plan.issues) == 1
        issue = plan.issues[0]
        assert issue.label == "公開買付けの適用要件"
        # LLMの役割にルール補正(「手続」→procedure)が加わる。競合ではなく仮説として残す。
        assert set(issue.requested_role_families) >= {"normative_rule", "qualification"}
        assert issue.source == ORIGIN_PLANNER

    def test_rule_families_are_added_as_hypotheses(self) -> None:
        """LLMとルールが競合しても両方を仮説として残す (§7.3)。"""
        raw = '{"issues": [{"label": "定義", "questionSpan": "公開買付けとは", "keyTerms": [], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.5}], "graphPotentiallyRequired": false}'
        plan = parse_issue_plan(raw, question="公開買付けとは何ですか")
        assert set(plan.issues[0].requested_role_families) >= {"normative_rule", "meaning_scope"}

    def test_unknown_role_families_are_dropped(self) -> None:
        raw = '{"issues": [{"label": "x", "questionSpan": "x", "keyTerms": [], "requestedRoleFamilies": ["delegated_detail"], "confidence": 0.5}], "graphPotentiallyRequired": false}'
        plan = parse_issue_plan(raw, question="株券等の取扱い")
        assert "delegated_detail" not in plan.issues[0].requested_role_families
        assert plan.issues[0].requested_role_families  # 空にはしない

    def test_planner_must_not_assert_article_numbers(self) -> None:
        """条番号はplannerではなく決定的パーサーの結果を正とする (§7.2)。"""
        raw = '{"issues": [{"label": "金商法第9999条", "questionSpan": "x", "keyTerms": ["第9999条"], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.9}], "graphPotentiallyRequired": false}'
        plan = parse_issue_plan(raw, question="公開買付けの要件")
        assert plan.issues[0].explicit_references == ()

    def test_invalid_json_falls_back(self) -> None:
        plan = parse_issue_plan("not json", question="公開買付けの手続はどのように行いますか")
        assert plan.validation_error is not None
        assert plan.fallback_used is True
        assert len(plan.issues) == 1
        assert plan.issues[0].source == ORIGIN_RULE
        assert "procedure" in plan.issues[0].requested_role_families

    def test_empty_issue_list_falls_back(self) -> None:
        plan = parse_issue_plan('{"issues": [], "graphPotentiallyRequired": false}', question="公開買付けとは")
        assert plan.fallback_used is True
        assert len(plan.issues) == 1

    def test_more_than_four_issues_are_kept(self) -> None:
        """論点数を4件に固定しない (§7.4)。"""
        issues = ", ".join(
            f'{{"label": "論点{index}", "questionSpan": "span{index}", "keyTerms": [], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.8}}'
            for index in range(6)
        )
        plan = parse_issue_plan(f'{{"issues": [{issues}], "graphPotentiallyRequired": false}}', question="q")
        assert len(plan.issues) == 6
        assert plan.overflow_issues == ()
        assert SOFT_MAX_PRIMARY_ISSUES == 6

    def test_hard_limit_moves_extra_issues_to_overflow_without_deleting(self) -> None:
        issues = ", ".join(
            f'{{"label": "論点{index}", "questionSpan": "span{index}", "keyTerms": [], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.8}}'
            for index in range(HARD_MAX_PRIMARY_ISSUES + 3)
        )
        plan = parse_issue_plan(f'{{"issues": [{issues}], "graphPotentiallyRequired": false}}', question="q")
        assert len(plan.issues) == HARD_MAX_PRIMARY_ISSUES
        assert len(plan.overflow_issues) == 3
        assert plan.out_of_scope_labels

    def test_issue_ids_are_stable(self) -> None:
        raw = '{"issues": [{"label": "a", "questionSpan": "a", "keyTerms": [], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.5}], "graphPotentiallyRequired": false}'
        first = parse_issue_plan(raw, question="q")
        second = parse_issue_plan(raw, question="q")
        assert [issue.issue_id for issue in first.issues] == [issue.issue_id for issue in second.issues]


class TestExplicitReferences:
    def test_references_are_attached_to_best_matching_issue(self) -> None:
        plan = parse_issue_plan(
            '{"issues": ['
            '{"label": "公開買付けの要件", "questionSpan": "公開買付けの要件", "keyTerms": ["公開買付け"], "requestedRoleFamilies": ["qualification"], "confidence": 0.9},'
            '{"label": "公告の手続", "questionSpan": "公告の手続", "keyTerms": ["公告"], "requestedRoleFamilies": ["procedure"], "confidence": 0.9}'
            '], "graphPotentiallyRequired": false}',
            question="公開買付けの要件と公告の手続",
        )
        merged = merge_explicit_references(
            plan,
            [
                {
                    "articleContentUnitId": "law-323AC0000000025-article-27_2",
                    "documentId": "law-323AC0000000025",
                    "matchedText": "公開買付け 第27条の2",
                }
            ],
        )
        holders = [issue for issue in merged.issues if issue.explicit_references]
        assert len(holders) == 1
        assert holders[0].label == "公開買付けの要件"

    def test_reference_without_match_goes_to_first_issue(self) -> None:
        plan = parse_issue_plan(
            '{"issues": [{"label": "x", "questionSpan": "x", "keyTerms": [], "requestedRoleFamilies": ["normative_rule"], "confidence": 0.5}], "graphPotentiallyRequired": false}',
            question="q",
        )
        merged = merge_explicit_references(
            plan, [{"articleContentUnitId": "law-a-article-1", "documentId": "law-a"}]
        )
        assert merged.issues[0].explicit_references == ("law-a-article-1",)


class TestFallbackAndPrompt:
    def test_fallback_uses_whole_question_as_one_issue(self) -> None:
        plan = fallback_issue_plan("公開買付けの適用除外はありますか", reason="planner_timeout")
        assert len(plan.issues) == 1
        assert plan.issues[0].source == ORIGIN_RULE
        assert plan.fallback_used is True
        assert plan.validation_error == "planner_timeout"

    def test_prompt_does_not_leak_gold(self) -> None:
        prompt = build_issue_plan_prompt("公開買付けの要件", choices={"a": "正しい", "b": "誤り"}, max_issues=8)
        assert "正解" not in prompt.split("選択肢:")[0] or "推測" in prompt
        assert "公開買付けの要件" in prompt

    def test_schema_limits_issue_count(self) -> None:
        schema = issue_plan_json_schema(max_issues=8)
        assert schema["properties"]["issues"]["maxItems"] == 8
        assert schema["properties"]["issues"]["items"]["properties"]["requestedRoleFamilies"]["items"]["enum"]
