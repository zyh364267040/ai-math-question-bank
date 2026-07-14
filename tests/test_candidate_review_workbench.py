import hashlib
import html
import json
import re
import sqlite3
from html.parser import HTMLParser

from tests.test_web_app import WebAppTests


class _InteractionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form_depth = 0
        self.nested_forms = 0
        self.buttons_inside_links = 0
        self.links_inside_buttons = 0
        self.link_depth = 0
        self.button_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            if self.form_depth:
                self.nested_forms += 1
            self.form_depth += 1
        elif tag == "a":
            if self.button_depth:
                self.links_inside_buttons += 1
            self.link_depth += 1
        elif tag == "button":
            if self.link_depth:
                self.buttons_inside_links += 1
            self.button_depth += 1

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_depth -= 1
        elif tag == "a":
            self.link_depth -= 1
        elif tag == "button":
            self.button_depth -= 1


class CandidateReviewWorkbenchTests(WebAppTests):
    def test_workbench_distinguishes_human_and_ai_approvals(self):
        self.assertEqual(303, self.quick_post(number=1).status_code)
        self.assertEqual(303, self.quick_post(number=2).status_code)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET status='approved',approval_source='ai_second_pass' WHERE source_question_no='2'"""
            )
        page = self.client.get("/reviews/1/questions/1")
        self.assertIn("已通过 2", page.text)
        self.assertIn("人工审核通过 1", page.text)
        self.assertIn("AI二审通过 1", page.text)
        self.assertIn("通过来源：人工审核", page.text)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(
                ("approved", "human"),
                connection.execute(
                    "SELECT status,approval_source FROM candidate_review_drafts WHERE source_question_no='1'"
                ).fetchone(),
            )
    def csrf(self, path="/reviews/1/questions/3"):
        response = self.client.get(path)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

    def post(self, number=3, **overrides):
        data = {
            "csrf_token": self.csrf(f"/reviews/1/questions/{number}"),
            "version": "1", "action": "save", "stem_markdown": "更新后题干 $x$",
            "question_type_code": (
                "single_choice" if number <= 12 else "fill_blank" if number <= 20 else "solution"
            ), "primary_knowledge_point_code": "01.01.01",
            "related_knowledge_point_codes": ["01.01.02", "01.01.02"],
            "review_notes": "人工备注",
        }
        if number <= 12:
            data.update({
                "option_source_index": ["0", "1"], "option_code": ["A", "B"],
                "option_content": ["甲", "乙"], "option_order": ["1", "2"],
            })
        elif number > 20:
            data.update({
                "subquestion_source_index": ["0"],
                "subquestion_content": ["证明 $x>0$"], "subquestion_order": ["1"],
            })
        data.update(overrides)
        return self.client.post(f"/reviews/1/questions/{number}", data=data, follow_redirects=False)

    def quick_post(self, number=3, action="approve", version="1", job_id=1, **overrides):
        path = f"/reviews/{job_id}/questions/{number}"
        data = {
            "csrf_token": self.csrf(),
            "version": version,
            "action": action,
        }
        data.update(overrides)
        return self.client.post(f"{path}/quick-status", data=data, follow_redirects=False)

    def inline_post(self, number=3, field="stem_markdown", value="原位修改 $x$", version="1",
                    index=None, job_id=1, **overrides):
        path = f"/reviews/{job_id}/questions/{number}"
        data = {
            "csrf_token": self.csrf(),
            "version": version,
            "field": field,
            "value": value,
        }
        if index is not None:
            data["index"] = str(index)
        data.update(overrides)
        return self.client.post(f"{path}/inline-edit", data=data)

    def delete_post(self, number=3, version="1", reason="unreadable", note="扫描模糊", **extra):
        data = {
            "csrf_token": self.csrf(f"/reviews/1/questions/{number}"),
            "version": version, "reason": reason, "note": note, "confirmed": "yes",
        }
        data.update(extra)
        return self.client.post(
            f"/reviews/1/questions/{number}/delete", data=data, follow_redirects=False
        )

    def restore_post(self, number=3, version="2"):
        return self.client.post(
            f"/reviews/1/questions/{number}/restore",
            data={"csrf_token": self.csrf("/reviews/1/deleted"), "version": version},
            follow_redirects=False,
        )

    def test_candidate_delete_entrances_confirmation_and_post_security(self):
        workbench = self.client.get("/reviews/1/questions/3")
        overview = self.client.get("/review/1")
        for page in (workbench.text, overview.text):
            self.assertIn('action="/reviews/1/questions/3/delete"', page)
            self.assertIn("删除本题", page)
            self.assertIn("可在已删除题目中恢复", page)
            parser = _InteractionParser(); parser.feed(page)
            self.assertEqual(0, parser.nested_forms)
        self.assertEqual(405, self.client.get("/reviews/1/questions/3/delete").status_code)
        self.assertEqual(403, self.client.post(
            "/reviews/1/questions/3/delete",
            data={"version": "1", "reason": "unreadable", "confirmed": "yes"},
        ).status_code)
        self.assertEqual(400, self.delete_post(reason="invalid").status_code)
        self.assertEqual(400, self.delete_post(note="x" * 501).status_code)
        self.assertEqual(400, self.delete_post(unexpected="x").status_code)

    def test_candidate_soft_delete_preserves_all_evidence_filters_and_rejects_edits(self):
        job_dir = self.private_root / "processing/import_job_1"
        evidence = [job_dir / "candidate_questions.json", *sorted(job_dir.glob("**/*.png"))]
        before = {path.relative_to(job_dir): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence}
        response = self.delete_post(number=3, note="<script>noteAttack()</script>")
        self.assertEqual(303, response.status_code)
        self.assertEqual("/reviews/1/questions/4?deleted=1", response.headers["location"])
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,version,deleted_at,deletion_reason,deletion_note FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
            self.assertEqual(("pending", 2), row[:2])
            self.assertIsNotNone(row[2])
            self.assertEqual(("unreadable", "<script>noteAttack()</script>"), row[3:])
            self.assertEqual(23, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])
        after = {path.relative_to(job_dir): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence}
        self.assertEqual(before, after)

        deleted_page = self.client.get("/reviews/1/questions/3")
        self.assertEqual(410, deleted_page.status_code)
        self.assertIn("本题已从审核流程删除", deleted_page.text)
        self.assertIn("恢复本题", deleted_page.text)
        self.assertNotIn("审核通过并进入下一题", deleted_page.text)
        self.assertNotIn("完整审核与修改", deleted_page.text)
        self.assertEqual(410, self.quick_post(number=3, version="2").status_code)
        self.assertEqual(410, self.inline_post(number=3, version="2").status_code)
        self.assertEqual(410, self.post(number=3, version="2").status_code)
        active = self.client.get("/reviews/1/questions/4").text
        self.assertNotIn('href="/reviews/1/questions/3"', active)
        self.assertIn("共 22 题", active)
        self.assertIn("已删除 1", active)
        overview = self.client.get("/review/1").text
        self.assertNotIn('id="question-3"', overview)
        papers = self.client.get("/papers").text
        self.assertIn("已删除 1 题", papers)
        self.assertIn('href="/reviews/1/deleted"', papers)
        recovery = self.client.get("/reviews/1/deleted")
        self.assertIn("&lt;script&gt;noteAttack()&lt;/script&gt;", recovery.text)

    def test_candidate_version_conflict_restore_and_original_status_structure(self):
        self.assertEqual(303, self.delete_post(number=22).status_code)
        conflict = self.delete_post(number=22, version="1")
        self.assertEqual(409, conflict.status_code)
        restored = self.restore_post(number=22)
        self.assertEqual(303, restored.status_code)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,version,deleted_at,deletion_reason,edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='22'"
            ).fetchone()
        self.assertEqual(("pending", 3, None, "unreadable"), row[:4])
        edited = json.loads(row[4])
        self.assertEqual(["（1）"], [item["label"] for item in edited["subquestions"]])
        page = self.client.get("/reviews/1/questions/22")
        self.assertEqual(200, page.status_code)
        self.assertIn("审核通过并进入下一题", page.text)
        self.assertEqual(409, self.restore_post(number=22, version="2").status_code)

    def test_deleting_last_active_candidate_has_safe_destination(self):
        candidate_path = self.private_root / "processing/import_job_1/candidate_questions.json"
        questions = json.loads(candidate_path.read_text(encoding="utf-8"))["questions"]
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("BEGIN")
            from src.web.app import _initialize_candidate_drafts
            _initialize_candidate_drafts(connection, 1, candidate_path, questions)
            connection.execute(
                "UPDATE candidate_review_drafts SET deleted_at='2026-07-13T00:00:00+08:00',deletion_reason='unneeded',version=2 WHERE source_question_no<>'23'"
            )
            connection.commit()
        response = self.delete_post(number=23)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/reviews/1/deleted?deleted=1", response.headers["location"])

    def test_inline_stem_updates_only_target_and_returns_structured_json(self):
        candidate_path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        asset_path = self.private_root / "processing/import_job_1/question_crops/Q003.png"
        asset_hash = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO candidate_review_drafts "
                "(import_job_id,source_question_no,source_candidate_sha256,source_snapshot_json,edited_json,review_notes) "
                "VALUES (1,'3',?,'{}',?,?)",
                ("d" * 64, json.dumps({
                    "stem_markdown": "旧题干", "question_type_code": "single_choice",
                    "options": [{"code": "A", "content": "甲", "legacy": {"keep": True}}],
                    "subquestions": [], "unknown_root": {"keep": [1, 2]},
                }, ensure_ascii=False), "不可改备注"),
            )
            before = connection.execute(
                "SELECT source_snapshot_json,review_notes FROM candidate_review_drafts "
                "WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()

        response = self.inline_post(value="新题干 <script>alert(1)</script> $x^2$")
        self.assertEqual(200, response.status_code)
        self.assertEqual({
            "ok", "field", "index", "value", "version", "status", "status_name", "message"
        }, set(response.json()))
        self.assertEqual("stem_markdown", response.json()["field"])
        self.assertIsNone(response.json()["index"])
        self.assertEqual(2, response.json()["version"])
        self.assertEqual("draft", response.json()["status"])
        self.assertNotIn("<html", response.text.lower())
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT edited_json,source_snapshot_json,review_notes,status,version,reviewed_at "
                "FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        edited = json.loads(row[0])
        self.assertEqual("新题干 <script>alert(1)</script> $x^2$", edited["stem_markdown"])
        self.assertEqual([{"code": "A", "content": "甲", "legacy": {"keep": True}}], edited["options"])
        self.assertEqual({"keep": [1, 2]}, edited["unknown_root"])
        self.assertEqual(before, row[1:3])
        self.assertEqual(("draft", 2, None), row[3:])
        self.assertEqual(candidate_hash, hashlib.sha256(candidate_path.read_bytes()).hexdigest())
        self.assertEqual(asset_hash, hashlib.sha256(asset_path.read_bytes()).hexdigest())

    def test_inline_option_and_subquestion_update_by_controlled_index(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][2]["options"] = [
            {"code": "A", "content": "甲", "legacy": 1},
            {"code": "B", "content": "乙", "unknown": {"keep": True}},
            {"code": "C", "content": "丙"},
        ]
        candidate["questions"][20]["subquestions"] = [
            {"label": "（1）", "stem_markdown": "第一问", "score": 4},
            {"label": "（2）", "stem_markdown": "第二问", "rubric": {"keep": True}},
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

        option = self.inline_post(field="option_content", index=1, value="只改乙")
        self.assertEqual(200, option.status_code)
        self.assertEqual(1, option.json()["index"])
        subquestion = self.inline_post(
            number=21, field="subquestion_content", index=0, value="只改第一问"
        )
        self.assertEqual(200, subquestion.status_code)
        with sqlite3.connect(self.db_path) as connection:
            option_edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()[0])
            sub_edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='21'"
            ).fetchone()[0])
        self.assertEqual(["甲", "只改乙", "丙"], [item["content"] for item in option_edited["options"]])
        self.assertEqual({"keep": True}, option_edited["options"][1]["unknown"])
        self.assertEqual(["只改第一问", "第二问"], [item["stem_markdown"] for item in sub_edited["subquestions"]])
        self.assertEqual(4, sub_edited["subquestions"][0]["score"])
        self.assertEqual({"keep": True}, sub_edited["subquestions"][1]["rubric"])

    def test_inline_edit_resets_approved_and_enforces_lock_and_whitelist(self):
        approved = self.quick_post(number=3)
        self.assertEqual(303, approved.status_code)
        changed = self.inline_post(number=3, version="2", value="审核后修改")
        self.assertEqual(200, changed.status_code)
        self.assertEqual("draft", changed.json()["status"])
        self.assertIn("需要重新审核", changed.json()["message"])
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,version,reviewed_at FROM candidate_review_drafts "
                "WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        self.assertEqual(("draft", 3, None), row)

        conflict = self.inline_post(number=3, version="2", value="过期修改")
        self.assertEqual(409, conflict.status_code)
        self.assertIn("内容已被其他操作更新，请刷新", conflict.json()["error"])
        for kwargs in (
            {"extra": "unknown"},
            {"field": "answer_markdown"},
            {"field": "option_content"},
            {"field": "option_content", "index": "-1"},
            {"field": "option_content", "index": "999"},
            {"field": "stem_markdown", "index": "0"},
            {"field": "stem_markdown", "value": "甲" * 20001},
        ):
            with self.subTest(kwargs=kwargs):
                response = self.inline_post(**kwargs)
                self.assertIn(response.status_code, (400, 413))
        self.assertEqual(403, self.client.post(
            "/reviews/1/questions/3/inline-edit",
            data={"version": "1", "field": "stem_markdown", "value": "x"},
        ).status_code)
        self.assertEqual(404, self.inline_post(job_id=999).status_code)
        self.assertEqual(404, self.inline_post(number=999).status_code)

    def test_inline_database_failure_rolls_back_first_draft_initialization(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_inline_review_update BEFORE UPDATE ON candidate_review_drafts
                   BEGIN SELECT RAISE(ABORT, 'forced inline review failure'); END"""
            )
        response = self.inline_post(number=3, value="不得落库")
        self.assertEqual(500, response.status_code)
        self.assertEqual("保存失败，草稿未发生变化，请重试", response.json()["error"])
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0],
            )

    def test_inline_first_screen_is_compact_mapped_and_advanced_areas_are_closed(self):
        page = self.client.get("/reviews/1/questions/21")
        self.assertEqual(200, page.status_code)
        comparison = page.text.index('class="source-recognition-comparison"')
        quick = page.text.index('class="quick-review')
        guidance = page.text.index('class="review-guidance')
        advanced = page.text.index('class="advanced-review-editor"')
        evidence = page.text.index('class="review-evidence"')
        self.assertLess(comparison, quick)
        self.assertLess(quick, guidance)
        self.assertLess(guidance, advanced)
        self.assertLess(advanced, evidence)
        self.assertIn("发现文字或公式错误，直接点击右侧对应内容修改。", page.text)
        self.assertNotIn("跳到下方", page.text)
        self.assertNotIn("到下方修改", page.text)
        self.assertRegex(page.text, r'<details class="advanced-review-editor"(?![^>]*\sopen)')
        self.assertRegex(page.text, r'<details class="review-evidence"(?![^>]*\sopen)')
        self.assertIn("更多修改（题型、知识点、选项排序等）", page.text)
        self.assertIn('data-inline-field="stem_markdown"', page.text)
        self.assertIn('data-inline-field="subquestion_content" data-inline-index="0"', page.text)
        self.assertIn('data-open-advanced="edit-question-type"', page.text)
        self.assertIn('name="version" value="1" data-review-version', page.text)

    def test_text_and_image_options_have_inline_mapping_without_placeholder_body(self):
        text_page = self.client.get("/reviews/1/questions/3")
        for index in range(4):
            self.assertIn(
                f'data-inline-field="option_content" data-inline-index="{index}"',
                text_page.text,
            )

        candidate_path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["questions"][2]["options"] = [
            {"code": code, "content": "见原页选项图", "legacy": code}
            for code in "ABCD"
        ]
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        image_page = self.client.get("/reviews/1/questions/3")
        recognition = image_page.text.split('<article class="complete-question"', 1)[1].split("</article>", 1)[0]
        self.assertNotIn(">见原页选项图<", recognition)
        for index, code in enumerate("ABCD"):
            self.assertIn(f'>{code} 选项编辑</button>', recognition)
            self.assertIn(
                f'data-inline-field="option_content" data-inline-index="{index}"',
                recognition,
            )

    def test_inline_javascript_contract_is_in_place_and_avoids_unsafe_dom_apis(self):
        source = self.client.get("/static/review_workbench.js").text
        for contract in (
            'addEventListener("click"', 'addEventListener("keydown"',
            'event.key === "Enter"', 'event.key === " "', 'event.key === "Escape"',
            "event.ctrlKey", "event.metaKey", 'fetch(', "URLSearchParams",
            "textContent", "createElement", "typesetPromise", "disabled = true",
            "data-inline-field", "data-review-version", "replaceChildren",
        ):
            self.assertIn(contract, source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("contenteditable", source.lower())
        self.assertNotIn("scrollIntoView", source)

    def test_comparison_follows_navigation_before_quick_review_guidance_and_editor(self):
        page = self.client.get("/reviews/1/questions/1")
        navigation = page.text.index('class="workbench-qnav"')
        comparison = page.text.index('class="source-recognition-comparison"')
        quick = page.text.index('class="quick-review"')
        guidance = page.text.index("本题审核重点")
        source = page.text.index("原始资料与配图证据")
        editor = page.text.index('class="review-editor"')
        self.assertLess(navigation, comparison)
        self.assertLess(comparison, quick)
        self.assertLess(quick, guidance)
        self.assertLess(quick, source)
        self.assertLess(quick, editor)

        bar = page.text[quick:guidance]
        self.assertEqual(4, bar.count('method="post"'))
        self.assertEqual(3, bar.count('/quick-status"'))
        for action, label in (
            ("approve", "审核通过并进入下一题"),
            ("needs_fix", "标记需要修正"),
            ("needs_recrop", "标记需要重切"),
        ):
            self.assertIn(f'name="action" value="{action}"', bar)
            self.assertIn(label, bar)
        self.assertIn('action="/reviews/1/questions/1/delete"', bar)
        self.assertIn("删除本题", bar)
        self.assertLess(bar.index("标记需要重切"), bar.index("删除本题"))
        self.assertNotIn('href="#review-editor"', bar)
        self.assertNotIn("到下方修改", bar)
        self.assertIn("当前审核状态", bar)

        parser = _InteractionParser()
        parser.feed(page.text)
        self.assertEqual(0, parser.nested_forms)
        self.assertEqual(0, parser.buttons_inside_links)
        self.assertEqual(0, parser.links_inside_buttons)

    def test_quick_approve_preserves_content_and_moves_to_next_question(self):
        saved = self.post(number=1, review_notes="必须保留的备注")
        self.assertEqual(303, saved.status_code)
        candidate_path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        asset_paths = sorted((self.private_root / "processing/import_job_1").glob("**/*.png"))
        asset_hashes = {path.relative_to(self.private_root): hashlib.sha256(path.read_bytes()).hexdigest() for path in asset_paths}
        with sqlite3.connect(self.db_path) as connection:
            before = connection.execute(
                "SELECT edited_json,review_notes,source_snapshot_json,source_candidate_sha256 FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='1'"
            ).fetchone()

        response = self.quick_post(number=1, version="2")
        self.assertEqual(303, response.status_code)
        self.assertEqual("/reviews/1/questions/2?quick=approved_previous", response.headers["location"])
        with sqlite3.connect(self.db_path) as connection:
            after = connection.execute(
                "SELECT edited_json,review_notes,source_snapshot_json,source_candidate_sha256,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='1'"
            ).fetchone()
        self.assertEqual(before, after[:4])
        self.assertEqual("approved", after[4])
        self.assertEqual(3, after[5])
        self.assertIsNotNone(after[6])
        self.assertEqual(candidate_hash, hashlib.sha256(candidate_path.read_bytes()).hexdigest())
        self.assertEqual(asset_hashes, {path.relative_to(self.private_root): hashlib.sha256(path.read_bytes()).hexdigest() for path in asset_paths})

        next_page = self.client.get(response.headers["location"])
        self.assertIn("上一题已审核通过", next_page.text)
        self.assertIn("已通过 1", next_page.text)
        self.assertRegex(next_page.text, r'class="[^\"]*approved[^\"]*" href="/reviews/1/questions/1"')

    def test_quick_approve_last_question_stays_and_reports_completion(self):
        response = self.quick_post(number=23)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/reviews/1/questions/23?quick=approved_last", response.headers["location"])
        page = self.client.get(response.headers["location"])
        self.assertIn("本题已审核通过，已是最后一题", page.text)
        self.assertRegex(page.text, r"当前审核状态：\s*<span[^>]*>审核通过</span>")
        self.assertIn("已审核通过（再次确认并进入下一题）", page.text)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='23'"
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM candidate_review_drafts WHERE import_job_id=1").fetchone()[0]
        self.assertEqual(("approved", 2), row[:2])
        self.assertIsNotNone(row[2])
        self.assertEqual(23, count)

    def test_quick_relabels_stay_on_question_clear_reviewed_at_and_update_progress(self):
        approved = self.quick_post(number=3)
        self.assertEqual(303, approved.status_code)
        with sqlite3.connect(self.db_path) as connection:
            approved_row = connection.execute(
                "SELECT edited_json,review_notes,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        self.assertIsNotNone(approved_row[4])

        needs_fix = self.quick_post(number=3, action="needs_fix", version="2")
        self.assertEqual(303, needs_fix.status_code)
        self.assertEqual("/reviews/1/questions/3?quick=needs_fix", needs_fix.headers["location"])
        fix_page = self.client.get(needs_fix.headers["location"])
        self.assertIn("已标记为需要修正，可直接在识别结果中修改", fix_page.text)
        self.assertIn("需要修正 1", fix_page.text)
        self.assertRegex(fix_page.text, r'class="[^\"]*needs_fix[^\"]*active[^\"]*" href="/reviews/1/questions/3"')
        with sqlite3.connect(self.db_path) as connection:
            fix_row = connection.execute(
                "SELECT edited_json,review_notes,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        self.assertEqual(approved_row[:2], fix_row[:2])
        self.assertEqual(("needs_fix", 3, None), fix_row[2:])

        needs_recrop = self.quick_post(number=3, action="needs_recrop", version="3")
        self.assertEqual(303, needs_recrop.status_code)
        self.assertEqual("/reviews/1/questions/3?quick=needs_recrop", needs_recrop.headers["location"])
        recrop_page = self.client.get(needs_recrop.headers["location"])
        self.assertIn("已标记为需要重切，可继续检查裁图", recrop_page.text)
        self.assertIn("需要重切 1", recrop_page.text)
        with sqlite3.connect(self.db_path) as connection:
            recrop_row = connection.execute(
                "SELECT edited_json,review_notes,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        self.assertEqual(approved_row[:2], recrop_row[:2])
        self.assertEqual(("needs_recrop", 4, None), recrop_row[2:])

    def test_quick_approve_cannot_bypass_draft_and_asset_validation(self):
        job_dir = self.private_root / "processing/import_job_1"
        candidate_path = job_dir / "candidate_questions.json"
        original_candidate = candidate_path.read_bytes()

        candidate_cases = (
            (1, lambda question: question.update(stem_markdown="   "), "题干不能为空"),
            (1, lambda question: question.update(question_type_code="unknown"), "题型或知识点无效"),
            (1, lambda question: question.update(primary_knowledge_point_code="unknown"), "题型或知识点无效"),
            (1, lambda question: question.update(options=[{"code": "A", "content": "仅一个"}]), "至少需要两个选项"),
            (1, lambda question: question.update(options=[{"code": "A", "content": "甲"}, {"code": "A", "content": "乙"}]), "选项标识不能重复"),
        )
        for number, mutate, message in candidate_cases:
            with self.subTest(message=message):
                candidate = json.loads(original_candidate)
                mutate(candidate["questions"][number - 1])
                candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
                response = self.quick_post(number=number)
                self.assertEqual(400, response.status_code)
                self.assertIn(message, response.text)
                with sqlite3.connect(self.db_path) as connection:
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])
        candidate_path.write_bytes(original_candidate)

        candidate = json.loads(original_candidate)
        for option in candidate["questions"][2]["options"]:
            option["content"] = "见原页选项图"
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        crop_path = job_dir / "question_crops/Q003.png"
        crop_bytes = crop_path.read_bytes()
        crop_path.unlink()
        image_option = self.quick_post(number=3)
        self.assertEqual(400, image_option.status_code)
        self.assertIn("图像选项题缺少必要图片", image_option.text)
        crop_path.write_bytes(crop_bytes)
        candidate_path.write_bytes(original_candidate)

        figure_manifest_path = job_dir / "figure_assets.json"
        figure_manifest = json.loads(figure_manifest_path.read_text(encoding="utf-8"))
        figure_manifest["assets"] = [item for item in figure_manifest["assets"] if item["question_no"] != "16"]
        figure_manifest_path.write_text(json.dumps(figure_manifest), encoding="utf-8")
        required_figure = self.quick_post(number=16)
        self.assertEqual(400, required_figure.status_code)
        self.assertIn("需要图形题缺少必要配图", required_figure.text)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])

    def test_quick_status_security_errors_and_optimistic_lock_are_safe(self):
        path = "/reviews/1/questions/3/quick-status"
        self.assertEqual(405, self.client.get(path).status_code)
        self.assertEqual(403, self.client.post(path, data={}).status_code)
        self.assertEqual(413, self.quick_post(number=3, padding="甲" * 300000).status_code)
        self.assertEqual(400, self.quick_post(number=3, extra="unknown").status_code)
        invalid_action = self.quick_post(number=3, action="<script>alert(1)</script>")
        self.assertEqual(400, invalid_action.status_code)
        self.assertIn("无效的快速审核操作", invalid_action.text)
        self.assertNotIn("<script>alert(1)</script>", invalid_action.text)
        self.assertEqual(400, self.quick_post(number=3, version="not-a-version").status_code)

        csrf_token = self.csrf()
        missing_job = self.client.post(
            "/reviews/999/questions/3/quick-status",
            data={"csrf_token": csrf_token, "version": "1", "action": "approve"},
        )
        self.assertEqual(404, missing_job.status_code)
        self.assertIn("未找到导入任务", missing_job.text)
        missing_question = self.client.post(
            "/reviews/1/questions/999/quick-status",
            data={"csrf_token": csrf_token, "version": "1", "action": "approve"},
        )
        self.assertEqual(404, missing_question.status_code)
        self.assertIn("未找到候选题", missing_question.text)

        first = self.quick_post(number=3, action="needs_fix")
        self.assertEqual(303, first.status_code)
        with sqlite3.connect(self.db_path) as connection:
            before = connection.execute(
                "SELECT edited_json,review_notes,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        conflict = self.quick_post(number=3, action="needs_recrop", version="1")
        self.assertEqual(409, conflict.status_code)
        self.assertIn("请刷新后重试", conflict.text)
        with sqlite3.connect(self.db_path) as connection:
            after = connection.execute(
                "SELECT edited_json,review_notes,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()
        self.assertEqual(before, after)

    def test_quick_status_database_failure_rolls_back_all_draft_initialization(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_quick_review_update BEFORE UPDATE ON candidate_review_drafts
                   BEGIN SELECT RAISE(ABORT, 'forced quick review failure'); END"""
            )
        response = self.quick_post(number=3, action="needs_fix")
        self.assertEqual(500, response.status_code)
        self.assertIn("快速审核失败，草稿未发生变化", response.text)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])

    def test_comparison_contains_scan_and_recognition_before_review_form(self):
        page = self.client.get("/reviews/1/questions/1")
        comparison = page.text.index("原题与识别结果对照")
        original = page.text.index("原题图片")
        recognition = page.text.index("识别后题目")
        guidance = page.text.index("本题审核重点")
        source = page.text.index("原始资料与配图证据")
        review = page.text.index("完整审核与修改")
        form = page.text.index('class="review-editor"')
        self.assertLess(comparison, original)
        self.assertLess(original, recognition)
        self.assertLess(recognition, guidance)
        self.assertLess(guidance, review)
        self.assertLess(review, source)
        self.assertLess(comparison, form)

    def test_regular_choice_question_has_dynamic_basic_review_checklist(self):
        page = self.client.get("/reviews/1/questions/2")
        guide = page.text.split('<section class="review-guidance', 1)[1].split(
            '<div class="workbench-layout">', 1
        )[0]
        self.assertIn("基础检查", guide)
        for text in (
            "题干文字、数字、符号和 LaTeX 公式",
            "选项数量、顺序和内容",
            "题型和主、关联知识点",
            "来源页码",
        ):
            self.assertIn(text, guide)

    def test_fill_solution_and_figure_basic_checks_are_type_specific(self):
        fill = self.client.get("/reviews/1/questions/13").text.split(
            '<section class="review-guidance', 1
        )[1].split('<div class="workbench-layout">', 1)[0]
        self.assertIn("填空位置和题干完整性", fill)
        self.assertNotIn("选项数量、顺序和内容", fill)

        solution = self.client.get("/reviews/1/questions/21").text.split(
            '<section class="review-guidance', 1
        )[1].split('<div class="workbench-layout">', 1)[0]
        self.assertIn("小问数量、顺序、编号和公式", solution)

        figure = self.client.get("/reviews/1/questions/3").text.split(
            '<section class="review-guidance', 1
        )[1].split('<div class="workbench-layout">', 1)[0]
        self.assertIn("配图完整性及其与题图对应关系", figure)
        self.assertIn("题干与配图是否完整、方向和选项对应是否正确", figure)

    def test_question_without_specific_risk_still_explains_basic_review(self):
        page = self.client.get("/reviews/1/questions/2")
        guide = page.text.split('<section class="review-guidance', 1)[1].split(
            '<div class="workbench-layout">', 1
        )[0]
        self.assertIn("暂未发现明确风险，按基础检查确认即可", guide)
        self.assertIn("基础检查", guide)
        self.assertNotIn("无需审核", guide)

    def test_priority_questions_derive_actions_from_candidate_crop_and_audit_data(self):
        job_dir = self.private_root / "processing/import_job_1"
        candidate_path = job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        by_no = {item["source_question_no"]: item for item in candidate["questions"]}
        by_no["11"]["warnings"] = ["OCR 公式置信度低"]
        by_no["18"]["review_notes"] = ["题型和知识点可能不准确"]
        by_no["20"]["confidence"] = "medium"
        by_no["23"]["warnings"] = ["题干中的数字 3 可能是 8"]
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

        crop_path = job_dir / "question_crops.json"
        crops = json.loads(crop_path.read_text(encoding="utf-8"))
        crops["questions"][11]["warnings"] = ["裁切可能混入相邻题"]
        crop_path.write_text(json.dumps(crops, ensure_ascii=False), encoding="utf-8")

        audit_path = job_dir / "ai_audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit_by_no = {item["source_question_no"]: item for item in audit["questions"]}
        audit_by_no["22"]["issues"] = ["小问编号或顺序异常"]
        audit_by_no["22"]["suggested_corrections"] = ["检查小问数量、顺序和编号"]
        audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

        expected = {
            11: "对照原题检查文字、符号和 LaTeX 公式",
            12: "检查裁图是否缺边、混入相邻题或遗漏题图",
            18: "确认题型和主、关联知识点是否准确",
            20: "候选识别置信度为中",
            22: "检查小问数量、顺序和编号是否完整",
            23: "题干中的数字 3 可能是 8",
        }
        for number, action in expected.items():
            with self.subTest(number=number):
                page = self.client.get(f"/reviews/1/questions/{number}")
                guide = page.text.split('<section class="review-guidance', 1)[1].split(
                    '<div class="workbench-layout">', 1
                )[0]
                self.assertIn("本题审核重点", guide)
                self.assertIn(action, guide)

    def test_crop_audit_issue_and_correction_are_deduplicated_as_one_action(self):
        job_dir = self.private_root / "processing/import_job_1"
        crop_path = job_dir / "question_crops.json"
        crops = json.loads(crop_path.read_text(encoding="utf-8"))
        crops["questions"][9]["warnings"] = ["crop may include next question"]
        crop_path.write_text(json.dumps(crops), encoding="utf-8")

        audit_path = job_dir / "ai_audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        item = audit["questions"][9]
        item["issues"] = ["裁图混入相邻题"]
        item["suggested_corrections"] = ["请检查裁图是否混入相邻题"]
        audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

        page = self.client.get("/reviews/1/questions/10")
        guide = page.text.split('<section class="review-guidance', 1)[1].split(
            '<div class="workbench-layout">', 1
        )[0]
        action = "检查裁图是否缺边、混入相邻题或遗漏题图"
        self.assertEqual(1, guide.count(action))
        self.assertNotIn("crop may include next question", guide)

    def test_guidance_focus_marker_matches_top_question_navigation(self):
        page = self.client.get("/reviews/1/questions/11")
        self.assertIn('class="review-guidance focus"', page.text)
        self.assertRegex(
            page.text,
            r'class="[^"]*focus[^"]*active[^"]*" href="/reviews/1/questions/11"',
        )

    def test_guidance_escapes_hostile_html_and_limits_count_and_item_length(self):
        job_dir = self.private_root / "processing/import_job_1"
        candidate_path = job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        question = candidate["questions"][3]
        question["warnings"] = [
            "<script>alert('guide-xss')</script>" + ("甲" * 500)
        ] + [f"异常细节 {index}" for index in range(3)]
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

        audit_path = job_dir / "ai_audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["questions"][3]["issues"] = [
            "<img src=x onerror=alert('audit-guide')>"
        ] + [f"AI 异常证据 {index}" for index in range(20)]
        audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

        page = self.client.get("/reviews/1/questions/4")
        guide = page.text.split('<section class="review-guidance', 1)[1].split(
            '<div class="workbench-layout">', 1
        )[0]
        priority = guide.split('class="review-priority"', 1)[1].split("</div>", 1)[0]
        items = re.findall(r"<li>(.*?)</li>", priority, flags=re.S)
        self.assertLessEqual(len(items), 8)
        self.assertTrue(all(len(html.unescape(item)) <= 180 for item in items))
        self.assertNotIn("<script>alert('guide-xss')</script>", guide)
        self.assertNotIn("<img src=x onerror=alert('audit-guide')>", guide)
        self.assertIn("&lt;script&gt;alert", guide)
        self.assertIn("&lt;img src=x onerror=alert", guide)

    def test_missing_required_figure_is_called_out_from_manifest_state(self):
        path = self.private_root / "processing/import_job_1/figure_assets.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["assets"] = [item for item in payload["assets"] if item["question_no"] != "16"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        page = self.client.get("/reviews/1/questions/16")
        guide = page.text.split('<section class="review-guidance', 1)[1].split(
            '<div class="workbench-layout">', 1
        )[0]
        self.assertIn("必要配图缺失", guide)
        self.assertIn("题干与配图是否完整", guide)

    def test_guidance_survives_draft_save_and_review_status_change(self):
        saved = self.post(number=3)
        self.assertEqual(303, saved.status_code)
        saved_page = self.client.get(saved.headers["location"])
        self.assertIn("本题审核重点", saved_page.text)
        self.assertIn("基础检查", saved_page.text)

        changed = self.post(number=4, action="needs_fix")
        self.assertEqual(303, changed.status_code)
        changed_page = self.client.get(changed.headers["location"])
        self.assertIn("本题审核重点", changed_page.text)
        self.assertIn("需要修正", changed_page.text)

    def test_guidance_has_readable_desktop_mobile_and_subtle_focus_styles(self):
        css = self.client.get("/static/workbench.css").text
        for selector in (".review-guidance", ".review-priority", ".review-no-risk"):
            self.assertIn(selector, css)
        self.assertRegex(css, r"@media\s*\(max-width:760px\).*\.review-guidance")
        self.assertIn("var(--red)", css)

    def test_question_one_comparison_has_crop_saved_stem_options_and_exact_editor_targets(self):
        page = self.client.get("/reviews/1/questions/1")
        comparison = page.text.split('<section class="source-recognition-comparison"', 1)[1].split(
            '<section class="quick-review', 1
        )[0]
        self.assertIn("question_crops/Q001.png", comparison)
        self.assertIn("$x^2$", comparison)
        self.assertIn("&lt;script&gt;alert", comparison)
        positions = [comparison.index(f">{code}<") for code in "ABCD"]
        self.assertEqual(sorted(positions), positions)
        for text in ("单选题", "集合的含义与表示", "元素与集合的关系", "来源页码", "当前审核状态", "等待审核"):
            self.assertIn(text, comparison)
        self.assertIn('data-inline-field="stem_markdown"', comparison)
        for index in range(4):
            self.assertIn(
                f'data-inline-field="option_content" data-inline-index="{index}"',
                comparison,
            )
        self.assertIn('role="button"', comparison)
        self.assertIn('tabindex="0"', comparison)

    def test_question_three_has_distinct_scan_required_figure_and_four_option_edit_entries(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        for option in candidate["questions"][2]["options"]:
            option["content"] = "见原页选项图"
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

        page = self.client.get("/reviews/1/questions/3")
        comparison = page.text.split('<section class="source-recognition-comparison"', 1)[1].split(
            '<section class="quick-review', 1
        )[0]
        self.assertIn("question_crops/Q003.png", comparison)
        self.assertIn("question_003_figure_01.png", comparison)
        self.assertNotIn(">见原页选项图<", comparison)
        for index, code in enumerate("ABCD"):
            self.assertIn(
                f'data-inline-field="option_content" data-inline-index="{index}"',
                comparison,
            )
            self.assertIn(f'>{code} 选项编辑<', comparison)

    def test_question_thirteen_fill_blank_has_only_stem_edit_target(self):
        page = self.client.get("/reviews/1/questions/13")
        comparison = page.text.split('<section class="source-recognition-comparison"', 1)[1].split(
            '<section class="quick-review', 1
        )[0]
        self.assertIn("第 13 题题干 $x+13$", comparison)
        self.assertIn("填空题", comparison)
        self.assertIn('data-inline-field="stem_markdown"', comparison)
        self.assertNotIn('data-inline-field="option_content"', comparison)

    def test_question_sixteen_keeps_scan_left_and_independent_required_figure_right(self):
        page = self.client.get("/reviews/1/questions/16")
        comparison = page.text.split('<section class="source-recognition-comparison"', 1)[1].split(
            '<section class="quick-review', 1
        )[0]
        original = comparison.split('class="original-question-panel"', 1)[1].split(
            'class="complete-question"', 1
        )[0]
        recognition = comparison.split('class="complete-question"', 1)[1]
        self.assertIn("question_crops/Q016.png", original)
        self.assertNotIn("question_016_figure_01.png", original)
        self.assertIn("question_016_figure_01.png", recognition)
        self.assertNotIn("question_crops/Q016.png", recognition)

    def test_question_22_groups_nested_labels_and_keeps_three_flat_edit_targets(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][21]["subquestions"] = [
            {"label": "（1）", "stem_markdown": "第一问"},
            {"label": "（2）（i）", "stem_markdown": "第二问 i"},
            {"label": "（2）（ii）", "stem_markdown": "第二问 ii"},
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        page = self.client.get("/reviews/1/questions/22")
        complete = page.text.split('<article class="complete-question"', 1)[1].split(
            '</article>', 1
        )[0]

        self.assertEqual(2, complete.count('class="subquestion-main"'))
        self.assertIn('class="subquestion-children"', complete)
        self.assertIn("（1）", complete)
        self.assertIn("（2）", complete)
        self.assertIn("（i）", complete)
        self.assertIn("（ii）", complete)
        self.assertNotIn("第 1 小问", complete)
        self.assertNotIn("第 2 小问", complete)
        self.assertNotIn("第 3 小问", complete)
        for index, title in enumerate(("第（1）问", "第（2）问（i）", "第（2）问（ii）")):
            self.assertIn(
                f'aria-label="原位修改{title}" data-inline-field="subquestion_content" data-inline-index="{index}"',
                complete,
            )
            self.assertIn(f'<span class="subquestion-number">{title}</span>', page.text)
            self.assertEqual(1, page.text.count(f'id="edit-subquestion-{index}-content"'))

    def test_questions_21_and_23_keep_their_original_labels_without_regression(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][20]["subquestions"] = [
            {"label": label, "stem_markdown": label} for label in ("（1）", "（2）", "（3）")
        ]
        candidate["questions"][22]["subquestions"] = [
            {"label": label, "stem_markdown": label} for label in ("（1）", "（2）①", "（2）②")
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        q21 = self.client.get("/reviews/1/questions/21").text
        for label in ("（1）", "（2）", "（3）"):
            self.assertIn(label, q21)
        q23 = self.client.get("/reviews/1/questions/23").text
        for label in ("（1）", "（2）①", "（2）②"):
            self.assertIn(label, q23)
        self.assertIn("请核对小问层级标签", q23)

    def test_legacy_review_page_uses_the_same_nested_question_22_structure(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][21]["subquestions"] = [
            {"label": "（1）", "stem_markdown": "第一问"},
            {"label": "（2）（i）", "stem_markdown": "第二问 i"},
            {"label": "（2）（ii）", "stem_markdown": "第二问 ii"},
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        page = self.client.get("/review/1?status=all").text
        marker = 'id="question-22"'
        card = page.split(marker, 1)[1].split("</article>", 1)[0]
        self.assertEqual(2, card.count('class="subquestion-main"'))
        self.assertIn('class="subquestion-children"', card)
        self.assertNotIn("（2）（i）", card)

    def test_metadata_targets_and_all_server_targets_are_unique_existing_ids(self):
        for number in (1, 3, 13, 16, 21):
            page = self.client.get(f"/reviews/1/questions/{number}")
            fields = re.findall(r'data-inline-field="([^"]+)"', page.text)
            self.assertTrue(fields)
            self.assertTrue(set(fields) <= {"stem_markdown", "option_content", "subquestion_content"})
        page = self.client.get("/reviews/1/questions/1").text
        self.assertIn('data-open-advanced="edit-question-type"', page)
        for target in ("edit-question-type", "edit-primary-knowledge", "edit-related-knowledge"):
            self.assertEqual(1, page.count(f'id="{target}"'))

    def test_candidate_text_cannot_inject_target_ids_or_nested_interactions(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][0]["stem_markdown"] = '题干" data-edit-target="attacker-target'
        candidate["questions"][0]["options"][0].update({
            "code": 'A" id="attacker-id',
            "content": '</li><button data-edit-target="attacker-target">恶意</button>',
        })
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        page = self.client.get("/reviews/1/questions/1").text
        self.assertNotIn('id="attacker-id"', page)
        self.assertNotIn('data-edit-target="attacker-target"', page)
        self.assertIn("&lt;/li&gt;&lt;button", page)
        parser = _InteractionParser()
        parser.feed(page)
        self.assertEqual(0, parser.nested_forms)
        self.assertEqual(0, parser.buttons_inside_links)
        self.assertEqual(0, parser.links_inside_buttons)

    def test_lower_source_area_does_not_repeat_crop_but_keeps_page_and_figure_evidence(self):
        page = self.client.get("/reviews/1/questions/16").text
        lower = page.split('<details class="review-evidence">', 1)[1].split("</details>", 1)[0]
        self.assertIn("原始资料与配图证据", lower)
        self.assertIn("pages/page_001.png", lower)
        self.assertIn("question_016_figure_01.png", lower)
        self.assertNotIn("question_crops/Q016.png", lower)

    def test_missing_crop_has_friendly_message_and_original_page_link(self):
        crop = self.private_root / "processing/import_job_1/question_crops/Q001.png"
        crop.unlink()
        page = self.client.get("/reviews/1/questions/1").text
        self.assertIn("暂未找到本题单题裁图", page)
        self.assertIn("打开原始第 1 页", page)
        self.assertIn("识别后题目", page)
        self.assertIn('name="stem_markdown"', page)

    def test_entry_navigation_initializes_idempotently_and_marks_focus(self):
        papers = self.client.get("/papers")
        self.assertIn('href="/reviews/1/questions/1"', papers.text)
        first = self.client.get("/reviews/1/questions/3")
        second = self.client.get("/reviews/1/questions/3")
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        for text in ("题目审核工作台", "上一题", "下一题", "23", "重点复核", "保存草稿", "需要修正", "需要重切"):
            self.assertIn(text, first.text)
        self.assertIn('/reviews/1/questions/2', first.text)
        self.assertIn('/reviews/1/questions/4', first.text)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])
        self.post()
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(23, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])

    def test_save_refresh_preserves_source_and_structure(self):
        source = self.private_root / "processing/import_job_1/candidate_questions.json"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        response = self.post()
        self.assertEqual(303, response.status_code)
        page = self.client.get(response.headers["location"])
        complete = page.text.split('<article class="complete-question"', 1)[1].split(
            '</article>', 1
        )[0]
        self.assertIn("更新后题干", complete)
        self.assertIn(">A<", complete)
        self.assertIn(">B<", complete)
        self.assertIn("保存成功", page.text)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT edited_json,status,version FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'").fetchone()
        edited = json.loads(row[0])
        self.assertEqual(["A", "B"], [x["code"] for x in edited["options"]])
        self.assertEqual(["01.01.02"], edited["related_knowledge_point_codes"])
        self.assertEqual(("draft", 2), row[1:])

    def test_choice_options_are_visual_not_json_and_support_crud_order(self):
        page = self.client.get("/reviews/1/questions/3")
        self.assertNotIn('name="options_json"', page.text)
        self.assertNotIn('name="subquestions_json"', page.text)
        self.assertIn('name="option_code" value="A"', page.text)
        self.assertIn('name="option_code" value="B"', page.text)
        self.assertIn('name="option_content"', page.text)
        self.assertIn("添加选项", page.text)
        self.assertIn("删除", page.text)
        response = self.post(
            option_source_index=["1", "", "0"],
            option_code=["B", "C", "A"],
            option_content=["乙（修改）", "新增", "甲"],
            option_order=["1", "2", "3"],
        )
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()[0])
        self.assertEqual(["B", "C", "A"], [item["code"] for item in edited["options"]])
        self.assertEqual("乙（修改）", edited["options"][0]["content"])
        reloaded = self.client.get(response.headers["location"]).text
        comparison = reloaded.split('<article class="complete-question"', 1)[1].split(
            "</article>", 1
        )[0]
        target_positions = [
            comparison.index(f'data-inline-field="option_content" data-inline-index="{index}"')
            for index in range(3)
        ]
        self.assertEqual(sorted(target_positions), target_positions)
        for index, (code, content) in enumerate((("B", "乙（修改）"), ("C", "新增"), ("A", "甲"))):
            self.assertRegex(
                comparison,
                rf'<b>{code}</b><span[^>]*data-inline-field="option_content" data-inline-index="{index}"',
            )
            self.assertIn(
                f'id="edit-option-{index}-content" name="option_content" maxlength="10000">{content}</textarea>',
                reloaded,
            )

    def test_option_merge_preserves_unknown_fields_and_image_placeholders(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        question = candidate["questions"][2]
        question["options"][0]["asset_ref"] = {"kind": "crop", "id": 7}
        for item in question["options"]:
            item["content"] = "见原页选项图"
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        response = self.post(
            option_source_index=["0", "1", "2", "3"],
            option_code=["A", "B", "C", "D"],
            option_content=["见原页选项图"] * 4,
            option_order=["1", "2", "3", "4"],
        )
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()[0])
        self.assertEqual({"kind": "crop", "id": 7}, edited["options"][0]["asset_ref"])
        self.assertTrue(all(item["content"] == "见原页选项图" for item in edited["options"]))
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_option_validation_rejects_bad_structured_arrays(self):
        self.assertEqual(400, self.post(option_code=["A"], option_content=["甲", "乙"]).status_code)
        self.assertEqual(400, self.post(option_code=["A", "A"]).status_code)
        self.assertEqual(400, self.post(option_code=["A", "<script>"]).status_code)
        self.assertEqual(400, self.post(option_content=["甲" * 10001, "乙"]).status_code)
        self.assertEqual(400, self.post(option_order=["1", "3"]).status_code)
        self.assertEqual(400, self.post(option_source_index=["0", "0"]).status_code)

    def test_fill_blank_hides_options_rejects_additions_and_preserves_anomalies(self):
        normal = self.client.get("/reviews/1/questions/13")
        self.assertIn('class="structured-editor option-editor" data-structure="options" hidden', normal.text)
        self.assertEqual(400, self.post(
            number=13, option_source_index=[""], option_code=["A"],
            option_content=["不应新增"], option_order=["1"],
        ).status_code)

        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][13]["options"] = [
            {"code": "A", "content": "异常遗留", "legacy": {"keep": True}}
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        warning = self.client.get("/reviews/1/questions/14")
        self.assertIn("异常选项将保留在草稿", warning.text)
        self.assertIn('data-structure="options" hidden', warning.text)
        saved = self.post(number=14)
        self.assertEqual(303, saved.status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='14'"
            ).fetchone()[0])
        self.assertEqual({"code": "A", "content": "异常遗留", "legacy": {"keep": True}}, edited["options"][0])

    def test_solution_subquestions_visual_crud_order_and_unknown_fields(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][20]["subquestions"] = [
            {"label": "（1）", "stem_markdown": "第一问", "score_hint": 4},
            {"label": "（2）（i）", "stem_markdown": "第二问", "rubric": {"level": 2}},
            {"label": "（2）（ii）", "stem_markdown": "第三问"},
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        page = self.client.get("/reviews/1/questions/21")
        self.assertIn('data-structure="subquestions"', page.text)
        self.assertIn('name="subquestion_content"', page.text)
        self.assertIn("第一问", page.text)
        self.assertIn("添加小问", page.text)
        response = self.post(
            number=21,
            subquestion_source_index=["1", "", "0"],
            subquestion_content=["第二问修改", "新增小问", "第一问"],
            subquestion_order=["1", "2", "3"],
        )
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='21'"
            ).fetchone()[0])
        self.assertEqual(["第二问修改", "新增小问", "第一问"], [x["stem_markdown"] for x in edited["subquestions"]])
        self.assertEqual({"level": 2}, edited["subquestions"][0]["rubric"])
        self.assertEqual("（2）（i）", edited["subquestions"][0]["label"])
        self.assertNotIn("label", edited["subquestions"][1])
        self.assertEqual(4, edited["subquestions"][2]["score_hint"])

    def test_question_22_inline_and_full_save_preserve_labels_and_target_only_flat_index(self):
        path = self.private_root / "processing/import_job_1/candidate_questions.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate["questions"][21]["subquestions"] = [
            {"label": "（1）", "stem_markdown": "第一问"},
            {"label": "（2）（i）", "stem_markdown": "第二问 i"},
            {"label": "（2）（ii）", "stem_markdown": "第二问 ii"},
        ]
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        inline = self.inline_post(
            number=22, field="subquestion_content", index=1, value="只修改（2）（i）"
        )
        self.assertEqual(200, inline.status_code)
        with sqlite3.connect(self.db_path) as connection:
            after_inline = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='22'"
            ).fetchone()[0])
        self.assertEqual(["（1）", "（2）（i）", "（2）（ii）"], [item["label"] for item in after_inline["subquestions"]])
        self.assertEqual("只修改（2）（i）", after_inline["subquestions"][1]["stem_markdown"])

        saved = self.post(
            number=22, version="2",
            subquestion_source_index=["0", "1", "2"],
            subquestion_content=["第一问保存", "第二问 i 保存", "第二问 ii 保存"],
            subquestion_order=["1", "2", "3"],
        )
        self.assertEqual(303, saved.status_code)
        with sqlite3.connect(self.db_path) as connection:
            after_save = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='22'"
            ).fetchone()[0])
        self.assertEqual(["（1）", "（2）（i）", "（2）（ii）"], [item["label"] for item in after_save["subquestions"]])
        self.assertEqual(before_hash, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_solution_allows_zero_and_validates_subquestion_arrays(self):
        self.assertEqual(303, self.post(
            number=21, subquestions_present="1",
            subquestion_source_index=[], subquestion_content=[], subquestion_order=[]
        ).status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='21'"
            ).fetchone()[0])
        self.assertEqual([], edited["subquestions"])
        self.assertEqual(400, self.post(number=22, subquestion_content=["一", "二"]).status_code)
        self.assertEqual(400, self.post(number=22, subquestion_order=["2"]).status_code)
        self.assertEqual(400, self.post(number=22, subquestion_content=["甲" * 10001]).status_code)

    def test_workbench_javascript_and_safe_preview_contract(self):
        page = self.client.get("/reviews/1/questions/3")
        self.assertIn('src="/static/review_workbench.js?v=20260713-inline-edit"', page.text)
        self.assertIn('class="edited-preview"', page.text)
        self.assertIn("已保存草稿", page.text)
        self.assertIn("修改后题目预览", page.text)
        asset = self.client.get("/static/review_workbench.js")
        self.assertEqual(200, asset.status_code)
        source = asset.text
        for contract in (
            "textContent", "setTimeout", "typesetPromise", "data-add-option",
            "data-add-subquestion", 'addEventListener("click"',
            'addEventListener("keydown"', 'event.key === "Enter"',
            'event.key === " "', "focus", "getElementById",
            "data-inline-field", "data-review-version", "closest",
        ):
            self.assertIn(contract, source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)
        self.assertNotIn("scrollIntoView", source)
        self.assertNotIn('`第 ${index + 1} 小问`', source)
        self.assertIn("groupSubquestions", source)
        self.assertIn("data-original-label", self.client.get("/reviews/1/questions/22").text)
        self.assertIn("addEventListener", source)

    def test_comparison_css_is_two_columns_then_original_first_with_compact_inline_editing(self):
        css = self.client.get("/static/workbench.css").text
        self.assertIn(".source-recognition-comparison", css)
        self.assertRegex(css, r"\.comparison-grid\{[^}]*grid-template-columns:[^}]*")
        self.assertIn(".original-question-panel", css)
        self.assertIn(".inline-editable", css)
        self.assertIn(".inline-editor", css)
        self.assertIn(".advanced-review-editor", css)
        self.assertIn(".review-evidence", css)
        self.assertIn("width:100%", css)
        self.assertIn("min-height:44px", css)
        self.assertRegex(
            css,
            r"@media\s*\(max-width:760px\).*\.comparison-grid\{grid-template-columns:1fr\}",
        )

    def test_structured_security_limits_unknown_fields_and_type_rules(self):
        self.assertEqual(400, self.post(options_json="[]").status_code)
        self.assertEqual(400, self.post(subquestions_json="[]").status_code)
        self.assertEqual(413, self.post(review_notes="甲" * 300000).status_code)
        self.assertEqual(400, self.post(
            question_type_code="multiple_choice", option_source_index=["0"],
            option_code=["A"], option_content=["一个"], option_order=["1"],
        ).status_code)
        switched = self.post(
            question_type_code="fill_blank",
            option_source_index=["0", "1", "2", "3"],
            option_code=["A", "B", "C", "D"],
            option_content=["甲", "乙", "丙", "丁"], option_order=["1", "2", "3", "4"],
        )
        self.assertEqual(303, switched.status_code)
        with sqlite3.connect(self.db_path) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'"
            ).fetchone()[0])
        self.assertEqual("fill_blank", edited["question_type_code"])
        self.assertEqual(["A", "B", "C", "D"], [item["code"] for item in edited["options"]])

    def test_security_validation_status_and_optimistic_lock(self):
        self.assertEqual(403, self.client.post("/reviews/1/questions/3", data={}).status_code)
        self.assertEqual(404, self.client.get("/reviews/999/questions/3").status_code)
        self.assertEqual(404, self.client.get("/reviews/1/questions/999").status_code)
        self.assertEqual(400, self.post(extra="unknown").status_code)
        self.assertEqual(400, self.post(action="approve", stem_markdown="").status_code)
        self.assertEqual(400, self.post(action="approve", primary_knowledge_point_code="unknown").status_code)
        ok = self.post(action="needs_fix")
        self.assertEqual(303, ok.status_code)
        conflict = self.post(version="1", action="needs_recrop")
        self.assertEqual(409, conflict.status_code)
        self.assertIn("已被其他操作更新", conflict.text)

    def test_progress_assets_and_xss_escaping(self):
        self.post(action="needs_fix")
        page = self.client.get("/reviews/1/questions/3")
        self.assertIn("需要修正 1", page.text)
        self.assertIn("question_crops/Q003.png", page.text)
        self.assertIn("question_003_figure_01.png", page.text)
        xss = self.client.get("/reviews/1/questions/1")
        self.assertNotIn("<script>alert('xss')</script>", xss.text)
        self.assertIn("&lt;script&gt;alert", xss.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", xss.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", xss.text)
        self.assertIn("window.MathJax", xss.text)
        self.assertIn("tex-mml-chtml.js", xss.text)

    def test_complete_display_titles_safe_asset_routes_and_mobile_css_exist(self):
        page = self.client.get("/reviews/1/questions/16")
        for title in ("原题与识别结果对照", "原题图片", "识别后题目", "原始资料与配图证据", "审核与修改"):
            self.assertIn(title, page.text)
        self.assertIn('src="/private-pages/1/assets/question_016_figure_01.png"', page.text)
        self.assertEqual(200, self.client.get("/private-pages/1/assets/question_016_figure_01.png").status_code)
        self.assertIn(self.client.get("/private-pages/1/../candidate_questions.json").status_code, (400, 404, 422))
        css = self.client.get("/static/workbench.css").text
        self.assertIn(".complete-question", css)
        self.assertIn(".complete-question-body", css)
        self.assertRegex(css, r"@media\s*\(max-width:760px\).*\.complete-question")

    def test_approve_records_time_and_invalid_write_rolls_back(self):
        approved = self.post(action="approve")
        self.assertEqual(303, approved.status_code)
        with sqlite3.connect(self.db_path) as connection:
            before = connection.execute("SELECT edited_json,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'").fetchone()
        self.assertEqual("approved", before[1])
        self.assertIsNotNone(before[3])
        rejected = self.post(version=str(before[2]), action="approve", option_code=["A"])
        self.assertEqual(400, rejected.status_code)
        with sqlite3.connect(self.db_path) as connection:
            after = connection.execute("SELECT edited_json,status,version,reviewed_at FROM candidate_review_drafts WHERE import_job_id=1 AND source_question_no='3'").fetchone()
        self.assertEqual(before, after)

    def test_database_failure_rolls_back_draft_initialization(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_review_update BEFORE UPDATE ON candidate_review_drafts
                   BEGIN SELECT RAISE(ABORT, 'forced review write failure'); END"""
            )
        response = self.post()
        self.assertEqual(500, response.status_code)
        self.assertIn("保存失败，草稿未发生变化", response.text)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])
