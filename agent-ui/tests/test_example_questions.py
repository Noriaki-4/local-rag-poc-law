import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from example_questions import EXAMPLE_QUESTIONS, LEVELS, examples_by_level


class ExampleQuestionsTest(unittest.TestCase):
    def test_levels_are_numbered_from_one_without_gaps(self):
        self.assertEqual([level.level for level in LEVELS], [1, 2, 3, 4])

    def test_every_example_belongs_to_a_defined_level(self):
        defined = {level.level for level in LEVELS}
        self.assertTrue({example.level for example in EXAMPLE_QUESTIONS} <= defined)

    def test_every_level_has_at_least_two_examples(self):
        for level, examples in examples_by_level():
            with self.subTest(level=level.level):
                self.assertGreaterEqual(len(examples), 2)

    def test_examples_are_grouped_in_level_order(self):
        self.assertEqual([level.level for level, _ in examples_by_level()], [1, 2, 3, 4])

    def test_titles_are_unique(self):
        titles = [example.title for example in EXAMPLE_QUESTIONS]
        self.assertEqual(len(titles), len(set(titles)))

    def test_every_example_crosses_at_least_two_sources(self):
        for example in EXAMPLE_QUESTIONS:
            with self.subTest(title=example.title):
                self.assertGreaterEqual(len(example.expected.split("＋")), 2)

    def test_every_example_defines_required_evidence_and_answer_points(self):
        for example in EXAMPLE_QUESTIONS:
            with self.subTest(title=example.title):
                self.assertGreaterEqual(len(example.required_evidence), 2)
                self.assertGreaterEqual(len(example.answer_points), 2)
                self.assertRegex(example.legal_as_of, r"^\d{4}-\d{2}-\d{2}$")

    def test_questions_do_not_name_the_documents_to_look_up(self):
        """質問文に参照先の法令名・資料名を書くと、検索できるかを試せなくなる。

        「想定する参照先」は答え合わせ用の情報であり、質問文には含めない。
        """
        forbidden = (
            "民法",
            "借地借家法",
            "薬機法",
            "医薬品医療機器等法",
            "金融商品取引法",
            "金商法",
            "施行令",
            "施行規則",
            "府令",
            "監督指針",
            "ガイドライン",
            "適正広告基準",
            "横断して",
        )
        for example in EXAMPLE_QUESTIONS:
            for keyword in forbidden:
                with self.subTest(title=example.title, keyword=keyword):
                    self.assertNotIn(keyword, example.question)

    def test_guidance_level_examples_reference_external_guidance(self):
        guidance_examples = [example for example in EXAMPLE_QUESTIONS if example.level == 4]
        for example in guidance_examples:
            with self.subTest(title=example.title):
                self.assertTrue(
                    any(
                        keyword in example.expected
                        for keyword in ("ガイドライン", "監督指針", "基準", "Q&A")
                    )
                )



class ExpectedSourceMatchTest(unittest.TestCase):
    """例題については、引用が想定参照先へ到達したかを照合して見せる。

    引用の件数や法令の種類数は関連性の証明にならないため、
    「複数法令を横断できた＝成功」と表示しないための土台。
    """

    def test_matches_the_example_by_its_question_text(self):
        from example_questions import find_example

        example = EXAMPLE_QUESTIONS[0]
        self.assertEqual(find_example(f"  {example.question}  "), example)
        self.assertIsNone(find_example("投入範囲外の質問です。"))

    def test_reports_reached_and_missing_expected_sources(self):
        from example_questions import expected_source_status

        status = expected_source_status(
            "民法＋原状回復ガイドライン",
            ["law-129AC0000000089", "guidance-mlit-restoration"],
        )

        self.assertEqual([s.name for s in status], ["民法", "原状回復ガイドライン"])
        self.assertTrue(all(s.reached for s in status))

    def test_flags_a_source_that_was_not_cited(self):
        from example_questions import expected_source_status

        status = expected_source_status(
            "金融商品取引法＋施行令＋定義府令",
            ["law-323AC0000000025", "law-340CO0000000321"],
        )

        self.assertEqual([s.reached for s in status], [True, True, False])

    def test_a_subordinate_regulation_alone_does_not_satisfy_its_parent_law(self):
        """施行規則のタイトルは法律名を含むため、部分一致だと親法まで到達扱いになる。"""
        from example_questions import expected_source_status

        status = expected_source_status("薬機法＋薬機法施行規則", ["law-336M50000100001"])

        self.assertEqual([s.reached for s in status], [False, True])

    def test_a_guidance_pdf_does_not_satisfy_an_expected_ordinance(self):
        """「株券等の公開買付けに関するQ&A」は公開買付府令ではない。"""
        from example_questions import expected_source_status

        status = expected_source_status("公開買付府令", ["guidance-fsa-tob-disclosure"])

        self.assertEqual([s.reached for s in status], [False])

    def test_every_expected_source_name_is_mapped_to_a_document_id(self):
        """expectedに書いた名前の綴り違いを、判定不能ではなく失敗として検出する。"""
        from example_questions import EXPECTED_SOURCE_DOCUMENT_IDS

        for example in EXAMPLE_QUESTIONS:
            for token in example.expected.split("＋"):
                with self.subTest(title=example.title, token=token):
                    self.assertIn(token.strip(), EXPECTED_SOURCE_DOCUMENT_IDS)


class ExampleEvaluationTest(unittest.TestCase):
    def test_an_unrelated_article_from_the_right_law_does_not_pass(self):
        """民法619条だけでは、原状回復の621条・敷金の622条の2を満たさない。"""
        from example_questions import evaluate_example

        example = next(
            item for item in EXAMPLE_QUESTIONS if item.title == "賃貸住宅を退去するとき"
        )
        citations = [
            {
                "documentId": "law-403AC0000000090",
                "contentUnitId": "law-403AC0000000090-article-28",
            },
            {
                "documentId": "law-129AC0000000089",
                "contentUnitId": "law-129AC0000000089-article-619-paragraph-1",
            },
        ]

        evaluation = evaluate_example(
            example,
            citations,
            "大家には正当事由が必要です。通常損耗は借主負担外で、敷金は返還します。",
        )

        self.assertTrue(all(status.reached for status in evaluation.source_statuses))
        self.assertEqual(
            [status.reached for status in evaluation.evidence_statuses],
            [True, False, False],
        )
        self.assertFalse(evaluation.passed)

    def test_article_prefix_matching_stops_at_the_id_boundary(self):
        """借地借家法3条の期待値が30条を誤って拾わない。"""
        from example_questions import evaluate_example

        example = next(
            item for item in EXAMPLE_QUESTIONS if item.title == "土地を借りる期間の違い"
        )
        citations = [
            {
                "documentId": "law-403AC0000000090",
                "contentUnitId": "law-403AC0000000090-article-30",
            },
            {
                "documentId": "law-129AC0000000089",
                "contentUnitId": "law-129AC0000000089-article-604-paragraph-1",
            },
        ]

        evaluation = evaluate_example(
            example,
            citations,
            "建物所有目的では30年、それ以外は最長50年です。",
        )

        self.assertEqual(
            [status.reached for status in evaluation.evidence_statuses],
            [False, True],
        )

    def test_investment_advice_article_does_not_satisfy_suitability(self):
        """投資助言業務の41条を、一般勧誘の適合性原則40条として扱わない。"""
        from example_questions import evaluate_example

        example = next(
            item for item in EXAMPLE_QUESTIONS if item.title == "高齢の顧客へのリスク商品の勧誘"
        )
        citations = [
            {
                "documentId": "law-323AC0000000025",
                "contentUnitId": "law-323AC0000000025-article-41-paragraph-1",
            },
            {
                "documentId": "guidance-fsa-financial-instruments-business",
                "contentUnitId": "guidance-fsa-financial-instruments-business-page-140-chunk-317",
            },
        ]

        evaluation = evaluate_example(
            example,
            citations,
            "高齢顧客には慎重な勧誘と適合性の確認、社内規則とモニタリングが必要です。",
        )

        self.assertEqual(
            [status.reached for status in evaluation.evidence_statuses],
            [False, True],
        )
        self.assertFalse(evaluation.passed)

    def test_answer_point_allows_variants_but_requires_each_concept(self):
        from example_questions import evaluate_example

        example = next(
            item for item in EXAMPLE_QUESTIONS if item.title == "借地上の建物を売るとき"
        )
        citations = [
            {
                "documentId": "law-129AC0000000089",
                "contentUnitId": "law-129AC0000000089-article-612-paragraph-1",
            },
            {
                "documentId": "law-403AC0000000090",
                "contentUnitId": "law-403AC0000000090-article-19-paragraph-1",
            },
        ]

        incomplete = evaluate_example(example, citations, "地主の承諾が必要です。")
        complete = evaluate_example(
            example,
            citations,
            "地主の承諾が必要ですが、裁判所の許可で代えることができます。",
        )

        self.assertFalse(incomplete.passed)
        self.assertTrue(complete.passed)

    def test_answer_point_normalizes_month_counter_variants(self):
        from example_questions import evaluate_example

        example = next(
            item
            for item in EXAMPLE_QUESTIONS
            if item.title == "少人数への株式の勧誘（少人数私募）"
        )
        answer = (
            "50名未満で、過去3ヶ月の勧誘人数と合算します。"
            "転売制限を設け、未届出であることを告知して書面を交付します。"
        )

        evaluation = evaluate_example(example, [], answer)

        assert next(
            item
            for item in evaluation.answer_point_statuses
            if item.name == "過去3か月の勧誘人数との合算"
        ).reached

    def test_answer_point_accepts_plain_language_for_normal_wear(self):
        from example_questions import evaluate_example

        example = next(
            item for item in EXAMPLE_QUESTIONS if item.title == "賃貸住宅を退去するとき"
        )
        answer = (
            "大家には正当事由が必要です。通常の使用による損耗や経年劣化は"
            "借主負担にならず、敷金は残額を返還します。"
        )

        evaluation = evaluate_example(example, [], answer)

        assert next(
            item
            for item in evaluation.answer_point_statuses
            if item.name == "通常損耗・経年変化は原則として借主負担外"
        ).reached

    def test_scoring_metadata_is_not_sent_to_the_agent_api(self):
        """gold条文・要点をリクエストへ混ぜると、例題への過学習を招く。"""
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from scripts.check_example_questions import build_request_payload

        example = EXAMPLE_QUESTIONS[0]
        payload = build_request_payload(example)

        self.assertEqual(
            set(payload),
            {"question", "pattern", "topK", "userClearanceLevel"},
        )
        self.assertEqual(payload["question"], example.question)
        self.assertNotIn("expected", payload)
        self.assertNotIn("required_evidence", payload)
        self.assertNotIn("answer_points", payload)


if __name__ == "__main__":
    unittest.main()
