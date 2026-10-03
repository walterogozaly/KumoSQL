"""Docs stay navigable: every page is in the docs index, and relative links point at files and headings that exist."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SIMPLE_DOCS = ROOT / "docs_simple"


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


@pytest.mark.parametrize("folder", [DOCS, SIMPLE_DOCS], ids=["full", "simple"])
def test_every_docs_page_is_in_the_index(folder):
    index = (folder / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in sorted(folder.glob("*.md")) if p.name != "README.md" and f"]({p.name})" not in index]
    assert not missing, f"add to {folder.name}/README.md: {', '.join(missing)}"
    assert "](evals/README.md)" in index, f"{folder.name}/README.md must link the evals folder"


@pytest.mark.parametrize("folder", [DOCS, SIMPLE_DOCS], ids=["full", "simple"])
def test_every_eval_page_is_in_the_evals_index(folder):
    index = (folder / "evals" / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in sorted((folder / "evals").glob("*.md")) if p.name != "README.md" and f"]({p.name})" not in index]
    assert not missing, f"add to {folder.name}/evals/README.md: {', '.join(missing)}"


def test_every_full_docs_page_has_a_simple_companion():
    expected = {p.relative_to(DOCS) for p in DOCS.rglob("*.md")}
    actual = {p.relative_to(SIMPLE_DOCS) for p in SIMPLE_DOCS.rglob("*.md") if "benchmarks" not in p.relative_to(SIMPLE_DOCS).parts}
    missing = sorted(str(p) for p in expected - actual)
    orphaned = sorted(str(p) for p in actual - expected)
    assert not missing, f"add simple companions: {', '.join(missing)}"
    assert not orphaned, f"simple pages without full references: {', '.join(orphaned)}"


def test_simple_companions_link_to_the_full_reference():
    for full in [*sorted(DOCS.rglob("*.md")), ROOT / "benchmarks" / "README.md"]:
        relative = full.relative_to(DOCS) if full.is_relative_to(DOCS) else full.relative_to(ROOT)
        simple = SIMPLE_DOCS / relative
        targets = [
            target.partition("#")[0]
            for _, line in _lines_outside_code(simple)
            for target in re.findall(r"\]\(([^)\s]+)\)", line)
            if not re.match(r"^[a-z]+:", target)
        ]
        assert any((simple.parent / target).resolve() == full for target in targets), f"{simple.relative_to(ROOT)} must link {full.relative_to(ROOT)}"


def test_every_results_file_is_in_the_evals_index():
    index = (DOCS / "evals" / "README.md").read_text(encoding="utf-8")
    missing = [p.stem for p in sorted((ROOT / "benchmarks" / "results").glob("*.json")) if f"`{p.stem}`" not in index]
    assert not missing, f"name in docs/evals/README.md: {', '.join(missing)}"


def test_relative_links_resolve():
    broken = []
    for page in [ROOT / "README.md", ROOT / "benchmarks" / "README.md", *sorted(DOCS.rglob("*.md")), *sorted(SIMPLE_DOCS.rglob("*.md"))]:
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
