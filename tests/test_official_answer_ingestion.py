import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from src.database.initialize import initialize_database
from src.importing.admit_questions import _assess, _effective_questions
from src.processing.official_answer_ingestion import (
    OfficialAnswerError,
    _formula_complete,
    apply_reviewed_official_answers,
    parse_answer_extraction_output,
    parse_answer_review_output,
    register_answer_source,
    replace_answer_source,
    run_answer_extraction,
    run_answer_review,
)
from src.reviewing.local_knowledge_classification import (
    KnowledgeClassificationRunError,
    repair_stale_knowledge_classification_after_official_answers,
)
from src.reviewing.candidate_review_ai import (
    CandidateAuditError,
    classification_scope_sha256,
    validated_official_answer_overlay,
    visual_question_scope_sha256,
    validate_ai_approval,
)
from src.reviewing.knowledge_classification import load_bound_knowledge_classification
from src.reviewing.finalize import FinalizationError, _plan
from src.web.app import create_app


def canonical_sha(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


class FakeRunner:
    def __init__(self, payload, run_id):
        self.payload = payload
        self.run_id = run_id
        self.calls = []

    def run(self, *, image_paths, prompt, schema):
        self.calls.append((tuple(image_paths), prompt, schema))
        return json.dumps(self.payload, ensure_ascii=False), self.run_id


class OfficialAnswerIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "fixture-private"
        self.job_dir = self.private / "processing" / "import_job_1"
        pages = self.job_dir / "pages"
        pages.mkdir(parents=True)
        entries = []
        for number in range(1, 5):
            path = pages / f"page_{number:03d}.png"
            Image.new("RGB", (32, 40), (240, number, 10)).save(path)
            raw = path.read_bytes()
            entries.append({
                "page_number": number,
                "relative_path": f"pages/page_{number:03d}.png",
                "pixel_width": 32,
                "pixel_height": 40,
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        manifest = {
            "version": 1, "import_job_id": 1, "dpi": 300,
            "source_pdf_sha256": "a" * 64, "source_page_count": 4,
            "page_start": 1, "page_end": 4, "page_count": 4,
            "pages": entries,
        }
        manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        (self.job_dir / "render_manifest.json").write_bytes(manifest_raw)
        self.candidate = {
            "import_job_id": 1, "source_paper_id": 1, "question_count": 2,
            "questions": [
                {
                    "source_question_no": "1", "stem_markdown": "题一",
                    "question_type_code": "fill_blank", "options": [],
                    "subquestions": [], "answer_markdown": "",
                    "analysis_markdown": "", "source_pages": [1],
                    "primary_knowledge_point_code": "01.01.06",
                    "related_knowledge_point_codes": [], "figure_required": False,
                },
                {
                    "source_question_no": "2", "stem_markdown": "题二",
                    "question_type_code": "solution", "options": [],
                    "subquestions": [
                        {"label": "（1）", "stem_markdown": "第一问"},
                        {"label": "（2）", "stem_markdown": "第二问"},
                    ],
                    "answer_markdown": "", "analysis_markdown": "",
                    "source_pages": [2],
                    "primary_knowledge_point_code": "01.01.06",
                    "related_knowledge_point_codes": [], "figure_required": False,
                },
            ],
        }
        candidate_raw = (json.dumps(self.candidate, ensure_ascii=False, indent=2) + "\n").encode()
        (self.job_dir / "candidate_questions.json").write_bytes(candidate_raw)
        self.candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO source_papers
                   (id,sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(1,?,1,'fixture.pdf','raw_papers/TJ/2026/fixture.pdf',
                          'TJ',2026,'YK','答案阶段合成卷')""", ("a" * 64,)
            )
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,1,'pending')"
            )
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages,
                    manifest_sha256,manifest_byte_size,published_batch_id,
                    source_pdf_sha256)
                   VALUES(1,'completed',300,4,4,?,?,?,?)""",
                (hashlib.sha256(manifest_raw).hexdigest(), len(manifest_raw),
                 "fixture-batch", "a" * 64),
            )
            for question in self.candidate["questions"]:
                encoded = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES(1,?,?,?,?, 'approved',1,?,'human',?)""",
                    (question["source_question_no"], self.candidate_sha, encoded, encoded,
                     "2026-08-01T00:00:00+00:00",
                     '{"method":"workbench","reviewed_at":"2026-08-01T00:00:00+00:00"}'),
                )

    def tearDown(self):
        self.temp.cleanup()

    def extraction_payload(self):
        return {
            "version": 1, "import_job_id": 1, "candidate_sha256": self.candidate_sha,
            "draft_batch_sha256": self.draft_batch_sha(),
            "question_count": 2,
            "questions": [
                {"source_question_no": "1", "content_kind": "short_answer",
                 "answer_markdown": "$3$", "analysis_markdown": "",
                 "subquestions": [], "source_pages": [3]},
                {"source_question_no": "2", "content_kind": "worked_solution",
                 "answer_markdown": "", "analysis_markdown": "总解答跨页。",
                 "subquestions": [
                     {"label": "（1）", "answer_markdown": "$x=1$", "analysis_markdown": "第一页推导"},
                     {"label": "（2）", "answer_markdown": "$y=2$", "analysis_markdown": "续页推导"},
                 ], "source_pages": [3, 4]},
            ],
        }

    def review_payload(self, extraction_sha):
        return {
            "version": 1, "import_job_id": 1,
            "candidate_sha256": self.candidate_sha,
            "draft_batch_sha256": self.draft_batch_sha(),
            "extraction_artifact_sha256": extraction_sha, "question_count": 2,
            "questions": [
                {"source_question_no": "1", "decision": "passed",
                 "question_number_match": True, "final_answer_match": True,
                 "all_subquestions_match": True, "page_boundaries_match": True,
                 "formula_complete": True, "source_pages": [3], "issues": []},
                {"source_question_no": "2", "decision": "passed",
                 "question_number_match": True, "final_answer_match": True,
                 "all_subquestions_match": True, "page_boundaries_match": True,
                 "formula_complete": True, "source_pages": [3, 4], "issues": []},
            ],
        }

    def draft_batch_sha(self):
        with closing(sqlite3.connect(self.db)) as connection:
            questions = [json.loads(row[0]) for row in connection.execute(
                "SELECT edited_json FROM candidate_review_drafts "
                "WHERE import_job_id=1 ORDER BY CAST(source_question_no AS INTEGER)"
            )]
        return canonical_sha({
            "version": 1, "import_job_id": 1, "questions": questions,
        })

    def seed_applied_classification_generation(self, bindings):
        questions = [{
            "source_question_no": number,
            "primary_code": "01.01.06",
            "related_codes": [],
            "reason": "旧答案内容上的分类",
        } for number, _, _ in bindings]
        artifact = json.dumps({
            "version": 1, "import_job_id": 1, "source_classifier": "codex-cli",
            "reviewer": "codex_double_pass", "scope": "knowledge_only_no_solution",
            "question_count": len(questions), "questions": questions,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        (self.job_dir / "knowledge_classification.json").write_bytes(artifact)
        artifact_sha = hashlib.sha256(artifact).hexdigest()
        adoption_evidence_sha = "e" * 64
        now = "2026-08-01T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO import_knowledge_classification_runs
                   (import_job_id,status,stage,question_count,processed_questions,
                    input_digest,taxonomy_digest,output_sha256,output_byte_size,
                    started_at,completed_at,updated_at,applied_at)
                   VALUES(1,'completed','review_ready',?,?,?,?,?,?,?,?,?,?)""",
                (len(bindings), len(bindings), "1" * 64, "2" * 64, artifact_sha,
                 len(artifact), now, now, now, now),
            )
            for number, version, edited_sha in bindings:
                connection.execute(
                    """INSERT INTO candidate_knowledge_classification_drafts
                       (import_job_id,source_question_no,approved_draft_version,
                        edited_sha256,proposal_primary_code,
                        proposal_related_codes_json,proposal_confidence,proposal_reason,
                        verifier_primary_code,verifier_related_codes_json,
                        verifier_confidence,verifier_reason,final_primary_code,
                        final_related_codes_json,final_reason,status,approval_source,
                        reviewed_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (1, number, version, edited_sha, "01.01.06", "[]", "high",
                     "旧初审", "01.01.06", "[]", "high", "旧复核", "01.01.06",
                     "[]", "旧结论", "approved", "codex_double_pass", now, now, now),
                )
                connection.execute(
                    """INSERT INTO candidate_knowledge_classifications
                       (import_job_id,source_question_no,approved_draft_version,
                        edited_sha256,primary_knowledge_point_code,
                        related_knowledge_point_codes_json,classifier,reviewer,
                        approval_source,classifier_run_id,evidence_sha256,reason,created_at)
                       VALUES(?,?,?,?,?,'[]','codex-cli','codex_double_pass',
                              'codex_double_pass','old-run',?,'旧结论',?)""",
                    (1, number, version, edited_sha, "01.01.06", adoption_evidence_sha, now),
                )
        return artifact

    def complete_answer_workflow(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        run_answer_extraction(
            self.db, self.private, 1,
            FakeRunner(self.extraction_payload(), "answer-producer"),
        )
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        run_answer_review(
            self.db, self.private, 1,
            FakeRunner(self.review_payload(extraction_sha), "answer-reviewer"),
        )

    def approve_all_human(self):
        reviewed_at = "2026-08-01T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET status='approved',approval_source='human',reviewed_at=?,
                       approval_evidence_json=json_object(
                           'method','workbench','reviewed_at',?
                       )""",
                (reviewed_at, reviewed_at),
            )

    def expand_fixture_to_twenty_questions(self):
        questions = []
        for number in range(1, 21):
            questions.append({
                "source_question_no": str(number),
                "stem_markdown": f"第{number}题",
                "question_type_code": "fill_blank", "options": [],
                "subquestions": [], "answer_markdown": "",
                "analysis_markdown": "", "source_pages": [1 if number <= 10 else 2],
                "primary_knowledge_point_code": "01.01.06",
                "related_knowledge_point_codes": [], "figure_required": False,
                "tags": ["求值"],
            })
        self.candidate = {
            "import_job_id": 1, "source_paper_id": 1,
            "question_count": 20, "questions": questions,
        }
        raw = (json.dumps(self.candidate, ensure_ascii=False, indent=2) + "\n").encode()
        (self.job_dir / "candidate_questions.json").write_bytes(raw)
        self.candidate_sha = hashlib.sha256(raw).hexdigest()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM candidate_review_drafts WHERE import_job_id=1")
            for question in questions:
                encoded = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version)
                       VALUES(1,?,?,?,?, 'approved',1)""",
                    (question["source_question_no"], self.candidate_sha, encoded, encoded),
                )
        self.approve_all_human()

    def twenty_answer_payload(self):
        return {
            "version": 1, "import_job_id": 1,
            "candidate_sha256": self.candidate_sha,
            "draft_batch_sha256": self.draft_batch_sha(), "question_count": 20,
            "questions": [{
                "source_question_no": str(number), "content_kind": "short_answer",
                "answer_markdown": f"${number}$", "analysis_markdown": "",
                "subquestions": [], "source_pages": [3],
            } for number in range(1, 21)],
        }

    def twenty_review_payload(self, extraction_sha):
        return {
            "version": 1, "import_job_id": 1,
            "candidate_sha256": self.candidate_sha,
            "draft_batch_sha256": self.draft_batch_sha(),
            "extraction_artifact_sha256": extraction_sha, "question_count": 20,
            "questions": [{
                "source_question_no": str(number), "decision": "passed",
                "question_number_match": True, "final_answer_match": True,
                "all_subquestions_match": True, "page_boundaries_match": True,
                "formula_complete": True, "source_pages": [3], "issues": [],
            } for number in range(1, 21)],
        }

    def seed_audit_anchor(self):
        audit = {
            "import_job_id": 1, "question_count": 2,
            "counts": {"auto_pass": 2, "disputed": 0, "human_required": 0},
            "questions": [{
                "source_question_no": number, "audit_status": "auto_pass",
                "issues": [], "suggested_corrections": [], "evidence_page": 1,
                "audit_confidence": "high",
            } for number in ("1", "2")],
        }
        candidate_raw = (self.job_dir / "candidate_questions.json").read_bytes()
        audit_raw = json.dumps(audit, ensure_ascii=False).encode()
        (self.job_dir / "ai_audit.json").write_bytes(audit_raw)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO import_candidate_audit_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_candidate_sha256,input_candidate_byte_size,input_crop_generation_id,
                    input_manifest_sha256,input_manifest_signature,output_sha256,
                    output_byte_size,completed_at,updated_at)
                   VALUES(1,'completed',2,2,'post-hoc-fixture',?,?,?,?,?,?,?,
                          '2026-08-02T00:00:00+00:00','2026-08-02T00:00:00+00:00')""",
                (hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
                 "1" * 32, "2" * 64, "3" * 64,
                 hashlib.sha256(audit_raw).hexdigest(), len(audit_raw)),
            )

    def prepare_post_hoc_generation(self):
        with closing(sqlite3.connect(self.db)) as connection:
            bindings = [
                (number, version, canonical_sha(json.loads(edited_json)))
                for number, version, edited_json in connection.execute(
                    "SELECT source_question_no,version,edited_json FROM candidate_review_drafts "
                    "ORDER BY CAST(source_question_no AS INTEGER)"
                )
            ]
        self.complete_answer_workflow()
        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))
        now = "2026-08-02T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET status='approved',approval_source='human',reviewed_at=?,
                       approval_evidence_json=json_object('method','workbench','reviewed_at',?)""",
                (now, now),
            )
        self.seed_audit_anchor()
        self.seed_applied_classification_generation(bindings)
        return bindings

    def make_immutable_three_line_draft_four_line(self):
        """Model the production Q19 correction without touching answer fields."""
        immutable = self.candidate["questions"][1]
        immutable["subquestions"] = [
            {"label": "Ⅰ", "stem_markdown": "第一问"},
            {"label": "Ⅱ(i)", "stem_markdown": "公共条件并求 Tn"},
            {"label": "Ⅱ(ii)", "stem_markdown": "最后一问"},
        ]
        candidate_raw = (
            json.dumps(self.candidate, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        (self.job_dir / "candidate_questions.json").write_bytes(candidate_raw)
        self.candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
        approved = json.loads(json.dumps(immutable, ensure_ascii=False))
        approved["subquestions"] = [
            {"label": "Ⅰ", "stem_markdown": "第一问"},
            {"label": "Ⅱ", "stem_markdown": "公共条件"},
            {"label": "Ⅱ(i)", "stem_markdown": "求 Tn"},
            {"label": "Ⅱ(ii)", "stem_markdown": "最后一问"},
        ]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET source_candidate_sha256=? "
                "WHERE import_job_id=1",
                (self.candidate_sha,),
            )
            connection.execute(
                "UPDATE candidate_review_drafts SET source_snapshot_json=?,edited_json=?,version=2 "
                "WHERE import_job_id=1 AND source_question_no='2'",
                (
                    json.dumps(immutable, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(approved, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return approved

    def four_line_extraction_payload(self):
        payload = self.extraction_payload()
        payload["questions"][1]["subquestions"] = [
            {"label": "Ⅰ", "answer_markdown": "$a$", "analysis_markdown": "第一问"},
            {"label": "Ⅱ", "answer_markdown": "", "analysis_markdown": "公共条件"},
            {"label": "Ⅱ(i)", "answer_markdown": "$T_n=n$", "analysis_markdown": "求和"},
            {"label": "Ⅱ(ii)", "answer_markdown": "$1$", "analysis_markdown": "结论"},
        ]
        return payload

    def test_schema_migration_and_explicit_source_states(self):
        register_answer_source(self.db, self.private, 1, "source_has_no_answer")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual("source_has_no_answer", connection.execute(
                "SELECT source_answer_state FROM import_answer_sources WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_answer_pages WHERE import_job_id=1"
            ).fetchone()[0])
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM import_answer_pages WHERE import_job_id=1"
            ).fetchone()[0])

    def test_registration_rejects_bad_range_path_and_hash_drift_atomically(self):
        with self.assertRaises(OfficialAnswerError):
            register_answer_source(
                self.db, self.private, 1, "source_has_answer_unprocessed", 2, 5
            )
        manifest = json.loads((self.job_dir / "render_manifest.json").read_text())
        manifest["pages"][2]["relative_path"] = "../escape.png"
        (self.job_dir / "render_manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(OfficialAnswerError):
            register_answer_source(
                self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
            )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_answer_sources"
            ).fetchone()[0])

    def test_strict_extraction_schema_rejects_missing_extra_and_bad_subquestion_mapping(self):
        payload = self.extraction_payload()
        parsed = parse_answer_extraction_output(
            json.dumps(payload), 1, self.candidate_sha, self.candidate,
            {3: "a" * 64, 4: "b" * 64}, self.draft_batch_sha(),
        )
        self.assertEqual([3, 4], parsed["questions"][1]["source_pages"])
        for mutate in (
            lambda p: p["questions"].pop(),
            lambda p: p["questions"].append(dict(p["questions"][0])),
            lambda p: p["questions"][1]["subquestions"].pop(),
            lambda p: p["questions"][0].update({"unexpected": True}),
            lambda p: p["questions"][0].update({"answer_markdown": "$"}),
        ):
            broken = self.extraction_payload()
            mutate(broken)
            with self.assertRaises(OfficialAnswerError):
                parse_answer_extraction_output(
                    json.dumps(broken), 1, self.candidate_sha, self.candidate,
                    {3: "a" * 64, 4: "b" * 64}, self.draft_batch_sha(),
                )

    def test_extract_review_and_apply_is_bound_independent_atomic_and_idempotent(self):
        reviewed_at = "2026-08-01T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET approval_source='human',reviewed_at=?,
                       approval_evidence_json=json_object(
                           'method','workbench','reviewed_at',?
                       )""",
                (reviewed_at, reviewed_at),
            )
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        producer = FakeRunner(self.extraction_payload(), "producer-fresh-run")
        extracted = run_answer_extraction(self.db, self.private, 1, producer)
        self.assertEqual(2, extracted)
        normalized = self.job_dir / "official_answers.json"
        extraction_sha = hashlib.sha256(normalized.read_bytes()).hexdigest()
        reviewer = FakeRunner(self.review_payload(extraction_sha), "reviewer-fresh-run")
        reviewed = run_answer_review(self.db, self.private, 1, reviewer)
        self.assertEqual(2, reviewed)
        self.assertNotEqual(producer.run_id, reviewer.run_id)
        self.assertNotIn("producer-fresh-run", reviewer.calls[0][1])

        immutable_candidate = (self.job_dir / "candidate_questions.json").read_bytes()
        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))
        self.assertEqual(
            immutable_candidate, (self.job_dir / "candidate_questions.json").read_bytes()
        )
        self.assertEqual(0, apply_reviewed_official_answers(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute(
                "SELECT source_question_no,edited_json,status,approval_source,approval_evidence_json,version "
                "FROM candidate_review_drafts ORDER BY source_question_no"
            ).fetchall()
            self.assertEqual(
                ("approved", "human", json.dumps({
                    "method": "workbench", "reviewed_at": reviewed_at,
                }, separators=(",", ":")), 2),
                rows[0][2:],
            )
            self.assertEqual("$3$", json.loads(rows[0][1])["answer_markdown"])
            second = json.loads(rows[1][1])
            self.assertEqual("$y=2$", second["subquestions"][1]["answer_markdown"])
            self.assertEqual("source_answer_linked", connection.execute(
                "SELECT source_answer_state FROM import_answer_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_current_approved_four_line_draft_drives_extraction_and_apply(self):
        approved = self.make_immutable_three_line_draft_four_line()
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        producer = FakeRunner(self.four_line_extraction_payload(), "draft-four-lines")
        self.assertEqual(2, run_answer_extraction(self.db, self.private, 1, producer))
        prompt = producer.calls[0][1]
        self.assertIn('"subquestion_labels":["Ⅰ","Ⅱ","Ⅱ(i)","Ⅱ(ii)"]', prompt)
        self.assertIn("逐行完整转写", prompt)
        self.assertIn("不得概括、缩写、合并步骤或省略公式", prompt)
        self.assertIn("只有题目结构中的父级条件行而答案页没有独立作答段时，该父级行答案与解析必须均为空", prompt)
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        reviewer = FakeRunner(
            self.review_payload(extraction_sha), "draft-four-lines-review"
        )
        self.assertEqual(2, run_answer_review(
            self.db, self.private, 1, reviewer,
        ))
        self.assertIn(
            "当前草稿结构中的空父级条件行不算额外小问，也不得因此判为结构不匹配",
            reviewer.calls[0][1],
        )
        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts "
                "WHERE import_job_id=1 AND source_question_no='2'"
            ).fetchone()[0])
            self.assertEqual(
                [item["label"] for item in approved["subquestions"]],
                [item["label"] for item in edited["subquestions"]],
            )
            self.assertEqual("$T_n=n$", edited["subquestions"][2]["answer_markdown"])

    def test_answer_apply_rebinds_applied_classification_without_archiving(self):
        reviewed_at = "2026-08-01T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET approval_source='human',reviewed_at=?,
                       approval_evidence_json=json_object(
                           'method','workbench','reviewed_at',?
                       )""",
                (reviewed_at, reviewed_at),
            )
        with closing(sqlite3.connect(self.db)) as connection:
            bindings = [
                (number, version, canonical_sha(json.loads(edited_json)))
                for number, version, edited_json in connection.execute(
                    "SELECT source_question_no,version,edited_json FROM candidate_review_drafts "
                    "ORDER BY CAST(source_question_no AS INTEGER)"
                )
            ]
        old_artifact = self.seed_applied_classification_generation(bindings)
        self.complete_answer_workflow()

        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))

        with closing(sqlite3.connect(self.db)) as connection:
            for table in (
                "candidate_knowledge_classifications",
                "candidate_knowledge_classification_drafts",
                "import_knowledge_classification_runs",
            ):
                self.assertEqual(2 if table != "import_knowledge_classification_runs" else 1, connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE import_job_id=1"
                ).fetchone()[0])
            self.assertEqual(
                [(2, canonical_sha(json.loads(edited_json)))
                 for edited_json, in connection.execute(
                     "SELECT edited_json FROM candidate_review_drafts "
                     "ORDER BY CAST(source_question_no AS INTEGER)"
                 )],
                list(connection.execute(
                    "SELECT approved_draft_version,edited_sha256 "
                    "FROM candidate_knowledge_classifications "
                    "ORDER BY CAST(source_question_no AS INTEGER)"
                )),
            )
        self.assertEqual(
            old_artifact, (self.job_dir / "knowledge_classification.json").read_bytes()
        )
        self.assertFalse((self.job_dir / "knowledge_classification_archive").exists())
        page = TestClient(create_app(self.db, self.private)).get("/imports/1/answers")
        self.assertNotIn("旧答案内容上的分类", page.text)
        self.assertNotIn("knowledge_classification_archive", page.text)

    def test_twenty_approved_classified_questions_keep_all_evidence_and_admission(self):
        self.expand_fixture_to_twenty_questions()
        with closing(sqlite3.connect(self.db)) as connection:
            bindings = [
                (number, version, canonical_sha(json.loads(edited_json)))
                for number, version, edited_json in connection.execute(
                    "SELECT source_question_no,version,edited_json "
                    "FROM candidate_review_drafts ORDER BY CAST(source_question_no AS INTEGER)"
                )
            ]
        artifact = self.seed_applied_classification_generation(bindings)
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        run_answer_extraction(
            self.db, self.private, 1,
            FakeRunner(self.twenty_answer_payload(), "twenty-producer"),
        )
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        run_answer_review(
            self.db, self.private, 1,
            FakeRunner(self.twenty_review_payload(extraction_sha), "twenty-reviewer"),
        )

        self.assertEqual(20, apply_reviewed_official_answers(self.db, self.private, 1))
        self.assertEqual(0, apply_reviewed_official_answers(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual((20, 20, 20, 1, 0), (
                connection.execute("SELECT count(*) FROM candidate_review_drafts WHERE status='approved'").fetchone()[0],
                connection.execute("SELECT count(*) FROM candidate_knowledge_classification_drafts").fetchone()[0],
                connection.execute("SELECT count(*) FROM candidate_knowledge_classifications").fetchone()[0],
                connection.execute("SELECT count(*) FROM import_knowledge_classification_runs WHERE applied_at IS NOT NULL").fetchone()[0],
                connection.execute("SELECT count(*) FROM corrected_draft_reaudits").fetchone()[0],
            ))
            self.assertEqual({2}, {
                row[0] for row in connection.execute(
                    "SELECT version FROM candidate_review_drafts"
                )
            })
            for draft in connection.execute(
                "SELECT * FROM candidate_review_drafts ORDER BY CAST(source_question_no AS INTEGER)"
            ):
                self.assertIsNotNone(load_bound_knowledge_classification(
                    connection, 1, draft["source_question_no"], dict(draft)
                ))
            context = (
                {"id": 1, "sha256": "a" * 64}, None,
                self.candidate["questions"],
                {str(number): {} for number in range(1, 21)},
                {str(number): {} for number in range(1, 21)}, {},
                self.candidate_sha, "unused-audit", "unused-crop",
            )
            effective = _effective_questions(connection, context)
            report = _assess(connection, context, effective=effective)
            self.assertEqual(20, len(report.eligible))
            self.assertEqual((), report.ineligible)
        self.assertEqual(artifact, (self.job_dir / "knowledge_classification.json").read_bytes())
        self.assertFalse((self.job_dir / "knowledge_classification_archive").exists())

    def test_restore_pre_overlay_answers_handles_corrected_parent_subquestion_structure(self):
        from src.reviewing.candidate_review_ai import _restore_pre_overlay_answers

        edited = {
            "answer_markdown": "总答案",
            "analysis_markdown": "总解析",
            "subquestions": [
                {"label": "（Ⅰ）", "stem_markdown": "第一问", "answer_markdown": "答1"},
                {"label": "（Ⅱ）", "stem_markdown": "公共条件", "answer_markdown": ""},
                {"label": "（Ⅱ）（i）", "stem_markdown": "子问一", "answer_markdown": "答2"},
            ],
        }
        source_snapshot = {
            "answer_markdown": "",
            "analysis_markdown": "",
            "subquestions": [
                {"label": "（Ⅰ）", "stem_markdown": "旧第一问"},
                {"label": "（Ⅱ）（i）", "stem_markdown": "旧子问一"},
            ],
        }

        restored = _restore_pre_overlay_answers(edited, source_snapshot)

        self.assertEqual(["（Ⅰ）", "（Ⅱ）", "（Ⅱ）（i）"], [
            item["label"] for item in restored["subquestions"]
        ])
        self.assertTrue(all(
            "answer_markdown" not in item and "analysis_markdown" not in item
            for item in restored["subquestions"]
        ))

    def test_visual_scope_mutation_invalidates_overlay_delegation(self):
        self.approve_all_human()
        self.complete_answer_workflow()
        apply_reviewed_official_answers(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            draft = dict(connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE source_question_no='1'"
            ).fetchone())
            self.assertIsNotNone(validated_official_answer_overlay(connection, draft))
        edited = json.loads(draft["edited_json"])
        edited["stem_markdown"] = "被篡改的题干"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=? WHERE source_question_no='1'",
                (json.dumps(edited, ensure_ascii=False),),
            )
            connection.row_factory = sqlite3.Row
            tampered = dict(connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE source_question_no='1'"
            ).fetchone())
            with self.assertRaises(CandidateAuditError):
                validated_official_answer_overlay(connection, tampered)

    def test_classification_scope_mismatch_rejects_rebind(self):
        self.approve_all_human()
        with closing(sqlite3.connect(self.db)) as connection:
            bindings = [
                (number, version, canonical_sha(json.loads(edited_json)))
                for number, version, edited_json in connection.execute(
                    "SELECT source_question_no,version,edited_json FROM candidate_review_drafts"
                )
            ]
        self.seed_applied_classification_generation(bindings)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TRIGGER candidate_knowledge_classifications_immutable")
            connection.execute("DROP TRIGGER knowledge_classification_applied_draft_immutable")
            connection.execute(
                "UPDATE candidate_knowledge_classifications SET classification_scope_sha256=? "
                "WHERE source_question_no='1'", ("f" * 64,),
            )
        self.complete_answer_workflow()
        with self.assertRaises(OfficialAnswerError):
            apply_reviewed_official_answers(self.db, self.private, 1)

    def test_answer_fields_are_excluded_but_unknown_content_is_conservative(self):
        before = json.loads(json.dumps(self.candidate["questions"][1]))
        after = json.loads(json.dumps(before))
        after["answer_markdown"] = "$9$"
        after["analysis_markdown"] = "新解析"
        after["subquestions"][0]["answer_markdown"] = "$x=9$"
        after["subquestions"][0]["analysis_markdown"] = "新小问解析"
        self.assertEqual(
            visual_question_scope_sha256(before), visual_question_scope_sha256(after)
        )
        self.assertEqual(
            classification_scope_sha256(before), classification_scope_sha256(after)
        )
        after["future_unknown_dependency"] = {"value": 1}
        self.assertNotEqual(
            visual_question_scope_sha256(before), visual_question_scope_sha256(after)
        )
        self.assertNotEqual(
            classification_scope_sha256(before), classification_scope_sha256(after)
        )

    def test_tampered_overlay_anchor_fails_closed(self):
        self.approve_all_human()
        self.complete_answer_workflow()
        apply_reviewed_official_answers(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TRIGGER candidate_official_answer_overlays_immutable")
            connection.execute(
                "UPDATE candidate_official_answer_overlays "
                "SET extraction_artifact_sha256=? WHERE source_question_no='1'",
                ("f" * 64,),
            )
            connection.row_factory = sqlite3.Row
            draft = dict(connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE source_question_no='1'"
            ).fetchone())
            with self.assertRaises(CandidateAuditError):
                validated_official_answer_overlay(connection, draft)

    def test_finalize_plan_fails_closed_for_non_string_overlay_approval_evidence(self):
        self.approve_all_human()
        self.complete_answer_workflow()
        apply_reviewed_official_answers(self.db, self.private, 1)
        batch = {
            "candidates": {
                item["source_question_no"]: item
                for item in self.candidate["questions"]
            },
            "candidate_sha": self.candidate_sha,
            "audits": {},
            "audit_sha": "unused-audit",
        }
        for evidence in (None, sqlite3.Binary(b"{}")):
            with self.subTest(evidence=evidence):
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute(
                        "UPDATE candidate_review_drafts SET approval_evidence_json=? "
                        "WHERE source_question_no='1'",
                        (evidence,),
                    )
                    draft = connection.execute(
                        "SELECT * FROM candidate_review_drafts WHERE source_question_no='1'"
                    ).fetchone()
                    with self.assertRaises(FinalizationError):
                        _plan(connection, [draft], batch)

    def test_ai_second_pass_visual_approval_survives_valid_overlay_chain(self):
        self.seed_audit_anchor()
        audit_raw = (self.job_dir / "ai_audit.json").read_bytes()
        audited_at = "2026-08-02T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            for number, candidate in (("1", self.candidate["questions"][0]),
                                      ("2", self.candidate["questions"][1])):
                evidence = {
                    "method": "batch_auto_pass",
                    "audit_output_sha256": hashlib.sha256(audit_raw).hexdigest(),
                    "candidate_sha256": self.candidate_sha,
                    "source_snapshot_sha256": canonical_sha(candidate),
                    "edited_sha256": canonical_sha(candidate),
                    "audit_run_id": "post-hoc-fixture", "audited_at": audited_at,
                    "reviewed_at": audited_at, "approved_draft_version": 1,
                }
                connection.execute(
                    """UPDATE candidate_review_drafts
                       SET approval_source='ai_second_pass',reviewed_at=?,
                           approval_evidence_json=? WHERE source_question_no=?""",
                    (audited_at, json.dumps(evidence, ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")), number),
                )
        self.complete_answer_workflow()
        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))
        audit_entries = {
            item["source_question_no"]: item
            for item in json.loads(audit_raw)["questions"]
        }
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                "SELECT * FROM candidate_review_drafts ORDER BY source_question_no"
            ):
                self.assertTrue(validate_ai_approval(
                    connection, dict(row),
                    self.candidate["questions"][int(row["source_question_no"]) - 1],
                    candidate_sha256=self.candidate_sha,
                    audit_sha256=hashlib.sha256(audit_raw).hexdigest(),
                    audit_entry=audit_entries[row["source_question_no"]],
                ))

    def test_answer_apply_does_not_invoke_classification_delete_path(self):
        with closing(sqlite3.connect(self.db)) as connection:
            bindings = [
                (number, version, canonical_sha(json.loads(edited_json)))
                for number, version, edited_json in connection.execute(
                    "SELECT source_question_no,version,edited_json FROM candidate_review_drafts"
                )
            ]
        old_artifact = self.seed_applied_classification_generation(bindings)
        self.complete_answer_workflow()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fixture_archive_failure
                   BEFORE DELETE ON candidate_knowledge_classifications
                   BEGIN SELECT RAISE(ABORT, 'fixture archive failure'); END"""
            )

        self.assertEqual(2, apply_reviewed_official_answers(self.db, self.private, 1))

        self.assertEqual(
            old_artifact, (self.job_dir / "knowledge_classification.json").read_bytes()
        )
        archive_root = self.job_dir / "knowledge_classification_archive"
        self.assertFalse(archive_root.exists())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])
            self.assertEqual([2, 2], [row[0] for row in connection.execute(
                "SELECT version FROM candidate_review_drafts ORDER BY source_question_no"
            )])
            self.assertEqual("source_answer_linked", connection.execute(
                "SELECT source_answer_state FROM import_answer_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_post_hoc_legacy_repair_refuses_to_bypass_overlay_chain(self):
        self.prepare_post_hoc_generation()

        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications "
                "WHERE import_job_id=1"
            ).fetchone()[0])

    def test_post_hoc_repair_fails_closed_on_partial_match_or_missing_evidence(self):
        self.prepare_post_hoc_generation()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            current = connection.execute(
                "SELECT version,edited_json FROM candidate_review_drafts "
                "WHERE import_job_id=1 AND source_question_no='1'"
            ).fetchone()
            connection.execute("DROP TRIGGER candidate_knowledge_classifications_immutable")
            connection.execute("DROP TRIGGER knowledge_classification_applied_draft_immutable")
            connection.execute(
                """UPDATE candidate_knowledge_classification_drafts
                   SET approved_draft_version=?,edited_sha256=?
                   WHERE import_job_id=1 AND source_question_no='1'""",
                (current[0], canonical_sha(json.loads(current[1]))),
            )
            connection.execute(
                """UPDATE candidate_knowledge_classifications
                   SET approved_draft_version=?,edited_sha256=?
                   WHERE import_job_id=1 AND source_question_no='1'""",
                (current[0], canonical_sha(json.loads(current[1]))),
            )
        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )
        self.assertTrue((self.job_dir / "knowledge_classification.json").exists())

    def test_post_hoc_repair_fails_closed_on_artifact_drift(self):
        self.prepare_post_hoc_generation()
        artifact = self.job_dir / "knowledge_classification.json"
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])

    def test_post_hoc_repair_fails_closed_on_missing_final_evidence(self):
        self.prepare_post_hoc_generation()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "DROP TRIGGER candidate_knowledge_classifications_delete_immutable"
            )
            connection.execute(
                "DELETE FROM candidate_knowledge_classifications "
                "WHERE import_job_id=1 AND source_question_no='2'"
            )
        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )
        self.assertTrue((self.job_dir / "knowledge_classification.json").exists())

    def test_post_hoc_repair_fails_closed_on_incomplete_answer_evidence(self):
        self.prepare_post_hoc_generation()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_official_answer_reviews SET decision='failed' "
                "WHERE import_job_id=1 AND source_question_no='2'"
            )
        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )
        self.assertTrue((self.job_dir / "knowledge_classification.json").exists())

    def test_post_hoc_repair_fails_closed_when_formal_question_exists(self):
        self.prepare_post_hoc_generation()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO question_sources
                   (question_id,source_paper_id,import_job_id,source_question_no,
                    source_pages_json) VALUES(999,1,1,'1','[1]')"""
            )
        with self.assertRaises(KnowledgeClassificationRunError):
            repair_stale_knowledge_classification_after_official_answers(
                self.db, self.private, 1
            )

    def test_parser_failure_preserves_private_anchored_untrusted_raw(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        broken = self.extraction_payload()
        broken["questions"][0]["answer_markdown"] = "$not-closed"
        raw = json.dumps(broken, ensure_ascii=False)
        with self.assertRaises(OfficialAnswerError):
            run_answer_extraction(
                self.db, self.private, 1, FakeRunner(broken, "parser-rejected-run")
            )
        self.assertFalse((self.job_dir / "official_answers_raw.json").exists())
        self.assertFalse((self.job_dir / "official_answers.json").exists())
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT artifact_relative_path,raw_sha256,byte_size,trusted "
                "FROM import_answer_raw_diagnostics WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual(0, row[3])
        artifact = self.job_dir / row[0]
        self.assertEqual(raw.encode(), artifact.read_bytes())
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), row[1])
        self.assertEqual(len(raw.encode()), row[2])
        self.assertEqual(0o600, stat.S_IMODE(artifact.stat().st_mode))
        response = TestClient(create_app(self.db, self.private)).get("/imports/1/answers")
        self.assertNotIn("not-closed", response.text)

    def test_review_fail_closed_on_hash_drift_missing_formula_and_reused_session(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        producer = FakeRunner(self.extraction_payload(), "same-run")
        run_answer_extraction(self.db, self.private, 1, producer)
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        payload = self.review_payload(extraction_sha)
        payload["questions"][1]["formula_complete"] = False
        payload["questions"][1]["decision"] = "failed"
        payload["questions"][1]["issues"] = ["公式缺失"]
        with self.assertRaises(OfficialAnswerError):
            run_answer_review(self.db, self.private, 1, FakeRunner(payload, "same-run"))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_official_answer_reviews WHERE decision='passed'"
            ).fetchone()[0])

    def test_apply_protects_human_answer_and_is_all_or_nothing(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        run_answer_extraction(self.db, self.private, 1, FakeRunner(self.extraction_payload(), "p"))
        extraction_sha = hashlib.sha256((self.job_dir / "official_answers.json").read_bytes()).hexdigest()
        run_answer_review(self.db, self.private, 1, FakeRunner(self.review_payload(extraction_sha), "r"))
        mutations = (
            lambda question: question.update(answer_markdown="人工答案"),
            lambda question: question.update(analysis_markdown="人工解析"),
            lambda question: question["subquestions"][0].update(answer_markdown="人工小问答案"),
            lambda question: question["subquestions"][0].update(analysis_markdown="人工小问解析"),
        )
        with closing(sqlite3.connect(self.db)) as connection:
            original = connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()[0]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                edited = json.loads(original)
                mutate(edited)
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute(
                        "UPDATE candidate_review_drafts SET edited_json=? WHERE source_question_no='2'",
                        (json.dumps(edited, ensure_ascii=False),),
                    )
                with self.assertRaisesRegex(OfficialAnswerError, "人工编辑"):
                    apply_reviewed_official_answers(self.db, self.private, 1)
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    self.assertEqual("source_has_answer_unprocessed", connection.execute(
                        "SELECT source_answer_state FROM import_answer_sources WHERE import_job_id=1"
                    ).fetchone()[0])
                    self.assertEqual([1, 1], [row[0] for row in connection.execute(
                        "SELECT version FROM candidate_review_drafts ORDER BY source_question_no"
                    )])
                    connection.execute(
                        "UPDATE candidate_review_drafts SET edited_json=? WHERE source_question_no='2'",
                        (original,),
                    )

    def test_explicit_safe_replacement_discards_legacy_three_line_batch_then_reruns(self):
        approved = self.make_immutable_three_line_draft_four_line()
        immutable = self.candidate["questions"][1]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=? WHERE source_question_no='2'",
                (json.dumps(immutable, ensure_ascii=False, separators=(",", ":")),),
            )
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        old_payload = self.extraction_payload()
        old_payload["questions"][1]["subquestions"] = [
            {"label": "Ⅰ", "answer_markdown": "$a$", "analysis_markdown": "第一问"},
            {"label": "Ⅱ(i)", "answer_markdown": "$T_n=n$", "analysis_markdown": "求和"},
            {"label": "Ⅱ(ii)", "answer_markdown": "$1$", "analysis_markdown": "结论"},
        ]
        run_answer_extraction(self.db, self.private, 1, FakeRunner(old_payload, "old-three"))
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        run_answer_review(
            self.db, self.private, 1,
            FakeRunner(self.review_payload(extraction_sha), "old-three-review"),
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=?,version=3 WHERE source_question_no='2'",
                (json.dumps(approved, ensure_ascii=False, separators=(",", ":")),),
            )

        replace_answer_source(self.db, self.private, 1, 3, 4)
        self.assertFalse((self.job_dir / "official_answers.json").exists())
        self.assertTrue(any(
            path.name == "official_answers.json"
            for path in (self.job_dir / "official_answer_archive").rglob("*.json")
        ))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_answer_extraction_runs WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual(self.draft_batch_sha(), connection.execute(
                "SELECT draft_batch_sha256 FROM import_answer_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_explicit_safe_replacement_accepts_fail_closed_review_batch(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        run_answer_extraction(
            self.db, self.private, 1,
            FakeRunner(self.extraction_payload(), "failed-review-producer"),
        )
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        failed = self.review_payload(extraction_sha)
        failed["questions"][1]["decision"] = "failed"
        failed["questions"][1]["issues"] = ["需要重跑提取"]
        with self.assertRaises(OfficialAnswerError):
            run_answer_review(
                self.db, self.private, 1,
                FakeRunner(failed, "failed-review-run"),
            )

        replace_answer_source(self.db, self.private, 1, 3, 4)
        self.assertFalse((self.job_dir / "official_answers.json").exists())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_answer_extraction_runs WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM import_answer_raw_diagnostics "
                "WHERE import_job_id=1 AND stage='review' AND trusted=0"
            ).fetchone()[0])

    def test_linked_official_answer_evidence_satisfies_answer_admission_gate(self):
        register_answer_source(
            self.db, self.private, 1, "source_has_answer_unprocessed", 3, 4
        )
        run_answer_extraction(
            self.db, self.private, 1, FakeRunner(self.extraction_payload(), "producer")
        )
        extraction_sha = hashlib.sha256(
            (self.job_dir / "official_answers.json").read_bytes()
        ).hexdigest()
        run_answer_review(
            self.db, self.private, 1,
            FakeRunner(self.review_payload(extraction_sha), "independent-reviewer"),
        )
        apply_reviewed_official_answers(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            effective = {}
            for row in connection.execute(
                "SELECT source_question_no,edited_json FROM candidate_review_drafts ORDER BY source_question_no"
            ):
                effective[row["source_question_no"]] = (
                    json.loads(row["edited_json"]), object(), (), False
                )
            context = (
                {"id": 1}, None, self.candidate["questions"],
                {"1": {}, "2": {}}, {"1": {}, "2": {}}, {},
                self.candidate_sha,
            )
            report = _assess(connection, context, effective=effective)
        self.assertEqual(["1", "2"], [item.question_no for item in report.eligible])
        self.assertEqual((), report.ineligible)

    def test_web_get_is_read_only_and_posts_require_csrf(self):
        client = TestClient(create_app(self.db, self.private))
        before = self.db.read_bytes()
        response = client.get("/imports/1/answers")
        self.assertEqual(200, response.status_code)
        self.assertEqual(before, self.db.read_bytes())
        self.assertIn("原卷答案", response.text)
        self.assertEqual(403, client.post(
            "/imports/1/answers/register", data={
                "source_answer_state": "source_has_answer_unprocessed",
                "page_start": "3", "page_end": "4",
            },
        ).status_code)
        for endpoint in ("extract", "review", "apply"):
            self.assertEqual(
                403, client.post(f"/imports/1/answers/{endpoint}").status_code
            )
        token = client.cookies.get("basket_csrf")
        response = client.post("/imports/1/answers/register", data={
            "csrf_token": token, "source_answer_state": "source_has_no_answer",
            "page_start": "", "page_end": "",
        }, follow_redirects=False)
        self.assertEqual(303, response.status_code)


class OfficialAnswerReviewParserTests(unittest.TestCase):
    def test_formula_check_does_not_treat_latex_linebreak_before_parenthesis_as_delimiter(self):
        self.assertTrue(_formula_complete(r"$\begin{cases}q=1,\\(5+4d)q^2=1\end{cases}$"))

    def test_review_parser_rejects_any_nonpassing_or_anchor_mismatch(self):
        extraction = {
            "version": 1, "import_job_id": 1, "candidate_sha256": "a" * 64,
            "draft_batch_sha256": "b" * 64,
            "question_count": 1, "questions": [{
                "source_question_no": "1", "content_kind": "short_answer",
                "answer_markdown": "$1$", "analysis_markdown": "",
                "subquestions": [], "source_pages": [2],
            }],
        }
        extraction_sha = canonical_sha(extraction)
        review = {
            "version": 1, "import_job_id": 1, "candidate_sha256": "a" * 64,
            "draft_batch_sha256": "b" * 64,
            "extraction_artifact_sha256": extraction_sha, "question_count": 1,
            "questions": [{
                "source_question_no": "1", "decision": "passed",
                "question_number_match": True, "final_answer_match": True,
                "all_subquestions_match": True, "page_boundaries_match": True,
                "formula_complete": True, "source_pages": [2], "issues": [],
            }],
        }
        self.assertEqual(1, len(parse_answer_review_output(
            json.dumps(review), 1, "a" * 64, extraction_sha, extraction,
            "b" * 64,
        )["questions"]))
        for field in ("question_number_match", "final_answer_match",
                      "all_subquestions_match", "page_boundaries_match",
                      "formula_complete"):
            broken = json.loads(json.dumps(review)); broken["questions"][0][field] = False
            with self.assertRaises(OfficialAnswerError):
                parse_answer_review_output(
                    json.dumps(broken), 1, "a" * 64, extraction_sha, extraction,
                    "b" * 64,
                )


if __name__ == "__main__":
    unittest.main()
