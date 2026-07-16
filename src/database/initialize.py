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


def _rebuild_layout_analysis_runs(connection, old_columns):
    """Replace a legacy layout-run table with the fully constrained schema."""
    connection.row_factory = sqlite3.Row
    old_rows = [dict(row) for row in connection.execute(
        "SELECT * FROM import_layout_analysis_runs"
    )]
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS import_layout_analysis_runs (")
    end = schema.index("\n\nCREATE TABLE IF NOT EXISTS import_upload_receipts", start)
    create_sql = schema[start:end].replace("IF NOT EXISTS ", "", 1).replace(
        "CREATE TABLE import_layout_analysis_runs (",
        "CREATE TABLE import_layout_analysis_runs_current (",
        1,
    )
    connection.execute(create_sql)
    columns = (
        "import_job_id", "status", "total_pages", "analyzed_pages",
        "detected_questions", "manifest_sha256", "manifest_byte_size",
        "published_batch_id", "source_pdf_sha256", "render_manifest_sha256",
        "error_message", "started_at", "completed_at", "updated_at",
    )
    for row in old_rows:
        total_pages = row.get("total_pages")
        total_pages = total_pages if type(total_pages) is int and total_pages > 0 else None
        analyzed_pages = row.get("analyzed_pages", 0)
        if (
            type(analyzed_pages) is not int
            or analyzed_pages < 0
            or (total_pages is not None and analyzed_pages > total_pages)
        ):
            analyzed_pages = 0
        detected_questions = row.get("detected_questions", 0)
        if type(detected_questions) is not int or detected_questions < 0:
            detected_questions = 0
        anchors = {
            "manifest_sha256": row.get("manifest_sha256") if "manifest_sha256" in old_columns else None,
            "manifest_byte_size": row.get("manifest_byte_size") if "manifest_byte_size" in old_columns else None,
            "published_batch_id": row.get("published_batch_id") if "published_batch_id" in old_columns else None,
            "source_pdf_sha256": row.get("source_pdf_sha256") if "source_pdf_sha256" in old_columns else None,
            "render_manifest_sha256": row.get("render_manifest_sha256") if "render_manifest_sha256" in old_columns else None,
        }
        anchors_valid = (
            all(isinstance(anchors[name], str) and len(anchors[name]) == 64 for name in (
                "manifest_sha256", "source_pdf_sha256", "render_manifest_sha256"
            ))
            and type(anchors["manifest_byte_size"]) is int
            and anchors["manifest_byte_size"] > 0
            and isinstance(anchors["published_batch_id"], str)
            and 1 <= len(anchors["published_batch_id"]) <= 64
        )
        status = row.get("status")
        if status not in {"pending", "processing", "completed", "failed"}:
            status = "failed"
        if status == "completed" and not (
            anchors_valid and total_pages is not None and analyzed_pages == total_pages
        ):
            status = "failed"
        if not anchors_valid:
            anchors = {name: None for name in anchors}
        values = {
            "import_job_id": row["import_job_id"],
            "status": status,
            "total_pages": total_pages,
            "analyzed_pages": analyzed_pages,
            "detected_questions": detected_questions,
            **anchors,
            "error_message": row.get("error_message"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at") if status == "completed" else None,
            "updated_at": row.get("updated_at") or "1970-01-01T00:00:00+00:00",
        }
        connection.execute(
            f"INSERT INTO import_layout_analysis_runs_current ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
    connection.execute("DROP TABLE import_layout_analysis_runs")
    connection.execute(
        "ALTER TABLE import_layout_analysis_runs_current "
        "RENAME TO import_layout_analysis_runs"
    )
    connection.row_factory = None


def _ensure_schema_migrations(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_points)")}
    if "sort_order" not in columns:
        connection.execute(
            "ALTER TABLE knowledge_points ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 1 CHECK (sort_order > 0)"
        )
    render_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(import_page_render_runs)")
    }
    render_anchor_columns = (
        ("manifest_sha256", "TEXT CHECK (manifest_sha256 IS NULL OR (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'))"),
        ("manifest_byte_size", "INTEGER CHECK (manifest_byte_size IS NULL OR manifest_byte_size > 0)"),
        ("published_batch_id", "TEXT CHECK (published_batch_id IS NULL OR length(published_batch_id) BETWEEN 1 AND 100)"),
        ("source_pdf_sha256", "TEXT CHECK (source_pdf_sha256 IS NULL OR (length(source_pdf_sha256) = 64 AND source_pdf_sha256 NOT GLOB '*[^0-9a-f]*'))"),
    )
    for name, declaration in render_anchor_columns:
        if name not in render_columns:
            connection.execute(
                f"ALTER TABLE import_page_render_runs ADD COLUMN {name} {declaration}"
            )
    split_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(import_question_split_runs)")
    }
    split_anchor_columns = (
        ("render_manifest_sha256", "TEXT CHECK (render_manifest_sha256 IS NULL OR length(render_manifest_sha256) = 64)"),
        ("source_pdf_sha256", "TEXT CHECK (source_pdf_sha256 IS NULL OR length(source_pdf_sha256) = 64)"),
        ("crop_manifest_sha256", "TEXT CHECK (crop_manifest_sha256 IS NULL OR length(crop_manifest_sha256) = 64)"),
        ("crop_generation_id", "TEXT CHECK (crop_generation_id IS NULL OR length(crop_generation_id) = 32)"),
        ("crop_manifest_signature", "TEXT CHECK (crop_manifest_signature IS NULL OR length(crop_manifest_signature) = 64)"),
    )
    for name, declaration in split_anchor_columns:
        if name not in split_columns:
            connection.execute(
                f"ALTER TABLE import_question_split_runs ADD COLUMN {name} {declaration}"
            )
    layout_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(import_layout_analysis_runs)"
        )
    }
    layout_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='import_layout_analysis_runs'"
    ).fetchone()
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    layout_start = schema.index(
        "CREATE TABLE IF NOT EXISTS import_layout_analysis_runs ("
    )
    layout_end = schema.index(
        "\n\nCREATE TABLE IF NOT EXISTS import_upload_receipts", layout_start
    )

    def normalize_layout_sql(sql):
        text = (sql or "").strip().rstrip(";")
        normalized = []
        in_literal = False
        index = 0
        while index < len(text):
            character = text[index]
            if character == "'":
                normalized.append(character)
                if in_literal and index + 1 < len(text) and text[index + 1] == "'":
                    normalized.append("'")
                    index += 2
                    continue
                in_literal = not in_literal
            elif not in_literal and character.isspace():
                if normalized and normalized[-1] != " ":
                    normalized.append(" ")
            elif in_literal:
                normalized.append(character)
            else:
                normalized.append(character.lower())
            index += 1
        result = "".join(normalized).strip()
        result = result.replace(
            "create table if not exists ", "create table ", 1
        )
        return result.replace(
            'create table "import_layout_analysis_runs"',
            "create table import_layout_analysis_runs",
            1,
        )

    expected_layout_sql = normalize_layout_sql(schema[layout_start:layout_end])
    if normalize_layout_sql(layout_sql_row[0]) != expected_layout_sql:
        _rebuild_layout_analysis_runs(connection, layout_columns)
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


def _execute_script_transactionally(connection, script):
    """Execute a SQLite script without executescript's implicit COMMIT."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                connection.execute(pending)
            pending = ""
    if pending.strip():
        raise sqlite3.OperationalError("incomplete schema statement")


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
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _execute_script_transactionally(connection, schema)
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
            _execute_script_transactionally(connection, schema)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"迁移后外键检查失败: {violations[:3]}")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
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
