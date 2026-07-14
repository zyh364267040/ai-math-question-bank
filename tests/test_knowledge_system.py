import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import initialize_database
from src.knowledge.parse_knowledge_system import (
    KnowledgeSystemError,
    parse_knowledge_system,
    write_knowledge_points_json,
)


MARKDOWN_PATH = PROJECT_ROOT / "docs/domain/高中数学高考知识点体系.md"
JSON_PATH = PROJECT_ROOT / "data/samples/knowledge_points_v1.json"


class KnowledgeSystemParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.points = parse_knowledge_system(MARKDOWN_PATH)

    def test_exactly_eleven_top_level_modules_are_parsed(self):
        modules = [point for point in self.points if point["level"] == 1]
        self.assertEqual([f"{number:02d}" for number in range(1, 12)], [p["code"] for p in modules])

    def test_all_codes_are_stable_unique_and_parents_exist(self):
        codes = [point["code"] for point in self.points]
        self.assertEqual(len(codes), len(set(codes)))
        code_set = set(codes)
        for point in self.points:
            self.assertEqual(
                {"code", "name", "level", "parent_code", "system_version", "sort_order"},
                set(point),
            )
            self.assertEqual("v1", point["system_version"])
            if point["level"] == 2:
                self.assertRegex(point["code"], r"^\d{2}\.\d{2}$")
            if point["level"] == 3:
                self.assertRegex(point["code"], r"^\d{2}\.\d{2}\.\d{2}$")
            if point["parent_code"] is not None:
                self.assertIn(point["parent_code"], code_set)

    def test_third_level_codes_and_parent_codes_follow_position(self):
        children = [p for p in self.points if p["parent_code"] == "01.01"]
        self.assertEqual(
            [f"01.01.{position:02d}" for position in range(1, len(children) + 1)],
            [point["code"] for point in children],
        )
        self.assertTrue(all(point["level"] == 3 for point in children))
        self.assertEqual(list(range(1, len(children) + 1)), [p["sort_order"] for p in children])

    def test_sections_after_module_eleven_are_not_parsed(self):
        names = {point["name"] for point in self.points}
        self.assertNotIn("主知识点", names)
        self.assertNotIn("天津卷中各三级知识点的实际覆盖情况", names)

    def test_checked_in_json_exactly_matches_live_parser_result(self):
        self.assertEqual(
            self.points,
            json.loads(JSON_PATH.read_text(encoding="utf-8")),
        )

    def test_wrong_topic_prefix_has_clear_error(self):
        source = MARKDOWN_PATH.read_text(encoding="utf-8")
        broken = source.replace("### 01.01 集合", "### 02.01 集合", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeSystemError, "02.01.*当前一级模块 01"):
                parse_knowledge_system(path)

    def test_duplicate_third_level_name_has_clear_error(self):
        source = MARKDOWN_PATH.read_text(encoding="utf-8")
        broken = source.replace("- 元素与集合的关系", "- 集合的含义与表示\n- 元素与集合的关系", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeSystemError, "01.01.*重复三级知识点.*集合的含义与表示"):
                parse_knowledge_system(path)


class KnowledgeSystemDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "question-bank.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialization_imports_all_points_and_is_idempotent(self):
        expected = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        connection = initialize_database(self.db_path)
        try:
            first_count = connection.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0]
        finally:
            connection.close()
        initialize_database(self.db_path).close()
        with sqlite3.connect(self.db_path) as connection:
            second_count = connection.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0]
            duplicate_count = connection.execute(
                "SELECT COUNT(*) FROM (SELECT code FROM knowledge_points GROUP BY code HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        self.assertEqual(len(expected), first_count)
        self.assertEqual(first_count, second_count)
        self.assertEqual(0, duplicate_count)

    def test_changed_name_updates_by_code_instead_of_inserting(self):
        source = MARKDOWN_PATH.read_text(encoding="utf-8")
        old_name = "集合的含义与表示"
        new_name = "集合的含义、表示与描述"
        with tempfile.TemporaryDirectory() as directory:
            markdown_path = Path(directory) / "knowledge.md"
            seed_path = Path(directory) / "knowledge.json"
            markdown_path.write_text(source, encoding="utf-8")
            write_knowledge_points_json(parse_knowledge_system(markdown_path), seed_path)
            initialize_database(self.db_path, knowledge_points_path=seed_path).close()

            markdown_path.write_text(source.replace(old_name, new_name, 1), encoding="utf-8")
            write_knowledge_points_json(parse_knowledge_system(markdown_path), seed_path)
            initialize_database(self.db_path, knowledge_points_path=seed_path).close()

            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT code, name FROM knowledge_points WHERE code = '01.01.01'"
                ).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0]
        self.assertEqual(("01.01.01", new_name), row)
        self.assertEqual(len(parse_knowledge_system(MARKDOWN_PATH)), count)


if __name__ == "__main__":
    unittest.main()
