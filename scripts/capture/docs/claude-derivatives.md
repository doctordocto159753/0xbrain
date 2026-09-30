# Interpretation gaps and Claude candidate derivatives (K4, additive)

Capture record schema stays `1.0.0`. Everything here is optional: records
written before this change validate unchanged and re-render byte-identically.

## Layers (never merged)

| Layer | Body section | Meaning |
|---|---|---|
| `literal` | Literal transcript or extraction | what was said / visibly written, no summary |
| `description` | Machine description | what a visual looks like |
| `interpretation` | Interpretation (candidate) | what it may mean; optional section, rendered only when non-empty |

The user's own text (`User-supplied text`) and the original media are never
written by any derivative path.

## Gap metadata

`interpretation_gaps(fm, sections)` is a pure function; the front-matter
field `interpretation_needs` is only a snapshot refreshed by core writers.

- `needs_transcription`: voice/handwriting/mixed with an empty literal layer
- `needs_description`: image/drawing/handwriting/mixed with an empty description
- `needs_interpretation`: only after `request_interpretation(id, actor)` and until an interpretation exists

Find work: `wiki_capture.py list --needs needs_transcription` (or `list_captures(needs=...)`).

## Claude-produced derivatives

`record_claude_derivative(id, layer, text, producer_ref="")` /
`wiki_capture.py add-derivative --id ID --layer literal|description|interpretation --text ...`

- method recorded as `claude` (`derivative_methods`, `transcription_method`)
- `derivative_tier: candidate`; `transcription_reviewed` stays `false`
- write-once per layer: never overwrites existing text (human, tool, or Claude)
- does not change `status`; state changes still need a named human actor
- the legacy writers `record_transcript` / `record_description` refuse
  `adapter="claude"` so Claude output cannot be laundered as reviewed

## Optional engines

No STT/OCR/vision engine is imported or enabled by default. Automatic STT still
needs a passing host benchmark file; remote providers are refused.

## Known limits (not fixed here; see Agent D report)

1. Any user text line equal to a body section header is re-parsed as a header.
2. CRLF text is hashed with `\r\n` but read back with universal newlines.
3. Writers drop front-matter keys they do not know.
