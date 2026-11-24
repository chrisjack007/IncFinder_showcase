"""Build reproducible, release-bound attachments with Go's standard library."""
import argparse
import hashlib
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAGS = ("v0.5.0rc2", "v0.5.0rc2-portable.1")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(out, go):
    for tag in TAGS:
        (out / tag).mkdir(parents=True, exist_ok=True)
    for system, script, binary in (
        ("windows", "IncFinder-Start.cmd", "incfinder-start-windows-amd64.exe"),
        ("linux", "incfinder-start.sh", "incfinder-start-linux-amd64"),
    ):
        artifact = out / TAGS[0] / binary
        subprocess.run([go, "build", "-trimpath", "-buildvcs=false", "-ldflags", "-s -w -buildid=",
                        "-o", str(artifact), "."], cwd=HERE, check=True,
                       env=dict(os.environ, GOOS=system, GOARCH="amd64", GOAMD64="v1", CGO_ENABLED="0",
                                GOPROXY="off", GOTOOLCHAIN="local"))
        data = artifact.read_bytes()
        template = (HERE / ("start.cmd.in" if system == "windows" else "start.sh.in")).read_text(encoding="utf8")
        for tag in TAGS:
            if tag != TAGS[0]:
                (out / tag / binary).write_bytes(data)
            entry = template.replace("@TAG@", tag).replace("@HASH@", sha(data))
            if system == "windows":
                entry = entry.replace("\n", "\r\n")
            (out / tag / script).write_bytes(entry.encode("utf8"))
    for tag in TAGS:
        root = out / tag
        names = ["IncFinder-Start.cmd", "incfinder-start.sh", "incfinder-start-linux-amd64", "incfinder-start-windows-amd64.exe"]
        (root / "LAUNCHER-SHA256SUMS").write_bytes(
            "".join(f"{sha((root / name).read_bytes())}  {name}\n" for name in names).encode("ascii"))
        print(tag, sha((root / "LAUNCHER-SHA256SUMS").read_bytes()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    build(args.output.resolve(), args.go)
