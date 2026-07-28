import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_map import build_evidence_dot, citation_label, group_citations, humanize_citation_ids


class EvidenceMapTest(unittest.TestCase):
    def test_groups_multiple_articles_by_law(self):
        citations = [
            {
                "documentId": "law-civil",
                "title": "民法",
                "heading": "第604条",
            },
            {
                "documentId": "law-civil",
                "title": "民法",
                "heading": "第622条の2",
            },
            {
                "documentId": "law-land-lease",
                "title": "借地借家法",
                "heading": "第3条",
            },
        ]

        groups = group_citations(citations)

        self.assertEqual([group["title"] for group in groups], ["民法", "借地借家法"])
        self.assertEqual(groups[0]["citationCount"], 2)

    def test_marks_title_guessed_relations_as_inferred(self):
        """タイトルの文字列から推測した関係は、グラフで確認した関係と線種を分ける。

        条文同士に実際の委任関係があることまでは確認していないため。
        """
        citations = [
            {"documentId": "law-fiea", "title": "金融商品取引法"},
            {"documentId": "law-order", "title": "金融商品取引法施行令"},
        ]

        dot = build_evidence_dot("届出の要否", citations)

        assert_line = [line for line in dot.splitlines() if "施行内容を具体化" in line]
        self.assertTrue(assert_line)
        self.assertIn('style="dotted"', assert_line[0])
        self.assertIn("名称から推定", assert_line[0])

    def test_keeps_graph_confirmed_relations_solid(self):
        citations = [
            {"documentId": "law-fiea", "title": "金融商品取引法"},
            {"documentId": "law-order", "title": "金融商品取引法施行令"},
        ]
        graph_paths = [
            {
                "nodes": [{"documentId": "law-fiea"}, {"documentId": "law-order"}],
                "edges": [{"edgeType": "REFERENCES"}],
            }
        ]

        dot = build_evidence_dot("届出の要否", citations, graph_paths)

        confirmed = [line for line in dot.splitlines() if "条文から参照" in line]
        self.assertTrue(confirmed)
        self.assertNotIn('style="dotted"', confirmed[0])
        self.assertNotIn('style="dashed"', confirmed[0])

    def test_shows_formal_and_co_cited_relations_separately(self):
        citations = [
            {"documentId": "law-fiea", "title": "金融商品取引法"},
            {"documentId": "law-order", "title": "金融商品取引法施行令"},
            {"documentId": "law-disclosure", "title": "企業内容等の開示に関する内閣府令"},
        ]

        dot = build_evidence_dot("提出要件を横断して確認したい", citations)

        self.assertIn("施行内容を具体化", dot)
        self.assertIn("この回答で併せて参照", dot)
        self.assertIn('style="dashed"', dot)

    def test_returns_none_without_citations(self):
        self.assertIsNone(build_evidence_dot("質問", []))


class CitationLabelTest(unittest.TestCase):
    def test_does_not_repeat_title_already_contained_in_heading(self):
        citation = {
            "title": "原状回復をめぐるトラブルとガイドライン（再改訂版）",
            "heading": "原状回復をめぐるトラブルとガイドライン（再改訂版） p.138",
        }

        self.assertEqual(
            citation_label(citation),
            "原状回復をめぐるトラブルとガイドライン（再改訂版） p.138",
        )

    def test_joins_title_and_heading_for_articles(self):
        citation = {"title": "民法", "heading": "第六百二十一条 （賃借人の原状回復義務）"}

        self.assertEqual(citation_label(citation), "民法 第六百二十一条 （賃借人の原状回復義務）")

    def test_falls_back_to_content_unit_id(self):
        self.assertEqual(citation_label({"contentUnitId": "law-x-article-1"}), "law-x-article-1")


class HumanizeCitationIdsTest(unittest.TestCase):
    def test_replaces_ids_with_readable_labels(self):
        answer = "民法第621条(law-129-article-621)は通常損耗を除外しています。"
        citations = [
            {
                "contentUnitId": "law-129-article-621",
                "title": "民法",
                "heading": "第六百二十一条 （賃借人の原状回復義務）",
            }
        ]

        self.assertEqual(
            humanize_citation_ids(answer, citations),
            "民法第621条（民法 第六百二十一条 （賃借人の原状回復義務））は通常損耗を除外しています。",
        )

    def test_keeps_unknown_ids_untouched(self):
        answer = "根拠は law-unknown-article-1 です。"

        self.assertEqual(humanize_citation_ids(answer, []), answer)

    def test_replaces_longest_id_first(self):
        answer = "(law-a-article-1-paragraph-2)"
        citations = [
            {"contentUnitId": "law-a-article-1", "title": "法", "heading": "第1条"},
            {"contentUnitId": "law-a-article-1-paragraph-2", "title": "法", "heading": "第1条 第2項"},
        ]

        self.assertEqual(humanize_citation_ids(answer, citations), "（法 第1条 第2項）")


if __name__ == "__main__":
    unittest.main()
