"""Regression tests for the distribution layer; use the released portable Python."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import models
from release import CFG, encode, rf_index


class Response(io.BytesIO):
    def __init__(self, body, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body)), "ETag": '"stable"', **headers}


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.index = rf_index()
        self.rx = models.PublicRF(self.index)
        self.rx._json = lambda path: (_ for _ in ()).throw(RuntimeError("API unavailable"))
        self.bodies = {"stage1.joblib": b"first", "stage2.joblib.part-001": b"second"}
        self.rows = [
            {"name": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in self.bodies.items()
        ]
        self.requests = []
        self.rx.opener.open = self.serve

    def serve(self, request, timeout):
        from urllib.parse import urlsplit

        url = urlsplit(request.full_url)
        name = url.path.rsplit("/", 1)[1]
        self.assertEqual(url.netloc, "github.com")
        self.assertEqual(url.path, f"/{CFG['repository']}/releases/download/{CFG['rf_tag']}/{name}")
        body = self.bodies[name]
        offset = int(request.headers["Range"][6:-1]) if "Range" in request.headers else 0
        self.requests.append((name, offset))
        if offset:
            return Response(
                body[offset:], 206, **{"Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}"}
            )
        return Response(body)

    def test_upgrade_and_single_damaged_part(self):
        for row in self.rows:
            self.rx.get_part(self.cache, row)
        self.requests.clear()
        upgrade = models.PublicRF(dict(self.index, tag="rf-future-same-weights"))
        upgrade.opener.open = lambda *args, **kwargs: self.fail("An upgrade fetched unchanged model bytes")
        for row in self.rows:
            upgrade.get_part(self.cache, row)
        self.assertEqual(self.requests, [])
        (self.cache / "rf" / self.rows[1]["name"]).write_bytes(b"x" * 6)
        for row in self.rows:
            self.rx.get_part(self.cache, row)
        self.assertEqual(self.requests, [(self.rows[1]["name"], 0)])

    def test_resume_interrupted_part(self):
        row = self.rows[0]
        complete, partial, state = models.download._paths(self.cache, row)
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"fi")
        identity = {"asset_id": None, "size_bytes": 5, "sha256": row["sha256"]}
        models.download.write_json(
            state, {"identity": identity, "etag_sha256": hashlib.sha256(b'"stable"').hexdigest()}
        )
        self.rx.get_part(self.cache, row)
        self.assertEqual(self.requests, [(row["name"], 2)])
        self.assertEqual(complete.read_bytes(), b"first")

    def test_catalog_and_modified_reference(self):
        folder = self.cache / "trust"
        folder.mkdir()
        data = encode(self.index)
        path = folder / "rf-index.json"
        path.write_bytes(data)
        with (
            patch.object(models, "HERE", self.cache),
            patch.object(models, "RF_INDEX_SHA256", hashlib.sha256(data).hexdigest()),
        ):
            frozen, rf, _, _ = models.install._sources()
            self.assertEqual(models.catalog(frozen, rf)["tag"], CFG["rf_tag"])
            path.write_bytes(data + b" ")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                models.catalog(frozen, rf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
