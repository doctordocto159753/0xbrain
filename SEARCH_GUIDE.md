# Search Guide

## 1. Search is a route, not evidence

The wiki contains several descriptions of the same material:

- a canonical page;
- a claim or relation record;
- a source record;
- a machine-extracted derivative;
- an immutable original;
- an index or semantic result.

They are not interchangeable. For an interpretive question:

```text
canonical record → claim/relation → source record → extracted passage → immutable original
```

A search score does not increase a statement's certainty.

## 2. Obsidian search

- **Exact:** `Ctrl+Shift+F` for a known phrase, ID, property, or filename.
- **Backlinks/Outgoing links:** a backlink is navigational, not evidence of
  influence.
- **Properties:** YAML frontmatter stores `type`, `status`, `certainty`,
  `current_claim_permission`, `relation_status`, `updated`. Do not rename a
  property globally without a system decision and migration.
- **Bases:** saved views over properties. A Base is a view, not a database
  and not an authority layer.

## 3. QMD collections (model-free)

Defined once in `00-system/configuration/qmd-collections.json`; the helpers
and `scripts/search_lexical.py` all read that file. Every collection is
rooted at the repository root and selected by mask, so result paths are
repository-relative. The index lives in `_search/qmd` (git-ignored,
disposable).

| Collection | Zone | Tier |
|---|---|---|
| `wiki` | `03-objects`, `04-notes`, `05-claims`, `06-relations` | canonical-record |
| `wiki-source-records` | `02-sources/records` | source-record |
| `wiki-derivatives` | `02-sources/text` | derivative (candidate passages) |
| `wiki-captures` | `01-inbox/captures` | capture (noncanonical) |

```sh
scripts/configure-search.sh     # Windows: configure-search.ps1  (idempotent)
scripts/refresh-search.sh       # Windows: refresh-search.ps1    (qmd update only)
python scripts/search_lexical.py status    # files on disk vs indexed; "stale": false expected
```

## 4. Search modes and scopes

No embedding, vector search, reranker or query-expansion model is part of the
default path. Forbidden by default: `qmd embed`, `qmd vsearch`, `qmd pull`, a
bare `qmd query "question"` (it loads a query-expansion model), and the
`qmd mcp` server (measured: its `query` tool accepts `vec:`/`hyde:` sub-queries,
and `get`/`multi_get` read indexed files by path, bypassing ref-only reads). `search_lexical.py` refuses them in
code (`assert_model_free`). Allowed: `qmd search`, `qmd query` as a typed
all-`lex:` document with `--no-rerank`, and the exact fallback.

```sh
scripts/search-wiki.sh "provenance tracing"                 # lexical, scope canonical
scripts/search-wiki.sh "حافظه جمعی" all 10                  # canonical and captures, separate groups
scripts/search-wiki.sh --exact "محمد" canonical             # exact/normalised substring, always current
python scripts/search_lexical.py search "query" --scope canonical|captures|all --mode lexical|exact -n 10
```

- `scope=canonical`: the five zones `02-sources`, `03-objects`, `04-notes`,
  `05-claims`, `06-relations`. Results are grouped by tier (canonical record,
  source record, derivative); each result carries `tier`, `zone`, `path`, `id`.
  A tier is never interleaved by score with another.
- `scope=captures`: `01-inbox/captures` only. Never canonical evidence.
- `scope=all`: two separate groups, `canonical` and `captures`; never one
  merged ranking. A capture never appears in a canonical group.
- No numeric score is returned. Rank orders attention only.

**Lexical mode is a relaxation ladder, not query understanding.** Steps, each
run only if all earlier steps returned nothing anywhere in the scope:
`strict` (AND of terms, plus orthographic variants) then `content-terms`
(stopwords dropped) then `persian-stem-prefix` (`کتابها` becomes `کتاب*`) then
`any-term` (OR of terms). The step that produced the results is reported as
`strategy`; an `any-term` result is a hint, not a match.

**Reformulation replaces server-side LLM expansion.** The server does not
expand or reinterpret a question. Claude, as the caller, should iterate: turn
a question into keywords, try the other language or spelling, try a known
identifier, then fall back to `mode="exact"`. Cross-language questions (a
Persian question whose records are in English) are not bridged by the server;
reformulate in the record's language. This is the designed division of labour;
the evaluation quantifies it (`tests/search_eval`).

### Persian and Arabic-script behaviour (qmd 2.8.3, measured)

QMD's tokenizer does not normalise Persian orthography. Measured facts:

| Situation | QMD alone | With `search_lexical.py` |
|---|---|---|
| Persian `ک ی` vs Arabic `ك ي` in query vs text | different words, no match | both spellings are tried (OR) |
| Persian `۱۴۰۳`, Arabic-Indic `١٤٠٣`, ASCII `1403` | three different tokens | all three are tried |
| ZWNJ in the query (`می‌خواهم`) | matches nothing | ZWNJ becomes a space; matches text written either way |
| Harakat/tatweel in the query | stripped by QMD | stripped |
| Harakat/tatweel inside indexed text (`مُحَمَّد`) | word is split apart; `محمد` cannot find it | not fixable query-side: use `mode="exact"` (folds harakat) |
| Joined plural `کتابها` vs indexed `کتاب‌ها` | no match | `persian-stem-prefix` step |
| Hyphen plus extension (`report-1402.pdf`) | matches nothing | rewritten as a phrase |
| Arabic `ة` vs Persian `ه`, `آ` vs `ا` | distinct | distinct in lexical; folded in exact only for `ة` |

The exact fallback folds: NFKC, case, `ک/ك`, `ی/ي/ى`, `ة`, digit scripts,
harakat, tatweel and ZWNJ; whitespace runs collapse. It is a contiguous
phrase match, not fuzzy: `حافظه جمعی` does not match `حافظه‌ی جمعی` (ezafe).

## 4a. Recommended routine

1. Identifier, filename or exact wording known: `mode="exact"`.
2. Otherwise `mode="lexical"`, `scope="canonical"`; read `strategy`.
3. Empty or `any-term` only: reformulate (keywords, other language, spelling)
   and call again; then `mode="exact"`.
4. Load-bearing hit: open the record, then its source record, then the
   immutable original (see section 1).

## 4b. Remote (`brain_search`)

The remote tool is a thin wrapper over the same `search_lexical.search`
(same modes, scopes, groups, tiers, ladder). Differences: results carry
public refs (`rec:`, `doc:`, `cap:`) instead of paths; the server refreshes
a stale index (or registers the collections on a fresh/restored install)
before a lexical query, so a new capture is findable immediately; without
QMD, lexical mode answers `E_UNAVAILABLE` and exact mode still works.

Naming nuance: scope `canonical` is the governed non-capture side
(canonical records, source records, derivatives). It does not mean every
hit is accepted canonical evidence: each hit keeps its `tier` and
`authority_level` (a `derivative` is a candidate passage, level 5).

Known limitations (model-free by design): no cross-language retrieval (a
Persian query does not find an English record with the same meaning);
exact/phrase edge cases around ezafe and ZWNJ (section 4); natural-language
questions have low lexical recall. The Claude standing instructions tell
Claude to reformulate iteratively (keywords, both languages, spelling
variants, exact mode). No local semantic model is added to compensate.

## 5. Boundaries

- `_originals/` is not indexed directly.
- Exact form, images, handwriting, tracked changes → the original.
- A catalogue entry is not a manuscript; a source list is not proof of
  reading; a canonical page is not a primary source; an index, Base, graph,
  or QMD result is not a claim.

## 6. Validation

```powershell
python scripts/validate_repo.py --full
python scripts/validate_content_release.py
powershell -ExecutionPolicy Bypass -File scripts/verify-install.ps1
python scripts/run_lexical_eval.py          # model-free lexical evaluation (about 3-4 min)
python scripts/prove_search_model_free.py   # clean-room no-model / no-network proof
```

`scripts/run-semantic-benchmark.py` is optional, model-backed, and refuses to
run without `--allow-models`.
