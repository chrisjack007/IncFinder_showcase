"""Install the real model bytes through the new RF transport in an isolated fixture."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit


def exercise(portable, source, temp_parent, receipt):
    sys.path.insert(0, str(portable))
    import models

    index = json.loads((portable / "trust/rf-index.json").read_bytes())
    rows = {row["name"]: row for row in index["parts"]}
    offsets = {}
    position = 0
    for n in rows:
        if ".part-" in n:
            offsets[n] = position
            position += rows[n]["size_bytes"]
        else:
            offsets[n] = 0
    transfers = []
    upstream = []

    class Slice:
        def __init__(self, name):
            self.path = source / "rf" / name.split(".part-")[0]
            self.handle = self.path.open("rb")
            self.handle.seek(offsets[name])
            self.remaining = rows[name]["size_bytes"]
            self.status = 200
            self.headers = {"Content-Length": str(self.remaining), "ETag": '"' + rows[name]["sha256"] + '"'}

        def read(self, length):
            chunk = self.handle.read(min(length, self.remaining))
            self.remaining -= len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            self.handle.close()

    def open_url(opener, req, timeout):
        url = urlsplit(req.full_url)
        if (
            url.hostname == "api.github.com"
            and url.path == "/repos/" + index["repository"] + "/releases/tags/" + index["tag"]
        ):
            raise HTTPError(req.full_url, 403, "fixture metadata rate limit", {}, None)
        root = "/" + index["repository"] + "/releases/download/" + index["tag"] + "/"
        if (
            url.hostname != "github.com"
            or not url.path.startswith(root)
            or url.path[len(root) :] not in rows
        ):
            raise AssertionError("Unexpected release source: " + url.path)
        name = url.path[len(root) :]
        transfers.append(name)
        return Slice(name)

    def hub(cache, supply, name):
        if (supply["repository"], supply["revision"]) not in {
            (index[k]["repository"], index[k]["revision"]) for k in ("ntv3", "ntv3_code")
        }:
            raise AssertionError("Unpinned upstream revision")
        upstream.append(name)
        return source / "ntv3" / name

    from urllib.request import OpenerDirector
    import os

    with tempfile.TemporaryDirectory(prefix="incfinder-dist-rf-", dir=temp_parent) as work:
        folder = Path(work)
        target = folder / "models"
        with (
            patch.dict(
                os.environ, {"INCFINDER_PORTABLE_HOME": str(folder / "cache"), "INCFINDER_OFFLINE": "0"}
            ),
            patch.object(OpenerDirector, "open", open_url),
            patch.object(models, "authorize", lambda **kw: None),
            patch.object(models.download, "download_hub", hub),
        ):
            models.ensure(target)
            if transfers != list(rows):
                raise AssertionError("The new RF transport did not read every part in order")
            first = len(transfers)
            first_up = len(upstream)
            with patch.dict(os.environ, {"INCFINDER_OFFLINE": "1"}):
                models.ensure(target)
            if len(transfers) != first or len(upstream) != first_up:
                raise AssertionError("Warm verification transferred model bytes")
            result = models.install.verify_models(target)
            if result["status"] != "PASS":
                raise AssertionError(result["status"])
            receipt.write_text(
                json.dumps(
                    dict(
                        origin="local transport fixture",
                        initial_model_cache="empty",
                        model_set_sha256=index["model_set_sha256"],
                        rf_tag=index["tag"],
                        files=transfers,
                        rf_bytes=sum(r["size_bytes"] for r in rows.values()),
                        ntv3_fixture_files=upstream,
                        warm_additional_rf_bytes=0,
                        model_verification=result["status"],
                    ),
                    indent=2,
                )
                + "\n",
                encoding="utf8",
            )
    print("Verified full initialization and offline reuse:", receipt, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for n in ("portable", "source", "temp-parent", "receipt"):
        parser.add_argument("--" + n, type=Path, required=True)
    args = parser.parse_args()
    exercise(
        args.portable.resolve(), args.source.resolve(), args.temp_parent.resolve(), args.receipt.resolve()
    )
