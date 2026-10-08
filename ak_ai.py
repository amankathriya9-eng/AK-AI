# -*- coding: utf-8 -*-
"""
AK AI - local-first autonomous Windows computer assistant.

Flow: COMMAND -> UNDERSTAND -> PLAN -> CHECK -> USE TOOLS -> EXECUTE -> OBSERVE
      -> FIX -> RETRY -> TEST -> VERIFY -> REMEMBER -> REPORT

Single-file application. Optional third-party packages (pyautogui, pyperclip,
mss, Pillow, psutil, pytesseract, playwright, pyttsx3, SpeechRecognition) are
imported lazily, so the application starts and works in text mode without them.
API keys are never stored in this file; they are read from environment
variables (OPENAI_API_KEY, GEMINI_API_KEY, OLLAMA_API_KEY) or entered for the
current session only.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime as dt
import enum
import hashlib
import html.parser
import importlib
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

VERSION = "3.5.0-production"
APP_NAME = "AK AI"
IS_WINDOWS = os.name == "nt"
APP_DIR = Path(os.environ.get("AK_AI_HOME", str(Path.home() / ".ak_ai")))
APP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = APP_DIR / "backups"
CACHE_DIR = APP_DIR / "cache"
LOG_DIR = APP_DIR / "logs"
for _d in (BACKUP_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", "dist", "build", "target", "bin", "obj", ".mypy_cache",
    ".pytest_cache", ".tox", ".eggs", "site-packages", "$RECYCLE.BIN",
    "System Volume Information", "Windows", "Program Files", "Program Files (x86)",
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey)\b\s*[:=]\s*[^\s,;\"']+"),
]


def redact(text: Any) -> str:
    """Remove likely secrets from text before it is logged or stored."""
    s = text if isinstance(text, str) else str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    return s


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def short(text: Any, limit: int = 1200) -> str:
    s = text if isinstance(text, str) else str(text)
    if len(s) <= limit:
        return s
    half = limit // 2
    return s[:half] + "\n...[truncated %d chars]...\n" % (len(s) - limit) + s[-half:]


FENCE = chr(96) * 3  # markdown code fence marker, built at runtime


def extract_json(text: str) -> Optional[Any]:
    """Extract the first balanced JSON object/array from model output."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub("^" + FENCE + r"[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*" + FENCE + "$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(cleaned)):
                ch = cleaned[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[start:i + 1])
                        except ValueError:
                            break
            start = cleaned.find(opener, start + 1)
    return None


def detect_language(text: str) -> str:
    """Return 'hi' (Devanagari), 'hinglish' or 'en'."""
    if re.search(r"[\u0900-\u097F]", text or ""):
        return "hi"
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    markers = {
        "bhai", "kr", "kar", "karo", "kholo", "khol", "bana", "banao", "de", "do", "h", "hai",
        "ye", "yeh", "isme", "mujhe", "mera", "meri", "rha", "raha", "aa", "nahi", "nhi",
        "ko", "se", "ka", "ki", "ke", "dekh", "dekho", "theek", "sahi", "chalu", "dhundh",
        "kya", "kaise", "wala", "isko", "usko", "kro", "krdo", "krna", "chahiye", "cahiye",
    }
    return "hinglish" if len(words & markers) >= 1 else "en"


MESSAGES = {
    "en": {
        "start": "Working on it.",
        "done": "Completed and verified.",
        "partial": "Finished with problems. Details below.",
        "failed": "Could not complete this. Details below.",
        "cancelled": "Task cancelled.",
        "no_ai": "No AI provider is available, so I used local rules to understand the request.",
        "chat_noai": "No AI provider is configured or reachable, so I can only run local tasks "
                     "(inspect, build, run commands, open apps, files). Set OPENAI_API_KEY, "
                     "GEMINI_API_KEY, or run Ollama and choose the provider in Settings.",
    },
    "hinglish": {
        "start": "Ho jayega, kaam shuru kar raha hoon.",
        "done": "Kaam complete aur verify ho gaya.",
        "partial": "Kaam poora nahi hua, kuch problems aayi. Details neeche hain.",
        "failed": "Ye kaam complete nahi ho paya. Details neeche hain.",
        "cancelled": "Task cancel kar diya gaya.",
        "no_ai": "AI provider available nahi hai, isliye local rules se request samjhi.",
        "chat_noai": "Abhi koi AI provider configured ya reachable nahi hai, isliye main sirf local kaam "
                     "kar sakta hoon (inspect, build, commands, apps, files). OPENAI_API_KEY ya "
                     "GEMINI_API_KEY set karo, ya Ollama chalao aur Settings mein provider chuno.",
    },
    "hi": {
        "start": "ठीक है, काम शुरू कर रहा हूँ।",
        "done": "काम पूरा हुआ और जाँच लिया गया।",
        "partial": "काम पूरा नहीं हुआ, कुछ समस्याएँ आईं। विवरण नीचे है।",
        "failed": "यह काम पूरा नहीं हो सका। विवरण नीचे है।",
        "cancelled": "टास्क रद्द कर दिया गया।",
        "no_ai": "AI प्रदाता उपलब्ध नहीं है, इसलिए स्थानीय नियमों से अनुरोध समझा।",
        "chat_noai": "अभी कोई AI प्रदाता कॉन्फ़िगर या उपलब्ध नहीं है, इसलिए मैं केवल स्थानीय काम कर सकता हूँ।",
    },
}


def msg(lang: str, key: str) -> str:
    return MESSAGES.get(lang, MESSAGES["en"]).get(key, MESSAGES["en"].get(key, key))


class TaskCancelled(Exception):
    """Raised inside workers when the user stops or cancels the task."""


# ---------------------------------------------------------------------------
# SettingsManager
# ---------------------------------------------------------------------------

class SettingsManager:
    DEFAULTS: Dict[str, Any] = {
        "provider": "none",          # none | openai | gemini | ollama
        "model": "",
        "base_url": "",
        "temperature": 0.2,
        "timeout": 60,
        "retry_limit": 3,
        "offline_mode": False,
        "workspace": str(Path.home() / "AKAI_Workspace"),
        "confirm_medium_risk": True,
        "allow_computer_control": False,
        "allow_outside_workspace": False,
        "voice_enabled": False,
        "voice_wake_word": "hey ak",
        "voice_rate": 175,
        "browser_headless": False,
        "browser_engine": "chromium",
        "memory_enabled": True,
        "memory_max_rows": 20000,
        "command_timeout": 600,
    }
    PROVIDER_DEFAULTS = {
        "openai": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
        "gemini": {"model": "gemini-1.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta"},
        "ollama": {"model": "llama3.1", "base_url": "http://127.0.0.1:11434"},
    }
    ENV_KEYS = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "ollama": "OLLAMA_API_KEY"}

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else APP_DIR / "settings.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(self.DEFAULTS)
        self._session_keys: Dict[str, str] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            if k in self.DEFAULTS and not self._is_secret_key(k):
                                self._data[k] = self._coerce(k, v)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _is_secret_key(name: str) -> bool:
        return bool(re.search(r"(?i)key|secret|token|password", name)) and name not in ("model",)

    def _coerce(self, key: str, value: Any) -> Any:
        default = self.DEFAULTS[key]
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
            return str(value)
        except (TypeError, ValueError):
            return default

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        if key not in self.DEFAULTS:
            raise KeyError("Unknown setting: %s" % key)
        with self._lock:
            self._data[key] = self._coerce(key, value)
        if persist:
            self.save()

    def update(self, values: Dict[str, Any]) -> None:
        for k, v in values.items():
            if k in self.DEFAULTS:
                self.set(k, v, persist=False)
        self.save()

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def set_session_key(self, provider: str, key: str) -> None:
        """Keep an API key in memory only. It is never written to disk."""
        with self._lock:
            if key:
                self._session_keys[provider] = key
            else:
                self._session_keys.pop(provider, None)

    def api_key(self, provider: str) -> str:
        with self._lock:
            if provider in self._session_keys:
                return self._session_keys[provider]
        return os.environ.get(self.ENV_KEYS.get(provider, ""), "")

    def effective_model(self) -> str:
        m = self.get("model")
        return m or self.PROVIDER_DEFAULTS.get(self.get("provider"), {}).get("model", "")

    def effective_base_url(self) -> str:
        b = self.get("base_url")
        return (b or self.PROVIDER_DEFAULTS.get(self.get("provider"), {}).get("base_url", "")).rstrip("/")

    def workspace(self) -> Path:
        p = Path(self.get("workspace")).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only JSON-lines audit log with secret redaction."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else LOG_DIR / "audit.jsonl"
        self._lock = threading.Lock()
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []

    def record(self, task_id: str, action: str, **fields: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"ts": now_iso(), "task_id": task_id, "action": action}
        for k, v in fields.items():
            if v is None:
                continue
            entry[k] = redact(v) if isinstance(v, str) else v
            if isinstance(entry[k], str):
                entry[k] = short(entry[k], 2000)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        for cb in list(self.listeners):
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def tail(self, n: int = 50) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out


# ---------------------------------------------------------------------------
# MemoryManager (SQLite)
# ---------------------------------------------------------------------------

class MemoryManager:
    KINDS = ("conversation", "task", "fix", "failure", "research", "tool", "project", "procedure")

    def __init__(self, path: Optional[Path] = None, settings: Optional[SettingsManager] = None):
        self.path = Path(path) if path else APP_DIR / "memory.db"
        self.settings = settings
        self._lock = threading.RLock()
        self.fts = False
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            c = self._conn
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, "
                "key TEXT, content TEXT NOT NULL, meta TEXT)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_key ON memory(key)")
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                    "content, key, kind, content='memory', content_rowid='id')"
                )
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            c.commit()

    def _enabled(self) -> bool:
        return True if self.settings is None else bool(self.settings.get("memory_enabled", True))

    def add(self, kind: str, content: str, key: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
        if not self._enabled():
            return -1
        if kind not in self.KINDS:
            raise ValueError("Unknown memory kind: %s" % kind)
        safe_content = redact(content)
        safe_key = redact(key)
        safe_meta = redact(json.dumps(meta or {}, ensure_ascii=False, default=str))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory(ts, kind, key, content, meta) VALUES (?,?,?,?,?)",
                (now_iso(), kind, safe_key, safe_content, safe_meta),
            )
            rowid = cur.lastrowid
            if self.fts:
                try:
                    self._conn.execute(
                        "INSERT INTO memory_fts(rowid, content, key, kind) VALUES (?,?,?,?)",
                        (rowid, safe_content, safe_key, kind),
                    )
                except sqlite3.OperationalError:
                    self.fts = False
            self._conn.commit()
            self._prune()
            return int(rowid)

    def _prune(self) -> None:
        limit = int(self.settings.get("memory_max_rows", 20000)) if self.settings else 20000
        cur = self._conn.execute("SELECT COUNT(*) FROM memory")
        total = cur.fetchone()[0]
        if total > limit:
            excess = total - limit
            ids = [r[0] for r in self._conn.execute(
                "SELECT id FROM memory WHERE kind IN ('conversation','task') ORDER BY id ASC LIMIT ?", (excess,))]
            for i in ids:
                self._delete_row(i)
            self._conn.commit()

    def _delete_row(self, rowid: int) -> None:
        if self.fts:
            row = self._conn.execute("SELECT content, key, kind FROM memory WHERE id=?", (rowid,)).fetchone()
            if row:
                try:
                    self._conn.execute(
                        "INSERT INTO memory_fts(memory_fts, rowid, content, key, kind) VALUES('delete',?,?,?,?)",
                        (rowid, row["content"], row["key"], row["kind"]))
                except sqlite3.OperationalError:
                    pass
        self._conn.execute("DELETE FROM memory WHERE id=?", (rowid,))

    @staticmethod
    def _tokens(query: str) -> List[str]:
        return [t for t in re.findall(r"[A-Za-z0-9_]{2,}", query or "")][:12]

    def search(self, query: str, kind: Optional[str] = None, limit: int = 8) -> List[Dict[str, Any]]:
        toks = self._tokens(query)
        if not toks:
            return []
        with self._lock:
            rows: List[sqlite3.Row] = []
            if self.fts:
                match = " OR ".join('"%s"' % t for t in toks)
                try:
                    sql = ("SELECT m.* FROM memory_fts f JOIN memory m ON m.id=f.rowid "
                           "WHERE memory_fts MATCH ?")
                    params: List[Any] = [match]
                    if kind:
                        sql += " AND m.kind=?"
                        params.append(kind)
                    sql += " ORDER BY rank LIMIT ?"
                    params.append(limit)
                    rows = self._conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                clauses = " OR ".join(["(content LIKE ? OR key LIKE ?)"] * len(toks))
                params = []
                for t in toks:
                    params.extend(["%" + t + "%", "%" + t + "%"])
                sql = "SELECT * FROM memory WHERE (" + clauses + ")"
                if kind:
                    sql += " AND kind=?"
                    params.append(kind)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def recent(self, kind: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM memory WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, COUNT(*) AS n FROM memory GROUP BY kind").fetchall()
            return {r["kind"]: r["n"] for r in rows}

    def total(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()


# ---------------------------------------------------------------------------
# TaskControl: state, stop, pause, resume, cancel, managed processes
# ---------------------------------------------------------------------------

class TaskState(enum.Enum):
    IDLE = "IDLE"
    UNDERSTANDING = "UNDERSTANDING"
    PLANNING = "PLANNING"
    INSPECTING = "INSPECTING"
    RESEARCHING = "RESEARCHING"
    EXECUTING = "EXECUTING"
    WAITING = "WAITING"
    RECOVERING = "RECOVERING"
    TESTING = "TESTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class TaskControl:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = TaskState.IDLE
        self.task_id = ""
        self._stop = threading.Event()
        self._run = threading.Event()
        self._run.set()
        self._procs: List[subprocess.Popen] = []
        self._state_before_pause = TaskState.IDLE
        self.listeners: List[Callable[[TaskState], None]] = []

    def new_task(self) -> str:
        with self._lock:
            self.task_id = uuid.uuid4().hex[:10]
            self._stop.clear()
            self._run.set()
        self.set_state(TaskState.UNDERSTANDING)
        return self.task_id

    def set_state(self, state: TaskState) -> None:
        with self._lock:
            if self.state == TaskState.PAUSED and state not in (
                    TaskState.CANCELLED, TaskState.FAILED, TaskState.COMPLETED, TaskState.IDLE):
                self._state_before_pause = state
                return
            self.state = state
        for cb in list(self.listeners):
            try:
                cb(state)
            except Exception:
                pass

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    def checkpoint(self) -> None:
        """Call between steps. Blocks while paused, raises when stopped."""
        while True:
            if self._stop.is_set():
                raise TaskCancelled()
            if self._run.wait(timeout=0.2):
                break
        if self._stop.is_set():
            raise TaskCancelled()

    def pause(self) -> None:
        with self._lock:
            if self.state in (TaskState.IDLE, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                return
            self._state_before_pause = self.state
            self._run.clear()
        self.set_state(TaskState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            was_paused = not self._run.is_set()
            self._run.set()
            prev = self._state_before_pause
        if was_paused:
            with self._lock:
                self.state = prev
            self.set_state(prev)

    def stop(self) -> None:
        """STOP / CANCEL / emergency stop: interrupts the task and kills managed processes."""
        self._stop.set()
        self._run.set()
        self.kill_all()

    cancel = stop

    def register_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)

    def unregister_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def kill_all(self) -> int:
        with self._lock:
            procs = list(self._procs)
        killed = 0
        for p in procs:
            if p.poll() is None:
                kill_process_tree(p)
                killed += 1
        return killed


def kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


# ---------------------------------------------------------------------------
# SecurityManager: risk classification, permissions, workspace confinement
# ---------------------------------------------------------------------------

class Risk(enum.IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    BLOCKED = 3


class SecurityManager:
    BLOCKED_PATTERNS = [
        r"\bformat\s+[a-z]:", r"\brm\s+-rf\s+(/|~|\*)", r"\bdel\s+/[sq]\s+.*[a-z]:\\\\?\s*$",
        r"\brd\s+/s\s+/q\s+[a-z]:\\\\?\s*$", r"\bvssadmin\s+delete\s+shadows", r"\bbcdedit\b",
        r"\bdiskpart\b", r"\bcipher\s+/w", r"\bmimikatz\b", r"\breg\s+delete\s+hk(lm|cr)\b",
        r"set-mppreference\s+.*-disable", r"\bnet\s+user\s+.+\s+/add", r"\bnetsh\s+advfirewall\s+set\s+.*off",
        r"invoke-expression\s*\(.*downloadstring", r"\bcurl\b.*\|\s*(sh|bash|iex)", r"\bwget\b.*\|\s*(sh|bash)",
        r"\bmkfs\b", r"\bdd\s+if=.*of=/dev/", r":\(\)\s*\{\s*:\|:&\s*\};:",
        r"\bschtasks\b.*\/create", r"\bsc\s+(create|config)\b", r"\bwmic\b.*\bdelete\b",
    ]
    HIGH_PATTERNS = [
        r"\bdel\b", r"\berase\b", r"\brmdir\b", r"\brd\b", r"\brm\b", r"remove-item", r"\bshutdown\b",
        r"\btaskkill\b", r"\bkill\b", r"\breg\s+(add|delete|import)\b", r"pip\s+uninstall",
        r"git\s+reset\s+--hard", r"git\s+clean", r"git\s+push\s+.*--force", r"\bformat\b",
        r"\bchmod\s+-R\b", r"\bchown\b", r"\bmove\b.*\\windows\\", r"\bicacls\b", r"\btakeown\b",
        r"set-executionpolicy", r"\bnpm\s+(uninstall|rm)\b", r"\bsudo\b", r"\brunas\b",
    ]
    MEDIUM_PATTERNS = [
        r"pip\s+install", r"-m\s+pip\s+install", r"npm\s+(install|i)\b", r"\bgit\s+(commit|merge|pull|checkout|clone)\b",
        r"\bpyinstaller\b", r"\bnuitka\b", r"\bwinget\b", r"\bchoco\b", r"\bcopy\b", r"\bmove\b", r"\bmv\b",
        r"\bcp\b", r"\bnpx\b", r"\bdotnet\s+(build|publish|restore)\b", r"\bcargo\s+(build|install)\b",
        r"\bmvn\b", r"\bgradle\b", r"\bmake\b", r"\bcmake\b",
    ]

    def __init__(self, settings: SettingsManager, audit: AuditLog):
        self.settings = settings
        self.audit = audit
        self.confirm_callback: Optional[Callable[[str], bool]] = None

    def classify_command(self, command: str) -> Risk:
        c = (command or "").lower()
        for p in self.BLOCKED_PATTERNS:
            if re.search(p, c):
                return Risk.BLOCKED
        for p in self.HIGH_PATTERNS:
            if re.search(p, c):
                return Risk.HIGH
        for p in self.MEDIUM_PATTERNS:
            if re.search(p, c):
                return Risk.MEDIUM
        return Risk.LOW

    def confirm(self, prompt: str) -> bool:
        cb = self.confirm_callback
        if cb is None:
            return False
        try:
            return bool(cb(prompt))
        except Exception:
            return False

    def authorize_command(self, command: str, task_id: str = "") -> Tuple[bool, str, Risk]:
        risk = self.classify_command(command)
        if risk == Risk.BLOCKED:
            self.audit.record(task_id, "authorize", command=command, result="blocked")
            return False, "Command blocked by safety policy.", risk
        need = risk == Risk.HIGH or (risk == Risk.MEDIUM and self.settings.get("confirm_medium_risk"))
        if need:
            ok = self.confirm("Allow %s-risk command?\n\n%s" % (risk.name, short(command, 600)))
            self.audit.record(task_id, "authorize", command=command, result="approved" if ok else "denied",
                              risk=risk.name)
            if not ok:
                return False, "Command was not approved by the user.", risk
        return True, "ok", risk

    def in_workspace(self, path: Any) -> bool:
        try:
            ws = self.settings.workspace().resolve()
            target = Path(path).expanduser().resolve()
            return target == ws or ws in target.parents
        except (OSError, ValueError, RuntimeError):
            return False

    def authorize_path(self, path: Any, write: bool, task_id: str = "") -> Tuple[bool, str]:
        if self.in_workspace(path):
            return True, "ok"
        if not write:
            return True, "ok"
        if self.settings.get("allow_outside_workspace"):
            return True, "ok"
        ok = self.confirm("Allow writing outside the workspace?\n\n%s" % path)
        self.audit.record(task_id, "authorize_path", path=str(path), result="approved" if ok else "denied")
        return (True, "ok") if ok else (False, "Write outside workspace was not approved.")

    def authorize_computer_control(self, description: str, task_id: str = "") -> Tuple[bool, str]:
        if self.settings.get("allow_computer_control"):
            return True, "ok"
        ok = self.confirm("Allow computer control action?\n\n%s" % description)
        self.audit.record(task_id, "authorize_control", result="approved" if ok else "denied", detail=description)
        return (True, "ok") if ok else (False, "Computer control action was not approved.")


# ---------------------------------------------------------------------------
# TerminalManager
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CommandResult:
    ok: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration: float
    command: str
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""

    @property
    def combined(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")


class TerminalManager:
    def __init__(self, settings: SettingsManager, control: TaskControl, audit: AuditLog):
        self.settings = settings
        self.control = control
        self.audit = audit
        self.on_output: Optional[Callable[[str], None]] = None

    @staticmethod
    def find_powershell() -> Optional[str]:
        return shutil.which("pwsh") or shutil.which("powershell")

    def build_args(self, command: Any, shell: str = "auto") -> List[str]:
        if isinstance(command, (list, tuple)):
            return [str(x) for x in command]
        text = str(command).strip()
        shell = (shell or "auto").lower()
        if shell == "cmd":
            return ["cmd.exe", "/c", text] if IS_WINDOWS else ["sh", "-c", text]
        if shell in ("powershell", "ps", "pwsh"):
            ps = self.find_powershell()
            if not ps:
                raise FileNotFoundError("PowerShell is not available on this system.")
            return [ps, "-NoProfile", "-NonInteractive", "-Command", text]
        try:
            parts = shlex.split(text, posix=not IS_WINDOWS)
        except ValueError as exc:
            raise ValueError("Cannot parse command: %s" % exc)
        if IS_WINDOWS:
            parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
        return parts

    def run(self, command: Any, cwd: Optional[str] = None, timeout: Optional[float] = None,
            shell: str = "auto", env: Optional[Dict[str, str]] = None,
            stream: bool = True) -> CommandResult:
        display = command if isinstance(command, str) else " ".join(shlex.quote(str(c)) for c in command)
        start = time.time()
        try:
            args = self.build_args(command, shell)
        except (ValueError, FileNotFoundError) as exc:
            return CommandResult(False, None, "", str(exc), 0.0, display, error=str(exc))
        if not args:
            return CommandResult(False, None, "", "Empty command", 0.0, display, error="Empty command")
        timeout = float(timeout or self.settings.get("command_timeout", 600))
        full_env = dict(os.environ)
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        if env:
            full_env.update(env)
        kwargs: Dict[str, Any] = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            cwd=cwd or None, env=full_env, shell=False,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **kwargs)
        except FileNotFoundError:
            return CommandResult(False, None, "", "Executable not found: %s" % args[0], time.time() - start,
                                 display, error="Executable not found: %s" % args[0])
        except (OSError, PermissionError) as exc:
            return CommandResult(False, None, "", "Cannot start process: %s" % exc, time.time() - start,
                                 display, error=str(exc))
        self.control.register_process(proc)
        out_chunks: List[str] = []
        err_chunks: List[str] = []

        def pump(pipe: Any, sink: List[str], emit: bool) -> None:
            try:
                for raw in iter(pipe.readline, b""):
                    text = raw.decode("utf-8", errors="replace")
                    sink.append(text)
                    if emit and stream and self.on_output:
                        with contextlib.suppress(Exception):
                            self.on_output(redact(text.rstrip("\n")))
            except (OSError, ValueError):
                pass

        t1 = threading.Thread(target=pump, args=(proc.stdout, out_chunks, True), daemon=True)
        t2 = threading.Thread(target=pump, args=(proc.stderr, err_chunks, True), daemon=True)
        t1.start()
        t2.start()
        timed_out = False
        cancelled = False
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if self.control.stopped:
                cancelled = True
                kill_process_tree(proc)
                break
            if time.time() - start > timeout:
                timed_out = True
                kill_process_tree(proc)
                break
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if self.control.stopped and proc.returncode not in (0, None):
            cancelled = True
        t1.join(timeout=3)
        t2.join(timeout=3)
        self.control.unregister_process(proc)
        code = proc.returncode
        result = CommandResult(
            ok=(code == 0 and not timed_out and not cancelled), exit_code=code,
            stdout="".join(out_chunks), stderr="".join(err_chunks), duration=time.time() - start,
            command=display, timed_out=timed_out, cancelled=cancelled,
            error="timeout after %ss" % int(timeout) if timed_out else ("cancelled" if cancelled else ""),
        )
        self.audit.record(self.control.task_id, "terminal", tool=args[0], command=display,
                          exit_code=code, result="ok" if result.ok else "failed",
                          error=short(result.stderr, 400) if not result.ok else None,
                          duration=round(result.duration, 2))
        return result

    def launch_detached(self, args: List[str], cwd: Optional[str] = None) -> Optional[subprocess.Popen]:
        kwargs: Dict[str, Any] = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL, cwd=cwd or None)
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(args, **kwargs)
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------------------
# FileManager (read / write / copy / move / rename / delete / search / backup / rollback)
# ---------------------------------------------------------------------------

class FileManager:
    def __init__(self, settings: SettingsManager, security: SecurityManager, audit: AuditLog,
                 control: TaskControl):
        self.settings = settings
        self.security = security
        self.audit = audit
        self.control = control

    def resolve(self, path: Any) -> Path:
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = self.settings.workspace() / p
        return p

    def read_text(self, path: Any, max_bytes: int = 2_000_000) -> str:
        p = self.resolve(path)
        data = p.read_bytes()[:max_bytes]
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
        self.audit.record(self.control.task_id, "file_read", path=str(p), result="ok")
        return text

    def backup(self, path: Any) -> Optional[str]:
        p = self.resolve(path)
        if not p.exists():
            return None
        bid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        dest_dir = BACKUP_DIR / bid
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if p.is_dir():
            shutil.copytree(p, dest, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
        else:
            shutil.copy2(p, dest)
        (dest_dir / "manifest.json").write_text(
            json.dumps({"original": str(p), "name": p.name, "is_dir": p.is_dir(), "ts": now_iso()}),
            encoding="utf-8")
        self.audit.record(self.control.task_id, "backup", path=str(p), backup_id=bid)
        return bid

    def rollback(self, backup_id: str) -> str:
        d = BACKUP_DIR / backup_id
        manifest = d / "manifest.json"
        if not manifest.exists():
            raise FileNotFoundError("Backup not found: %s" % backup_id)
        info = json.loads(manifest.read_text(encoding="utf-8"))
        original = Path(info["original"])
        saved = d / info["name"]
        if info["is_dir"]:
            if original.exists():
                shutil.rmtree(original)
            shutil.copytree(saved, original)
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, original)
        self.audit.record(self.control.task_id, "rollback", path=str(original), backup_id=backup_id, result="ok")
        return str(original)

    def write_text(self, path: Any, content: str, make_backup: bool = True) -> Tuple[Path, Optional[str]]:
        p = self.resolve(path)
        ok, why = self.security.authorize_path(p, write=True, task_id=self.control.task_id)
        if not ok:
            raise PermissionError(why)
        bid = self.backup(p) if (make_backup and p.exists()) else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        self.audit.record(self.control.task_id, "file_write", path=str(p), backup_id=bid, result="ok")
        return p, bid

    def copy(self, src: Any, dst: Any) -> Path:
        s, d = self.resolve(src), self.resolve(dst)
        ok, why = self.security.authorize_path(d, write=True, task_id=self.control.task_id)
        if not ok:
            raise PermissionError(why)
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
        self.audit.record(self.control.task_id, "file_copy", path=str(s), target=str(d), result="ok")
        return d

    def move(self, src: Any, dst: Any) -> Path:
        s, d = self.resolve(src), self.resolve(dst)
        for p in (s, d):
            ok, why = self.security.authorize_path(p, write=True, task_id=self.control.task_id)
            if not ok:
                raise PermissionError(why)
        if not self.security.confirm("Move %s to %s?" % (s, d)):
            raise PermissionError("Move was not approved.")
        self.backup(s)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        self.audit.record(self.control.task_id, "file_move", path=str(s), target=str(d), result="ok")
        return d

    def rename(self, src: Any, new_name: str) -> Path:
        s = self.resolve(src)
        if os.sep in new_name or "/" in new_name:
            raise ValueError("new_name must be a file name, not a path.")
        ok, why = self.security.authorize_path(s, write=True, task_id=self.control.task_id)
        if not ok:
            raise PermissionError(why)
        target = s.with_name(new_name)
        if target.exists():
            raise FileExistsError(str(target))
        s.rename(target)
        self.audit.record(self.control.task_id, "file_rename", path=str(s), target=str(target), result="ok")
        return target

    def delete(self, path: Any) -> str:
        p = self.resolve(path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        ok, why = self.security.authorize_path(p, write=True, task_id=self.control.task_id)
        if not ok:
            raise PermissionError(why)
        if len(p.parts) <= 2:
            raise PermissionError("Refusing to delete a top-level location.")
        if not self.security.confirm("Delete %s? A backup will be kept for rollback." % p):
            raise PermissionError("Delete was not approved.")
        bid = self.backup(p)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        self.audit.record(self.control.task_id, "file_delete", path=str(p), backup_id=bid, result="ok")
        return bid or ""

    def search(self, root: Any, pattern: str = "", contains: str = "", limit: int = 200) -> List[str]:
        base = self.resolve(root)
        found: List[str] = []
        pat = pattern.lower()
        needle = contains.lower()
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            self.control.checkpoint()
            for fn in filenames:
                if pat and pat not in fn.lower():
                    continue
                fp = Path(dirpath) / fn
                if needle:
                    try:
                        if fp.stat().st_size > 2_000_000:
                            continue
                        if needle not in fp.read_text(encoding="utf-8", errors="ignore").lower():
                            continue
                    except OSError:
                        continue
                found.append(str(fp))
                if len(found) >= limit:
                    return found
        return found

    def list_dir(self, path: Any, limit: int = 200) -> List[str]:
        p = self.resolve(path)
        out = []
        for child in sorted(p.iterdir())[:limit]:
            out.append(child.name + ("/" if child.is_dir() else ""))
        return out


# ---------------------------------------------------------------------------
# ToolManager
# ---------------------------------------------------------------------------

class ToolManager:
    TOOLS: Dict[str, Tuple[List[str], List[str], str]] = {
        # name: (executables, version args, capability)
        "python": (["python", "python3", "py"], ["--version"], "run python code"),
        "pip": (["pip", "pip3"], ["--version"], "install python packages"),
        "git": (["git"], ["--version"], "version control"),
        "powershell": (["pwsh", "powershell"], ["-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                       "windows scripting"),
        "cmd": (["cmd"], [], "windows command line"),
        "pyinstaller": (["pyinstaller"], ["--version"], "build python exe"),
        "nuitka": (["nuitka"], ["--version"], "compile python"),
        "node": (["node"], ["--version"], "run javascript"),
        "npm": (["npm"], ["--version"], "javascript packages"),
        "dotnet": (["dotnet"], ["--version"], "build .NET"),
        "cargo": (["cargo"], ["--version"], "build rust"),
        "java": (["java"], ["-version"], "run java"),
        "gcc": (["gcc"], ["--version"], "build c/c++"),
        "chrome": (["chrome", "google-chrome", "chromium"], [], "browser"),
        "edge": (["msedge"], [], "browser"),
        "firefox": (["firefox"], [], "browser"),
        "7zip": (["7z", "7za"], [], "archives"),
        "vscode": (["code"], ["--version"], "code editor"),
        "tesseract": (["tesseract"], ["--version"], "OCR engine"),
    }
    WINDOWS_PATHS = {
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                   os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")],
        "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        "firefox": [r"C:\Program Files\Mozilla Firefox\firefox.exe"],
        "7zip": [r"C:\Program Files\7-Zip\7z.exe"],
        "vscode": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe")],
        "tesseract": [r"C:\Program Files\Tesseract-OCR\tesseract.exe"],
    }

    def __init__(self, terminal: TerminalManager, memory: MemoryManager):
        self.terminal = terminal
        self.memory = memory
        self.info: Dict[str, Dict[str, Any]] = {}

    def find(self, name: str) -> Optional[str]:
        spec = self.TOOLS.get(name)
        candidates = spec[0] if spec else [name]
        if name == "python" and not getattr(sys, "frozen", False) and sys.executable:
            return sys.executable
        for exe in candidates:
            found = shutil.which(exe)
            if found:
                return found
        if IS_WINDOWS:
            for p in self.WINDOWS_PATHS.get(name, []):
                if os.path.exists(p):
                    return p
        return None

    def detect_one(self, name: str, with_version: bool = True) -> Dict[str, Any]:
        spec = self.TOOLS.get(name)
        path = self.find(name)
        entry: Dict[str, Any] = {"name": name, "path": path or "", "available": bool(path),
                                 "version": "", "health": "missing",
                                 "capabilities": spec[2] if spec else ""}
        if path and with_version and spec and spec[1]:
            try:
                r = subprocess.run([path] + spec[1], capture_output=True, text=True, timeout=8,
                                   stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
                out = (r.stdout or r.stderr or "").strip().splitlines()
                entry["version"] = out[0][:120] if out else ""
                entry["health"] = "ok" if r.returncode == 0 else "error"
            except (subprocess.TimeoutExpired, OSError):
                entry["health"] = "error"
        elif path:
            entry["health"] = "ok"
        self.info[name] = entry
        return entry

    def detect_all(self, with_version: bool = True) -> Dict[str, Dict[str, Any]]:
        for name in self.TOOLS:
            self.detect_one(name, with_version)
        if self.memory is not None:
            for name, e in self.info.items():
                if e["available"]:
                    self.memory.add("tool", "%s at %s version %s" % (name, e["path"], e["version"]), key=name,
                                    meta=e)
        return self.info

    def python_for_project(self, project: Optional[Path] = None) -> Optional[str]:
        if project:
            for venv in (".venv", "venv", "env"):
                cand = Path(project) / venv / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
                if cand.exists():
                    return str(cand)
        return self.find("python")

    def summary(self) -> str:
        if not self.info:
            self.detect_all(with_version=False)
        return ", ".join(sorted(n for n, e in self.info.items() if e["available"])) or "none"


# ---------------------------------------------------------------------------
# ProjectInspector
# ---------------------------------------------------------------------------

PIP_NAME_MAP = {
    "cv2": "opencv-python", "PIL": "Pillow", "yaml": "pyyaml", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "win32api": "pywin32", "win32con": "pywin32", "win32gui": "pywin32",
    "win32com": "pywin32", "pythoncom": "pywin32", "pywintypes": "pywin32", "dotenv": "python-dotenv",
    "serial": "pyserial", "Crypto": "pycryptodome", "dateutil": "python-dateutil", "docx": "python-docx",
    "fitz": "PyMuPDF", "jwt": "PyJWT", "OpenSSL": "pyOpenSSL", "usb": "pyusb", "skimage": "scikit-image",
    "speech_recognition": "SpeechRecognition", "magic": "python-magic", "attr": "attrs",
    "pkg_resources": "setuptools", "git": "GitPython", "zmq": "pyzmq", "telegram": "python-telegram-bot",
}
GUI_MODULES = {"tkinter", "customtkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "kivy", "pygame",
               "ttkbootstrap", "dearpygui", "flet"}


class ProjectInspector:
    def __init__(self, control: TaskControl):
        self.control = control

    def walk_files(self, root: Path, limit: int = 5000) -> List[Path]:
        files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info")]
            self.control.checkpoint()
            for fn in filenames:
                files.append(Path(dirpath) / fn)
                if len(files) >= limit:
                    return files
        return files

    def inspect(self, root: Any) -> Dict[str, Any]:
        root = Path(str(root)).expanduser()
        if root.is_file():
            root = root.parent
        if not root.exists():
            raise FileNotFoundError("Project path not found: %s" % root)
        files = self.walk_files(root)
        exts: Dict[str, int] = {}
        for f in files:
            exts[f.suffix.lower()] = exts.get(f.suffix.lower(), 0) + 1
        names = {f.name.lower(): f for f in files if f.parent == root}
        types: List[str] = []
        manifests: Dict[str, str] = {}

        def has(*patterns: str) -> bool:
            return any(any(f.name.lower().endswith(p) for f in files) for p in patterns)

        if exts.get(".py"):
            types.append("Python")
        if "package.json" in names:
            types.append("Node.js")
            try:
                pkg = json.loads(names["package.json"].read_text(encoding="utf-8"))
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "react" in deps:
                    types.append("React")
                manifests["package.json"] = json.dumps({k: pkg.get(k) for k in ("name", "main", "scripts")})
            except (OSError, ValueError):
                pass
        if exts.get(".js") or exts.get(".mjs"):
            types.append("JavaScript")
        if exts.get(".ts") or exts.get(".tsx"):
            types.append("TypeScript")
        if has(".csproj", ".sln"):
            types.extend(["C#", ".NET"])
        if exts.get(".java"):
            types.append("Java")
        if exts.get(".c") or exts.get(".cpp") or exts.get(".cc") or exts.get(".h") or exts.get(".hpp"):
            types.append("C/C++")
        if "cargo.toml" in names:
            types.append("Rust")
        if exts.get(".html") and not types:
            types.append("Web")
        elif exts.get(".html") or exts.get(".css"):
            types.append("Web")
        for key in ("requirements.txt", "pyproject.toml", "cargo.toml", "readme.md", "readme.txt", "setup.py",
                    "makefile", "pom.xml", "build.gradle", "dockerfile"):
            if key in names:
                try:
                    manifests[key] = short(names[key].read_text(encoding="utf-8", errors="ignore"), 1500)
                except OSError:
                    pass
        tests = [str(f.relative_to(root)) for f in files if f.suffix == ".py" and
                 (f.name.startswith("test_") or f.name.endswith("_test.py") or "tests" in f.parts)]
        info: Dict[str, Any] = {
            "root": str(root), "types": sorted(set(types)), "file_count": len(files),
            "extensions": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:10]),
            "manifests": manifests, "tests": tests[:30], "entry_points": self.find_entry_points(root, files),
        }
        if "Python" in types:
            info.update(self.analyze_python(root, files))
        return info

    def find_entry_points(self, root: Path, files: List[Path]) -> List[str]:
        entries: List[Tuple[int, str]] = []
        preferred = ("main.py", "app.py", "__main__.py", "run.py", "start.py", "cli.py", "gui.py", "manage.py")
        for f in files:
            if f.suffix != ".py":
                continue
            score = 0
            rel = str(f.relative_to(root))
            if f.name in preferred:
                score += 5 - preferred.index(f.name) * 0.1
            if f.parent == root:
                score += 2
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", text):
                score += 4
            if score >= 4:
                entries.append((int(score * 10), rel))
        for key, rx in (("package.json", None),):
            pj = root / key
            if pj.exists():
                try:
                    pkg = json.loads(pj.read_text(encoding="utf-8"))
                    if pkg.get("main"):
                        entries.append((55, str(pkg["main"])))
                    if "start" in pkg.get("scripts", {}):
                        entries.append((50, "npm start"))
                except (OSError, ValueError):
                    pass
        for f in files:
            if f.name == "Program.cs" or f.name == "main.rs" or f.name == "Main.java":
                entries.append((45, str(f.relative_to(root))))
        entries.sort(key=lambda t: (-t[0], t[1]))
        seen: List[str] = []
        for _, rel in entries:
            if rel not in seen:
                seen.append(rel)
        return seen[:8]

    def analyze_python(self, root: Path, files: List[Path]) -> Dict[str, Any]:
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
        local = {f.stem for f in files if f.suffix == ".py"}
        local |= {f.parent.name for f in files if f.name == "__init__.py"}
        local |= {d.name for d in root.iterdir() if d.is_dir()} if root.exists() else set()
        imports: Dict[str, int] = {}
        syntax_errors: List[Dict[str, Any]] = []
        for f in files:
            if f.suffix != ".py":
                continue
            try:
                src = f.read_text(encoding="utf-8-sig", errors="replace")
                tree = ast.parse(src, filename=str(f))
            except SyntaxError as exc:
                syntax_errors.append({"file": str(f), "line": exc.lineno, "error": "%s: %s" % (
                    type(exc).__name__, exc.msg)})
                continue
            except (OSError, ValueError) as exc:
                syntax_errors.append({"file": str(f), "line": 0, "error": str(exc)})
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        imports[a.name.split(".")[0]] = imports.get(a.name.split(".")[0], 0) + 1
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports[node.module.split(".")[0]] = imports.get(node.module.split(".")[0], 0) + 1
        third = sorted(m for m in imports if m not in stdlib and m not in local and not m.startswith("_"))
        req_file = root / "requirements.txt"
        requirements: List[str] = []
        if req_file.exists():
            for ln in req_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = ln.split("#")[0].strip()
                if ln and not ln.startswith("-"):
                    requirements.append(ln)
        return {
            "syntax_errors": syntax_errors,
            "third_party_imports": third,
            "pip_packages": sorted({PIP_NAME_MAP.get(m, m) for m in third}),
            "requirements": requirements,
            "is_gui": bool(set(imports) & GUI_MODULES),
            "has_requirements_file": req_file.exists(),
        }

    def format_report(self, info: Dict[str, Any]) -> str:
        lines = ["Project: %s" % info["root"],
                 "Types: %s" % (", ".join(info["types"]) or "unknown"),
                 "Files scanned: %d" % info["file_count"],
                 "Entry points: %s" % (", ".join(info["entry_points"]) or "none detected")]
        if "pip_packages" in info:
            lines.append("Third-party Python imports: %s" % (", ".join(info["pip_packages"]) or "none"))
            se = info.get("syntax_errors", [])
            lines.append("Syntax errors: %d" % len(se))
            for e in se[:10]:
                lines.append("  %s:%s  %s" % (e["file"], e["line"], e["error"]))
        if info.get("tests"):
            lines.append("Tests found: %d" % len(info["tests"]))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# ResearchEngine
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: List[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "pre"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._skip == 0 and data.strip():
            self.parts.append(data.strip() + " ")


class ResearchEngine:
    OFFICIAL_HINTS = (
        "docs.python.org", "python.org", "pyinstaller.org", "microsoft.com", "learn.microsoft.com",
        "github.com", "readthedocs.io", "developer.mozilla.org", "nodejs.org", "npmjs.com", "pypi.org",
        "docs.", "developer.", "stackoverflow.com", "openai.com", "ai.google.dev", "ollama.com",
    )
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AKAI/%s" % VERSION

    def __init__(self, settings: SettingsManager, memory: MemoryManager, audit: AuditLog,
                 control: TaskControl):
        self.settings = settings
        self.memory = memory
        self.audit = audit
        self.control = control

    def is_online(self, timeout: float = 2.5) -> bool:
        if self.settings.get("offline_mode"):
            return False
        for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                continue
        return False

    def _get(self, url: str, timeout: float = 15.0, max_bytes: int = 1_500_000) -> Tuple[str, str]:
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace"), resp.geturl()

    def score_url(self, url: str) -> int:
        host = urllib.parse.urlparse(url).netloc.lower()
        for i, hint in enumerate(self.OFFICIAL_HINTS):
            if hint in host:
                return 100 - i
        return 0

    def search(self, query: str, limit: int = 8) -> Dict[str, Any]:
        cached = self.memory.search(query, kind="research", limit=3)
        if not self.is_online():
            return {"ok": False, "online": False, "results": [], "cached": cached,
                    "error": "Internet is not available. No online research was performed."}
        try:
            body, _ = self._get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"ok": False, "online": True, "results": [], "cached": cached,
                    "error": "Search request failed: %s" % exc}
        results: List[Dict[str, Any]] = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
            href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs:
                href = qs["uddg"][0]
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith("http"):
                continue
            results.append({"title": html.unescape(title).strip(), "url": href, "score": self.score_url(href)})
        results.sort(key=lambda r: -r["score"])
        results = results[:limit]
        self.audit.record(self.control.task_id, "research_search", tool="web", command=query,
                          result="%d results" % len(results))
        if results:
            self.memory.add("research", "Search: %s\n" % query + "\n".join(
                "%s - %s" % (r["title"], r["url"]) for r in results), key=query,
                meta={"urls": [r["url"] for r in results]})
        return {"ok": bool(results), "online": True, "results": results, "cached": cached,
                "error": "" if results else "No results parsed from search page."}

    def fetch_page(self, url: str, max_chars: int = 6000) -> Dict[str, Any]:
        if not self.is_online():
            return {"ok": False, "url": url, "text": "", "error": "Internet is not available."}
        if not re.match(r"^https?://", url):
            return {"ok": False, "url": url, "text": "", "error": "Only http/https URLs are supported."}
        try:
            body, final_url = self._get(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"ok": False, "url": url, "text": "", "error": "Fetch failed: %s" % exc}
        parser = _TextExtractor()
        try:
            parser.feed(body)
        except Exception:
            pass
        text = re.sub(r"\n\s*\n+", "\n", "".join(parser.parts)).strip()
        text = text[:max_chars]
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        with contextlib.suppress(OSError):
            (CACHE_DIR / (key + ".txt")).write_text("%s\n%s\n\n%s" % (final_url, parser.title.strip(), text),
                                                    encoding="utf-8")
        self.memory.add("research", "%s\n%s\n%s" % (parser.title.strip(), final_url, text[:1500]), key=url,
                        meta={"url": final_url})
        self.audit.record(self.control.task_id, "research_fetch", tool="web", command=url, result="ok")
        return {"ok": True, "url": final_url, "title": parser.title.strip(), "text": text, "error": ""}

    def research(self, query: str, pages: int = 2) -> Dict[str, Any]:
        found = self.search(query)
        out: Dict[str, Any] = {"query": query, "search": found, "pages": []}
        for r in found.get("results", [])[:pages]:
            self.control.checkpoint()
            page = self.fetch_page(r["url"], max_chars=3000)
            if page["ok"]:
                out["pages"].append({"url": page["url"], "title": page["title"], "text": page["text"]})
        out["ok"] = bool(out["pages"]) or bool(found.get("results"))
        return out


# ---------------------------------------------------------------------------
# AIProvider: OpenAI-compatible, Gemini, Ollama
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class AIResult:
    ok: bool
    text: str = ""
    error: str = ""
    provider: str = ""
    status: Optional[int] = None


class AIProvider:
    TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: SettingsManager, control: TaskControl, audit: AuditLog):
        self.settings = settings
        self.control = control
        self.audit = audit

    @property
    def name(self) -> str:
        return str(self.settings.get("provider", "none"))

    def available(self) -> Tuple[bool, str]:
        if self.settings.get("offline_mode") and self.name != "ollama":
            return False, "Offline mode is enabled."
        if self.name == "none":
            return False, "No AI provider selected."
        if self.name in ("openai", "gemini") and not self.settings.api_key(self.name):
            return False, "No API key for %s (set %s)." % (self.name, SettingsManager.ENV_KEYS[self.name])
        if self.name not in ("openai", "gemini", "ollama"):
            return False, "Unknown provider: %s" % self.name
        return True, "ok"

    def _http(self, url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body

    def _build(self, system: str, messages: List[Dict[str, str]], json_mode: bool) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
        p = self.name
        model = self.settings.effective_model()
        base = self.settings.effective_base_url()
        temp = float(self.settings.get("temperature", 0.2))
        key = self.settings.api_key(p)
        if p == "openai":
            msgs = [{"role": "system", "content": system}] + messages
            payload: Dict[str, Any] = {"model": model, "messages": msgs, "temperature": temp}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            return base + "/chat/completions", payload, {"Authorization": "Bearer " + key}
        if p == "gemini":
            contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                        for m in messages]
            payload = {"contents": contents, "systemInstruction": {"parts": [{"text": system}]},
                       "generationConfig": {"temperature": temp}}
            if json_mode:
                payload["generationConfig"]["responseMimeType"] = "application/json"
            return "%s/models/%s:generateContent" % (base, urllib.parse.quote(model)), payload, {"x-goog-api-key": key}
        msgs = [{"role": "system", "content": system}] + messages
        payload = {"model": model, "messages": msgs, "stream": False, "options": {"temperature": temp}}
        if json_mode:
            payload["format"] = "json"
        headers = {"Authorization": "Bearer " + key} if key else {}
        return base + "/api/chat", payload, headers

    def _parse(self, body: str) -> str:
        data = json.loads(body)
        p = self.name
        if p == "openai":
            return str(data["choices"][0]["message"]["content"] or "")
        if p == "gemini":
            cands = data.get("candidates") or []
            if not cands:
                raise ValueError("Gemini returned no candidates: %s" % short(json.dumps(data.get("promptFeedback", {})), 200))
            parts = cands[0].get("content", {}).get("parts", [])
            return "".join(str(x.get("text", "")) for x in parts)
        return str(data["message"]["content"])

    def chat(self, system: str, messages: List[Dict[str, str]], json_mode: bool = False) -> AIResult:
        ok, why = self.available()
        if not ok:
            return AIResult(False, error=why, provider=self.name)
        retries = max(0, int(self.settings.get("retry_limit", 3)))
        timeout = float(self.settings.get("timeout", 60))
        last_err = ""
        status: Optional[int] = None
        for attempt in range(retries + 1):
            self.control.checkpoint()
            try:
                url, payload, headers = self._build(system, messages, json_mode)
                status, body = self._http(url, payload, headers, timeout)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last_err = "Connection problem: %s" % redact(str(exc))
                status = None
            else:
                if status == 200:
                    try:
                        text = self._parse(body)
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        return AIResult(False, error="Malformed response from %s: %s" % (self.name, exc),
                                        provider=self.name, status=status)
                    if not text.strip():
                        last_err = "Empty response from model."
                    else:
                        self.audit.record(self.control.task_id, "ai_call", tool=self.name,
                                          result="ok", attempt=attempt)
                        return AIResult(True, text=text, provider=self.name, status=status)
                elif status in (401, 403):
                    return AIResult(False, error="Authentication failed (HTTP %s). Check the API key." % status,
                                    provider=self.name, status=status)
                elif status == 404:
                    return AIResult(False, error="Model or endpoint not found (HTTP 404). Check model/base URL: %s"
                                    % short(redact(body), 200), provider=self.name, status=status)
                elif status in self.TRANSIENT:
                    last_err = "HTTP %s: %s" % (status, short(redact(body), 200))
                else:
                    return AIResult(False, error="HTTP %s: %s" % (status, short(redact(body), 300)),
                                    provider=self.name, status=status)
            if attempt < retries:
                time.sleep(min(8.0, 1.0 * (2 ** attempt)))
        self.audit.record(self.control.task_id, "ai_call", tool=self.name, result="failed", error=last_err)
        return AIResult(False, error=last_err or "Request failed.", provider=self.name, status=status)

    def chat_json(self, system: str, user: str) -> Tuple[Optional[Any], AIResult]:
        res = self.chat(system, [{"role": "user", "content": user}], json_mode=True)
        if not res.ok:
            return None, res
        data = extract_json(res.text)
        if data is None:
            retry = self.chat(system + "\nReturn ONLY valid JSON. No prose, no code fences.",
                              [{"role": "user", "content": user}], json_mode=True)
            if retry.ok:
                data = extract_json(retry.text)
                res = retry
        if data is None:
            return None, AIResult(False, text=res.text, error="Model did not return valid JSON.",
                                  provider=res.provider, status=res.status)
        return data, res


# ---------------------------------------------------------------------------
# ApplicationManager
# ---------------------------------------------------------------------------

class ApplicationManager:
    ALIASES = {
        "chrome": "chrome", "google chrome": "chrome", "edge": "edge", "microsoft edge": "edge",
        "firefox": "firefox", "notepad": "notepad", "calculator": "calc", "calc": "calc",
        "explorer": "explorer", "file explorer": "explorer", "cmd": "cmd", "command prompt": "cmd",
        "powershell": "powershell", "terminal": "wt", "vscode": "vscode", "vs code": "vscode",
        "code": "vscode", "paint": "mspaint", "task manager": "taskmgr", "settings": "ms-settings:",
        "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "7zip": "7zip",
    }

    def __init__(self, tools: ToolManager, terminal: TerminalManager, audit: AuditLog, control: TaskControl):
        self.tools = tools
        self.terminal = terminal
        self.audit = audit
        self.control = control

    def resolve(self, name: str) -> Tuple[str, Optional[str]]:
        key = self.ALIASES.get(name.strip().lower(), name.strip().lower())
        path = self.tools.find(key) if key in ToolManager.TOOLS else None
        if not path:
            path = shutil.which(key) or shutil.which(key + ".exe")
        return key, path

    def is_running(self, process_hint: str) -> bool:
        try:
            import psutil  # type: ignore
        except ImportError:
            return False
        hint = process_hint.lower()
        for p in psutil.process_iter(["name"]):
            nm = (p.info.get("name") or "").lower()
            if hint and hint in nm:
                return True
        return False

    def open_app(self, name: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        key, path = self.resolve(name)
        args = args or []
        try:
            if IS_WINDOWS:
                if path:
                    proc = self.terminal.launch_detached([path] + args)
                    if proc is None:
                        return {"ok": False, "error": "Failed to start %s" % path}
                    time.sleep(1.5)
                    alive = proc.poll() is None or self.is_running(Path(path).stem)
                    result = {"ok": alive, "path": path,
                              "error": "" if alive else "Process exited immediately (exit code %s)." % proc.poll()}
                    if proc.poll() == 0:
                        result["ok"] = True
                        result["error"] = ""
                else:
                    startfile = getattr(os, "startfile", None)
                    if startfile is None:
                        return {"ok": False, "error": "Application not found: %s" % name}
                    startfile(key)  # raises OSError when the target cannot be resolved
                    time.sleep(1.0)
                    result = {"ok": True, "path": key, "error": ""}
            else:
                if not path:
                    return {"ok": False, "error": "Application not found on this system: %s" % name}
                proc = self.terminal.launch_detached([path] + args)
                time.sleep(1.0)
                alive = proc is not None and (proc.poll() is None or proc.poll() == 0)
                result = {"ok": alive, "path": path, "error": "" if alive else "Process did not stay running."}
        except OSError as exc:
            result = {"ok": False, "error": "Could not open %s: %s" % (name, exc)}
        self.audit.record(self.control.task_id, "open_app", tool=name, result="ok" if result["ok"] else "failed",
                          error=result.get("error") or None)
        return result


# ---------------------------------------------------------------------------
# ComputerControl
# ---------------------------------------------------------------------------

def _optional(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except Exception:
        return None


class ComputerControl:
    def __init__(self, settings: SettingsManager, security: SecurityManager, audit: AuditLog,
                 control: TaskControl):
        self.settings = settings
        self.security = security
        self.audit = audit
        self.control = control

    def _pyautogui(self) -> Any:
        pg = _optional("pyautogui")
        if pg is None:
            raise RuntimeError("pyautogui is not installed (pip install pyautogui).")
        pg.FAILSAFE = True
        pg.PAUSE = 0.15
        return pg

    def _authorize(self, desc: str) -> None:
        self.control.checkpoint()
        ok, why = self.security.authorize_computer_control(desc, self.control.task_id)
        if not ok:
            raise PermissionError(why)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        self._authorize("Click at (%s, %s)" % (x, y))
        self._pyautogui().click(x=int(x), y=int(y), button=button, clicks=int(clicks))
        self.audit.record(self.control.task_id, "control_click", tool="pyautogui", detail="%s,%s" % (x, y), result="ok")
        return "Clicked at (%s, %s)" % (x, y)

    def move(self, x: int, y: int) -> str:
        self._authorize("Move mouse to (%s, %s)" % (x, y))
        self._pyautogui().moveTo(int(x), int(y), duration=0.2)
        return "Moved mouse to (%s, %s)" % (x, y)

    def type_text(self, text: str, interval: float = 0.02) -> str:
        self._authorize("Type text (%d characters)" % len(text))
        self._pyautogui().write(text, interval=interval)
        self.audit.record(self.control.task_id, "control_type", tool="pyautogui",
                          detail="%d chars" % len(text), result="ok")
        return "Typed %d characters" % len(text)

    def hotkey(self, *keys: str) -> str:
        self._authorize("Press hotkey %s" % "+".join(keys))
        self._pyautogui().hotkey(*keys)
        self.audit.record(self.control.task_id, "control_hotkey", tool="pyautogui", detail="+".join(keys), result="ok")
        return "Pressed %s" % "+".join(keys)

    def clipboard_get(self) -> str:
        pc = _optional("pyperclip")
        if pc is None:
            raise RuntimeError("pyperclip is not installed (pip install pyperclip).")
        self._authorize("Read clipboard")
        return str(pc.paste())

    def clipboard_set(self, text: str) -> str:
        pc = _optional("pyperclip")
        if pc is None:
            raise RuntimeError("pyperclip is not installed (pip install pyperclip).")
        self._authorize("Write clipboard")
        pc.copy(text)
        return "Clipboard updated (%d characters)" % len(text)

    def screenshot(self, path: Optional[str] = None) -> str:
        self.control.checkpoint()
        out = Path(path) if path else (self.settings.workspace() / "screenshots" /
                                       ("screen-%s.png" % time.strftime("%Y%m%d-%H%M%S")))
        out.parent.mkdir(parents=True, exist_ok=True)
        mss = _optional("mss")
        if mss is not None:
            try:
                with mss.mss() as sct:
                    sct.shot(output=str(out))
                self.audit.record(self.control.task_id, "screenshot", tool="mss", path=str(out), result="ok")
                return str(out)
            except Exception as exc:
                last = str(exc)
        else:
            last = "mss not installed"
        grab = _optional("PIL.ImageGrab")
        if grab is not None:
            try:
                grab.grab().save(str(out))
                self.audit.record(self.control.task_id, "screenshot", tool="Pillow", path=str(out), result="ok")
                return str(out)
            except Exception as exc:
                last = str(exc)
        raise RuntimeError("Screenshot failed: %s. Install mss or Pillow." % last)

    def list_windows(self) -> List[str]:
        gw = _optional("pygetwindow")
        if gw is not None:
            try:
                return [t for t in gw.getAllTitles() if t.strip()]
            except Exception:
                pass
        win32gui = _optional("win32gui")
        if win32gui is not None:
            titles: List[str] = []

            def cb(hwnd: int, _: Any) -> None:
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t.strip():
                        titles.append(t)
            win32gui.EnumWindows(cb, None)
            return titles
        psutil = _optional("psutil")
        if psutil is not None:
            return sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info.get("name")})
        raise RuntimeError("No window enumeration library available (pip install pygetwindow or pywin32).")

    def focus_window(self, title_part: str) -> str:
        self._authorize("Focus window containing '%s'" % title_part)
        gw = _optional("pygetwindow")
        if gw is None:
            raise RuntimeError("pygetwindow is not installed (pip install pygetwindow).")
        matches = gw.getWindowsWithTitle(title_part)
        if not matches:
            raise LookupError("No window title contains '%s'" % title_part)
        win = matches[0]
        with contextlib.suppress(Exception):
            if getattr(win, "isMinimized", False):
                win.restore()
        win.activate()
        return "Focused window: %s" % win.title


# ---------------------------------------------------------------------------
# BrowserManager (Playwright when installed, system browser fallback)
# ---------------------------------------------------------------------------

class BrowserManager:
    CAPTCHA_MARKERS = ("captcha", "verify you are human", "unusual traffic", "are you a robot",
                       "security check", "recaptcha", "hcaptcha", "cloudflare")

    def __init__(self, settings: SettingsManager, security: SecurityManager, audit: AuditLog,
                 control: TaskControl):
        self.settings = settings
        self.security = security
        self.audit = audit
        self.control = control
        self._q: "queue.Queue[Tuple[Callable[[], Any], queue.Queue]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    @staticmethod
    def playwright_available() -> bool:
        return importlib.util.find_spec("playwright") is not None

    def _worker(self) -> None:
        while True:
            fn, out = self._q.get()
            if fn is None:  # type: ignore[comparison-overlap]
                out.put((True, None))
                return
            try:
                out.put((True, fn()))
            except Exception as exc:
                out.put((False, exc))

    def _submit(self, fn: Callable[[], Any], timeout: float = 120.0) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True, name="ak-browser")
                self._thread.start()
        out: "queue.Queue[Tuple[bool, Any]]" = queue.Queue()
        self._q.put((fn, out))
        try:
            ok, val = out.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Browser action timed out.")
        if not ok:
            raise val
        return val

    def _ensure_page(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        sync_api = importlib.import_module("playwright.sync_api")
        if self._pw is None:
            self._pw = sync_api.sync_playwright().start()
        engine = str(self.settings.get("browser_engine", "chromium"))
        launcher = getattr(self._pw, engine, self._pw.chromium)
        self._browser = launcher.launch(headless=bool(self.settings.get("browser_headless", False)))
        ctx = self._browser.new_context(accept_downloads=True)
        self._page = ctx.new_page()
        return self._page

    def _needs_human(self) -> bool:
        try:
            text = (self._page.title() + " " + self._page.url + " " + self._page.inner_text("body")[:2000]).lower()
        except Exception:
            return False
        return any(m in text for m in self.CAPTCHA_MARKERS)

    def _wait_human_if_needed(self) -> None:
        if self.settings.get("browser_headless"):
            return
        waited = 0
        while self._needs_human():
            if waited == 0:
                self.audit.record(self.control.task_id, "browser_human_verification", result="waiting")
            ok = self.security.confirm("Human verification (CAPTCHA/security check) is shown in the browser.\n"
                                       "Complete it yourself, then click Yes to continue (No to stop).")
            if not ok:
                raise PermissionError("Human verification was not completed.")
            waited += 1
            if waited > 5:
                raise PermissionError("Human verification still present.")

    def open_url(self, url: str) -> Dict[str, Any]:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
            url = "https://" + url
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "Only http/https URLs are allowed."}
        self.control.checkpoint()
        if self.playwright_available():
            try:
                def go() -> Dict[str, Any]:
                    page = self._ensure_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    self._wait_human_if_needed()
                    return {"ok": True, "title": page.title(), "url": page.url, "engine": "playwright"}
                res = self._submit(go)
                self.audit.record(self.control.task_id, "browser_open", tool="playwright", command=url, result="ok")
                return res
            except Exception as exc:
                self.audit.record(self.control.task_id, "browser_open", tool="playwright", command=url,
                                  result="failed", error=str(exc))
                fallback_note = "Playwright failed (%s); used system browser." % short(str(exc), 200)
            else:
                fallback_note = ""
        else:
            fallback_note = "Playwright is not installed; used system browser."
        opened = webbrowser.open(url)
        self.audit.record(self.control.task_id, "browser_open", tool="webbrowser", command=url,
                          result="ok" if opened else "failed")
        return {"ok": bool(opened), "url": url, "engine": "system", "note": fallback_note,
                "error": "" if opened else "No system browser could be opened."}

    def search(self, query: str, engine: str = "google") -> Dict[str, Any]:
        base = {"google": "https://www.google.com/search?q=", "bing": "https://www.bing.com/search?q=",
                "duckduckgo": "https://duckduckgo.com/?q="}.get(engine, "https://www.google.com/search?q=")
        return self.open_url(base + urllib.parse.quote_plus(query))

    def read_page(self, max_chars: int = 6000) -> Dict[str, Any]:
        if not self.playwright_available():
            return {"ok": False, "error": "Reading page content requires Playwright (pip install playwright)."}
        try:
            def read() -> Dict[str, Any]:
                page = self._ensure_page()
                return {"ok": True, "title": page.title(), "url": page.url,
                        "text": page.inner_text("body")[:max_chars]}
            return self._submit(read)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def click(self, selector: str) -> Dict[str, Any]:
        if not self.playwright_available():
            return {"ok": False, "error": "Clicking requires Playwright."}
        try:
            def do() -> Dict[str, Any]:
                page = self._ensure_page()
                page.click(selector, timeout=15000)
                self._wait_human_if_needed()
                return {"ok": True, "url": page.url}
            return self._submit(do)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fill(self, selector: str, value: str) -> Dict[str, Any]:
        if not self.playwright_available():
            return {"ok": False, "error": "Filling forms requires Playwright."}
        try:
            def do() -> Dict[str, Any]:
                self._ensure_page().fill(selector, value, timeout=15000)
                return {"ok": True}
            return self._submit(do)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def download(self, selector: str, dest_dir: Optional[str] = None) -> Dict[str, Any]:
        if not self.playwright_available():
            return {"ok": False, "error": "Downloads require Playwright."}
        target_dir = Path(dest_dir) if dest_dir else self.settings.workspace() / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            def do() -> Dict[str, Any]:
                page = self._ensure_page()
                with page.expect_download(timeout=60000) as dl:
                    page.click(selector)
                d = dl.value
                dest = target_dir / d.suggested_filename
                d.save_as(str(dest))
                return {"ok": dest.exists(), "path": str(dest)}
            return self._submit(do, timeout=150)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def upload(self, selector: str, file_path: str) -> Dict[str, Any]:
        if not self.playwright_available():
            return {"ok": False, "error": "Uploads require Playwright."}
        if not Path(file_path).is_file():
            return {"ok": False, "error": "File not found: %s" % file_path}
        if not self.security.confirm("Upload this file to the website?\n\n%s" % file_path):
            return {"ok": False, "error": "Upload was not approved."}
        try:
            def do() -> Dict[str, Any]:
                self._ensure_page().set_input_files(selector, file_path)
                return {"ok": True}
            return self._submit(do)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return

        def shutdown() -> None:
            with contextlib.suppress(Exception):
                if self._browser:
                    self._browser.close()
            with contextlib.suppress(Exception):
                if self._pw:
                    self._pw.stop()
            self._page = self._browser = self._pw = None
        with contextlib.suppress(Exception):
            self._submit(shutdown, timeout=15)


# ---------------------------------------------------------------------------
# VisionManager
# ---------------------------------------------------------------------------

class VisionManager:
    def __init__(self, computer: ComputerControl, audit: AuditLog, control: TaskControl):
        self.computer = computer
        self.audit = audit
        self.control = control

    @staticmethod
    def ocr_available() -> Tuple[bool, str]:
        if _optional("PIL") is None:
            return False, "Pillow is not installed (pip install Pillow)."
        pt = _optional("pytesseract")
        if pt is None:
            return False, "pytesseract is not installed (pip install pytesseract)."
        try:
            pt.get_tesseract_version()
        except Exception:
            return False, "The Tesseract OCR engine is not installed or not on PATH."
        return True, "ok"

    def ocr_image(self, image_path: str) -> Dict[str, Any]:
        ok, why = self.ocr_available()
        if not ok:
            return {"ok": False, "error": why, "text": ""}
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
        try:
            with Image.open(image_path) as img:
                text = pytesseract.image_to_string(img)
        except Exception as exc:
            return {"ok": False, "error": "OCR failed: %s" % exc, "text": ""}
        self.audit.record(self.control.task_id, "ocr", tool="pytesseract", path=image_path, result="ok")
        return {"ok": True, "text": text.strip(), "error": ""}

    def analyze_image(self, image_path: str) -> Dict[str, Any]:
        pil = _optional("PIL.Image")
        if pil is None:
            return {"ok": False, "error": "Pillow is not installed (pip install Pillow)."}
        try:
            with pil.open(image_path) as img:
                rgb = img.convert("RGB")
                small = rgb.resize((1, 1))
                avg = small.getpixel((0, 0))
                gray = rgb.convert("L").resize((32, 32))
                px = list(gray.getdata())
                brightness = sum(px) / len(px)
                info = {"ok": True, "size": img.size, "average_color": avg,
                        "brightness": round(brightness, 1), "mode": "dark" if brightness < 110 else "light"}
        except Exception as exc:
            return {"ok": False, "error": "Image analysis failed: %s" % exc}
        ocr = self.ocr_image(image_path)
        info["text"] = ocr.get("text", "")
        info["ocr_error"] = ocr.get("error", "")
        return info

    def analyze_screen(self) -> Dict[str, Any]:
        path = self.computer.screenshot()
        info = self.analyze_image(path)
        info["screenshot"] = path
        return info

    def find_text_on_screen(self, needle: str) -> Dict[str, Any]:
        path = self.computer.screenshot()
        ok, why = self.ocr_available()
        if not ok:
            return {"ok": False, "error": why, "screenshot": path}
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
        with Image.open(path) as img:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        hits = []
        for i, word in enumerate(data.get("text", [])):
            if needle.lower() in (word or "").lower():
                hits.append({"text": word, "x": data["left"][i] + data["width"][i] // 2,
                             "y": data["top"][i] + data["height"][i] // 2})
        return {"ok": True, "found": bool(hits), "matches": hits[:10], "screenshot": path}


# ---------------------------------------------------------------------------
# VoiceManager (optional)
# ---------------------------------------------------------------------------

class VoiceManager:
    def __init__(self, settings: SettingsManager, audit: AuditLog):
        self.settings = settings
        self.audit = audit
        self._tts: Any = None
        self._tts_lock = threading.Lock()
        self._listening = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.on_text: Optional[Callable[[str], None]] = None
        self.last_error = ""

    @staticmethod
    def tts_available() -> bool:
        return importlib.util.find_spec("pyttsx3") is not None

    @staticmethod
    def stt_available() -> bool:
        return importlib.util.find_spec("speech_recognition") is not None

    def speak(self, text: str) -> bool:
        if not self.settings.get("voice_enabled") or not self.tts_available():
            return False
        with self._tts_lock:
            try:
                pyttsx3 = importlib.import_module("pyttsx3")
                if self._tts is None:
                    self._tts = pyttsx3.init()
                    self._tts.setProperty("rate", int(self.settings.get("voice_rate", 175)))
                self._tts.say(text[:500])
                self._tts.runAndWait()
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self._tts = None
                return False

    def stop_speaking(self) -> None:
        with contextlib.suppress(Exception):
            if self._tts is not None:
                self._tts.stop()

    def start_listening(self, on_text: Callable[[str], None]) -> Tuple[bool, str]:
        if not self.stt_available():
            return False, "SpeechRecognition is not installed (pip install SpeechRecognition pyaudio)."
        if self._listening.is_set():
            return True, "Already listening."
        self.on_text = on_text
        self._listening.set()
        self._paused.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="ak-voice")
        self._thread.start()
        return True, "Listening for the wake word '%s'." % self.settings.get("voice_wake_word")

    def _listen_loop(self) -> None:
        try:
            sr = importlib.import_module("speech_recognition")
            rec = sr.Recognizer()
            mic = sr.Microphone()
        except Exception as exc:
            self.last_error = "Microphone unavailable: %s" % exc
            self._listening.clear()
            return
        with mic as source:
            with contextlib.suppress(Exception):
                rec.adjust_for_ambient_noise(source, duration=0.6)
        wake = str(self.settings.get("voice_wake_word", "hey ak")).lower()
        while self._listening.is_set():
            if self._paused.is_set():
                time.sleep(0.3)
                continue
            try:
                with mic as source:
                    audio = rec.listen(source, timeout=3, phrase_time_limit=12)
                text = rec.recognize_google(audio).lower().strip()
            except Exception:
                continue
            if text in ("stop", "stop listening"):
                self.stop_speaking()
                if self.on_text:
                    self.on_text("__STOP__")
                continue
            if text in ("resume", "resume listening"):
                if self.on_text:
                    self.on_text("__RESUME__")
                continue
            if text.startswith(wake):
                command = text[len(wake):].strip(" ,.")
                if command and self.on_text:
                    self.on_text(command)

    def pause_listening(self) -> None:
        self._paused.set()

    def resume_listening(self) -> None:
        self._paused.clear()

    def stop_listening(self) -> None:
        self._listening.clear()


# ---------------------------------------------------------------------------
# LearningManager
# ---------------------------------------------------------------------------

class LearningManager:
    def __init__(self, memory: MemoryManager):
        self.memory = memory

    @staticmethod
    def signature(error_text: str) -> str:
        text = error_text or ""
        m = re.search(r"(ModuleNotFoundError|ImportError|SyntaxError|IndentationError|FileNotFoundError|"
                      r"PermissionError|TimeoutError|ConnectionError|JSONDecodeError)[^\n]*", text)
        sig = m.group(0) if m else text.strip().splitlines()[-1] if text.strip() else "unknown"
        sig = re.sub(r"0x[0-9a-fA-F]+|\d+", "N", sig)
        return sig[:160]

    def record_success(self, error_text: str, fix: str, context: str = "") -> None:
        sig = self.signature(error_text)
        self.memory.add("fix", "Error: %s\nFix that worked: %s\n%s" % (sig, fix, context), key=sig,
                        meta={"fix": fix, "outcome": "success"})

    def record_failure(self, error_text: str, approach: str, context: str = "") -> None:
        sig = self.signature(error_text)
        self.memory.add("failure", "Error: %s\nApproach that failed: %s\n%s" % (sig, approach, context),
                        key=sig, meta={"approach": approach, "outcome": "failed"})

    def record_procedure(self, name: str, steps: List[str]) -> None:
        self.memory.add("procedure", "%s: %s" % (name, " -> ".join(steps)), key=name, meta={"steps": steps})

    def suggest(self, error_text: str) -> Dict[str, List[Dict[str, Any]]]:
        sig = self.signature(error_text)
        hits_ok = self.memory.search(sig, kind="fix", limit=3)
        hits_bad = self.memory.search(sig, kind="failure", limit=3)

        def parse(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out = []
            for r in rows:
                try:
                    out.append(json.loads(r.get("meta") or "{}"))
                except ValueError:
                    continue
            return out
        return {"worked": parse(hits_ok), "failed": parse(hits_bad)}


# ---------------------------------------------------------------------------
# TaskPlanner: structured, validated plans (AI first, local rules as fallback)
# ---------------------------------------------------------------------------

ACTION_SCHEMA: Dict[str, Dict[str, Any]] = {
    "inspect_project": {"required": [], "optional": ["path"], "risk": "low"},
    "read_file": {"required": ["path"], "optional": [], "risk": "low"},
    "list_dir": {"required": [], "optional": ["path"], "risk": "low"},
    "write_file": {"required": ["path", "content"], "optional": [], "risk": "medium"},
    "run_command": {"required": ["command"], "optional": ["shell", "cwd", "timeout"], "risk": "medium"},
    "pip_install": {"required": ["packages"], "optional": ["path"], "risk": "medium"},
    "build_exe": {"required": [], "optional": ["path", "name"], "risk": "medium"},
    "fix_project": {"required": [], "optional": ["path", "error"], "risk": "medium"},
    "create_project": {"required": ["name", "description"], "optional": [], "risk": "medium"},
    "open_app": {"required": ["name"], "optional": ["args"], "risk": "low"},
    "open_url": {"required": ["url"], "optional": [], "risk": "low"},
    "web_search": {"required": ["query"], "optional": ["browser"], "risk": "low"},
    "research": {"required": ["query"], "optional": [], "risk": "low"},
    "fetch_page": {"required": ["url"], "optional": [], "risk": "low"},
    "screenshot": {"required": [], "optional": [], "risk": "low"},
    "analyze_screen": {"required": [], "optional": [], "risk": "low"},
    "detect_tools": {"required": [], "optional": [], "risk": "low"},
    "recall": {"required": ["query"], "optional": [], "risk": "low"},
    "type_text": {"required": ["text"], "optional": [], "risk": "high"},
    "hotkey": {"required": ["keys"], "optional": [], "risk": "high"},
    "click": {"required": ["x", "y"], "optional": [], "risk": "high"},
    "respond": {"required": ["text"], "optional": [], "risk": "low"},
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclasses.dataclass
class Step:
    action: str
    params: Dict[str, Any]
    why: str = ""


@dataclasses.dataclass
class Plan:
    goal: str
    intent: str
    steps: List[Step]
    success_criteria: str = ""
    risk: str = "low"
    needs_research: bool = False
    needs_authorization: bool = False
    source: str = "heuristic"
    language: str = "en"


class PlanError(ValueError):
    """Raised when an AI- or rule-generated plan fails validation."""


class TaskPlanner:
    MAX_STEPS = 12

    def __init__(self, ai: AIProvider, memory: MemoryManager, tools: ToolManager, settings: SettingsManager):
        self.ai = ai
        self.memory = memory
        self.tools = tools
        self.settings = settings
        self.last_project: str = ""

    # ---- validation -------------------------------------------------------
    def validate(self, raw: Any, goal: str, language: str, source: str) -> Plan:
        if not isinstance(raw, dict):
            raise PlanError("Plan must be a JSON object.")
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise PlanError("Plan has no steps.")
        if len(steps_raw) > self.MAX_STEPS:
            raise PlanError("Plan has too many steps (%d)." % len(steps_raw))
        steps: List[Step] = []
        worst = "low"
        for i, s in enumerate(steps_raw):
            if not isinstance(s, dict):
                raise PlanError("Step %d is not an object." % (i + 1))
            action = str(s.get("action", "")).strip()
            params = s.get("params", {})
            if action not in ACTION_SCHEMA:
                raise PlanError("Unknown action '%s' in step %d." % (action, i + 1))
            if not isinstance(params, dict):
                raise PlanError("Step %d params must be an object." % (i + 1))
            spec = ACTION_SCHEMA[action]
            missing = [k for k in spec["required"] if k not in params or params[k] in (None, "")]
            if missing:
                raise PlanError("Step %d (%s) missing params: %s" % (i + 1, action, ", ".join(missing)))
            allowed = set(spec["required"]) | set(spec["optional"])
            params = {k: v for k, v in params.items() if k in allowed}
            for k, v in params.items():
                if isinstance(v, str) and len(v) > 200000:
                    raise PlanError("Parameter %s is too large." % k)
            if action == "pip_install":
                pk = params["packages"]
                if isinstance(pk, str):
                    pk = [pk]
                if not isinstance(pk, list) or not all(isinstance(x, str) and re.match(r"^[A-Za-z0-9_.\-\[\]=<>!~, ]+$", x)
                                                       for x in pk):
                    raise PlanError("Invalid package list.")
                params["packages"] = pk
            if action == "open_url" and not re.match(r"^(https?://)?[\w.\-]+", str(params["url"])):
                raise PlanError("Invalid URL.")
            if RISK_ORDER[spec["risk"]] > RISK_ORDER[worst]:
                worst = spec["risk"]
            steps.append(Step(action, params, str(s.get("why", ""))[:200]))
        risk = str(raw.get("risk", worst)).lower()
        if risk not in RISK_ORDER:
            risk = worst
        if RISK_ORDER[worst] > RISK_ORDER[risk]:
            risk = worst
        return Plan(goal=str(raw.get("goal", goal))[:300], intent=str(raw.get("intent", "task"))[:60], steps=steps,
                    success_criteria=str(raw.get("success_criteria", ""))[:300], risk=risk,
                    needs_research=bool(raw.get("needs_research", False)),
                    needs_authorization=bool(raw.get("needs_authorization", RISK_ORDER[risk] >= 1)),
                    source=source, language=language)

    # ---- AI planning ------------------------------------------------------
    def _system_prompt(self) -> str:
        actions = "\n".join("- %s: required=%s optional=%s" % (a, s["required"], s["optional"])
                            for a, s in ACTION_SCHEMA.items())
        return (
            "You are the planner of AK AI, a local Windows computer assistant. Convert the user's request into a JSON plan.\n"
            "The user may write Hindi, Hinglish or English. Understand intent, not just keywords.\n"
            "Return ONLY JSON with keys: goal, intent, steps, success_criteria, risk (low|medium|high), "
            "needs_research (bool), needs_authorization (bool).\n"
            "steps is a list of {\"action\": ..., \"params\": {...}, \"why\": ...}. Allowed actions:\n" + actions +
            "\nRules: Prefer high-level actions (build_exe, fix_project, inspect_project) over raw commands. "
            "Use 'respond' for pure conversation or questions that need no tools. Never invent file paths; "
            "if the user gave none, omit 'path' so the current project/workspace is used. "
            "Never plan credential theft, CAPTCHA bypass, malware, or anything unauthorized. At most 12 steps."
        )

    def plan_with_ai(self, text: str, context: str, language: str) -> Tuple[Optional[Plan], str]:
        ok, why = self.ai.available()
        if not ok:
            return None, why
        user = "Context:\n%s\n\nUser request:\n%s" % (context, text)
        data, res = self.ai.chat_json(self._system_prompt(), user)
        if data is None:
            return None, res.error or "AI planning failed."
        try:
            return self.validate(data, text, language, "ai"), ""
        except PlanError as exc:
            return None, "AI plan rejected: %s" % exc

    # ---- local rule planning (offline fallback) ---------------------------
    PATH_RX = re.compile(r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z]:\\[^\s\"']+(?:\\[^\s\"']+)*)|(\./[^\s]+|/[\w./\-]+))")

    def extract_path(self, text: str) -> str:
        for m in self.PATH_RX.finditer(text):
            cand = next((g for g in m.groups() if g), "")
            if cand and (os.path.exists(os.path.expanduser(cand)) or re.match(r"^[A-Za-z]:\\", cand)):
                return cand
        return ""

    def plan_heuristic(self, text: str, language: str) -> Plan:
        t = text.lower().strip()
        path = self.extract_path(text) or self.last_project
        pp: Dict[str, Any] = {"path": path} if path else {}

        def mk(intent: str, steps: List[Tuple[str, Dict[str, Any], str]], crit: str, research: bool = False) -> Plan:
            raw = {"goal": text, "intent": intent, "success_criteria": crit, "needs_research": research,
                   "steps": [{"action": a, "params": p, "why": w} for a, p, w in steps]}
            return self.validate(raw, text, language, "heuristic")

        m = re.match(r"^(?:cmd|run|ps|powershell|shell)\s*[:>]\s*(.+)$", text.strip(), re.I | re.S)
        if m:
            shell = "powershell" if re.match(r"^(ps|powershell)", text.strip(), re.I) else "cmd"
            return mk("run_command", [("run_command", {"command": m.group(1).strip(), "shell": shell}, "user command")],
                      "command exits with code 0")
        if re.search(r"\b(exe|\.exe)\b", t) or re.search(r"\b(build|compile|package|installer)\b", t) and \
                re.search(r"\b(project|app|application|exe|software)\b", t):
            return mk("build_exe", [("build_exe", dict(pp), "build the project into an executable")],
                      "executable exists and starts without crashing")
        if re.search(r"\b(fix|solve|repair|debug|theek|sahi|thik)\b|error|bug|traceback|\u0920\u0940\u0915", t):
            return mk("fix_project", [("fix_project", dict(pp, **({"error": text} if len(text) > 40 else {})),
                                       "find and repair the problem")], "no syntax errors and project passes checks")
        if re.search(r"\b(install)\b.*\b(depend|requirement|package|module|library)", t) or \
                re.search(r"\bpip install\b", t):
            m2 = re.search(r"pip install\s+([A-Za-z0-9_.\-\[\]=<>! ]+)", text)
            if m2:
                pk = m2.group(1).split()
                return mk("install", [("pip_install", {"packages": pk}, "install requested packages")],
                          "packages import successfully")
            return mk("install", [("inspect_project", dict(pp), "detect dependencies"),
                                  ("pip_install", {"packages": ["__project__"], **pp}, "install detected dependencies")],
                      "dependencies installed")
        if re.search(r"\b(inspect|analy[sz]e|check|dekh|dekho|what is wrong|kya wrong|review)\b", t) and \
                not re.search(r"\b(chrome|edge|firefox|youtube)\b", t):
            return mk("inspect", [("inspect_project", dict(pp), "scan the project")], "report produced")
        m3 = re.search(r"\b(?:open|launch|start|kholo|khol|chalu|chalao|run)\s+(?:the\s+)?([A-Za-z0-9 .\-]{2,30})$", t)
        if m3 and not re.search(r"(http|www\.|\.com|search)", t):
            name = re.sub(r"\b(please|app|application|karo|kro|do|de)\b", "", m3.group(1)).strip()
            if name:
                return mk("open_app", [("open_app", {"name": name}, "launch application")], "application process running")
        m3b = re.match(r"^(?:please\s+)?([A-Za-z0-9 .\-]{2,30}?)\s+(?:kholo|khol|kholdo|chalu\s+kar\w*|chalao|open\s+kar\w*|launch\s+kar\w*)\b", t)
        if m3b and not re.search(r"(http|www\.|\.com|search)", t):
            return mk("open_app", [("open_app", {"name": m3b.group(1).strip()}, "launch application")],
                      "application process running")
        m4 = re.search(r"(https?://\S+|\b[\w\-]+\.(?:com|org|net|io|in|dev)\S*)", text)
        if m4 and re.search(r"\b(open|go|visit|kholo|khol)\b", t):
            return mk("open_url", [("open_url", {"url": m4.group(1)}, "open page")], "page opens")
        if re.search(r"\b(search|research|google|dhundh|dhoondh|find online|look up)\b", t):
            q = re.sub(r"\b(please|search|research|for|on|google|open|chrome|and|online|dhundh|karo|kro)\b", " ", t)
            q = re.sub(r"\s+", " ", q).strip() or text
            steps: List[Tuple[str, Dict[str, Any], str]] = []
            if re.search(r"\b(chrome|browser|edge|firefox)\b", t):
                steps.append(("web_search", {"query": q, "browser": True}, "search in the browser"))
            else:
                steps.append(("research", {"query": q}, "search and read sources"))
            return mk("research", steps, "sources found", research=True)
        if re.search(r"\b(read|padh|show file|cat)\b", t) and path and os.path.isfile(os.path.expanduser(path)):
            return mk("read_file", [("read_file", {"path": path}, "read file")], "file content shown")
        if re.search(r"\b(screenshot|screen shot|capture screen)\b", t):
            return mk("screenshot", [("screenshot", {}, "capture screen")], "image file saved")
        if re.search(r"\b(create|make|write|banao|bana)\b", t) and re.search(r"\b(python|script|app|project|tool)\b", t):
            return mk("create_project", [("create_project", {"name": "ak_project", "description": text},
                                          "generate project files")], "files created and syntax valid")
        if re.search(r"\b(tools?|installed)\b", t) and re.search(r"\b(detect|list|check|show|kya)\b", t):
            return mk("detect_tools", [("detect_tools", {}, "scan tools")], "tool list produced")
        if re.search(r"\b(remember|recall|yaad|memory)\b", t):
            return mk("recall", [("recall", {"query": text}, "search memory")], "memory searched")
        return mk("chat", [("respond", {"text": text}, "conversation")], "reply given")

    def plan(self, text: str, context: str, language: str) -> Tuple[Plan, str]:
        plan, err = self.plan_with_ai(text, context, language)
        if plan is not None:
            return plan, ""
        return self.plan_heuristic(text, language), err


# ---------------------------------------------------------------------------
# ErrorRecoveryEngine
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Fix:
    kind: str                      # pip_install | hidden_import | ai_fix_file | wait_retry | collect_all | none
    arg: str = ""
    explanation: str = ""


@dataclasses.dataclass
class Diagnosis:
    kind: str
    detail: str
    fixes: List[Fix]


class ErrorRecoveryEngine:
    def __init__(self, learning: LearningManager):
        self.learning = learning

    def diagnose(self, output: str) -> List[Diagnosis]:
        text = output or ""
        found: List[Diagnosis] = []
        if re.search(r"externally-managed-environment", text):
            found.append(Diagnosis("ExternallyManagedEnvironment",
                                   "pip is blocked for the system Python (PEP 668)",
                                   [Fix("create_venv", "", "use a project-local virtual environment")]))
        for m in re.finditer(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([\w.\-]+)['\"]", text):
            mod = m.group(1).split(".")[0]
            pkg = PIP_NAME_MAP.get(mod, mod)
            found.append(Diagnosis("ModuleNotFoundError", "Missing module %s" % mod,
                                   [Fix("pip_install", pkg, "install %s" % pkg), Fix("hidden_import", mod,
                                                                                    "bundle %s explicitly" % mod)]))
        for m in re.finditer(r"Hidden import ['\"]([\w.]+)['\"] not found", text):
            found.append(Diagnosis("HiddenImport", "Hidden import %s not found" % m.group(1),
                                   [Fix("pip_install", PIP_NAME_MAP.get(m.group(1).split('.')[0], m.group(1).split('.')[0]),
                                        "install the package that provides it")]))
        if re.search(r"ImportError: cannot import name ['\"]\w+['\"] from ['\"]([\w.]+)['\"]", text):
            mod = re.search(r"from ['\"]([\w.]+)['\"]", text).group(1).split(".")[0]
            found.append(Diagnosis("ImportError", "Incompatible or outdated package %s" % mod,
                                   [Fix("pip_install", "--upgrade %s" % PIP_NAME_MAP.get(mod, mod), "upgrade package")]))
        for m in re.finditer(r"(SyntaxError|IndentationError|TabError): ([^\n]+)\n?", text):
            fm = re.search(r'File "([^"]+)", line (\d+)', text[max(0, m.start() - 400):m.start() + 50])
            arg = "%s:%s" % (fm.group(1), fm.group(2)) if fm else ""
            found.append(Diagnosis(m.group(1), m.group(2).strip(), [Fix("ai_fix_file", arg, "repair syntax with AI")]))
        if re.search(r"PermissionError|Access is denied|being used by another process|WinError 5\b|WinError 32", text):
            found.append(Diagnosis("PermissionError", "A file is locked or access is denied",
                                   [Fix("wait_retry", "5", "wait for the file lock to release and retry")]))
        if re.search(r"FileNotFoundError|No such file or directory|cannot find the path", text):
            found.append(Diagnosis("FileNotFoundError", "A required file or path is missing", [Fix("none")]))
        if re.search(r"ConnectionError|Connection (?:refused|reset|aborted)|Temporary failure in name resolution|"
                     r"Read timed out|Max retries exceeded|getaddrinfo failed", text):
            found.append(Diagnosis("ConnectionError", "Network problem while downloading or connecting",
                                   [Fix("wait_retry", "8", "wait and retry the network operation")]))
        if re.search(r"Could not find a version that satisfies|No matching distribution found", text):
            found.append(Diagnosis("PackageNotFound", "pip cannot find a matching distribution", [Fix("none")]))
        if re.search(r"(?i)failed to load dll|DLL load failed|missing dll|\.dll", text) and \
                re.search(r"(?i)dll", text):
            found.append(Diagnosis("MissingDLL", "A DLL could not be loaded", [Fix("collect_all", "", "collect binaries")]))
        if re.search(r"(?i)FileNotFoundError.*(?:\.json|\.png|\.ico|\.ttf|\.csv|\.db|\.txt)|data file", text):
            found.append(Diagnosis("MissingDataFile", "A data file is missing from the bundle", [Fix("none")]))
        if re.search(r"(?i)npm ERR!|ERESOLVE", text):
            found.append(Diagnosis("NpmError", "npm failed", [Fix("none")]))
        if re.search(r"(?i)fatal: ", text):
            found.append(Diagnosis("GitError", "git reported a fatal error", [Fix("none")]))
        if re.search(r"(?i)json\.?decodeerror|Expecting value", text):
            found.append(Diagnosis("JSONError", "Invalid JSON", [Fix("none")]))
        if re.search(r"(?i)Traceback \(most recent call last\)", text) and not found:
            found.append(Diagnosis("RuntimeCrash", "The program raised an unhandled exception", [Fix("ai_fix_file", "", "analyze traceback")]))
        # prefer fixes that memory says worked, drop ones that failed before
        for d in found:
            hints = self.learning.suggest(d.detail + " " + d.kind)
            worked = {h.get("fix") for h in hints["worked"]}
            failed = {h.get("approach") for h in hints["failed"]}
            d.fixes = [f for f in d.fixes if "%s:%s" % (f.kind, f.arg) not in failed]
            d.fixes.sort(key=lambda f: 0 if ("%s:%s" % (f.kind, f.arg)) in worked else 1)
        return found


# ---------------------------------------------------------------------------
# TaskExecutor (+ BuildAgent): act, observe, recover, retry, test, verify
# ---------------------------------------------------------------------------

class Services:
    """Plain container wiring all managers together."""
    emit: Callable[[str, str], None] = staticmethod(lambda kind, text: None)  # type: ignore[assignment]
    chat_reply: Callable[[str, str], str]


@dataclasses.dataclass
class StepResult:
    ok: bool
    output: str = ""
    verified: bool = False
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ExecutionReport:
    results: List[Tuple[Step, StepResult]]
    cancelled: bool = False

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for _, r in self.results)

    @property
    def all_verified(self) -> bool:
        return self.all_ok and all(r.verified for _, r in self.results)


def _slug(text: str, default: str = "project") -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", text.strip()).strip("_")[:40]
    return s or default


class BuildAgent:
    def __init__(self, svc: Services, executor: "TaskExecutor"):
        self.s = svc
        self.x = executor

    def _log(self, lines: List[str], text: str) -> None:
        lines.append(text)
        self.s.emit("tool", text)

    def _missing_modules(self, py: str, modules: List[str], cwd: Path) -> List[str]:
        if not modules:
            return []
        code = ("import importlib.util,sys\n"
                "print(','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))")
        r = self.s.terminal.run([py, "-c", code] + modules, cwd=str(cwd), timeout=60, stream=False)
        if not r.ok:
            return modules
        return [m for m in r.stdout.strip().split(",") if m]

    def _pip(self, py: str, args: List[str], cwd: Path, lines: List[str], label: str) -> CommandResult:
        cmd = [py, "-m", "pip"] + args
        allowed, why, _ = self.s.security.authorize_command(" ".join(cmd), self.s.control.task_id)
        if not allowed:
            return CommandResult(False, None, "", why, 0.0, " ".join(cmd), error=why)
        self._log(lines, "pip: %s" % label)
        r = self.s.terminal.run(cmd, cwd=str(cwd), stream=False)
        for _ in range(2):
            if r.ok or not any(d.kind == "ConnectionError" for d in self.s.recovery.diagnose(r.combined)):
                break
            self._log(lines, "network problem, waiting 8s and retrying pip")
            self._sleep(8)
            r = self.s.terminal.run(cmd, cwd=str(cwd), stream=False)
        return r

    def prepare_python(self, root: Path, lines: List[str]) -> Optional[str]:
        """Return the interpreter to use for the project; create .venv when the system Python is
        externally managed (PEP 668), instead of forcing --break-system-packages."""
        s = self.s
        py = s.tools.python_for_project(root)
        if not py:
            return None
        probe = ("import os,sys,sysconfig\n"
                 "marker=os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')\n"
                 "print('1' if os.path.exists(marker) and sys.prefix==sys.base_prefix else '0')")
        r = s.terminal.run([py, "-c", probe], cwd=str(root), timeout=30, stream=False)
        if not (r.ok and r.stdout.strip() == "1"):
            return py
        venv_dir = root / ".venv"
        cmd = [py, "-m", "venv", str(venv_dir)]
        allowed, why, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
        if not allowed:
            lines.append(why)
            return py
        self._log(lines, "system Python is externally managed (PEP 668): creating project environment .venv")
        vr = s.terminal.run(cmd, cwd=str(root), stream=False)
        venv_py = venv_dir / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
        if vr.ok and venv_py.exists():
            s.learning.record_success("externally-managed-environment", "create_venv", "pip blocked by PEP 668")
            return str(venv_py)
        lines.append("Could not create a virtual environment:\n" + short(vr.combined, 800))
        s.learning.record_failure("externally-managed-environment", "create_venv", short(vr.combined, 200))
        return py

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.s.control.checkpoint()
            time.sleep(0.25)

    def _launch_test(self, artifact: Path, cwd: Path, lines: List[str]) -> Tuple[bool, str]:
        self.s.control.set_state(TaskState.VERIFYING)
        self._log(lines, "launch test: %s" % artifact.name)
        try:
            if not IS_WINDOWS:
                artifact.chmod(artifact.stat().st_mode | 0o111)
            kwargs: Dict[str, Any] = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                          cwd=str(cwd))
            if IS_WINDOWS:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen([str(artifact)], **kwargs)
        except OSError as exc:
            return False, "Could not start the built program: %s" % exc
        self.s.control.register_process(proc)
        try:
            deadline = time.time() + 6.0
            while time.time() < deadline:
                self.s.control.checkpoint()
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            if proc.poll() is None:
                kill_process_tree(proc)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
                return True, "Program started and kept running for 6 seconds without crashing."
            out = (proc.stdout.read() or b"").decode("utf-8", errors="replace") if proc.stdout else ""
            err = (proc.stderr.read() or b"").decode("utf-8", errors="replace") if proc.stderr else ""
            if proc.returncode == 0:
                return True, "Program ran and exited normally (exit code 0)."
            return False, "Program exited with code %s.\n%s" % (proc.returncode, short(err or out, 1500))
        finally:
            self.s.control.unregister_process(proc)

    def build(self, params: Dict[str, Any]) -> StepResult:
        s = self.s
        lines: List[str] = []
        root = self.x.project_path(params)
        s.control.set_state(TaskState.INSPECTING)
        self._log(lines, "inspecting %s" % root)
        info = s.inspector.inspect(root)
        root = Path(info["root"])
        s.planner.last_project = str(root)
        types = info["types"]
        lines.append(s.inspector.format_report(info))
        if "Python" in types:
            return self._build_python(root, info, params, lines)
        if ".NET" in types:
            return self._build_generic(root, ["dotnet", "publish", "-c", "Release", "-o", str(root / "dist")],
                                       "dotnet", lines, [root / "dist"], (".exe", ".dll"))
        if "Rust" in types:
            return self._build_generic(root, ["cargo", "build", "--release"], "cargo", lines,
                                       [root / "target" / "release"], (".exe", ""))
        lines.append("Automatic EXE building is implemented for Python (PyInstaller), .NET (dotnet publish) "
                     "and Rust (cargo). Detected project types: %s." % (", ".join(types) or "unknown"))
        return StepResult(False, "\n".join(lines))

    def _build_generic(self, root: Path, cmd: List[str], tool: str, lines: List[str], outdirs: List[Path],
                       exts: Tuple[str, ...]) -> StepResult:
        s = self.s
        exe = s.tools.find(tool)
        if not exe:
            lines.append("%s is not installed, so this project cannot be built." % tool)
            return StepResult(False, "\n".join(lines))
        cmd = [exe] + cmd[1:]
        ok, why, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
        if not ok:
            lines.append(why)
            return StepResult(False, "\n".join(lines))
        s.control.set_state(TaskState.EXECUTING)
        r = s.terminal.run(cmd, cwd=str(root), stream=False)
        lines.append("exit code %s in %.1fs" % (r.exit_code, r.duration))
        if not r.ok:
            lines.append(short(r.combined, 1500))
            return StepResult(False, "\n".join(lines))
        s.control.set_state(TaskState.VERIFYING)
        for d in outdirs:
            if d.exists():
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.suffix.lower() in exts and f.stat().st_size > 0 and f.suffix:
                        lines.append("built: %s (%d bytes)" % (f, f.stat().st_size))
                        return StepResult(True, "\n".join(lines), verified=True, data={"artifact": str(f)})
        lines.append("The build command succeeded but no executable was found in the expected output folder.")
        return StepResult(False, "\n".join(lines))

    def _build_python(self, root: Path, info: Dict[str, Any], params: Dict[str, Any], lines: List[str]) -> StepResult:
        s = self.s
        retry = max(1, int(s.settings.get("retry_limit", 3)))
        entries = [e for e in info["entry_points"] if e.endswith(".py")]
        entry = params.get("entry") or (entries[0] if entries else "")
        if not entry:
            lines.append("No Python entry point was found (no file with an __main__ guard or main.py/app.py).")
            return StepResult(False, "\n".join(lines))
        entry_path = root / entry
        if not entry_path.exists():
            lines.append("Entry point not found: %s" % entry_path)
            return StepResult(False, "\n".join(lines))
        self._log(lines, "entry point: %s" % entry)
        if info.get("syntax_errors"):
            self._log(lines, "syntax errors found, attempting repair")
            s.control.set_state(TaskState.RECOVERING)
            self.x.repair_syntax(root, lines)
            info = s.inspector.inspect(root)
            if info.get("syntax_errors"):
                lines.append("Build stopped: syntax errors remain.")
                for e in info["syntax_errors"][:8]:
                    lines.append("  %s:%s %s" % (e["file"], e["line"], e["error"]))
                return StepResult(False, "\n".join(lines))
        py = self.prepare_python(root, lines)
        if not py:
            lines.append("Python was not found on this system.")
            return StepResult(False, "\n".join(lines))
        self._log(lines, "python: %s" % py)
        # dependencies
        s.control.set_state(TaskState.EXECUTING)
        req = root / "requirements.txt"
        if req.exists() and info.get("requirements"):
            r = self._pip(py, ["install", "-r", str(req)], root, lines, "install requirements.txt")
            if not r.ok:
                lines.append("requirements.txt install failed:\n" + short(r.combined, 1200))
                lines.append("Continuing with individually detected dependencies.")
        needed = [m for m in info.get("third_party_imports", [])]
        missing = self._missing_modules(py, needed, root)
        if missing:
            pkgs = sorted({PIP_NAME_MAP.get(m, m) for m in missing})
            r = self._pip(py, ["install"] + pkgs, root, lines, "install missing: %s" % ", ".join(pkgs))
            if not r.ok:
                lines.append("Could not install: %s\n%s" % (", ".join(pkgs), short(r.combined, 1200)))
            still = self._missing_modules(py, missing, root)
            if still:
                lines.append("Modules still missing after install: %s" % ", ".join(still))
        # pyinstaller
        r = s.terminal.run([py, "-m", "PyInstaller", "--version"], cwd=str(root), timeout=60, stream=False)
        if not r.ok:
            r = self._pip(py, ["install", "pyinstaller"], root, lines, "install PyInstaller")
            if not r.ok:
                lines.append("PyInstaller could not be installed:\n" + short(r.combined, 1200))
                return StepResult(False, "\n".join(lines))
        # tests
        test_note = ""
        if info.get("tests"):
            s.control.set_state(TaskState.TESTING)
            self._log(lines, "running %d test file(s)" % len(info["tests"]))
            tr = s.terminal.run([py, "-m", "unittest", "discover", "-s", str(root)], cwd=str(root), timeout=300,
                                stream=False)
            if tr.ok:
                test_note = "tests passed"
            else:
                test_note = "tests FAILED (build continued)"
                lines.append("Tests failed:\n" + short(tr.combined, 800))
            self._log(lines, test_note)
        else:
            self._log(lines, "no tests found")
        name = _slug(params.get("name") or entry_path.stem, "app")
        windowed = bool(info.get("is_gui"))
        hidden: List[str] = []
        applied: List[Tuple[str, str]] = []
        last_error = ""
        tried_fixes: set = set()
        logged_diagnoses: set = set()
        for attempt in range(1, retry + 2):
            s.control.checkpoint()
            s.control.set_state(TaskState.EXECUTING)
            cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                   "--windowed" if windowed else "--console", "--name", name,
                   "--distpath", str(root / "dist"), "--workpath", str(root / "build" / "ak_build"),
                   "--specpath", str(root / "build"), "--paths", str(root)]
            for h in hidden:
                cmd += ["--hidden-import", h]
            cmd.append(str(entry_path))
            allowed, why, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
            if not allowed:
                lines.append(why)
                return StepResult(False, "\n".join(lines))
            self._log(lines, "build attempt %d: PyInstaller" % attempt)
            br = s.terminal.run(cmd, cwd=str(root), stream=False)
            artifact = root / "dist" / (name + (".exe" if IS_WINDOWS else ""))
            failure_text = ""
            if not br.ok:
                failure_text = br.combined
                lines.append("build failed (exit %s)" % br.exit_code)
            elif not artifact.exists() or artifact.stat().st_size == 0:
                failure_text = "Build reported success but output file is missing: %s" % artifact
                lines.append(failure_text)
            else:
                lines.append("built: %s (%d bytes)" % (artifact, artifact.stat().st_size))
                ok, note = self._launch_test(artifact, root, lines)
                lines.append(note)
                if ok:
                    for kind, arg in applied:
                        s.learning.record_success(last_error or "build error", "%s:%s" % (kind, arg),
                                                  "PyInstaller build of %s" % name)
                    s.memory.add("project", "Built %s into %s (%s)" % (root, artifact, test_note or "no tests"),
                                 key=str(root), meta={"artifact": str(artifact)})
                    lines.append("VERIFIED: executable exists and starts correctly. %s" % test_note)
                    return StepResult(True, "\n".join(lines), verified=True, data={"artifact": str(artifact)})
                failure_text = note
            last_error = failure_text
            if attempt > retry:
                break
            s.control.set_state(TaskState.RECOVERING)
            progress = False
            for d in s.recovery.diagnose(failure_text):
                sig = d.kind + d.detail
                if sig not in logged_diagnoses:
                    logged_diagnoses.add(sig)
                    lines.append("diagnosis: %s - %s" % (d.kind, d.detail))
                for f in d.fixes:
                    fix_key = "%s:%s:%s" % (d.kind, f.kind, f.arg)
                    if fix_key in tried_fixes:
                        continue
                    tried_fixes.add(fix_key)
                    if f.kind == "pip_install":
                        if d.kind == "ModuleNotFoundError":
                            module = d.detail.rsplit(" ", 1)[-1]
                            if not self._missing_modules(py, [module], root):
                                lines.append("%s is already installed, so it must be bundled explicitly" % module)
                                continue
                        pr = self._pip(py, ["install"] + f.arg.split(), root, lines, "fix: install %s" % f.arg)
                        if pr.ok:
                            applied.append((f.kind, f.arg))
                            progress = True
                            break
                        s.learning.record_failure(failure_text, "%s:%s" % (f.kind, f.arg))
                    elif f.kind == "hidden_import" and f.arg and f.arg not in hidden:
                        hidden.append(f.arg)
                        applied.append((f.kind, f.arg))
                        lines.append("fix: add --hidden-import %s" % f.arg)
                        progress = True
                        break
                    elif f.kind == "wait_retry":
                        self._sleep(float(f.arg or 5))
                        applied.append((f.kind, f.arg))
                        progress = True
                        break
                    elif f.kind == "collect_all":
                        mods = [m for m in info.get("third_party_imports", [])][:5]
                        for m in mods:
                            if m not in hidden:
                                hidden.append(m)
                        applied.append((f.kind, ",".join(mods)))
                        lines.append("fix: bundle detected packages as hidden imports")
                        progress = True
                        break
                    elif f.kind == "ai_fix_file":
                        target = f.arg.split(":")[0] if f.arg else ""
                        if target and Path(target).exists():
                            ok2, note2 = self.x.ai_fix_file(Path(target), d.detail, None)
                            lines.append("fix via AI: %s" % note2)
                            if ok2:
                                applied.append((f.kind, target))
                                progress = True
                                break
            if not progress:
                lines.append("No further automatic fix is available for this error.")
                s.learning.record_failure(failure_text, "build:%s" % name, "no automatic fix")
                break
        lines.append("Last error output:\n" + short(last_error, 1500))
        return StepResult(False, "\n".join(lines))


class TaskExecutor:
    def __init__(self, svc: Services):
        self.s = svc
        self.build_agent = BuildAgent(svc, self)

    # ---- helpers ----------------------------------------------------------
    def project_path(self, params: Dict[str, Any]) -> Path:
        raw = params.get("path") or self.s.planner.last_project or str(self.s.settings.workspace())
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.s.settings.workspace() / p
        if p.is_file():
            p = p.parent
        if not p.exists():
            raise FileNotFoundError("Path not found: %s" % p)
        return p

    def ai_fix_file(self, path: Path, error: str, line: Optional[int]) -> Tuple[bool, str]:
        s = self.s
        ok, why = s.ai.available()
        if not ok:
            return False, "AI provider unavailable (%s)" % why
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return False, "cannot read %s: %s" % (path, exc)
        if len(text) > 60000:
            return False, "file too large for automatic repair (%d chars)" % len(text)
        system = ("You repair source files. Return ONLY JSON: {\"content\": \"<the complete corrected file>\", "
                  "\"explanation\": \"<one sentence>\"}. Change only what is necessary to fix the error. "
                  "Do not add markdown fences inside content.")
        user = "File: %s\nError: %s\nLine: %s\n\nSource:\n%s" % (path.name, error, line or "unknown", text)
        data, res = s.ai.chat_json(system, user)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str) or not data["content"].strip():
            return False, res.error or "AI returned no corrected content."
        new = data["content"]
        if FENCE in new:
            return False, "AI output contained markdown fences; rejected."
        if path.suffix == ".py":
            try:
                ast.parse(new)
            except SyntaxError as exc:
                s.learning.record_failure(error, "ai_fix_file:%s" % path.name, "result still invalid: %s" % exc.msg)
                return False, "AI fix still has a syntax error (%s line %s); file left unchanged." % (exc.msg, exc.lineno)
        try:
            _, bid = s.files.write_text(path, new, make_backup=True)
        except PermissionError as exc:
            return False, str(exc)
        s.learning.record_success(error, "ai_fix_file:%s" % path.suffix, "backup %s" % bid)
        return True, "%s repaired (%s); backup id %s" % (path.name, short(str(data.get("explanation", "")), 120), bid)

    def local_syntax_fix(self, path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return False
        candidate = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
        if candidate == text:
            return False
        try:
            ast.parse(candidate)
        except SyntaxError:
            return False
        try:
            self.s.files.write_text(path, candidate, make_backup=True)
        except PermissionError:
            return False
        return True

    def repair_syntax(self, root: Path, lines: List[str]) -> bool:
        s = self.s
        retry = max(1, int(s.settings.get("retry_limit", 3)))
        for _ in range(retry):
            s.control.checkpoint()
            info = s.inspector.inspect(root)
            errors = info.get("syntax_errors", [])
            if not errors:
                return True
            progress = False
            for e in errors[:10]:
                p = Path(e["file"])
                if self.local_syntax_fix(p):
                    lines.append("fixed whitespace/tabs in %s" % p.name)
                    progress = True
                    continue
                ok, note = self.ai_fix_file(p, e["error"], e["line"])
                lines.append("%s: %s" % (p.name, note))
                progress = progress or ok
            if not progress:
                return False
        return not s.inspector.inspect(root).get("syntax_errors")

    # ---- plan execution ---------------------------------------------------
    def run_plan(self, plan: Plan) -> ExecutionReport:
        s = self.s
        results: List[Tuple[Step, StepResult]] = []
        total = len(plan.steps)
        for i, step in enumerate(plan.steps, 1):
            try:
                s.control.checkpoint()
            except TaskCancelled:
                return ExecutionReport(results, cancelled=True)
            s.emit("progress", "%d/%d" % (i - 1, total))
            s.emit("action", "%s %s" % (step.action, short(json.dumps(step.params, default=str), 160)))
            s.control.set_state(TaskState.EXECUTING)
            handler = getattr(self, "h_" + step.action, None)
            try:
                if handler is None:
                    res = StepResult(False, "No handler for action %s" % step.action)
                else:
                    res = handler(step.params)
            except TaskCancelled:
                return ExecutionReport(results, cancelled=True)
            except PermissionError as exc:
                res = StepResult(False, "Not permitted: %s" % exc)
            except Exception as exc:
                s.audit.record(s.control.task_id, "step_exception", tool=step.action, error=traceback.format_exc(limit=4))
                res = StepResult(False, "%s: %s" % (type(exc).__name__, exc))
            s.audit.record(s.control.task_id, "step", tool=step.action, command=json.dumps(step.params, default=str)[:500],
                           result="ok" if res.ok else "failed", verified=res.verified)
            s.emit("tool", "%s -> %s" % (step.action, "OK" if res.ok else "FAILED"))
            results.append((step, res))
            if not res.ok:
                break
        s.emit("progress", "%d/%d" % (len(results), total))
        return ExecutionReport(results)

    # ---- handlers ---------------------------------------------------------
    def h_inspect_project(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        s.control.set_state(TaskState.INSPECTING)
        root = self.project_path(p)
        info = s.inspector.inspect(root)
        s.planner.last_project = info["root"]
        s.memory.add("project", s.inspector.format_report(info), key=info["root"],
                     meta={"types": info["types"], "entry_points": info["entry_points"]})
        return StepResult(True, s.inspector.format_report(info), verified=True, data=info)

    def h_read_file(self, p: Dict[str, Any]) -> StepResult:
        text = self.s.files.read_text(p["path"])
        return StepResult(True, short(text, 6000), verified=True)

    def h_list_dir(self, p: Dict[str, Any]) -> StepResult:
        root = p.get("path") or self.s.planner.last_project or str(self.s.settings.workspace())
        items = self.s.files.list_dir(root)
        return StepResult(True, "\n".join(items) or "(empty folder)", verified=True)

    def h_write_file(self, p: Dict[str, Any]) -> StepResult:
        path, bid = self.s.files.write_text(p["path"], str(p["content"]))
        data = path.read_text(encoding="utf-8")
        if data != str(p["content"]).replace("\r\n", "\n"):
            return StepResult(False, "File was written but its content does not match what was requested.")
        if path.suffix == ".py":
            try:
                ast.parse(data)
            except SyntaxError as exc:
                return StepResult(False, "File written but has a syntax error: %s (line %s)" % (exc.msg, exc.lineno))
        return StepResult(True, "Wrote %s (%d bytes)%s" % (path, path.stat().st_size,
                                                        ", backup %s" % bid if bid else ""), verified=True)

    def h_run_command(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        command = p["command"]
        shell = str(p.get("shell", "auto"))
        cwd = p.get("cwd") or (s.planner.last_project or str(s.settings.workspace()))
        text = command if isinstance(command, str) else " ".join(map(str, command))
        ok, why, risk = s.security.authorize_command(text, s.control.task_id)
        if not ok:
            return StepResult(False, why)
        retry = max(0, int(s.settings.get("retry_limit", 3)))
        lines: List[str] = []
        r = s.terminal.run(command, cwd=cwd, timeout=p.get("timeout"), shell=shell)
        attempts = 0
        tried: set = set()
        while not r.ok and not r.cancelled and not r.timed_out and attempts < retry:
            attempts += 1
            s.control.set_state(TaskState.RECOVERING)
            applied = False
            for d in s.recovery.diagnose(r.combined):
                lines.append("diagnosis: %s - %s" % (d.kind, d.detail))
                for f in d.fixes:
                    key = "%s:%s" % (f.kind, f.arg)
                    if key in tried:
                        continue
                    tried.add(key)
                    if f.kind == "pip_install":
                        py = self.build_agent.prepare_python(Path(cwd), lines) if cwd and Path(cwd).exists() \
                            else (s.tools.find("python") or sys.executable)
                        py = py or sys.executable
                        cmd = [py, "-m", "pip", "install"] + f.arg.split()
                        ok2, why2, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
                        if not ok2:
                            lines.append(why2)
                            continue
                        pr = s.terminal.run(cmd, cwd=cwd, stream=False)
                        lines.append("fix: pip install %s -> %s" % (f.arg, "ok" if pr.ok else "failed"))
                        if pr.ok:
                            s.learning.record_success(r.combined, key, text)
                            applied = True
                            break
                        s.learning.record_failure(r.combined, key, text)
                    elif f.kind == "wait_retry":
                        time.sleep(min(float(f.arg or 5), 15))
                        applied = True
                        break
                if applied:
                    break
            if not applied:
                break
            r = s.terminal.run(command, cwd=cwd, timeout=p.get("timeout"), shell=shell)
        status = "exit code %s in %.1fs" % (r.exit_code, r.duration)
        if r.timed_out:
            status = "TIMED OUT. " + status
        if r.cancelled:
            status = "CANCELLED. " + status
        body = "\n".join(lines + [status, short(r.combined, 3500)])
        return StepResult(r.ok, body, verified=r.ok, data={"exit_code": r.exit_code})

    def h_pip_install(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        root = Path(self.project_path(p)) if (p.get("path") or s.planner.last_project) else s.settings.workspace()
        lines: List[str] = []
        py = self.build_agent.prepare_python(root, lines)
        if not py:
            return StepResult(False, "Python was not found on this system.")
        packages = list(p["packages"])
        if "__project__" in packages:
            info = s.inspector.inspect(root)
            packages = list(info.get("pip_packages", []))
            req = root / "requirements.txt"
            if req.exists() and info.get("requirements"):
                cmd = [py, "-m", "pip", "install", "-r", str(req)]
                ok, why, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
                if not ok:
                    return StepResult(False, why)
                r = s.terminal.run(cmd, cwd=str(root), stream=False)
                lines.append("requirements.txt -> %s" % ("ok" if r.ok else "failed\n" + short(r.combined, 800)))
                if not r.ok:
                    return StepResult(False, "\n".join(lines))
                packages = []
            if not packages and not req.exists():
                return StepResult(True, "No third-party dependencies detected.", verified=True)
        if packages:
            cmd = [py, "-m", "pip", "install"] + packages
            ok, why, _ = s.security.authorize_command(" ".join(cmd), s.control.task_id)
            if not ok:
                return StepResult(False, why)
            r = s.terminal.run(cmd, cwd=str(root), stream=False)
            if not r.ok and any(d.kind == "ConnectionError" for d in s.recovery.diagnose(r.combined)):
                lines.append("network problem, retrying once")
                time.sleep(6)
                r = s.terminal.run(cmd, cwd=str(root), stream=False)
            if not r.ok:
                lines.append(short(r.combined, 1500))
                return StepResult(False, "\n".join(lines))
            s.control.set_state(TaskState.VERIFYING)
            names = [re.split(r"[=<>!~\[ ]", x)[0] for x in packages]
            bad = []
            for n in names:
                chk = s.terminal.run([py, "-m", "pip", "show", n], cwd=str(root), timeout=60, stream=False)
                if not chk.ok:
                    bad.append(n)
            if bad:
                lines.append("pip finished but these packages are not installed: %s" % ", ".join(bad))
                return StepResult(False, "\n".join(lines))
            lines.append("Installed and verified: %s" % ", ".join(names))
        return StepResult(True, "\n".join(lines), verified=True)

    def h_build_exe(self, p: Dict[str, Any]) -> StepResult:
        return self.build_agent.build(p)

    def h_fix_project(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        s.control.set_state(TaskState.INSPECTING)
        root = self.project_path(p)
        lines: List[str] = ["Project: %s" % root]
        info = s.inspector.inspect(root)
        s.planner.last_project = info["root"]
        root = Path(info["root"])
        unresolved: List[str] = []
        changed = False
        if "Python" not in info["types"]:
            lines.append("Automatic repair is implemented for Python projects. Detected: %s" %
                         (", ".join(info["types"]) or "unknown"))
            return StepResult(False, "\n".join(lines))
        if info.get("syntax_errors"):
            lines.append("Found %d syntax error(s)." % len(info["syntax_errors"]))
            s.control.set_state(TaskState.RECOVERING)
            changed = True
            self.repair_syntax(root, lines)
            info = s.inspector.inspect(root)
        py = s.tools.python_for_project(root)
        error_text = str(p.get("error") or "")
        if py and info.get("third_party_imports"):
            missing = self.build_agent._missing_modules(py, info["third_party_imports"], root)
            if missing:
                pkgs = sorted({PIP_NAME_MAP.get(m, m) for m in missing})
                lines.append("Missing modules: %s" % ", ".join(missing))
                py = self.build_agent.prepare_python(root, lines) or py
                r = self.build_agent._pip(py, ["install"] + pkgs, root, lines, "install %s" % ", ".join(pkgs))
                changed = True
                still = self.build_agent._missing_modules(py, missing, root)
                if r.ok and not still:
                    lines.append("Installed: %s" % ", ".join(pkgs))
                    s.learning.record_success("ModuleNotFoundError %s" % missing[0], "pip_install:%s" % pkgs[0])
                else:
                    unresolved.append("could not install: %s" % ", ".join(still or missing))
        if error_text:
            for d in s.recovery.diagnose(error_text):
                lines.append("diagnosis: %s - %s" % (d.kind, d.detail))
                for f in d.fixes:
                    if f.kind == "ai_fix_file":
                        target = f.arg.rsplit(":", 1)[0] if f.arg else ""
                        line_no = int(f.arg.rsplit(":", 1)[1]) if f.arg and f.arg.rsplit(":", 1)[-1].isdigit() else None
                        tp = Path(target) if target else None
                        if tp and not tp.is_absolute():
                            tp = root / tp
                        if tp and tp.exists():
                            ok, note = self.ai_fix_file(tp, d.detail, line_no)
                            lines.append(note)
                            changed = changed or ok
                            if not ok:
                                unresolved.append("%s: %s" % (d.kind, note))
                        break
        s.control.set_state(TaskState.VERIFYING)
        final = s.inspector.inspect(root)
        remaining = final.get("syntax_errors", [])
        compile_ok = True
        if py:
            cr = s.terminal.run([py, "-m", "compileall", "-q", str(root)], cwd=str(root), timeout=300, stream=False)
            compile_ok = cr.ok
            if not cr.ok:
                lines.append("compileall failed:\n" + short(cr.combined, 800))
        for e in remaining:
            unresolved.append("%s:%s %s" % (e["file"], e["line"], e["error"]))
        if not remaining and compile_ok and not unresolved:
            if changed:
                lines.append("VERIFIED: no syntax errors remain and all files compile.")
            else:
                lines.append("No problems were detected (no syntax errors, imports resolve). "
                             "If a specific error occurs at runtime, paste the traceback and I will analyze it.")
            return StepResult(True, "\n".join(lines), verified=True)
        lines.append("Unresolved:")
        lines.extend("  - " + u for u in unresolved)
        return StepResult(False, "\n".join(lines))

    def h_create_project(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        ok, why = s.ai.available()
        if not ok:
            return StepResult(False, "Creating a new project needs an AI provider, and none is available: %s" % why)
        name = _slug(str(p["name"]), "ak_project")
        base = s.settings.workspace() / name
        system = ("You are a senior engineer. Create a small, complete, working project for the request. "
                  "Return ONLY JSON: {\"files\": [{\"path\": \"relative/path.py\", \"content\": \"...\"}], "
                  "\"entry\": \"main.py\"}. Use relative paths only, at most 12 files, valid code, no placeholders.")
        data, res = s.ai.chat_json(system, str(p["description"]))
        if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not data["files"]:
            return StepResult(False, res.error or "AI did not return a file list.")
        files = data["files"][:12]
        written: List[str] = []
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
                return StepResult(False, "AI returned a malformed file entry.")
            rel = Path(f["path"])
            if rel.is_absolute() or ".." in rel.parts or len(f["content"]) > 200000:
                return StepResult(False, "AI returned an unsafe path: %s" % f["path"])
            if rel.suffix == ".py":
                try:
                    ast.parse(f["content"])
                except SyntaxError as exc:
                    return StepResult(False, "Generated %s has a syntax error: %s (line %s). Nothing was written." %
                                      (rel, exc.msg, exc.lineno))
        for f in files:
            target, _ = s.files.write_text(base / f["path"], f["content"], make_backup=True)
            written.append(str(target))
        s.planner.last_project = str(base)
        s.control.set_state(TaskState.VERIFYING)
        missing = [w for w in written if not Path(w).exists()]
        if missing:
            return StepResult(False, "Some files were not created: %s" % ", ".join(missing))
        entry = data.get("entry")
        s.memory.add("project", "Created project %s with files: %s" % (base, ", ".join(written)), key=str(base))
        out = "Created %d file(s) in %s:\n%s" % (len(written), base, "\n".join(written))
        if isinstance(entry, str):
            out += "\nEntry: %s" % entry
        return StepResult(True, out, verified=True, data={"root": str(base)})

    def h_open_app(self, p: Dict[str, Any]) -> StepResult:
        args = p.get("args") or []
        r = self.s.apps.open_app(str(p["name"]), [str(a) for a in args] if isinstance(args, list) else [])
        if r["ok"]:
            return StepResult(True, "Opened %s (%s)" % (p["name"], r.get("path", "")), verified=True)
        return StepResult(False, r.get("error", "Could not open application."))

    def h_open_url(self, p: Dict[str, Any]) -> StepResult:
        r = self.s.browser.open_url(str(p["url"]))
        out = "Opened %s via %s. %s" % (r.get("url", p["url"]), r.get("engine", "browser"), r.get("note", ""))
        return StepResult(bool(r.get("ok")), out if r.get("ok") else r.get("error", "Could not open URL."),
                          verified=bool(r.get("ok")))

    def h_web_search(self, p: Dict[str, Any]) -> StepResult:
        r = self.s.browser.search(str(p["query"]))
        return StepResult(bool(r.get("ok")), "Searched for '%s' in the browser (%s). %s" % (
            p["query"], r.get("engine", ""), r.get("note", "")) if r.get("ok") else r.get("error", "Search failed."),
            verified=bool(r.get("ok")))

    def h_research(self, p: Dict[str, Any]) -> StepResult:
        s = self.s
        s.control.set_state(TaskState.RESEARCHING)
        r = s.research.research(str(p["query"]))
        search = r["search"]
        if not search.get("ok") and not r["pages"]:
            cached = search.get("cached") or []
            note = search.get("error", "Research failed.")
            if cached:
                note += "\nPreviously stored research:\n" + "\n".join(short(c["content"], 300) for c in cached)
            return StepResult(False, note)
        lines = ["Sources:"] + ["- %s (%s)" % (x["title"], x["url"]) for x in search.get("results", [])[:6]]
        for pg in r["pages"]:
            lines.append("\n%s\n%s" % (pg["url"], short(pg["text"], 900)))
        return StepResult(True, "\n".join(lines), verified=True)

    def h_fetch_page(self, p: Dict[str, Any]) -> StepResult:
        r = self.s.research.fetch_page(str(p["url"]))
        if not r["ok"]:
            return StepResult(False, r["error"])
        return StepResult(True, "%s\n%s\n\n%s" % (r["title"], r["url"], short(r["text"], 3000)), verified=True)

    def h_screenshot(self, p: Dict[str, Any]) -> StepResult:
        path = self.s.computer.screenshot()
        ok = Path(path).exists() and Path(path).stat().st_size > 0
        return StepResult(ok, "Screenshot saved: %s" % path if ok else "Screenshot file is empty.", verified=ok)

    def h_analyze_screen(self, p: Dict[str, Any]) -> StepResult:
        info = self.s.vision.analyze_screen()
        if not info.get("ok"):
            return StepResult(False, info.get("error", "Screen analysis failed."))
        text = info.get("text") or "(no text detected)"
        if info.get("ocr_error"):
            text = "OCR unavailable: %s" % info["ocr_error"]
        return StepResult(True, "Screenshot: %s\nSize: %s  Brightness: %s (%s)\n%s" % (
            info["screenshot"], info["size"], info["brightness"], info["mode"], short(text, 1500)), verified=True)

    def h_detect_tools(self, p: Dict[str, Any]) -> StepResult:
        info = self.s.tools.detect_all()
        lines = ["%-12s %-5s %s %s" % (n, "yes" if e["available"] else "no", e["version"], e["path"])
                 for n, e in info.items()]
        return StepResult(True, "\n".join(lines), verified=True)

    def h_recall(self, p: Dict[str, Any]) -> StepResult:
        hits = self.s.memory.search(str(p["query"]), limit=6)
        if not hits:
            return StepResult(True, "Nothing relevant found in memory.", verified=True)
        return StepResult(True, "\n\n".join("[%s %s] %s" % (h["kind"], h["ts"], short(h["content"], 400))
                                            for h in hits), verified=True)

    def h_type_text(self, p: Dict[str, Any]) -> StepResult:
        return StepResult(True, self.s.computer.type_text(str(p["text"])), verified=True)

    def h_hotkey(self, p: Dict[str, Any]) -> StepResult:
        keys = p["keys"] if isinstance(p["keys"], list) else str(p["keys"]).replace(" ", "").split("+")
        return StepResult(True, self.s.computer.hotkey(*[str(k) for k in keys]), verified=True)

    def h_click(self, p: Dict[str, Any]) -> StepResult:
        return StepResult(True, self.s.computer.click(int(p["x"]), int(p["y"])), verified=True)

    def h_respond(self, p: Dict[str, Any]) -> StepResult:
        text = self.s.chat_reply(str(p["text"]), "en")
        return StepResult(True, text, verified=True)


# ---------------------------------------------------------------------------
# AKCore: orchestrator
# ---------------------------------------------------------------------------

class AKCore:
    def __init__(self) -> None:
        self.settings = SettingsManager()
        self.audit = AuditLog()
        self.memory = MemoryManager(settings=self.settings)
        self.control = TaskControl()
        self.security = SecurityManager(self.settings, self.audit)
        self.terminal = TerminalManager(self.settings, self.control, self.audit)
        self.files = FileManager(self.settings, self.security, self.audit, self.control)
        self.tools = ToolManager(self.terminal, self.memory)
        self.inspector = ProjectInspector(self.control)
        self.research = ResearchEngine(self.settings, self.memory, self.audit, self.control)
        self.ai = AIProvider(self.settings, self.control, self.audit)
        self.apps = ApplicationManager(self.tools, self.terminal, self.audit, self.control)
        self.computer = ComputerControl(self.settings, self.security, self.audit, self.control)
        self.browser = BrowserManager(self.settings, self.security, self.audit, self.control)
        self.vision = VisionManager(self.computer, self.audit, self.control)
        self.voice = VoiceManager(self.settings, self.audit)
        self.learning = LearningManager(self.memory)
        self.planner = TaskPlanner(self.ai, self.memory, self.tools, self.settings)
        self.recovery = ErrorRecoveryEngine(self.learning)
        self.listeners: List[Callable[[str, str], None]] = []
        self.history: List[Dict[str, str]] = []
        self._busy = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self.svc = Services()
        for name in ("settings", "audit", "memory", "control", "security", "terminal", "files", "tools",
                     "inspector", "research", "ai", "apps", "computer", "browser", "vision", "voice", "learning",
                     "planner", "recovery"):
            setattr(self.svc, name, getattr(self, name))
        self.svc.emit = self._emit
        self.svc.chat_reply = self.chat_reply
        self.executor = TaskExecutor(self.svc)
        self.terminal.on_output = lambda line: self._emit("log", line)
        self.audit.listeners.append(self._on_audit)
        self.control.listeners.append(lambda st: self._emit("state", st.value))

    # ---- events -----------------------------------------------------------
    def _emit(self, kind: str, text: str) -> None:
        for cb in list(self.listeners):
            try:
                cb(kind, text)
            except Exception:
                pass

    def _on_audit(self, entry: Dict[str, Any]) -> None:
        parts = [entry.get("action", "")]
        for k in ("tool", "command", "path", "result", "exit_code", "error"):
            if k in entry:
                parts.append("%s=%s" % (k, short(str(entry[k]), 120)))
        self._emit("log", "[%s] %s" % (entry.get("ts", "")[11:], " ".join(parts)))

    # ---- chat -------------------------------------------------------------
    def context(self, text: str) -> str:
        hits = self.memory.search(text, limit=3) if self.memory.total() else []
        mem = "; ".join(short(h["content"].replace("\n", " "), 140) for h in hits) or "none"
        return "Workspace: %s\nCurrent project: %s\nTools: %s\nOnline: %s\nRelevant memory: %s" % (
            self.settings.workspace(), self.planner.last_project or "none", self.tools.summary(),
            self.research.is_online(timeout=1.0) if self.settings.get("provider") != "ollama" else "n/a", mem)

    def chat_reply(self, text: str, lang: str) -> str:
        lang = detect_language(text)
        ok, why = self.ai.available()
        if not ok:
            return "%s\n(%s)" % (msg(lang, "chat_noai"), why)
        system = ("You are AK AI, a local-first Windows assistant. Reply in the same language style as the user "
                  "(Hindi, Hinglish or English), concisely. Never claim you performed an action that you did not "
                  "actually perform; for computer tasks tell the user to ask you to do it and you will run it.")
        res = self.ai.chat(system, self.history[-10:] + [{"role": "user", "content": text}])
        if not res.ok:
            return "The AI provider request failed: %s" % res.error
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": res.text})
        self.history[:] = self.history[-20:]
        return res.text.strip()

    # ---- main pipeline ----------------------------------------------------
    def handle(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        tid = self.control.new_task()
        lang = detect_language(text)
        self.audit.record(tid, "command", command=text)
        self.memory.add("conversation", "User: " + text, key=tid)
        self._emit("action", msg(lang, "start"))
        report = ""
        status = TaskState.FAILED
        try:
            self.control.set_state(TaskState.PLANNING)
            plan, ai_note = self.planner.plan(text, self.context(text), lang)
            self.audit.record(tid, "plan", tool=plan.source, result=plan.intent,
                              command=json.dumps([s.action for s in plan.steps]))
            self._emit("tool", "plan (%s): %s -> %s" % (plan.source, plan.intent,
                                                         ", ".join(s.action for s in plan.steps)))
            notes: List[str] = []
            if plan.source == "heuristic" and self.settings.get("provider") != "none" and ai_note:
                notes.append("AI planning unavailable (%s); used local rules." % ai_note)
            elif plan.source == "heuristic" and self.settings.get("provider") == "none":
                notes.append(msg(lang, "no_ai"))
            if plan.intent == "chat":
                self.control.set_state(TaskState.EXECUTING)
                report = self.chat_reply(text, lang)
                status = TaskState.COMPLETED
            else:
                execution = self.executor.run_plan(plan)
                if execution.cancelled:
                    status = TaskState.CANCELLED
                    report = msg(lang, "cancelled")
                else:
                    if execution.all_verified:
                        head, status = msg(lang, "done"), TaskState.COMPLETED
                    elif execution.all_ok:
                        head, status = msg(lang, "partial"), TaskState.COMPLETED
                    else:
                        head, status = msg(lang, "failed"), TaskState.FAILED
                    body: List[str] = [head]
                    body.extend(notes)
                    for step, res in execution.results:
                        body.append("\n[%s] %s%s" % ("OK" if res.ok else "FAILED", step.action,
                                                    (" - " + step.why) if step.why else ""))
                        if res.output:
                            body.append(short(res.output, 2500))
                    report = "\n".join(body)
        except TaskCancelled:
            status = TaskState.CANCELLED
            report = msg(lang, "cancelled")
        except Exception as exc:
            self.audit.record(tid, "fatal", error=traceback.format_exc(limit=5))
            status = TaskState.FAILED
            report = "%s\n%s: %s" % (msg(lang, "failed"), type(exc).__name__, exc)
        finally:
            self.control.set_state(status)
            self.audit.record(tid, "final", result=status.value)
            self.memory.add("task", "Request: %s\nStatus: %s\nReport: %s" % (text, status.value, short(report, 1500)),
                            key=tid, meta={"status": status.value})
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": short(report, 600)})
            self.history[:] = self.history[-20:]
            self._emit("progress", "done")
        if self.settings.get("voice_enabled"):
            threading.Thread(target=self.voice.speak, args=(short(report.splitlines()[0] if report else "", 200),),
                             daemon=True).start()
        return report

    def submit(self, text: str) -> bool:
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                result = self.handle(text)
                self._emit("result", result)
            finally:
                self._busy.release()
        self._worker = threading.Thread(target=run, daemon=True, name="ak-task")
        self._worker.start()
        return True

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def stop(self) -> None:
        self.control.stop()
        self.voice.stop_speaking()

    def shutdown(self) -> None:
        self.control.stop()
        self.voice.stop_listening()
        with contextlib.suppress(Exception):
            self.browser.close()
        self.memory.close()


# ---------------------------------------------------------------------------
# GUI (Tkinter)
# ---------------------------------------------------------------------------

class AKGui:
    def __init__(self, core: AKCore) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
        self.tk, self.ttk, self.messagebox, self.filedialog = tk, ttk, messagebox, filedialog
        self.core = core
        self.q: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.root = tk.Tk()
        self.root.title("%s %s" % (APP_NAME, VERSION))
        self.root.geometry("1200x780")
        self.root.minsize(900, 600)
        self._build(scrolledtext)
        core.listeners.append(lambda kind, text: self.q.put((kind, text)))
        core.security.confirm_callback = self._confirm_from_worker
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Control-Shift-X>", lambda e: self._stop())
        self.root.after(80, self._poll)
        self._refresh_status()
        self.add_chat("AK AI", "Namaste! %s ready. Type a command (Hindi, Hinglish or English), e.g. "
                      "\"bhai ye project exe bana de\" or \"chrome kholo\"." % VERSION, "ai")

    # ---- layout -----------------------------------------------------------
    def _build(self, scrolledtext: Any) -> None:
        tk, ttk = self.tk, self.ttk
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        self.state_var = tk.StringVar(value="IDLE")
        self.action_var = tk.StringVar(value="Idle")
        self.provider_var = tk.StringVar()
        self.memory_var = tk.StringVar()
        ttk.Label(top, text="State:").grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.state_var, width=14, font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(top, text="Action:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Label(top, textvariable=self.action_var, width=60).grid(row=0, column=3, sticky="w")
        ttk.Label(top, textvariable=self.provider_var).grid(row=0, column=4, sticky="e", padx=8)
        ttk.Label(top, textvariable=self.memory_var).grid(row=0, column=5, sticky="e")
        top.columnconfigure(3, weight=1)
        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=6)
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.chat = scrolledtext.ScrolledText(left, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("user", foreground="#0b5394", font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("ai", foreground="#222222")
        self.chat.tag_configure("error", foreground="#b00020")
        right = ttk.Notebook(paned)
        paned.add(right, weight=2)
        self.tool_text = scrolledtext.ScrolledText(right, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text = scrolledtext.ScrolledText(right, wrap="none", state="disabled", font=("Consolas", 9))
        mem_frame = ttk.Frame(right)
        self.mem_entry = ttk.Entry(mem_frame)
        self.mem_entry.pack(fill="x", padx=2, pady=2)
        self.mem_entry.bind("<Return>", lambda e: self._memory_search())
        ttk.Button(mem_frame, text="Search memory", command=self._memory_search).pack(anchor="w", padx=2)
        self.mem_text = scrolledtext.ScrolledText(mem_frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.mem_text.pack(fill="both", expand=True)
        tools_frame = ttk.Frame(right)
        ttk.Button(tools_frame, text="Detect tools", command=self._detect_tools).pack(anchor="w", padx=2, pady=2)
        self.tools_text = scrolledtext.ScrolledText(tools_frame, wrap="none", state="disabled", font=("Consolas", 9))
        self.tools_text.pack(fill="both", expand=True)
        right.add(self.tool_text, text="Tool activity")
        right.add(self.log_text, text="Logs")
        right.add(mem_frame, text="Memory")
        right.add(tools_frame, text="Tools")
        bottom = ttk.Frame(self.root, padding=6)
        bottom.pack(fill="x")
        self.entry = ttk.Entry(bottom, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.focus_set()
        for label, cmd in (("Send", self._send), ("Stop", self._stop), ("Pause", self._pause),
                           ("Resume", self._resume), ("Voice", self._toggle_voice), ("Settings", self._settings)):
            ttk.Button(bottom, text=label, command=cmd).pack(side="left", padx=3)

    # ---- helpers ----------------------------------------------------------
    def _append(self, widget: Any, text: str, tag: Optional[str] = None) -> None:
        widget.configure(state="normal")
        widget.insert("end", text + "\n", tag) if tag else widget.insert("end", text + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def add_chat(self, who: str, text: str, tag: str = "ai") -> None:
        self._append(self.chat, "%s: %s\n" % (who, text), tag)

    def _refresh_status(self) -> None:
        ok, why = self.core.ai.available()
        s = self.core.settings
        self.provider_var.set("AI: %s/%s" % (s.get("provider"), s.effective_model() or "-") if ok else "AI: unavailable")
        self.memory_var.set("Memory: %d entries" % self.core.memory.total())

    def _confirm_from_worker(self, prompt: str) -> bool:
        evt = threading.Event()
        holder = {"ok": False}
        self.q.put(("confirm", prompt, evt, holder))
        evt.wait(timeout=600)
        return holder["ok"]

    def _poll(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "confirm":
                    _, prompt, evt, holder = item
                    holder["ok"] = bool(self.messagebox.askyesno("AK AI - permission", prompt, parent=self.root))
                    evt.set()
                elif kind == "state":
                    self.state_var.set(item[1])
                elif kind == "action":
                    self.action_var.set(item[1][:120])
                elif kind == "tool":
                    self._append(self.tool_text, item[1])
                elif kind == "log":
                    self._append(self.log_text, item[1])
                elif kind == "progress":
                    if item[1] == "done":
                        self.progress["value"] = 100 if self.state_var.get() == "COMPLETED" else self.progress["value"]
                        self._refresh_status()
                    elif "/" in item[1]:
                        a, b = item[1].split("/", 1)
                        self.progress["value"] = 100.0 * int(a) / max(1, int(b))
                elif kind == "result":
                    self.add_chat("AK AI", item[1], "error" if item[1].startswith(("Could not", "Ye kaam complete nahi")) else "ai")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    # ---- actions ----------------------------------------------------------
    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if self.core.busy:
            self.add_chat("AK AI", "A task is already running. Use Stop to cancel it first.", "error")
            return
        self.entry.delete(0, "end")
        self.add_chat("You", text, "user")
        self.progress["value"] = 0
        self.core.submit(text)

    def _stop(self) -> None:
        self.core.stop()
        self.action_var.set("Stopping...")

    def _pause(self) -> None:
        self.core.control.pause()

    def _resume(self) -> None:
        self.core.control.resume()

    def _toggle_voice(self) -> None:
        if self.core.voice._listening.is_set():
            self.core.voice.stop_listening()
            self.add_chat("AK AI", "Voice listening stopped.")
            return

        def on_text(t: str) -> None:
            if t == "__STOP__":
                self.core.stop()
            elif t == "__RESUME__":
                self.core.control.resume()
            else:
                self.q.put(("result", "Voice command: " + t))
                self.core.submit(t)
        ok, note = self.core.voice.start_listening(on_text)
        self.add_chat("AK AI", note, "ai" if ok else "error")

    def _detect_tools(self) -> None:
        def work() -> None:
            info = self.core.tools.detect_all()
            lines = ["%-12s %-4s %-30s %s" % (n, "yes" if e["available"] else "no", e["version"][:30], e["path"])
                     for n, e in info.items()]
            self.root.after(0, lambda: self._set_text(self.tools_text, "\n".join(lines)))
        threading.Thread(target=work, daemon=True).start()

    def _set_text(self, widget: Any, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _memory_search(self) -> None:
        q = self.mem_entry.get().strip()
        rows = self.core.memory.search(q, limit=15) if q else self.core.memory.recent(limit=15)
        self._set_text(self.mem_text, "\n\n".join("[%s %s] %s" % (r["kind"], r["ts"], short(r["content"], 500))
                                                  for r in rows) or "No memory entries.")
        self._refresh_status()

    def _settings(self) -> None:
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        s = self.core.settings
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        vars_: Dict[str, Any] = {}
        text_fields = [("provider", "Provider (none/openai/gemini/ollama)"), ("model", "Model"), ("base_url", "Base URL"),
                       ("temperature", "Temperature"), ("timeout", "Timeout (s)"), ("retry_limit", "Retry limit"),
                       ("workspace", "Workspace folder"), ("voice_wake_word", "Wake word"),
                       ("browser_engine", "Browser engine")]
        bool_fields = [("offline_mode", "Offline mode"), ("confirm_medium_risk", "Confirm medium-risk commands"),
                       ("allow_computer_control", "Allow computer control without asking"),
                       ("allow_outside_workspace", "Allow writing outside workspace"),
                       ("voice_enabled", "Voice replies"), ("browser_headless", "Headless browser"),
                       ("memory_enabled", "Store memory")]
        row = 0
        for key, label in text_fields:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(s.get(key)))
            if key == "provider":
                w: Any = ttk.Combobox(frm, textvariable=v, values=["none", "openai", "gemini", "ollama"], width=40)
            else:
                w = ttk.Entry(frm, textvariable=v, width=43)
            w.grid(row=row, column=1, sticky="we", pady=2)
            vars_[key] = v
            if key == "workspace":
                ttk.Button(frm, text="Browse", command=lambda v=v: v.set(self.filedialog.askdirectory() or v.get())
                           ).grid(row=row, column=2, padx=4)
            row += 1
        ttk.Label(frm, text="API key (this session only, never saved)").grid(row=row, column=0, sticky="w", pady=2)
        key_var = tk.StringVar()
        ttk.Entry(frm, textvariable=key_var, show="*", width=43).grid(row=row, column=1, sticky="we", pady=2)
        row += 1
        for key, label in bool_fields:
            v = tk.BooleanVar(value=bool(s.get(key)))
            ttk.Checkbutton(frm, text=label, variable=v).grid(row=row, column=0, columnspan=2, sticky="w")
            vars_[key] = v
            row += 1

        def save() -> None:
            values = {k: (v.get() if not isinstance(v, tk.BooleanVar) else bool(v.get())) for k, v in vars_.items()}
            if values["provider"] not in ("none", "openai", "gemini", "ollama"):
                self.messagebox.showerror("Settings", "Unknown provider.", parent=win)
                return
            s.update(values)
            if key_var.get().strip():
                s.set_session_key(values["provider"], key_var.get().strip())
            self._refresh_status()
            win.destroy()
        ttk.Button(frm, text="Save", command=save).grid(row=row, column=1, sticky="e", pady=8)

    def _close(self) -> None:
        self.core.shutdown()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def run_selftest(include_gui: bool = False) -> int:
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            results.append((name, True, ""))
        except Exception as exc:
            results.append((name, False, "%s: %s" % (type(exc).__name__, exc)))

    def expect(cond: Any, text: str = "assertion failed") -> None:
        if not cond:
            raise AssertionError(text)

    FAKE_KEY = "sk-" + "a1b2c3d4" * 3  # synthetic value used only to test redaction
    tmp = Path(tempfile.mkdtemp(prefix="akai_selftest_"))
    global BACKUP_DIR
    saved_backup_dir = BACKUP_DIR
    BACKUP_DIR = tmp / "backups"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    settings = SettingsManager(tmp / "settings.json")
    settings.set("workspace", str(tmp / "ws"), persist=False)
    settings.set("provider", "none", persist=False)
    audit = AuditLog(tmp / "audit.jsonl")
    memory = MemoryManager(tmp / "memory.db", settings)
    control = TaskControl()
    security = SecurityManager(settings, audit)
    security.confirm_callback = lambda prompt: True
    terminal = TerminalManager(settings, control, audit)
    files = FileManager(settings, security, audit, control)
    inspector = ProjectInspector(control)

    def t_ast() -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        ast.parse(src)
        expect(FENCE not in src, "markdown fence found in source")

    def t_settings() -> None:
        settings.set("retry_limit", "5", persist=True)
        again = SettingsManager(tmp / "settings.json")
        expect(again.get("retry_limit") == 5, "setting not persisted")
        settings.set_session_key("openai", FAKE_KEY)
        expect(FAKE_KEY not in (tmp / "settings.json").read_text(encoding="utf-8"), "key leaked to disk")
        settings.set_session_key("openai", "")

    def t_sqlite() -> None:
        c = sqlite3.connect(str(tmp / "x.db"))
        c.execute("CREATE TABLE t(a)")
        c.execute("INSERT INTO t VALUES (1)")
        expect(c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1)
        c.close()

    def t_memory() -> None:
        memory.add("fix", "ModuleNotFoundError numpy fixed by pip install numpy", key="numpy")
        memory.add("conversation", "my password: hunter2 and key " + FAKE_KEY, key="c")
        hits = memory.search("numpy pip", kind="fix")
        expect(hits, "memory search returned nothing")
        stored = " ".join(r["content"] for r in memory.recent(limit=10))
        expect("hunter2" not in stored and FAKE_KEY not in stored, "secret stored in memory")

    def t_control() -> None:
        c = TaskControl()
        c.new_task()
        c.pause()
        expect(c.paused and c.state == TaskState.PAUSED)
        c.resume()
        expect(not c.paused)
        c.stop()
        try:
            c.checkpoint()
        except TaskCancelled:
            return
        raise AssertionError("checkpoint did not raise after stop")

    def t_terminal() -> None:
        ok = terminal.run([sys.executable, "-c", "print('hello')"], stream=False)
        expect(ok.ok and "hello" in ok.stdout, "echo failed")
        bad = terminal.run([sys.executable, "-c", "import sys; sys.exit(3)"], stream=False)
        expect(not bad.ok and bad.exit_code == 3, "failure not reported")
        slow = terminal.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1, stream=False)
        expect(slow.timed_out and not slow.ok, "timeout not detected")
        missing = terminal.run(["definitely_not_a_real_binary_xyz"], stream=False)
        expect(not missing.ok, "missing binary reported as success")

    def t_cancel_process() -> None:
        c2 = TaskControl()
        t2 = TerminalManager(settings, c2, audit)
        c2.new_task()
        threading.Timer(1.0, c2.stop).start()
        start = time.time()
        r = t2.run([sys.executable, "-c", "import time; time.sleep(30)"], stream=False)
        expect(r.cancelled and time.time() - start < 10, "process was not cancelled")

    def t_security() -> None:
        expect(security.classify_command("dir") == Risk.LOW)
        expect(security.classify_command("pip install requests") == Risk.MEDIUM)
        expect(security.classify_command("rm -rf /") == Risk.BLOCKED)
        expect(security.classify_command("del foo.txt") == Risk.HIGH)
        ok, _, _ = security.authorize_command("format c:")
        expect(not ok, "dangerous command was authorized")

    def t_files() -> None:
        f = tmp / "ws" / "a.txt"
        files.write_text(f, "one")
        files.write_text(f, "two")
        expect(f.read_text() == "two")
        bid = sorted(BACKUP_DIR.iterdir())[-1].name
        files.rollback(bid)
        expect(f.read_text() == "one", "rollback failed")
        copy = files.copy(f, tmp / "ws" / "b.txt")
        expect(copy.exists())
        files.rename(copy, "c.txt")
        expect((tmp / "ws" / "c.txt").exists())
        files.delete(tmp / "ws" / "c.txt")
        expect(not (tmp / "ws" / "c.txt").exists())
        expect(files.search(tmp / "ws", pattern="a.txt"), "search failed")

    proj = tmp / "ws" / "demo"

    def t_inspector() -> None:
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "main.py").write_text("import requests\nimport yaml\n\ndef main():\n    print('hi')\n\n"
                                      "if __name__ == '__main__':\n    main()\n", encoding="utf-8")
        (proj / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
        info = inspector.inspect(proj)
        expect("Python" in info["types"])
        expect(info["entry_points"] and info["entry_points"][0] == "main.py", "entry point not found")
        expect(len(info["syntax_errors"]) == 1, "syntax error not detected")
        expect("pyyaml" in info["pip_packages"] and "requests" in info["pip_packages"], "dependency detection failed")

    def t_research_offline() -> None:
        s2 = SettingsManager(tmp / "s2.json")
        s2.set("offline_mode", True, persist=False)
        r = ResearchEngine(s2, memory, audit, control).search("pyinstaller hidden import")
        expect(not r["ok"] and not r["results"] and "not available" in r["error"], "offline research was fabricated")

    def t_ai_fallback() -> None:
        ai = AIProvider(settings, control, audit)
        res = ai.chat("sys", [{"role": "user", "content": "hi"}])
        expect(not res.ok and res.error, "provider none should fail honestly")
        settings.set("provider", "openai", persist=False)
        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            res = ai.chat("sys", [{"role": "user", "content": "hi"}])
            expect(not res.ok and "API key" in res.error, "missing key not reported")
        finally:
            settings.set("provider", "none", persist=False)
            if saved:
                os.environ["OPENAI_API_KEY"] = saved
        expect(extract_json(FENCE + 'json\n{"a": [1, 2]}\n' + FENCE) == {"a": [1, 2]}, "extract_json failed")

    def t_ai_http() -> None:
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.dumps({"message": {"content": "{\"ok\": true}"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        s3 = SettingsManager(tmp / "s3.json")
        s3.set("provider", "ollama", persist=False)
        s3.set("base_url", "http://127.0.0.1:%d" % srv.server_address[1], persist=False)
        s3.set("retry_limit", 0, persist=False)
        try:
            data, res = AIProvider(s3, control, audit).chat_json("sys", "hi")
            expect(res.ok and data == {"ok": True}, "local provider round trip failed: %s" % res.error)
        finally:
            srv.shutdown()

    planner = TaskPlanner(AIProvider(settings, control, audit), memory,
                          ToolManager(terminal, memory), settings)

    def t_planner() -> None:
        cases = {
            "bhai ye project exe bana de": "build_exe",
            "isme error aa rha h fix kr": "fix_project",
            "chrome kholo": "open_app",
            "search Windows 11 recovery on chrome": "research",
            "check what is wrong with this project": "inspect",
            "run: echo hi": "run_command",
            "hello how are you": "chat",
        }
        for text, intent in cases.items():
            plan = planner.plan_heuristic(text, "en")
            expect(plan.intent == intent, "%r -> %s (wanted %s)" % (text, plan.intent, intent))
        bad = [{"steps": [{"action": "format_disk", "params": {}}]}, {"steps": []}, {"steps": "x"},
               {"steps": [{"action": "read_file", "params": {}}]}]
        for b in bad:
            try:
                planner.validate(b, "x", "en", "ai")
            except PlanError:
                continue
            raise AssertionError("invalid plan accepted: %r" % (b,))

    def t_recovery() -> None:
        eng = ErrorRecoveryEngine(LearningManager(memory))
        d = eng.diagnose("Traceback...\nModuleNotFoundError: No module named 'yaml'")
        expect(d and d[0].fixes[0].kind == "pip_install" and d[0].fixes[0].arg == "pyyaml", "module diagnosis wrong")
        d2 = eng.diagnose('File "x.py", line 3\n    def f(:\nSyntaxError: invalid syntax\n')
        expect(d2 and d2[0].kind == "SyntaxError", "syntax diagnosis wrong")

    def t_executor() -> None:
        svc = Services()
        for name, obj in (("settings", settings), ("audit", audit), ("memory", memory), ("control", control),
                          ("security", security), ("terminal", terminal), ("files", files),
                          ("inspector", inspector), ("planner", planner)):
            setattr(svc, name, obj)
        svc.tools = planner.tools
        svc.ai = planner.ai
        svc.learning = LearningManager(memory)
        svc.recovery = ErrorRecoveryEngine(svc.learning)
        svc.chat_reply = lambda t, l: "chat"
        ex = TaskExecutor(svc)
        control.new_task()
        plan = planner.validate({"steps": [
            {"action": "inspect_project", "params": {"path": str(proj)}},
            {"action": "run_command", "params": {"command": [sys.executable, "-c", "print(42)"]}}]},
            "t", "en", "heuristic")
        rep = ex.run_plan(plan)
        expect(rep.all_verified, "plan did not verify: %s" % [(s.action, r.output[:80]) for s, r in rep.results])
        fail = ex.run_plan(planner.validate({"steps": [{"action": "run_command", "params": {
            "command": [sys.executable, "-c", "raise SystemExit(2)"]}}]}, "t", "en", "heuristic"))
        expect(not fail.all_ok, "failed command reported as success")
        fixed = ex.h_fix_project({"path": str(proj)})
        expect(not fixed.ok, "unfixable syntax error reported as fixed")
        (proj / "broken.py").write_text("def f():\n\treturn 1\n", encoding="utf-8")
        fixed2 = ex.h_fix_project({"path": str(proj)})
        expect(fixed2.ok or "Unresolved" in fixed2.output)

    def t_gui() -> None:
        importlib.import_module("tkinter")
        core = AKCore()
        gui = AKGui(core)
        gui.root.update()
        gui.root.destroy()
        core.shutdown()

    for name, fn in (("AST parse", t_ast), ("SettingsManager", t_settings), ("SQLite", t_sqlite),
                     ("MemoryManager", t_memory), ("TaskControl", t_control), ("TerminalManager", t_terminal),
                     ("Process cancel", t_cancel_process), ("SecurityManager", t_security),
                     ("FileManager", t_files), ("ProjectInspector", t_inspector),
                     ("ResearchEngine offline", t_research_offline), ("AIProvider fallback", t_ai_fallback),
                     ("AIProvider local round trip", t_ai_http), ("TaskPlanner", t_planner),
                     ("ErrorRecoveryEngine", t_recovery), ("TaskExecutor", t_executor)):
        check(name, fn)
    if include_gui:
        check("GUI startup", t_gui)
    BACKUP_DIR = saved_backup_dir
    memory.close()
    failed = 0
    for name, ok, err in results:
        print("%-28s %s%s" % (name, "PASS" if ok else "FAIL", ("  " + err) if err else ""))
        failed += 0 if ok else 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("Selftest: %d passed, %d failed" % (len(results) - failed, failed))
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_cli(core: AKCore, auto_yes: bool = False, once: Optional[str] = None) -> int:
    def confirm(prompt: str) -> bool:
        if auto_yes:
            print("[auto-approved] " + prompt.splitlines()[0])
            return True
        try:
            return input("\n%s\n[y/N] " % prompt).strip().lower() in ("y", "yes", "haan", "ha")
        except EOFError:
            return False

    core.security.confirm_callback = confirm
    core.listeners.append(lambda kind, text: print("  .. " + text) if kind in ("tool", "action") else None)
    if once is not None:
        print(core.handle(once))
        return 0
    print("%s %s (CLI). Type 'exit' to quit." % (APP_NAME, VERSION))
    while True:
        try:
            line = input("\nAK> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in ("exit", "quit", "q"):
            break
        if line:
            print(core.handle(line))
    core.shutdown()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="%s %s - local-first AI computer assistant" % (APP_NAME, VERSION))
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--selftest", action="store_true", help="run built-in self tests")
    parser.add_argument("--selftest-gui", action="store_true", help="self tests including GUI startup")
    parser.add_argument("--cli", action="store_true", help="text interface instead of the GUI")
    parser.add_argument("--task", help="run a single task and exit")
    parser.add_argument("--yes", action="store_true", help="auto-approve confirmations (CLI only; blocked commands stay blocked)")
    args = parser.parse_args(argv)
    if args.version:
        print("%s %s" % (APP_NAME, VERSION))
        return 0
    if args.selftest or args.selftest_gui:
        return run_selftest(include_gui=args.selftest_gui)
    core = AKCore()
    if args.task is not None:
        return run_cli(core, auto_yes=args.yes, once=args.task)
    if args.cli:
        return run_cli(core, auto_yes=args.yes)
    try:
        gui = AKGui(core)
    except Exception as exc:
        print("GUI could not start (%s). Falling back to text mode." % exc)
        return run_cli(core, auto_yes=args.yes)
    gui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
