"""Deterministic, rule-based stand-in for the model step (CI only; no model,
no network, no randomness). It reads a pending item exactly as
`brain_enrichment_pending` returns it and produces an annotation object that
satisfies CONTRACT-E1 section 5, so the loop and the AnnotationService can be
tested end to end.

Rules (all substring-exact, nothing normalised):
* chunk_annotations: for each chunk with `annotated: false` on this page (max 20):
  a short summary (first line, <= 120 code points), up to 3 frequent words as tags.
* entity: when a chunk starts with `<name>: <rest>` (name <= 60 code points, no
  line break) the name is an entity (kind "project") with that exact prefix as
  its mention, plus an `about` link whose evidence is the first <= 60 code
  points of the rest of the first line, when that is >= 8 code points.
* artifact summary: only when this page is the whole artifact.
This is test scaffolding, not a quality bar for real annotation.
"""
from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_HEAD = re.compile(r"^([^\r\n:]{2,60}):[ \t]+(?=\S)")
PROMPT_VERSION = "annotate-1"


def _first_line(text: str) -> str:
    return re.split(r"\r\n|\r|\n", text.strip(), maxsplit=1)[0]


def _tags(text: str, n: int) -> list[str]:
    counts: Counter = Counter()
    order: dict[str, int] = {}
    for m in _WORD.finditer(text):
        w = m.group(0).casefold()
        counts[w] += 1
        order.setdefault(w, m.start())
    ranked = sorted(counts, key=lambda w: (-counts[w], order[w]))
    return [w[:64] for w in ranked[:n]]


def annotate(item: dict) -> dict:
    page = item.get("page") or {}
    chunks = [c for c in page.get("chunks", []) if isinstance(c.get("text"), str) and c["text"].strip()]
    whole = (not page.get("truncated")) and int(page.get("offset") or 0) == 0 and \
        int(page.get("returned") or len(chunks)) >= int(page.get("total") or len(chunks))
    out: dict = {}
    chunk_ann, entities, links = [], [], []
    for c in chunks:
        if c.get("annotated") is True or len(chunk_ann) >= 20:
            continue
        ann: dict = {"chunk_ref": c["chunk_ref"], "summary": _first_line(c["text"])[:120] or "text"}
        tags = _tags(c["text"], 3)
        if tags:
            ann["tags"] = tags
        chunk_ann.append(ann)
        m = _HEAD.match(c["text"])
        if m and not entities:
            name = m.group(1)
            entities.append({"name": name, "kind": "project", "mention": {"chunk_ref": c["chunk_ref"], "text": name}})
            rest = _first_line(c["text"][m.end():])[:60]
            if len(rest) >= 8:
                links.append({"relation": "about", "target": {"entity": name},
                              "evidence": [{"chunk_ref": c["chunk_ref"], "quote": rest}]})
    if chunk_ann:
        out["chunk_annotations"] = chunk_ann
    if entities:
        out["entities"] = entities
    if links:
        out["links"] = links
    if whole and chunks:
        out["summary"] = _first_line(chunks[0]["text"])[:200] or "text"
        tags = _tags(" ".join(c["text"] for c in chunks), 5)
        if tags:
            out["tags"] = tags
    if not out:
        out["uncertainty"] = ["no unannotated text on this page"]
    out["producer_reported"] = {"harness": "fake-annotator", "model": "rules", "prompt_version": PROMPT_VERSION}
    return out
