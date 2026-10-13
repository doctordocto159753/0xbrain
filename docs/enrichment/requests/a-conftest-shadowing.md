# Request a-conftest-shadowing (WP-A -> AGENT-0)

Shared file: `tests/enrichment/` (package layout, AGENT-0 owned). Not applied by WP-A.

## Problem

A single pytest session over the whole `tests/` tree fails at collection:

```
$ python -m pytest -q tests
tests/test_brain_fixtures.py:  from conftest import PNG ...
E   ImportError: cannot import name 'PNG' from 'conftest' (.../tests/enrichment/conftest.py)
```

Two rootless `conftest.py` files (`tests/conftest.py`, `tests/enrichment/conftest.py`) are both
imported under the top-level module name `conftest`. `tests/enrichment` is collected first
(alphabetical), so `sys.modules["conftest"]` is the enrichment one when the older test modules
run `from conftest import PNG/OGG/commit_all/...`. Reproduced on the freeze commit (347fa40) with
only AGENT-0 files: `pytest tests/test_brain_fixtures.py tests/enrichment/test_contract.py`.
Running the two trees in separate pytest invocations works, which is why the baseline numbers
were obtained that way.

## Proposed patch

Add an empty `tests/enrichment/__init__.py`. The directory then becomes the package `enrichment`
(rootdir `tests/` is put on `sys.path`), its conftest is imported as `enrichment.conftest`, and the
older `from conftest import ...` keeps resolving to `tests/conftest.py`. The `sys.path` entries that
`tests/enrichment/conftest.py` already inserts keep `from support import ...` working.

Verified locally (without committing the file):

```
$ touch tests/enrichment/__init__.py
$ python -m pytest -q tests/test_brain_fixtures.py tests/enrichment/test_contract.py tests/enrichment/test_chunking.py
2 failed, 186 passed   # the 2 failures are the known qmd baseline failures
```

Until applied, run the existing suites and `tests/enrichment` as separate pytest invocations.
