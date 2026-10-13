"""Deterministic chunker `chunk-1` (CONTRACT-E1 section 4, normative).

Pure and import-side-effect free. A layer's chunks PARTITION [0, len(text)):
no gap, no overlap, no stripping, no normalisation, so for every chunk
`chunk.text == layer.text[chunk.start_char:chunk.end_char]`. Offsets are
zero-based code points; `\\r\\n` is never split (a `\\r` stays part of its line).

Algorithm (all thresholds come from the ChunkPolicy; defaults are the
contract constants):

1. empty text -> no chunk; len <= soft_max -> exactly one chunk.
2. otherwise the text is cut into *blocks* at the offsets where a line
   starts that is: a heading (`^#{1,6} `), the first line after a blank-line
   run (paragraph start), the first line of a contiguous table (`|`-led
   lines, never cut inside), a `## Page N` / `## Slide N` marker (also a
   heading), or the line after the closing `---` of a leading YAML front
   matter block. Lines end at `\\n` only.
3. greedy: append blocks while the chunk stays <= soft_max; cut before the
   block that would exceed it. A block with soft_max < len <= hard_max is
   its own chunk.
4. a block > hard_max is split into pieces <= soft_max at the last sentence
   boundary (`.`, `!`, `?`, `؟`, `۔`, `…` or `\\n`, followed by whitespace or
   the end of the block) at or before soft_max from the piece start, the
   following whitespace run staying with the preceding sentence when it fits;
   with no boundary, a hard cut at soft_max (+1 when that would split a
   `\\r\\n`). The pieces take part in step 3 as blocks.
5. a final chunk shorter than min_tail merges into its predecessor when the
   sum is <= hard_max.
6. no chunk exceeds hard_max (guaranteed by 3-4, asserted).
"""
from __future__ import annotations

import re

from .contract import (Chunk, ChunkPolicy, SourceProjection, chunk_digest as _digest,
                       chunk_ref as _ref)

_HEADING = re.compile(r"#{1,6} ")
_SENTENCE_END = frozenset(".!?؟۔…\n")
_BLANK_CHARS = " \t\r"
_WS = frozenset(" \t\r\n\x0b\x0c\x85   ")


def _lines(text: str) -> list[tuple[int, int]]:
    """(start, end) of every LF-terminated line; end includes the `\\n`."""
    out, pos, n = [], 0, len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl == -1 else nl + 1
        out.append((pos, end))
        pos = end
    return out


def _is_blank(line: str) -> bool:
    return line.strip(_BLANK_CHARS + "\n") == ""


def _front_matter_end(text: str, lines: list[tuple[int, int]]) -> int | None:
    """Offset just after the closing `---` line of a leading YAML block."""
    if not lines or text[lines[0][0]:lines[0][1]].rstrip("\r\n") != "---":
        return None
    for s, e in lines[1:]:
        if text[s:e].rstrip("\r\n") == "---":
            return e
    return None


def block_starts(text: str) -> list[int]:
    """Sorted block start offsets (always begins with 0) for step 2."""
    lines = _lines(text)
    starts = {0}
    fm_end = _front_matter_end(text, lines)
    if fm_end is not None and fm_end < len(text):
        starts.add(fm_end)
    prev_blank = False
    prev_table = False
    for s, e in lines:
        line = text[s:e]
        if _is_blank(line):
            prev_blank, prev_table = True, False
            continue
        table = line.lstrip(" \t").startswith("|")
        if _HEADING.match(line) or prev_blank or (table and not prev_table):
            starts.add(s)
        prev_blank, prev_table = False, table
    return sorted(starts)


def _split_oversized(text: str, start: int, end: int, policy: ChunkPolicy) -> list[tuple[int, int]]:
    """Step 4: split [start, end) into pieces <= soft_max."""
    soft = policy.soft_max
    pieces: list[tuple[int, int]] = []
    pos = start
    while end - pos > soft:
        limit = pos + soft
        cut = None
        # candidates: terminator at index i followed by whitespace/end-of-block;
        # the cut lands after the terminator, extended over the whitespace run.
        i = limit - 1
        while i > pos:
            if text[i] in _SENTENCE_END:
                j = i + 1
                if j >= end or text[j] in _WS:
                    k = j
                    while k < end and text[k] in _WS:
                        k += 1
                    cut = k if k <= limit else j
                    break
            i -= 1
        if cut is None:
            cut = limit
            if text[cut - 1] == "\r" and cut < end and text[cut] == "\n":
                cut += 1
        pieces.append((pos, cut))
        pos = cut
    pieces.append((pos, end))
    return pieces


def spans(text: str, policy: ChunkPolicy | None = None) -> list[tuple[int, int]]:
    """Chunk spans of one layer text (steps 1-6)."""
    policy = policy or ChunkPolicy()
    n = len(text)
    if n == 0:
        return []
    if n <= policy.soft_max:
        return [(0, n)]
    starts = block_starts(text)
    bounds = starts + [n]
    blocks: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a > policy.hard_max:
            blocks.extend(_split_oversized(text, a, b, policy))
        else:
            blocks.append((a, b))
    chunks: list[list[int]] = []
    for a, b in blocks:
        if chunks and (b - chunks[-1][0]) <= policy.soft_max:
            chunks[-1][1] = b
        else:
            chunks.append([a, b])
    if len(chunks) > 1 and (chunks[-1][1] - chunks[-1][0]) < policy.min_tail \
            and (chunks[-1][1] - chunks[-2][0]) <= policy.hard_max:
        chunks[-2][1] = chunks[-1][1]
        chunks.pop()
    out = [(a, b) for a, b in chunks]
    assert out[0][0] == 0 and out[-1][1] == n
    assert all(x[1] == y[0] for x, y in zip(out, out[1:]))
    assert all(b - a <= policy.hard_max for a, b in out)
    return out


def _heading_path(text: str, heads: list[tuple[int, int, str]], start: int) -> tuple[str, ...]:
    """Informational: the headings in force at `start` (most recent per level)."""
    stack: list[tuple[int, str]] = []
    for off, level, title in heads:
        if off > start:
            break
        stack = [(lv, t) for lv, t in stack if lv < level]
        stack.append((level, title))
    return tuple(t for _, t in stack)


def _headings(text: str) -> list[tuple[int, int, str]]:
    out = []
    for s, e in _lines(text):
        line = text[s:e]
        m = _HEADING.match(line)
        if m:
            level = len(line) - len(line.lstrip("#"))
            out.append((s, level, line[level:].strip()[:120]))
    return out


class Chunker:
    """Implements contract.Chunker. Stateless and deterministic."""

    def build(self, projection: SourceProjection, policy: ChunkPolicy | None = None) -> list[Chunk]:
        policy = policy or ChunkPolicy()
        chunks: list[Chunk] = []
        for layer in projection.layers:
            text = layer.text
            layer_spans = spans(text, policy)
            heads = _headings(text) if len(layer_spans) > 1 else []
            digests = [_digest(projection.source_ref, projection.source_version, layer.layer_id,
                               a, b, policy.chunker_version) for a, b in layer_spans]
            refs = [_ref(d) for d in digests]
            for i, ((a, b), dg) in enumerate(zip(layer_spans, digests)):
                chunks.append(Chunk(
                    chunk_ref=refs[i], chunk_digest=dg, source_ref=projection.source_ref,
                    source_version=projection.source_version, text_ref=layer.text_ref,
                    layer_id=layer.layer_id, index=len(chunks), start_char=a, end_char=b,
                    text=text[a:b],
                    prev_chunk_ref=refs[i - 1] if i else None,
                    next_chunk_ref=refs[i + 1] if i + 1 < len(refs) else None,
                    quality_flags=tuple(projection.quality_flags),
                    heading_path=_heading_path(text, heads, a) if heads else ()))
        return chunks


def build(projection: SourceProjection, policy: ChunkPolicy | None = None) -> list[Chunk]:
    return Chunker().build(projection, policy)
