# Request d-pytest-conftest-collision: `tests/enrichment/conftest.py` shadows `tests/conftest.py`

Shared file owner: AGENT-0 (`tests/enrichment/`). WP-D did not add it.

## Symptom (reproducible at the freeze commit, before any WP-D file)

```
python -m pytest -q -p no:cacheprovider tests/enrichment tests/test_brain_surface.py
ImportError: cannot import name 'commit_all' from 'conftest' (tests/enrichment/conftest.py)
python -m pytest -p no:cacheprovider tests --co -q      -> 7 collection errors
```

Both conftests are imported under the bare module name `conftest` (no
`__init__.py`), so `from conftest import commit_all, record` in
`tests/test_brain_surface.py` (and six other modules) resolves to the enrichment
one whenever the two directories are collected in one run. `pytest tests/enrichment`
alone and `pytest tests/test_brain_surface.py` alone are fine, which is why it
was not seen at the freeze.

## Proposed patch

Add an empty `tests/enrichment/__init__.py`. Verified in WP-D's worktree:
`pytest tests --co` -> 690 collected, 0 errors; the full suite
(`pytest tests`) -> `3 failed, 667 passed, 21 skipped`, the three failures being
the documented qmd baseline ones. The enrichment tests keep working because
`tests/enrichment/conftest.py` puts its own directory on `sys.path` (so
`from support import ...` still resolves); WP-D's test modules additionally
`from test_surface import ...` (a second import of that file under its bare name,
harmless).

WP-D's verification command output in the final report was produced with that
file present temporarily and removed before the commit.
