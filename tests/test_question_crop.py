import hashlib
import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.processing.question_crop import (
    QuestionCropError,
    generate_question_crops,
    generate_question_crops_report,
)
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SimulatedProcessKill(BaseException):
    pass


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
        options = {"min_width": 20, "min_height": 20, "separator_height": 7, **kwargs}
        return generate_question_crops(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=[1, 2],
            **options,
        )

    def generate_report(self, specs=None, **kwargs):
        specs = specs or [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [5, 8, 105, 58]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        options = {"min_width": 20, "min_height": 20, "separator_height": 7, **kwargs}
        return generate_question_crops_report(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=[1, 2],
            **options,
        )

    def set_review_status(self, question_no, status):
        path = self.job_dir / "question_crops.json"
        manifest = json.loads(path.read_text())
        manifest["questions"][question_no - 1]["review_status"] = status
        manifest.pop("signature")
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

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

    def test_report_marks_first_batch_as_recropped(self):
        report = self.generate_report()
        self.assertEqual([1, 2], report.recropped_question_nos)
        self.assertEqual([], report.reused_question_nos)
        self.assertEqual("atomic_snapshot", report.publication_mode)
        self.assertEqual(report.manifest, self.generate())

    def test_manifest_is_signed_and_coordinated_tampering_cannot_inherit_review(self):
        report = self.generate_report()
        self.assertRegex(report.generation_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(report.generation_id, report.manifest["generation_id"])
        self.assertRegex(report.manifest["signature"], r"\A[0-9a-f]{64}\Z")

        output = self.job_dir / "question_crops/Q001.png"
        Image.new("RGB", (100, 50), "green").save(output, "PNG")
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        entry = manifest["questions"][0]
        entry["byte_size"] = output.stat().st_size
        entry["sha256"] = sha256(output)
        entry["review_status"] = "ai_review_passed"
        manifest_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(QuestionCropError, "签名|完整性"):
            self.generate_report()

    def test_unsigned_legacy_manifest_forces_one_full_signed_rebuild(self):
        report = self.generate_report()
        path = self.job_dir / "question_crops.json"
        manifest = json.loads(path.read_text())
        manifest.pop("signature", None)
        manifest.pop("generation_id", None)
        path.write_text(json.dumps(manifest))
        rebuilt = self.generate_report()
        self.assertEqual([1, 2], rebuilt.recropped_question_nos)
        self.assertEqual([], rebuilt.reused_question_nos)
        self.assertNotEqual(report.generation_id, rebuilt.generation_id)
        self.assertRegex(rebuilt.manifest["signature"], r"\A[0-9a-f]{64}\Z")

    def test_hardlinked_source_page_is_rejected(self):
        external = self.job_dir / "hardlink-source.png"
        self.page1.replace(external)
        os.link(external, self.page1)
        self.assertGreater(self.page1.stat().st_nlink, 1)
        with self.assertRaisesRegex(QuestionCropError, "链接|文件身份"):
            self.generate_report()

    def test_region_budget_rejects_before_any_image_open(self):
        with patch("src.processing.question_crop.Image.open",
                   side_effect=AssertionError("Pillow must not open before budget rejection")):
            with self.assertRaisesRegex(QuestionCropError, "预算|区域"):
                self.generate_report(max_total_regions=1)

    def test_source_size_pixel_and_disk_budgets_reject_before_pillow(self):
        with patch("src.processing.question_crop.Image.open",
                   side_effect=AssertionError("Pillow must not run before source byte budget")):
            with self.assertRaisesRegex(QuestionCropError, "大小|预算"):
                self.generate_report(max_source_file_bytes=1)

        manifest_path = self.job_dir / "render_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["pages"][0]["pixel_width"] = 10_000
        manifest["pages"][0]["pixel_height"] = 10_000
        manifest_path.write_text(json.dumps(manifest))
        with patch("src.processing.question_crop.Image.open",
                   side_effect=AssertionError("Pillow must not run before source pixel budget")):
            with self.assertRaisesRegex(QuestionCropError, "像素|预算"):
                self.generate_report(max_source_pixels_per_page=1_000)

        manifest["pages"][0]["pixel_width"] = 120
        manifest["pages"][0]["pixel_height"] = 100
        manifest_path.write_text(json.dumps(manifest))
        usage = namedtuple("usage", "total used free")(10_000, 9_999, 1)
        with patch("src.processing.question_crop.shutil.disk_usage", return_value=usage), \
                patch("src.processing.question_crop.Image.open",
                      side_effect=AssertionError("Pillow must not run before disk budget")):
            with self.assertRaisesRegex(QuestionCropError, "磁盘"):
                self.generate_report(min_free_disk_bytes=10)

    def test_output_byte_budget_rolls_back_complete_batch(self):
        with self.assertRaisesRegex(QuestionCropError, "输出字节|预算"):
            self.generate_report(max_total_output_bytes=1)
        self.assertFalse((self.job_dir / "question_crops").exists())
        self.assertFalse((self.job_dir / "question_crops.json").exists())
        self.assertEqual([], list(self.job_dir.glob(".question_crops.*.tmp")))

    def test_output_byte_budget_includes_final_manifest_bytes(self):
        first = self.generate_report()
        png_bytes = sum(entry["byte_size"] for entry in first.manifest["questions"])
        path = self.job_dir / "question_crops.json"
        legacy = json.loads(path.read_text())
        legacy.pop("signature")
        legacy.pop("generation_id")
        path.write_text(json.dumps(legacy))
        before = path.read_bytes()
        with self.assertRaisesRegex(QuestionCropError, "输出字节|manifest|预算"):
            self.generate_report(max_total_output_bytes=png_bytes)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], list(self.job_dir.glob(".question_crops.*.tmp")))

    def test_signed_manifest_schema_rejects_status_hex_bool_and_extra_keys(self):
        self.generate_report()
        path = self.job_dir / "question_crops.json"
        original = json.loads(path.read_text())
        mutations = (
            lambda data: data["questions"][0].__setitem__("review_status", "approved"),
            lambda data: data["questions"][0].__setitem__("sha256", "A" * 64),
            lambda data: data["questions"][0]["regions"][0]["bbox"].__setitem__(0, True),
            lambda data: data["questions"][0].__setitem__("extra", "forbidden"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = json.loads(json.dumps(original))
                data.pop("signature")
                mutate(data)
                path.write_text(json.dumps(sign_manifest(load_hmac_key(self.job_dir), data)))
                with self.assertRaisesRegex(QuestionCropError, "有效签名|schema|拒绝"):
                    self.generate_report()
        path.write_text(json.dumps(original))

    def test_shared_lock_symlink_is_rejected(self):
        self.generate_report()
        lock = self.job_dir / ".crop_artifacts.lock"
        lock.unlink()
        external = self.job_dir / "external.lock"
        external.write_bytes(b"do not lock me")
        lock.symlink_to(external)
        with self.assertRaisesRegex(QuestionCropError, "锁|job目录"):
            self.generate_report()

    def test_job_parent_symlink_is_rejected(self):
        alias = self.job_dir.parent.parent / "processing-alias"
        alias.symlink_to(self.job_dir.parent, target_is_directory=True)
        specs = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [5, 8, 105, 58]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        with self.assertRaisesRegex(QuestionCropError, "job目录|锁"):
            generate_question_crops_report(
                job_dir=alias / self.job_dir.name,
                questions=specs,
                expected_question_nos=[1, 2],
                min_width=20,
                min_height=20,
            )

    def test_hardlinked_signed_manifest_is_rejected(self):
        self.generate_report()
        manifest = self.job_dir / "question_crops.json"
        external = self.job_dir / "hardlinked-manifest.json"
        manifest.replace(external)
        os.link(external, manifest)
        with self.assertRaisesRegex(QuestionCropError, "有效签名|拒绝|链接"):
            self.generate_report()

    def test_source_path_replace_after_pinned_read_cannot_change_output_identity(self):
        import src.processing.question_crop as module
        real_read = module.read_pinned_descriptor
        replaced = {"done": False}
        original_hash = sha256(self.page1)

        def replace_after_read(descriptor, **kwargs):
            artifact = real_read(descriptor, **kwargs)
            if not replaced["done"]:
                replaced["done"] = True
                replacement = self.job_dir / "replacement.png"
                Image.new("RGB", (120, 100), "blue").save(replacement, "PNG")
                replacement.replace(self.page1)
            return artifact

        with patch("src.processing.question_crop.read_pinned_descriptor",
                   side_effect=replace_after_read):
            report = self.generate_report()
        self.assertEqual(original_hash, report.manifest["source_pages"][0]["sha256"])
        self.assertNotEqual(original_hash, sha256(self.page1))
        with Image.open(self.job_dir / "question_crops/Q001.png") as output:
            self.assertEqual((240, 20, 20), output.convert("RGB").getpixel((5, 5)))

    def test_temp_directory_open_failure_closes_images_and_removes_temp_artifacts(self):
        import src.processing.question_crop as module
        real_load_sources = module._load_sources
        real_open_directory = module.open_directory_at
        tracked = []

        def load_with_tracked_images(*args, **kwargs):
            sources, images = real_load_sources(*args, **kwargs)
            for image in images.values():
                original_close = image.close
                close_calls = {"count": 0}

                def tracked_close(original=original_close, calls=close_calls):
                    calls["count"] += 1
                    return original()

                image.close = tracked_close
                tracked.append(close_calls)
            return sources, images

        def fail_temp_open(root_fd, relative):
            if relative.startswith(".question_crops.") and relative.endswith(".tmp"):
                raise OSError("injected temp directory open failure")
            return real_open_directory(root_fd, relative)

        with patch("src.processing.question_crop._load_sources",
                   side_effect=load_with_tracked_images), \
                patch("src.processing.question_crop.open_directory_at",
                      side_effect=fail_temp_open):
            with self.assertRaises(QuestionCropError):
                self.generate_report()
        self.assertTrue(tracked)
        self.assertTrue(all(item["count"] == 1 for item in tracked))
        self.assertEqual([], list(self.job_dir.glob(".question_crops.*.tmp")))

    def test_directory_validation_and_recursive_removal_have_entry_budgets(self):
        import src.processing.question_crop as module
        self.generate_report()
        with patch("src.processing.question_crop.os.listdir",
                   side_effect=AssertionError("pair validation must not use unbounded listdir")):
            self.assertEqual([], self.generate_report().recropped_question_nos)

        from src.processing.secure_crop_artifacts import locked_job
        with locked_job(self.job_dir) as lock:
            os.mkdir("oversized", dir_fd=lock.descriptor)
            directory_fd = module.open_directory_at(lock.descriptor, "oversized")
            try:
                for number in range(4):
                    fd = os.open(
                        f"item-{number}", os.O_CREAT | os.O_WRONLY, 0o600,
                        dir_fd=directory_fd,
                    )
                    os.close(fd)
            finally:
                os.close(directory_fd)
            with patch("src.processing.question_crop.MAX_DIRECTORY_ENTRIES", 3):
                with self.assertRaisesRegex(QuestionCropError, "目录|预算"):
                    module._remove_at(lock.descriptor, "oversized")
            self.assertTrue((self.job_dir / "oversized").is_dir())

    def test_report_reuses_identical_batch_without_writes(self):
        first = self.generate_report()
        paths = [self.job_dir / "question_crops.json",
                 self.job_dir / "question_crops/Q001.png",
                 self.job_dir / "question_crops/Q002.png"]
        before = [(sha256(path), path.stat().st_mtime_ns) for path in paths]
        with patch("src.processing.question_crop.secrets.token_hex",
                   side_effect=AssertionError("idempotent run must not begin publication")):
            second = self.generate_report()
        self.assertEqual([], second.recropped_question_nos)
        self.assertEqual([1, 2], second.reused_question_nos)
        self.assertEqual(first.manifest, second.manifest)
        self.assertEqual(before, [(sha256(path), path.stat().st_mtime_ns) for path in paths])

    def test_only_changed_bbox_is_recropped_and_reused_review_is_preserved(self):
        self.generate_report()
        self.set_review_status(2, "ai_review_passed")
        q2 = self.job_dir / "question_crops/Q002.png"
        q2_before = (sha256(q2), q2.stat().st_mtime_ns)
        changed = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [10, 8, 105, 58]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        report = self.generate_report(changed)
        self.assertEqual([1], report.recropped_question_nos)
        self.assertEqual([2], report.reused_question_nos)
        entries = {entry["question_no"]: entry for entry in report.manifest["questions"]}
        self.assertEqual("pending_ai_review", entries[1]["review_status"])
        self.assertEqual("ai_review_passed", entries[2]["review_status"])
        self.assertEqual(q2_before, (sha256(q2), q2.stat().st_mtime_ns))

    def test_tampered_signed_png_fails_closed_and_review_approval_is_not_used(self):
        self.generate_report()
        self.set_review_status(2, "ai_review_passed")
        Image.new("RGB", (100, 70), "green").save(
            self.job_dir / "question_crops/Q002.png", "PNG")
        with self.assertRaisesRegex(QuestionCropError, "有效签名|完整性|拒绝"):
            self.generate_report()

    def test_symlinked_old_png_fails_closed(self):
        self.generate_report()
        q2 = self.job_dir / "question_crops/Q002.png"
        q2.unlink()
        q2.symlink_to(self.job_dir / "question_crops/Q001.png")
        with self.assertRaisesRegex(QuestionCropError, "有效签名|拒绝"):
            self.generate_report()
        self.assertTrue(q2.is_symlink())

    def test_abnormal_old_manifest_identity_safely_falls_back_to_full_recrop(self):
        self.generate_report()
        self.set_review_status(2, "ai_review_passed")
        path = self.job_dir / "question_crops.json"
        manifest = json.loads(path.read_text())
        manifest["import_job_id"] = 999
        manifest.pop("signature")
        path.write_text(json.dumps(sign_manifest(load_hmac_key(self.job_dir), manifest)))
        report = self.generate_report()
        self.assertEqual([1, 2], report.recropped_question_nos)
        self.assertEqual([], report.reused_question_nos)
        self.assertEqual(
            ["pending_ai_review", "pending_ai_review"],
            [entry["review_status"] for entry in report.manifest["questions"]],
        )

    def test_changed_question_is_the_only_one_sent_through_crop_operation(self):
        self.generate_report()
        changed = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [10, 8, 105, 58]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        real_crop = Image.Image.crop
        cropped_boxes = []

        def track_crop(image, box=None):
            cropped_boxes.append(box)
            return real_crop(image, box)

        with patch.object(Image.Image, "crop", track_crop):
            report = self.generate_report(changed)
        self.assertEqual([1], report.recropped_question_nos)
        self.assertEqual([[10, 8, 105, 58]], cropped_boxes)

    def test_multi_region_separator_and_cross_page_identity_is_per_question(self):
        specs = [
            {"question_no": 1, "regions": [
                {"page_number": 1, "bbox": [0, 0, 100, 30]},
                {"page_number": 2, "bbox": [0, 40, 100, 70]},
            ]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        self.generate_report(specs)
        changed = [dict(specs[0]), specs[1]]
        changed[0] = {**changed[0], "warnings": ["manual boundary"]}
        report = self.generate_report(changed)
        self.assertEqual([1], report.recropped_question_nos)
        self.assertEqual([2], report.reused_question_nos)
        report = self.generate_report(changed, separator_height=8)
        self.assertEqual([1], report.recropped_question_nos)
        self.assertEqual([2], report.reused_question_nos)

    def test_reused_output_must_meet_new_minimum_dimensions(self):
        self.generate_report()
        with self.assertRaisesRegex(QuestionCropError, "最小值"):
            self.generate_report(min_width=101)

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

        def fail_during_publish(source, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated publish failure")
            return real_replace(source, target, **kwargs)

        with patch("src.processing.question_crop.os.replace", side_effect=fail_during_publish):
            with self.assertRaises(QuestionCropError):
                self.generate(changed)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(old_output, (self.job_dir / "question_crops/Q001.png").read_bytes())

    def test_each_crop_rename_boundary_is_recovered_on_next_entry(self):
        import src.processing.question_crop as module
        changed = [
            {"question_no": 1, "regions": [{"page_number": 1, "bbox": [10, 10, 110, 80]}]},
            {"question_no": 2, "regions": [{"page_number": 2, "bbox": [10, 20, 110, 90]}]},
        ]
        for boundary in range(1, 5):
            with self.subTest(boundary=boundary):
                self.generate_report()
                real_replace = module._replace_at
                real_recover = module._recover_crop_publication
                calls = {"replace": 0, "recover": 0}

                def kill_after_rename(root_fd, source, target):
                    real_replace(root_fd, source, target)
                    calls["replace"] += 1
                    if calls["replace"] == boundary:
                        raise SimulatedProcessKill(f"kill after rename {boundary}")

                def skip_in_process_recovery(root_fd, key):
                    calls["recover"] += 1
                    if calls["recover"] > 1:
                        raise SimulatedProcessKill("process is gone")
                    return real_recover(root_fd, key)

                with patch("src.processing.question_crop._replace_at",
                           side_effect=kill_after_rename), \
                        patch("src.processing.question_crop._recover_crop_publication",
                              side_effect=skip_in_process_recovery):
                    with self.assertRaises(QuestionCropError):
                        self.generate_report(changed)

                recovered = self.generate_report(changed)
                on_disk = json.loads((self.job_dir / "question_crops.json").read_text())
                self.assertEqual(recovered.generation_id, on_disk["generation_id"])
                self.assertEqual(recovered.manifest["signature"], on_disk["signature"])
                self.assertFalse((self.job_dir / ".question_crops.previous").exists())
                self.assertFalse((self.job_dir / ".question_crops.previous.json").exists())


if __name__ == "__main__":
    unittest.main()
