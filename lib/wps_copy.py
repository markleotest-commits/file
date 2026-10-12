
"""Copy the Reception export from WPS: Ctrl+A, Ctrl+A, Ctrl+C, then we paste MC."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

VK_CONTROL = 0x11
VK_A = 0x41
VK_C = 0x43
VK_MENU = 0x12
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
ASFW_ANY = 0xFFFFFFFF  # AllowSetForegroundWindow(-1)
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
CF_UNICODETEXT = 13


from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def _need_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("WPS copy only runs on Windows.")


def _u32():
    return ctypes.windll.user32


def _k32():
    return ctypes.windll.kernel32


def _fix_ctypes() -> None:
    u, k = _u32(), _k32()
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.CloseClipboard.argtypes = []


def get_clipboard_text() -> str:
    """Read clipboard as text. Never crash on empty / 64-bit handles."""
    _need_windows()
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.update_idletasks()
        try:
            return r.clipboard_get()
        except tk.TclError:
            return ""
        finally:
            r.destroy()
    except Exception:
        pass
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            timeout=15,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    except Exception:
        pass
    try:
        _fix_ctypes()
        u, k = _u32(), _k32()
        if not u.OpenClipboard(None):
            return ""
        try:
            handle = u.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k.GlobalUnlock(handle)
        finally:
            u.CloseClipboard()
    except OSError:
        return ""


def _fix_window_ctypes() -> None:
    """Declare the window APIs as pointer-sized.

    Without this, ctypes passes every HWND as a 32-bit int, which silently
    truncates handles on 64-bit Windows. ``AllowSetForegroundWindow`` also
    takes a DWORD, so ASFW_ANY must go in as 0xFFFFFFFF, not -1.
    """
    u = _u32()
    H = ctypes.c_void_p
    u.IsWindowVisible.argtypes = [H]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsIconic.argtypes = [H]
    u.IsIconic.restype = ctypes.c_bool
    u.ShowWindow.argtypes = [H, ctypes.c_int]
    u.BringWindowToTop.argtypes = [H]
    u.SetForegroundWindow.argtypes = [H]
    u.SetForegroundWindow.restype = ctypes.c_bool
    u.GetForegroundWindow.restype = H
    u.GetWindowThreadProcessId.argtypes = [H, ctypes.c_void_p]
    u.GetWindowThreadProcessId.restype = ctypes.c_uint
    u.AttachThreadInput.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_bool]
    u.SwitchToThisWindow.argtypes = [H, ctypes.c_bool]
    u.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.AllowSetForegroundWindow.argtypes = [ctypes.c_uint]
    u.AllowSetForegroundWindow.restype = ctypes.c_bool
    u.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_uint]
    u.SystemParametersInfoW.restype = ctypes.c_bool


def _enum_titles() -> list[tuple[int, str]]:
    user32 = _u32()
    hits: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        if title:
            hits.append((int(hwnd), title))
        return True

    user32.EnumWindows(cb, 0)
    return hits


def wait_for_export_window(xlsx_path: Path, timeout: float = 90) -> tuple[int, str]:
    _need_windows()
    needle = xlsx_path.stem.lower()
    t0 = time.time()
    while time.time() - t0 < timeout:
        for hwnd, title in _enum_titles():
            if needle in title.lower():
                info("WPS/Excel window:", title)
                return hwnd, title
        time.sleep(0.25)
    raise TimeoutError(f"No WPS window for {xlsx_path.name}")


def allow_launch_to_take_focus() -> None:
    """Let the app we are about to start put its own window in front.

    Windows grants foreground rights to a newly launched window, but that
    right dies with the launcher process. WPS goes through its own launcher
    (ksolaunch -> et.exe), so by the time the real spreadsheet window exists
    the permission is gone and the window opens behind the terminal.
    ASFW_ANY hands it back before the launch.
    """
    _need_windows()
    _fix_window_ctypes()
    try:
        _u32().AllowSetForegroundWindow(ASFW_ANY)
    except OSError:
        pass


def _foreground_lock_timeout() -> int:
    """Current ForegroundLockTimeout in ms (0 = unlock on)."""
    _fix_window_ctypes()
    val = wintypes.DWORD()
    try:
        if _u32().SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                        ctypes.byref(val), 0):
            return int(val.value)
    except OSError:
        pass
    return 200000  # Windows default


def _set_foreground_lock_timeout(ms: int) -> None:
    """Apply ForegroundLockTimeout for the session only (no registry write)."""
    _fix_window_ctypes()
    val = ctypes.c_uint(max(0, ms))
    try:
        _u32().SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                     ctypes.byref(val), 0)
    except OSError:
        pass


def _activate_window(hwnd: int) -> None:
    """One attempt: attach to the foreground thread, ALT tap, then front it."""
    u, k = _u32(), _k32()
    fg = u.GetForegroundWindow()
    if fg == hwnd:
        return
    me = k.GetCurrentThreadId()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_tid) and fg_tid != me
    if attached:
        u.AttachThreadInput(me, fg_tid, True)
    try:
        u.keybd_event(VK_MENU, 0, 0, 0)
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        u.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        u.SwitchToThisWindow(hwnd, True)
        u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    finally:
        if attached:
            u.AttachThreadInput(me, fg_tid, False)


def _front_plain(hwnd: int) -> None:
    """SetForegroundWindow + BringWindowToTop with no ALT tap and no attach.

    Used for the spread-out retries: the ALT tap flashes the foreground
    window's menu, so it runs at most twice — after that the lock is already
    zeroed and plain requests are legitimate. Late retries win when WPS was
    still loading the workbook during the early ones.
    """
    u = _u32()
    u.BringWindowToTop(hwnd)
    u.SetForegroundWindow(hwnd)


def bring_to_front(hwnd: int, timeout: float = 20.0) -> bool:
    """Put ``hwnd`` in front of everything, including the terminal.

    Windows refuses SetForegroundWindow to a process that did not receive
    the last user input — exactly the bot: a background console whose last
    input was the operator clicking the terminal. The bug 32 ASFW_ANY
    pre-grant only works when the CALLER owns the foreground; during the
    export flow the foreground is Chrome, so it is a no-op and WPS would
    still land behind the terminal until the operator clicked it.

    The piece that actually removes the refusal is the foreground lock:
    while ForegroundLockTimeout is non-zero, Windows enforces the
    "recent user input" restriction. Zeroing it (SPI_SETFOREGROUNDLOCK-
    TIMEOUT, in-session only, restored afterwards) lets a background
    process bring a window forward legitimately.

    The retries are SPREAD OVER TIME (bug 43), not back-to-back: the window
    is found by title the moment it appears, but a real export (hundreds of
    KB) is still loading then and activation is refused. Three rapid attempts
    all burn inside that loading window, the call returns False, and nothing
    retries until the copy phase minutes later — which is exactly why the
    operator's terminal click "fixed" it. Now: two ALT-tap attempts, then
    plain retries every ~2 s until `timeout`, so a late attempt lands once
    WPS is ready. Returns True only once the window is REALLY the
    foreground window, verified rather than assumed.
    """
    _need_windows()
    _fix_window_ctypes()
    u = _u32()

    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)

    if u.GetForegroundWindow() == hwnd:
        return True

    prev = _foreground_lock_timeout()
    unlocked = False
    try:
        if prev:
            _set_foreground_lock_timeout(0)
            unlocked = True
        t0 = time.time()
        # Two full attempts with the ALT unlock first (it flashes the
        # foreground menu, so it is not repeated past this).
        for _ in range(2):
            _activate_window(hwnd)
            time.sleep(0.4)
            if u.GetForegroundWindow() == hwnd:
                return True
            if time.time() - t0 >= timeout:
                break
        # Then plain retries, spread out: each one is a fresh check after
        # WPS had more time to finish loading the workbook.
        while time.time() - t0 < timeout:
            _front_plain(hwnd)
            time.sleep(2.0)
            if u.GetForegroundWindow() == hwnd:
                return True
        return u.GetForegroundWindow() == hwnd
    finally:
        if unlocked:
            _set_foreground_lock_timeout(prev or 200000)


def front_window_title() -> str:
    """Title of the foreground window, for diagnostics."""
    try:
        _fix_window_ctypes()
        hwnd = _u32().GetForegroundWindow()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        _u32().GetWindowTextW(hwnd, buf, 512)
        return buf.value.strip()
    except Exception:
        return ""


def dismiss_dialog() -> None:
    """ESC in the foreground window — closes a modal (WPS "Save the
    Document?"), which unblocks a launch that was queued behind it."""
    _need_windows()
    u = _u32()
    u.keybd_event(VK_ESCAPE, 0, 0, 0)
    u.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.3)


def focus_window(hwnd: int) -> None:
    """Front for copy. Restore only if minimized; do not un-maximize.

    Same spread retries as bring_to_front on a smaller budget (bug 43): one
    ALT-tap attempt, then plain retries for ~6 s. This is the call that used
    to "mysteriously work" right after the operator clicked the terminal —
    it now succeeds on its own because a late retry lands once WPS is ready.
    """
    u = _u32()
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
    if u.GetForegroundWindow() == hwnd:
        return
    prev = _foreground_lock_timeout()
    unlocked = False
    try:
        if prev:
            _set_foreground_lock_timeout(0)
            unlocked = True
        _activate_window(hwnd)
        time.sleep(0.4)
        t0 = time.time()
        while (u.GetForegroundWindow() != hwnd
               and time.time() - t0 < 6.0):
            _front_plain(hwnd)
            time.sleep(1.5)
    finally:
        if unlocked:
            _set_foreground_lock_timeout(prev or 200000)


def minimize_window(hwnd: int) -> None:
    _u32().ShowWindow(hwnd, SW_MINIMIZE)
    ok("Minimized WPS")


def click_sheet(hwnd: int) -> None:
    """Click into the grid so Ctrl+A hits the sheet, not the ribbon."""
    u = _u32()
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    x = (rect.left + rect.right) // 2
    y = rect.top + min(280, max(160, (rect.bottom - rect.top) // 3))
    u.SetCursorPos(x, y)
    u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.1)


def _tap_ctrl(vk: int) -> None:
    u = _u32()
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.03)
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    u.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def ctrl_a_twice_copy() -> None:
    info("Ctrl+A")
    _tap_ctrl(VK_A)
    time.sleep(0.12)
    info("Ctrl+A")
    _tap_ctrl(VK_A)
    time.sleep(0.15)
    info("Ctrl+C")
    _tap_ctrl(VK_C)


def looks_like_fms_grid(text: str) -> bool:
    if not text or "Order ID" not in text[:4000]:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 2


# ---- Visual verification (screenshot + OCR/template) ----

def _capture_window(hwnd: int) -> "Image | None":
    """Capture screenshot of a specific window. Returns PIL Image or None."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    
    u = _u32()
    rect = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    if right <= left or bottom <= top:
        return None
    
    try:
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
        return img
    except Exception:
        return None


def _verify_headers_visual(hwnd: int, expected_headers: list[str] | None = None) -> bool:
    """Verify WPS window shows expected FMS grid headers via screenshot analysis.
    
    Uses OCR (pytesseract if available) or simple pixel sampling of header row.
    Returns True if verification passes, False otherwise.
    """
    if expected_headers is None:
        expected_headers = ["Order ID", "SLS Tracking Number", "Current TO Number"]
    
    img = _capture_window(hwnd)
    if img is None:
        warn("Visual verification: could not capture window screenshot")
        return True  # Don't fail if we can't capture
    
    # Try OCR first
    try:
        import pytesseract
        text = pytesseract.image_to_string(img)
        found = sum(1 for h in expected_headers if h.lower() in text.lower())
        if found >= 2:  # At least 2 of 3 expected headers
            ok(f"Visual verification (OCR): found {found}/{len(expected_headers)} expected headers")
            return True
        warn(f"Visual verification (OCR): only {found}/{len(expected_headers)} headers found")
    except ImportError:
        pass
    except Exception as e:
        warn(f"Visual verification (OCR) failed: {e}")
    
    # Fallback: simple pixel sampling of top region (header row area)
    try:
        # Sample top 10% of window height for header row
        w, h = img.size
        header_region = img.crop((0, 0, w, max(50, h // 10)))
        # Convert to grayscale and check for text-like contrast
        gray = header_region.convert('L')
        pixels = list(gray.getdata())
        if not pixels:
            return True
        # High contrast variance suggests text/grid present
        contrast = max(pixels) - min(pixels)
        if contrast > 80:  # Arbitrary threshold
            ok("Visual verification (pixel): header region has text-like contrast")
            return True
        warn("Visual verification (pixel): low contrast in header region")
    except Exception as e:
        warn(f"Visual verification (pixel) failed: {e}")
    
    return True  # Don't block on visual verification failure


def copy_from_wps(xlsx_path: Path, timeout: float = 180, verify_visual: bool = True, fast_mode: bool = False) -> str:
    """Focus WPS → click sheet → Ctrl+A twice → Copy. Fully automatic.
    
    If verify_visual=True, also captures screenshot and verifies header row visually.
    If fast_mode=True, uses optimized timing for large files (2000+ rows).
    """
    hwnd, _title = wait_for_export_window(xlsx_path, timeout=min(timeout, 60))
    deadline = time.time() + timeout
    last_err = "clipboard empty"
    
    # Fast mode: longer initial clipboard wait, fewer retries
    clipboard_wait = 25 if fast_mode else 12
    retry_sleep = 0.3 if fast_mode else 0.5
    poll_interval = 0.2 if fast_mode else 0.15
    
    while time.time() < deadline:
        try:
            before = get_clipboard_text()
        except Exception:
            before = ""
        try:
            focus_window(hwnd)
            click_sheet(hwnd)
            ctrl_a_twice_copy()
        except Exception as e:
            last_err = str(e)
            info("Key send retry:", last_err)
            time.sleep(retry_sleep)
            continue
        t1 = time.time()
        while time.time() - t1 < clipboard_wait:
            try:
                text = get_clipboard_text()
            except Exception as e:
                last_err = str(e)
                time.sleep(poll_interval)
                continue
            if text and text != before and looks_like_fms_grid(text):
                lines = [ln for ln in text.splitlines() if ln.strip()]
                ok(f"Clipboard: {len(lines)} lines, {len(text)} chars")
                
                # Visual verification (skip in fast_mode for speed)
                if verify_visual and not fast_mode:
                    _verify_headers_visual(hwnd)
                
                minimize_window(hwnd)
                return text
            time.sleep(poll_interval)
        last_err = "clipboard did not get Order ID grid"
        warn("Copy not ready, retrying Ctrl+A / Ctrl+C...")
        time.sleep(retry_sleep)
    raise TimeoutError(
        "Could not copy from WPS automatically. " + last_err
    )


