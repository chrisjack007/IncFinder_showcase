"""Small cross-platform checks for the release contract and promotion gate."""

import hashlib
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import acceptance
from release import CFG, ROOT, digest, encode, rf_index
from pipeline import check_published_models


class Gate(unittest.TestCase):
    def test_frozen_identity(self):
        index = rf_index()
        self.assertEqual(len(index["parts"]), 23)
        self.assertIn("ntv3/modeling_ntv3_pretrained.py", index["files"])
        self.assertEqual(sum(p["size_bytes"] for p in index["parts"]), 24748119410)
        self.assertNotEqual(CFG["tag"], index["tag"])
        self.assertTrue(
            all(
                n == n.lower() and "amd64" in n
                for n in [*CFG["packages"].values(), *CFG["launchers"].values()]
            )
        )

    def test_software_requires_every_public_rf_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idx = root / "rf-index.json"
            idx.write_bytes(encode(rf_index()))
            sig = idx.with_suffix(".json.sig")
            sig.write_bytes(b"unit-test signature")
            assets = [
                {"name": row["name"], "digest": "sha256:" + row["sha256"]} for row in rf_index()["parts"]
            ]
            assets.extend({"name": p.name, "digest": "sha256:" + digest(p)} for p in (idx, sig))
            with (
                patch("pipeline.accepted_index", return_value=idx),
                patch.object(acceptance, "anonymous_release", return_value={"assets": assets}),
            ):
                check_published_models(root)
                assets[0]["digest"] = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "Published RF assets"):
                    check_published_models(root)
                assets.pop(0)
                with self.assertRaisesRegex(ValueError, "Published RF assets"):
                    check_published_models(root)

    def test_gate_refuses_local_repeated_or_stale_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SHA256SUMS").write_bytes(b"public-manifest")
            sha = hashlib.sha256(b"public-manifest").hexdigest()
            sigs = {name: "a" * 64 for name in [*CFG["launchers"].values(), *CFG["packages"].values()]}
            reference = "c" * 64
            (root / "SHA256SUMS.sig").write_bytes(b"test-sig")
            remote = {**sigs, "SHA256SUMS": sha, "SHA256SUMS.sig": hashlib.sha256(b"test-sig").hexdigest()}
            release = {"assets": [{"name": n, "digest": "sha256:" + h} for n, h in remote.items()]}
            paths = []
            sample = ["test_frag_000000001", "test_frag_000000002"]
            for target in acceptance.PLATFORMS:
                binary = CFG["launchers"]["windows" if target == "windows" else "linux"]
                runs = [
                    dict(
                        id=target + str(i),
                        csv=f"{target}/run{i}/inc_results.csv",
                        csv_sha256="e" * 64,
                        ids=sample if i < 2 else ["read-z", "read-a"],
                        input_sha256=digest(ROOT / "NT_V3_650M_TSRF_SW/examples/sample.fasta")
                        if i < 2
                        else "2" * 64,
                    )
                    for i in range(3)
                ]
                row = dict(
                    platform=target,
                    tag=CFG["tag"],
                    origin="public",
                    initial_cache="empty",
                    offline=True,
                    manifest_sha256=sha,
                    reference_sha256=reference,
                    launcher_url=acceptance.BASE + CFG["tag"] + "/" + binary,
                    package=CFG["packages"][target],
                    package_sha256="a" * 64,
                    launcher_sha256="a" * 64,
                    runs=runs,
                )
                path = root / (target + ".json")
                path.write_bytes(encode(row))
                paths.append(path)
            with (
                patch.object(
                    acceptance,
                    "signed_sums",
                    side_effect=lambda path, key: sigs
                    if path.name == "SHA256SUMS"
                    else {"example-reference.json": reference},
                ),
                patch.object(acceptance, "anonymous_release", return_value=release),
            ):
                with redirect_stdout(io.StringIO()):
                    acceptance.gate(root, paths)
                with self.assertRaises(ValueError):
                    acceptance.gate(root, paths[:3])
                with self.assertRaises(ValueError):
                    acceptance.gate(root, [paths[0]] * 4)
                wrong_sample = [dict(entry, input_sha256="0" * 64) for entry in runs[:2]] + runs[2:]
                for field, value in (
                    ("runs", wrong_sample),
                    ("origin", "local"),
                    ("initial_cache", "populated"),
                    ("reference_sha256", "0" * 64),
                ):
                    paths[-1].write_bytes(encode(dict(row, **{field: value})))
                    with self.assertRaises(ValueError):
                        acceptance.gate(root, paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
