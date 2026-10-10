# Request d-wiring: adapter wiring (AGENT-0) and the factory table

Owner of the files: AGENT-0 (`scripts/remote_mcp/adapter.py`). WP-D did not edit them.

## 1. Adapter patch (`scripts/remote_mcp/adapter.py`)

`surface.tool_specs()` returns plain `(name, description, input_schema, handler)`
tuples. The base seven must stay first and byte-identical; the four are appended
only when `BRAIN_ENRICHMENT=on`; any failure to load the extension must leave the
seven untouched.

```python
def semantic_adapter(root: Path | None = None, env: dict | None = None) -> list[ToolSpec]:
    """The seven semantic tools (+ the four enrichment tools when BRAIN_ENRICHMENT=on)."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    from brain_surface import BrainSurface, TOOLS  # noqa: PLC0415
    from brain_surface.backend import WikiBackend  # noqa: PLC0415

    backend = WikiBackend(root)
    surface = BrainSurface(backend)

    def bind(name: str) -> Callable[[dict], dict]:
        return lambda args: surface.call(name, args)

    specs = [ToolSpec(t["name"], t["description"], t["inputSchema"], bind(t["name"])) for t in TOOLS]
    if tuple(s.name for s in specs) != SEMANTIC_TOOLS:          # unchanged drift guard, BEFORE appending
        raise RuntimeError(f"semantic surface drifted from K3: {[s.name for s in specs]}")
    return specs + _enrichment_specs(root, backend, os.environ if env is None else env)


def _enrichment_specs(root, backend, env) -> list[ToolSpec]:
    try:
        from brain_enrichment.config import EnrichmentConfig  # noqa: PLC0415
        from brain_enrichment.surface import build_extension, tool_specs  # noqa: PLC0415
        ext = build_extension(root or ROOT, EnrichmentConfig.from_env(env), legacy_backend=backend)
        return [ToolSpec(*t) for t in tool_specs(ext)] if ext is not None else []
    except Exception:  # noqa: BLE001 - graceful absence: the seven base tools must keep working
        return []
```

`load_adapter` must pass `env` through (`semantic_adapter(env=env)`), so that
`load_adapter(env={})` is still exactly the seven (the existing
`test_default_adapter_is_exactly_the_six_semantic_tools`). `build_extension` does
no I/O and imports nothing, so the `except` only guards the import of
`brain_enrichment` itself. `scripts/remote_mcp` must import only
`brain_enrichment.config` and `brain_enrichment.surface`, never `review` / `cli`
(a test in `test_review.py` asserts `surface.py` does not import them).

## 2. Real-module factories (`surface.REAL_FACTORIES`, one table)

`Services._build` imports `brain_enrichment.<module>` lazily and calls the first
attribute that exists, defined in that module (class or function), passing only
the keyword arguments the callable declares from: `root, config, store, index,
legacy_backend, legacy_search, source_reader, create`.

| component | module | tried names (in order) | expected result |
|---|---|---|---|
| store | `store` | `open_store`, `ProjectionStoreSQLite`, `SqliteProjectionStore`, `SQLiteProjectionStore` | `ProjectionStore`; `create=False` must not create the file; must apply `config.max_input_chars`/`PAGE_MAX_CHUNKS` to `pending` pages |
| source reader | `source` | `open_source_reader`, `SourceReader`, `FileSourceReader` | `SourceReader` (`load(source_ref)`); optional (freshness checks are skipped without it) |
| annotation | `annotation` | `build_annotation_service`, `AnnotationService` | `AnnotationService`; takes `store` (+ `root`, `config`) |
| retrieval | `retrieval` | `build_retrieval_engine`, `RetrievalEngine` | `RetrievalEngine`; may take `store`, `index`, `legacy_backend` / `legacy_search(query, mode, scope, n)` |
| index | `indexing` | `build_index_builder`, `IndexBuilder` | `IndexBuilder` (`refresh(batch_limit)`) |

If a landed module uses other names, edit only `REAL_FACTORIES` in `surface.py`.
`tests/enrichment/test_e2e_extension.py` is the acceptance run; it skips only
while one of those five modules is absent.
