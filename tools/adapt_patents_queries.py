"""Turn the BigQuery queries embedded in google/patents-public-data's notebooks and Python files into ``.sql`` files.

The repository holds one ``.sql`` file; its other queries are triple-quoted strings in Python and notebook cells, with
Python format fields (``{}``, ``{YEAR}``, ``%s``, ``[max_results]``) where values go in at run time. A string cannot be
read as SQL until those fields are replaced, so each query is copied verbatim and only its fields are replaced, by the
value the same code or cell gives when it has one and by a synthetic one (``my-project``, ``my_dataset``) otherwise.
Every file starts with comment lines naming the source file, the string and each replacement.

``python tools/fetch_bq_corpora.py`` runs :func:`adapt` on the pinned checkout; ``--check-sources`` runs it again and
compares the result with the pinned SHA-256. Nothing here reads a network or a credential.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LITERAL = re.compile(r"""(?s)[rRbf]*(\"\"\"|''')(.*?)\1""")

PROJECT, DATASET = "my_project_name", "claims_analysis"  # the values claim_text_extraction.ipynb gives its own variables
PUBLICATIONS = "('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1')"  # BERT_For_Patents.ipynb's test_pubs

# name, upstream file, cell (notebooks) or None, which string, replacements (old, new, how many times old appears)
QUERIES = [
    ("bert_abstracts_by_publication", "examples/BERT_For_Patents.ipynb", 14, 0, [("{}", PUBLICATIONS, 1)]),
    ("bert_abstracts_by_cpc_and_word", "examples/BERT_For_Patents.ipynb", 19, 0,
     [("cpc.code = '{}'", "cpc.code = 'B41J2/165'", 1), ("{}", "priming", 1)]),
    ("bert_cpc_class_sample", "examples/BERT_For_Patents.ipynb", 23, 0, [("{}", "200", 1)]),
    ("bert_document_claims", "examples/Document_representation_from_BERT.ipynb", 12, 0,
     [("{}", "@js", 1), ("{}", "('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1', 'US-10722718-B2')", 1)]),
    ("claim_text_publications", "examples/claim-text/claim_text_extraction.ipynb", 10, 0,
     [("{}.{}.{}", f"{PROJECT}.{DATASET}.claim_text_publications", 1)]),
    ("claim_text_sample", "examples/claim-text/claim_text_extraction.ipynb", 12, 0, [("{}.{}", f"{PROJECT}.{DATASET}", 1)]),
    ("claim_text_first_claim", "examples/claim-text/claim_text_extraction.ipynb", 18, 0,
     [("{}", "@js:17", 1), ("{}.{}", f"{PROJECT}.{DATASET}", 1)]),
    ("claim_text_first_claim_sample", "examples/claim-text/claim_text_extraction.ipynb", 19, 0,
     [("{}.{}.{}", f"{PROJECT}.{DATASET}.20k_G06F_pubs_after_1994_split_first_claim", 1)]),
    ("claim_text_words_by_year", "examples/claim-text/claim_text_extraction.ipynb", 23, 0,
     [("%s.%s.%s", f"{PROJECT}.{DATASET}.20k_G06F_pubs_after_1994_split_first_claim", 1)]),
    ("claim_text_elements_by_year", "examples/claim-text/claim_text_extraction.ipynb", 26, 0,
     [("%s.%s.%s", f"{PROJECT}.{DATASET}.20k_G06F_pubs_after_1994_split_first_claim", 1)]),
    ("claim_breadth_training_data", "models/claim_breadth/preprocess.py", None, 0,
     [("{YEAR}", "2005", 2), ("{CPCS}", "('A', 'B')", 2), ("{KEEP_PCT}", "0.2", 1)]),
    ("claim_breadth_fake_test_data", "models/claim_breadth/preprocess_test.py", None, 0, [("{half_max}", "50", 2)]),
    ("landscape_seed_publications", "models/landscaping/expansion.py", None, 0, [("{}", "'8000000', '10722718'", 1)]),
    ("landscape_us_patent_count", "models/landscaping/expansion.py", None, 1, []),
    ("landscape_cpc_counts", "models/landscaping/expansion.py", None, 2, [("{}", "1=1", 1)]),
    ("landscape_expand_l2", "models/landscaping/expansion.py", None, 3, [("{}", "patents._l2_tmp", 1)]),
    ("landscape_expand_l1", "models/landscaping/expansion.py", None, 4,
     [("{}", "'G06F', 'H04L'", 1), ("{}", "patents._l1_tmp", 1)]),
    ("landscape_anti_seed", "models/landscaping/expansion.py", None, 5,
     [("{}", "patents.antiseed_tmp", 1), ("{}", "15000", 1)]),
    ("landscape_training_data", "models/landscaping/expansion.py", None, 6, [("{}", "patents._tmp_training", 1)]),
    ("dataset_docs_join_sample", "tools/generate_dataset_docs.py", None, 0,
     [("{first_column}", "publication_number", 2), ("{first_table}", "patents.publications", 1),
      ("{second_column}", "family_id", 1), ("{second_table}", "patents.publications", 1)]),
    ("dataset_docs_join_grouped", "tools/generate_dataset_docs.py", None, 1,
     [("{group_by}", "country_code", 1), ("{first_column}", "publication_number", 2), ("{first_table}", "patents.publications", 1),
      ("{second_column}", "family_id", 1), ("{second_table}", "patents.publications", 1)]),
]

SIMILARITY = {  # patent_set_expansion.ipynb, cell 14: the similarity query takes [placeholders] that str.replace fills in
    "[cluster_center]": "[0.1, 0.2, 0.3]", "[cluster_label]": "1", "[max_distance]": "0.5", "[max_results]": "100",
    "[cluster_input_list]": "('US-8000000-B2', 'US-2007186831-A1')",
}


def strings(text: str, *, anywhere: bool) -> list[str]:
    """The triple-quoted strings of ``text``; unless ``anywhere``, only those that hold a SELECT and a FROM."""

    found = [m.group(2) for m in LITERAL.finditer(text)]
    return found if anywhere else [s for s in found if re.search(r"\bselect\b", s, re.I) and re.search(r"\bfrom\b", s, re.I)]


def _source_text(checkout: Path, path: str, cell: int | None) -> str:
    text = (checkout / path).read_text(encoding="utf-8")
    if cell is None:
        return text
    return "".join(json.loads(text)["cells"][cell]["source"])


def _header(path: str, cell: int | None, picked: int, note: str, replacements: list[str]) -> str:
    where = f"{path}, cell {cell}" if cell is not None else path
    lines = [f"-- Adapted from google/patents-public-data: {where}, string {picked}. {note}"]
    lines += [f"-- Replaced {item}" for item in replacements]
    return "\n".join(lines) + "\n"


def upstream_files() -> list[str]:
    """The files of the repository that the adapted queries are read from (their SHA-256 is pinned in ``sources.json``)."""

    return sorted({path for _, path, _, _, _ in QUERIES} | {"examples/patent_set_expansion.ipynb"})


def adapt(checkout: Path) -> dict[str, str]:
    """``{file name: text}`` for every adapted query, read from a checkout of the pinned commit."""

    out: dict[str, str] = {}
    for name, path, cell, pick, replacements in QUERIES:
        body = strings(_source_text(checkout, path, cell), anywhere=False)[pick]
        notes = []
        for old, new, times in replacements:
            if new.startswith("@js"):  # the JavaScript string a cell (default: the query's own) defines first
                js_cell = int(new[4:]) if ":" in new else cell
                new = strings(_source_text(checkout, path, js_cell), anywhere=True)[0].strip("\n")
                notes.append(f"{old!r} by the JavaScript string that cell {js_cell} defines first")
            else:
                notes.append(f"{old!r} by {new!r}")
            assert body.count(old) >= times, (name, old, body.count(old))
            for _ in range(times):
                body = body.replace(old, new, 1)
        out[f"{name}.sql"] = _header(path, cell, pick, "Python format fields replaced:", notes) + body.strip("\n") + "\n"
    # The set-expansion notebook builds one query by concatenating strings around a search term and a count.
    path = "examples/patent_set_expansion.ipynb"
    pieces = strings(_source_text(checkout, path, 6), anywhere=True)
    query = pieces[0] + "neural network" + pieces[1] + "250" + pieces[2]
    note = ["the search term and the count between its three string pieces by the notebook's own defaults, 'neural network' and 250"]
    out["set_expansion_publications.sql"] = _header(path, 6, 0, "Three string pieces joined:", note) + query.strip("\n") + "\n"
    query = strings(_source_text(checkout, path, 14), anywhere=False)[0]
    for old, new in SIMILARITY.items():
        query = query.replace(old, new)
    out["set_expansion_similarity.sql"] = _header(
        path, 14, 0, "str.replace placeholders filled in:", [f"{old} by {new}" for old, new in SIMILARITY.items()]
    ) + query.strip("\n") + "\n"
    return out
