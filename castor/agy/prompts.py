"""Antigravity constants: install and auth text, timeouts, prompts, schemas."""

import json


INSTALL_CMD = "curl -fsSL https://antigravity.google/cli/install.sh | bash"


AUTH_HINT = (
    'Run the `gemini_auth` tool or type `! agy -p "ok"` '
    "in the Claude Code prompt to sign in — it is a one-time step."
)


# Baked prompt for gemini_index — requests a structured JSON object.
INDEX_PROMPT = """\
You are a technical assistant. Produce a structured JSON index of this
codebase for use as context by another engineer.

Return a JSON object with exactly these keys:

  "summary"         — one-paragraph description of what this codebase does
  "entry_points"    — list of top-level entry point paths as strings
  "modules"         — object mapping each key module or class name to
                      {"file": "<path>", "line": <int>, "role": "<desc>"}
  "exception_types" — list of custom exception or error type names
                      (empty list if none)
  "architecture"    — short paragraph on how the major pieces connect

Be precise — use real file paths, symbol names, and line numbers from
the code. Do NOT wrap the output in a code fence. Return only the JSON
object, nothing else.\
"""


# Baked prompt for gemini_summarize — structured, token-lean summary.
SUMMARIZE_PROMPT = """\
Read `{target}` (a file or directory under the project root) and summarize
it. Return ONLY a JSON object with exactly these keys:

  "summary"     — one short paragraph on what {target} is and does
  "key_points"  — list of terse, information-dense bullets. For a
                  directory, make each bullet a per-area roll-up prefixed
                  with the area's path.

Every bullet must carry information. Do NOT wrap the output in a code
fence. Return only the JSON object, nothing else.\
"""


# Baked prompt for gemini_semantic_search — ranked hits as JSON. Wrapped
# in a "hits" object (rather than a bare array) because agy's
# --json-schema enforcement requires a root object schema — see
# SEMANTIC_SEARCH_JSON_SCHEMA and _parse_hits.
SEMANTIC_SEARCH_PROMPT = """\
Search this codebase for code relevant to: "{query}".

Return ONLY a JSON object with one key "hits", an array of at most {n}
objects, most relevant first:

  {{"hits": [{{"path": "<repo-relative path>", "line": <int>,
  "reason": "<=12 words"}}]}}

Use real paths and line numbers from the code. Do NOT wrap the output in a
code fence. Return only the JSON object, nothing else.\
"""


# Baked prompt for gemini_document — proposed docs in the project's style.
DOCUMENT_PROMPT = """\
Generate documentation for `{target}` in this project. If `{target}`
resolves to a file, document its public items; otherwise treat it as a
symbol name and document that symbol. Match the project's existing
docstring and comment style — infer it from neighboring code. Return only
the proposed docstrings or markdown; do not restate or rewrite the
implementation, and do not modify any file.\
"""


# agy print mode waits this long for a single prompt to resolve.
PRINT_TIMEOUT = "300s"


# Subprocess wall-clock guard, slightly above PRINT_TIMEOUT.
SUBPROCESS_TIMEOUT = 310


# Max hits gemini_semantic_search asks agy to return.
SEMANTIC_SEARCH_MAX_HITS = 20


_INDEX_KEYS = frozenset(
    {
        "summary",
        "entry_points",
        "modules",
        "exception_types",
        "architecture",
    }
)


# --json-schema payloads enforced on the three JSON-emitting workflow
# tools (gemini_index, gemini_summarize, gemini_semantic_search), so
# structure is a contract agy enforces rather than a request it might
# ignore (see _parse_ndjson_result). Confirmed live against agy 1.1.9
# that --json-schema requires a root object schema — a root array
# schema returns an error result with no structured_output at all —
# which is why SEMANTIC_SEARCH_JSON_SCHEMA wraps its array in a "hits"
# object instead of matching gemini_semantic_search's bare-array prompt
# shape 1:1 (see SEMANTIC_SEARCH_PROMPT and _parse_hits).
INDEX_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "entry_points": {
                "type": "array",
                "items": {"type": "string"},
            },
            "modules": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "role": {"type": "string"},
                    },
                    "required": ["file", "line", "role"],
                },
            },
            "exception_types": {
                "type": "array",
                "items": {"type": "string"},
            },
            "architecture": {"type": "string"},
        },
        "required": [
            "summary",
            "entry_points",
            "modules",
            "exception_types",
            "architecture",
        ],
    }
)


SUMMARIZE_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "key_points"],
    }
)


SEMANTIC_SEARCH_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "hits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["path", "line", "reason"],
                },
                "maxItems": SEMANTIC_SEARCH_MAX_HITS,
            },
        },
        "required": ["hits"],
    }
)
