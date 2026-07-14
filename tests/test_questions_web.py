import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.importing.admit_questions import admit_questions
from src.web.app import _required_question_content, create_app
from tests.fixture_factory import create_import_job_fixture


class QuestionsPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.knowledge_values = []
        self._in_knowledge = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "select" and attributes.get("name") == "knowledge":
            self._in_knowledge = True
        elif self._in_knowledge and tag == "option" and attributes.get("value"):
            self.knowledge_values.append(attributes["value"])

    def handle_endtag(self, tag):
        if tag == "select" and self._in_knowledge:
            self._in_knowledge = False


class QuestionsWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.private = root / "private"
        create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            source = con.execute(
                """INSERT INTO source_papers
                (sha256,file_size,original_filename,stored_path,region_code,exam_year,exam_type_code,paper_name)
                VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf','TJ',2025,'YK','合成测试学校')""", ("b"*64,)
            ).lastrowid
            con.execute("INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) VALUES(1,?,1,4,'needs_review')", (source,))
        admit_questions(self.db, self.private, 1)
        self.client = TestClient(create_app(self.db, self.private))

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def card(self, page, number):
        marker = f'<span class="tag tag-number">原题号 {number}</span>'
        marker_at = page.find(marker)
        self.assertGreaterEqual(marker_at, 0, number)
        start = page.rfind('<div class="question-list-item">', 0, marker_at)
        end = page.find("</article>", marker_at)
        self.assertGreaterEqual(start, 0, number)
        self.assertGreaterEqual(end, 0, number)
        return page[start:end + len("</article>")]

    def test_home_and_navigation_link_to_real_22_question_library(self):
        response = self.client.get("/")
        self.assertIn('href="/questions"', response.text)
        self.assertIn("22", response.text)
        listing = self.client.get("/questions")
        self.assertEqual(200, listing.status_code)
        self.assertEqual(22, listing.text.count('class="question-list-item"'))
        self.assertNotIn("原题号 12", listing.text)

    def test_filters_and_invalid_parameters_are_safe(self):
        self.assertGreater(self.client.get("/questions?question_type=single_choice").text.count('class="question-list-item"'), 0)
        self.assertGreater(self.client.get("/questions?knowledge=01.01.06").text.count('class="question-list-item"'), 0)
        self.assertEqual(2, self.client.get("/questions?has_figure=true").text.count('class="question-list-item"'))
        self.assertEqual(0, self.client.get("/questions?has_figure=false&source=不存在").text.count('class="question-list-item"'))
        for query in ("question_type=bogus", "knowledge=bogus", "has_figure=maybe", "source=%00"):
            self.assertEqual(400, self.client.get("/questions?" + query).status_code)

    def test_required_image_filter_includes_3_and_16_and_false_excludes_both(self):
        with_images = self.client.get("/questions?has_figure=true").text
        without_images = self.client.get("/questions?has_figure=false").text
        self.assertEqual({"3", "16"}, set(re.findall(r"原题号 (\d+)</span>", with_images)))
        self.assertNotIn("原题号 3</span>", without_images)
        self.assertNotIn("原题号 16</span>", without_images)
        self.assertIn("含必要图片", with_images)
        self.assertIn("无必要图片", without_images)
        self.assertNotIn("无独立配图", without_images)

    def test_listing_uses_compact_metadata_and_separate_detail_link(self):
        page = self.client.get("/questions").text
        self.assertIn('class="question-list-item"', page)
        self.assertIn('href="/questions/', page)
        self.assertNotIn('<a class="question-list-item"', page)
        self.assertIn('class="question-card-tags"', page)
        self.assertIn('class="question-stem"', page)
        self.assertIn('class="question-secondary"', page)
        self.assertNotIn("<h2>原题号", page)

    def test_listing_shows_complete_text_options_with_math(self):
        page = self.client.get("/questions?question_type=single_choice").text
        self.assertIn('class="question-list-options"', page)
        for code, content in zip("ABCD", ("$S_1$", "$S_2$", "$S_3$", "$S_4$")):
            self.assertIn(f'<span class="option-label">{code}</span>', page)
            self.assertIn(content, page)

    def test_listing_shows_all_subquestions_for_solution_questions(self):
        page = self.client.get("/questions?question_type=solution").text
        q22 = self.card(page, 22)
        q23 = self.card(page, 23)
        self.assertIn('class="subquestions"', q22)
        self.assertIn('class="subquestions"', q23)
        for text in ("曲线 $y=f(x)$", "求 $a$ 的取值范围", "证明两个合成零点之积大于 $1$"):
            self.assertIn(text, q22)
        for text in ("讨论 $f(x)$ 的单调性", "求参数 $m$ 的取值范围", "比较两个合成式"):
            self.assertIn(text, q23)
        self.assertLess(q22.index('class="question-stem"'), q22.index('class="subquestions"'))
        self.assertLess(q22.index('class="subquestions"'), q22.index('class="question-secondary"'))

    def test_listing_image_option_uses_one_complete_question_without_placeholder_or_figure(self):
        with sqlite3.connect(self.db) as con:
            question_id = con.execute(
                "SELECT id FROM questions WHERE source_question_no='3'"
            ).fetchone()[0]
            con.executemany(
                "INSERT INTO question_options(question_id,option_code,content_markdown,display_order) "
                "VALUES(?,?,'见原页选项图',?)",
                [(question_id, code, order) for order, code in enumerate("ABCD", 1)],
            )
        page = self.client.get("/questions").text
        card = self.card(page, 3)
        self.assertEqual(1, card.count('class="question-required-image"'))
        self.assertIn("question_crops/Q003.png", card)
        self.assertNotIn("question_003_figure_01.png", card)
        self.assertNotIn("见原页选项图", card)
        self.assertNotIn("点击查看详情与配图", card)

    def test_listing_real_required_image_semantics_for_questions_1_3_and_16(self):
        page = self.client.get("/questions").text
        q1, q3, q16 = (self.card(page, number) for number in (1, 3, 16))
        self.assertIn(">无图</span>", q1)
        self.assertNotIn("<img", q1)
        self.assertNotIn("question_crops/Q001.png", q1)
        self.assertIn(">含图</span>", q3)
        self.assertIn("question_crops/Q003.png", q3)
        self.assertNotIn("question_003_figure_01.png", q3)
        self.assertIn(">含图</span>", q16)
        self.assertIn("question_016_figure_01.png", q16)
        self.assertNotIn("question_crops/Q016.png", q16)

    def test_required_images_are_after_content_before_source_and_accessible_responsive(self):
        page = self.client.get("/questions").text
        for number in (3, 16):
            card = self.card(page, number)
            self.assertLess(card.index('class="question-stem"'), card.index('class="question-required-images"'))
            self.assertLess(card.index('class="question-required-images"'), card.index('class="question-secondary"'))
            self.assertIn('loading="lazy"', card)
            self.assertRegex(card, rf'alt="第 {number} 题.+"')
            self.assertIn('href="/question-assets/', card)
        css = self.client.get("/static/questions.css").text
        self.assertIn(".question-required-image", css)
        self.assertIn("max-height", css)
        self.assertIn("object-fit:contain", css)

    def test_required_image_manifest_failure_is_friendly_and_never_renders_broken_img(self):
        manifest = self.private / "processing/import_job_1/question_crops.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["questions"][2]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(data), encoding="utf-8")
        response = self.client.get("/questions")
        self.assertEqual(200, response.status_code)
        card = self.card(response.text, 3)
        self.assertIn("题目配图暂不可用，请进入详情核对", card)
        self.assertNotIn("<img", card)

    def test_malformed_manifest_structure_does_not_make_listing_500(self):
        manifest = self.private / "processing/import_job_1/figure_assets.json"
        manifest.write_text("[]", encoding="utf-8")
        response = self.client.get("/questions")
        self.assertEqual(200, response.status_code)
        card = self.card(response.text, 16)
        self.assertIn("题目配图暂不可用，请进入详情核对", card)
        self.assertNotIn("<img", card)

    def test_multiple_required_figures_render_in_display_order(self):
        source = self.private / "processing/import_job_1/assets/question_016_figure_01.png"
        second = source.with_name("question_016_figure_02.png")
        shutil.copyfile(source, second)
        manifest_path = self.private / "processing/import_job_1/figure_assets.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        second_entry = dict(next(item for item in manifest["assets"] if item["question_no"] == "16"))
        second_entry["output_relative_path"] = "assets/question_016_figure_02.png"
        manifest["assets"].append(second_entry)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            qid = con.execute("SELECT id FROM questions WHERE source_question_no='16'").fetchone()[0]
            original = con.execute(
                "SELECT * FROM question_assets WHERE question_id=? AND asset_kind='question_figure'",
                (qid,),
            ).fetchone()
            con.execute(
                """INSERT INTO question_assets
                   (question_id,import_job_id,asset_kind,relative_path,width,height,byte_size,sha256,review_status,display_order)
                   VALUES(?,?,?,?,?,?,?,?,?,2)""",
                (qid, original[2], original[3], second_entry["output_relative_path"],
                 original[5], original[6], original[7], original[8], original[9]),
            )
        card = self.card(self.client.get("/questions").text, 16)
        self.assertEqual(2, card.count('class="question-required-image"'))
        self.assertLess(card.index("question_016_figure_01.png"), card.index("question_016_figure_02.png"))

    def test_shared_required_asset_selector_deduplicates_paths(self):
        question = {"figure_review_status": "passed", "question_type_code": "fill_blank"}
        assets = [
            {"id": 3, "asset_kind": "question_figure", "relative_path": "assets/b.png", "display_order": 2},
            {"id": 1, "asset_kind": "question_figure", "relative_path": "assets/a.png", "display_order": 1},
            {"id": 2, "asset_kind": "question_figure", "relative_path": "assets/a.png", "display_order": 3},
        ]
        selected = _required_question_content(question, [], assets)["display_assets"]
        self.assertEqual(["assets/a.png", "assets/b.png"], [item["relative_path"] for item in selected])

    def test_listing_has_no_empty_option_block_for_questions_without_options(self):
        page = self.client.get("/questions?question_type=fill_blank").text
        self.assertGreater(page.count('class="question-list-item"'), 0)
        self.assertNotIn('class="question-list-options"', page)
        self.assertNotIn('class="image-options-hint"', page)

        page = self.client.get("/questions?question_type=solution").text
        self.assertGreater(page.count('class="question-list-item"'), 0)
        self.assertNotIn('class="question-list-options"', page)
        self.assertNotIn('class="image-options-hint"', page)

    def test_listing_escapes_option_html_and_still_excludes_q12(self):
        with sqlite3.connect(self.db) as con:
            question_id = con.execute(
                "SELECT id FROM questions WHERE source_question_no='1'"
            ).fetchone()[0]
            con.execute(
                "UPDATE question_options SET content_markdown='<script>optionAttack()</script>' "
                "WHERE question_id=? AND option_code='A'", (question_id,)
            )
        page = self.client.get("/questions").text
        self.assertNotIn("<script>optionAttack()</script>", page)
        self.assertIn("&lt;script&gt;optionAttack()&lt;/script&gt;", page)
        self.assertNotIn("原题号 12", page)

    def test_knowledge_filter_only_lists_points_used_by_formal_questions(self):
        with sqlite3.connect(self.db) as con:
            expected = {
                row[0] for row in con.execute(
                    """SELECT kp.code FROM knowledge_points kp
                       WHERE kp.id IN (SELECT primary_knowledge_point_id FROM questions)
                          OR kp.id IN (SELECT knowledge_point_id FROM question_related_knowledge_points)"""
                )
            }
            all_active_count = con.execute(
                "SELECT COUNT(*) FROM knowledge_points WHERE is_active=1"
            ).fetchone()[0]
        parser = QuestionsPageParser()
        parser.feed(self.client.get("/questions").text)
        self.assertEqual(expected, set(parser.knowledge_values))
        self.assertEqual(len(expected), len(parser.knowledge_values))
        self.assertLess(len(parser.knowledge_values), all_active_count)

    def test_detail_has_structure_source_assets_audit_and_missing_answer(self):
        with sqlite3.connect(self.db) as con:
            code = con.execute("SELECT question_code FROM questions WHERE source_question_no='3'").fetchone()[0]
        detail = self.client.get(f"/questions/{code}")
        self.assertEqual(200, detail.status_code)
        for expected in ("原卷未提供答案", "完整题目原图", "独立配图", "合成测试学校", "原题号 3", "AI审核通过"):
            self.assertIn(expected, detail.text)
        self.assertNotIn("见原页选项图", detail.text)
        self.assertEqual(404, self.client.get("/questions/Q-bbbbbbbbbbbbbbbb-012").status_code)

    def test_question_22_detail_has_two_main_questions_with_nested_roman_items(self):
        with sqlite3.connect(self.db) as con:
            code = con.execute("SELECT question_code FROM questions WHERE source_question_no='22'").fetchone()[0]
        detail = self.client.get(f"/questions/{code}").text
        self.assertEqual(2, detail.count('class="subquestion-main"'))
        self.assertIn('class="subquestion-children"', detail)
        self.assertNotIn("（3）", detail)
        self.assertNotIn("（2）（i）", detail)

    def test_formal_images_are_db_whitelisted_and_manifest_verified(self):
        with sqlite3.connect(self.db) as con:
            code, path = con.execute(
                """SELECT q.question_code,a.relative_path FROM questions q JOIN question_assets a ON a.question_id=q.id
                   WHERE q.source_question_no='3' AND a.asset_kind='complete_question'"""
            ).fetchone()
        self.assertEqual(200, self.client.get(f"/question-assets/{code}/{path}").status_code)
        self.assertEqual(403, self.client.get(f"/question-assets/{code}/question_crops/Q004.png").status_code)
        self.assertIn(self.client.get(f"/question-assets/{code}/%2e%2e/question-bank.db").status_code, (403, 404))
        manifest = self.private / "processing/import_job_1/question_crops.json"
        data = json.loads(manifest.read_text()); data["questions"][2]["sha256"] = "0"*64
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(404, self.client.get(f"/question-assets/{code}/{path}").status_code)

    def test_html_is_escaped(self):
        with sqlite3.connect(self.db) as con:
            code = con.execute("SELECT question_code FROM questions LIMIT 1").fetchone()[0]
            con.execute("UPDATE questions SET stem_markdown='<script>alert(1)</script>' WHERE question_code=?", (code,))
        page = self.client.get(f"/questions/{code}").text
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__": unittest.main()
