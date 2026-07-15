"""Create the SQLite database and seed first-version dictionaries."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.parse_knowledge_system import validate_knowledge_points


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "private" / "question-bank.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_KNOWLEDGE_POINTS_PATH = PROJECT_ROOT / "data" / "samples" / "knowledge_points_v1.json"


DICTIONARY_ROWS = {
    "regions": [("TJ", "天津")],
    "exam_types": [
        ("GK", "高考"), ("YM", "一模"), ("EM", "二模"), ("YK", "月考"),
        ("QZ", "期中"), ("QM", "期末"), ("JC", "教材"), ("JF", "教辅"),
        ("ZB", "教师自编"), ("CT", "学生错题"), ("QT", "其他"),
    ],
    "question_types": [
        ("single_choice", "单选"), ("multiple_choice", "多选"),
        ("fill_blank", "填空"), ("solution", "解答"),
    ],
    "review_statuses": [
        ("pending", "待审核"), ("passed", "通过"), ("rejected", "不通过"),
        ("not_applicable", "不适用"),
    ],
    "usability_statuses": [
        ("draft", "草稿"), ("pending_review", "待审核"), ("usable", "可用"),
        ("needs_fix", "待修正"), ("disabled", "已停用"),
    ],
    "version_statuses": [
        ("current", "当前"), ("superseded", "已替代"), ("archived", "已归档"),
    ],
}

DIFFICULTIES = [
    (1, "基础识记", "单一概念、公式或基本运算"),
    (2, "基础应用", "单一主知识点的常规变式"),
    (3, "中等综合", "需要知识点联结或一次关键转化"),
    (4, "较难综合", "跨知识点、多阶段推理或分类讨论"),
    (5, "压轴探究", "结构隐蔽且需要多次关键转化"),
]

TAG_NAMES = {
    "task": ["求值", "求解", "证明", "判断", "参数范围", "最值", "取值范围", "零点个数", "存在性", "轨迹", "定点", "定值", "计数", "概率计算", "模型建立"],
    "method": ["数形结合", "分类讨论", "等价转化", "构造函数", "换元", "韦达定理", "空间向量", "概率建模", "函数与方程", "分离参数", "配方法", "基本不等式", "判别式", "特殊值法", "反证法", "裂项相消", "错位相减"],
    "error": ["概念混淆", "条件遗漏", "分类不全", "定义域遗漏", "取值范围遗漏", "符号错误", "公式误用", "运算错误", "图形关系误判", "答案格式错误"],
    "scenario": ["课堂例题", "课堂练习", "课后作业", "专题复习", "测试组卷", "错题巩固"],
}


def _tag_code(category, position):
    return f"{category}_{position:02d}"


def _ensure_schema_migrations(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_points)")}
    if "sort_order" not in columns:
        connection.execute(
            "ALTER TABLE knowledge_points ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 1 CHECK (sort_order > 0)"
        )
    layout_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(import_layout_analysis_runs)"
        )
    }
    layout_anchor_columns = {
        "manifest_sha256": "TEXT CHECK (manifest_sha256 IS NULL OR length(manifest_sha256) = 64)",
        "manifest_byte_size": "INTEGER CHECK (manifest_byte_size IS NULL OR manifest_byte_size > 0)",
        "published_batch_id": "TEXT CHECK (published_batch_id IS NULL OR length(published_batch_id) BETWEEN 1 AND 64)",
        "source_pdf_sha256": "TEXT CHECK (source_pdf_sha256 IS NULL OR length(source_pdf_sha256) = 64)",
        "render_manifest_sha256": "TEXT CHECK (render_manifest_sha256 IS NULL OR length(render_manifest_sha256) = 64)",
    }
    for name, declaration in layout_anchor_columns.items():
        if name not in layout_columns:
            connection.execute(
                f"ALTER TABLE import_layout_analysis_runs "
                f"ADD COLUMN {name} {declaration}"
            )
    question_columns = {row[1] for row in connection.execute("PRAGMA table_info(questions)")}
    if "answer_status" not in question_columns:
        # SQLite cannot drop the historic non-empty-answer CHECK in place.  This
        # rebuild keeps ids and all child relations stable; foreign keys are
        # checked again by callers after initialization.
        create_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        start = create_sql.index("CREATE TABLE IF NOT EXISTS questions (")
        end = create_sql.index("\n\nCREATE TABLE IF NOT EXISTS question_options", start)
        question_sql = create_sql[start:end].replace("IF NOT EXISTS ", "", 1)
        connection.execute(question_sql.replace("CREATE TABLE questions (", "CREATE TABLE questions_with_answer_status (", 1))
        old_columns = [row[1] for row in connection.execute("PRAGMA table_info(questions)")]
        copied = ", ".join(old_columns)
        connection.execute(
            f"INSERT INTO questions_with_answer_status ({copied}, answer_status) SELECT {copied}, 'provided' FROM questions"
        )
        connection.execute("DROP TABLE questions")
        connection.execute("ALTER TABLE questions_with_answer_status RENAME TO questions")
    sub_columns = {row[1] for row in connection.execute("PRAGMA table_info(subquestions)")}
    if "answer_status" not in sub_columns:
        connection.execute("ALTER TABLE subquestions ADD COLUMN answer_status TEXT NOT NULL DEFAULT 'missing' CHECK (answer_status IN ('provided', 'missing'))")
    deletion_columns = (
        ("deleted_at", "TEXT"),
        ("deletion_reason", "TEXT CHECK (deletion_reason IS NULL OR deletion_reason IN ('unreadable', 'incomplete', 'duplicate', 'unneeded', 'other'))"),
        ("deletion_note", "TEXT CHECK (deletion_note IS NULL OR length(deletion_note) <= 500)"),
    )
    for table in ("questions", "candidate_review_drafts"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in deletion_columns:
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    draft_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(candidate_review_drafts)")
    }
    if "approval_source" not in draft_columns:
        connection.execute(
            "ALTER TABLE candidate_review_drafts ADD COLUMN approval_source TEXT "
            "CHECK (approval_source IN ('human', 'ai_second_pass') OR approval_source IS NULL)"
        )
    if "approval_evidence_json" not in draft_columns:
        connection.execute(
            "ALTER TABLE candidate_review_drafts ADD COLUMN approval_evidence_json TEXT "
            "CHECK (approval_evidence_json IS NULL OR json_valid(approval_evidence_json))"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_questions_deleted ON questions(deleted_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_review_job_deleted "
        "ON candidate_review_drafts(import_job_id, deleted_at)"
    )


def _load_knowledge_points(path):
    try:
        points = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取知识点种子 {path}: {error}") from error
    if not isinstance(points, list):
        raise ValueError(f"知识点种子必须是 JSON 数组：{path}")
    return validate_knowledge_points(points)


def _upsert_knowledge_points(connection, points):
    ids_by_code = {}
    for level in (1, 2, 3):
        for point in (item for item in points if item["level"] == level):
            parent_id = ids_by_code.get(point["parent_code"])
            connection.execute(
                """INSERT INTO knowledge_points
                       (code, name, level, parent_id, system_version, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                       name = excluded.name,
                       level = excluded.level,
                       parent_id = excluded.parent_id,
                       system_version = excluded.system_version,
                       sort_order = excluded.sort_order""",
                (
                    point["code"], point["name"], point["level"], parent_id,
                    point["system_version"], point["sort_order"],
                ),
            )
            ids_by_code[point["code"]] = connection.execute(
                "SELECT id FROM knowledge_points WHERE code = ?", (point["code"],)
            ).fetchone()[0]


def initialize_database(
    database_path=DEFAULT_DATABASE_PATH,
    knowledge_points_path=DEFAULT_KNOWLEDGE_POINTS_PATH,
):
    """Initialize *database_path* idempotently and return an open connection."""
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    existing_question_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(questions)")
    }
    rebuilding_questions = bool(existing_question_columns) and "answer_status" not in existing_question_columns
    connection.execute(f"PRAGMA foreign_keys = {'OFF' if rebuilding_questions else 'ON'}")
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with connection:
            _ensure_schema_migrations(connection)
            for table, rows in DICTIONARY_ROWS.items():
                connection.executemany(
                    f"INSERT OR IGNORE INTO {table} (code, name) VALUES (?, ?)", rows
                )
            connection.executemany(
                """INSERT OR IGNORE INTO difficulty_levels
                   (level, name, description) VALUES (?, ?, ?)""",
                DIFFICULTIES,
            )
            tag_rows = [
                (category, _tag_code(category, position), name)
                for category, names in TAG_NAMES.items()
                for position, name in enumerate(names, start=1)
            ]
            connection.executemany(
                """INSERT OR IGNORE INTO tag_definitions
                   (category, code, name) VALUES (?, ?, ?)""",
                tag_rows,
            )
            _upsert_knowledge_points(
                connection, _load_knowledge_points(knowledge_points_path)
            )
            connection.execute(
                "INSERT OR IGNORE INTO baskets (basket_key, name) VALUES ('default', '默认选题篮')"
            )
        if rebuilding_questions:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"迁移后外键检查失败: {violations[:3]}")
    except Exception:
        connection.close()
        raise
    return connection


def main():
    parser = argparse.ArgumentParser(description="初始化 AI 数学题库 SQLite 数据库")
    parser.add_argument(
        "database_path",
        nargs="?",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"数据库路径（默认：{DEFAULT_DATABASE_PATH}）",
    )
    args = parser.parse_args()
    connection = initialize_database(args.database_path)
    connection.close()
    print(f"数据库已初始化：{args.database_path.expanduser().resolve()}")


if __name__ == "__main__":
    main()
