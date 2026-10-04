"""Query generators aimed at particular rule modules, for ``tools/rule_fuzz.py run --corpus target:<module>``.

Each module defines ``cases(seed: int, count: int) -> list[dict]`` returning cases in the harness's format (``sql``,
``dialect``, ``schema`` as ``{table: [[column, TYPE], ...]}``, ``constraints``, ``options``, ``source``). Generators
should produce inputs the module's rules actually fire on, plus near misses where a guard must stop them.
"""
