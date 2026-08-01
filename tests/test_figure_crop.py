import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.processing.figure_crop import CropError, crop_figure
from src.database.initialize import initialize_database
from src.processing.figure_review import FigureReviewError, record_figure_review


class FigureCropTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp_dir.name) / "processing" / "import_job_9"
        self.source = self.job_dir / "pages" / "page_001.png"
        self.source.parent.mkdir(parents=True)
        image = Image.new("RGB", (100, 80), "white")
        for x in range(100):
            for y in range(80):
                image.putpixel((x, y), (x * 2, y * 3, 40))
        image.save(self.source, "PNG")
        self.db = Path(self.temp_dir.name) / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,1,'fixture.pdf','raw_papers/TJ/unknown/fixture.pdf',
                           'TJ','QT','fixture')""", ("a" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES (9,?,'pending')",
                (source_id,),
            )
        (self.job_dir / "candidate_questions.json").write_text(json.dumps({
            "version": 1, "import_job_id": 9, "question_count": 1,
            "questions": [{"source_question_no": "3", "figure_required": True}],
        }))

    def tearDown(self):
        self.temp_dir.cleanup()

    def crop(self, **overrides):
        arguments = {
            "job_dir": self.job_dir,
            "source_png": self.source,
            "output_relative_path": "assets/question_003_figure_01.png",
            "crop_box": (10, 12, 70, 62),
            "question_no": 3,
            "page_number": 1,
            "kind": "question_figure",
        }
        arguments.update(overrides)
        return crop_figure(**arguments)

    def test_normal_crop_is_valid_png_with_hash_and_manifest(self):
        asset = self.crop()
        output = self.job_dir / asset["output_relative_path"]
        with Image.open(output) as image:
            self.assertEqual("PNG", image.format)
            self.assertEqual((60, 50), image.size)
        self.assertEqual(64, len(asset["sha256"]))
        self.assertEqual(output.stat().st_size, asset["byte_size"])
        manifest = json.loads((self.job_dir / "figure_assets.json").read_text())
        self.assertEqual([asset], manifest["assets"])
        self.assertEqual([0.1, 0.15, 0.7, 0.775], asset["crop_box_normalized"])

    def test_normalized_coordinates_and_margin_are_applied_without_overflow(self):
        asset = self.crop(crop_box=(0.1, 0.1, 0.9, 0.9), normalized=True, margin=20)
        self.assertEqual([0, 0, 100, 80], asset["crop_box_pixels"])
        self.assertEqual((100, 80), (asset["width"], asset["height"]))

    def test_invalid_source_coordinates_and_small_crop_are_rejected(self):
        cases = [
            {"crop_box": (10, 10, 101, 20)},
            {"crop_box": (20, 10, 10, 20)},
            {"crop_box": (1, 1, 4, 4), "min_width": 10, "min_height": 10},
            {"source_png": self.job_dir / "pages" / "missing.png"},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(CropError):
                self.crop(**arguments)
        jpg = self.source.with_suffix(".jpg")
        Image.open(self.source).save(jpg, "JPEG")
        with self.assertRaises(CropError):
            self.crop(source_png=jpg)

    def test_output_path_traversal_and_absolute_path_are_rejected(self):
        for output in ("../escape.png", "/tmp/escape.png", "assets/../../escape.png"):
            with self.subTest(output=output), self.assertRaises(CropError):
                self.crop(output_relative_path=output)

    def test_atomic_replace_failure_cleans_temporary_file(self):
        with patch("src.processing.figure_crop.os.replace", side_effect=OSError("stop")):
            with self.assertRaises(CropError):
                self.crop()
        self.assertFalse((self.job_dir / "assets/question_003_figure_01.png").exists())
        self.assertEqual([], list(self.job_dir.rglob("*.tmp")))

    def test_identical_run_is_idempotent(self):
        first = self.crop()
        first_mtime = (self.job_dir / first["output_relative_path"]).stat().st_mtime_ns
        second = self.crop()
        self.assertEqual(first, second)
        self.assertEqual(first_mtime, (self.job_dir / first["output_relative_path"]).stat().st_mtime_ns)
        manifest = json.loads((self.job_dir / "figure_assets.json").read_text())
        self.assertEqual(1, len(manifest["assets"]))

    def test_source_hash_change_refuses_silent_reuse(self):
        self.crop()
        Image.new("RGB", (100, 80), "black").save(self.source, "PNG")
        with self.assertRaisesRegex(CropError, "源页面哈希"):
            self.crop()

    def test_review_evidence_enhancement_records_processing_and_scale(self):
        original = self.crop(
            kind="review_evidence",
            output_relative_path="review/question_012_evidence_original.png",
            question_no="12",
            processing={"variant": "original"},
        )
        enhanced = self.crop(
            kind="review_evidence",
            output_relative_path="review/question_012_evidence_enhanced.png",
            question_no="12",
            processing={"variant": "enhanced", "scale": 3, "contrast": 1.25, "sharpen": True},
        )
        self.assertEqual((180, 150), (enhanced["width"], enhanced["height"]))
        self.assertEqual("enhanced", enhanced["processing"]["variant"])
        self.assertEqual("review_evidence", original["review_status"])

    def test_kind_and_processing_are_strictly_validated(self):
        with self.assertRaises(CropError):
            self.crop(kind="thumbnail")
        with self.assertRaises(CropError):
            self.crop(kind="question_figure", processing={"scale": 2})

    def test_caller_cannot_mark_new_figure_as_review_passed(self):
        with self.assertRaises(CropError):
            self.crop(review_status="ai_review_passed")
        self.assertFalse((self.job_dir / "assets/question_003_figure_01.png").exists())

    def test_independent_figure_review_is_signed_and_recrop_invalidates_generation(self):
        asset = self.crop()
        manifest = json.loads((self.job_dir / "figure_assets.json").read_text())
        evidence = record_figure_review(self.db, Path(self.temp_dir.name), {
            "version": 1, "import_job_id": 9,
            "input_generation_id": manifest["generation_id"], "question_no": 3,
            "output_relative_path": asset["output_relative_path"],
            "reviewer": "independent-figure-review-1", "decision": "approved",
        })
        self.assertEqual("approved", evidence["decision"])
        self.assertRegex(evidence["signature"], r"\A[0-9a-f]{64}\Z")
        with sqlite3.connect(self.db) as connection:
            anchor = connection.execute(
                """SELECT generation_id,artifact_sha256,evidence_signature
                   FROM import_crop_security_reviews
                   WHERE import_job_id=9 AND evidence_kind='figure'"""
            ).fetchone()
        self.assertEqual(manifest["generation_id"], anchor[0])
        self.assertEqual(asset["sha256"], anchor[1])
        self.assertEqual(evidence["signature"], anchor[2])

        second = self.crop(
            output_relative_path="assets/question_003_figure_02.png",
            crop_box=(20, 20, 60, 50),
        )
        changed = json.loads((self.job_dir / "figure_assets.json").read_text())
        self.assertNotEqual(manifest["generation_id"], changed["generation_id"])
        with self.assertRaises(FigureReviewError):
            record_figure_review(self.db, Path(self.temp_dir.name), {
                "version": 1, "import_job_id": 9,
                "input_generation_id": manifest["generation_id"], "question_no": 3,
                "output_relative_path": second["output_relative_path"],
                "reviewer": "independent-figure-review-2", "decision": "approved",
            })


if __name__ == "__main__":
    unittest.main()
