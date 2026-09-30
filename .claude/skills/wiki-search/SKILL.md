---
name: wiki-search
description: Search the wiki read-only with the model-free lexical helper (QMD BM25 plus exact fallback), by scope. Use for bounded questions and retrieval. Do not change files.
---

# Wiki Search

## Terms

- **bounded question**: a question whose answer can be responsibly developed from a selected set of relevant records.
- **lexical result**: a ranked candidate that shares words with the query; it is not evidence until its source is opened.
- **source record**: the provenance page that points to the immutable original and searchable derivative.

## Model-free rule

Never run `qmd embed`, `qmd vsearch`, `qmd pull`, a bare `qmd query "question"`, or the `qmd mcp` tools in the default path. Use `python scripts/search_lexical.py` (or `scripts/search-wiki.sh`). Reformulating the query yourself replaces server-side query expansion.

## Procedure

1. Restate the query in one precise sentence, then reduce it to keywords in the language of the likely records.
2. Run `python scripts/search_lexical.py search "<keywords>" --scope canonical -n 10`. Read `groups.canonical.strategy`: `strict` is a real match; `content-terms` or `persian-stem-prefix` is a relaxed match; `any-term` is only a hint.
3. If empty or hint-only, reformulate and run again: synonyms, the other language (Persian/English), Arabic-letter spelling, a known ID or filename. A Persian question whose records are English is not bridged by the search: rewrite it in English.
4. When wording, IDs, filenames, or a vocalised (harakat) Persian word matter, run `python scripts/search_lexical.py exact "<text>" --scope canonical`.
5. Use `--scope captures` for intake material and `--scope all` to see both; captures are noncanonical and never evidence.
6. Inspect results. Prefer canonical records, then source records; treat derivative hits as candidate passages. Open the source record for each load-bearing result.
7. Open the immutable original when wording, layout, page sequence, images, tracked changes, or version identity matters.
8. Return:
   - answer;
   - source paths;
   - evidence status;
   - queries tried and the strategy that produced results;
   - unresolved points;
   - whether the question requires full-corpus reconciliation.

Do not edit files. Do not call search rank proof.
