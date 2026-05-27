import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from server import (
    ADMIN_TOKEN,
    BUDDY_TYPES,
    BUDDY_RARITIES,
    TOKENS_BASE,
    _compute_level,
    _tokens_for_level,
    app,
    get_or_assign,
    roll_buddy,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    import server as srv
    status = tmp_path / "hermes-status.json"
    buddies = tmp_path / "buddies.json"
    monkeypatch.setattr(srv, "STATUS_FILE", status)
    monkeypatch.setattr(srv, "BUDDIES_FILE", buddies)
    monkeypatch.setattr(srv, "ADMIN_TOKEN", "test-token")
    yield {"status": status, "buddies": buddies}


# ── Health ────────────────────────────────────────────────────────────────────

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── Status — no file ──────────────────────────────────────────────────────────

def test_status_no_file_returns_idle(isolated_files):
    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] == 0
    assert data["msg"] == "Idle"


# ── Status — stale file → idle ────────────────────────────────────────────────

def test_status_stale_running_becomes_idle(isolated_files):
    isolated_files["status"].write_text(json.dumps({
        "running": 1, "tool": "bash", "msg": "bash",
        "tokens_today": 0, "ts": int(time.time()) - 400,
    }))
    r = client.get("/status")
    assert r.json()["running"] == 0
    assert r.json()["msg"] == "Idle"


def test_status_fresh_running_stays_running(isolated_files):
    isolated_files["status"].write_text(json.dumps({
        "running": 1, "tool": "bash", "msg": "bash",
        "tokens_today": 0, "ts": int(time.time()),
    }))
    r = client.get("/status")
    assert r.json()["running"] == 1


# ── Tool type mapping ─────────────────────────────────────────────────────────

def test_tool_type_bash(isolated_files):
    isolated_files["status"].write_text(json.dumps({
        "running": 1, "tool": "bash", "msg": "bash",
        "tokens_today": 0, "ts": int(time.time()),
    }))
    r = client.get("/status")
    assert r.json()["tool_type"] == 1


def test_tool_type_read_file(isolated_files):
    isolated_files["status"].write_text(json.dumps({
        "running": 1, "tool": "read_file", "msg": "read_file",
        "tokens_today": 0, "ts": int(time.time()),
    }))
    r = client.get("/status")
    assert r.json()["tool_type"] == 2


def test_tool_type_unknown(isolated_files):
    isolated_files["status"].write_text(json.dumps({
        "running": 1, "tool": "some_custom_tool", "msg": "some_custom_tool",
        "tokens_today": 0, "ts": int(time.time()),
    }))
    r = client.get("/status")
    assert r.json()["tool_type"] == 0


# ── Buddy hatch ───────────────────────────────────────────────────────────────

def test_buddy_assigned_on_first_poll(isolated_files):
    r = client.get("/status?device_id=test-device-1")
    assert r.status_code == 200
    data = r.json()
    assert data["buddy_name"] != ""
    assert data["buddy_type"] in range(10)
    assert data["buddy_rarity"] in ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mystical"]
    assert data["buddy_level"] == 1


def test_same_buddy_on_repeat_poll(isolated_files):
    r1 = client.get("/status?device_id=test-device-2")
    r2 = client.get("/status?device_id=test-device-2")
    assert r1.json()["buddy_name"] == r2.json()["buddy_name"]
    assert r1.json()["buddy_type"] == r2.json()["buddy_type"]


def test_different_devices_may_get_different_buddies(isolated_files):
    types = set()
    for i in range(20):
        r = client.get(f"/status?device_id=device-{i}")
        types.add(r.json()["buddy_type"])
    assert len(types) > 1


# ── Level progression ─────────────────────────────────────────────────────────

def test_compute_level_zero_tokens():
    assert _compute_level(0) == 1


def test_compute_level_at_threshold():
    # LV2 threshold = TOKENS_BASE * (2^1 - 1) = 1000
    assert _compute_level(TOKENS_BASE) == 2


def test_compute_level_lv3():
    # LV3 threshold = TOKENS_BASE * (2^2 - 1) = 3000
    assert _compute_level(3 * TOKENS_BASE) == 3


def test_tokens_for_level_1():
    assert _tokens_for_level(1) == 0


def test_tokens_for_level_2():
    assert _tokens_for_level(2) == TOKENS_BASE


# ── Admin endpoints ───────────────────────────────────────────────────────────

def test_admin_assign_valid(isolated_files):
    r = client.post(
        "/admin/assign",
        json={"device_id": "test-dev", "buddy_type": 7, "buddy_name": "Forge"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200
    assert r.json()["buddy_name"] == "Forge" or r.json()["name"] == "Forge"


def test_admin_assign_invalid_token(isolated_files):
    r = client.post(
        "/admin/assign",
        json={"device_id": "test-dev", "buddy_type": 0, "buddy_name": "Bolt"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403


def test_admin_assign_invalid_buddy_type(isolated_files):
    r = client.post(
        "/admin/assign",
        json={"device_id": "test-dev", "buddy_type": 99, "buddy_name": "Bolt"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 400


def test_admin_buddies_lists_devices(isolated_files):
    client.get("/status?device_id=dev-a")
    client.get("/status?device_id=dev-b")
    r = client.get("/admin/buddies", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    data = r.json()
    assert "dev-a" in data
    assert "dev-b" in data
