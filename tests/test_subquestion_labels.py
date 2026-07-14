import unittest

from src.web.app import _group_labeled_subquestions, _review_guidance


class SubquestionLabelTests(unittest.TestCase):
    def group(self, labels):
        return _group_labeled_subquestions([
            {"label": label, "stem_markdown": f"内容{index}"}
            for index, label in enumerate(labels, 1)
        ])

    def test_nested_labels_form_two_main_questions_and_keep_flat_indexes(self):
        result = self.group(["（1）", "（2）（i）", "（2）（ii）"])

        self.assertFalse(result["needs_review"])
        self.assertEqual(["（1）", "（2）"], [group["main_label"] for group in result["groups"]])
        self.assertEqual([], result["groups"][0]["children"])
        self.assertEqual(["（i）", "（ii）"], [item["child_label"] for item in result["groups"][1]["children"]])
        self.assertEqual([0, 1, 2], [item["flat_index"] for item in result["flat_items"]])
        self.assertEqual(
            ["第（1）问", "第（2）问（i）", "第（2）问（ii）"],
            [item["title"] for item in result["flat_items"]],
        )

    def test_plain_three_questions_remain_three_main_questions(self):
        result = self.group(["（1）", "（2）", "（3）"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(["（1）", "（2）", "（3）"], [group["main_label"] for group in result["groups"]])

    def test_ascii_and_mixed_roman_labels_are_whitelisted(self):
        for labels in (["(1)", "(2)(i)", "(2)(ii)"], ["（1）", "（2）(i)", "（2）(ii)"]):
            with self.subTest(labels=labels):
                result = self.group(labels)
                self.assertFalse(result["needs_review"])
                self.assertEqual(2, len(result["groups"]))
                self.assertEqual(["(i)", "(ii)"], [item["child_label"] for item in result["groups"][1]["children"]])

    def test_circled_children_keep_parent_condition_and_original_labels(self):
        result = _group_labeled_subquestions([
            {"label": "（1）", "stem_markdown": "第一问"},
            {"label": "（2）", "stem_markdown": "公共条件"},
            {"label": "（2）①", "stem_markdown": "第一小问"},
            {"label": "（2）②", "stem_markdown": "第二小问"},
        ])
        self.assertFalse(result["needs_review"])
        self.assertEqual("公共条件", result["groups"][1]["parent"]["stem_markdown"])
        self.assertEqual(["①", "②"], [item["child_label"] for item in result["groups"][1]["children"]])
        self.assertEqual([1, 2, 3], [item["flat_index"] for item in [result["groups"][1]["parent"], *result["groups"][1]["children"]]])

    def test_nonstandard_missing_duplicate_and_disordered_labels_fall_back_without_rewriting(self):
        cases = [
            ["（1）", "（2）甲", "（2）乙"],
            ["（1）", None, "（3）"],
            ["（1）", "（1）"],
            ["（2）", "（1）"],
        ]
        for labels in cases:
            with self.subTest(labels=labels):
                result = self.group(labels)
                self.assertTrue(result["needs_review"])
                self.assertEqual([], result["groups"])
                expected = [label if label else f"第{index}项" for index, label in enumerate(labels, 1)]
                self.assertEqual(expected, [item["display_label"] for item in result["flat_items"]])

    def test_formal_database_style_stems_are_split_once_without_double_numbering(self):
        result = _group_labeled_subquestions([
            {"stem_markdown": "（1） 第一问"},
            {"stem_markdown": "（2）（i） 第二问"},
            {"stem_markdown": "（2）（ii） 第三问"},
        ])
        self.assertEqual(["第一问", "第二问", "第三问"], [item["stem_markdown"] for item in result["flat_items"]])
        self.assertEqual(["（1）", "（2）（i）", "（2）（ii）"], [item["original_label"] for item in result["flat_items"]])

    def test_unsafe_labels_add_review_priority(self):
        edited = {
            "question_type_code": "solution",
            "options": [],
            "subquestions": [{"label": "（1）", "stem_markdown": "甲"}, {"label": "（1）", "stem_markdown": "乙"}],
        }
        guidance = _review_guidance({}, edited, None, [], None)
        self.assertIn("请核对小问层级标签", guidance["priority_items"])


if __name__ == "__main__":
    unittest.main()
