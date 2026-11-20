"""Find a working Claude Code CLI on this machine and check that it is logged in.

Search order (first one that actually runs wins):
    1. CLAUDE_CLI_PATH from .env, if set
    2. native installer:   %USERPROFILE%\\.local\\bin\\claude.exe
    3. legacy native:      %LOCALAPPDATA%\\Programs\\claude\\claude.exe, %USERPROFILE%\\.claude\\local\\claude(.exe)
    4. whatever `claude` resolves to on PATH (shutil.which)
    5. npm shim:           %APPDATA%\\npm\\claude.cmd

Each candidate is probed with `claude --version` (no console window), so a broken install, such as an
npm package whose native binary never downloaded, is skipped instead of failing every matching
call. Login state comes from `claude auth status` (JSON). Only `loggedIn` and the plan name are kept;
the account e-mail is never stored or logged.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

INSTALL_HINT = "Install it with PowerShell: irm https://claude.ai/install.ps1 | iex   (or set CLAUDE_CLI_PATH in .env)"


@dataclass
class CliInfo:
    path: str = ""
    version: str = ""
    source: str = ""  # which search step found it
    logged_in: Optional[bool] = None  # None = unknown (older CLI without `auth status`)
    plan: str = ""
    skipped: List[str] = field(default_factory=list)  # candidates that exist but don't run
    checked_at: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.path)

    def login_command(self) -> str:
        return f'"{self.path}" auth login' if self.path else "claude auth login"

    def public(self) -> dict:
        return {"path": self.path, "version": self.version, "source": self.source,
                "logged_in": self.logged_in, "plan": self.plan, "skipped": self.skipped}


def _no_window() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # always the logged-in subscription, never API billing
    return env


def _run(args: List[str], timeout: float) -> Tuple[int, str, str]:
    p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, env=child_env(), **_no_window())
    return p.returncode, p.stdout.decode("utf-8", "replace").strip(), p.stderr.decode("utf-8", "replace").strip()


def candidates(configured: str) -> List[Tuple[str, str]]:
    """(path, source) in priority order, de-duplicated, only files that exist."""
    out: List[Tuple[str, str]] = []
    home = Path.home()
    conf = (configured or "").strip().strip('"')
    if conf and conf.lower() not in ("claude", "auto"):
        p = Path(os.path.expandvars(conf)).expanduser()
        out.append((str(p) if p.is_file() else (shutil.which(conf) or str(p)), "CLAUDE_CLI_PATH"))
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        out += [(str(home / ".local" / "bin" / "claude.exe"), "native installer"),
                (str(local / "Programs" / "claude" / "claude.exe"), "native (Programs)"),
                (str(home / ".claude" / "local" / "claude.exe"), "native (legacy)")]
        w = shutil.which("claude")
        if w:
            out.append((w, "PATH"))
        out.append((str(roaming / "npm" / "claude.cmd"), "npm global"))
    else:
        out += [(str(home / ".local" / "bin" / "claude"), "native installer"),
                (str(home / ".claude" / "local" / "claude"), "native (legacy)")]
        w = shutil.which("claude")
        if w:
            out.append((w, "PATH"))
    seen, uniq = set(), []
    for path, src in out:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not Path(path).is_file():
            continue
        seen.add(key)
        uniq.append((path, src))
    return uniq


def discover(configured: str, probe_timeout: float = 30.0) -> CliInfo:
    """Blocking (runs short subprocesses); call via asyncio.to_thread from async code."""
    info = CliInfo(checked_at=time.time())
    for path, src in candidates(configured):
        try:
            rc, out, err = _run([path, "--version"], probe_timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            info.skipped.append(f"{path} ({src}): {exc.__class__.__name__}: {exc}"[:200])
            continue
        text = out or err
        if rc != 0 or "not installed" in text.lower() or "claude" not in text.lower():
            info.skipped.append(f"{path} ({src}): {text.splitlines()[0][:120] if text else f'exit {rc}'}")
            continue
        info.path, info.source, info.version = path, src, text.splitlines()[0].strip()
        break
    if info.found:
        info.logged_in, info.plan = auth_status(info.path)
    return info


def auth_status(path: str, timeout: float = 30.0) -> Tuple[Optional[bool], str]:
    try:
        rc, out, _ = _run([path, "auth", "status"], timeout)
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""
    if "loggedIn" not in data:
        return None, ""
    return bool(data.get("loggedIn")), str(data.get("subscriptionType") or data.get("planTier") or "")
