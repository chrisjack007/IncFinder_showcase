"""Replay the sealed rc2 FASTA/ct results. Run with the portable Python and the four-platform receipt ZIP."""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from ntv3_tsrf.common import ROOT, load_thresholds, read_json

from verify_example import verify_example


HERE = Path(__file__).resolve().parent
REFERENCE = HERE.parent / "NT_V3_650M_TSRF_SW/examples/expected_outputs/raw_cpu.json"
REFERENCE_SHA = next(line.split()[0] for line in (HERE / "trust/rc2-SHA256SUMS").read_text().splitlines()
                     if line.split()[1] == "example-reference.json")
ARCHIVE_SHA = "ffc264f609c1d9fb15fa76cfc18284d13a5aae4df9cf34e3da81319690cb1c33"
PLATFORMS = {"windows": "Windows", "wsl2": "WSL2", "linux": "GX2-Linux", "docker": "Docker"}


class SealedExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get("INCFINDER_EXAMPLE_RECEIPT")
        if not path:
            raise unittest.SkipTest("Supply the four-platform receipt ZIP")
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != ARCHIVE_SHA:
            raise ValueError("Unexpected four-platform receipt SHA-256")
        cls.archive = zipfile.ZipFile(path)
        cls.addClassCleanup(cls.archive.close)
        contract = read_json(ROOT / "config/contract.json")
        cls.labels, cls.thresholds = contract["labels"], load_thresholds(contract)

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.extract("windows")

    def extract(self, platform):
        for name in ("inc_results.csv", "run_summary.json", "probabilities.npz"):
            member = f"{PLATFORMS[platform]}/results/fasta-ct/{name}"
            (self.directory / name).write_bytes(self.archive.read(member))

    def verify(self, platform="windows"):
        verify_example(self.directory, REFERENCE, REFERENCE_SHA, platform, self.labels, self.thresholds)

    def test_four_platforms(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.extract(platform)
                self.verify(platform)

    def test_csv_rows_and_displayed_call(self):
        path = self.directory / "inc_results.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            header, first, second = csv.reader(stream)
        wrong = first.copy()
        wrong[header.index("Result")] = "Inc Unclassified"
        for rows, failure in (([first], "missing row"), ([second, first], "row_index"),
                              ([wrong, second], "Result"), ([first, second, second], "extra row")):
            with self.subTest(failure=failure):
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    csv.writer(stream).writerows([header, *rows])
                with self.assertRaisesRegex(ValueError, failure):
                    self.verify()

    def test_run_identity(self):
        path = self.directory / "run_summary.json"
        original = path.read_bytes()
        for group, name, value in (("prediction", "threshold_mode", "u05"),
                                   ("prediction", "label_order", self.labels[::-1]),
                                   ("prediction", "stage2_thresholds", [0.5] * 21),
                                   ("input", "sha256", "0" * 64)):
            with self.subTest(name=name):
                summary = json.loads(original)
                summary[group][name] = value
                path.write_text(json.dumps(summary), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "SHA-256" if name == "sha256" else name):
                    self.verify()

    def test_previous_run_directory(self):
        with self.assertRaisesRegex(ValueError, "run_summary.run_id"):
            verify_example(self.directory, REFERENCE, REFERENCE_SHA, "windows", self.labels, self.thresholds, "different-run")

    def test_raw_array_differences(self):
        path = self.directory / "probabilities.npz"
        for name, indices, value, failure in (
                ("stage1", (0,), 0.517, "stage1"), ("stage2", (0, 0), 0.01, "stage2"),
                ("stage2", (1, 0), 0, "NA mask"), ("gate", (1,), True, "gate"),
                ("labels", (0, 2), 0, "labels")):
            with self.subTest(name=name, indices=indices):
                self.extract("windows")
                with np.load(path, allow_pickle=False) as original:
                    arrays = {field: original[field] for field in original.files}
                arrays[name][indices] = value
                np.savez(path, **arrays)
                with self.assertRaisesRegex(ValueError, failure):
                    self.verify()

    def test_corrupt_reference_cli(self):
        reference = self.directory / "reference.json"
        reference.write_bytes(REFERENCE.read_bytes() + b" ")
        result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(HERE / "verify_example.py"),
                                 str(self.directory), str(reference), REFERENCE_SHA, "windows"],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertIn("example-reference.json SHA-256", result.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python test_verify_example.py FOUR_PLATFORM_RECEIPT.zip")
    os.environ["INCFINDER_EXAMPLE_RECEIPT"] = sys.argv.pop()
    unittest.main(verbosity=2)
