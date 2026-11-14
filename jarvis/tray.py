"""Windows system tray icon using Shell_NotifyIconW via ctypes (no pystray).

Falls back gracefully when running headless / in CI (no interactive desktop session).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_LEFTBUTTON = 0x0000
TPM_RETURNCMD = 0x0100
MF_CHECKED = 0x00000008
IDM_BASE = 2000

ICON_SIZE = 32
_HEADLESS = False

# ---------------------------------------------------------------------------
# Check whether we have a usable desktop session
# ---------------------------------------------------------------------------
def _is_interactive() -> bool:
    """Return True if we have an interactive Windows desktop session."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # Simple heuristic: try to get a window station handle
        ws = ctypes.windll.user32.GetProcessWindowStation()
        return ws != 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NOTIFYICONDATA structure (Unicode, Shell_NotifyIconW)
# ---------------------------------------------------------------------------
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("hWnd", ctypes.wintypes.HWND),
        ("uID", ctypes.wintypes.UINT),
        ("uFlags", ctypes.wintypes.UINT),
        ("uCallbackMessage", ctypes.wintypes.UINT),
        ("hIcon", ctypes.wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.wintypes.DWORD),
        ("dwStateMask", ctypes.wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", ctypes.wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# WNDCLASSEX / MSG needed for a hidden message-only window
# ---------------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT is pointer-sized on 64-bit Windows
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HICON),
        ("hCursor", ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm", ctypes.wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", ctypes.wintypes.HWND),
        ("message", ctypes.wintypes.UINT),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD),
        ("pt", ctypes.wintypes.POINT),
    ]


def _declare_prototypes() -> None:
    """64-bit Windows needs explicit signatures: ctypes would otherwise truncate HWND/LRESULT to 32-bit ints (HWND_MESSAGE = -3 included)."""
    if sys.platform != "win32":
        return
    w = ctypes.wintypes
    u32, s32 = ctypes.windll.user32, ctypes.windll.shell32
    u32.CreateWindowExW.restype = w.HWND
    u32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    u32.DefWindowProcW.restype = ctypes.c_ssize_t
    u32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    u32.RegisterClassExW.argtypes = [ctypes.c_void_p]
    u32.GetMessageW.argtypes = [ctypes.c_void_p, w.HWND, w.UINT, w.UINT]
    u32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    u32.DestroyWindow.argtypes = [w.HWND]
    u32.CreatePopupMenu.restype = w.HMENU
    u32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    u32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p]
    u32.DestroyMenu.argtypes = [w.HMENU]
    u32.SetForegroundWindow.argtypes = [w.HWND]
    s32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.c_void_p]
    ctypes.windll.kernel32.GetModuleHandleW.restype = w.HINSTANCE


_declare_prototypes()


# ---------------------------------------------------------------------------
# Icon generation via Pillow
# ---------------------------------------------------------------------------
def _make_icon_handle() -> ctypes.wintypes.HICON | None:
    """Generate a simple JARVIS icon in memory via Pillow → HICON."""
    try:
        from PIL import Image, ImageDraw
        import io

        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Draw a teal circle with "J" label
        draw.ellipse([2, 2, ICON_SIZE - 2, ICON_SIZE - 2], fill=(0, 180, 180, 255))
        draw.text((10, 8), "J", fill=(255, 255, 255, 255))

        # Convert PIL image → HICON via CreateIconFromResource
        with io.BytesIO() as buf:
            img.save(buf, format="ICO")
            ico_data = buf.getvalue()

        # Use CreateIconFromResourceEx if available; fall back to None
        user32 = ctypes.windll.user32
        hicon = user32.CreateIconFromResourceEx(
            ico_data,
            len(ico_data),
            True,  # fIcon
            0x00030000,  # dwVer
            ICON_SIZE,
            ICON_SIZE,
            0,  # LR_DEFAULTCOLOR
        )
        return hicon if hicon else None
    except Exception as exc:
        logger.debug("Icon generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JarvisTray
# ---------------------------------------------------------------------------
class JarvisTray:
    """Windows system tray icon using Shell_NotifyIconW via ctypes (no pystray)."""

    def __init__(self, menu_provider: Callable[[], list["MenuItem"]], tooltip: str = "JARVIS"):
        self._menu_provider = menu_provider
        self._tooltip = tooltip
        self._callbacks: dict[int, Callable[[], object]] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._hwnd: ctypes.wintypes.HWND | None = None
        self._thread_id: int = 0
        self._nid: NOTIFYICONDATAW | None = None
        self._hicon: ctypes.wintypes.HICON | None = None
        self._headless = not _is_interactive()
        if self._headless:
            logger.info("JarvisTray: headless/CI environment detected — tray disabled")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start Win32 message loop in a background thread."""
        if self._headless or self._running:
            return
        self._thread = threading.Thread(target=self._message_loop, daemon=True, name="jarvis-tray")
        self._thread.start()

    def stop(self) -> None:
        """Remove tray icon and stop the message pump."""
        if self._headless or not self._running:
            return
        self._running = False
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def show_notification(self, title: str, message: str, timeout_ms: int = 3000) -> None:
        """Show a balloon tooltip notification."""
        if self._headless or self._nid is None:
            logger.debug("show_notification(%r): headless or not started", title)
            return
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfo = message[:255]
            nid.szInfoTitle = title[:63]
            nid.uTimeout = timeout_ms
            nid.dwInfoFlags = NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as exc:
            logger.debug("show_notification failed: %s", exc)

    def update_tooltip(self, text: str) -> None:
        """Update hover tooltip (max 128 chars)."""
        if self._headless or self._nid is None:
            return
        try:
            self._nid.szTip = text[:127]
            self._nid.uFlags = NIF_TIP
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception as exc:
            logger.debug("update_tooltip failed: %s", exc)

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Internal Win32 implementation
    # ------------------------------------------------------------------
    def _message_loop(self) -> None:
        """Win32 message loop — runs in its own thread."""
        try:
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            hwnd = self._create_window()
            if hwnd is None:
                logger.warning("JarvisTray: failed to create hidden window")
                return
            self._hwnd = hwnd
            self._hicon = _make_icon_handle()
            self._add_tray_icon()
            self._running = True

            msg = MSG()
            while self._running:
                ret = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            logger.error("JarvisTray message loop error: %s", exc)
        finally:
            self._running = False
            self._remove_tray_icon()

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        """Window procedure for the hidden message-only window."""
        try:
            if msg == WM_TRAY:
                if lparam in (WM_RBUTTONUP, WM_LBUTTONDBLCLK):
                    self._show_context_menu(hwnd)
            elif msg == WM_COMMAND:
                cb = self._callbacks.get(wparam & 0xFFFF)
                if cb:
                    threading.Thread(target=self._safe_call, args=(cb,), daemon=True, name="tray-action").start()
            elif msg == WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
        except Exception as exc:
            logger.debug("_wnd_proc error: %s", exc)
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self) -> ctypes.wintypes.HWND | None:
        """Register window class and create a message-only hidden window."""
        try:
            user32 = ctypes.windll.user32
            hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            class_name = "JarvisTrayWnd"
            self._wnd_proc_ref = WNDPROC(self._wnd_proc)

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wnd_proc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = class_name

            # Ignore error if class already registered
            user32.RegisterClassExW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                "JARVIS Tray",
                0,
                0, 0, 0, 0,
                -3,  # HWND_MESSAGE
                None,
                hinstance,
                None,
            )
            return hwnd or None
        except Exception as exc:
            logger.debug("_create_window failed: %s", exc)
            return None

    def _add_tray_icon(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon or ctypes.windll.user32.LoadIconW(None, 32512)  # IDI_APPLICATION
        nid.szTip = self._tooltip[:127]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid

    def _remove_tray_icon(self) -> None:
        if self._nid is not None:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None

    @staticmethod
    def _safe_call(cb: Callable[[], object]) -> None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            logger.exception("tray action failed")

    def _show_context_menu(self, hwnd) -> None:
        try:
            user32 = ctypes.windll.user32
            hmenu = user32.CreatePopupMenu()
            self._callbacks = {}
            for i, item in enumerate(self._menu_provider()):
                if item.separator:
                    user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                    continue
                cid = IDM_BASE + i
                self._callbacks[cid] = item.callback
                user32.AppendMenuW(hmenu, MF_STRING | (MF_CHECKED if item.checked else 0), cid, item.label)
            pt = ctypes.wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(hmenu, TPM_LEFTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(hmenu)
            if cmd:
                user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)
        except Exception as exc:
            logger.debug("_show_context_menu failed: %s", exc)


# ---------------------------------------------------------------------------
# Menu model + the actions that operate the real application
# ---------------------------------------------------------------------------
from dataclasses import dataclass  # noqa: E402


@dataclass
class MenuItem:
    label: str = ""
    callback: Callable[[], object] = lambda: None
    checked: bool = False
    separator: bool = False


class TrayActions:
    """Menu actions backed by the running JARVIS HTTP API (loopback) - the same lifecycle the dashboard controls.

    `api(method, path, json=None) -> dict` is injectable so the logic is testable without a desktop session.
    """

    def __init__(self, base_url: str, logs_dir, api: Callable | None = None, open_url: Callable[[str], object] | None = None,
                 open_path: Callable[[str], object] | None = None, on_quit: Callable[[], None] | None = None):
        self.base = base_url.rstrip("/")
        self.logs_dir = logs_dir
        self._api = api or self._http
        self._open_url = open_url or self._default_open_url
        self._open_path = open_path or self._default_open_path
        self._on_quit = on_quit or (lambda: None)
        self.muted = False

    # -- plumbing
    def _http(self, method: str, path: str, json_body: dict | None = None) -> dict:
        import json as _json
        import urllib.request

        data = _json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"X-Jarvis": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:  # loopback only (Settings.host is validated as loopback)
            return _json.loads(r.read() or b"{}")

    @staticmethod
    def _default_open_url(url: str) -> None:
        import webbrowser

        webbrowser.open(url)

    @staticmethod
    def _default_open_path(path: str) -> None:
        os.startfile(path)  # Windows only

    # -- actions
    def start(self) -> dict:
        return self._api("POST", "/api/voice/mic", {"enabled": True})

    def stop(self) -> dict:
        return self._api("POST", "/api/voice/mic", {"enabled": False})

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        self._api("POST", "/api/voice/session", {"action": "pause" if self.muted else "resume"})
        return self.muted

    def open_dashboard(self) -> None:
        self._open_url(self.base + "/")

    def open_logs(self) -> None:
        self._open_path(str(self.logs_dir))

    def toggle_dictation(self) -> bool:
        cur = self._api("GET", "/api/v2/dictation/status").get("enabled", False)
        self._api("PUT", "/api/v2/settings", {"dictation": {"enabled": not cur}})
        return not cur

    def quit(self) -> None:
        self._on_quit()

    def menu(self) -> list[MenuItem]:
        dict_on = False
        try:
            dict_on = bool(self._api("GET", "/api/v2/dictation/status").get("enabled"))
        except Exception:  # noqa: BLE001
            pass
        return [
            MenuItem("Open dashboard", self.open_dashboard),
            MenuItem(separator=True),
            MenuItem("Start JARVIS (microphone on)", self.start),
            MenuItem("Stop JARVIS (microphone off)", self.stop),
            MenuItem("Mute microphone", self.toggle_mute, checked=self.muted),
            MenuItem("Dictation hotkey", self.toggle_dictation, checked=dict_on),
            MenuItem(separator=True),
            MenuItem("Open logs folder", self.open_logs),
            MenuItem("Quit JARVIS", self.quit),
        ]
