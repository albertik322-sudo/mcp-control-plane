from __future__ import annotations

import os
import subprocess
import time
import ctypes
import ctypes.wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import pyautogui
import pyperclip
from PIL import Image, ImageOps
from mcp_runtime import configure_stdio, create_mcp_server, run_powershell as _run_powershell

try:
    import pygetwindow as gw
except Exception:  # pragma: no cover - optional desktop backend
    gw = None

try:
    from pywinauto import Desktop
except Exception:  # pragma: no cover - optional UIA backend
    Desktop = None


ROOT = Path(__file__).resolve().parent
configure_stdio()
ARTIFACTS = ROOT / "gui_artifacts"
ARTIFACTS.mkdir(exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

mcp = create_mcp_server("windows-gui")

SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
user32 = ctypes.windll.user32


def _artifact_path(prefix: str, suffix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return ARTIFACTS / f"{prefix}-{stamp}-{int(time.time() * 1000) % 1000:03d}{suffix}"


def _window_dict(window: Any) -> dict[str, Any]:
    return {
        "title": getattr(window, "title", ""),
        "left": getattr(window, "left", None),
        "top": getattr(window, "top", None),
        "width": getattr(window, "width", None),
        "height": getattr(window, "height", None),
        "is_active": bool(getattr(window, "isActive", False)),
        "is_minimized": bool(getattr(window, "isMinimized", False)),
        "is_maximized": bool(getattr(window, "isMaximized", False)),
    }


def _check_screen_bounds(x: int, y: int) -> None:
    size = pyautogui.size()
    if not (0 <= x < size.width and 0 <= y < size.height):
        raise ValueError(
            {
                "reason": "coordinates are outside the primary screen",
                "x": x,
                "y": y,
                "screen": {"width": size.width, "height": size.height},
            }
        )


def _resolve_artifact(path: str | None, prefix: str, suffix: str) -> Path:
    target = Path(path).expanduser() if path else _artifact_path(prefix, suffix)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _save_image(
    image: Image.Image,
    path: str | None,
    prefix: str,
    fmt: str = "png",
    quality: int = 85,
    grayscale: bool = False,
    resize_width: int | None = None,
) -> dict[str, Any]:
    fmt_l = fmt.lower()
    if fmt_l not in {"png", "jpeg", "jpg"}:
        raise ValueError("fmt must be png or jpeg")
    if grayscale:
        image = ImageOps.grayscale(image)
    if resize_width and resize_width > 0 and image.width > resize_width:
        height = max(1, round(image.height * (resize_width / image.width)))
        image = image.resize((resize_width, height), Image.Resampling.LANCZOS)
    suffix = ".jpg" if fmt_l in {"jpeg", "jpg"} else ".png"
    target = _resolve_artifact(path, prefix, suffix)
    save_kwargs: dict[str, Any] = {}
    if suffix == ".jpg":
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        save_kwargs["quality"] = max(1, min(int(quality), 100))
        save_kwargs["optimize"] = True
    else:
        save_kwargs["optimize"] = True
    image.save(target, **save_kwargs)
    return {"path": str(target), "width": image.width, "height": image.height, "format": suffix.lstrip(".")}


@mcp.tool()
def get_screen_size() -> dict[str, int]:
    """Return the primary screen size in pixels."""
    size = pyautogui.size()
    return {"width": size.width, "height": size.height}


@mcp.tool()
def screenshot(path: str | None = None) -> dict[str, Any]:
    """Take a screenshot and save it under gui_artifacts unless path is provided."""
    image = pyautogui.screenshot()
    return _save_image(image, path, "screenshot", "png")


@mcp.tool()
def screenshot_optimized(
    path: str | None = None,
    fmt: str = "jpeg",
    quality: int = 85,
    grayscale: bool = True,
    resize_width: int = 1280,
) -> dict[str, Any]:
    """Take an AI-readable screenshot, optionally grayscale JPEG resized to a target width."""
    image = pyautogui.screenshot()
    return _save_image(image, path, "screenshot-optimized", fmt, quality, grayscale, resize_width)


@mcp.tool()
def screenshot_region(left: int, top: int, width: int, height: int, path: str | None = None) -> dict[str, Any]:
    """Take a screenshot of a rectangular region."""
    image = pyautogui.screenshot(region=(left, top, width, height))
    result = _save_image(image, path, "screenshot-region", "png")
    result["region"] = [left, top, width, height]
    return result


@mcp.tool()
def screenshot_region_optimized(
    left: int,
    top: int,
    width: int,
    height: int,
    path: str | None = None,
    fmt: str = "jpeg",
    quality: int = 85,
    grayscale: bool = True,
    resize_width: int = 1280,
) -> dict[str, Any]:
    """Take an optimized screenshot of a rectangular region."""
    image = pyautogui.screenshot(region=(left, top, width, height))
    result = _save_image(image, path, "screenshot-region-optimized", fmt, quality, grayscale, resize_width)
    result["region"] = [left, top, width, height]
    return result


@mcp.tool()
def pixel_color(x: int, y: int) -> dict[str, Any]:
    """Return RGB color of a screen pixel."""
    image = pyautogui.screenshot(region=(x, y, 1, 1))
    return {"x": x, "y": y, "rgb": list(image.getpixel((0, 0))[:3])}


@mcp.tool()
def mouse_position() -> dict[str, int]:
    """Return the current mouse cursor position."""
    pos = pyautogui.position()
    return {"x": pos.x, "y": pos.y}


@mcp.tool()
def move_mouse(x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    """Move the mouse pointer to screen coordinates."""
    _check_screen_bounds(x, y)
    pyautogui.moveTo(x, y, duration=max(0.0, duration))
    return mouse_position()


@mcp.tool()
def click(x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    """Click at the current position or at supplied screen coordinates."""
    if x is not None and y is not None:
        _check_screen_bounds(x, y)
    pyautogui.click(x=x, y=y, button=button, clicks=max(1, clicks))
    return mouse_position()


@mcp.tool()
def click_at(x: int, y: int, button: str = "left", restore_position: bool = True) -> dict[str, Any]:
    """Move to screen coordinates, click, and optionally return to the original cursor position."""
    _check_screen_bounds(x, y)
    original = pyautogui.position()
    pyautogui.click(x=x, y=y, button=button)
    if restore_position:
        pyautogui.moveTo(original.x, original.y)
    return {
        "clicked": [x, y],
        "button": button,
        "restore_position": restore_position,
        "original": [original.x, original.y],
        "current": [pyautogui.position().x, pyautogui.position().y],
    }


@mcp.tool()
def double_click(x: int | None = None, y: int | None = None, button: str = "left") -> dict[str, Any]:
    """Double-click at the current position or at supplied screen coordinates."""
    if x is not None and y is not None:
        _check_screen_bounds(x, y)
    pyautogui.doubleClick(x=x, y=y, button=button)
    return mouse_position()


@mcp.tool()
def drag_to(x: int, y: int, duration: float = 0.2, button: str = "left") -> dict[str, Any]:
    """Drag the mouse to screen coordinates."""
    pyautogui.dragTo(x, y, duration=max(0.0, duration), button=button)
    return mouse_position()


@mcp.tool()
def scroll(clicks: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    """Scroll the active window. Positive values scroll up, negative values scroll down."""
    pyautogui.scroll(clicks, x=x, y=y)
    return mouse_position()


@mcp.tool()
def press(key: str, presses: int = 1, interval: float = 0.0) -> dict[str, Any]:
    """Press one keyboard key."""
    pyautogui.press(key, presses=max(1, presses), interval=max(0.0, interval))
    return {"pressed": key, "presses": presses}


@mcp.tool()
def key_down(key: str) -> dict[str, Any]:
    """Press and hold a keyboard key until key_up is called."""
    pyautogui.keyDown(key)
    return {"key": key, "state": "down"}


@mcp.tool()
def key_up(key: str) -> dict[str, Any]:
    """Release a keyboard key."""
    pyautogui.keyUp(key)
    return {"key": key, "state": "up"}


@mcp.tool()
def hold_key(key: str, duration_ms: int = 250) -> dict[str, Any]:
    """Hold a keyboard key for a duration in milliseconds, then release it."""
    duration = max(0, int(duration_ms)) / 1000
    pyautogui.keyDown(key)
    try:
        time.sleep(duration)
    finally:
        pyautogui.keyUp(key)
    return {"key": key, "duration_ms": int(duration_ms)}


@mcp.tool()
def hotkey(keys: list[str]) -> dict[str, Any]:
    """Press a key combination, for example ['ctrl', 'l']."""
    if not keys:
        raise ValueError("keys must not be empty")
    pyautogui.hotkey(*keys)
    return {"hotkey": keys}


@mcp.tool()
def type_text(text: str, interval: float = 0.0) -> dict[str, Any]:
    """Type text into the active control."""
    pyautogui.write(text, interval=max(0.0, interval))
    return {"typed_chars": len(text)}


@mcp.tool()
def paste_text(text: str) -> dict[str, Any]:
    """Paste text into the active control using the clipboard."""
    old = pyperclip.paste()
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    return {"pasted_chars": len(text), "previous_clipboard_chars": len(old)}


@mcp.tool()
def get_clipboard() -> dict[str, Any]:
    """Return clipboard text."""
    text = pyperclip.paste()
    return {"text": text, "chars": len(text)}


@mcp.tool()
def set_clipboard(text: str) -> dict[str, Any]:
    """Set clipboard text."""
    pyperclip.copy(text)
    return {"chars": len(text)}


@mcp.tool()
def list_windows() -> dict[str, Any]:
    """List visible top-level windows."""
    if gw is None:
        return {"windows": [], "error": "pygetwindow is unavailable"}
    windows = [_window_dict(w) for w in gw.getAllWindows() if getattr(w, "title", "")]
    return {"windows": windows, "count": len(windows)}


def _enum_windows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def enum_handler(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows.append({"hwnd": hwnd, "title": buffer.value, "rectangle": [rect.left, rect.top, rect.right, rect.bottom]})
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(enum_handler)
    user32.EnumWindows(callback, 0)
    return rows


def _matching_window(title_substring: str) -> dict[str, Any] | None:
    needle = title_substring.lower()
    for window in _enum_windows():
        if needle in window["title"].lower():
            return window
    return None


def _window_bounds(title_substring: str) -> tuple[int, int, int, int, dict[str, Any]]:
    window = _matching_window(title_substring)
    if not window:
        raise RuntimeError(f"no window matching {title_substring!r}")
    left, top, right, bottom = window["rectangle"]
    return left, top, right - left, bottom - top, window


def _foreground_window() -> dict[str, Any] | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": hwnd,
        "title": buffer.value,
        "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
    }


@mcp.tool()
def list_windows_win32() -> dict[str, Any]:
    """List visible top-level windows using Win32 APIs."""
    rows = _enum_windows()
    return {"windows": rows, "count": len(rows)}


@mcp.tool()
def open_url(url: str, focus_title: str | None = None, wait_seconds: float = 1.0) -> dict[str, Any]:
    """Open a URL or file association through Windows Shell, optionally focusing a matching window."""
    os.startfile(url)  # noqa: S606 - intentional local GUI action
    time.sleep(max(0.0, wait_seconds))
    focused: dict[str, Any] | None = None
    if focus_title:
        focused = focus_window_win32(focus_title)
    return {"opened": url, "focused": focused}


@mcp.tool()
def wait_for_window(title_substring: str, timeout: float = 10.0, interval: float = 0.25) -> dict[str, Any]:
    """Wait until a visible top-level window title contains title_substring."""
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        window = _matching_window(title_substring)
        if window:
            return {"found": True, "window": window}
        time.sleep(max(0.05, interval))
    return {"found": False, "title_substring": title_substring}


@mcp.tool()
def screenshot_window(title_substring: str, path: str | None = None, margin: int = 0) -> dict[str, Any]:
    """Take a screenshot of a matching top-level window instead of the full desktop."""
    left, top, width, height, window = _window_bounds(title_substring)
    screen = pyautogui.size()
    x1 = max(0, left - max(0, margin))
    y1 = max(0, top - max(0, margin))
    x2 = min(screen.width, left + width + max(0, margin))
    y2 = min(screen.height, top + height + max(0, margin))
    target = Path(path).expanduser() if path else _artifact_path("screenshot-window", ".png")
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    image = pyautogui.screenshot(region=(x1, y1, max(1, x2 - x1), max(1, y2 - y1)))
    image.save(target)
    return {
        "path": str(target),
        "width": image.width,
        "height": image.height,
        "window": window,
        "region": [x1, y1, image.width, image.height],
    }


@mcp.tool()
def screenshot_window_optimized(
    title_substring: str,
    path: str | None = None,
    margin: int = 0,
    fmt: str = "jpeg",
    quality: int = 85,
    grayscale: bool = True,
    resize_width: int = 1280,
) -> dict[str, Any]:
    """Take an optimized screenshot of a matching top-level window."""
    left, top, width, height, window = _window_bounds(title_substring)
    screen = pyautogui.size()
    x1 = max(0, left - max(0, margin))
    y1 = max(0, top - max(0, margin))
    x2 = min(screen.width, left + width + max(0, margin))
    y2 = min(screen.height, top + height + max(0, margin))
    image = pyautogui.screenshot(region=(x1, y1, max(1, x2 - x1), max(1, y2 - y1)))
    result = _save_image(image, path, "screenshot-window-optimized", fmt, quality, grayscale, resize_width)
    result["window"] = window
    result["region"] = [x1, y1, image.width, image.height]
    return result


@mcp.tool()
def active_window() -> dict[str, Any]:
    """Return the current foreground window title and bounds."""
    window = _foreground_window()
    return {"window": window}


@mcp.tool()
def screenshot_active_window(path: str | None = None, margin: int = 0) -> dict[str, Any]:
    """Take a screenshot of the foreground window."""
    window = _foreground_window()
    if not window or not window.get("title"):
        raise RuntimeError("no active window")
    return screenshot_window(window["title"], path, margin)


@mcp.tool()
def screenshot_active_window_optimized(
    path: str | None = None,
    margin: int = 0,
    fmt: str = "jpeg",
    quality: int = 85,
    grayscale: bool = True,
    resize_width: int = 1280,
) -> dict[str, Any]:
    """Take an optimized screenshot of the foreground window."""
    window = _foreground_window()
    if not window or not window.get("title"):
        raise RuntimeError("no active window")
    return screenshot_window_optimized(window["title"], path, margin, fmt, quality, grayscale, resize_width)


@mcp.tool()
def click_in_window(
    title_substring: str,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    focus: bool = True,
) -> dict[str, Any]:
    """Click at coordinates relative to a matching window's top-left corner."""
    left, top, width, height, window = _window_bounds(title_substring)
    if not (0 <= x <= width and 0 <= y <= height):
        raise ValueError({"reason": "relative coordinates are outside the window", "window": window, "x": x, "y": y})
    if focus:
        focus_window_win32(title_substring)
    pyautogui.click(x=left + x, y=top + y, button=button, clicks=max(1, clicks))
    return {"window": window, "relative": [x, y], "absolute": [left + x, top + y]}


@mcp.tool()
def scroll_in_window(title_substring: str, clicks: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    """Scroll inside a matching window using optional relative coordinates."""
    left, top, width, height, window = _window_bounds(title_substring)
    if x is None:
        x = width // 2
    if y is None:
        y = height // 2
    focus_window_win32(title_substring)
    pyautogui.scroll(clicks, x=left + x, y=top + y)
    return {"window": window, "relative": [x, y], "clicks": clicks}


@mcp.tool()
def paste_url_in_window(title_substring: str, url: str) -> dict[str, Any]:
    """Focus a browser-like window, paste URL into the address bar, and press Enter."""
    focus = focus_window_win32(title_substring)
    pyperclip.copy(url)
    pyautogui.hotkey("ctrl", "l")
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")
    return {"url": url, "focus": focus}


@mcp.tool()
def activate_window(title_substring: str) -> dict[str, Any]:
    """Activate the first top-level window whose title contains the given text."""
    if gw is None:
        raise RuntimeError("pygetwindow is unavailable")
    needle = title_substring.lower()
    matches = [w for w in gw.getAllWindows() if needle in getattr(w, "title", "").lower()]
    if not matches:
        return {"activated": False, "reason": "no matching window"}
    window = matches[0]
    if getattr(window, "isMinimized", False):
        window.restore()
    window.activate()
    return {"activated": True, "window": _window_dict(window)}


@mcp.tool()
def focus_window_win32(title_substring: str) -> dict[str, Any]:
    """Restore and focus a top-level window using Win32 APIs."""
    needle = title_substring.lower()
    matches: list[dict[str, Any]] = []

    def enum_handler(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if needle in title.lower():
            matches.append({"hwnd": hwnd, "title": title})
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(enum_handler)
    user32.EnumWindows(callback, 0)
    if not matches:
        return {"focused": False, "reason": "no matching window"}
    hwnd = matches[0]["hwnd"]
    user32.ShowWindowAsync(hwnd, SW_RESTORE)
    time.sleep(0.2)
    focused = bool(user32.SetForegroundWindow(hwnd))
    return {"focused": focused, "window": matches[0]}


@mcp.tool()
def launch_app(path: str, args: list[str] | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Launch a local Windows application."""
    command = [str(Path(path).expanduser())] + (args or [])
    proc = subprocess.Popen(command, cwd=cwd or str(ROOT))
    return {"pid": proc.pid, "command": command}


@mcp.tool()
def window_action_win32(title_substring: str, action: str) -> dict[str, Any]:
    """Run a Win32 window action: minimize, maximize, restore, close."""
    actions = {"minimize": 6, "maximize": 3, "restore": 9}
    matches = [w for w in _enum_windows() if title_substring.lower() in w["title"].lower()]
    if not matches:
        return {"ok": False, "reason": "no matching window"}
    hwnd = matches[0]["hwnd"]
    if action == "close":
        ok = bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))
    elif action in actions:
        ok = bool(user32.ShowWindowAsync(hwnd, actions[action]))
    else:
        raise ValueError("action must be minimize, maximize, restore, or close")
    return {"ok": ok, "window": matches[0], "action": action}


@mcp.tool()
def minimize_window(title_substring: str) -> dict[str, Any]:
    """Minimize a top-level window whose title contains the given text."""
    return window_action_win32(title_substring, "minimize")


@mcp.tool()
def maximize_window(title_substring: str) -> dict[str, Any]:
    """Maximize a top-level window whose title contains the given text."""
    return window_action_win32(title_substring, "maximize")


@mcp.tool()
def restore_window(title_substring: str) -> dict[str, Any]:
    """Restore a top-level window whose title contains the given text."""
    return window_action_win32(title_substring, "restore")


@mcp.tool()
def set_window_bounds_win32(title_substring: str, left: int, top: int, width: int, height: int) -> dict[str, Any]:
    """Move and resize a top-level window using Win32 APIs."""
    matches = [w for w in _enum_windows() if title_substring.lower() in w["title"].lower()]
    if not matches:
        return {"ok": False, "reason": "no matching window"}
    hwnd = matches[0]["hwnd"]
    ok = bool(user32.MoveWindow(hwnd, left, top, width, height, True))
    return {"ok": ok, "window": matches[0], "bounds": [left, top, width, height]}


@mcp.tool()
def resize_window(title_substring: str, width: int, height: int) -> dict[str, Any]:
    """Resize a top-level window while keeping its current top-left position."""
    left, top, _, _, window = _window_bounds(title_substring)
    result = set_window_bounds_win32(title_substring, left, top, max(1, width), max(1, height))
    result["previous_window"] = window
    return result


@mcp.tool()
def reposition_window(title_substring: str, left: int, top: int) -> dict[str, Any]:
    """Move a top-level window while keeping its current size."""
    _, _, width, height, window = _window_bounds(title_substring)
    result = set_window_bounds_win32(title_substring, left, top, width, height)
    result["previous_window"] = window
    return result


@mcp.tool()
def inspect_window(title_substring: str | None = None, max_items: int = 80) -> dict[str, Any]:
    """Inspect UI Automation elements in a window or across top-level windows."""
    if Desktop is None:
        return {"items": [], "error": "pywinauto is unavailable"}
    desktop = Desktop(backend="uia")
    windows = desktop.windows()
    if title_substring:
        needle = title_substring.lower()
        windows = [w for w in windows if needle in w.window_text().lower()]

    items: list[dict[str, Any]] = []
    for window in windows[:10]:
        try:
            rect = window.rectangle()
            items.append(
                {
                    "title": window.window_text(),
                    "class_name": window.class_name(),
                    "control_type": window.element_info.control_type,
                    "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
                }
            )
        except Exception as exc:
            items.append({"error": str(exc)})
        if len(items) >= max_items:
            break
    return {"items": items, "count": len(items)}


@mcp.tool()
def list_processes(name_filter: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List local Windows processes."""
    needle = name_filter.lower() if name_filter else None
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "username", "status", "cpu_percent", "memory_info"]):
        info = proc.info
        name = info.get("name") or ""
        if needle and needle not in name.lower():
            continue
        mem = info.get("memory_info")
        rows.append(
            {
                "pid": info.get("pid"),
                "name": name,
                "username": info.get("username"),
                "status": info.get("status"),
                "rss_mb": round((mem.rss if mem else 0) / 1024 / 1024, 1),
            }
        )
        if len(rows) >= limit:
            break
    return {"processes": rows, "count": len(rows)}


@mcp.tool()
def process_info(pid: int) -> dict[str, Any]:
    """Return detailed information for a local process."""
    proc = psutil.Process(pid)
    with proc.oneshot():
        return {
            "pid": proc.pid,
            "name": proc.name(),
            "exe": proc.exe(),
            "cmdline": proc.cmdline(),
            "cwd": proc.cwd(),
            "username": proc.username(),
            "status": proc.status(),
            "create_time": proc.create_time(),
            "memory_info": proc.memory_info()._asdict(),
            "cpu_percent": proc.cpu_percent(interval=0.1),
        }


@mcp.tool()
def terminate_process(pid: int, kill: bool = False) -> dict[str, Any]:
    """Terminate or kill a local process by PID."""
    proc = psutil.Process(pid)
    name = proc.name()
    if kill:
        proc.kill()
    else:
        proc.terminate()
    return {"pid": pid, "name": name, "kill": kill}


@mcp.tool()
def run_powershell(command: str, timeout: int = 60) -> dict[str, Any]:
    """Run a local PowerShell command and return stdout/stderr."""
    return _run_powershell(command, timeout=timeout, cwd=str(ROOT))


if __name__ == "__main__":
    mcp.run()
