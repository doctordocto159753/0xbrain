# Request b-conftest-collision (WP-B to AGENT-0)

Observed at the freeze commit 347fa40 (independent of WP-B files): running the
whole suite in one process,

    python -m pytest -q -p no:cacheprovider tests

fails at collection with 7 errors such as
`ImportError: cannot import name 'OGG' from 'conftest' (.../tests/enrichment/conftest.py)`.
The older tests do `from conftest import OGG, PNG, commit_all` and expect the
module `conftest` to be `tests/conftest.py`; `tests/enrichment/conftest.py` is a
second module with the same basename and wins once `tests/enrichment` is first
on `sys.path`.

Each part passes on its own:

    python -m pytest -q -p no:cacheprovider tests --ignore=tests/enrichment   # 3 failed (qmd), 541 passed, 20 skipped
    python -m pytest -q -p no:cacheprovider tests/enrichment                  # all enrichment tests pass

Proposed patch (AGENT-0 owns `tests/enrichment/conftest.py`): move the
`enrichment_wiki` fixture into a uniquely named plugin module, e.g.
`tests/enrichment/enrichment_fixtures.py`, and load it from the repository-root
`tests/conftest.py` with `pytest_plugins = ["enrichment_fixtures"]` (or delete
`tests/enrichment/conftest.py` and import the fixture in each enrichment test);
keep the `sys.path` setup for `support` inside that module with `append`, not
`insert(0, ...)`. CI that runs `pytest tests` in one process needs this before
integration.
