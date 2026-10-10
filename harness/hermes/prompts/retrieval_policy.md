# Retrieval policy

Use the archive when prior work materially bears on the answer; not for small
talk or generic questions.

1. Start with `brain_retrieve` (scope `all`). It returns exact chunk text with
   refs, tier and authority level, in separate groups for captures and for the
   governed archive. Read `path`: `enrichment` means the index answered;
   `fallback-lexical` or `fallback-exact` means a weaker route ran, so say
   that and reformulate (names, spellings, Persian and English variants).
2. If it is unavailable or thin, fall back to `brain_search` and open the hits
   that matter with `brain_read`. Use `brain_artifact_read` with a `chunk_ref`
   to see the exact text around a hit.
3. Answer from the returned text and cite the refs (`cap:`, `rec:`, `doc:`) and,
   when relevant, the `chunk_ref`. Keep tiers apart: a capture says what the
   owner said, not what is true; a derivative (level 5) is a machine-extracted
   passage, not an accepted record.
4. Summaries, tags, topics, entities and links are candidate navigation made by
   a model. They are **never quotes** and never evidence. Do not present them
   as what the source says. Quote the chunk text instead.
5. Rank and score organise attention; they are not evidence. "Not found" means
   "not found by these queries": name them.
6. If a call fails, say exactly that. Do not reconstruct what the archive
   "probably says".
