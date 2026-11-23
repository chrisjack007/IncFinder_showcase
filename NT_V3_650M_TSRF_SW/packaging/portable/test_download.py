"""Regression checks for the portable layer's public transfer and repair behavior."""
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import models

BODY = b"a small portable test volume"
ROW = {"name": "stage2.joblib.part-001", "asset_id": 17, "size_bytes": len(BODY),
       "sha256": hashlib.sha256(BODY).hexdigest()}


class Response(BytesIO):
    def __init__(self, data, offset=0, truncate=False):
        super().__init__(data[offset:offset + 7] if truncate else data[offset:])
        self.status = 206 if offset else 200
        self.headers = {"Content-Length": str(len(data) - offset), "ETag": '"immutable-volume"'}
        if offset:
            self.headers["Content-Range"] = f"bytes {offset}-{len(data)-1}/{len(data)}"


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.cache = Path(self.scratch.name)
        self.receiver = models.PublicRF()
        self.receiver.direct = True

    def test_resume_after_interrupted_body(self):
        offsets = []
        def transfer(row, offset):
            offsets.append(offset)
            return Response(BODY, offset, truncate=len(offsets) == 1)
        with patch.object(self.receiver, "_binary", side_effect=transfer), patch.object(models.time, "sleep"):
            self.receiver.get_part(self.cache, ROW)
        self.assertEqual(offsets, [0, 7])
        self.assertEqual((self.cache / "rf" / ROW["name"]).read_bytes(), BODY)

    def test_corrupt_complete_and_bad_transfer_are_replaced(self):
        (self.cache / "rf").mkdir()
        dest = self.cache / "rf" / ROW["name"]
        dest.write_bytes(b"X" * len(BODY))
        with patch.object(self.receiver, "_binary", side_effect=[Response(b"Y" * len(BODY)), Response(BODY)]), patch.object(models.time, "sleep"):
            self.receiver.get_part(self.cache, ROW)
        self.assertEqual(dest.read_bytes(), BODY)

    def test_rate_limit_falls_back_to_official_asset(self):
        self.receiver.direct = False
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            if request.full_url.startswith("https://api.github.com/"):
                raise HTTPError(request.full_url, 403, "", {}, BytesIO())
            if request.full_url.startswith("https://github.com/"):
                raise HTTPError(request.full_url, 302, "", {"Location": "https://release-assets.githubusercontent.com/object"}, BytesIO())
            return Response(BODY)
        with patch.object(self.receiver.opener, "open", side_effect=open_request):
            self.receiver.get_part(self.cache, ROW)
        self.assertEqual(len(calls), 3)
        self.assertIn("/IncFinder_showcase/releases/download/v0.5.0rc2/stage2.joblib.part-001", calls[1].full_url)
        self.assertTrue(all(not req.has_header("Authorization") for req in calls))

    def test_gate_denial_stops_before_download(self):
        import huggingface_hub
        with patch.object(huggingface_hub, "get_token", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "approved Hugging Face login"):
                models.authorize(prompt=False)

    def test_no_external_request_for_complete_models(self):
        with patch.object(models, "complete", return_value=True), patch.object(models.install, "verify_models") as verify, patch.object(models, "authorize") as auth, patch.object(models.PublicRF, "get_part") as fetch:
            models.ensure(self.cache)
            verify.assert_called_once_with(self.cache)
            auth.assert_not_called()
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
