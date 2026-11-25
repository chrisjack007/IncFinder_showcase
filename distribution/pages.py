"""Render or check the shared first-run platform table in the four candidate documents."""

import argparse
from html import escape

from release import CFG, ROOT

URL = "https://github.com/" + CFG["repository"] + "/releases/download/" + CFG["tag"] + "/"
ROWS = [
    ("Windows", "Windows", "windows"),
    ("WSL2", "WSL2", "linux"),
    ("原生 Linux", "Native Linux", "linux"),
    ("Docker", "Docker", "docker"),
]
HEAD = (
    ("平台（x86-64）", "Platform (x86-64)"),
    ("下载启动器", "Download the launcher"),
    ("打开菜单", "Open the menu"),
)


def dual(zh, en):
    return f'<span lang="zh-CN">{zh}</span><span lang="en">{en}</span>'


def table(lang):
    html = lang is None

    def words(pair):
        return dual(*pair) if html else pair[lang]

    def code(text):
        return f"<code>{escape(text)}</code>" if html else f"`{text}`"

    def link(name, title):
        return f'<a href="{URL}{name}">{title}</a>' if html else f"[{title}]({URL}{name})"

    head = [words(p) for p in HEAD]
    tab = (
        ["<table><thead><tr>" + "".join(f'<th scope="col">{v}</th>' for v in head) + "</tr></thead><tbody>"]
        if html
        else ["| " + " | ".join(head) + " |", "| --- | --- | --- |"]
    )
    for zh, en, system in ROWS:
        title = words((zh, en)) if zh != en else en
        if system == "docker":
            a = link(CFG["launchers"]["windows"], words(("Windows 宿主", "Windows host")))
            b = link(CFG["launchers"]["linux"], words(("Linux / WSL2 宿主", "Linux / WSL2 host")))
            action = words(
                (
                    "启动 Linux 容器引擎，运行宿主启动器，在运行方式中选择 2。",
                    "Start the Linux container engine. Run the host launcher and choose runtime 2.",
                )
            )
            links = a + " · " + b
        else:
            binary = CFG["launchers"][system]
            links = link(binary, binary)
            if system == "windows":
                action = words(
                    (
                        "双击下载的程序，按 Enter 选择本机运行。",
                        "Double-click the downloaded program. Press Enter for native.",
                    )
                )
            else:
                mode = code("chmod +x " + binary)
                run = code("./" + binary)
                action = words((f"在终端运行 {mode}，再运行 {run}。", f"Run {mode}, then {run}."))
        cells = [title, links, action]
        tab.append(
            f'<tr><th scope="row">{title}</th><td>{links}</td><td>{action}</td></tr>'
            if html
            else "| " + " | ".join(cells) + " |"
        )
    if html:
        tab.append("</tbody></table>")
    return "\n".join(tab)


def render(check=False):
    for name in ("index.html", "README.md", "README.zh-CN.md", "docs/dist.1.md"):
        p = ROOT / name
        current = p.read_text(encoding="utf8")
        body = current
        for label, lang in (("html", None), ("en", 1), ("zh", 0)):
            start, end = f"<!-- dist-table-{label} -->", f"<!-- /dist-table-{label} -->"
            if start in body:
                if body.count(start) != 1 or body.count(end) != 1:
                    raise ValueError("Ambiguous table in " + name)
                a, b = body.split(start)
                _, c = b.split(end)
                body = a + start + "\n" + table(lang) + "\n" + end + c
        if "<!-- dist-table-" not in body or (
            name.endswith("dist.1.md") and body.count("<!-- dist-table-") != 2
        ):
            raise ValueError("Missing platform table: " + name)
        if body != current:
            if check:
                raise ValueError("Stale platform table: " + name)
            p.write_bytes(body.encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    render(parser.parse_args().check)
