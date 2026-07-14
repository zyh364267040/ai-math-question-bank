import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.processing.question_crop import QuestionCropError, generate_question_crops


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QuestionCropTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp_dir.name) / "processing" / "import_job_9"
        pages = self.job_dir / "pages"
        pages.mkdir(parents=True)
        self.page1 = pages / "page_001.png"
        self.page2 = pages / "page_002.png"
        Image.new("RGB", (120, 100), (240, 20, 20)).save(self.page1, "PNG")
        Image.new("RGB", (120, 100), (20, 20, 240)).save(self.page2, "PNG")
        manifest = {
            "import_job_id": 9,
            "page_count": 2,
            "pages": [
                {"page_number": 1, "relative_path": "pages/page_001.png",
                 "pixel_width": 120, "pixel_height": 100,
                 "sha256": sha256(self.page1)},
                {"page_number": 2, "relative_path": "pages/page_002.png",
                 "pixel_width": 120, "pixel_height": 100,
                 "sha256": sha256(self.page2)},
            ],
        }
        (self.job_dir / "render_manifest.json").write_text(json.dumps(manifest))

    def tearDown(self):
        self.temp_dir.cleanup()

    def generate(self, specs=None, **kwargs):
        specs = specs or [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [5, 8, 105, 58]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        return generate_question_crops(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=[1, 2],
            min_width=20,
            min_height=20,
            separator_height=7,
            **kwargs,
        )

    def test_single_regions_generate_valid_png_hashes_and_consistent_manifest(self):
        manifest = self.generate()
        self.assertEqual(9, manifest["import_job_id"])
        self.assertEqual(2, manifest["question_count"])
        self.assertEqual([1, 2], [q["question_no"] for q in manifest["questions"]])
        self.assertEqual(2, len(manifest["source_pages"]))
        for entry in manifest["questions"]:
            output = self.job_dir / entry["output_relative_path"]
            with Image.open(output) as image:
                image.verify()
            self.assertEqual("generated", entry["crop_status"])
            self.assertEqual("pending_ai_review", entry["review_status"])
            self.assertEqual(output.stat().st_size, entry["byte_size"])
            self.assertEqual(sha256(output), entry["sha256"])
            self.assertEqual([], entry["warnings"])
        on_disk = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual(manifest, on_disk)

    def test_multiple_regions_are_stacked_in_reading_order_with_composition(self):
        specs = [
            {"question_no": 1, "regions": [
                {"page_number": 1, "bbox": [5, 10, 105, 40]},
                {"page_number": 1, "bbox": [10, 50, 90, 80]},
            ]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        manifest = self.generate(specs)
        first = manifest["questions"][0]
        self.assertEqual((100, 67), (first["width"], first["height"]))
        self.assertEqual({"mode": "vertical", "separator_height": 7,
                          "background": "white", "region_count": 2}, first["composition"])
        with Image.open(self.job_dir / first["output_relative_path"]) as image:
            self.assertEqual((255, 255, 255), image.convert("RGB").getpixel((1, 33)))

    def test_cross_page_regions_are_supported_without_special_cases(self):
        specs = [
            {"question_no": 1, "regions": [
                {"page_number": 1, "bbox": [0, 60, 120, 100]},
                {"page_number": 2, "bbox": [0, 0, 120, 30]},
            ]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 30, 110, 90]}]},
        ]
        first = self.generate(specs)["questions"][0]
        self.assertEqual([1, 2], [r["page_number"] for r in first["regions"]])
        with Image.open(self.job_dir / first["output_relative_path"]) as image:
            rgb = image.convert("RGB")
            self.assertEqual((240, 20, 20), rgb.getpixel((5, 5)))
            self.assertEqual((20, 20, 240), rgb.getpixel((5, 52)))

    def test_out_of_bounds_bad_order_and_non_whitelisted_pages_are_rejected(self):
        invalid_regions = (
            [{"page_number": 1, "bbox": [0, 0, 121, 50]}],
            [{"page_number": 1, "bbox": [20, 20, 10, 50]}],
            [{"page_number": 3, "bbox": [0, 0, 50, 50]}],
        )
        for regions in invalid_regions:
            with self.subTest(regions=regions), self.assertRaises(QuestionCropError):
                self.generate([{"question_no": 1, "regions": regions},
                               {"question_no": 2, "regions": [{"page_number": 2, "bbox": [0, 0, 50, 50]}]}])

    def test_duplicate_missing_and_invalid_question_numbers_are_rejected(self):
        cases = (
            [{"question_no": 1, "regions": [{"page_number": 1, "bbox": [0, 0, 50, 50]}]}] * 2,
            [{"question_no": 1, "regions": [{"page_number": 1, "bbox": [0, 0, 50, 50]}]}],
            [{"question_no": "<1>", "regions": [{"page_number": 1, "bbox": [0, 0, 50, 50]}]},
             {"question_no": 2, "regions": [{"page_number": 2, "bbox": [0, 0, 50, 50]}]}],
        )
        for specs in cases:
            with self.subTest(specs=specs), self.assertRaises(QuestionCropError):
                self.generate(specs)

    def test_output_path_traversal_absolute_non_png_and_wrong_name_are_rejected(self):
        for relative in ("../escape.png", "/tmp/escape.png", "question_crops/Q001.jpg", "question_crops/Q999.png"):
            specs = [
                {"question_no": 1, "output_relative_path": relative,
                 "regions": [{"page_number": 1, "bbox": [0, 0, 50, 50]}]},
                {"question_no": 2, "regions": [{"page_number": 2, "bbox": [0, 0, 50, 50]}]},
            ]
            with self.subTest(relative=relative), self.assertRaises(QuestionCropError):
                self.generate(specs)

    def test_source_hash_change_is_rejected_before_replacing_outputs(self):
        self.generate()
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        old_output = (self.job_dir / "question_crops/Q001.png").read_bytes()
        Image.new("RGB", (120, 100), "black").save(self.page1, "PNG")
        with self.assertRaisesRegex(QuestionCropError, "哈希"):
            self.generate()
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(old_output, (self.job_dir / "question_crops/Q001.png").read_bytes())

    def test_identical_run_is_idempotent_and_keeps_mtimes(self):
        first = self.generate()
        manifest_path = self.job_dir / "question_crops.json"
        output_path = self.job_dir / "question_crops/Q001.png"
        mtimes = (manifest_path.stat().st_mtime_ns, output_path.stat().st_mtime_ns)
        second = self.generate()
        self.assertEqual(first, second)
        self.assertEqual(mtimes, (manifest_path.stat().st_mtime_ns, output_path.stat().st_mtime_ns))

    def test_batch_failure_rolls_back_and_preserves_complete_old_result(self):
        self.generate()
        before = {p.name: p.read_bytes() for p in (self.job_dir / "question_crops").iterdir()}
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        changed = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [10, 10, 100, 70]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [0, 0, 5, 5]}]},
        ]
        with self.assertRaises(QuestionCropError):
            self.generate(changed)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.job_dir / "question_crops").iterdir()})
        self.assertEqual([], list(self.job_dir.glob(".question_crops.*")))

    def test_atomic_publish_failure_restores_previous_directory_and_manifest(self):
        self.generate()
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        old_output = (self.job_dir / "question_crops/Q001.png").read_bytes()
        changed = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [10, 10, 110, 80]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 10, 110, 80]}]},
        ]
        import src.processing.question_crop as module
        real_replace = module.os.replace
        calls = {"count": 0}

        def fail_during_publish(source, target):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated publish failure")
            return real_replace(source, target)

        with patch("src.processing.question_crop.os.replace", side_effect=fail_during_publish):
            with self.assertRaises(QuestionCropError):
                self.generate(changed)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(old_output, (self.job_dir / "question_crops/Q001.png").read_bytes())


if __name__ == "__main__":
    unittest.main()
