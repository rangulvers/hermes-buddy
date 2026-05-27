import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "hermes_buddy_plugin", PLUGIN_DIR / "__init__.py"
)
hermes_buddy_plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_buddy_plugin"] = hermes_buddy_plugin
_spec.loader.exec_module(hermes_buddy_plugin)


@pytest.fixture(autouse=True)
def tmp_status(tmp_path, monkeypatch):
    """Redirect STATUS_FILE to a temp path for each test."""
    # Re-exec module to reset _last_usage_key between tests.
    # importlib.reload() requires the module name to be findable on sys.path,
    # which isn't possible for a spec_from_file_location with a synthetic name,
    # so we re-exec the spec's loader directly instead.
    _spec.loader.exec_module(hermes_buddy_plugin)
    status = tmp_path / "hermes-status.json"
    monkeypatch.setattr(hermes_buddy_plugin, "STATUS_FILE", status)
    yield status


def read_status(path: Path) -> dict:
    return json.loads(path.read_text())


def test_write_status_creates_file(tmp_status):
    hermes_buddy_plugin._write_status({"running": 1, "tool": "bash"})
    data = read_status(tmp_status)
    assert data["running"] == 1
    assert data["tool"] == "bash"
    assert "ts" in data


def test_write_status_merges_existing(tmp_status):
    hermes_buddy_plugin._write_status({"running": 1, "tokens_today": 100})
    hermes_buddy_plugin._write_status({"running": 0})
    data = read_status(tmp_status)
    assert data["tokens_today"] == 100  # preserved
    assert data["running"] == 0


def test_on_pre_tool_call_sets_running_and_tool(tmp_status):
    hermes_buddy_plugin.on_pre_tool_call(tool_name="read_file")
    data = read_status(tmp_status)
    assert data["running"] == 1
    assert data["tool"] == "read_file"
    assert data["msg"] == "read_file"


def test_on_post_tool_call_clears_tool(tmp_status):
    hermes_buddy_plugin._write_status({"running": 1, "tool": "bash"})
    hermes_buddy_plugin.on_post_tool_call()
    data = read_status(tmp_status)
    assert data["tool"] == ""
    assert data["msg"] == "Thinking..."


def test_accumulate_tokens_deduplicates(tmp_status):
    hermes_buddy_plugin._last_usage_key = None
    usage = {"input_tokens": 100, "output_tokens": 50}
    delta1 = hermes_buddy_plugin._accumulate_tokens("task1", 1, usage)
    delta2 = hermes_buddy_plugin._accumulate_tokens("task1", 1, usage)  # same key
    assert delta1 == 150
    assert delta2 == 0  # deduplicated


def test_accumulate_tokens_different_keys(tmp_status):
    hermes_buddy_plugin._last_usage_key = None
    usage = {"input_tokens": 100, "output_tokens": 50}
    delta1 = hermes_buddy_plugin._accumulate_tokens("task1", 1, usage)
    delta2 = hermes_buddy_plugin._accumulate_tokens("task1", 2, usage)  # different api_call_count
    assert delta1 == 150
    assert delta2 == 150


def test_on_post_api_request_accumulates_tokens(tmp_status):
    hermes_buddy_plugin._last_usage_key = None
    hermes_buddy_plugin.on_post_api_request(
        task_id="t1", api_call_count=1,
        usage={"input_tokens": 200, "output_tokens": 50}
    )
    data = read_status(tmp_status)
    assert data["tokens_today"] == 250
    assert data["running"] == 1
    assert data["msg"] == "Thinking..."


def test_on_post_llm_call_delegates_to_post_api_request(tmp_status):
    hermes_buddy_plugin._last_usage_key = None
    hermes_buddy_plugin.on_post_llm_call(
        task_id="t2", api_call_count=1,
        usage={"input_tokens": 10, "output_tokens": 5}
    )
    data = read_status(tmp_status)
    assert data["tokens_today"] == 15


def test_register_wires_hooks_and_starts_thread(tmp_status):
    ctx = MagicMock()
    with patch.object(hermes_buddy_plugin, "_start_server"):
        with patch("threading.Thread") as mock_thread:
            mock_t = MagicMock()
            mock_thread.return_value = mock_t
            hermes_buddy_plugin.register(ctx)
    assert ctx.register_hook.call_count == 4
    hook_names = [call[0][0] for call in ctx.register_hook.call_args_list]
    assert set(hook_names) == {"pre_tool_call", "post_tool_call", "post_api_request", "post_llm_call"}
    mock_t.start.assert_called_once()
