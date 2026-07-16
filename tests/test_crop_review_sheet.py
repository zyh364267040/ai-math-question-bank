import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.processing.crop_review_sheet import (
    SHEET_DIRECTORY,
    CropReviewSheetError,
    generate_crop_review_sheets,
)
from src.processing.question_crop import generate_question_crops_report
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SimulatedProcessKill(BaseException):
    pass


class CropReviewSheetTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp_dir.name) / "processing" / "import_job_4"
        pages = self.job_dir / "pages"
        pages.mkdir(parents=True)
        page = pages / "page_001.png"
        Image.new("RGB", (160, 160), (230, 235, 240)).save(page, "PNG")
        (self.job_dir / "render_manifest.json").write_text(json.dumps({
            "import_job_id": 4,
            "page_count": 1,
            "pages": [{
                "page_number": 1,
                "relative_path": "pages/page_001.png",
                "pixel_width": 160,
                "pixel_height": 160,
                "sha256": sha256(page),
            }],
        }))
        specs = [
            {"question_no": number, "regions": [{
                "page_number": 1,
                "bbox": [number, number, 120 + number, 45 + number],
            }]}
            for number in range(1, 9)
        ]
        generate_question_crops_report(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=list(range(1, 9)),
            min_width=20,
            min_height=20,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @property
    def sheets(self):
        return self.job_dir / "review"

    def test_fixed_groups_and_incremental_rebuild_preserve_unaffected_sheet(self):
        self.assertEqual("review", SHEET_DIRECTORY)
        self.sheets.mkdir()
        evidence = {
            "boundary.png": b"existing png evidence",
            "audit.json": b'{"status":"kept"}\n',
            "notes.md": b"# Existing review notes\n",
        }
        for name, content in evidence.items():
            (self.sheets / name).write_bytes(content)
        built = generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        self.assertEqual(["crops_01_04.jpg", "crops_05_08.jpg"], built)
        for name in built:
            with Image.open(self.sheets / name) as image:
                self.assertEqual("JPEG", image.format)
                self.assertLessEqual(image.width, 2400)
                self.assertLessEqual(image.height, 12000)

        unaffected = self.sheets / "crops_05_08.jpg"
        os.utime(unaffected, ns=(1_000_000_000, 1_000_000_000))
        before = (sha256(unaffected), unaffected.stat().st_mtime_ns)
        import src.processing.crop_review_sheet as module
        with patch("src.processing.crop_review_sheet._build_sheet",
                   wraps=module._build_sheet) as build:
            rebuilt = generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])
        self.assertEqual(["crops_01_04.jpg"], rebuilt)
        self.assertEqual([1, 2, 3, 4], [item[0] for item in build.call_args.args[0]])
        self.assertEqual(before, (sha256(unaffected), unaffected.stat().st_mtime_ns))
        self.assertEqual(evidence, {
            name: (self.sheets / name).read_bytes() for name in evidence
        })

    def test_no_changes_performs_zero_writes(self):
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        before = {path.name: (sha256(path), path.stat().st_mtime_ns)
                  for path in self.sheets.iterdir()}
        self.assertEqual([], generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=[]))
        self.assertEqual(before, {path.name: (sha256(path), path.stat().st_mtime_ns)
                                  for path in self.sheets.iterdir()})

    def test_total_input_byte_budget_rejects_before_any_image_open(self):
        with patch("src.processing.crop_review_sheet.Image.open",
                   side_effect=AssertionError("budget must reject before Pillow")):
            with self.assertRaisesRegex(CropReviewSheetError, "预算|字节"):
                generate_crop_review_sheets(
                    job_dir=self.job_dir,
                    recropped_question_nos=[2, 6],
                    max_input_bytes=1,
                )

    def test_disk_budget_rejects_before_input_png_open(self):
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")(10_000, 9_999, 1)
        with patch("src.processing.crop_review_sheet.shutil.disk_usage", return_value=usage), \
                patch("src.processing.crop_review_sheet.Image.open",
                      side_effect=AssertionError("disk budget must reject before Pillow")):
            with self.assertRaisesRegex(CropReviewSheetError, "磁盘"):
                generate_crop_review_sheets(
                    job_dir=self.job_dir,
                    recropped_question_nos=[2],
                    min_free_disk_bytes=10,
                )

    def test_hardlinked_crop_input_is_rejected(self):
        q2 = self.job_dir / "question_crops/Q002.png"
        external = self.job_dir / "hardlinked-q2.png"
        q2.replace(external)
        os.link(external, q2)
        with self.assertRaisesRegex(CropReviewSheetError, "链接|身份"):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])

    def test_twenty_question_incremental_flow_only_rebuilds_17_20(self):
        specs = [
            {"question_no": number, "regions": [{
                "page_number": 1,
                "bbox": [1, 1, 121, 45],
            }]}
            for number in range(1, 21)
        ]
        first = generate_question_crops_report(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=list(range(1, 21)),
            min_width=20,
            min_height=20,
        )
        self.assertEqual(list(range(1, 21)), first.recropped_question_nos)
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest["questions"][:19]:
            entry["review_status"] = "ai_review_passed"
        manifest.pop("signature")
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        unchanged = generate_question_crops_report(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=list(range(1, 21)),
            min_width=20,
            min_height=20,
        )
        self.assertEqual([], unchanged.recropped_question_nos)
        self.assertEqual(list(range(1, 21)), unchanged.reused_question_nos)
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 21)))
        evidence = self.sheets / "audit.json"
        evidence.write_bytes(b"keep this review evidence")
        unaffected = {
            path.name: (sha256(path), path.stat().st_mtime_ns)
            for path in self.sheets.glob("crops_*.jpg")
            if path.name != "crops_17_20.jpg"
        }

        changed = json.loads(json.dumps(specs))
        changed[19]["regions"][0]["bbox"] = [2, 1, 121, 45]
        real_crop = Image.Image.crop
        crop_calls = []

        def tracking_crop(image, box=None):
            crop_calls.append(box)
            return real_crop(image, box)

        with patch.object(Image.Image, "crop", tracking_crop):
            report = generate_question_crops_report(
                job_dir=self.job_dir,
                questions=changed,
                expected_question_nos=list(range(1, 21)),
                min_width=20,
                min_height=20,
            )
        self.assertEqual([20], report.recropped_question_nos)
        self.assertEqual(list(range(1, 20)), report.reused_question_nos)
        self.assertEqual("atomic_snapshot", report.publication_mode)
        self.assertEqual([[2, 1, 121, 45]], crop_calls)
        self.assertTrue(all(
            entry["review_status"] == "ai_review_passed"
            for entry in report.manifest["questions"][:19]
        ))
        self.assertEqual("pending_ai_review", report.manifest["questions"][19]["review_status"])
        self.assertEqual(["crops_17_20.jpg"], generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=report.recropped_question_nos))
        self.assertEqual(unaffected, {
            path.name: (sha256(path), path.stat().st_mtime_ns)
            for path in self.sheets.glob("crops_*.jpg")
            if path.name != "crops_17_20.jpg"
        })
        self.assertEqual(b"keep this review evidence", evidence.read_bytes())

    def test_coordinated_unsigned_input_tampering_is_rejected(self):
        q2 = self.job_dir / "question_crops/Q002.png"
        Image.new("RGB", (119, 44), "green").save(q2, "PNG")
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        entry = manifest["questions"][1]
        entry["byte_size"] = q2.stat().st_size
        entry["sha256"] = sha256(q2)
        entry["review_status"] = "ai_review_passed"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(CropReviewSheetError, "签名|完整性"):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])

    def test_corrupt_escape_symlink_and_budget_inputs_are_rejected_without_old_damage(self):
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        before = {path.name: path.read_bytes() for path in self.sheets.iterdir()}
        manifest_path = self.job_dir / "question_crops.json"
        original_manifest = manifest_path.read_bytes()
        q2 = self.job_dir / "question_crops/Q002.png"
        original_q2 = q2.read_bytes()

        q2.write_bytes(b"not a png")
        with self.assertRaises(CropReviewSheetError):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])
        q2.write_bytes(original_q2)

        manifest = json.loads(original_manifest)
        manifest["questions"][1]["output_relative_path"] = "../escape.png"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(CropReviewSheetError):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])
        manifest_path.write_bytes(original_manifest)

        q2.unlink()
        q2.symlink_to(self.job_dir / "question_crops/Q001.png")
        with self.assertRaises(CropReviewSheetError):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])
        q2.unlink()
        q2.write_bytes(original_q2)

        with self.assertRaises(CropReviewSheetError):
            generate_crop_review_sheets(
                job_dir=self.job_dir, recropped_question_nos=[2], max_input_pixels=10)

        self.assertEqual(before, {path.name: path.read_bytes() for path in self.sheets.iterdir()})

    def test_publish_failure_rolls_back_every_affected_sheet(self):
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        before = {path.name: path.read_bytes() for path in self.sheets.iterdir()}
        import src.processing.crop_review_sheet as module
        real_replace = module.os.replace
        calls = {"count": 0}

        def fail_during_publish(source, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated sheet publish failure")
            return real_replace(source, target, **kwargs)

        with patch("src.processing.crop_review_sheet.os.replace", side_effect=fail_during_publish):
            with self.assertRaises(CropReviewSheetError):
                generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2, 6])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.sheets.iterdir()})

    def test_symlinked_review_directory_is_rejected(self):
        target = self.job_dir / "actual_review"
        target.mkdir()
        marker = target / "audit.json"
        marker.write_bytes(b"untouched")
        self.sheets.symlink_to(target, target_is_directory=True)
        with self.assertRaises(CropReviewSheetError):
            generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2])
        self.assertEqual(b"untouched", marker.read_bytes())
        self.assertEqual([], list(target.glob("crops_*.jpg")))

    def test_each_sheet_target_rename_boundary_recovers_from_journal(self):
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        marker = self.sheets / "audit.json"
        marker.write_bytes(b"permanent evidence")
        import src.processing.crop_review_sheet as module
        real_replace = module._replace_at

        for boundary in range(2, 6):
            with self.subTest(boundary=boundary):
                calls = {"count": 0}

                def kill_after_rename(source_fd, source, target_fd, target):
                    real_replace(source_fd, source, target_fd, target)
                    calls["count"] += 1
                    if calls["count"] == boundary:
                        raise SimulatedProcessKill(f"kill after sheet rename {boundary}")

                with patch("src.processing.crop_review_sheet._replace_at",
                           side_effect=kill_after_rename):
                    with self.assertRaises(SimulatedProcessKill):
                        generate_crop_review_sheets(
                            job_dir=self.job_dir, recropped_question_nos=[2, 6])
                self.assertTrue((self.job_dir / ".crop_review_sheet_journal.json").exists())
                self.assertEqual([], generate_crop_review_sheets(
                    job_dir=self.job_dir, recropped_question_nos=[]))
                self.assertFalse((self.job_dir / ".crop_review_sheet_journal.json").exists())
                self.assertEqual(b"permanent evidence", marker.read_bytes())
                self.assertEqual(
                    ["crops_01_04.jpg", "crops_05_08.jpg"],
                    sorted(path.name for path in self.sheets.glob("crops_*.jpg")),
                )

    def test_failed_publish_and_failed_rollback_preserve_journal_for_next_entry(self):
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        import src.processing.crop_review_sheet as module
        real_replace = module._replace_at
        calls = {"count": 0}

        def fail_publish_then_rollback(source_fd, source, target_fd, target):
            calls["count"] += 1
            if calls["count"] in {3, 4}:
                raise OSError("publish/rollback failure")
            return real_replace(source_fd, source, target_fd, target)

        with patch("src.processing.crop_review_sheet._replace_at",
                   side_effect=fail_publish_then_rollback):
            with self.assertRaisesRegex(CropReviewSheetError, "恢复未完成"):
                generate_crop_review_sheets(job_dir=self.job_dir, recropped_question_nos=[2, 6])
        self.assertTrue((self.job_dir / ".crop_review_sheet_journal.json").exists())
        self.assertEqual([], generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=[]))
        self.assertFalse((self.job_dir / ".crop_review_sheet_journal.json").exists())

    def test_cross_generation_journal_is_rolled_back_then_rebuilt_from_current_crops(self):
        specs = [
            {"question_no": number, "regions": [{
                "page_number": 1,
                "bbox": [number, number, 120 + number, 45 + number],
            }]}
            for number in range(1, 9)
        ]
        generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=list(range(1, 9)))
        old_generation = json.loads(
            (self.job_dir / "question_crops.json").read_text())["generation_id"]
        import src.processing.crop_review_sheet as module
        real_replace = module._replace_at
        calls = {"count": 0}

        def kill_during_publish(source_fd, source, target_fd, target):
            real_replace(source_fd, source, target_fd, target)
            calls["count"] += 1
            if calls["count"] == 3:
                raise SimulatedProcessKill("leave old-generation journal")

        with patch("src.processing.crop_review_sheet._replace_at",
                   side_effect=kill_during_publish):
            with self.assertRaises(SimulatedProcessKill):
                generate_crop_review_sheets(
                    job_dir=self.job_dir, recropped_question_nos=[2, 6])
        journal_path = self.job_dir / ".crop_review_sheet_journal.json"
        journal = json.loads(journal_path.read_text())
        self.assertEqual(old_generation, journal["source_generation_id"])

        page = self.job_dir / "pages/page_001.png"
        Image.new("RGB", (160, 160), (15, 30, 245)).save(page, "PNG")
        render_path = self.job_dir / "render_manifest.json"
        render = json.loads(render_path.read_text())
        render["pages"][0]["sha256"] = sha256(page)
        render_path.write_text(json.dumps(render))
        crop_report = generate_question_crops_report(
            job_dir=self.job_dir,
            questions=specs,
            expected_question_nos=list(range(1, 9)),
            min_width=20,
            min_height=20,
        )
        self.assertNotEqual(old_generation, crop_report.generation_id)

        with patch("src.processing.crop_review_sheet._build_sheet",
                   side_effect=CropReviewSheetError("injected current-generation build failure")):
            with self.assertRaises(CropReviewSheetError):
                generate_crop_review_sheets(
                    job_dir=self.job_dir, recropped_question_nos=[])
        self.assertTrue(journal_path.exists())

        rebuilt = generate_crop_review_sheets(
            job_dir=self.job_dir, recropped_question_nos=[])
        self.assertEqual(["crops_01_04.jpg", "crops_05_08.jpg"], rebuilt)
        self.assertFalse(journal_path.exists())
        for name in rebuilt:
            with Image.open(self.sheets / name) as sheet:
                pixels = list(sheet.convert("RGB").getdata())
            self.assertTrue(any(blue > red + 80 for red, _green, blue in pixels))

    def test_open_failure_loop_does_not_leak_fds_or_temp_directories(self):
        before = len(os.listdir("/dev/fd"))
        with patch("src.processing.crop_review_sheet._open_review",
                   side_effect=OSError("injected review open failure")):
            for _ in range(10):
                with self.assertRaises(CropReviewSheetError):
                    generate_crop_review_sheets(
                        job_dir=self.job_dir, recropped_question_nos=[2])
        after = len(os.listdir("/dev/fd"))
        self.assertEqual(0, after - before)
        self.assertEqual([], list(self.job_dir.glob(".crop_review_sheets.stage.*")))
        self.assertEqual([], list(self.job_dir.glob(".crop_review_sheets.backup.*")))


if __name__ == "__main__":
    unittest.main()
