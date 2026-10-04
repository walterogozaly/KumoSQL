"""Copy the open-source BigQuery projects listed in ``tests/fixtures/bq_corpora/sources.json`` into that folder.

Each project is cloned at its pinned commit and only the listed paths (and the licence) are kept, under
``tests/fixtures/bq_corpora/<name>/``. ``sample`` keeps that many matching files, spread evenly over the sorted list, and ``flatten`` names each file after its
folders (``bases/a/b/publish.sql`` becomes ``a__b.sql``), for repositories whose files all share one name.

    python tools/fetch_bq_corpora.py [--only NAME]
    python tools/fetch_bq_corpora.py --check [--only NAME]

``sha256`` in ``sources.json`` is the digest of the files kept (see :func:`digest`); ``--check`` recomputes it from the
copies on disk and fails when a file was edited, added or removed, so a pinned commit and the bytes under test agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CORPORA = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bq_corpora"


def digest(folder: Path) -> str:
    """SHA-256 over every file under ``folder``: for each, in path order, its relative path, a NUL and its own SHA-256."""

    lines = [
        f"{path.relative_to(folder).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    ]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def _matches(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern)) if any(c in pattern for c in "*?[") else [root / pattern]


def fetch(project: dict, workdir: Path) -> int:
    checkout = workdir / project["name"]
    subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", project["repo"], str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", project["commit"]], check=True)
    root = checkout / project["root"]
    target = CORPORA / project["name"]
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    count = 0
    for pattern in project["copy"]:
        found = [path for path in _matches(root, pattern) if path.exists()]
        if "sample" in project and len(found) > project["sample"]:
            step = len(found) / project["sample"]
            found = [found[int(i * step)] for i in range(project["sample"])]
        for path in found:
            relative = path.relative_to(root)
            if project.get("flatten"):
                relative = Path("__".join(relative.parent.parts) + path.suffix)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, destination)
                count += sum(1 for p in path.rglob("*") if p.is_file())
            else:
                shutil.copy2(path, destination)
                count += 1
    licence = next(checkout.glob("LICEN[CS]E*"))
    shutil.copy2(licence, target / "LICENSE")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="fetch only this project")
    parser.add_argument("--check", action="store_true", help="compare the copies on disk with the pinned SHA-256, fetch nothing")
    args = parser.parse_args(argv)
    sources = json.loads((CORPORA / "sources.json").read_text())
    if args.check:
        bad = [p["name"] for p in sources["projects"] if (not args.only or p["name"] == args.only) and digest(CORPORA / p["name"]) != p.get("sha256")]
        print("changed or unpinned: " + ", ".join(bad) if bad else "every copy matches its pinned SHA-256")
        return 1 if bad else 0
    with tempfile.TemporaryDirectory() as workdir:
        for project in sources["projects"]:
            if args.only and project["name"] != args.only:
                continue
            print(f"{project['name']}: {fetch(project, Path(workdir))} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
