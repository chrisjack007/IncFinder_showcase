"""Relabel an exported OCI archive without changing the accepted image or its layers."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

IMAGE_ID = "sha256:914adae6b696d804ee749f8b130e03aa9965324372f307e79a59b25b33351230"
TAG = "incfinder-portable:0.5.0rc2-p1"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def export(source, destination, receipt):
    if source.resolve() == destination.resolve():
        raise ValueError("Use a separate portable image archive")
    verified = json.loads(receipt.read_text(encoding="utf8"))
    if verified["image_index"] != IMAGE_ID or digest(source) != verified["sha256"]:
        raise ValueError("Unexpected source Docker archive")
    with tarfile.open(source) as original, tarfile.open(destination, "w") as output:
        for member in original:
            stream = original.extractfile(member) if member.isfile() else None
            if member.name in ("index.json", "manifest.json"):
                doc = json.load(stream)
                if member.name == "index.json":
                    entries = doc["manifests"]
                    if len(entries) != 1 or entries[0]["digest"] != IMAGE_ID:
                        raise ValueError("Unexpected OCI index")
                    entries[0]["annotations"].update({"io.containerd.image.name": "docker.io/library/" + TAG,
                                                      "org.opencontainers.image.ref.name": TAG.split(":")[1]})
                else:
                    if len(doc) != 1 or doc[0]["RepoTags"] != [verified["repo_tag"]]:
                        raise ValueError("Unexpected source Docker tag")
                    doc[0]["RepoTags"] = [TAG]
                payload = json.dumps(doc, separators=(",", ":")).encode("utf8")
                member.size = len(payload)
                stream = io.BytesIO(payload)
            output.addfile(member, stream)
    verified.update(source_archive_sha256=verified["sha256"], source_repo_tag=verified["repo_tag"],
                    sha256=digest(destination), repo_tag=TAG, archive_bytes=destination.stat().st_size)
    (destination.parent / "image.json").write_text(json.dumps(verified, indent=2) + "\n", encoding="utf8")
    print(TAG, verified["image_index"], verified["sha256"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "destination", "receipt"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    export(args.source, args.destination, args.receipt)
