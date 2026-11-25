"""Update the delivery layer of the signed portable archives; leave their runtimes intact."""

import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

from release import CFG, OLD, ROOT, TRUST, accepted_index, digest, encode, signed_sums, write_sums

STAMP = (2026, 1, 1, 0, 0, 0)


def member_bytes(path, names):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return {name: z.read(name) for name in names}
    result = {}
    with tarfile.open(path, "r|gz") as t:
        for m in t:
            name = m.name.removeprefix("./")
            if name in names:
                result[name] = t.extractfile(m).read()
    if set(result) != set(names):
        raise ValueError("Missing portable members: " + str(set(names) - set(result)))
    return result


def zip_info(name):
    item = zipfile.ZipInfo(name, STAMP)
    item.create_system = 3
    item.external_attr = 0o100644 << 16
    item.compress_type = zipfile.ZIP_DEFLATED
    return item


def repack(source, target, changes):
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(fd)
    try:
        if source.suffix == ".zip":
            with zipfile.ZipFile(source) as src, zipfile.ZipFile(tmp, "w") as dst:
                for member in src.infolist():
                    if member.filename in changes:
                        continue
                    info = copy.copy(member)
                    info._compresslevel = 3
                    with src.open(member) as inp, dst.open(info, "w") as out:
                        shutil.copyfileobj(inp, out, 2 << 20)
                for name, data in sorted(changes.items()):
                    dst.writestr(zip_info(name), data)
        else:
            with (
                open(tmp, "wb") as raw,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=3) as gz,
            ):
                with tarfile.open(source, "r|gz") as src, tarfile.open(fileobj=gz, mode="w|") as dst:
                    for member in src:
                        if member.name.removeprefix("./") not in changes:
                            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
                    for name, data in sorted(changes.items()):
                        member = tarfile.TarInfo(name)
                        member.mode, member.size = 0o644, len(data)
                        dst.addfile(member, io.BytesIO(data))
        if not 0 < os.path.getsize(tmp) < 2 * 1024**3:
            raise ValueError("Release asset size out of range")
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def amend_app(source):
    text = source.decode()
    before = "ensure(model_dir(args))"
    after = 'ensure(model_dir(args), parts_dir=Path(parts).expanduser().absolute() if (parts := option(args, "--parts-dir")) else None)'
    default = 'return Path(value).expanduser().absolute() if value else data_home() / "models/0.5.0rc2"'
    if text.count(before) != 1 or text.count(default) != 1:
        raise ValueError("Unexpected portable app")
    return (
        text.replace(before, after)
        .replace(
            default,
            """if value:
        return Path(value).expanduser().absolute()
    from models import complete
    legacy = data_home() / "models/0.5.0rc2"
    if complete(legacy):
        return legacy
    import json
    key = json.loads((HERE / "bundle.json").read_bytes())["model_set_sha256"]
    return data_home() / "models" / key""",
        )
        .encode()
    )


def build(args):
    out = args.out / CFG["tag"]
    out.mkdir(parents=True, exist_ok=True)
    internal = args.out / "internal"
    internal.mkdir(exist_ok=True)
    idx = accepted_index(args.out)
    index = json.loads(idx.read_bytes())
    old_sums = signed_sums(TRUST / "portable-SHA256SUMS", TRUST / "allowed_signers")
    rc2 = signed_sums(TRUST / "rc2-SHA256SUMS", TRUST / "allowed_signers")
    sources, data, evidence = {}, {}, {}
    for platform, name in OLD.items():
        path = args.portable / name
        if digest(path) != old_sums[name]:
            raise ValueError("Portable source differs: " + name)
        sources[platform] = path
        sbom = f"docs/SBOM-{platform}.cdx.json"
        data[platform] = member_bytes(
            path,
            ["portable/bundle.json", "portable/app.py", sbom, "README.md", "docs/example-reference.json"],
        )
        if (
            hashlib.sha256(data[platform]["docs/example-reference.json"]).hexdigest()
            != rc2["example-reference.json"]
        ):
            raise ValueError("Example reference differs: " + platform)
        evidence[sbom] = data[platform][sbom]
    markers = {key: json.loads(val["portable/bundle.json"]) for key, val in data.items()}
    if args.layout == "shared" and {k: v for k, v in markers["linux"].items() if k != "platform"} != {
        k: v for k, v in markers["wsl2"].items() if k != "platform"
    }:
        raise ValueError("Linux and WSL2 runtime contracts differ")
    assets = []
    transport = (
        (ROOT / "distribution/models.py").read_bytes().replace(b"@RF_INDEX_SHA256@", digest(idx).encode())
    )
    for platform, name in CFG["packages"].items():
        if platform == "wsl2" and args.layout == "shared":
            continue
        targets = ["linux", "wsl2"] if platform == "linux" and args.layout == "shared" else [platform]
        marker = dict(
            markers[platform],
            distribution=CFG["tag"],
            supported_targets=targets,
            model_set_sha256=index["model_set_sha256"],
        )
        readme = data[platform]["README.md"].decode().replace(CFG["upstream"], CFG["tag"])
        readme = readme.split("Official upstream access and release verification")[0]
        readme += (
            "\nSupported: "
            + ", ".join(targets)
            + "\nhttps://github.com/"
            + CFG["repository"]
            + "/releases/tag/"
            + CFG["tag"]
            + "\n"
        )
        changes = {
            "portable/bundle.json": encode(marker),
            "portable/app.py": amend_app(data[platform]["portable/app.py"]),
            "portable/models.py": transport,
            "portable/trust/rf-index.json": idx.read_bytes(),
            "portable/trust/rf-index.json.sig": idx.with_suffix(".json.sig").read_bytes(),
            "README.md": readme.encode(),
        }
        for supported in targets:
            path = f"docs/SBOM-{supported}.cdx.json"
            sbom = json.loads(evidence[path])
            sbom["metadata"]["component"].update(name=name, version=CFG["tag"][1:])
            changes[path] = encode(sbom)
        for supported in targets:
            file = f"docs/SBOM-{supported}.cdx.json"
            evidence[file] = changes[file]
        repack(sources[platform], out / name, changes)
        assets.append(out / name)
        print("Built", name, flush=True)
    for name in (
        "MODEL_CARD.en.md",
        "MODEL_CARD.zh-CN.md",
        "MODEL-LICENSE.md",
        "holdout_metrics.json",
        "per_class_metrics.tsv",
        "public-source-tree.json",
        "release-manifest.json",
        "example-reference.json",
        *sorted(n for n in rc2 if n.startswith("SBOM-")),
    ):
        path = args.legacy / name
        if digest(path) != rc2[name]:
            raise ValueError("Scientific evidence changed: " + name)
        evidence["science/" + name] = path.read_bytes()
    evidence["science/LICENSE"] = (ROOT / "NT_V3_650M_TSRF_SW/LICENSE").read_bytes()
    evidence["provenance.json"] = (ROOT / "distribution/provenance.json").read_bytes()
    evidence["README.md"] = (ROOT / "docs/distribution-evidence.md").read_bytes()
    evidence["distribution.json"] = (ROOT / "launcher/distribution.json").read_bytes()
    evidence["rf-index.json"] = idx.read_bytes()
    evidence["rf-index.json.sig"] = idx.with_suffix(".json.sig").read_bytes()
    with zipfile.ZipFile(out / CFG["evidence"], "w") as z:
        for name, raw in sorted(evidence.items()):
            z.writestr(zip_info(name), raw)
    write_sums(internal / "RUNTIME-SHA256SUMS", [*assets, idx])
    print("Inner manifest ready:", digest(internal / "RUNTIME-SHA256SUMS"), flush=True)
