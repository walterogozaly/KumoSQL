"""Docs stay navigable: every page is in the docs index, and relative links point at files and headings that exist."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lstrip("#").strip().lower()).replace(" ", "-")


def _lines_outside_code(path: Path):
    fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            fence = not fence
        elif not fence:
            yield number, line


def _anchors(path: Path) -> set[str]:
    return {_slug(line) for _, line in _lines_outside_code(path) if line.startswith("#")}


def test_every_docs_page_is_in_the_index():
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in sorted(DOCS.glob("*.md")) if p.name != "README.md" and f"]({p.name})" not in index]
    assert not missing, f"add to docs/README.md: {', '.join(missing)}"


def test_relative_links_resolve():
    broken = []
    for page in [ROOT / "README.md", ROOT / "benchmarks" / "README.md", *sorted(DOCS.glob("*.md"))]:
        for number, line in _lines_outside_code(page):
            for target in re.findall(r"\]\(([^)\s]+)\)", line):
                if re.match(r"^[a-z]+:", target):
                    continue
                file, _, anchor = target.partition("#")
                resolved = (page.parent / file).resolve() if file else page
                if not resolved.exists():
                    broken.append(f"{page.relative_to(ROOT)}:{number}: {target} (no such file)")
                elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                    broken.append(f"{page.relative_to(ROOT)}:{number}: {target} (no such heading)")
    assert not broken, "\n".join(broken)
