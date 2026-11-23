"""Portable command entry point for the signed IncFinder 0.5.0rc2 runtime."""
import datetime
import os
from pathlib import Path
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bootstrap import data_home


def science(args):
    return subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-m", "incfinder", *args]).returncode


def option(args, name):
    for i, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name:
            if i + 1 == len(args):
                raise ValueError("Missing value for " + name)
            return args[i + 1]
    return None


def choose_input():
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    try:
        return filedialog.askopenfilename(parent=root, title="IncFinder · FASTA / FASTQ",
            filetypes=[("FASTA / FASTQ", "*.fa *.fna *.fasta *.fq *.fastq *.gz"), ("All files", "*.*")])
    finally:
        root.destroy()


def auto_output(path):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    parent = Path(os.environ.get("INCFINDER_RESULTS_DIR", Path.home() / "IncFinder/results")).expanduser()
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{path.stem[:60]}-{stamp}-{uuid.uuid4().hex[:6]}"


def main(args):
    picker = not args and os.name == "nt"
    if picker:
        selection = choose_input()
        if not selection:
            return 0
        args = [selection]
    if not args or args[0] in ("-h", "--help", "help"):
        print("IncFinder 0.5.0rc2 portable\n"
              "  incfinder FILE [--threshold-mode ct|u05] [--output DIRECTORY]\n"
              "  incfinder models init [--model-dir DIRECTORY]\n"
              "  incfinder auth login\n"
              "First analysis downloads the verified models. Later analyses can run offline.\n", flush=True)
        return science(["--help"])
    if args[:2] == ["auth", "login"]:
        from models import authorize
        authorize(prompt=True, force=True)
        return 0
    if args[:2] == ["portable", "ready"]:
        from models import complete
        return 0 if complete(model_dir(args)) else 3
    if args[:2] == ["models", "init"]:
        from models import ensure
        ensure(model_dir(args))
        return 0
    if len(args) > 1 and args[0] == "models" and args[1] == "verify" and not option(args, "--model-dir"):
        return science([*args, "--model-dir", str(model_dir(args))])
    if args[0] == "predict" and any(x in args for x in ("-h", "--help")):
        return science(args)
    if args[0].split("=", 1)[0] in ("--input", "--i", "-i"):
        args = ["predict", *args]
    quick = Path(args[0]).is_file()
    if not quick and args[0] != "predict":
        return science(args)
    from models import ensure
    target = model_dir(args)
    if quick:
        source = Path(args[0]).resolve()
        args = ["predict", str(source), *args[1:]]
        if not any(option(args, flag) for flag in ("--output", "--o", "-o")):
            args.extend(["--output", str(auto_output(source))])
        if "--allow-encoder-drift" not in args:
            args.append("--allow-encoder-drift")
    if not option(args, "--model-dir"):
        args.extend(["--model-dir", str(target)])
    if "--dry-run" not in args:
        # The signed prediction CLI verifies every model byte before inference.
        ensure(target, verify_existing=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    result = science(args)
    output = option(args, "--output") or option(args, "--o") or option(args, "-o")
    if not result and output:
        print("Results: " + str(Path(output).resolve()), flush=True)
    return result


def model_dir(args):
    value = option(args, "--model-dir") or os.environ.get("INCFINDER_MODEL_DIR")
    return Path(value).expanduser().absolute() if value else data_home() / "models/0.5.0rc2"


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("IncFinder: " + str(error))
