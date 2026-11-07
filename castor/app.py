import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Parse --log-file from sys.argv
_log_file_path = None
_new_argv = []
_idx = 0
while _idx < len(sys.argv):
    _arg = sys.argv[_idx]
    if _arg == "--log-file":
        if _idx + 1 < len(sys.argv):
            _log_file_path = sys.argv[_idx + 1]
            _idx += 2
            continue
    elif _arg.startswith("--log-file="):
        _log_file_path = _arg.split("=", 1)[1]
        _idx += 1
        continue
    _new_argv.append(_arg)
    _idx += 1
sys.argv = _new_argv

logger = logging.getLogger("claude-castor")
logger.addHandler(logging.NullHandler())

_file_handler = None
if _log_file_path:
    try:
        _log_path = Path(_log_file_path).resolve()
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(_log_path, encoding="utf-8")
        _formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _file_handler.setFormatter(_formatter)
        logger.addHandler(_file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except Exception as _e:
        sys.stderr.write(f"Failed to configure logging: {_e}\n")

mcp = FastMCP("claude-castor")

if _log_file_path and _file_handler:
    try:
        _fastmcp_logger = logging.getLogger("fastmcp")
        _fastmcp_logger.addHandler(_file_handler)
        _fastmcp_logger.setLevel(logging.INFO)

        _root_logger = logging.getLogger()
        _root_logger.addHandler(_file_handler)
        _root_logger.setLevel(logging.INFO)

        logger.info(
            "Logging initialized. Server starting up. Writing logs to %s",
            _log_file_path,
        )
    except Exception as _e:
        pass


def _register_gated(fns: tuple[Any, ...], env: str) -> None:
    """Register tools whose eval gate has not passed only when env is "1".

    The functions stay importable either way, so tests and the eval script
    call them directly.
    """
    if os.environ.get(env) == "1":
        for fn in fns:
            mcp.tool()(fn)
