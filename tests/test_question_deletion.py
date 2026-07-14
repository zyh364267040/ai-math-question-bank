import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.web.app import create_app


class FormalQuestionDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name) / "private"
        self.private.mkdir()
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            paper = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'paper.pdf','raw_papers/TJ/2026/paper.pdf','TJ',2026,'GK','测试卷')""",
                ("a" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,?,'completed')",
                (paper,),
            )
            point = connection.execute(
                "SELECT id FROM knowledge_points ORDER BY id LIMIT 1"
            ).fetchone()[0]
            self.code = "Q-delete-test-001"
            question = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,answer_markdown,region_code,exam_year,
                    exam_type_code,question_type_code,primary_knowledge_point_id,content_hash)
                   VALUES (?,'<b>题干</b>','答案','TJ',2026,'GK','solution',?,'delete-test')""",
                (self.code, point),
            ).lastrowid
            connection.execute(
                "INSERT INTO question_sources(question_id,source_paper_id,import_job_id,source_question_no,source_pages_json) VALUES(?,?,1,'1','[1]')",
                (question, paper),
            )
            connection.execute(
                "INSERT INTO subquestions(question_id,display_order,stem_markdown) VALUES(?,1,'小问')",
                (question,),
            )
        self.client = TestClient(create_app(self.db, self.private))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def csrf(self, path="/questions"):
        self.client.get(path)
        return self.client.cookies.get("basket_csrf")

    def post(self, path, **values):
        values.setdefault("csrf_token", self.csrf())
        return self.client.post(path, data=values, follow_redirects=False)

    def add_to_basket(self):
        response = self.post(f"/basket/add/{self.code}")
        self.assertEqual(303, response.status_code)

    def delete(self, reason="unreadable", note="原扫描模糊"):
        return self.post(
            f"/questions/{self.code}/delete",
            reason=reason,
            note=note,
            confirmed="yes",
            next="/questions",
        )

    def test_delete_entrances_are_present_without_nested_forms(self):
        listing = self.client.get("/questions").text
        detail = self.client.get(f"/questions/{self.code}").text
        self.add_to_basket()
        basket = self.client.get("/basket").text
        for page in (listing, detail, basket):
            self.assertIn(f'action="/questions/{self.code}/delete"', page)
            self.assertIn("可在已删除题目中恢复", page)
            self.assertNotIn("<form", page[page.find("<form"):].split("</form>", 1)[0].replace("<form", "", 1))
        self.assertIn("从题库删除", basket)
        self.assertIn("移出选题篮", basket)

    def test_question_list_places_basket_and_delete_actions_in_one_row(self):
        listing = self.client.get("/questions").text
        start = listing.index('class="question-card-actions"')
        actions = listing[start:listing.index("</article>", start)]
        self.assertIn('class="basket-inline-form"', actions)
        self.assertIn('class="delete-panel question-card-delete"', actions)
        self.assertLess(actions.index("加入选题篮"), actions.index("删除题目"))

    def test_local_stylesheets_have_cache_busting_versions(self):
        listing = self.client.get("/questions").text
        self.assertIn("/static/questions.css?v=20260714-actions", listing)
        self.assertIn("/static/workbench.css?v=20260714-actions", listing)

    def test_delete_requires_post_csrf_confirmation_whitelist_and_size_limits(self):
        self.assertEqual(405, self.client.get(f"/questions/{self.code}/delete").status_code)
        self.assertEqual(
            403,
            self.client.post(
                f"/questions/{self.code}/delete",
                data={"csrf_token": "bad", "reason": "unreadable", "confirmed": "yes"},
            ).status_code,
        )
        self.assertEqual(400, self.post(f"/questions/{self.code}/delete", reason="unreadable").status_code)
        self.assertEqual(400, self.post(f"/questions/{self.code}/delete", reason="evil", confirmed="yes").status_code)
        self.assertEqual(400, self.post(f"/questions/{self.code}/delete", reason="other", confirmed="yes", note="x" * 501).status_code)
        self.assertEqual(400, self.post(f"/questions/{self.code}/delete", reason="other", confirmed="yes", unexpected="x").status_code)

    def test_soft_delete_preserves_children_removes_basket_and_filters_everywhere(self):
        self.add_to_basket()
        response = self.delete(note="<script>attack()</script>")
        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT deleted_at,deletion_reason,deletion_note FROM questions WHERE question_code=?",
                (self.code,),
            ).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(("unreadable", "<script>attack()</script>"), row[1:])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM subquestions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0])
        self.assertNotIn(self.code, self.client.get("/questions").text)
        gone = self.client.get(f"/questions/{self.code}")
        self.assertEqual(410, gone.status_code)
        self.assertIn("题目已删除，可前往恢复", gone.text)
        self.assertNotIn("&lt;b&gt;题干", gone.text)
        self.assertEqual(410, self.post(f"/basket/add/{self.code}").status_code)
        self.assertIn("选题篮还是空的", self.client.get("/basket").text)
        self.assertEqual(400, self.post("/basket/preview").status_code)
        self.assertEqual(400, self.post("/basket/export").status_code)
        deleted = self.client.get("/questions/deleted")
        self.assertEqual(200, deleted.status_code)
        self.assertIn("&lt;script&gt;attack()&lt;/script&gt;", deleted.text)
        self.assertNotIn("<script>attack()", deleted.text)

    def test_delete_and_restore_are_idempotent_and_restore_never_readds_basket(self):
        self.add_to_basket()
        self.assertEqual(303, self.delete().status_code)
        again = self.delete()
        self.assertEqual(303, again.status_code)
        self.assertIn("already_deleted=1", again.headers["location"])
        restored = self.post(f"/questions/{self.code}/restore")
        self.assertEqual(303, restored.status_code)
        again = self.post(f"/questions/{self.code}/restore")
        self.assertEqual(303, again.status_code)
        self.assertIn("already_active=1", again.headers["location"])
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT deleted_at,deletion_reason,deletion_note FROM questions WHERE question_code=?",
                (self.code,),
            ).fetchone()
            self.assertEqual((None, "unreadable", "原扫描模糊"), row)
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0])
        self.assertIn(self.code, self.client.get("/questions").text)

    def test_deleted_route_has_priority_over_question_detail(self):
        response = self.client.get("/questions/deleted")
        self.assertEqual(200, response.status_code)
        self.assertIn("已删除题目", response.text)


if __name__ == "__main__":
    unittest.main()
