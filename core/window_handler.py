import os
import sys
import logging
import psutil


def _load_target_list():
    targets = {"processes": [], "titles": []}
    if getattr(sys, 'frozen', False):
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    target_file = os.path.join(base_dir, "data", "target_windows.txt")
    if not os.path.exists(target_file):
        logging.warning("target_windows.txt not found, using defaults")
        return ["bluearchive", "notepad", "wine-preloader"], ["blue archive", "блокнот", "notepad"]

    try:
        with open(target_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: process_name | window_title  (either side optional)
                if "|" in line:
                    proc_part, _, title_part = line.partition("|")
                    proc = proc_part.strip().lower().replace(".exe", "")
                    title = title_part.strip().lower()
                    if proc:
                        targets["processes"].append(proc)
                    if title:
                        targets["titles"].append(title)
                    continue
                lower = line.lower()
                if lower.endswith(".exe"):
                    targets["processes"].append(lower.replace(".exe", ""))
                elif " " in lower:
                    # Phrases with spaces are window titles, not process names
                    targets["titles"].append(lower)
                else:
                    targets["processes"].append(lower)
    except Exception:
        logging.exception("Failed to load target_windows.txt")

    if not targets["processes"] and not targets["titles"]:
        return ["bluearchive", "notepad", "wine-preloader"], ["blue archive", "блокнот", "notepad"]

    return targets["processes"], targets["titles"]


TARGET_PROCESSES, TARGET_TITLES = _load_target_list()

_win32_available = False
if sys.platform == "win32":
    try:
        import win32gui
        import win32process
        _win32_available = True
    except ImportError:
        logging.warning("win32gui/win32process not installed. Window detection will be limited.")


def _is_target_by_hwnd(hwnd):
    if not _win32_available:
        return False
    try:
        title = win32gui.GetWindowText(hwnd).lower()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc_name = psutil.Process(pid).name().lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            proc_name = ""
        return any(tp in proc_name for tp in TARGET_PROCESSES) or any(tt in title for tt in TARGET_TITLES)
    except Exception:
        return False


# Matches the target process but must never be treated as the game surface:
# BlueStacks' key-mapping overlay is invisible, nearly fullscreen and often
# topmost — its "target" status let foreign text pass occlusion and its rect
# became the auto-capture region (wrong title, wrong strip cut).
_SKIP_TITLES = ("keymap overlay",)


def _skipped_title(hwnd):
    if not _win32_available:
        return False
    try:
        t = win32gui.GetWindowText(hwnd).lower()
        return any(s in t for s in _SKIP_TITLES)
    except Exception:
        return False


def _geom_from_hwnd(hwnd):
    if not _win32_available:
        return None
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > 0 and h > 0:
            return {"x": rect[0], "y": rect[1], "w": w, "h": h}
    except Exception:
        pass
    return None


def get_window_at_cursor(x, y):
    if sys.platform == "win32":
        if not _win32_available:
            return None
        try:
            hwnd = win32gui.WindowFromPoint((x, y))
            if not hwnd:
                return None
            root_hwnd = win32gui.GetAncestor(hwnd, 2)
            if root_hwnd:
                hwnd = root_hwnd
            if _is_target_by_hwnd(hwnd):
                return _geom_from_hwnd(hwnd)
        except Exception:
            logging.exception("Failed to get window at cursor (win32)")
        return None
    else:
        try:
            import Xlib.display
            disp = Xlib.display.Display()
            root = disp.screen().root
            pointer = root.query_pointer()
            child = pointer.child
            if not child:
                return None
            title = child.get_wm_name()
            if title:
                title = title.lower()
            else:
                title = ""
            pid_property = child.get_full_property(disp.intern_atom('_NET_WM_PID'), Xlib.X.AnyPropertyType)
            proc_name = ""
            if pid_property:
                pid = pid_property.value[0] if isinstance(pid_property.value, (list, tuple)) else pid_property.value
                try:
                    proc_name = psutil.Process(pid).name().lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    proc_name = ""
            is_target = any(tp in proc_name for tp in TARGET_PROCESSES) or any(tt in title for tt in TARGET_TITLES)
            if is_target:
                geom = child.get_geometry()
                return {"x": geom.x, "y": geom.y, "w": geom.width, "h": geom.height}
        except Exception:
            logging.exception("Failed to get window at cursor (X11)")
    return None


def _is_transparent_or_cloaked(hwnd):
    """DWM-cloaked or WS_EX_TRANSPARENT windows draw nothing (touch keyboard,
    hidden UWP frames) — they must not count as occluders."""
    try:
        import ctypes
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            int(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
        if val.value:
            return True
    except Exception:
        pass
    try:
        ex = win32gui.GetWindowLong(int(hwnd), win32gui.GWL_EXSTYLE)
        if ex & 0x20:  # WS_EX_TRANSPARENT
            return True
    except Exception:
        pass
    return False


def get_visible_windows(region):
    """Visible top-level windows intersecting `region`, in z-order
    (topmost first), as [(l, t, r, b, is_target)].

    A pixel belongs to the FIRST window in this list that contains it —
    that is the window actually shown there. Text inside a non-target
    window (terminal, browser, our own UI) is not ours to translate.
    Works even when the target window moved or the region is stale."""
    if not _win32_available or not region:
        return []
    rl = int(region.get("left", 0))
    rt = int(region.get("top", 0))
    rr = rl + int(region.get("width", 0))
    rb = rt + int(region.get("height", 0))
    if rr <= rl or rb <= rt:
        return []
    out = []

    def cb(hwnd, _extra):
        # NOTE: never return False here — EnumWindows stops with error(0).
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        if r <= rl or l >= rr or b <= rt or t >= rb:
            return True
        if _is_transparent_or_cloaked(hwnd):
            return True
        try:
            title = win32gui.GetWindowText(hwnd).lower()
            if any(s in title for s in _SKIP_TITLES):
                # Invisible pass-through overlay: occludes nothing (its
                # "target" flag used to let foreign windows below it count
                # as game content — our settings UI got translated).
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname = psutil.Process(pid).name().lower()
        except Exception:
            return True
        is_target = (any(tp in pname for tp in TARGET_PROCESSES)
                     or any(tt in title for tt in TARGET_TITLES))
        out.append((max(l, rl), max(t, rt), min(r, rr), min(b, rb), is_target))
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        logging.exception("get_visible_windows failed")
        return []
    return out


def is_target_window(hwnd):
    """Public check: does this hwnd belong to the game/target window set?"""
    return _is_target_by_hwnd(hwnd)


def get_own_window_rects():
    """Rects of OUR OWN visible top-level windows (settings UI, hint card,
    translation overlays). Whatever OCR reads inside them is our own UI,
    never game text — the dialogue filter treats them as a hard deny-list.
    Own overlay windows are WS_EX_TRANSPARENT (invisible to WindowFromPoint)
    but they still paint pixels, so they must be listed here explicitly."""
    if not _win32_available:
        return []
    out = []
    our_pid = os.getpid()

    def cb(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid != our_pid:
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if r > l and b > t:
                out.append((l, t, r, b))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        logging.exception("get_own_window_rects failed")
    return out


def top_window_at(x, y):
    """Topmost window at a point via WindowFromPoint (ground-truth z-order),
    resolved to its top-level root hwnd, or 0. WS_EX_TRANSPARENT windows
    are skipped by the OS — the window actually painting there wins."""
    if not _win32_available:
        return 0
    try:
        hw = win32gui.WindowFromPoint((int(x), int(y)))
        if not hw:
            return 0
        root = win32gui.GetAncestor(hw, 2)  # GA_ROOT
        return root or hw
    except Exception:
        return 0


def find_target_window_region():
    """Region dict (left/top/width/height/title/hwnd) for the foreground
    target window, or else for any visible target window. Used by the
    auto-capture loop so translation starts without a manual click.
    Returns None while no game window exists (incl. minimized)."""
    if sys.platform != "win32" or not _win32_available:
        return None
    import ctypes
    import ctypes.wintypes
    user32 = ctypes.windll.user32

    def build(hwnd):
        try:
            if user32.IsIconic(hwnd):
                return None
            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    return {"left": rect.left, "top": rect.top, "width": w,
                            "height": h, "title": buf.value, "hwnd": hwnd}
        except Exception:
            pass
        return None

    try:
        fg = user32.GetForegroundWindow()
        if fg and _is_target_by_hwnd(fg) and not _skipped_title(fg):
            reg = build(fg)
            if reg:
                return reg
        found = []

        def cb(hwnd, _extra):
            try:
                if (win32gui.IsWindowVisible(hwnd)
                        and _is_target_by_hwnd(hwnd)
                        and not _skipped_title(hwnd)):
                    reg = build(hwnd)
                    if reg and reg["width"] >= 200 and reg["height"] >= 200:
                        found.append(reg)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return found[0] if found else None
    except Exception:
        logging.exception("find_target_window_region failed")
        return None


def get_active_target_window():
    if sys.platform == "win32":
        if not _win32_available:
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd and _is_target_by_hwnd(hwnd):
                return _geom_from_hwnd(hwnd)
        except Exception:
            logging.exception("Failed to detect active target window (win32)")
        return None
    else:
        try:
            import Xlib.display
            disp = Xlib.display.Display()
            root = disp.screen().root
            active_window_id = root.get_full_property(disp.intern_atom('_NET_ACTIVE_WINDOW'), Xlib.X.AnyPropertyType).value
            window = disp.create_resource_object('window', active_window_id)
            title = window.get_wm_name().lower()
            pid_property = window.get_full_property(disp.intern_atom('_NET_WM_PID'), Xlib.X.AnyPropertyType)
            proc_name = ""
            if pid_property:
                pid = pid_property.value[0] if isinstance(pid_property.value, (list, tuple)) else pid_property.value
                proc_name = psutil.Process(pid).name().lower()
            if any(tp in proc_name for tp in TARGET_PROCESSES) or any(tt in title for tt in TARGET_TITLES):
                geom = window.get_geometry()
                return {"x": geom.x, "y": geom.y, "w": geom.width, "h": geom.height}
        except Exception:
            logging.exception("Failed to detect active target window (X11)")
    return None
