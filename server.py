"""Claude Castor MCP server: the entry point.

The tools live in the `castor` package. Importing each module registers its
`@mcp.tool()` functions on the shared `mcp` object.
"""

from castor.app import _register_gated, mcp
from castor import tools  # noqa: F401  (the castor_* tools)
from castor.agy import tools as agy_tools  # noqa: F401  (the gemini_* tools)
from castor.nim import backend, summarize, agent, roles, explain, edit  # noqa: F401
from castor.run import tier, compare  # noqa: F401

# nim_edit failed its stage 1 gate (run 6, 2026-11-04: two whitespace-wrong
# patches); it is offered only with CASTOR_NIM_EDIT=1.
_register_gated((edit.nim_edit, edit.nim_edit_start), "CASTOR_NIM_EDIT")


if __name__ == "__main__":
    mcp.run()
