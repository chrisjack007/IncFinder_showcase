"""Check portable output against the existing signed rc2 platform reference."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import numpy as np

EXPECTED_SUMS = "cc80bd39c4e9a7eb1d0ba223c37b72786b625e322e2e27f3013e08ab7662c20e"


def sha(f):
    with f.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(package, out, platform, fmt, mode, lineage=None):
    sums_path = package / "portable/trust/rc2-SHA256SUMS"
    assert sha(sums_path) == EXPECTED_SUMS
    hashes = dict((n, h) for h, n in (line.split(None, 1) for line in sums_path.read_text().splitlines()))
    docs = package / "docs"
    for name in ("example-reference.json", "release-manifest.json"):
        assert sha(docs / name) == hashes[name]
    reference = json.loads((docs / "example-reference.json").read_text(encoding="utf8"))
    manifest = json.loads((docs / "release-manifest.json").read_text(encoding="utf8"))
    package_sha = hashes["incfinder-0.5.0rc2-py_0.conda"]
    meta = json.loads((out / "run_summary.json").read_text(encoding="utf8"))
    assert meta["prediction"]["threshold_mode"] == mode and meta["model_lineage"]["installation"]["sha256"] == package_sha
    assert meta["input"]["record_count"] == len(reference["record_ids"])
    assert sha(package / "examples" / ("sample." + fmt)) == reference["input_sha256"][fmt]
    for name in ("inc_results.csv", "layer1_decisions.csv", "layer2_probabilities.csv", "inc_label_predictions.csv", "probabilities.npz", "run_summary.json"):
        assert (out / name).is_file(), name
    with (out / "layer1_decisions.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert [(int(r["row_index"]), r["Header"]) for r in csv.DictReader(handle)] == list(enumerate(reference["record_ids"]))
    with np.load(out / "probabilities.npz", allow_pickle=False) as data:
        oracle = reference["platforms"][platform][mode]["arrays"]
        for name in ("stage1", "stage2", "gate", "labels"):
            actual = data[name]
            expected = np.asarray(oracle[name], dtype=actual.dtype)
            assert np.array_equal(actual, expected, equal_nan=True), name
        assert data["labels"].shape == (len(reference["record_ids"]), 21)
        assert np.isfinite(data["stage1"]).all()
        assert np.array_equal(np.isnan(data["stage2"]), np.broadcast_to(~data["gate"][:, None], data["stage2"].shape))
    receipt = {"status": "PASS", "platform": platform, "input": fmt, "mode": mode, "records": len(reference["record_ids"]),
               "reference_sha256": hashes["example-reference.json"], "package_sha256": package_sha,
               "run_id": meta["model_lineage"]["run_id"], "probabilities_sha256": sha(out / "probabilities.npz")}
    if lineage:
        row = json.loads(lineage.read_text(encoding="utf-8-sig"))
        identity = row["identity"]
        assert row["row_index"] == 0 and identity["threshold_mode"] == mode
        assert identity["model_lineage"]["installation"]["sha256"] == package_sha
        for layer in ("stage1", "stage2"):
            job = identity["training_jobs"][layer]
            assert job["attempt_id"] and job["weight_sha256"] == manifest["models"]["rf_whole_files"][layer]["sha256"]
        receipt["training_jobs"] = identity["training_jobs"]
    return receipt


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("package", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("platform", choices=("windows", "wsl2", "linux", "docker"))
    p.add_argument("format", choices=("fasta", "fastq"))
    p.add_argument("mode", choices=("ct", "u05"))
    p.add_argument("--lineage", type=Path)
    a = p.parse_args()
    print(json.dumps(check(a.package, a.output, a.platform, a.format, a.mode, a.lineage), indent=2))
