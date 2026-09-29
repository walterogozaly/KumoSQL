# Getting started

KumoSQL provides Python functions and a local browser UI for working with BigQuery SQL and Dataform SQLX. It requires Python 3.11 or newer.

## Install

Install the latest version from this repository with pip:

```shell
python -m pip install "git+https://github.com/walterogozaly/KumoSQL.git"
```

On Windows, `py -3.11 -m pip install "git+https://github.com/walterogozaly/KumoSQL.git"` selects Python 3.11 explicitly. To install a local checkout instead, run `python -m pip install .` from the repository root. For development, use `python -m pip install -e ".[dev]"`.

The distribution is named `kumosql`; the Python import package is `kumosql`.

## Use a Python function

For example, `lift_subqueries` moves subqueries from `FROM` and `JOIN` clauses into top-level CTEs:

```python
from kumosql import lift_subqueries

sql = """\
SELECT c.id
FROM (
  SELECT id
  FROM `my-project.analytics.customers`
) AS c
"""

result = lift_subqueries(sql)
if not result.success:
    raise RuntimeError(result.diagnostics)

print(result.sql)
```

The returned result includes the transformed SQL, diagnostics, and a success flag. A failed transformation should be reviewed before using its SQL.

## Start and use the browser UI

After installing the package, start the UI from a terminal:

```shell
kumosql-ui
```

The command starts a local server at `http://127.0.0.1:8765/` and opens it in your browser. If the browser does not open automatically, navigate to that address. On Windows, if you installed into the user-owned environment shown in the repository README, run:

```powershell
& "$env:LOCALAPPDATA\kumosql\Scripts\kumosql-ui.exe"
```

Paste BigQuery SQL or Dataform SQLX into **Original SQL**, choose the transformations to apply, and inspect **Proposed SQL** and the verification report. Transformations run in the order shown. The editor updates after you type or change a selection; you can also use **Transform SQL** or Ctrl+Enter. Use **Copy SQL** to copy the result. Review any verification warning before using the proposed SQL.

The UI listens only on your computer (`127.0.0.1`) and processes SQL locally; it does not contact BigQuery. Stop the server with Ctrl+C. To suppress automatic browser opening, run `kumosql-ui --no-browser`; to use a different port, run `kumosql-ui --port 8766` and open `http://127.0.0.1:8766/`.
