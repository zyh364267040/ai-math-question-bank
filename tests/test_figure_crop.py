import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.processing.figure_crop import CropError, crop_figure


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


if __name__ == "__main__":
    unittest.main()
