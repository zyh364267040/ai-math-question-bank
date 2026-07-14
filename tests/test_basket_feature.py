import hashlib
import json
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.importing.admit_questions import admit_questions
from src.web.app import create_app
from tests.fixture_factory import create_import_job_fixture


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.forms = []; self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.stack.append({"action": attrs.get("action"), "inputs": {}})
        elif tag == "input" and self.stack and attrs.get("name"):
            self.stack[-1]["inputs"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.stack:
            self.forms.append(self.stack.pop())


class InteractionParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.interactive = []; self.nested = []

    def handle_starttag(self, tag, attrs):
        if tag in ("a", "button", "form"):
            if self.interactive and (tag == "form" or self.interactive[-1] in ("a", "button")):
                self.nested.append((self.interactive[-1], tag))
            self.interactive.append(tag)

    def handle_endtag(self, tag):
        if tag in ("a", "button", "form") and self.interactive:
            if tag in self.interactive:
                self.interactive = self.interactive[:len(self.interactive) - 1 - self.interactive[::-1].index(tag)]


class BasketFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.private = root / "private"
        create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            paper = con.execute("""INSERT INTO source_papers
              (sha256,file_size,original_filename,stored_path,region_code,exam_year,exam_type_code,paper_name)
              VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf','TJ',2025,'YK','<b>来源</b>')""", ("b"*64,)).lastrowid
            con.execute("INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) VALUES(1,?,1,4,'needs_review')", (paper,))
        admit_questions(self.db, self.private, 1)
        self.client = TestClient(create_app(self.db, self.private))

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def codes(self):
        with sqlite3.connect(self.db) as con:
            return [r[0] for r in con.execute("SELECT question_code FROM questions ORDER BY id LIMIT 3")]

    def code_for_source_number(self, number):
        with sqlite3.connect(self.db) as con:
            return con.execute(
                """SELECT q.question_code FROM questions q
                   JOIN question_sources qs ON qs.question_id=q.id
                   WHERE qs.source_question_no=?""", (str(number),)
            ).fetchone()[0]

    def csrf(self, path="/questions"):
        parser = FormParser(); parser.feed(self.client.get(path).text)
        token = next(f["inputs"]["csrf_token"] for f in parser.forms if "csrf_token" in f["inputs"])
        return token

    def post(self, path, data=None):
        values = dict(data or {}); values["csrf_token"] = self.csrf()
        return self.client.post(path, data=values, follow_redirects=False)

    def ajax(self, path, data=None, csrf=True):
        values = dict(data or {})
        if csrf: values["csrf_token"] = self.csrf()
        return self.client.post(path, data=values, headers={"Accept": "application/json"}, follow_redirects=False)

    def preview(self, data=None, csrf=True):
        return self.ajax("/basket/preview", data, csrf)

    def test_ajax_add_remove_json_count_and_idempotence(self):
        code = self.codes()[0]
        added = self.ajax(f"/basket/add/{code}")
        self.assertEqual(200, added.status_code)
        self.assertEqual({"ok": True, "in_basket": True, "basket_count": 1, "label": "移出选题篮"}, added.json())
        repeated = self.ajax(f"/basket/add/{code}")
        self.assertEqual(200, repeated.status_code)
        self.assertEqual(1, repeated.json()["basket_count"])
        self.assertTrue(repeated.json()["in_basket"])
        removed = self.ajax(f"/basket/remove/{code}")
        self.assertEqual(200, removed.status_code)
        self.assertEqual({"ok": True, "in_basket": False, "basket_count": 0, "label": "加入选题篮"}, removed.json())
        repeated = self.ajax(f"/basket/remove/{code}")
        self.assertEqual(0, repeated.json()["basket_count"])
        self.assertFalse(repeated.json()["in_basket"])

    def test_non_ajax_next_returns_origin_and_preserves_valid_filters(self):
        code = self.codes()[0]
        target = "/questions?question_type=single_choice&has_figure=false&source=%E5%A4%A9%E6%B4%A5"
        response = self.post(f"/basket/add/{code}", {"next": target})
        self.assertEqual(303, response.status_code)
        self.assertEqual(target, response.headers["location"])
        detail = f"/questions/{code}"
        response = self.post(f"/basket/remove/{code}", {"next": detail})
        self.assertEqual(detail, response.headers["location"])

    def test_next_rejects_open_redirects_and_uses_safe_default(self):
        code = self.codes()[0]
        malicious = ("//evil.example/x", "https://evil.example/x", "/papers", "/questions/other", "/questions%0d%0aLocation:%20//evil")
        for target in malicious:
            response = self.post(f"/basket/add/{code}", {"next": target})
            self.assertEqual("/questions", response.headers["location"], target)
        response = self.post(f"/basket/add/{code}", {"next": "/basket"})
        self.assertEqual("/basket", response.headers["location"])

    def test_ajax_csrf_q12_and_invalid_code_errors(self):
        code = self.codes()[0]
        response = self.ajax(f"/basket/add/{code}", csrf=False)
        self.assertEqual(403, response.status_code)
        self.assertEqual(False, response.json()["ok"])
        for invalid in ("Q12", "not-a-code"):
            response = self.ajax(f"/basket/add/{invalid}")
            self.assertEqual(404, response.status_code)
            self.assertEqual(False, response.json()["ok"])

    def test_pages_load_basket_script_and_have_non_nested_interactions(self):
        code = self.codes()[0]
        for path in ("/questions", f"/questions/{code}"):
            page = self.client.get(path)
            self.assertIn('src="/static/basket.js"', page.text)
            parser = InteractionParser(); parser.feed(page.text)
            self.assertEqual([], parser.nested)
            self.assertIn('name="next"', page.text)
        script = self.client.get("/static/basket.js")
        self.assertEqual(200, script.status_code)
        self.assertIn("fetch(", script.text)

    def test_schema_default_uniqueness_cascade_and_migration_idempotence(self):
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM baskets WHERE basket_key='default'").fetchone()[0])
            basket = con.execute("SELECT id FROM baskets WHERE basket_key='default'").fetchone()[0]
            q = con.execute("SELECT id FROM questions LIMIT 1").fetchone()[0]
            con.execute("INSERT INTO basket_items(basket_id,question_id,position) VALUES(?,?,1)", (basket,q))
            with self.assertRaises(sqlite3.IntegrityError): con.execute("INSERT INTO basket_items(basket_id,question_id,position) VALUES(?,?,2)", (basket,q))
            con.execute("DELETE FROM questions WHERE id=?", (q,))
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0])

    def test_post_only_csrf_idempotent_add_order_move_remove_and_clear(self):
        a,b,c = self.codes()
        self.assertEqual(405, self.client.get(f"/basket/add/{a}").status_code)
        self.assertEqual(403, self.client.post(f"/basket/add/{a}").status_code)
        self.assertEqual(403, self.client.post(f"/basket/add/{a}", data={"csrf_token":"wrong"}).status_code)
        for code in (a,b,c,a): self.assertEqual(303, self.post(f"/basket/add/{code}").status_code)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(3, con.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0])
        self.post(f"/basket/move-up/{a}"); self.post(f"/basket/move-down/{c}")
        self.post(f"/basket/move-up/{c}")
        page = self.client.get("/basket").text
        self.assertLess(page.index(c), page.index(b))
        self.post(f"/basket/remove/{b}"); self.post(f"/basket/remove/{b}")
        self.post("/basket/clear")
        self.assertIn("选题篮还是空的", self.client.get("/basket").text)

    def test_navigation_listing_detail_count_join_state_and_invalid_code(self):
        code = self.codes()[0]
        self.assertEqual(404, self.post("/basket/add/Q-bbbbbbbbbbbbbbbb-012").status_code)
        self.assertEqual(404, self.post("/basket/add/not-a-code").status_code)
        self.post(f"/basket/add/{code}")
        page = self.client.get("/questions").text
        self.assertIn("选题篮（1）", page); self.assertIn("已加入", page)
        self.assertNotIn("<a class=\"question-list-item\"", page)
        self.assertIn("&lt;b&gt;来源&lt;/b&gt;", self.client.get("/basket").text)
        detail = self.client.get(f"/questions/{code}").text
        self.assertIn(f'action="/basket/remove/{code}"', detail)

    def test_export_options_numbering_missing_answer_images_history_and_download(self):
        codes = [self.code_for_source_number(number) for number in (1, 3, 16)]
        for code in codes: self.post(f"/basket/add/{code}")
        response = self.post("/basket/export", {"include_source":"on", "include_knowledge":"on", "include_answers":"on", "include_analysis":"on"})
        self.assertEqual(303, response.status_code)
        success = self.client.get(response.headers["location"])
        self.assertEqual(200, success.status_code)
        with sqlite3.connect(self.db) as con:
            row = con.execute("SELECT id,output_path,sha256,options_json FROM basket_exports ORDER BY id DESC LIMIT 1").fetchone()
        md_path = self.private / row[1]; text = md_path.read_text(encoding="utf-8")
        self.assertEqual(["## 1.", "## 2.", "## 3."], [line for line in text.splitlines() if line.startswith("## ")])
        self.assertIn("A.", text); self.assertIn("原卷未提供答案", text); self.assertIn("原卷未提供解析", text)
        self.assertIn("来源：", text); self.assertIn("知识点：", text); self.assertIn("assets/", text)
        references = __import__('re').findall(r'!\[[^]]*\]\((assets/[^)]+)\)', text)
        self.assertEqual(2, len(references))
        self.assertEqual(len(references), len(set(references)))
        for reference in references:
            self.assertTrue((md_path.parent / reference).is_file(), reference)
        copied = sorted(path.name for path in (md_path.parent / "assets").iterdir())
        self.assertEqual(sorted(Path(reference).name for reference in references), copied)
        self.assertEqual(
            ["002_01_complete_question.png", "003_01_question_figure.png"], copied
        )
        self.assertEqual(row[2], hashlib.sha256(md_path.read_bytes()).hexdigest())
        self.assertNotIn("include_images", json.loads(row[3]))
        self.assertEqual(200, self.client.get(f"/basket/exports/{row[0]}/download").status_code)
        self.assertEqual(404, self.client.get("/basket/exports/99999/download").status_code)
        second = self.post("/basket/export", {"include_images":"off"})
        self.assertEqual(303, second.status_code)
        self.assertNotEqual(response.headers["location"], second.headers["location"])

    def test_export_text_question_ignores_registered_complete_question_and_has_no_assets_directory(self):
        code = self.code_for_source_number(1)
        self.post(f"/basket/add/{code}")
        response = self.post("/basket/export")
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db) as con:
            output_path = con.execute(
                "SELECT output_path FROM basket_exports ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        export_dir = (self.private / output_path).parent
        self.assertNotIn("assets/", (export_dir / "练习.md").read_text(encoding="utf-8"))
        self.assertFalse((export_dir / "assets").exists())

    def test_question_22_basket_preview_and_markdown_keep_nested_label_structure(self):
        code = self.code_for_source_number(22)
        self.post(f"/basket/add/{code}")

        basket = self.client.get("/basket").text
        preview = self.preview({}).json()["html"]
        for html in (basket, preview):
            self.assertEqual(2, html.count('class="subquestion-main"'))
            self.assertIn('class="subquestion-children"', html)
            self.assertNotIn("第2小问", html)
            self.assertNotIn("第3小问", html)
            self.assertNotIn("（3）", html)
            self.assertNotIn("（2）（i）", html)

        response = self.post("/basket/export")
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db) as con:
            output_path = con.execute(
                "SELECT output_path FROM basket_exports ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        markdown = (self.private / output_path).read_text(encoding="utf-8")
        self.assertIn("\n（1）若曲线", markdown)
        self.assertIn("\n（2）设函数 $f(x)$ 满足以下公共合成条件：", markdown)
        self.assertIn("\n　　（i）若函数", markdown)
        self.assertIn("\n　　（ii）证明两个合成零点", markdown)
        self.assertNotIn("（3）", markdown)
        self.assertNotIn("（2）（i）", markdown)

    def test_plain_question_21_still_has_three_main_questions(self):
        code = self.code_for_source_number(21)
        self.post(f"/basket/add/{code}")
        preview = self.preview({}).json()["html"]
        self.assertEqual(3, preview.count('class="subquestion-main"'))
        self.assertNotIn('class="subquestion-children"', preview)
        for label in ("（1）", "（2）", "（3）"):
            self.assertIn(label, preview)

    def test_empty_export_and_rollback_on_invalid_manifest(self):
        self.assertEqual(400, self.post("/basket/export", {"include_images":"on"}).status_code)
        code = self.code_for_source_number(3)
        self.post(f"/basket/add/{code}")
        manifest = self.private / "processing/import_job_1/question_crops.json"
        payload = json.loads(manifest.read_text()); payload["questions"][2]["sha256"] = "0"*64
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(500, self.post("/basket/export", {"include_images":"on"}).status_code)
        exports = self.private / "exports"
        self.assertFalse(exports.exists() and any(exports.iterdir()))

    def test_preview_defaults_all_switches_and_missing_messages(self):
        code = self.code_for_source_number(1)
        self.post(f"/basket/add/{code}")
        default = self.preview({})
        self.assertEqual(200, default.status_code)
        self.assertTrue(default.json()["ok"])
        html = default.json()["html"]
        self.assertIn('练习预览', html)
        self.assertIn('class="preview-number">1', html)
        self.assertNotIn('/question-assets/', html)
        self.assertNotIn('预览来源：', html)
        self.assertNotIn('预览知识点：', html)
        self.assertNotIn('原卷未提供答案', html)
        self.assertNotIn('原卷未提供解析', html)

        everything = self.preview({
            "include_source": "on", "include_knowledge": "on",
            "include_answers": "on", "include_analysis": "on",
        })
        html = everything.json()["html"]
        self.assertIn('预览来源：', html)
        self.assertIn('预览知识点：', html)
        self.assertIn('原卷未提供答案', html)
        self.assertIn('原卷未提供解析', html)
        markers = {
            "include_source": "预览来源：",
            "include_knowledge": "预览知识点：",
            "include_answers": "原卷未提供答案",
            "include_analysis": "原卷未提供解析",
        }
        for option, marker in markers.items():
            isolated = self.preview({option: "on"}).json()["html"]
            self.assertIn(marker, isolated, option)
            for other_option, other_marker in markers.items():
                if other_option != option:
                    self.assertNotIn(other_marker, isolated, f"{option} unexpectedly enabled {other_option}")

    def test_preview_legacy_or_malicious_image_field_is_ignored_and_cannot_hide_images(self):
        code = self.code_for_source_number(16)
        self.post(f"/basket/add/{code}")
        for value in ("on", "off", "yes", "../assets/evil.png", "<script>"):
            response = self.preview({"include_images": value})
            self.assertEqual(200, response.status_code, value)
            self.assertIn('/question-assets/', response.json()["html"], value)

    def test_synthetic_fixture_asset_semantics_for_questions_1_3_and_16(self):
        expected = {
            1: (0, None),
            3: (1, "question_crops/"),
            16: (1, "question_016_figure_01.png"),
        }
        for number, (image_count, selected_path) in expected.items():
            self.post(f"/basket/add/{self.code_for_source_number(number)}")
            html = self.preview({}).json()["html"]
            self.assertEqual(image_count, html.count('class="preview-image"'), number)
            if selected_path:
                self.assertIn(selected_path, html, number)
            if number == 3:
                self.assertNotIn("见原页选项图", html)
                self.assertNotIn("question_003_figure_01.png", html)
            if number == 16:
                self.assertNotIn("question_crops/", html)
            self.post("/basket/clear")

    def test_preview_order_renumber_escape_csrf_empty_and_no_export_side_effects(self):
        a, b, c = self.codes()
        for code in (a, b, c): self.post(f"/basket/add/{code}")
        self.post(f"/basket/move-up/{c}")
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE questions SET stem_markdown=? WHERE question_code=?", ('<script>alert(1)</script> $x^2$', c))
            before = con.execute("SELECT COUNT(*) FROM basket_exports").fetchone()[0]
        response = self.preview({"include_images": "on"})
        self.assertEqual(200, response.status_code)
        html = response.json()["html"]
        self.assertEqual(["1", "2", "3"], __import__('re').findall(r'class="preview-number">(\d+)', html))
        self.assertLess(html.index(c), html.index(b))
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; $x^2$', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(before, con.execute("SELECT COUNT(*) FROM basket_exports").fetchone()[0])
        self.assertFalse((self.private / "exports").exists())
        self.assertEqual(403, self.preview({}, csrf=False).status_code)
        invalid = self.preview({"include_images": "yes", "unexpected": "on"})
        self.assertEqual(400, invalid.status_code)
        self.assertIn("无效的导出选项", invalid.json()["error"])
        self.post("/basket/clear")
        empty = self.preview({"include_images": "on"})
        self.assertEqual(400, empty.status_code)
        self.assertIn("空选题篮", empty.json()["error"])

    def test_basket_preview_ui_script_defaults_and_stale_response_contract(self):
        empty_page = self.client.get("/basket").text
        self.assertIn("选题篮还是空的", empty_page)
        self.assertNotIn('data-basket-preview-form', empty_page)
        code = self.codes()[0]
        self.post(f"/basket/add/{code}")
        page = self.client.get("/basket").text
        self.assertIn('src="/static/basket_preview.js"', page)
        self.assertIn('data-basket-preview-form', page)
        self.assertNotIn('name="include_images"', page)
        self.assertIn("题目必需的配图将自动携带", page)
        self.assertNotIn("含图题将自动携带图片", page)
        for name in ("include_source", "include_knowledge", "include_answers", "include_analysis"):
            self.assertRegex(page, rf'name="{name}"(?! checked)')
        script = self.client.get("/static/basket_preview.js")
        self.assertEqual(200, script.status_code)
        self.assertIn("AbortController", script.text)
        self.assertIn("requestSequence", script.text)
        self.assertIn("MathJax.typesetPromise", script.text)


if __name__ == "__main__": unittest.main()
