"""Copy the open-source BigQuery projects listed in ``tests/fixtures/bq_corpora/sources.json`` into that folder.

Each project is cloned at its pinned commit and only the listed paths (and the licence) are kept, under
``tests/fixtures/bq_corpora/<name>/``. ``sample`` keeps that many matching files, spread evenly over the sorted list, and ``flatten`` names each file after its
folders (``bases/a/b/publish.sql`` becomes ``a__b.sql``), for repositories whose files all share one name.

    python tools/fetch_bq_corpora.py [--only NAME]
    python tools/fetch_bq_corpora.py --check [--only NAME]
    python tools/fetch_bq_corpora.py --check-sources [--only NAME]

``sha256`` in ``sources.json`` is the digest of the files kept (see :func:`digest`); ``--check`` recomputes it from the
copies on disk and fails when a file was edited, added or removed, so a pinned commit and the bytes under test agree.
``--check-sources`` goes further and needs GitHub: it clones each pinned commit and rebuilds the project in a temporary
folder, so the pin is shown to be what the commit yields, not only what the copy says. A project with an ``adapter``
(queries embedded in Python and notebooks, turned into ``.sql`` files by ``tools/<adapter>.py``) also pins the SHA-256 of
every upstream file the adapter reads (``upstream``).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
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


def clone(project: dict, workdir: Path) -> Path:
    """A checkout of the project's repository at its pinned commit."""

    checkout = workdir / project["name"]
    subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", project["repo"], str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", project["commit"]], check=True)
    return checkout


def build(project: dict, checkout: Path, target: Path) -> int:
    """Write the project's files into ``target`` from a checkout; returns how many files were kept (licence excluded)."""

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    count = 0
    if "adapter" in project:  # queries embedded in Python and notebooks, adapted to SQL files by tools/<adapter>.py
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        module = importlib.import_module(project["adapter"])
        for path in module.upstream_files():
            found = hashlib.sha256((checkout / path).read_bytes()).hexdigest()
            if found != project["upstream"].get(path):
                raise SystemExit(f"{project['name']}: {path} at the pinned commit is not the file the adapter was written for")
        for name, text in module.adapt(checkout).items():
            (target / name).write_text(text, encoding="utf-8")
            count += 1
    root = checkout / project["root"]
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


def fetch(project: dict, workdir: Path) -> int:
    return build(project, clone(project, workdir), CORPORA / project["name"])


def check_sources(project: dict, workdir: Path) -> bool:
    """Rebuild the project from its pinned commit, then compare with the pinned SHA-256 and with the copy on disk."""

    rebuilt = workdir / "rebuilt" / project["name"]
    build(project, clone(project, workdir), rebuilt)
    return digest(rebuilt) == project.get("sha256") == digest(CORPORA / project["name"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="fetch only this project")
    parser.add_argument("--check", action="store_true", help="compare the copies on disk with the pinned SHA-256, fetch nothing")
    parser.add_argument("--check-sources", action="store_true",
                        help="clone each pinned commit (needs GitHub), rebuild the project and compare it with the pinned SHA-256")
    args = parser.parse_args(argv)
    sources = json.loads((CORPORA / "sources.json").read_text())
    chosen = [p for p in sources["projects"] if not args.only or p["name"] == args.only]
    if args.check:
        bad = [p["name"] for p in chosen if digest(CORPORA / p["name"]) != p.get("sha256")]
        print("changed or unpinned: " + ", ".join(bad) if bad else "every copy matches its pinned SHA-256")
        return 1 if bad else 0
    with tempfile.TemporaryDirectory() as workdir:
        if args.check_sources:
            bad = [p["name"] for p in chosen if not check_sources(p, Path(workdir))]
            print("rebuilt from the pinned commit differently: " + ", ".join(bad) if bad
                  else "every project rebuilds from its pinned commit to its pinned SHA-256")
            return 1 if bad else 0
        for project in chosen:
            print(f"{project['name']}: {fetch(project, Path(workdir))} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
