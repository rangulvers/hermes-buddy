"""hermes-buddy — Hermes plugin for OLED status display.

Hooks into Hermes tool/LLM events, writes /tmp/hermes-status.json,
and starts a FastAPI server on port 3004 (HERMES_BUDDY_PORT) in a
daemon thread so it lives exactly as long as the Hermes process.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Add plugin directory to sys.path so server.py can be imported as a sibling module.
# Necessary because Hermes loads plugins via spec_from_file_location,
# which does not add the plugin dir to sys.path automatically.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

STATUS_FILE = Path(f"/tmp/hermes-status-{os.getpid()}.json")

_last_usage_key: tuple | None = None
_usage_lock = threading.Lock()
_write_lock = threading.Lock()


def _write_status(updates: dict[str, Any], tokens_delta: int = 0) -> None:
    """Atomically write updates merged into STATUS_FILE under an exclusive flock."""
    with _write_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(STATUS_FILE.parent), prefix=".hermes-status-", suffix=".tmp"
        )
        try:
            current: dict[str, Any] = {}
            if STATUS_FILE.exists():
                try:
                    current = json.loads(STATUS_FILE.read_text())
                except Exception:
                    pass
            current.update(updates)
            if tokens_delta > 0:
                current["tokens_today"] = current.get("tokens_today", 0) + tokens_delta
            current["ts"] = int(time.time())
            with os.fdopen(tmp_fd, "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    json.dump(current, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            os.replace(tmp_path, STATUS_FILE)
        except Exception as exc:
            logger.warning("hermes-buddy: status write failed: %s", exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def on_pre_tool_call(*, tool_name: str = "", **_: Any) -> None:
    _write_status({"running": 1, "tool": tool_name, "msg": tool_name})


def on_post_tool_call(**_: Any) -> None:
    _write_status({"tool": "", "msg": "Thinking..."})


def _accumulate_tokens(task_id: str, api_call_count: int, usage: Any) -> int:
    """Return token delta; returns 0 if (task_id, api_call_count) already seen.

    Guards against double-counting when both post_api_request and post_llm_call
    fire for the same LLM turn in newer Hermes versions.
    """
    global _last_usage_key
    if not usage or not isinstance(usage, dict):
        return 0
    key = (task_id, api_call_count)
    with _usage_lock:
        if key == _last_usage_key:
            return 0
        _last_usage_key = key
    return usage.get("input_tokens", 0) + usage.get("output_tokens", 0)


def on_post_api_request(
    *,
    task_id: str = "",
    api_call_count: int = 0,
    usage: Any = None,
    **_: Any,
) -> None:
    delta = _accumulate_tokens(task_id, api_call_count, usage)
    _write_status({"running": 1, "msg": "Thinking...", "tool": ""}, tokens_delta=delta)


def on_post_llm_call(
    *,
    task_id: str = "",
    api_call_count: int = 0,
    usage: Any = None,
    **_: Any,
) -> None:
    on_post_api_request(task_id=task_id, api_call_count=api_call_count, usage=usage)


def _start_server() -> None:
    port = int(os.environ.get("HERMES_BUDDY_PORT", "3004"))
    try:
        import uvicorn
        from server import app  # server.py is on sys.path via _PLUGIN_DIR insertion above
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as exc:
        logger.warning("hermes-buddy: server failed to start on port %d: %s", port, exc)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("post_llm_call", on_post_llm_call)

    t = threading.Thread(target=_start_server, daemon=True, name="hermes-buddy-server")
    t.start()
    logger.info(
        "hermes-buddy: server thread started on port %s",
        os.environ.get("HERMES_BUDDY_PORT", "3004"),
    )
