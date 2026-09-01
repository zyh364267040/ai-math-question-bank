import hashlib
import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from src.database.initialize import initialize_database
from src.importing.admit_questions import admit_questions
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest
from src.web.app import (
    _required_question_content,
    _verified_asset,
    _verified_asset_path,
    create_app,
)
from tests.fixture_factory import (
    anchor_synthetic_candidate_audit,
    anchor_synthetic_figure_reviews,
    create_import_job_fixture,
)


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
        job_dir = create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            source = con.execute(
                """INSERT INTO source_papers
                (sha256,file_size,original_filename,stored_path,region_code,exam_year,exam_type_code,paper_name)
                VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf','TJ',2025,'YK','合成测试学校')""", ("b"*64,)
            ).lastrowid
            con.execute("INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) VALUES(1,?,1,4,'needs_review')", (source,))
        anchor_synthetic_candidate_audit(self.db, job_dir)
        anchor_synthetic_figure_reviews(self.db, self.private)
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

    def test_listing_heading_has_import_link_and_filters_still_work(self):
        listing = self.client.get("/questions?question_type=single_choice")

        self.assertEqual(200, listing.status_code)
        self.assertIn('class="button" href="/imports/new"', listing.text)
        self.assertIn("导入新试卷", listing.text)
        self.assertIn('name="question_type"', listing.text)
        self.assertGreater(listing.text.count('class="question-list-item"'), 0)

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

    def test_detail_renders_source_linked_answers_stored_on_subquestions(self):
        with sqlite3.connect(self.db) as con:
            question_id, code = con.execute(
                "SELECT id,question_code FROM questions WHERE source_question_no='22'"
            ).fetchone()
            subquestion_ids = [
                row[0] for row in con.execute(
                    "SELECT id FROM subquestions WHERE question_id=? ORDER BY display_order",
                    (question_id,),
                )
            ]
            self.assertEqual(4, len(subquestion_ids))
            for index, subquestion_id in enumerate(subquestion_ids, 1):
                con.execute(
                    "UPDATE subquestions SET answer_markdown=?,analysis_markdown=?,answer_status='provided' WHERE id=?",
                    (f"小问答案{index}", f"小问解析{index}", subquestion_id),
                )
            con.execute(
                "UPDATE questions SET answer_markdown='',analysis_markdown=NULL WHERE id=?",
                (question_id,),
            )
            con.execute(
                "UPDATE import_answer_sources SET source_answer_state='source_answer_linked',"
                "answer_page_start=1,answer_page_end=4,render_manifest_sha256=? "
                "WHERE import_job_id=1",
                ("f" * 64,),
            )

        detail = self.client.get(f"/questions/{code}")

        self.assertEqual(200, detail.status_code)
        self.assertIn("原卷答案已审核", detail.text)
        for index in range(1, 5):
            self.assertIn(f"小问答案{index}", detail.text)
            self.assertIn(f"小问解析{index}", detail.text)

    def test_detail_keeps_legacy_provided_answer_without_answer_source_row_visible(self):
        with sqlite3.connect(self.db) as con:
            code = con.execute(
                "SELECT question_code FROM questions WHERE source_question_no='1'"
            ).fetchone()[0]
            answer = "旧流程候选答案"
            analysis = "旧流程候选解析"
            con.execute(
                "UPDATE questions SET answer_markdown=?,answer_status='provided',"
                "answer_review_status='passed',analysis_markdown=?,"
                "analysis_review_status='passed' WHERE question_code=?",
                (answer, analysis, code),
            )
            con.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")

        detail = self.client.get(f"/questions/{code}")

        self.assertEqual(200, detail.status_code)
        self.assertIn("AI候选答案", detail.text)
        self.assertIn(answer, detail.text)
        self.assertIn(analysis, detail.text)
        self.assertNotIn("原卷答案待处理", detail.text)

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

    def test_complete_question_get_uses_bytes_fixed_during_verification(self):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            asset = connection.execute(
                """SELECT a.*,q.question_code FROM questions q
                   JOIN question_assets a ON a.question_id=q.id
                   WHERE q.source_question_no='3'
                     AND a.asset_kind='complete_question'"""
            ).fetchone()
        target = (
            self.private
            / f"processing/import_job_{asset['import_job_id']}"
            / asset["relative_path"]
        )
        verified_png = target.read_bytes()

        def replace_path_after_verification(private_root, candidate, connection=None):
            verified = _verified_asset(private_root, candidate, connection)
            target.write_bytes(b"replaced after ordinary asset verification")
            return verified

        with patch(
            "src.web.app._verified_asset", side_effect=replace_path_after_verification
        ):
            response = self.client.get(
                f"/question-assets/{asset['question_code']}/{asset['relative_path']}"
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(verified_png, response.content)

    def test_pending_question_figure_with_passed_db_row_is_served(self):
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            asset = con.execute(
                """SELECT a.*,q.question_code FROM questions q JOIN question_assets a ON a.question_id=q.id
                   WHERE q.source_question_no='16' AND a.asset_kind='question_figure'"""
            ).fetchone()

        self.assertIsInstance(asset, sqlite3.Row)
        self.assertTrue(_verified_asset_path(self.private, asset).is_file())
        response = self.client.get(
            f"/question-assets/{asset['question_code']}/{asset['relative_path']}"
        )
        self.assertEqual(200, response.status_code)

    def test_legacy_passed_question_figure_with_passed_db_is_served(self):
        with sqlite3.connect(self.db) as con:
            code, path = con.execute(
                """SELECT q.question_code,a.relative_path FROM questions q JOIN question_assets a ON a.question_id=q.id
                   WHERE q.source_question_no='16' AND a.asset_kind='question_figure'"""
            ).fetchone()
        manifest_path = self.private / "processing/import_job_1/figure_assets.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(item for item in manifest["assets"] if item["output_relative_path"] == path)
        entry["review_status"] = "ai_review_passed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertEqual(200, self.client.get(f"/question-assets/{code}/{path}").status_code)

    def test_question_figure_other_review_status_combinations_fail_closed(self):
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """SELECT a.* FROM questions q JOIN question_assets a ON a.question_id=q.id
                   WHERE q.source_question_no='16' AND a.asset_kind='question_figure'"""
            ).fetchone()
        manifest_path = self.private / "processing/import_job_1/figure_assets.json"

        for manifest_status, database_status in (
            ("pending_ai_review", "pending_ai_review"),
            ("ai_review_passed", "pending_ai_review"),
            ("rejected", "ai_review_passed"),
            (None, "ai_review_passed"),
        ):
            with self.subTest(manifest=manifest_status, database=database_status):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = next(
                    item for item in manifest["assets"]
                    if item["output_relative_path"] == row["relative_path"]
                )
                if manifest_status is None:
                    entry.pop("review_status", None)
                else:
                    entry["review_status"] = manifest_status
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                asset = dict(row)
                asset["review_status"] = database_status

                with self.assertRaises(ValueError):
                    _verified_asset_path(self.private, asset)

    def test_html_is_escaped(self):
        with sqlite3.connect(self.db) as con:
            code = con.execute("SELECT question_code FROM questions LIMIT 1").fetchone()[0]
            con.execute("UPDATE questions SET stem_markdown='<script>alert(1)</script>' WHERE question_code=?", (code,))
        page = self.client.get(f"/questions/{code}").text
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


class HistoricalCropAssetRouteTests(unittest.TestCase):
    """Production-route coverage for immutable historical crop recovery authority."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.private = root / "private"
        self.job_dir = create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf',
                           'TJ',2025,'YK','历史恢复路由测试')""",
                ("b" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) "
                "VALUES(1,?,1,4,'needs_review')",
                (source_id,),
            )
        anchor_synthetic_candidate_audit(self.db, self.job_dir)
        anchor_synthetic_figure_reviews(self.db, self.private)
        admit_questions(self.db, self.private, 1)
        self.client = TestClient(create_app(self.db, self.private))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _publish_recovery(
        self, *, manifest_mutator=None, question_nos=None,
        recovery_generation=None, recovered_png=None, publish_resumption=True,
        resumption_source_paper_id=1, resumption_source_pdf_sha256=None,
        resumption_formal_question_count=22, resumption_formal_batch_sha256=None,
        resumption_crop_question_count=23, resumption_manifest_sha256=None,
        resumption_generation=None, resumption_signature=None,
    ):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            asset = dict(connection.execute(
                """SELECT a.*,q.question_code FROM question_assets a
                   JOIN questions q ON q.id=a.question_id
                   WHERE q.source_question_no='3'
                     AND a.asset_kind='complete_question'"""
            ).fetchone())

        if recovered_png is None:
            image = io.BytesIO()
            Image.new("RGB", (53, 37), (12, 34, 56)).save(image, format="PNG")
            recovered_png = image.getvalue()
        target = self.job_dir / asset["relative_path"]
        target.write_bytes(recovered_png)

        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(
            item for item in manifest["questions"]
            if item["question_no"] == 3
        )
        entry.update({
            "width": 53,
            "height": 37,
            "byte_size": len(recovered_png),
            "sha256": hashlib.sha256(recovered_png).hexdigest(),
        })
        manifest["generation_id"] = "f" * 32
        manifest.pop("signature")
        initial_manifest = json.loads(json.dumps(manifest))
        initial_entry = next(
            item for item in initial_manifest["questions"]
            if item["question_no"] == 3
        )
        initial_entry["review_status"] = "pending_ai_review"
        initial_manifest = sign_manifest(load_hmac_key(self.job_dir), initial_manifest)
        initial_raw = (
            json.dumps(initial_manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        if manifest_mutator is not None:
            manifest_mutator(manifest, entry)
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_raw = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        manifest_path.write_bytes(manifest_raw)

        recovery_question_nos = (
            list(range(1, 24)) if question_nos is None else question_nos
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO historical_v1_crop_recoveries
                   (import_job_id,source_paper_id,source_pdf_sha256,
                    render_manifest_sha256,render_manifest_byte_size,
                    regions_manifest_sha256,regions_manifest_byte_size,
                    legacy_crop_manifest_sha256,legacy_crop_manifest_byte_size,
                    question_nos_json,prior_job_status,prior_split_status,
                    preserved_codex_run_id,new_crop_manifest_sha256,
                    new_crop_generation_id,new_crop_manifest_signature,
                    formal_question_count,formal_batch_sha256,candidate_sha256,
                    candidate_byte_size,draft_batch_sha256,migration_evidence_kind,
                    migration_evidence_json,recovered_at)
                   VALUES(1,1,?, ?,1, ?,1, ?,1, ?,'needs_review',NULL,NULL,
                          ?,?,?,22,?,NULL,NULL,?,
                          'system_migration_placeholder',?,?)""",
                (
                    "1" * 64, "2" * 64, "3" * 64, "4" * 64,
                    json.dumps(recovery_question_nos, separators=(",", ":")),
                    hashlib.sha256(initial_raw).hexdigest(),
                    recovery_generation or manifest["generation_id"],
                    initial_manifest["signature"],
                    "5" * 64, "6" * 64,
                    json.dumps({"synthetic": True}, separators=(",", ":")),
                    "2026-08-31T00:00:00+00:00",
                ),
            )
            if publish_resumption:
                connection.execute(
                    """INSERT INTO historical_v1_pipeline_resumptions
                       (import_job_id,source_paper_id,source_pdf_sha256,
                        formal_question_count,formal_batch_sha256,
                        crop_question_count,crop_manifest_sha256,
                        crop_generation_id,crop_manifest_signature,
                        reviewer_run_id,review_request_sha256,
                        review_evidence_signature,reviewed_at,resumed_at)
                       VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        resumption_source_paper_id,
                        resumption_source_pdf_sha256 or "1" * 64,
                        resumption_formal_question_count,
                        resumption_formal_batch_sha256 or "5" * 64,
                        resumption_crop_question_count,
                        resumption_manifest_sha256
                        or hashlib.sha256(manifest_raw).hexdigest(),
                        resumption_generation or manifest["generation_id"],
                        resumption_signature or manifest["signature"],
                        "fresh-reviewer-run",
                        "7" * 64,
                        "8" * 64,
                        "2026-08-31T00:01:00+00:00",
                        "2026-08-31T00:02:00+00:00",
                    ),
                )
        return asset, manifest, entry, target, manifest_path

    def _get(self, asset):
        return self.client.get(
            f"/question-assets/{asset['question_code']}/{asset['relative_path']}"
        )

    def _csrf(self):
        page = self.client.get("/questions")
        return re.search(
            r'name="csrf_token" value="([^"]+)"', page.text
        ).group(1)

    def _post(self, path):
        return self.client.post(
            path, data={"csrf_token": self._csrf()}, follow_redirects=False
        )

    def _assert_fail_closed(self, asset):
        response = self._get(asset)
        self.assertEqual(404, response.status_code)
        self.assertIn("图片暂时无法读取", response.text)
        self.assertNotIn("Traceback", response.text)

    def test_route_serves_recovered_crop_when_db_retains_old_formal_metadata(self):
        asset, _manifest, entry, _target, _path = self._publish_recovery()

        self.assertNotEqual(asset["sha256"], entry["sha256"])
        self.assertNotEqual(asset["byte_size"], entry["byte_size"])
        with sqlite3.connect(self.db) as connection:
            anchors = connection.execute(
                """SELECT c.new_crop_manifest_sha256,
                          c.new_crop_generation_id,
                          c.new_crop_manifest_signature,
                          r.crop_manifest_sha256,r.crop_generation_id,
                          r.crop_manifest_signature
                   FROM historical_v1_crop_recoveries c
                   JOIN historical_v1_pipeline_resumptions r
                     ON r.import_job_id=c.import_job_id"""
            ).fetchone()
        self.assertNotEqual(anchors[0], anchors[3])
        self.assertEqual(anchors[1], anchors[4])
        self.assertNotEqual(anchors[2], anchors[5])
        response = self._get(asset)
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])

    def test_basket_export_writes_verified_recovered_crop_bytes(self):
        image = io.BytesIO()
        Image.new("RGB", (53, 37), (12, 34, 56)).save(image, format="PNG")
        recovered_png = image.getvalue()
        asset, _manifest, entry, target, _path = self._publish_recovery(
            recovered_png=recovered_png
        )

        self.assertNotEqual(asset["sha256"], entry["sha256"])
        self.assertEqual(
            303, self._post(f"/basket/add/{asset['question_code']}").status_code
        )
        csrf_token = self._csrf()

        def replace_path_after_verification(private_root, candidate, connection=None):
            verified = _verified_asset(private_root, candidate, connection)
            target.write_bytes(b"replaced after fixed-descriptor verification")
            return verified

        with patch(
            "src.web.app._verified_asset", side_effect=replace_path_after_verification
        ):
            response = self.client.post(
                "/basket/export",
                data={"csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db) as connection:
            output_path = connection.execute(
                "SELECT output_path FROM basket_exports ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        markdown_path = self.private / output_path
        markdown = markdown_path.read_text(encoding="utf-8")
        reference = re.search(r"\]\((assets/[^)]+\.png)\)", markdown).group(1)
        self.assertEqual(recovered_png, (markdown_path.parent / reference).read_bytes())

    def test_manifest_raw_bytes_hash_tampering_fails_closed(self):
        asset, _manifest, _entry, _target, path = self._publish_recovery()
        path.write_bytes(path.read_bytes() + b" ")
        self._assert_fail_closed(asset)

    def test_missing_resumption_fails_closed(self):
        asset, *_ = self._publish_recovery(publish_resumption=False)
        self._assert_fail_closed(asset)

    def test_recovery_resumption_generation_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(recovery_generation="e" * 32)
        self._assert_fail_closed(asset)

    def test_current_manifest_hash_mismatch_with_resumption_fails_closed(self):
        asset, *_ = self._publish_recovery(resumption_manifest_sha256="e" * 64)
        self._assert_fail_closed(asset)

    def test_current_manifest_generation_mismatch_with_resumption_fails_closed(self):
        asset, *_ = self._publish_recovery(resumption_generation="e" * 32)
        self._assert_fail_closed(asset)

    def test_current_manifest_signature_mismatch_with_resumption_fails_closed(self):
        asset, *_ = self._publish_recovery(resumption_signature="e" * 64)
        self._assert_fail_closed(asset)

    def test_resumption_source_pdf_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            resumption_source_pdf_sha256="e" * 64
        )
        self._assert_fail_closed(asset)

    def test_resumption_formal_count_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(resumption_formal_question_count=21)
        self._assert_fail_closed(asset)

    def test_resumption_formal_hash_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            resumption_formal_batch_sha256="e" * 64
        )
        self._assert_fail_closed(asset)

    def test_resumption_crop_count_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(resumption_crop_question_count=22)
        self._assert_fail_closed(asset)

    def test_resumption_source_paper_binding_mismatch_fails_closed(self):
        with sqlite3.connect(self.db) as connection:
            other_source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'other.pdf','raw_papers/TJ/2025/other.pdf',
                           'TJ',2025,'YK','其他试卷')""",
                ("e" * 64,),
            ).lastrowid
        asset, *_ = self._publish_recovery(
            resumption_source_paper_id=other_source_id
        )
        self._assert_fail_closed(asset)

    def test_manifest_job_id_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            manifest_mutator=lambda manifest, _entry: manifest.update(import_job_id=2)
        )
        self._assert_fail_closed(asset)

    def test_recovery_question_membership_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            question_nos=[number for number in range(1, 24) if number != 3]
        )
        self._assert_fail_closed(asset)

    def test_question_source_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE question_sources SET source_question_no='003' "
                "WHERE question_id=?",
                (asset["question_id"],),
            )
        self._assert_fail_closed(asset)

    def test_question_source_job_binding_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) "
                "VALUES(2,1,1,4,'needs_review')"
            )
            connection.execute(
                "UPDATE question_sources SET import_job_id=2 WHERE question_id=?",
                (asset["question_id"],),
            )
        self._assert_fail_closed(asset)

    def test_duplicate_manifest_question_entry_fails_closed(self):
        def duplicate_question(manifest, entry):
            manifest["questions"].append(dict(entry))
            manifest["question_count"] += 1

        asset, *_ = self._publish_recovery(manifest_mutator=duplicate_question)
        self._assert_fail_closed(asset)

    def test_duplicate_manifest_path_fails_closed(self):
        def duplicate_path(manifest, entry):
            other = next(item for item in manifest["questions"] if item["question_no"] == 4)
            other["output_relative_path"] = entry["output_relative_path"]

        asset, *_ = self._publish_recovery(manifest_mutator=duplicate_path)
        self._assert_fail_closed(asset)

    def test_manifest_entry_review_status_tampering_fails_closed(self):
        asset, *_ = self._publish_recovery(
            manifest_mutator=lambda _manifest, entry: entry.update(
                review_status="pending_ai_review"
            )
        )
        self._assert_fail_closed(asset)

    def test_recovered_file_bytes_tampering_fails_closed(self):
        asset, _manifest, _entry, target, _path = self._publish_recovery()
        content = bytearray(target.read_bytes())
        content[-1] ^= 1
        target.write_bytes(content)
        self._assert_fail_closed(asset)

    def test_recovered_manifest_file_hash_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            manifest_mutator=lambda _manifest, entry: entry.update(sha256="0" * 64)
        )
        self._assert_fail_closed(asset)

    def test_recovered_manifest_file_size_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            manifest_mutator=lambda _manifest, entry: entry.update(
                byte_size=entry["byte_size"] + 1
            )
        )
        self._assert_fail_closed(asset)

    def test_recovered_manifest_file_dimensions_mismatch_fails_closed(self):
        asset, *_ = self._publish_recovery(
            manifest_mutator=lambda _manifest, entry: entry.update(
                width=entry["width"] + 1
            )
        )
        self._assert_fail_closed(asset)

    def test_recovered_file_must_decode_as_png(self):
        asset, *_ = self._publish_recovery(recovered_png=b"not-a-png")
        self._assert_fail_closed(asset)

    def test_recovery_authority_never_relaxes_question_figure_metadata(self):
        _crop_asset, *_ = self._publish_recovery()
        with sqlite3.connect(self.db) as connection:
            code, path = connection.execute(
                """SELECT q.question_code,a.relative_path FROM question_assets a
                   JOIN questions q ON q.id=a.question_id
                   WHERE q.source_question_no='16'
                     AND a.asset_kind='question_figure'"""
            ).fetchone()
        figure_manifest_path = self.job_dir / "figure_assets.json"
        figure_manifest = json.loads(figure_manifest_path.read_text(encoding="utf-8"))
        entry = next(item for item in figure_manifest["assets"] if item["output_relative_path"] == path)
        entry["sha256"] = "0" * 64
        figure_manifest_path.write_text(json.dumps(figure_manifest), encoding="utf-8")

        self.assertEqual(404, self.client.get(f"/question-assets/{code}/{path}").status_code)

    def test_current_db_manifest_match_still_uses_normal_asset_path(self):
        self._publish_recovery()
        with sqlite3.connect(self.db) as connection:
            code, path = connection.execute(
                """SELECT q.question_code,a.relative_path FROM question_assets a
                   JOIN questions q ON q.id=a.question_id
                   WHERE q.source_question_no='1'
                     AND a.asset_kind='complete_question'"""
            ).fetchone()
        self.assertEqual(200, self.client.get(f"/question-assets/{code}/{path}").status_code)


if __name__ == "__main__": unittest.main()
