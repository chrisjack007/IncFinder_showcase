"""Prepare the public, signed rc2 models and the two gated upstream revisions."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request

from bootstrap import data_home
from ntv3_tsrf import model_download as download
from ntv3_tsrf import model_install as install
from ntv3_tsrf.encoder_source import CHECKPOINT_SOURCE, CODE_SOURCE
from ntv3_tsrf.resource_plan import _disk_budget, _nearest_directory
from ntv3_tsrf.run_record import _writer_lease

HERE = Path(__file__).resolve().parent
SUMS_SHA = "cc80bd39c4e9a7eb1d0ba223c37b72786b625e322e2e27f3013e08ab7662c20e"
REPOSITORY = "chrisjack007/IncFinder_showcase"
TAG = "v0.5.0rc2"
RELEASE_ID = 455155431
HF_ENDPOINT = "https://huggingface.co"


def authorize(prompt=False, force=False):
    from huggingface_hub import get_hf_file_metadata, get_token, hf_hub_url, login
    sources = ((CHECKPOINT_SOURCE, "config.json"), (CODE_SOURCE, "configuration_ntv3_pretrained.py"))
    def granted():
        try:
            if not get_token():
                return False
            for source, name in sources:
                url = hf_hub_url(source["repository"], name, revision=source["revision"], endpoint=HF_ENDPOINT)
                get_hf_file_metadata(url, token=True, timeout=20)
            return True
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in (401, 403, 404):
                return False
            raise RuntimeError("Cannot reach Hugging Face to check model access. Check the connection and retry.") from None
    if not force and granted():
        return
    links = "\n".join(HF_ENDPOINT + "/" + row["repository"] for row, _ in sources)
    print("Request access to both official model repositories with your Hugging Face account:\n" + links,
          file=sys.stderr, flush=True)
    if prompt and sys.stdin.isatty():
        login(add_to_git_credential=False)
        if granted():
            return
    raise RuntimeError("An approved Hugging Face login is required. Run the packaged launcher with 'auth login', then retry.")


def signed_rows(rf):
    blob = (HERE / "trust/rc2-SHA256SUMS").read_bytes()
    if hashlib.sha256(blob).hexdigest() != SUMS_SHA:
        raise ValueError("The original signed rc2 checksum file has changed.")
    signed = dict((name.lstrip("*"), checksum) for checksum, name in
                  (line.split(None, 1) for line in blob.decode("utf8").splitlines() if line.strip()))
    ids = json.loads((HERE / "trust/rc2-asset-ids.json").read_text(encoding="utf8"))
    rows = []
    for row in rf["parts"]:
        name = row["name"]
        identifier = ids[name]
        if signed.get(name) != row["sha256"] or type(identifier) is not int or identifier <= 0:
            raise ValueError("RF catalog differs from the signed rc2 manifest: " + name)
        rows.append(dict(row, asset_id=identifier))
    return rows


class PublicRF(download.GitHubRF):
    def __init__(self):
        super().__init__({"repository": REPOSITORY, "tag": TAG, "release_id": RELEASE_ID, "access": "public"})
        self.direct = False

    def _validate_asset(self, row):
        if not self.direct:
            try:
                return super()._validate_asset(row)
            except RuntimeError:
                # Metadata may be rate-limited. Names, IDs, sizes and file digests remain pinned.
                self.direct = True
                print("Using the public GitHub release attachment links.", flush=True)
        self.release_id = RELEASE_ID
        return row["asset_id"]

    def _binary(self, row, offset):
        if not self.direct:
            try:
                return super()._binary(row, offset)
            except RuntimeError:
                self.direct = True
        url = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/" + quote(row["name"], safe="")
        request = {"Accept-Encoding": "identity", "User-Agent": "IncFinder-portable"}
        if offset:
            request["Range"] = f"bytes={offset}-"
        try:
            return self.opener.open(Request(url + "?download=" + str(time.time_ns()), headers=request), timeout=60)
        except HTTPError as error:
            status = error.code
            location = error.headers.get("Location", "")
            error.close()
            if status not in (301, 302, 303, 307, 308):
                raise RuntimeError(f"GitHub rejected {row['name']} (HTTP {status}). Retry to resume.") from None
        return self.opener.open(Request(download._safe_storage(location), headers=request), timeout=90)

    def get_part(self, cache, row):
        complete_path, _, _ = download._paths(cache, row)
        if install._regular(complete_path) is not None:
            try:
                install._check(complete_path, row, row["name"])
                return
            except ValueError:
                # Only replace a regular, single-link file in this release's managed cache.
                complete_path.unlink()
        for attempt in range(3):
            try:
                _, receipt = self.fetch(cache, row)
                print(f"{row['name']} verified ({receipt['resumed_from_bytes']:,} bytes resumed).", flush=True)
                return
            except (RuntimeError, ValueError, OSError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)


def complete(target):
    model, _, _, _ = install._sources()
    return all((target / name).is_file() for name in model)


def ensure(target, *, verify_existing=True):
    model, rf, _, _ = install._sources()
    if complete(target):
        if verify_existing:
            install.verify_models(target)
        return
    if os.environ.get("INCFINDER_OFFLINE") == "1":
        raise RuntimeError("The offline model directory is incomplete. Start once online to initialize it.")
    cache = data_home() / "downloads/rc2"
    cache.mkdir(parents=True, exist_ok=True)
    with _writer_lease(cache, ".portable-download.lock"):
        if complete(target):
            install.verify_models(target)
            return
        missing = {name for name in model if not (target / name).is_file()}
        rf_names = {Path(name).name for name in missing if name.startswith("rf/")}
        rows = [row for row in signed_rows(rf) if row["name"].split(".part-")[0] in rf_names]
        staged = sum(model[name]["size_bytes"] for name in missing)
        incoming = sum(download.rf_required_bytes(cache, row) for row in rows)
        upstream = sum(model[name]["size_bytes"] for name in missing if not name.startswith("rf/"))
        _disk_budget({"published models": _nearest_directory(target), "download cache": cache},
                     {"model setup": {"published models": (staged, staged),
                                      "download cache": (incoming + 2 * upstream, incoming + 2 * upstream)}})
        if upstream:
            authorize(prompt=True)
        receiver = PublicRF()
        for index, row in enumerate(rows, 1):
            print(f"RF {index}/{len(rows)}: {row['name']}", flush=True)
            receiver.get_part(cache, row)
        part_dir = cache / "rf" if rows else None
        install.install_models(target, parts_dir=part_dir, cache_dir=cache / "upstream", download_ntv3=True,
                               progress=lambda line: print(line, flush=True))
        for row in rows:
            path, _, _ = download._paths(cache, row)
            if install._regular(path) is not None:
                path.unlink()
        print("Verified models: " + str(target), flush=True)
