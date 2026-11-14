"""read_screen: local screenshot + local OCR, opt-in, approval on first use, sensitive windows blocked, every capture audited.

* Off by default (`Settings.screen.enabled`). The first capture ever needs an owner approval through the shared ApprovalService.
* The image lives only in memory: it is OCR'd by the local Tesseract binary (no network, no cloud) and discarded.
* Capture is refused while ANY visible top-level window (not just the focused one) matches a blocked title/process, or when the focused
  native control is a password edit box. Browser password fields cannot be detected from Win32: they are covered by the window-title
  heuristic ("sign in", "log in", "password", banks, password managers...), which is a best effort, not a guarantee.
* Output is secret/e-mail redacted and wrapped as untrusted before it can reach the model or memory.
* No loop, no timer: one capture per explicit call, rate-limited.
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import audit
from .config import ScreenSpec
from .security.pii_firewall import redact_secrets
from .security.untrusted import UNTRUSTED_DATA_POLICY, wrap_untrusted

log = logging.getLogger("jarvis.screen")

BLOCKED_PROCESSES = frozenset(
    "keepass.exe keepassxc.exe 1password.exe bitwarden.exe lastpass.exe dashlane.exe keeper.exe enpass.exe nordpass.exe authy.exe".split()
)
MIN_INTERVAL_S = 2.0
MAX_PER_MINUTE = 6
TESSERACT_DEFAULT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"


@dataclass
class WindowInfo:
    title: str
    process: str = ""


def list_visible_windows() -> list[WindowInfo]:
    """Visible, non-minimised, titled top-level windows (Windows only; empty elsewhere)."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes as wt

    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    out: list[WindowInfo] = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            sz = wt.DWORD(520)
            pbuf = ctypes.create_unicode_buffer(520)
            if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(sz)):
                proc = Path(pbuf.value).name.lower()
            k32.CloseHandle(h)
        out.append(WindowInfo(buf.value, proc))
        return True

    u32.EnumWindows(proto(cb), 0)
    return out


def focused_is_password_box() -> bool:
    """True when the focused *native* edit control has ES_PASSWORD (browser/Electron fields are invisible to this check)."""
    if sys.platform != "win32":
        return False
    from ctypes import wintypes as wt

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND),
                    ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]

    u32 = ctypes.windll.user32
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not u32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
        return False
    ES_PASSWORD, GWL_STYLE = 0x20, -16
    return bool(u32.GetWindowLongW(info.hwndFocus, GWL_STYLE) & ES_PASSWORD)


def blocked_reason(spec: ScreenSpec, windows: list[WindowInfo], password_focus: bool = False) -> str | None:
    """Why capture must be refused, WITHOUT echoing the window title (it may itself be sensitive)."""
    if password_focus:
        return "a password field has focus"
    pats = [p.strip().lower() for p in spec.blocked_window_titles if p.strip()]
    for w in windows:
        t = w.title.lower()
        if w.process in BLOCKED_PROCESSES:
            return f"a protected application is visible ({w.process})"
        for p in pats:
            if p in t:
                return f"a visible window matches the blocked pattern '{p}'"
    return None


def find_tesseract() -> str | None:
    return shutil.which("tesseract") or (str(TESSERACT_DEFAULT) if TESSERACT_DEFAULT.exists() else None)


def tesseract_ocr(img, exe: str | None = None, timeout: float = 30.0) -> str:
    """Local OCR via the Tesseract binary (stdin->stdout, no temp files, no network)."""
    exe = exe or find_tesseract()
    if not exe:
        raise RuntimeError("no local OCR engine: install Tesseract OCR (https://github.com/UB-Mannheim/tesseract/wiki) and restart JARVIS")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = subprocess.run([exe, "stdin", "stdout", "-l", "eng"], input=buf.getvalue(), capture_output=True, timeout=timeout, check=False)
    if p.returncode != 0:
        raise RuntimeError("OCR failed: " + p.stderr.decode("utf-8", "replace")[:200])
    return p.stdout.decode("utf-8", "replace")


def default_grab():
    from PIL import ImageGrab

    return ImageGrab.grab()


class ScreenReader:
    def __init__(
        self,
        settings,
        approvals=None,
        *,
        audit_file: Path | None = None,
        grab: Callable | None = None,
        ocr: Callable | None = None,
        windows: Callable[[], list[WindowInfo]] | None = None,
        password_focus: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings, self.approvals, self.audit_file = settings, approvals, audit_file
        self._grab, self._ocr = grab or default_grab, ocr or tesseract_ocr
        self._windows, self._pw = windows or list_visible_windows, password_focus or focused_is_password_box
        self._clock = clock
        self._times: list[float] = []

    @property
    def spec(self) -> ScreenSpec:
        return self.settings.screen

    def guard_reason(self) -> str | None:
        """Shared with the legacy `take_screenshot` action so both paths honour the same blocklist."""
        return blocked_reason(self.spec, self._windows(), self._pw())

    def _audit(self, allowed: bool, reason: str, chars: int = 0, source: str = "llm") -> None:
        audit.record_external("screen_capture", "read_screen", "local:screen", allowed, reason=f"{reason} source={source} chars={chars}", audit_file=self.audit_file)

    async def read_screen(self, *, source: str = "llm", task_id: str | None = None) -> dict:
        if not self.spec.enabled:
            self._audit(False, "denied: screen reading is disabled", source=source)
            return {"ok": False, "error": "Screen reading is disabled. The owner can enable it in Settings → Screen."}
        if not self.spec.first_use_approved:
            if self.approvals is None:
                self._audit(False, "denied: no approval service", source=source)
                return {"ok": False, "error": "approval service unavailable"}
            req = await asyncio.to_thread(
                self.approvals.create_request,
                action_type="screen_first_use",
                target_resource="read_screen",
                preview_diff="Allow JARVIS to take a screenshot and read it with local OCR when you ask it to. Nothing is saved or uploaded; password managers, banking and sign-in windows are blocked.",
                args={"source": source, "task_id": task_id},
            )
            self._audit(False, "approval_required: first use", source=source)
            return {"ok": False, "status": "approval_required", "approval_token": req["token"], "message": "First use needs your approval; ask again after approving."}
        now = self._clock()
        self._times = [t for t in self._times if now - t < 60]
        if (self._times and now - self._times[-1] < MIN_INTERVAL_S) or len(self._times) >= MAX_PER_MINUTE:
            self._audit(False, "denied: rate limit (no continuous capture)", source=source)
            return {"ok": False, "error": "Screen capture is rate limited; try again in a few seconds."}
        why = await asyncio.to_thread(self.guard_reason)
        if why:
            self._audit(False, f"blocked: {why}", source=source)
            return {"ok": False, "blocked": True, "error": f"Not reading the screen: {why}. Close or minimise it and ask again."}
        self._times.append(now)
        try:
            img = await asyncio.to_thread(self._grab)
            text = await asyncio.to_thread(self._ocr, img)
        except Exception as e:  # noqa: BLE001
            self._audit(True, f"error: {type(e).__name__}", source=source)
            return {"ok": False, "error": str(e)[:240]}
        finally:
            img = None  # never persisted
        clean, _ = redact_secrets(text or "", mask_email=True)
        self._audit(True, "captured", chars=len(clean), source=source)
        return {"ok": True, "policy": UNTRUSTED_DATA_POLICY, "text": wrap_untrusted(clean, source_type="screen_ocr", source_id="read_screen", max_chars=self.spec.max_chars)}

    # called by the shared approval endpoint after the owner approves a `screen_first_use` request
    def first_use_token_ok(self, row: dict) -> bool:
        return row.get("action_type") == "screen_first_use" and row.get("status") == "approved"


def screen_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "read_screen",
            "description": "Take a screenshot of the user's screen and read its visible text with local OCR. Use only when the user asks about what is on their screen.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
