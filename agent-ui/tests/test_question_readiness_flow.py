import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).parents[1] / "app.py"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _readiness_response(url, **kwargs):
    if not url.endswith("/question/readiness"):
        raise AssertionError(f"unexpected request: {url}")
    question = kwargs.get("json", {}).get("question", "")
    if "曖昧" in question:
        return _Response(
            {
                "decision": "clarification_required",
                "reason": "主体によって検索経路が分かれます。",
                "clarification_question": "誰が行いますか。",
                "choices": [
                    {
                        "choice_id": "company",
                        "label": "会社",
                        "refined_question": "会社が行う場合の要件は何ですか。",
                    },
                    {
                        "choice_id": "person",
                        "label": "個人",
                        "refined_question": "個人が行う場合の要件は何ですか。",
                    },
                ],
            }
        )
    return _Response(
        {
            "decision": "ready",
            "reason": "一般論として調査できます。",
            "clarification_question": None,
            "choices": [],
        }
    )


def _answer_response(url, **_kwargs):
    if not url.endswith("/answer"):
        raise AssertionError(f"unexpected request: {url}")
    return _Response(
        {
            "answer": "直接調査した回答です。",
            "citations": [],
            "graphPaths": [],
            "route": [],
            "trace": {},
        }
    )


def _button(app, label):
    return next(item for item in app.button if item.label == label)


class QuestionReadinessFlowTest(unittest.TestCase):
    @patch("requests.post", side_effect=_readiness_response)
    def test_ready_question_requires_confirmation_before_answer(self, _post):
        app = AppTest.from_file(str(APP_PATH)).run()
        app.text_area(key="question_text").set_value("一般的な要件は何ですか。")

        self.assertTrue(
            any(item.label == "質問を整理する" for item in app.button)
        )
        self.assertTrue(
            any(item.label == "このまま調べる" for item in app.button)
        )

        _button(app, "質問を整理する").click().run()

        self.assertFalse(app.exception)
        self.assertTrue(
            any(item.label == "この内容で調べる" for item in app.button)
        )
        self.assertTrue(
            any("そのまま法令調査" in item.value for item in app.success)
        )

    @patch("requests.post", side_effect=_answer_response)
    def test_direct_research_skips_question_readiness(self, post):
        app = AppTest.from_file(str(APP_PATH)).run()
        app.text_area(key="question_text").set_value("このまま調べる質問です。")

        _button(app, "このまま調べる").click().run()

        self.assertFalse(app.exception)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/answer"))
        self.assertTrue(any(item.value == "回答" for item in app.subheader))

    @patch("requests.post", side_effect=_readiness_response)
    def test_clarification_choice_updates_existing_question_field(self, _post):
        app = AppTest.from_file(str(APP_PATH)).run()
        app.text_area(key="question_text").set_value("曖昧な質問です。")
        _button(app, "質問を整理する").click().run()

        self.assertFalse(app.exception)
        self.assertEqual(app.radio[0].value, "company")
        self.assertTrue(
            any(
                item.label == "質問を直接修正してください"
                for item in app.text_area
            )
        )
        self.assertTrue(
            any("一つずつ選び" in item.value for item in app.caption)
        )

        _button(app, "この質問案を入力欄へ反映").click().run()

        self.assertEqual(
            app.text_area(key="question_text").value,
            "会社が行う場合の要件は何ですか。",
        )
        self.assertTrue(
            any(item.label == "この内容で調べる" for item in app.button)
        )


if __name__ == "__main__":
    unittest.main()
