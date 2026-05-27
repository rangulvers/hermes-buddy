"""hermes-buddy server — FastAPI status + buddy hatch server.

Reads /tmp/hermes-status.json (written by Hermes plugin hooks) and
serves it at GET /status with buddy hatch progression. Loaded by
__init__.py via sys.path insertion; not intended to be run directly.
"""
from __future__ import annotations

import fcntl
import hmac
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("hermes-buddy")

_STATUS_DIR  = Path("/tmp")
_STATUS_GLOB = "hermes-status-*.json"
BUDDIES_FILE = Path.home() / ".hermes-buddy" / "buddies.json"
BUDDIES_FILE.parent.mkdir(exist_ok=True)

STALE_AFTER = 300
ADMIN_TOKEN = os.environ.get("HERMES_BUDDY_TOKEN", "hermes-buddy-changeme")
TOKENS_BASE = 1_000

if ADMIN_TOKEN == "hermes-buddy-changeme":
    print(
        "WARNING: HERMES_BUDDY_TOKEN is set to the default value. "
        "Set HERMES_BUDDY_TOKEN in ~/.hermes/.env before exposing this server.",
        file=sys.stderr,
    )

BUDDY_TYPES    = ["BOT","CAT","OWL","GHOST","ALIEN","BEAR","FOX","DRAGON","BUNNY","CRYSTAL"]
BUDDY_RARITIES = ["Common","Common","Uncommon","Rare","Epic","Common","Rare","Legendary","Uncommon","Mystical"]
BUDDY_NAMES = {
    0: ["Bolt","Chip","Circuit","Cog","Gear","Pixel","Spark","Static","Volt","Wire"],
    1: ["Byte","Cache","Claw","Cursor","Hiss","Mew","Patch","Purr","Tab","Tail"],
    2: ["Binary","Codec","Hoot","Lumen","Null","Query","Sage","Sigma","Twig","Woo"],
    3: ["Async","Boo","Daemon","Echo","Flicker","Glitch","Phantom","Vapor","Void","Zero"],
    4: ["Alpha","Delta","Flux","Gamma","Helix","Nova","Orb","Pulse","Qubit","Zeta"],
    5: ["Blob","Buff","Chunk","Dense","Fuzzy","Hash","Heap","Stack","Stub","Thick"],
    6: ["Cache","Clever","Debug","Delta","Fleet","Parse","Quick","Sharp","Swift","Trace"],
    7: ["Blaze","Crypt","Forge","Glyph","Hex","Kernel","Root","Rune","Shell","Smog"],
    8: ["Buffer","Hop","Jump","Loop","Nibble","Ping","Quick","Skip","Sprint","Tick"],
    9: ["Array","Core","Facet","Grid","Index","Lattice","Matrix","Node","Prism","Vector"],
}
SPAWN_WEIGHTS = [
    (0, 200), (1, 370), (5, 500), (8, 620), (2, 745),
    (6, 870), (3, 945), (4, 975), (7, 995), (9, 1000),
]

# Tool name → display type int (matches claude-buddy tool_labels on OLED)
# 0=unknown, 1=BASH, 2=FILE, 3=WEB, 4=AGNT, 5=PLAN
TOOL_TYPES = {
    "bash": 1, "run_terminal_cmd": 1, "execute_command": 1,
    "read_file": 2, "write_file": 2, "create_file": 2, "edit_file": 2,
    "grep": 2, "find_files": 2, "list_directory": 2,
    "web_search": 3, "web_fetch": 3, "browser": 3, "fetch_url": 3,
    "agent": 4, "spawn_agent": 4, "delegate": 4,
}

_BUDDY_NAME_RE  = re.compile(r'^[A-Za-z0-9 _\-]+$')
_DEVICE_ID_RE   = re.compile(r'^[A-Za-z0-9_\-:.]+$')

app = FastAPI(title="Hermes Buddy Server")
_buddies_lock = threading.Lock()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
security = HTTPBearer()

_default: dict = {
    "running": 0, "tool": "", "msg": "Idle",
    "buddy_type": 0, "buddy_name": "", "buddy_rarity": "",
    "buddy_level": 1, "buddy_tokens": 0, "levelup": False,
    "tokens_today": 0, "ts": 0,
}


def load_buddies() -> dict:
    if not BUDDIES_FILE.exists():
        return {}
    try:
        with BUDDIES_FILE.open("r") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                return json.load(fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("buddies.json unreadable: %s", exc)
        return {}


def save_buddies(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(BUDDIES_FILE.parent), prefix=".buddies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        os.replace(tmp, BUDDIES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_active_sessions() -> list[dict]:
    """Read all per-PID status files. Clean up dead sessions. Apply stale timeout."""
    now = time.time()
    active: list[dict] = []
    for p in _STATUS_DIR.glob(_STATUS_GLOB):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        age = now - data.get("ts", 0)
        if age > STALE_AFTER * 2:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        if age > STALE_AFTER and data.get("running"):
            data["running"] = 0
            data["msg"] = "Idle"
            data["tool"] = ""
        active.append(data)
    return active


def roll_buddy() -> tuple[int, str]:
    roll = random.randint(0, 999)
    for type_id, upper in SPAWN_WEIGHTS:
        if roll < upper:
            return type_id, random.choice(BUDDY_NAMES[type_id])
    return 9, random.choice(BUDDY_NAMES[9])


def _compute_level(tokens_total: int) -> int:
    if tokens_total <= 0:
        return 1
    return max(1, int(math.log2(tokens_total / TOKENS_BASE + 1)) + 1)


def _tokens_for_level(level: int) -> int:
    return TOKENS_BASE * (2 ** (level - 1) - 1)


def get_or_assign(device_id: str, tokens_today: int = 0) -> tuple[dict, bool]:
    with _buddies_lock:
        buddies = load_buddies()
        if device_id not in buddies:
            type_id, name = roll_buddy()
            buddies[device_id] = {
                "type": type_id, "name": name,
                "type_name": BUDDY_TYPES[type_id], "rarity": BUDDY_RARITIES[type_id],
                "assigned_at": int(time.time()),
                "tokens_total": 0, "tokens_last": 0,
                "level": 1, "level_notified": 1,
            }
        buddy = buddies[device_id]
        last  = buddy.get("tokens_last", 0)
        total = buddy.get("tokens_total", 0)
        total += tokens_today - last if tokens_today >= last else tokens_today
        buddy["tokens_total"] = total
        buddy["tokens_last"]  = tokens_today
        new_level = _compute_level(total)
        levelup   = new_level > buddy.get("level_notified", 1)
        buddy["level"] = new_level
        if levelup:
            buddy["level_notified"] = new_level
        buddy["last_seen"] = int(time.time())
        buddies[device_id] = buddy
        save_buddies(buddies)
    return buddy, levelup


def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not hmac.compare_digest(creds.credentials, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status(device_id: str = ""):
    sessions = _read_active_sessions()

    running_sessions = [s for s in sessions if s.get("running")]
    if running_sessions:
        data = max(running_sessions, key=lambda s: s.get("ts", 0))
    elif sessions:
        data = max(sessions, key=lambda s: s.get("ts", 0))
    else:
        data = dict(_default)

    tokens_today = sum(s.get("tokens_today", 0) for s in sessions
                       if isinstance(s.get("tokens_today"), int))
    data["tokens_today"] = tokens_today
    data["sessions"] = len(running_sessions)

    if device_id and (len(device_id) > 64 or not _DEVICE_ID_RE.match(device_id)):
        device_id = ""

    buddy, levelup = None, False
    if device_id:
        buddy, levelup = get_or_assign(device_id, tokens_today)

    tool = data.get("tool", "")
    data["tool_type"] = TOOL_TYPES.get(tool, 0) if data.get("running") else 0

    if buddy:
        lvl      = buddy["level"]
        cost     = TOKENS_BASE * (2 ** (lvl - 1))
        in_level = buddy["tokens_total"] - _tokens_for_level(lvl)
        level_pct = min(100, max(0, int(in_level * 100 / cost))) if cost > 0 else 0
        data["buddy_type"]      = buddy["type"]
        data["buddy_name"]      = buddy["name"]
        data["buddy_rarity"]    = buddy["rarity"]
        data["buddy_level"]     = lvl
        data["buddy_level_pct"] = level_pct
        data["buddy_tokens"]    = buddy["tokens_total"]
        data["levelup"]         = levelup
    else:
        data.setdefault("buddy_type",   0)
        data.setdefault("buddy_name",   "")
        data.setdefault("buddy_rarity", "")
        data.setdefault("buddy_level",  1)
        data.setdefault("buddy_tokens", 0)
        data.setdefault("levelup",      False)

    return JSONResponse(data)


class AssignRequest(BaseModel):
    device_id:  str = Field(..., max_length=64)
    buddy_type: int
    buddy_name: str = Field(..., min_length=1, max_length=32)

    @field_validator("buddy_name")
    @classmethod
    def name_safe_chars(cls, v: str) -> str:
        if not _BUDDY_NAME_RE.match(v):
            raise ValueError("buddy_name may only contain letters, digits, spaces, hyphens, underscores")
        return v

    @field_validator("device_id")
    @classmethod
    def device_id_safe_chars(cls, v: str) -> str:
        if not re.match(r'^[A-Za-z0-9_\-:.]+$', v):
            raise ValueError("device_id may only contain letters, digits, underscores, hyphens, colons, dots")
        return v


@app.post("/admin/assign", dependencies=[Depends(require_admin)])
def admin_assign(req: AssignRequest):
    if req.buddy_type < 0 or req.buddy_type > 9:
        raise HTTPException(status_code=400, detail="buddy_type must be 0-9")
    with _buddies_lock:
        buddies = load_buddies()
        prev = buddies.get(req.device_id, {})
        buddies[req.device_id] = {
            "type": req.buddy_type, "name": req.buddy_name,
            "type_name": BUDDY_TYPES[req.buddy_type], "rarity": BUDDY_RARITIES[req.buddy_type],
            "assigned_at": int(time.time()), "last_seen": prev.get("last_seen", 0),
            "tokens_total": prev.get("tokens_total", 0), "tokens_last": prev.get("tokens_last", 0),
            "level": prev.get("level", 1), "level_notified": prev.get("level_notified", 1),
            "admin_assigned": True,
        }
        save_buddies(buddies)
        buddy = buddies[req.device_id]
    return {"ok": True, "device_id": req.device_id, "buddy_name": buddy["name"], **buddy}


@app.get("/admin/buddies", dependencies=[Depends(require_admin)])
def admin_buddies():
    return load_buddies()
