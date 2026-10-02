"""Analysis on a fixture-sized project must stay within a time budget (a stage that hangs fails here)."""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from kumosql import load_sqlx_project
from kumosql.repeated_work import find_repeated_work

TOOLS = Path(__file__).resolve().parent.parent / "tools"

# Each stage took 2 to 5 s on this project when the budgets were set; a change that makes a stage scale
# badly (the duplicate search once hung on a 3,400-file project) takes minutes.
MODELS = 1000
STAGE_BUDGET_SECONDS = 40


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("make_dataform_fixture", TOOLS / "make_dataform_fixture.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    out = tmp_path_factory.mktemp("scale")
    module.generate(out, MODELS, seed=11)
    pipeline = load_sqlx_project(str(out))
    pipeline._analyse()
    return pipeline


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(lambda p: p.duplicate_selects(), id="exact-duplicates"),
        pytest.param(lambda p: p.near_duplicate_selects(), id="near-duplicates"),
        pytest.param(find_repeated_work, id="repeated-work"),
    ],
)
def test_stage_finishes_within_budget(project, stage):
    start = time.perf_counter()
    stage(project)
    assert time.perf_counter() - start < STAGE_BUDGET_SECONDS
