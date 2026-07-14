import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import initialize_database
from src.web.app import create_app


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "question-bank.db"
        self.private_root = self.root / "private"
        initialize_database(self.db_path).close()
        self._insert_fixture_records()
        self._write_job_files()
        self.client = TestClient(
            create_app(database_path=self.db_path, private_root=self.private_root)
        )

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def _insert_fixture_records(self):
        with sqlite3.connect(self.db_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256, file_size, original_filename, stored_path, region_code,
                    exam_year, exam_type_code, paper_name)
                   VALUES (?, 100, ?, ?, 'TJ', 2025, 'YK', ?)""",
                (
                    "a" * 64,
                    "生态城原卷.pdf",
                    "raw_papers/TJ/2025/fixture.pdf",
                    "生态城测试卷",
                ),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id, page_start, page_end, status)
                   VALUES (?, 1, 4, 'needs_review')""",
                (source_id,),
            )
            knowledge_id = connection.execute(
                "SELECT id FROM knowledge_points WHERE code = '01.01.01'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO questions
                   (question_code, stem_markdown, answer_markdown, region_code,
                    exam_type_code, question_type_code, primary_knowledge_point_id,
                    content_hash)
                   VALUES ('FORMAL-1', '正式题', '答案', 'TJ', 'YK',
                           'single_choice', ?, 'formal-hash')""",
                (knowledge_id,),
            )

    def _write_job_files(self):
        job_dir = self.private_root / "processing" / "import_job_1"
        pages_dir = job_dir / "pages"
        pages_dir.mkdir(parents=True)
        (pages_dir / "page_001.png").write_bytes(b"\x89PNG\r\nfixture")
        assets_dir = job_dir / "assets"
        review_dir = job_dir / "review"
        assets_dir.mkdir()
        review_dir.mkdir()
        for path in (
            assets_dir / "question_003_figure_01.png",
            assets_dir / "question_016_figure_01.png",
            review_dir / "question_012_evidence_original.png",
            review_dir / "question_012_evidence_enhanced.png",
        ):
            Image.new("RGB", (20, 10), "white").save(path, "PNG")
        (self.private_root / "question-bank.db").write_bytes(b"secret database")
        (self.private_root / "raw.pdf").write_bytes(b"%PDF-secret")

        questions = []
        for number in range(1, 24):
            questions.append(
                {
                    "source_question_no": str(number),
                    "question_type_code": (
                        "single_choice" if number <= 12 else "fill_blank" if number <= 20 else "solution"
                    ),
                    "stem_markdown": (
                        "<script>alert('xss')</script> $x^2$" if number == 1
                        else f"第 {number} 题题干 $x+{number}$"
                    ),
                    "options": [
                        {"code": "A", "content": "<img src=x onerror=alert(1)>"},
                        {"code": "B", "content": "$2$"},
                        {"code": "C", "content": "$3$"},
                        {"code": "D", "content": "$4$"},
                    ] if number <= 12 else [],
                    "subquestions": [
                        {"label": "（1）", "stem_markdown": "证明 $x>0$"}
                    ] if number > 20 else [],
                    "answer_markdown": "",
                    "source_pages": [1],
                    "primary_knowledge_point_code": "01.01.01",
                    "related_knowledge_point_codes": ["01.01.02"],
                    "figure_required": number in (3, 16),
                    "figure_notes": "需要保留完整图形" if number in (3, 16) else "",
                    "confidence": "medium" if number in (11, 23) else "high",
                    "review_notes": ["重点检查公式"] if number in (11, 23) else [],
                }
            )
        candidate = {
            "import_job_id": 1,
            "source_paper_id": 1,
            "paper_name": "生态城测试卷",
            "page_range": [1, 4],
            "question_count": 23,
            "questions": questions,
            "global_review_notes": ["本卷页面未提供答案或解析。"],
        }
        (job_dir / "candidate_questions.json").write_text(
            json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
        )
        manifest = {
            "import_job_id": 1,
            "page_count": 1,
            "pages": [
                {
                    "page_number": 1,
                    "relative_path": "pages/page_001.png",
                    "pixel_width": 100,
                    "pixel_height": 200,
                }
            ],
        }
        (job_dir / "render_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        figure_assets = {
            "version": 1,
            "assets": [
                {"question_no": "3", "kind": "question_figure", "output_relative_path": "assets/question_003_figure_01.png", "review_status": "pending_ai_review"},
                {"question_no": "16", "kind": "question_figure", "output_relative_path": "assets/question_016_figure_01.png", "review_status": "pending_ai_review"},
                {"question_no": "12", "kind": "review_evidence", "output_relative_path": "review/question_012_evidence_original.png", "processing": {"variant": "original"}, "review_status": "review_evidence"},
                {"question_no": "12", "kind": "review_evidence", "output_relative_path": "review/question_012_evidence_enhanced.png", "processing": {"variant": "enhanced"}, "review_status": "review_evidence"},
            ],
        }
        (job_dir / "figure_assets.json").write_text(json.dumps(figure_assets), encoding="utf-8")
        crops_dir = job_dir / "question_crops"
        crops_dir.mkdir()
        crop_questions = []
        for number in range(1, 24):
            relative = f"question_crops/Q{number:03d}.png"
            output = job_dir / relative
            Image.new("RGB", (30, 20), (number, 20, 30)).save(output, "PNG")
            crop_questions.append({
                "question_no": number,
                "regions": [{"page_number": 1, "bbox": [0, 0, 30, 20]}],
                "output_relative_path": relative,
                "width": 30, "height": 20, "byte_size": output.stat().st_size,
                "sha256": __import__("hashlib").sha256(output.read_bytes()).hexdigest(),
                "crop_status": "generated", "review_status": "pending_ai_review",
                "warnings": [],
            })
        crop_manifest = {
            "import_job_id": 1, "question_count": 23,
            "source_pages": [{"page_number": 1, "relative_path": "pages/page_001.png"}],
            "questions": crop_questions,
        }
        (job_dir / "question_crops.json").write_text(json.dumps(crop_manifest), encoding="utf-8")
        self._write_audit(job_dir)

    def _write_audit(self, job_dir=None):
        job_dir = job_dir or self.private_root / "processing" / "import_job_1"
        audit_questions = []
        for number in range(1, 24):
            status = "human_required" if number in (3, 12, 16) else "auto_pass"
            audit_questions.append(
                {
                    "source_question_no": str(number),
                    "audit_status": status,
                    "issues": [
                        "<script>alert('audit-xss')</script> 必须核对原图"
                    ] if number == 3 else (["关键条件被遮挡"] if status == "human_required" else []),
                    "suggested_corrections": [
                        "<img src=x onerror=alert(2)> 建议查看高清原件"
                    ] if status == "human_required" else [],
                    "evidence_page": 1 if number <= 5 else 2 if number <= 15 else 3,
                    "audit_confidence": "high",
                }
            )
        audit = {
            "import_job_id": 1,
            "question_count": 23,
            "counts": {"auto_pass": 20, "disputed": 0, "human_required": 3},
            "questions": audit_questions,
            "random_sample_recommendation": {
                "question_nos": [2, 5, 9],
                "reason": "覆盖函数、概率与 <b>重点公式</b>。",
            },
        }
        (job_dir / "ai_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False), encoding="utf-8"
        )

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_home_has_cards_and_real_statistics(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        for text in ("AI 数学题库", "我的题库", "试卷与审核", "选题篮", "数据与设置"):
            self.assertIn(text, response.text)
        self.assertGreaterEqual(response.text.count("开发中"), 1)
        self.assertIn('href="/basket"', response.text)
        self.assertIn('href="/questions"', response.text)
        for label in ("原始试卷数", "导入任务数", "待审核任务数", "正式题目数"):
            self.assertIn(label, response.text)
        self.assertIn('href="/papers"', response.text)

    def test_papers_reads_database_and_links_review(self):
        response = self.client.get("/papers")
        self.assertEqual(200, response.status_code)
        for text in ("生态城测试卷", "生态城原卷.pdf", "天津", "2025", "月考", "1–4", "待人工审核", "23 道", "AI已复核", "自动通过 20", "AI有争议 0", "需人工确认 3", "处理人工确认", "查看全部"):
            self.assertIn(text, response.text)
        self.assertIn('href="/review/1?status=human_required"', response.text)
        self.assertIn('href="/review/1?status=all"', response.text)

    def test_review_defaults_to_three_human_required_questions(self):
        response = self.client.get("/review/1")
        self.assertEqual(200, response.status_code)
        for text in ("整卷 23 道候选题", "自动通过", "AI有争议", "需人工确认", "当前显示 3 道", "候选识别，尚未人工确认", "本卷未提供答案", "配图待AI复核", "知识点", "上一题", "下一题"):
            self.assertIn(text, response.text)
        self.assertEqual(3, response.text.count('class="question-card'))
        self.assertIn("集合的含义与表示", response.text)
        for number in (3, 12, 16):
            self.assertIn(f'id="question-{number}"', response.text)
        self.assertNotIn('id="question-1"', response.text)

    def test_all_four_audit_filters(self):
        cases = {
            "human_required": (3, "当前显示 3 道"),
            "disputed": (0, "本卷暂无AI争议题"),
            "auto_pass": (20, "当前显示 20 道"),
            "all": (23, "当前显示 23 道"),
        }
        for status, (count, text) in cases.items():
            with self.subTest(status=status):
                response = self.client.get(f"/review/1?status={status}")
                self.assertEqual(200, response.status_code)
                self.assertEqual(count, response.text.count('class="question-card'))
                self.assertIn(text, response.text)
        response = self.client.get("/review/1?status=all")
        for status in ("human_required", "disputed", "auto_pass", "all"):
            self.assertIn(f'?status={status}', response.text)

    def test_audit_details_warning_and_sample_recommendation(self):
        response = self.client.get("/review/1")
        for text in ("第二轮AI审核：需人工确认", "问题", "必须核对原图", "建议更正", "建议查看高清原件", "证据页：第 1 页", "建议抽查", "覆盖函数、概率"):
            self.assertIn(text, response.text)
        self.assertIn('/review/1?status=auto_pass#question-2', response.text)
        auto_response = self.client.get("/review/1?status=auto_pass")
        self.assertIn("第二轮AI审核：自动通过", auto_response.text)
        self.assertIn("仅文字/结构/公式复核通过；答案正确性尚未审核", auto_response.text)

    def test_missing_audit_falls_back_to_original_review(self):
        (self.private_root / "processing/import_job_1/ai_audit.json").unlink()
        papers = self.client.get("/papers")
        self.assertIn("等待AI复核", papers.text)
        self.assertIn('href="/review/1"', papers.text)
        self.assertNotIn("自动通过 20", papers.text)
        review = self.client.get("/review/1")
        self.assertEqual(23, review.text.count('class="question-card'))
        self.assertNotIn("AI复核数据损坏或不完整", review.text)

    def test_broken_or_mismatched_audit_falls_back_without_500(self):
        path = self.private_root / "processing/import_job_1/ai_audit.json"
        cases = ("{broken", None)
        for broken in cases:
            with self.subTest(broken=broken is not None):
                if broken is not None:
                    path.write_text(broken, encoding="utf-8")
                else:
                    self._write_audit()
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["questions"][0]["source_question_no"] = "999"
                    path.write_text(json.dumps(data), encoding="utf-8")
                response = self.client.get("/review/1")
                self.assertEqual(200, response.status_code)
                self.assertIn("AI复核数据损坏或不完整", response.text)
                self.assertEqual(23, response.text.count('class="question-card'))

    def test_audit_text_is_html_escaped(self):
        response = self.client.get("/review/1")
        self.assertNotIn("<script>alert('audit-xss')</script>", response.text)
        self.assertIn("&lt;script&gt;alert", response.text)
        self.assertNotIn("<img src=x onerror=alert(2)>", response.text)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", response.text)
        self.assertNotIn("<b>重点公式</b>", response.text)
        self.assertIn("&lt;b&gt;重点公式&lt;/b&gt;", response.text)

    def test_candidate_text_is_html_escaped(self):
        response = self.client.get("/review/1?status=all")
        self.assertNotIn("<script>alert('xss')</script>", response.text)
        self.assertIn("&lt;script&gt;alert", response.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", response.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", response.text)

    def test_missing_job_returns_chinese_404(self):
        response = self.client.get("/review/999")
        self.assertEqual(404, response.status_code)
        self.assertIn("未找到导入任务", response.text)
        self.assertNotIn(str(self.root), response.text)

    def test_broken_candidate_json_has_safe_chinese_error(self):
        candidate_path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate_path.write_text("{broken", encoding="utf-8")
        response = self.client.get("/review/1")
        self.assertEqual(500, response.status_code)
        self.assertIn("候选题数据损坏", response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("Traceback", response.text)

    def test_manifest_whitelisted_png_is_accessible(self):
        response = self.client.get("/private-pages/1/pages/page_001.png")
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual(b"\x89PNG\r\nfixture", response.content)

    def test_unlisted_private_files_and_non_png_are_denied(self):
        for url in (
            "/private-pages/1/question-bank.db",
            "/private-pages/1/raw.pdf",
            "/private-pages/1/pages/not-listed.png",
            "/data/private/question-bank.db",
            "/data/private/raw.pdf",
        ):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (404, 403))

    def test_figure_asset_whitelist_and_review_display(self):
        for relative in (
            "assets/question_003_figure_01.png",
            "assets/question_016_figure_01.png",
            "review/question_012_evidence_original.png",
            "review/question_012_evidence_enhanced.png",
        ):
            response = self.client.get(f"/private-pages/1/{relative}")
            self.assertEqual(200, response.status_code)
            self.assertEqual("image/png", response.headers["content-type"])
        response = self.client.get("/review/1")
        self.assertEqual(2, response.text.count("配图待AI复核"))
        self.assertIn("question_003_figure_01.png", response.text)
        self.assertIn("question_016_figure_01.png", response.text)
        self.assertIn("question_012_evidence_original.png", response.text)
        self.assertIn("question_012_evidence_enhanced.png", response.text)
        self.assertIn("增强仅用于辨认，不代表恢复原文", response.text)
        self.assertNotIn("需要切图", response.text)

    def test_question_figure_label_reflects_actual_ai_review_status(self):
        manifest_path = self.private_root / "processing/import_job_1/figure_assets.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for asset in manifest["assets"]:
            if asset["question_no"] == "3" and asset["kind"] == "question_figure":
                asset["review_status"] = "ai_review_passed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        response = self.client.get("/review/1")
        self.assertEqual(200, response.status_code)
        question_3 = response.text.split('id="question-3"', 1)[1].split(
            'id="question-12"', 1
        )[0]
        question_16 = response.text.split('id="question-16"', 1)[1]
        self.assertIn("配图AI复核通过", question_3)
        self.assertNotIn("配图待AI复核", question_3)
        self.assertIn("配图待AI复核", question_16)
        self.assertNotIn("配图AI复核通过", question_16)

    def test_malformed_or_unsafe_figure_manifest_is_not_exposed(self):
        path = self.private_root / "processing/import_job_1/figure_assets.json"
        data = json.loads(path.read_text())
        data["assets"].append({
            "question_no": "<script>x</script>", "kind": "question_figure",
            "output_relative_path": "../page_001.png", "review_status": "pending_ai_review",
        })
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(403, self.client.get("/private-pages/1/assets/not-listed.png").status_code)
        response = self.client.get("/review/1")
        self.assertNotIn("<script>x</script>", response.text)

    def test_path_traversal_and_absolute_paths_are_denied(self):
        for url in (
            "/private-pages/1/%2e%2e/question-bank.db",
            "/private-pages/1/pages/%2e%2e/%2e%2e/raw.pdf",
            "/private-pages/1/%2Fetc%2Fpasswd",
        ):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (404, 403))

    def test_all_23_question_crops_are_matched_displayed_and_whitelisted(self):
        response = self.client.get("/review/1?status=all")
        self.assertEqual(23, response.text.count("完整题目原图（待AI复核）"))
        for number in range(1, 24):
            relative = f"question_crops/Q{number:03d}.png"
            self.assertIn(relative, response.text)
            image = self.client.get(f"/private-pages/1/{relative}")
            self.assertEqual(200, image.status_code)
            self.assertEqual("image/png", image.headers["content-type"])

    def test_broken_or_incomplete_question_crop_manifest_safely_falls_back(self):
        path = self.private_root / "processing/import_job_1/question_crops.json"
        original = path.read_text(encoding="utf-8")
        for mutation in ("broken", "missing", "duplicate"):
            with self.subTest(mutation=mutation):
                path.write_text(original, encoding="utf-8")
                if mutation == "broken":
                    path.write_text("{broken", encoding="utf-8")
                else:
                    data = json.loads(path.read_text())
                    if mutation == "missing":
                        data["questions"].pop()
                    else:
                        data["questions"][-1]["question_no"] = 1
                    path.write_text(json.dumps(data), encoding="utf-8")
                response = self.client.get("/review/1?status=all")
                self.assertEqual(200, response.status_code)
                self.assertEqual(23, response.text.count("单题原图尚未生成或清单异常"))
                self.assertNotIn("完整题目原图（待AI复核）", response.text)

    def test_question_crop_manifest_path_traversal_and_html_are_not_exposed(self):
        path = self.private_root / "processing/import_job_1/question_crops.json"
        data = json.loads(path.read_text())
        data["questions"][0]["output_relative_path"] = "../<script>alert(1)</script>.png"
        path.write_text(json.dumps(data), encoding="utf-8")
        response = self.client.get("/review/1?status=all")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertEqual(23, response.text.count("单题原图尚未生成或清单异常"))
        self.assertIn(self.client.get("/private-pages/1/question_crops/%2e%2e/raw.pdf").status_code, (403, 404))


if __name__ == "__main__":
    unittest.main()
