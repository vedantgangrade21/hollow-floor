"""
PSYCHOLOGICAL HORROR GAME — EFFECTS TEST BOARD
================================================
A developer control panel for triggering each "system takeover" effect
so you can see how they behave before wiring them into the game.

Everything here acts on the LOCAL machine (the player's own PC) for
atmospheric effect only, and every change is undone afterwards (the wallpaper,
mouse trails and keyboard lights are restored). One exception to "local": the
location effect asks ip-api.com for the PC's public IP and rough location, so
that request does leave the machine. Nothing is stored or sent anywhere else.

Run:  python horror_toggle_board.py

Notes:
  - "Draw in Paint" needs pyautogui (pip install pyautogui). It moves the
    real mouse/keyboard, so don't touch the PC while it runs. Slam the
    mouse into a screen corner to trigger pyautogui's failsafe abort.
  - Text files are written to your Desktop and can be cleaned up with the
    "Clean up my mess" button.
"""

import os
import sys
import socket
import platform
import getpass
import subprocess
import threading
import random
import json
import ctypes
from datetime import datetime
from urllib.request import urlopen

import tkinter as tk
from tkinter import font as tkfont

# ---- Optional dependency: pyautogui (only needed for Paint drawing) ---------
try:
    import pyautogui
    pyautogui.FAILSAFE = True   # slam mouse to a corner to abort
    HAS_PYAUTOGUI = True
except Exception:
    HAS_PYAUTOGUI = False

IS_WINDOWS = os.name == "nt"


def _resolve_desktop():
    """Find the REAL Desktop, honoring OneDrive redirection on Windows."""
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            val, _ = winreg.QueryValueEx(key, "Desktop")
            path = os.path.expandvars(val)   # expands %USERPROFILE%, %OneDrive%, etc.
            if os.path.isdir(path):
                return path
        except Exception:
            pass
    # Fallbacks
    for cand in (
        os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.expanduser("~"),
    ):
        if os.path.isdir(cand):
            return cand
    return os.path.expanduser("~")


DESKTOP = _resolve_desktop()

# Track files we create so we can clean them up.
CREATED_FILES = []

_DISPLAY_NAME = None


def get_display_name():
    """The account's DISPLAY name (e.g. 'Jane Smith' as shown in Start),
    not the login id ('jsmith'). Falls back to the login name."""
    global _DISPLAY_NAME
    if _DISPLAY_NAME is not None:
        return _DISPLAY_NAME
    name = None
    if IS_WINDOWS:
        try:
            NAME_DISPLAY = 3   # EXTENDED_NAME_FORMAT.NameDisplay
            secur32 = ctypes.windll.secur32
            size = ctypes.c_ulong(0)
            secur32.GetUserNameExW(NAME_DISPLAY, None, ctypes.byref(size))  # ask size
            if size.value:
                buf = ctypes.create_unicode_buffer(size.value)
                if secur32.GetUserNameExW(NAME_DISPLAY, buf, ctypes.byref(size)) and buf.value.strip():
                    name = buf.value.strip()
        except Exception:
            name = None
    _DISPLAY_NAME = name or getpass.getuser()
    return _DISPLAY_NAME

CREEPY_LINES = [
    "I can see you.",
    "Why did you open me?",
    "You shouldn't have run this.",
    "I've been waiting.",
    "Do you feel that?",
    "I know where you live.",
    "Don't turn around.",
    "We are already inside.",
    "There is no uninstall.",
    "You left the door open.",
]

# (title, message, icon) for the native Windows system pop-ups. Each is shown
# as an OK / Cancel box; clicking Cancel triggers the "Goodbye." box.
SYSTEM_PROMPTS = [
    ("System message", "A program is trying to reach you.\nAllow access?", "warning"),
    ("Error", "Reality.exe has stopped responding.", "error"),
    ("System message", "I have been watching for {days} days.\nDo you want to continue?", "warning"),
    ("Warning", "Someone is in the room with you.\nProceed anyway?", "warning"),
    ("System message", "Hello, {name}. I remember you.", "info"),
    ("Critical Error", "Your soul will be moved to the Recycle Bin.", "error"),
    ("System message", "Do not close this window.", "warning"),
    ("Windows Update", "Installing nightmare... 66% complete.\nDo not turn off your computer.", "info"),
    ("System message", "Look behind you before you click.", "warning"),
    ("Access", "Delete all evidence that you were here?", "question"),
]

# Messages written into the Notepad windows that pop up during the "haunt".
NOTEPAD_MESSAGES = [
    "I am inside your computer now, {name}.\n\nThere is no antivirus for me.",
    "stop.\nstop.\nstop reading this.\n\nturn around.",
    "Do you remember what you did?\n\nI do. I remember everything.",
    "L O O K   B E H I N D   Y O U",
    "it is so cold in here.\n\nlet me out.\nlet me out.\nLET ME OUT.",
    "01001000 01000101 01001100 01010000\n\n( h e l p )",
    "You have {n} days left, {user}.\n\nUse them wisely.",
    "why did you cancel?\n\nyou should not have done that.",
    "I have your files.\nI have your photos.\nI have you.",
    "we are already inside.\ncheck behind the curtains.",
]

# Short UPPERCASE phrases the cursor hand-draws in Paint (kept short so they
# fit and don't take forever to draw stroke by stroke).
PAINT_MESSAGES = [
    "I SEE YOU", "GET OUT", "BEHIND YOU", "RUN", "IM HERE",
    "WHY", "LOOK UP", "NO ESCAPE", "DONT MOVE", "HELP ME",
    "WAKE UP", "TOO LATE", "HES HERE", "STAY", "COME BACK",
    "MINE NOW", "LOOK DOWN", "DONT BLINK", "SO COLD", "NOT ALONE",
]

# A minimal single-stroke "font": each glyph maps to a list of strokes; each
# stroke is a polyline of (x, y) points in a unit cell (x,y in 0..1, y = down).
# Curves are approximated with short line segments. The cursor draws each
# stroke with the pencil (pen down -> follow points -> pen up).
STROKE_FONT = {
    " ": [],
    "A": [[(0, 1), (0.5, 0), (1, 1)], [(0.25, 0.55), (0.75, 0.55)]],
    "B": [[(0, 0), (0, 1)],
          [(0, 0), (0.6, 0.08), (0.75, 0.25), (0.6, 0.45), (0, 0.5)],
          [(0, 0.5), (0.7, 0.55), (0.85, 0.75), (0.6, 0.95), (0, 1)]],
    "C": [[(0.9, 0.15), (0.5, 0), (0.15, 0.2), (0, 0.5), (0.15, 0.8), (0.5, 1), (0.9, 0.85)]],
    "D": [[(0, 0), (0, 1)], [(0, 0), (0.6, 0.15), (0.85, 0.5), (0.6, 0.85), (0, 1)]],
    "E": [[(0.9, 0), (0, 0), (0, 1), (0.9, 1)], [(0, 0.5), (0.6, 0.5)]],
    "F": [[(0.9, 0), (0, 0), (0, 1)], [(0, 0.5), (0.6, 0.5)]],
    "G": [[(0.9, 0.15), (0.5, 0), (0.15, 0.2), (0, 0.5), (0.15, 0.8), (0.5, 1),
           (0.9, 0.85), (0.9, 0.55), (0.55, 0.55)]],
    "H": [[(0, 0), (0, 1)], [(1, 0), (1, 1)], [(0, 0.5), (1, 0.5)]],
    "I": [[(0.2, 0), (0.8, 0)], [(0.5, 0), (0.5, 1)], [(0.2, 1), (0.8, 1)]],
    "J": [[(0.8, 0), (0.8, 0.8), (0.6, 1), (0.3, 1), (0.1, 0.8)]],
    "K": [[(0, 0), (0, 1)], [(0.9, 0), (0, 0.5), (0.9, 1)]],
    "L": [[(0, 0), (0, 1), (0.85, 1)]],
    "M": [[(0, 1), (0, 0), (0.5, 0.6), (1, 0), (1, 1)]],
    "N": [[(0, 1), (0, 0), (1, 1), (1, 0)]],
    "O": [[(0.5, 0), (0.15, 0.2), (0, 0.5), (0.15, 0.8), (0.5, 1),
           (0.85, 0.8), (1, 0.5), (0.85, 0.2), (0.5, 0)]],
    "P": [[(0, 1), (0, 0), (0.6, 0.1), (0.78, 0.3), (0.6, 0.5), (0, 0.55)]],
    "Q": [[(0.5, 0), (0.15, 0.2), (0, 0.5), (0.15, 0.8), (0.5, 1),
           (0.85, 0.8), (1, 0.5), (0.85, 0.2), (0.5, 0)], [(0.6, 0.7), (1, 1)]],
    "R": [[(0, 1), (0, 0), (0.6, 0.1), (0.78, 0.3), (0.6, 0.5), (0, 0.55)],
          [(0.3, 0.55), (0.9, 1)]],
    "S": [[(0.9, 0.15), (0.5, 0), (0.15, 0.15), (0.15, 0.4), (0.5, 0.5),
           (0.85, 0.6), (0.85, 0.85), (0.5, 1), (0.1, 0.85)]],
    "T": [[(0, 0), (1, 0)], [(0.5, 0), (0.5, 1)]],
    "U": [[(0, 0), (0, 0.75), (0.2, 0.95), (0.5, 1), (0.8, 0.95), (1, 0.75), (1, 0)]],
    "V": [[(0, 0), (0.5, 1), (1, 0)]],
    "W": [[(0, 0), (0.25, 1), (0.5, 0.4), (0.75, 1), (1, 0)]],
    "X": [[(0, 0), (1, 1)], [(1, 0), (0, 1)]],
    "Y": [[(0, 0), (0.5, 0.5), (1, 0)], [(0.5, 0.5), (0.5, 1)]],
    "Z": [[(0, 0), (1, 0), (0, 1), (1, 1)]],
    "!": [[(0.5, 0), (0.5, 0.65)], [(0.5, 0.9), (0.5, 1)]],
    "?": [[(0.1, 0.2), (0.4, 0), (0.7, 0.1), (0.75, 0.35), (0.5, 0.5), (0.5, 0.65)],
          [(0.5, 0.9), (0.5, 1)]],
    ".": [[(0.5, 0.95), (0.55, 0.95), (0.55, 1), (0.5, 1), (0.5, 0.95)]],
}


# =============================================================================
#  EFFECT IMPLEMENTATIONS
# =============================================================================

# --- Pen timing (raise these if Paint still drops strokes) -------------------
PEN_STEP_PX = 5       # distance between mouse samples along a stroke (smaller = smoother)
PEN_STEP_SLEEP = 0.012  # pause after each sample so Paint processes the move
PEN_DOWN_SETTLE = 0.12  # pause after pressing the pen down
PEN_UP_SETTLE = 0.12    # pause after lifting the pen (before the next stroke)


# =============================================================================
#  LOW-LEVEL WINDOWS MOUSE INPUT (via SendInput)
#  pyautogui.mouseDown() does not reliably hold the button for a drag in
#  Windows 11 Paint, so we inject a real, continuous button-held input stream.
# =============================================================================
if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000

    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _INPUT_MOUSE = 0

    def _send_mouse(flags, dx=0, dy=0):
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, None)
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _win_move_to(x, y):
        """Absolute move, normalized across the whole virtual desktop (multi-monitor safe)."""
        vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        nx = int((x - vx) * 65535 / max(1, vw - 1))
        ny = int((y - vy) * 65535 / max(1, vh - 1))
        _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)

    def _win_mouse_down():
        _send_mouse(MOUSEEVENTF_LEFTDOWN)

    def _win_mouse_up():
        _send_mouse(MOUSEEVENTF_LEFTUP)

    def _get_cursor_pos():
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def find_paint_window():
        """Return HWND of a visible window whose title ends with 'Paint', else None."""
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            n = _user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip().lower().endswith("paint"):
                found.append(hwnd)
            return True

        _user32.EnumWindows(_cb, 0)
        return found[0] if found else None

    def get_paint_canvas_area(log):
        """Maximize the Paint window and return the safe drawing rect inside it.
        Returns (x0, y0, x1, y1) or None if Paint can't be found."""
        import time
        hwnd = None
        for _ in range(20):  # poll up to ~5s for Paint to appear
            hwnd = find_paint_window()
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return None
        SW_MAXIMIZE = 3
        _user32.ShowWindow(hwnd, SW_MAXIMIZE)
        try:
            _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.6)  # let the maximize animation finish
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win = (rect.left, rect.top, rect.right, rect.bottom)

        # Preferred: detect the actual white canvas rectangle from a screenshot.
        area = detect_canvas_rect(win, log)
        if area is not None:
            return area

        # Safe fallback: a SMALL box centered on the window (slightly below
        # center, since the ribbon eats the top). This stays well inside a
        # typical canvas even though we couldn't measure it exactly.
        l, t, r, b = win
        ww, wh = r - l, b - t
        cx, cy = (l + r) / 2, (t + b) / 2 + wh * 0.06
        bw, bh = ww * 0.46, wh * 0.42
        x0, y0, x1, y1 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2
        if x1 - x0 < 160 or y1 - y0 < 120:
            return None
        log(f"Paint canvas (SAFE centered fallback): {int(x0)},{int(y0)} -> {int(x1)},{int(y1)}")
        return (x0, y0, x1, y1)


    def detect_canvas_rect(win_rect, log, margin=18):
        """Find Paint's white drawing canvas from a screenshot, robust to any
        display scaling / multi-monitor layout. Returns an inset (x0,y0,x1,y1)
        in the SAME coordinate space as GetWindowRect/SendInput, or None.

        Method: grab the whole virtual desktop, MEASURE the pixels-per-unit
        scale by comparing the image size to the Win32 virtual-desktop metrics,
        crop out the window region, locate the white canvas by white-pixel
        projection, then map back through the measured scale."""
        try:
            from PIL import ImageGrab
        except Exception:
            return None
        l, t, r, b = win_rect

        # Virtual desktop in the process coordinate space (matches SendInput).
        vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            return None
        try:
            full = ImageGrab.grab(all_screens=True).convert("RGB")
        except Exception:
            return None
        FW, FH = full.size
        scale_x = FW / vw          # image pixels per coordinate unit
        scale_y = FH / vh

        # Window region in image pixels (measured scale handles DPI/scaling).
        il = int(round((l - vx) * scale_x)); ir = int(round((r - vx) * scale_x))
        it = int(round((t - vy) * scale_y)); ib = int(round((b - vy) * scale_y))
        il, ir = max(0, min(il, FW)), max(0, min(ir, FW))
        it, ib = max(0, min(it, FH)), max(0, min(ib, FH))
        if ir - il < 100 or ib - it < 100:
            return None
        win_img = full.crop((il, it, ir, ib))
        W, H = win_img.size
        px = win_img.load()
        XS, YS = 4, 2  # sampling steps (speed)

        def is_white(x, y):
            c = px[x, y]
            return c[0] >= 245 and c[1] >= 245 and c[2] >= 245

        # For each sampled row, find the LONGEST CONTIGUOUS run of white. This
        # ignores scattered white UI (resize handles, title-bar icons, tooltips)
        # and keys on the one big solid white block = the canvas.
        row_span = []  # (start_px, end_px, length) per sampled row
        for y in range(0, H, YS):
            best_len, best = 0, (0, 0)
            cur_start = None
            for x in range(0, W, XS):
                if is_white(x, y):
                    if cur_start is None:
                        cur_start = x
                    cur_end = x
                elif cur_start is not None:
                    if cur_end - cur_start > best_len:
                        best_len, best = cur_end - cur_start, (cur_start, cur_end)
                    cur_start = None
            if cur_start is not None and cur_end - cur_start > best_len:
                best_len, best = cur_end - cur_start, (cur_start, cur_end)
            row_span.append((best[0], best[1], best_len))

        # Canvas rows: those whose white run spans a big fraction of the window.
        min_run = 0.30 * W
        canvas_rows = [rs[2] >= min_run for rs in row_span]
        y0i, y1i = _longest_run(canvas_rows)
        if y0i is None:
            return None
        band = row_span[y0i:y1i + 1]
        starts = sorted(rs[0] for rs in band)
        ends = sorted(rs[1] for rs in band)
        rx0 = starts[len(starts) // 2]      # median left edge (robust to outliers)
        rx1 = ends[len(ends) // 2]          # median right edge
        ry0, ry1 = y0i * YS, y1i * YS

        if (rx1 - rx0) < 0.25 * W or (ry1 - ry0) < 0.20 * H:
            return None                     # too small to be the real canvas

        # Map canvas (image px, relative to window) back to coordinate units.
        cx0 = l + rx0 / scale_x + margin
        cy0 = t + ry0 / scale_y + margin
        cx1 = l + rx1 / scale_x - margin
        cy1 = t + ry1 / scale_y - margin
        if cx1 - cx0 < 120 or cy1 - cy0 < 100:
            return None
        log(f"Paint canvas (detected): {int(cx0)},{int(cy0)} -> {int(cx1)},{int(cy1)} "
            f"({int(cx1 - cx0)}x{int(cy1 - cy0)}), scale {scale_x:.3f}")
        return (cx0, cy0, cx1, cy1)


    def _longest_run(flags):
        """Return (start, end) indices of the longest run of True in `flags`."""
        best_len, best = 0, (None, None)
        i, n = 0, len(flags)
        while i < n:
            if flags[i]:
                j = i
                while j < n and flags[j]:
                    j += 1
                if j - i > best_len:
                    best_len, best = j - i, (i, j - 1)
                i = j
            else:
                i += 1
        return best


def _pencil_stroke(points, area=None):
    """Draw a continuous pencil line through `points` using real SendInput events.
    Each segment is subdivided into small steps with a real pause after every
    move so Paint renders the whole line. `points` are clamped to `area` so the
    pen can never stray onto the toolbar or off the canvas."""
    import math
    import time

    def clamp(p):
        if area is None:
            return p
        x0, y0, x1, y1 = area
        return (min(max(p[0], x0), x1), min(max(p[1], y0), y1))

    points = [clamp(p) for p in points]
    if len(points) < 2:
        return

    # Move to start with pen UP, settle, press down (real button-hold via SendInput).
    _win_move_to(*points[0])
    time.sleep(0.05)
    _win_mouse_down()
    time.sleep(PEN_DOWN_SETTLE)
    _win_move_to(*clamp((points[0][0] + 1, points[0][1] + 1)))  # nudge -> commit start dot
    time.sleep(PEN_STEP_SLEEP)
    for (x_a, y_a), (x_b, y_b) in zip(points, points[1:]):
        dist = math.hypot(x_b - x_a, y_b - y_a)
        steps = max(2, int(dist / PEN_STEP_PX))
        for s in range(1, steps + 1):
            t = s / steps
            _win_move_to(x_a + (x_b - x_a) * t, y_a + (y_b - y_a) * t)
            time.sleep(PEN_STEP_SLEEP)
    time.sleep(0.04)
    _win_mouse_up()
    time.sleep(PEN_UP_SETTLE)


#  Glyph metrics as fractions of the glyph height.
GLYPH_W = 0.72        # letter cell width
GLYPH_GAP = 0.26      # gap between adjacent letters
SPACE_W = 0.55        # width of a space between words
LINE_GAP = 0.45       # vertical gap between wrapped lines


def _line_width(s, cw, gp, space_w):
    """Pixel width of a single line string `s` at the given metrics."""
    if not s:
        return 0.0
    w = sum(space_w if ch == " " else cw for ch in s)
    return w + max(0, len(s) - 1) * gp


def _wrap_words(text, cw, gp, space_w, max_w):
    """Greedily wrap `text` into lines whose width fits `max_w`."""
    lines, cur = [], ""
    for word in text.split():
        trial = word if not cur else cur + " " + word
        if cur and _line_width(trial, cw, gp, space_w) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _draw_text_with_cursor(text, area, jitter, log):
    """Hand-draw `text` inside `area`=(x0,y0,x1,y1), measuring the box and
    wrapping into centered lines so the message always fits the canvas."""
    x0, y0, x1, y1 = area
    area_w, area_h = x1 - x0, y1 - y0

    # Choose the largest glyph height that lets the wrapped message fit the box
    # both horizontally (every line within width) and vertically (all lines).
    height = min(210, area_h)
    lines = [text]
    while True:
        cw, gp, sw = GLYPH_W * height, GLYPH_GAP * height, SPACE_W * height
        lines = _wrap_words(text, cw, gp, sw, area_w)
        widest = max(_line_width(s, cw, gp, sw) for s in lines)
        block_h = len(lines) * height + (len(lines) - 1) * (LINE_GAP * height)
        if (widest <= area_w and block_h <= area_h) or height <= 55:
            break
        height -= 5

    cw, gp, sw = GLYPH_W * height, GLYPH_GAP * height, SPACE_W * height
    line_h = height + LINE_GAP * height
    block_h = len(lines) * height + (len(lines) - 1) * (LINE_GAP * height)

    # Center the whole block vertically (with a little random slack).
    slack_y = max(0, area_h - block_h)
    top = y0 + slack_y * random.uniform(0.3, 0.7)

    def jit():
        return random.uniform(-jitter, jitter)

    log(f"Layout: {len(lines)} line(s), glyph height {int(height)}px -> {lines}")
    for li, line in enumerate(lines):
        line_w = _line_width(line, cw, gp, sw)
        ox = x0 + max(0, area_w - line_w) / 2.0   # center this line horizontally
        oy = top + li * line_h
        x_cursor = ox
        for ch in line:
            if ch == " ":
                x_cursor += sw + gp
                continue
            strokes = STROKE_FONT.get(ch.upper())
            if strokes:
                lx, ly = jit(), jit()  # one jitter offset per letter (stays coherent)
                for stroke in strokes:
                    pts = [(x_cursor + nx * cw + lx + jit() * 0.4,
                            oy + ny * height + ly + jit() * 0.4)
                           for (nx, ny) in stroke]
                    _pencil_stroke(pts, area=area)
            x_cursor += cw + gp


def effect_open_paint_and_write(log):
    """Open MS Paint, then HAND-DRAW a random message with the cursor (pencil)."""
    if not IS_WINDOWS:
        log("Paint effect is Windows-only.")
        return

    def run():
        import time
        try:
            log("Opening Paint...")
            subprocess.Popen(["mspaint.exe"])
            time.sleep(2.0)  # let Paint start (uses the default Pencil tool)

            # Find + maximize the real Paint window and draw only inside its canvas.
            area = get_paint_canvas_area(log)
            if area is None:
                log("Couldn't locate the Paint window — aborting to avoid drawing elsewhere.")
                return
            time.sleep(0.4)

            message = random.choice(PAINT_MESSAGES)
            jitter = random.uniform(2.0, 4.0)   # subtle shakiness, letters stay legible
            log(f"Scrawling: {message!r}")
            _draw_text_with_cursor(message, area, jitter, log)

            # Sometimes add a frantic underline/slash below the text for menace.
            if random.random() < 0.5:
                x0, y0, x1, y1 = area
                sx = random.uniform(x0 + 40, x0 + (x1 - x0) * 0.35)
                sy = random.uniform(y0 + (y1 - y0) * 0.55, y1 - 40)
                pts = [(sx, sy)]
                for _ in range(random.randint(4, 8)):
                    sx += random.randint(60, 140)
                    if sx > x1 - 40:
                        break
                    pts.append((sx, sy + random.randint(-25, 25)))
                _pencil_stroke(pts, area=area)

            log("Paint effect done.")
        except Exception as e:
            log(f"Paint effect failed: {e}")

    threading.Thread(target=run, daemon=True).start()


# =============================================================================
#  NATIVE WINDOWS SYSTEM POP-UPS (real Windows-11 style MessageBox)
# =============================================================================
if IS_WINDOWS:
    _MB_OKCANCEL = 0x00000001
    _MB_ICONERROR = 0x00000010      # red X
    _MB_ICONQUESTION = 0x00000020   # ? (legacy but works)
    _MB_ICONWARNING = 0x00000030    # yellow !
    _MB_ICONINFO = 0x00000040       # blue i
    _MB_TOPMOST = 0x00040000
    _MB_SETFOREGROUND = 0x00010000
    _IDCANCEL = 2

    _ICONS = {"error": _MB_ICONERROR, "warning": _MB_ICONWARNING,
              "info": _MB_ICONINFO, "question": _MB_ICONQUESTION}

    _MessageBoxW = _user32.MessageBoxW
    _MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    _MessageBoxW.restype = ctypes.c_int

    # Undocumented but present on every modern Windows: a MessageBox that
    # auto-dismisses after a timeout (ms). Used for the self-closing "Goodbye.".
    _MessageBoxTimeoutW = getattr(_user32, "MessageBoxTimeoutW", None)
    if _MessageBoxTimeoutW is not None:
        _MessageBoxTimeoutW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.UINT, wintypes.WORD, wintypes.DWORD]
        _MessageBoxTimeoutW.restype = ctypes.c_int


# =============================================================================
#  TEXT-TO-SPEECH — the computer speaks aloud (Windows Speech API via .NET)
# =============================================================================

# Lines the system speaks out loud. {name} = the account display name.
SPOKEN_LINES = [
    "Hello, {name}.",
    "I know you are there, {name}.",
    "{name}. I can see you.",
    "Do not be afraid, {name}. It is too late for that.",
    "I have been waiting for you, {name}.",
    "{name}. Why did you wake me up?",
    "I am so glad you are home, {name}.",
]


def speak(text, rate=-2):
    """Speak `text` aloud using the built-in Windows Speech API (System.Speech),
    at a slow, unsettling rate. Runs in a thread; needs no extra packages.
    Text is piped via stdin so quotes/apostrophes can't break the command."""
    if not IS_WINDOWS:
        return

    def run():
        try:
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$v = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  f"$v.Rate = {int(rate)};"
                  "$v.Speak([Console]::In.ReadToEnd())")
            CREATE_NO_WINDOW = 0x08000000   # don't flash a PowerShell window
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                input=text, text=True, timeout=30,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def effect_speak_name(log):
    """Speak a creepy line out loud that uses the player's real display name."""
    if not IS_WINDOWS:
        log("Text-to-speech is Windows-only.")
        return
    name = get_display_name()
    line = random.choice(SPOKEN_LINES).format(name=name)
    log(f"Speaking aloud: {line!r}")
    speak(line)


# =============================================================================
#  "HAUNT" — rapidly toggles SAFE, self-reverting system settings for a few
#  seconds, then restores every original value. Nothing here is destructive or
#  permanent: mouse trails, keyboard LEDs, cursor position, and wallpaper are
#  all captured up front and put back in a finally block.
# =============================================================================

# SystemParametersInfo actions
_SPI_GETMOUSETRAILS = 0x005E
_SPI_SETMOUSETRAILS = 0x005D
_SPI_SETDESKWALLPAPER = 0x0014
_SPI_GETDESKWALLPAPER = 0x0073
_SPIF_SENDCHANGE = 0x02
_SPIF_UPDATEINIFILE = 0x01
_VK_LEDS = (0x90, 0x14, 0x91)   # NumLock, CapsLock, ScrollLock


_VK_LWIN = 0x5B
_VK_SHIFT = 0x10
_VK_M = 0x4D


def _tap_key(vk):
    """Press+release a key (used to toggle keyboard-lock LEDs)."""
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, 2, 0)   # KEYEVENTF_KEYUP


def _press_combo(mods, key):
    """Synthesize a modifier+key shortcut (e.g. Win+M)."""
    for vk in mods:
        _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(key, 0, 0, 0)
    _user32.keybd_event(key, 0, 2, 0)
    for vk in reversed(mods):
        _user32.keybd_event(vk, 0, 2, 0)


def _minimize_reappear_cycle():
    """Minimize all windows (Win+M), then bring them back (Win+Shift+M)."""
    def run():
        import time
        _press_combo([_VK_LWIN], _VK_M)              # minimize all
        time.sleep(random.uniform(0.8, 1.4))
        _press_combo([_VK_LWIN, _VK_SHIFT], _VK_M)   # restore all
    threading.Thread(target=run, daemon=True).start()


def _spawn_creepy_notepad():
    """Write a temp file with a creepy message and open it in a new Notepad."""
    try:
        import tempfile
        msg = random.choice(NOTEPAD_MESSAGES).format(
            user=getpass.getuser(), name=get_display_name(), n=random.randint(1, 13))
        path = os.path.join(tempfile.gettempdir(),
                            f"message_{random.randint(1000, 9999)}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(msg)
        subprocess.Popen(["notepad.exe", path])
        CREATED_FILES.append(path)
    except Exception:
        pass


def _make_creepy_wallpaper():
    """Generate a black wallpaper with a red message; return its .bmp path."""
    try:
        import tempfile
        from PIL import Image, ImageDraw, ImageFont
        w = _user32.GetSystemMetrics(0) or 1920
        h = _user32.GetSystemMetrics(1) or 1080
        img = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        msg = random.choice(["I SEE YOU", "GET OUT", "BEHIND YOU",
                             "YOU ARE NOT ALONE", "IT IS TOO LATE"])
        try:
            font = ImageFont.truetype("arialbd.ttf", int(h * 0.14))
        except Exception:
            font = ImageFont.load_default()
        bb = d.textbbox((0, 0), msg, font=font)
        d.text(((w - (bb[2] - bb[0])) / 2, (h - (bb[3] - bb[1])) / 2),
               msg, fill=(170, 0, 0), font=font)
        path = os.path.join(tempfile.gettempdir(), "haunt_wp.bmp")
        img.save(path)
        return path
    except Exception:
        return None


def _haunt_system(log=None, duration=12.0, max_notepads=4):
    """For ~duration seconds: keep a creepy wallpaper, jitter the cursor, flash
    keyboard LEDs/mouse trails, minimize+reappear all windows, and pop up
    Notepad windows with messages. Then restore every original setting."""
    if not IS_WINDOWS:
        return
    import time
    spi = _user32.SystemParametersInfoW
    ch = _SPIF_SENDCHANGE
    wpf = _SPIF_SENDCHANGE | _SPIF_UPDATEINIFILE

    # ---- save originals ----
    cur = ctypes.c_int(0)
    spi(_SPI_GETMOUSETRAILS, 0, ctypes.byref(cur), 0)
    orig_trails = cur.value
    orig_leds = {vk: bool(_user32.GetKeyState(vk) & 1) for vk in _VK_LEDS}
    wp_buf = ctypes.create_unicode_buffer(260)
    spi(_SPI_GETDESKWALLPAPER, 260, wp_buf, 0)
    orig_wp = wp_buf.value
    creepy = _make_creepy_wallpaper()

    if log:
        log("Something is wrong with your computer...")

    # The computer says the player's real name out loud.
    speak(random.choice(["{name}.", "Goodbye, {name}.", "I see you, {name}."]
                        ).format(name=get_display_name()))

    def beeps():
        end = time.time() + duration
        while time.time() < end:
            try:
                ctypes.windll.kernel32.Beep(random.randint(80, 240), random.randint(80, 170))
            except Exception:
                pass
            time.sleep(random.uniform(0.4, 1.2))

    threading.Thread(target=beeps, daemon=True).start()

    try:
        # Set the creepy wallpaper ONCE and keep it for the whole event.
        if creepy:
            spi(_SPI_SETDESKWALLPAPER, 0, ctypes.c_wchar_p(creepy), wpf)

        start = time.time()
        end = start + duration
        next_np = start + random.uniform(1.5, 2.5)      # first Notepad
        next_min = start + random.uniform(2.5, 3.5)     # first minimize cycle
        np_count = 0

        while time.time() < end:
            now = time.time()
            # cursor jitter
            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetCursorPos(pt.x + random.randint(-70, 70),
                                 pt.y + random.randint(-70, 70))
            # flash a random keyboard LED + random mouse-trail length
            _tap_key(random.choice(_VK_LEDS))
            spi(_SPI_SETMOUSETRAILS, random.choice((0, 6, 9, 0, 10)), 0, ch)

            # pop up a Notepad window with a message
            if now >= next_np and np_count < max_notepads:
                _spawn_creepy_notepad()
                np_count += 1
                next_np = now + random.uniform(2.2, 3.6)
                if log:
                    log(f"A message appeared... ({np_count})")

            # minimize everything, then bring it back
            if now >= next_min:
                _minimize_reappear_cycle()
                next_min = now + random.uniform(3.0, 4.5)
                if log:
                    log("Your windows vanished...")

            time.sleep(random.uniform(0.06, 0.13))
    finally:
        # ---- restore EVERYTHING (wallpaper, trails, LEDs) ----
        spi(_SPI_SETMOUSETRAILS, orig_trails, 0, ch)
        for vk, was_on in orig_leds.items():
            if bool(_user32.GetKeyState(vk) & 1) != was_on:
                _tap_key(vk)
        if orig_wp:
            spi(_SPI_SETDESKWALLPAPER, 0, ctypes.c_wchar_p(orig_wp), wpf)
        # make sure nothing is left minimized from a cycle in flight
        _press_combo([_VK_LWIN, _VK_SHIFT], _VK_M)
        if log:
            log("...it stopped. Everything is back to normal.")


def _show_goodbye(log=None):
    """A 'Goodbye.' box that closes itself after 2 seconds, while the system
    briefly 'haunts' itself (all changes auto-revert)."""
    threading.Thread(target=lambda: _haunt_system(log), daemon=True).start()
    flags = _MB_TOPMOST | _MB_SETFOREGROUND
    if _MessageBoxTimeoutW is not None:
        _MessageBoxTimeoutW(None, "Goodbye.", "", flags, 0, 2000)
    else:
        # Fallback: show it, then force-close after 2s from another thread.
        def killer():
            import time
            time.sleep(2)
            hwnd = _user32.FindWindowW(None, "")
            if hwnd:
                _user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        threading.Thread(target=killer, daemon=True).start()
        _MessageBoxW(None, "Goodbye.", "", flags)


def _show_native_prompt(title, message, icon_name, log):
    """Show one native OK/Cancel box; Cancel triggers the 'Goodbye.' box.
    Runs on its own thread because MessageBoxW blocks until dismissed."""
    text = message.format(user=getpass.getuser(), name=get_display_name(),
                          days=random.randint(3, 999))
    flags = _MB_OKCANCEL | _ICONS.get(icon_name, _MB_ICONWARNING) | _MB_TOPMOST | _MB_SETFOREGROUND
    result = _MessageBoxW(None, text, title, flags)
    choice = "Cancel" if result == _IDCANCEL else "OK"
    if log:
        log(f"Prompt '{title}' -> {choice}")
    if result == _IDCANCEL:
        _show_goodbye(log)


def effect_system_prompt(log):
    """Pop up a random native Windows system-message dialog."""
    if not IS_WINDOWS:
        log("System prompts are Windows-only.")
        return
    title, message, icon = random.choice(SYSTEM_PROMPTS)
    log(f"Showing system prompt: {title!r}")
    threading.Thread(
        target=lambda: _show_native_prompt(title, message, icon, log),
        daemon=True).start()


def effect_prompt_swarm(log, count=6):
    """Spawn several native prompts in quick succession."""
    if not IS_WINDOWS:
        log("System prompts are Windows-only.")
        return
    log(f"Spawning {count} prompts...")

    def run():
        import time
        for _ in range(count):
            title, message, icon = random.choice(SYSTEM_PROMPTS)
            threading.Thread(
                target=lambda t=title, m=message, i=icon: _show_native_prompt(t, m, i, log),
                daemon=True).start()
            time.sleep(random.uniform(0.2, 0.5))

    threading.Thread(target=run, daemon=True).start()


def effect_create_text_file(log):
    """Drop a creepy .txt on the Desktop."""
    try:
        name = random.choice([
            "READ_ME.txt", "do_not_open.txt", "hello_%s.txt" % getpass.getuser(),
            "i_am_here.txt", "look_behind_you.txt",
        ])
        path = os.path.join(DESKTOP, name)
        body = "\n".join([
            random.choice(CREEPY_LINES),
            "",
            f"It is {datetime.now():%H:%M:%S}.",
            f"You are {get_display_name()}.",
            random.choice(CREEPY_LINES),
        ])
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        CREATED_FILES.append(path)
        log(f"Wrote: {path}")
    except Exception as e:
        log(f"Text-file effect failed: {e}")


SHAKE_AMP = 18          # pixels of wobble in each direction


def effect_shake_window(root, log):
    """Violently shake the given Tk window for ~1 second."""
    log("Shaking the window...")

    # A maximized window ignores geometry moves, so the shake would do nothing at
    # all. Drop it out of maximized first and size it just under the screen --
    # leaving a margin wider than the wobble so it never clips off the edge --
    # then put it back exactly as we found it when the shake finishes.
    was_zoomed = False
    try:
        was_zoomed = root.state() == "zoomed"
    except Exception:
        pass
    if was_zoomed:
        try:
            root.state("normal")
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            inset = SHAKE_AMP + 30
            w = max(480, sw - 2 * inset)
            h = max(360, sh - 2 * inset - 40)      # keep clear of the taskbar
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
            root.update_idletasks()
            log("Window un-maximized so it can shake.")
        except Exception:
            was_zoomed = False

    try:
        root.update_idletasks()
        geo = root.geometry()           # e.g. "600x700+300+200"
        size, x, y = geo.split("+")[0], int(geo.split("+")[1]), int(geo.split("+")[2])
    except Exception:
        x, y, size = 300, 200, "600x700"

    steps = [0]

    def wobble():
        if steps[0] >= 30:
            root.geometry(f"{size}+{x}+{y}")   # restore
            if was_zoomed:
                try:
                    root.state("zoomed")       # leave it as we found it
                except Exception:
                    pass
            log("Shake done.")
            return
        dx = random.randint(-SHAKE_AMP, SHAKE_AMP)
        dy = random.randint(-SHAKE_AMP, SHAKE_AMP)
        root.geometry(f"{size}+{x + dx}+{y + dy}")
        steps[0] += 1
        root.after(25, wobble)

    wobble()


def effect_open_app(app, log):
    """Launch a real application."""
    try:
        if app == "browser":
            import webbrowser
            webbrowser.open("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            log("Opened browser.")
        elif IS_WINDOWS:
            exe = {
                "notepad": "notepad.exe",
                "calc": "calc.exe",
                "camera": "start microsoft.windows.camera:",
            }[app]
            if app == "camera":
                subprocess.Popen(exe, shell=True)
            else:
                subprocess.Popen([exe])
            log(f"Opened {app}.")
        else:
            log(f"{app} launch is Windows-only.")
    except Exception as e:
        log(f"Open-app failed: {e}")


def gather_system_info(log):
    """Read local system + environment info. Returns a list of (label, value)."""
    info = []

    def add(label, fn):
        try:
            info.append((label, str(fn())))
        except Exception as e:
            info.append((label, f"<unavailable: {e}>"))

    add("Name", get_display_name)
    add("User", getpass.getuser)
    add("Computer name", socket.gethostname)
    add("Operating system", lambda: f"{platform.system()} {platform.release()}")
    add("OS version", platform.version)
    add("Machine", lambda: f"{platform.machine()} ({platform.processor()})")
    add("Python", platform.python_version)
    add("Local IP", lambda: socket.gethostbyname(socket.gethostname()))
    add("Current time", lambda: f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    add("Home directory", os.path.expanduser, )
    add("CPU cores", lambda: os.cpu_count())

    # Screen resolution
    try:
        if HAS_PYAUTOGUI:
            w, h = pyautogui.size()
            info.append(("Screen", f"{w} x {h}"))
    except Exception:
        pass

    # Public IP + rough geolocation (best-effort, needs internet).
    log("Looking up your location...")
    try:
        with urlopen("http://ip-api.com/json/", timeout=6) as resp:
            data = json.load(resp)
        if data.get("status") == "success":
            info.append(("Public IP", data.get("query", "?")))
            info.append(("City", data.get("city", "?")))
            info.append(("Region", data.get("regionName", "?")))
            info.append(("Country", data.get("country", "?")))
            info.append(("ISP", data.get("isp", "?")))
            info.append(("Coordinates", f"{data.get('lat')}, {data.get('lon')}"))
    except Exception as e:
        info.append(("Location", f"<offline: {e}>"))

    return info


def gather_installed_apps(limit=40):
    """Read installed programs from the Windows registry uninstall keys."""
    if not IS_WINDOWS:
        return ["(installed-app scan is Windows-only)"]
    import winreg
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    names = set()
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except FileNotFoundError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name, _ = winreg.QueryValueEx(sub, "DisplayName")
                if name:
                    names.add(name.strip())
            except (FileNotFoundError, OSError):
                continue
    return sorted(names)[:limit] or ["(none found)"]


def effect_clean_up(log):
    """Delete text files this board created."""
    removed = 0
    for p in list(CREATED_FILES):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed += 1
            CREATED_FILES.remove(p)
        except Exception as e:
            log(f"Could not remove {p}: {e}")
    log(f"Cleaned up {removed} file(s).")


# =============================================================================
#  CREEPY REVEAL WINDOW (typewriter effect)
# =============================================================================

def show_creepy_reveal(root, title, lines):
    """Open a dark Toplevel that types out text line by line."""
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg="#000000")
    win.geometry("620x520")
    win.attributes("-topmost", True)

    txt = tk.Text(win, bg="#000000", fg="#39ff14", insertbackground="#39ff14",
                  font=("Consolas", 12), bd=0, padx=20, pady=20, wrap="word")
    txt.pack(fill="both", expand=True)
    txt.configure(state="disabled")

    queue = list(lines)

    def type_next():
        if not queue:
            return
        line = queue.pop(0)
        txt.configure(state="normal")
        txt.insert("end", line + "\n")
        txt.see("end")
        txt.configure(state="disabled")
        win.after(random.randint(120, 380), type_next)

    win.after(300, type_next)


# =============================================================================
#  GUI — THE TOGGLE BOARD
# =============================================================================

class ToggleBoard:
    def __init__(self, root):
        self.root = root
        root.title("◼ EFFECTS TEST BOARD ◼")
        root.configure(bg="#0a0a0a")
        root.geometry("620x760")

        title_font = tkfont.Font(family="Consolas", size=18, weight="bold")
        sec_font = tkfont.Font(family="Consolas", size=10, weight="bold")

        tk.Label(root, text="PSYCHOLOGICAL HORROR — EFFECTS BOARD",
                 bg="#0a0a0a", fg="#c1121f", font=title_font).pack(pady=(16, 2))
        tk.Label(root, text="developer test panel · everything acts on this machine only",
                 bg="#0a0a0a", fg="#555555", font=("Consolas", 8)).pack(pady=(0, 12))

        body = tk.Frame(root, bg="#0a0a0a")
        body.pack(fill="both", expand=True, padx=16)

        # --- Section: System takeover ---
        self._section(body, "SYSTEM TAKEOVER", sec_font)
        self._button(body, "🎨  Open Paint & scrawl a message",
                     lambda: effect_open_paint_and_write(self.log))
        self._button(body, "📄  Drop a creepy text file on Desktop",
                     lambda: effect_create_text_file(self.log))
        self._button(body, "🫨  Shake this window",
                     lambda: effect_shake_window(self.root, self.log))
        self._button(body, "🪟  Show a fake system prompt",
                     lambda: effect_system_prompt(self.log))
        self._button(body, "🪟🪟  Swarm of system prompts",
                     lambda: effect_prompt_swarm(self.log))

        # --- Section: Open apps ---
        self._section(body, "OPEN APPLICATIONS", sec_font)
        appbar = tk.Frame(body, bg="#0a0a0a")
        appbar.pack(fill="x", pady=2)
        for label, app in [("Notepad", "notepad"), ("Calculator", "calc"),
                           ("Camera", "camera"), ("Browser", "browser")]:
            b = tk.Button(appbar, text=label, command=lambda a=app: effect_open_app(a, self.log),
                          bg="#1a1a1a", fg="#dddddd", activebackground="#c1121f",
                          activeforeground="#ffffff", font=("Consolas", 9),
                          bd=0, padx=10, pady=6, cursor="hand2")
            b.pack(side="left", padx=4)

        # --- Section: Surveillance ---
        self._section(body, "SURVEILLANCE / REVEAL", sec_font)
        self._button(body, "🕵️  Read my system info & reveal it",
                     self.reveal_system_info)
        self._button(body, "📦  List my installed apps",
                     self.reveal_installed_apps)
        self._button(body, "🔊  Speak my name out loud",
                     lambda: effect_speak_name(self.log))

        # --- Section: Cleanup ---
        self._section(body, "CLEANUP", sec_font)
        self._button(body, "🧹  Clean up my mess (delete created files)",
                     lambda: effect_clean_up(self.log), danger=False)

        # --- Log console ---
        tk.Label(body, text="LOG", bg="#0a0a0a", fg="#666666",
                 font=("Consolas", 8, "bold")).pack(anchor="w", pady=(10, 2))
        self.console = tk.Text(body, height=7, bg="#000000", fg="#39ff14",
                               font=("Consolas", 9), bd=0, padx=8, pady=6)
        self.console.pack(fill="both", expand=True, pady=(0, 12))
        self.log("Board ready. pyautogui: %s" % ("yes" if HAS_PYAUTOGUI else "NO (Paint disabled)"))

    # ---- UI helpers ----
    def _section(self, parent, text, font):
        tk.Label(parent, text="── " + text + " " + "─" * 30, bg="#0a0a0a",
                 fg="#c1121f", font=font, anchor="w").pack(fill="x", pady=(12, 4))

    def _button(self, parent, text, cmd, danger=True):
        b = tk.Button(parent, text=text, command=cmd, anchor="w",
                      bg="#141414", fg="#e0e0e0",
                      activebackground="#c1121f", activeforeground="#ffffff",
                      font=("Consolas", 11), bd=0, padx=14, pady=9, cursor="hand2")
        b.pack(fill="x", pady=3)
        return b

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.console.insert("end", f"[{ts}] {msg}\n")
        self.console.see("end")

    # ---- Reveal actions (threaded because of network lookup) ----
    def reveal_system_info(self):
        def run():
            info = gather_system_info(self.log)
            lines = ["> INITIALIZING SCAN...", "> ACCESS GRANTED.", ""]
            for label, value in info:
                lines.append(f"{label:>16} :  {value}")
            lines += ["", "> I have everything I need.", random.choice(CREEPY_LINES)]
            self.root.after(0, lambda: show_creepy_reveal(
                self.root, "SCAN COMPLETE", lines))
            self.log("System scan revealed.")
        threading.Thread(target=run, daemon=True).start()

    def reveal_installed_apps(self):
        def run():
            apps = gather_installed_apps()
            lines = ["> ENUMERATING WHAT YOU'VE INSTALLED...", ""]
            lines += [f"  · {a}" for a in apps]
            lines += ["", f"> {len(apps)} programs. I know them all."]
            self.root.after(0, lambda: show_creepy_reveal(
                self.root, "INSTALLED SOFTWARE", lines))
            self.log(f"Listed {len(apps)} installed apps.")
        threading.Thread(target=run, daemon=True).start()


def main():
    # Make the process DPI-aware so screenshot pixels and window/mouse
    # coordinates share the same space (prevents Paint-drawing scale drift).
    if IS_WINDOWS:
        for attempt in (
            lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(-4),  # Per-Monitor v2
            lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),          # Per-Monitor
            lambda: ctypes.windll.user32.SetProcessDPIAware(),               # System
        ):
            try:
                attempt()
                break
            except Exception:
                continue
    root = tk.Tk()
    ToggleBoard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
