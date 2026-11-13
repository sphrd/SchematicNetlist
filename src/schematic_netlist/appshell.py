"""App shell: drawn title bar with **About** / **Check for updates**, the app dialog frame, and
self-update from the GitHub releases of this app (download, verify, swap the exe, relaunch).

Generic: the app passes an `AppInfo` and its UI module (theme tokens, `_RoundButton`,
`_paint_round_rect`). Windows only for the drawn title bar; elsewhere the native one stays.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageFont, ImageTk

TITLE_H = 36
CLOSE_HOVER = "#c42b1c"
# design tokens shared by every app of this family (the apps' gui.py repeat them for their widgets)
TOKENS = {
    "bg": "#222831", "surface": "#2d333d", "border": "#3a414c", "fg": "#ffffff", "muted": "#aab4c0",
    "disabled_fg": "#6b7480", "accent": "#4da3ff", "accent_light": "#7bbcff",
}


@dataclass
class AppInfo:
    name: str
    version: str
    author: str
    description: str  # one line, shown in About
    licence: str  # e.g. "Free and open source (MIT licence)."
    repo: str  # "<owner>/<repo>" on GitHub
    asset: str  # release asset of this platform, e.g. "<App>.exe"
    icon_png: Path
    private: bool = False  # private repository: use the GitHub sign-in found on this PC
    shortcuts: list[tuple[str, str]] = field(default_factory=lambda: [("F1", "About")])


# --------------------------------------------------------------------------- updater (no Tk)
@dataclass
class Release:
    version: tuple
    tag: str
    page: str
    asset_url: Optional[str] = None  # API asset URL (works for private and public repositories)
    size: int = 0
    sha256: Optional[str] = None


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", text)[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def short(v: tuple) -> str:
    return ".".join(str(n) for n in (v if v[3] else v[:3]))


def exe_path() -> Optional[Path]:
    """The running exe when this is a built (frozen) app, else None (source run: nothing to swap)."""
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Release assets redirect to a signed storage URL that rejects the GitHub token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _github_tokens() -> list[str]:
    """Tokens of the GitHub sign-ins on this PC, best first: GH_TOKEN/GITHUB_TOKEN, GitHub CLI, Git
    credential manager. Never prompts (a background check must not open a login window)."""
    out: list[str] = [t for t in (os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")) if t]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True,
                           timeout=10, creationflags=flags, env=env)
        if r.returncode == 0 and r.stdout.strip():
            out.append(r.stdout.strip())
        # every other account signed in to the GitHub CLI (work and personal accounts side by side)
        st = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"], capture_output=True, text=True,
                            timeout=10, creationflags=flags, env=env)
        for user in re.findall(r"account (\S+)", st.stdout + st.stderr):
            r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com", "--user", user], capture_output=True,
                               text=True, timeout=10, creationflags=flags, env=env)
            if r.returncode == 0 and r.stdout.strip():
                out.append(r.stdout.strip())
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=15, creationflags=flags, env=env)
        m = re.search(r"^password=(.+)$", r.stdout, re.M)
        if r.returncode == 0 and m:
            out.append(m.group(1).strip())
    return list(dict.fromkeys(out))


def _request(url: str, info: AppInfo, token: Optional[str], accept: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{info.name}/{info.version}"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return _opener.open(req, timeout=timeout)


def get_latest(info: AppInfo) -> tuple[Release, Optional[str]]:
    """Latest published release and the token that could read it (None for a public repository)."""
    url = f"https://api.github.com/repos/{info.repo}/releases/latest"
    tokens: list[Optional[str]] = ([] if not info.private else _github_tokens()) + [None]
    last: Optional[Exception] = None
    for tok in tokens:
        try:
            with _request(url, info, tok, "application/vnd.github+json") as resp:
                doc = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (401, 403, 404):
                raise
    else:
        if info.private and isinstance(last, urllib.error.HTTPError) and last.code in (401, 404):
            raise UpdateError(
                f"sign in to GitHub with an account that can see {info.repo} (Git or the GitHub CLI), then try again"
            )
        raise last or UpdateError("no answer from GitHub")
    rel = Release(parse_version(doc["tag_name"]), doc["tag_name"], doc["html_url"])
    for a in doc.get("assets", []):
        if a.get("name", "").lower() == info.asset.lower():
            rel.asset_url, rel.size = a["url"], int(a.get("size") or 0)
            d = a.get("digest") or ""
            rel.sha256 = d[7:].lower() if d.startswith("sha256:") else None
    return rel, tok


def folder_writable(folder: Path) -> bool:
    probe = folder / f".update-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def download(info: AppInfo, rel: Release, token: Optional[str], progress: Callable[[int, int], None],
             cancel: threading.Event) -> Path:
    """Streams the release exe to `<exe>.download`, verifying size and SHA-256 (when GitHub gives one)."""
    target = exe_path().with_name(exe_path().name + ".download")
    h = hashlib.sha256()
    done = 0
    with _request(rel.asset_url, info, token, "application/octet-stream", timeout=30) as resp, open(target, "wb") as f:
        total = rel.size or int(resp.headers.get("Content-Length") or 0)
        while chunk := resp.read(1 << 16):
            if cancel.is_set():
                raise InterruptedError
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    if rel.size and done != rel.size:
        raise UpdateError(f"download incomplete ({done:,} of {rel.size:,} bytes)")
    if rel.sha256 and h.hexdigest() != rel.sha256:
        raise UpdateError("downloaded file does not match the release checksum")
    return target


def swap_in(downloaded: Path) -> None:
    """A running exe can be renamed but not overwritten: exe -> exe.old, download -> exe (rolled back on failure)."""
    exe = exe_path()
    old = exe.with_name(exe.name + ".old")
    if old.exists():
        old.unlink()
    exe.rename(old)
    try:
        downloaded.rename(exe)
    except OSError:
        old.rename(exe)
        raise


def cleanup_leftovers() -> None:
    """Removes the replaced exe and unfinished downloads of an earlier update; right after a restart the
    previous process may still hold the old exe, so this retries in the background for ~15 s."""
    exe = exe_path()
    if exe is None:
        return

    def attempt() -> bool:
        ok = True
        for p in (exe.with_name(exe.name + ".old"), exe.with_name(exe.name + ".download")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                ok = False
        return ok

    def retry() -> None:
        for _ in range(30):
            if attempt():
                return
            time.sleep(0.5)

    if not attempt():
        threading.Thread(target=retry, daemon=True).start()


def relaunch() -> None:
    """Starts the (updated) exe as a new, independent app instance."""
    exe = exe_path()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # a new one-file instance, not a child of this one
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, close_fds=True, creationflags=flags)


def cli_update(info: AppInfo, restart: bool = False) -> int:
    """Headless update (`<App>.exe --cli update [--restart]`): same lookup, verification and swap as the window."""
    rel, tok = get_latest(info)
    current = parse_version(info.version)
    if rel.version <= current:
        print(f"Up to date: version {short(current)} is the latest version.")
        return 0
    exe = exe_path()
    if exe is None or not rel.asset_url:
        print(f"Version {short(rel.version)} is available: {rel.page}")
        return 1
    if not folder_writable(exe.parent):
        print(f"Can't update here ({exe.parent} is read-only): {rel.page}")
        return 1
    print(f"Downloading version {short(rel.version)} ...")
    swap_in(download(info, rel, tok, lambda d, t: None, threading.Event()))
    print(f"Version {short(rel.version)} is installed.")
    if restart:
        relaunch()
    return 0


def _first_line(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"GitHub answered {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"no connection to GitHub ({e.reason})"
    return (str(e).splitlines() or [type(e).__name__])[0].rstrip(".")


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --------------------------------------------------------------------------- flair (busy states, brand moments)
def _rgb(c: str) -> tuple:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(ra, rb))


def pil_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Segoe UI at `size` PIXELS (the splash and other generated art are laid out in pixels)."""
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        with contextlib.suppress(OSError):
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def comet_frames(size: int, bg: str, color: str = TOKENS["accent"], frames: int = 60, sweep: float = 270.0) -> list[Image.Image]:
    """Comet spinner frames (one turn): a line whose tail fades in towards its head; nothing at the
    head (no dot). Stroke max(2.5, 5*size/64), inset 1.5 strokes."""
    s = 4
    n = size * s
    w = max(2.5, 5 * size / 64) * s
    inset = 1.5 * w
    box = (inset, inset, n - inset, n - inset)
    base = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    steps, step = 28, sweep / 28
    for i in range(steps):
        k = (i + 1) / steps
        a = int(255 * k * k)
        if a <= 0:
            continue
        layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        ImageDraw.Draw(layer).arc(box, -sweep + i * step, -sweep + (i + 1) * step + 0.6, fill=color + "%02x" % a,
                                  width=int(round(w)))
        base.alpha_composite(layer)
    out = []
    for f in range(frames):
        fr = base.rotate(-360.0 * f / frames, resample=Image.BICUBIC)  # clockwise
        img = Image.new("RGBA", (n, n), bg)
        img.alpha_composite(fr)
        out.append(img.resize((size, size), Image.LANCZOS).convert("RGB"))
    return out


class CometSpinner(tk.Label):
    """Comet spinner (one turn per second); animates only between start() and stop()."""

    _cache: dict = {}

    def __init__(self, parent: tk.Misc, size: int, bg: str) -> None:
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0)
        key = (size, bg)
        if key not in CometSpinner._cache:
            CometSpinner._cache[key] = [ImageTk.PhotoImage(f) for f in comet_frames(size, bg)]
        self._frames = CometSpinner._cache[key]
        self.configure(image=self._frames[0])
        self._job: Optional[str] = None
        self._t0 = 0.0

    def start(self) -> None:
        if self._job is None:
            self._t0 = time.monotonic()
            self._tick()

    def stop(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def _tick(self) -> None:
        t = time.monotonic() - self._t0
        self.configure(image=self._frames[int(t * len(self._frames)) % len(self._frames)])
        self._job = self.after(16, self._tick)


def progress_image(width: int, fraction: float, t: float, bg: str, height: int = 6, glow_room: int = 0) -> Image.Image:
    """Pill progress bar: `border` track, `accent` fill with a sweep (accent-light, one pass per 1.6 s) gliding
    over the fill."""
    s = 2
    W, H = width * s, (height + 2 * glow_room) * s
    y0, y1 = glow_room * s, (glow_room + height) * s
    img = Image.new("RGBA", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, y0, W - 1, y1 - 1), radius=height * s // 2, fill=TOKENS["border"])
    fw = int(round(W * max(0.0, min(1.0, fraction))))
    if fw > 0:
        fw = max(fw, height * s)
        d.rounded_rectangle((0, y0, fw - 1, y1 - 1), radius=height * s // 2, fill=TOKENS["accent"])
        seg = 110 * s
        pos = -seg + (t % 1.6) / 1.6 * (fw + seg)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, y0, fw - 1, y1 - 1), radius=height * s // 2, fill=255)
        sweep = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sweep)
        for x in range(max(0, int(pos)), min(fw, int(pos + seg))):
            u = (x - pos) / seg
            a = max(0, int(153 * (1 - abs(2 * u - 1))))  # ~60 % at the centre of the highlight
            sd.line((x, y0, x, y1), fill=TOKENS["accent_light"] + "%02x" % a)
        sweep.putalpha(Image.composite(sweep.getchannel("A"), mask, mask))
        img.alpha_composite(sweep)
    return img.resize((width, height + 2 * glow_room), Image.LANCZOS).convert("RGB")


# --------------------------------------------------------------------------- splash (bundled exe)
def draw_splash(name: str, author: str, version: str, description: str, icon: Image.Image,
                width: int = 520, height: int = 300) -> Image.Image:
    """Static part of the start-up splash (App Engineer 2.4, all sizes in pixels): logo, name, by-line
    with the version pill, accent bar, divider and description. The bootloader shows it; the loading log,
    its spinner and the divider sweep are drawn live by `splash_tcl()`."""
    T = TOKENS
    img = Image.new("RGBA", (width, height), T["bg"])
    logo = icon.convert("RGBA").resize((96, 96), Image.LANCZOS)
    img.alpha_composite(logo, (36, 44))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, width - 1, height - 1), outline=T["surface"], width=2)
    size = 30
    while size > 20 and 156 + d.textlength(name, font=pil_font(size, True)) > width - 36:
        size -= 1
    d.text((156, 50), name, font=pil_font(size, True), fill=T["fg"])
    by = f"by {author}"
    d.text((158, 97), by, font=pil_font(14), fill=T["muted"])
    pill_text = f"v{version}"
    pf = pil_font(12, True)
    px = 158 + int(d.textlength(by, font=pil_font(14))) + 12
    pw = int(d.textlength(pill_text, font=pf)) + 18
    d.rounded_rectangle((px, 96, px + pw, 118), radius=11, fill=T["surface"], outline=T["border"])
    # baseline so the capitals/digits sit in the pill's middle ("mm" centres ascender..descender: too low)
    d.text((px + pw / 2, 107 + round(CAP_HEIGHT * 12 / 2)), pill_text, font=pf, fill=T["accent"], anchor="ms")
    d.rounded_rectangle((158, 130, 202, 134), radius=2, fill=T["accent"])
    d.rectangle((36, 158, width - 36, 159), fill=T["surface"])
    d.text((36, 172), description, font=pil_font(14), fill=T["fg"])
    return img.convert("RGB")


def splash_tcl() -> str:
    """Tcl for the PyInstaller splash (replaces its single status line): a loading log of the last 4 steps
    (finished ones with a check mark, the current one with a comet spinner) and the accent sweep along the
    divider, animated on the splash's own thread. Every update_text() / unpacked file is a new step.
    A click or Esc closes it (used by `--splash` preview). Goes through PyInstaller's %-formatting."""
    T = {k: v for k, v in TOKENS.items()}
    T["check"] = _mix(T["bg"], T["accent"], 0.63)
    tcl = r"""
font create logFont -family {Segoe UI} -size -12
set _log {}
set _cur ""
set _t0 [clock milliseconds]
proc _mix {a b t} {
    scan $a "#%%2x%%2x%%2x" r1 g1 b1
    scan $b "#%%2x%%2x%%2x" r2 g2 b2
    return [format "#%%02x%%02x%%02x" [expr {int($r1+($r2-$r1)*$t)}] [expr {int($g1+($g2-$g1)*$t)}] [expr {int($b1+($b2-$b1)*$t)}]]
}
proc _fit {s w} {
    if {[font measure logFont $s] <= $w} {return $s}
    while {[string length $s] > 1 && [font measure logFont "$s..."] > $w} {set s [string range $s 0 end-1]}
    return "$s..."
}
proc _done {s} {
    if {$s eq "Starting..."} {return "Started"}
    foreach {a b} {"Unpacking " "Unpacked " "Loading " "Loaded " "Starting " "Started " "Building " "Built " "Opening " "Opened "} {
        if {[string first $a $s] == 0} {return "$b[string range $s [string length $a] end]"}
    }
    return $s
}
proc _status {args} {
    global status_text _log _cur
    set s [string trim $status_text]
    if {$s eq "" || $s eq $_cur} return
    if {[string first " " $s] < 0 && ![string match "*..." $s]} {set s "Unpacking $s"}
    if {$_cur ne ""} {
        lappend _log [_done $_cur]
        set _log [lrange $_log end-2 end]
    }
    set _cur $s
    if {[string first "Preview" $s] == 0} {focus -force .}
    _draw_log
}
proc _draw_log {} {
    global _log _cur
    set c .root.canvas
    $c delete logtext
    set lines $_log
    if {$_cur ne ""} {lappend lines $_cur}
    set n [llength $lines]
    for {set i 0} {$i < $n} {incr i} {
        set y [expr {206 + 18*$i}]
        set last [expr {$i == $n-1 && $_cur ne ""}]
        set fg [expr {$last ? "@muted@" : "@disabled_fg@"}]
        $c create text 56 $y -anchor nw -font logFont -fill $fg -text [_fit [lindex $lines $i] 428] -tags logtext
        if {!$last} {
            $c create line 38.1 [expr {$y+8.05}] 41.4 [expr {$y+11.35}] 47.45 [expr {$y+3.65}] -fill @check@ -width 1.6 -capstyle round -joinstyle round -tags logtext
        }
    }
}
proc _tick {} {
    global _t0 _log _cur
    set c .root.canvas
    set t [expr {([clock milliseconds] - $_t0) / 1000.0}]
    $c delete anim
    set x0 36.0
    set x1 484.0
    set seg 140.0
    set pos [expr {$x0 - $seg + fmod($t, 1.6) / 1.6 * ($x1 - $x0 + $seg)}]
    for {set k 0} {$k < 28} {incr k} {
        set a [expr {$pos + $seg*$k/28.0}]
        set b [expr {$pos + $seg*($k+1)/28.0}]
        if {$b <= $x0 || $a >= $x1} continue
        if {$a < $x0} {set a $x0}
        if {$b > $x1} {set b $x1}
        set u [expr {($k+0.5)/28.0}]
        $c create rectangle $a 158 $b 160 -fill [_mix @surface@ @accent@ [expr {1.0-abs(2*$u-1)}]] -width 0 -tags anim
    }
    if {$_cur ne ""} {
        set y [expr {206 + 18*[llength $_log]}]
        set head [expr {-fmod($t*360.0, 360.0)}]
        set step [expr {270.0/28}]
        for {set k 0} {$k < 28} {incr k} {
            set q [expr {($k+1.0)/28}]
            $c create arc 38 [expr {$y+3}] 47 [expr {$y+12}] -style arc -start [expr {$head + 270.0 - ($k+1)*$step}] -extent [expr {$step+0.6}] -outline [_mix @bg@ @accent@ [expr {$q*$q}]] -width 2 -tags anim
        }
    }
    after 30 _tick
}
bind . <Button-1> {exit}
bind . <Escape> {exit}
trace add variable status_text write _status
set status_text "%(default_text)s"
_tick
"""
    for k, v in T.items():
        tcl = tcl.replace(f"@{k}@", v)
    return tcl


def patch_splash_template() -> None:
    """Build time: make PyInstaller's splash draw `splash_tcl()` instead of its one status line."""
    from PyInstaller.building import splash_templates

    splash_templates.splash_canvas_text = splash_tcl()


def splash_step(text: str) -> None:
    """Adds a line to the bundled exe's splash log (no-op when not running from the exe or after it closed)."""
    with contextlib.suppress(Exception):
        import pyi_splash  # pyright: ignore[reportMissingModuleSource]  # only exists inside the PyInstaller build

        if pyi_splash.is_alive():
            pyi_splash.update_text(text)


def close_splash(app: Optional[tk.Tk] = None) -> None:
    with contextlib.suppress(Exception):
        import pyi_splash  # pyright: ignore[reportMissingModuleSource]

        pyi_splash.close()
    if app is not None:
        with contextlib.suppress(tk.TclError):
            app.lift()
            app.focus_force()


def splash_preview(modules: list[str]) -> int:
    """`<App>.exe --splash`: keeps the splash on screen, listing the loaded modules, until it is clicked or
    Esc is pressed (no main window)."""
    try:
        import pyi_splash  # pyright: ignore[reportMissingModuleSource]
    except ImportError:
        print("--splash shows the start-up splash of the built exe; there is none in a source run.")
        return 1
    for m in modules:
        splash_step(f"Loaded {m}")
        time.sleep(0.25)
    splash_step("Preview - click or press Esc to close")
    while pyi_splash.is_alive():
        time.sleep(0.2)
    return 0


# --------------------------------------------------------------------------- UI pieces
CAP_HEIGHT = 0.70  # Segoe UI capital height in em (1434 / 2048 units)


def text_top(widget: tk.Misc, font: tkfont.Font, h: float) -> int:
    """y of a canvas text item (anchor "n"/"nw") that centres the CAPITALS in a box `h` high.

    Tk's anchor "center" centres the font's line box (ascent + descent). Segoe UI's ascent keeps
    room above the capitals for accents, so centred text sits visibly low. This centres the
    capital height instead, which is what the eye reads as the middle of a label."""
    size = font.actual("size")
    em = -size if size < 0 else size * widget.winfo_fpixels("1p")
    return round(h / 2 + CAP_HEIGHT * em / 2 - font.metrics("ascent"))


def _logo(info: AppInfo, size: int) -> ImageTk.PhotoImage:
    img = Image.open(info.icon_png).convert("RGBA")
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


def _dark_title(win: tk.Misc) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    win.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    on = ctypes.c_int(1)
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (older builds: 19)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
            break
    # repaint the frame now (Windows 10 otherwise keeps the light caption until the next activation)
    swp = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE NOMOVE NOZORDER NOACTIVATE FRAMECHANGED
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, swp)


def round_popup(win: tk.Misc) -> None:
    """Windows 11: rounded corners and a `border`-coloured outline for a borderless popup (drop-down list)."""
    if sys.platform != "win32":
        return
    import ctypes

    with contextlib.suppress(Exception):
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
        dwm = ctypes.windll.dwmapi
        pref = ctypes.c_int(3)  # DWMWCP_ROUNDSMALL: the 8 px radius of menus and popups
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        r, g, b = _rgb(TOKENS["border"])
        color = ctypes.c_int(r | g << 8 | b << 16)  # COLORREF is 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))


class AppDialog(tk.Toplevel):
    """One frame for every app-level window: logo left, content column, optional progress pill,
    buttons bottom right (rightmost = primary). Fixed width, grows in height to fit, Escape closes."""

    W, H = 440, 250

    def __init__(self, master: tk.Misc, info: AppInfo, ui, title: str) -> None:
        super().__init__(master, bg=ui.BG)
        # Invisible until fully built: Tk maps a new toplevel at the first idle (here in _place), i.e. with
        # Windows' light caption and before the dark-mode attribute and the content exist. Alpha 0 hides
        # that first frame; show() makes the window opaque once it is dark and laid out.
        self.attributes("-alpha", 0.0)
        self._shown = False
        self.ui, self.info = ui, info
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        with contextlib.suppress(tk.TclError):
            self.iconbitmap(str(info.icon_png.with_suffix(".ico")))
        self._logo = _logo(info, 84)
        tk.Label(self, image=self._logo, bg=ui.BG, bd=0).place(x=32, y=32)
        self.content = tk.Frame(self, bg=ui.BG)
        self.content.place(x=140, y=28, width=self.W - 164)
        self.pill = tk.Label(self, bg=ui.BG, bd=0)
        self._fraction: Optional[float] = None
        self._pill_job: Optional[str] = None
        self.buttons = tk.Frame(self, bg=ui.BG)
        self.bind("<Escape>", lambda _e: self.destroy())
        self._place(self.W, self.H, center=True)
        _dark_title(self)
        self.focus_force()

    def _place(self, w: int, h: int, center: bool = False) -> None:
        if center:
            m = self.master
            m.update_idletasks()
            x = m.winfo_rootx() + (m.winfo_width() - w) // 2
            y = m.winfo_rooty() + max(0, (m.winfo_height() - h) // 3)
            self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        else:
            self.geometry(f"{w}x{h}")

    def text(self, text: str, size: int = 10, bold: bool = False, color: Optional[str] = None,
             pady: tuple = (0, 4), link: Optional[str] = None) -> tk.Label:
        ui = self.ui
        lbl = tk.Label(self.content, text=text, bg=ui.BG, fg=color or ui.FG, justify="left", anchor="w",
                       font=("Segoe UI", size, "bold" if bold else "normal"), wraplength=self.W - 164)
        lbl.pack(fill="x", pady=pady)
        if link:
            lbl.configure(cursor="hand2")
            lbl.bind("<Button-1>", lambda _e: webbrowser.open(link))
        return lbl

    def show(self, heading: str, body: str, buttons: list[tuple[str, Callable]], progress: Optional[float] = None,
             extra: Optional[Callable[["AppDialog"], None]] = None) -> None:
        ui = self.ui
        for w in self.content.winfo_children():
            w.destroy()
        for w in self.buttons.winfo_children():
            w.destroy()
        self.text(heading, 16, bold=True, pady=(0, 6))
        self.body = self.text(body)
        if extra:
            extra(self)
        for i, (label, cmd) in enumerate(buttons):
            ui._RoundButton(self.buttons, label, cmd, kind="primary" if i == 0 else "secondary").pack(side="right", padx=(8, 0))
        self.update_idletasks()
        content_h = 28 + self.content.winfo_reqheight()
        pill_h = 22 if progress is not None else 0
        h = max(self.H, max(content_h, 32 + 84) + 16 + pill_h + self.buttons.winfo_reqheight() + 20)
        self._place(self.W, h)
        self.buttons.place(relx=1.0, rely=1.0, x=-20, y=-20, anchor="se")
        if progress is None:
            self._fraction = None
            self.pill.place_forget()
        else:
            self.pill.place(x=140, y=h - 20 - self.buttons.winfo_reqheight() - 18)
            self.set_progress(None, progress)
        if not self._shown:
            self._shown = True
            self.update_idletasks()  # dark caption + content painted while still transparent
            self.attributes("-alpha", 1.0)
            self.focus_force()

    def set_progress(self, body: Optional[str], fraction: float) -> None:
        if body is not None:
            self.body.configure(text=body)
        self._fraction = fraction
        if self._pill_job is None:
            self._animate_pill()

    def _animate_pill(self) -> None:
        """Repaints only the bar (~30 ms) while it shows: the sweep keeps moving between progress updates."""
        if self._fraction is None or not self.winfo_exists():
            self._pill_job = None
            return
        self._pill_img = ImageTk.PhotoImage(progress_image(self.W - 164, self._fraction, time.monotonic(), self.ui.BG))
        self.pill.configure(image=self._pill_img)
        self._pill_job = self.after(30, self._animate_pill)


def message(app: tk.Misc, info: AppInfo, ui, heading: str, body: str, title: Optional[str] = None) -> None:
    """Modal notice in the app's dialog frame (replaces tkinter.messagebox); Enter or Escape closes."""
    win = AppDialog(app, info, ui, title or f"{info.name} - {heading}")
    win.show(heading, body, [("OK", win.destroy)])
    win.bind("<Return>", lambda _e: win.destroy())
    with contextlib.suppress(tk.TclError):
        win.grab_set()
    app.wait_window(win)


class UpdateManager:
    """Every update step in one app dialog. Automatic checks stay silent unless an update exists."""

    def __init__(self, app: tk.Tk, info: AppInfo, ui, can_restart: Callable[[], bool], busy_text: str) -> None:
        self.app, self.info, self.ui = app, info, ui
        self.can_restart, self.busy_text = can_restart, busy_text
        self.win: Optional[AppDialog] = None
        self.release: Optional[Release] = None
        self.token: Optional[str] = None
        self.cancel: Optional[threading.Event] = None
        self.checking = False
        self.q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._poll()

    def _poll(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                self.q.get_nowait()()
        if self.app.winfo_exists():
            self.app.after(80, self._poll)

    def _open(self) -> bool:
        return self.win is not None and self.win.winfo_exists()

    def _window(self) -> AppDialog:
        if not self._open():
            self.win = AppDialog(self.app, self.info, self.ui, f"{self.info.name} - updates")
            self.win.bind("<Destroy>", lambda e: self.cancel.set() if e.widget is self.win and self.cancel else None)
        else:
            self.win.lift()
            self.win.focus_force()
        return self.win

    def _close(self) -> None:
        if self._open():
            self.win.destroy()

    def _page(self) -> None:
        webbrowser.open(self.release.page if self.release else f"https://github.com/{self.info.repo}/releases/latest")

    def check(self, manual: bool) -> None:
        if self.checking or self.cancel is not None:
            if manual and self._open():
                self.win.lift()
            return
        self.checking = True
        if manual:
            self._window().show("Checking for updates...", "Looking for the latest release on GitHub.", [("Close", self._close)])

        def work() -> None:
            try:
                rel, tok = get_latest(self.info)
                self.q.put(lambda: self._checked(manual, rel, tok, None))
            except Exception as e:  # noqa: BLE001 -- shown to the user, never raised
                self.q.put(lambda e=e: self._checked(manual, None, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, manual: bool, rel: Optional[Release], tok: Optional[str], err: Optional[Exception]) -> None:
        self.checking = False
        if manual and not self._open():
            return  # closed while checking
        if err is not None:
            if manual:
                self._window().show("Update check failed", f"Could not check for updates: {_first_line(err)}.",
                                    [("Close", self._close), ("Open download page", self._page)])
            return
        self.release, self.token = rel, tok
        current = parse_version(self.info.version)
        if rel.version <= current:
            if manual:
                self._window().show("You're up to date", f"Version {short(current)} is the latest version.",
                                    [("Close", self._close), ("What's new", self._page)])
            return
        self._offer()

    def _offer(self) -> None:
        rel = self.release
        msg = f"Version {short(rel.version)} is available -- you have {short(parse_version(self.info.version))}."
        if rel.asset_url and exe_path() is not None:
            self._window().show("Update available", msg,
                                [("Update now", self._start), ("What's new", self._page), ("Later", self._close)])
        else:
            self._window().show("Update available", msg, [("Open download page", self._page), ("Later", self._close)])

    def _start(self) -> None:
        exe = exe_path()
        if not folder_writable(exe.parent):
            self._window().show("Can't update here", "This folder can't be written to, so the update can't be "
                                "installed here -- download it from the release page.",
                                [("Open download page", self._page), ("Later", self._close)])
            return
        rel, cancel = self.release, threading.Event()
        self.cancel = cancel
        label = f"Version {short(rel.version)}"
        w = self._window()
        w.show("Downloading update", f"{label}...", [("Cancel", cancel.set)], progress=0.0)
        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < 0.1 and done < total:
                return
            last[0] = now
            text = (f"{label}: {_size(done)} of {_size(total)} ({100 * done / total:.0f} %)" if total
                    else f"{label}: {_size(done)}")
            self.q.put(lambda: self._open() and self.win.set_progress(text, done / total if total else 0))

        def work() -> None:
            try:
                f = download(self.info, rel, self.token, progress, cancel)
                if cancel.is_set():
                    raise InterruptedError
                swap_in(f)
                self.q.put(lambda: self._done(None))
            except Exception as e:  # noqa: BLE001
                self.q.put(lambda e=e: self._done(e))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, err: Optional[Exception]) -> None:
        cancelled = self.cancel is not None and self.cancel.is_set()
        self.cancel = None
        if err is not None or cancelled:
            cleanup_leftovers()
            if isinstance(err, InterruptedError) or cancelled:
                if self._open():
                    self._offer()
                return
            self._window().show("Update failed", f"{_first_line(err)}. You can download it from the release page.",
                                [("Open download page", self._page), ("Close", self._close)])
            return
        self._window().show("Update installed",
                            f"Version {short(self.release.version)} is installed -- restart the app to use it.",
                            [("Restart now", self._restart), ("Later", self._close)])

    def _restart(self) -> None:
        if not self.can_restart():
            self.win.body.configure(text=self.busy_text)
            return
        try:
            relaunch()
        except OSError as e:
            self.win.body.configure(text=f"Could not restart: {_first_line(e)}. Start the app again yourself.")
            return
        self._close()
        self.app.destroy()


def show_about(app: tk.Tk, info: AppInfo, ui, updates: UpdateManager) -> None:
    win = AppDialog(app, info, ui, f"About {info.name}")

    def extra(d: AppDialog) -> None:
        d.text(f"Author: {info.author}")
        d.text(f"{info.description} {info.licence}", 9, color=ui.MUTED, pady=(4, 4))
        d.text(f"github.com/{info.repo}", 9, color=ui.ACCENT, link=f"https://github.com/{info.repo}", pady=(0, 10))
        if info.shortcuts:
            d.text("Keyboard shortcuts", 10, bold=True, pady=(0, 2))
            grid = tk.Frame(d.content, bg=ui.BG)
            grid.pack(fill="x")
            grid.columnconfigure(0, minsize=90)
            for r, (key, what) in enumerate(info.shortcuts):
                tk.Label(grid, text=key, bg=ui.BG, fg=ui.FG, font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w")
                tk.Label(grid, text=what, bg=ui.BG, fg=ui.MUTED, font=("Segoe UI", 9)).grid(row=r, column=1, sticky="w")

    def check() -> None:
        win.destroy()
        updates.check(manual=True)

    win.show(info.name, f"Version {short(parse_version(info.version))}", [("Close", win.destroy), ("Check for updates", check)],
             extra=extra)


# --------------------------------------------------------------------------- title bar
class TitleBar(tk.Frame):
    """Logo, app name, the app-level items, and minimise / maximise / close (Windows)."""

    def __init__(self, app: tk.Tk, info: AppInfo, ui, items: list[tuple[str, Callable]], on_close: Callable,
                 caption_buttons: bool = True) -> None:
        super().__init__(app, bg=ui.BG, height=TITLE_H)
        self.app, self.ui = app, ui
        self.pack_propagate(False)
        self._logo = _logo(info, 20)
        self.logo = tk.Label(self, image=self._logo, bg=ui.BG, bd=0)
        self.logo.pack(side="left", padx=(12, 8))
        self.name = tk.Label(self, text=info.name, bg=ui.BG, fg=ui.FG, font=("Segoe UI", 10))
        self.name.pack(side="left", padx=(0, 10))
        self.hot: list[tk.Widget] = []  # widgets that take clicks (the rest of the bar drags the window)
        for text, cmd in items:
            self.hot.append(self._item(text, cmd))
        if caption_buttons:
            for kind, cmd in (("close", on_close), ("max", self._toggle_max), ("min", app.iconify)):
                self.hot.append(self._caption_button(kind, cmd))
        app.bind("<Activate>", lambda _e: self.name.configure(fg=ui.FG), add="+")
        app.bind("<Deactivate>", lambda _e: self.name.configure(fg=ui.MUTED), add="+")

    def _item(self, text: str, cmd: Callable) -> tk.Canvas:
        ui = self.ui
        f = tkfont.Font(family="Segoe UI", size=10)
        w, h = f.measure(text) + 20, 26
        c = tk.Canvas(self, width=w, height=h, bg=ui.BG, highlightthickness=0, bd=0, cursor="hand2")
        c.pack(side="left", padx=(0, 2))

        def draw(hover: bool) -> None:
            c.delete("all")
            if hover:
                ui._paint_round_rect(c, w, h, 8, ui.BORDER, None)
            c.create_text(w / 2, text_top(c, f, h), anchor="n", text=text, fill=ui.FG if hover else ui.MUTED, font=f)

        draw(False)
        c.bind("<Enter>", lambda _e: draw(True))
        c.bind("<Leave>", lambda _e: draw(False))
        c.bind("<ButtonRelease-1>", lambda e: cmd() if 0 <= e.x < w and 0 <= e.y < h else None)
        return c

    def _caption_button(self, kind: str, cmd: Callable) -> tk.Canvas:
        ui = self.ui
        c = tk.Canvas(self, width=46, height=TITLE_H, bg=ui.BG, highlightthickness=0, bd=0)
        c.pack(side="right")

        def draw(hover: bool) -> None:
            c.delete("all")
            bg = (CLOSE_HOVER if kind == "close" else ui.BORDER) if hover else ui.BG
            c.configure(bg=bg)
            fg = "#ffffff" if hover else ui.FG
            x, y = 23, TITLE_H // 2
            if kind == "close":
                c.create_line(x - 5, y - 5, x + 5, y + 5, fill=fg, width=1)
                c.create_line(x - 5, y + 5, x + 5, y - 5, fill=fg, width=1)
            elif kind == "min":
                c.create_line(x - 5, y, x + 6, y, fill=fg, width=1)
            elif self.app.state() == "zoomed":
                c.create_rectangle(x - 5, y - 3, x + 3, y + 5, outline=fg)
                c.create_line(x - 3, y - 5, x + 5, y - 5, x + 5, y + 3, fill=fg)
            else:
                c.create_rectangle(x - 5, y - 5, x + 5, y + 5, outline=fg)

        draw(False)
        c.bind("<Enter>", lambda _e: draw(True))
        c.bind("<Leave>", lambda _e: draw(False))
        c.bind("<ButtonRelease-1>", lambda e: cmd() if 0 <= e.x < 46 and 0 <= e.y < TITLE_H else None)
        if kind == "max":
            self.app.bind("<Configure>", lambda e: draw(False) if e.widget is self.app else None, add="+")
        return c

    def _toggle_max(self) -> None:
        self.app.state("normal" if self.app.state() == "zoomed" else "zoomed")

    def hot_ranges(self) -> list[tuple[int, int]]:
        return [(w.winfo_x(), w.winfo_x() + w.winfo_width()) for w in self.hot]


class _CaptionHook:
    """Removes the native caption of a Tk window but keeps its resize borders, shadow, Snap and system
    menu: WM_NCCALCSIZE gives the caption area to the client, WM_NCHITTEST makes the drawn bar a
    caption (drag, double-click to maximise) and its top edge a resize border."""

    def __init__(self, app: tk.Tk, bar: TitleBar) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wt = ctypes, wintypes
        u = ctypes.windll.user32
        self.u = u
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.CallWindowProcW.restype = LRESULT
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

        class NCCALCSIZE_PARAMS(ctypes.Structure):
            _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]

        self.P = NCCALCSIZE_PARAMS
        self.ranges: list[tuple[int, int]] = []
        self.width = 0
        bar.bind("<Configure>", lambda _e: self._refresh(bar), add="+")
        app.update_idletasks()
        self.hwnd = int(app.wm_frame(), 16)
        self.proc = self.WNDPROC(self._proc)  # keep a reference: Windows calls it for the window's life
        self.old = u.SetWindowLongPtrW(self.hwnd, -4, ctypes.cast(self.proc, ctypes.c_void_p))
        u.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)  # SWP_FRAMECHANGED
        app.after(50, lambda: self._refresh(bar))

    def _refresh(self, bar: TitleBar) -> None:
        self.ranges = bar.hot_ranges()
        self.width = bar.winfo_width()

    def bind_drag(self, app: tk.Tk, widgets: list[tk.Widget]) -> None:
        """Tk gives the title bar (and its labels) their own child windows, so the mouse never reaches the
        outer window's hit test there: a press on them hands the move (or the top-edge resize) to Windows,
        which keeps Snap and Aero Shake; a double-click maximises / restores."""
        u = self.u

        def press(e) -> None:
            y = e.y_root - app.winfo_rooty()
            x = e.x_root - app.winfo_rootx()
            hit = 2  # HTCAPTION
            if y < 5 and not u.IsZoomed(self.hwnd):
                hit = 13 if x < 12 else 14 if x > self.width - 12 else 12  # HTTOPLEFT / HTTOPRIGHT / HTTOP
            u.ReleaseCapture()
            # posted, not sent: the move loop must run from Tk's message loop, not nested in this callback
            # (Tk events inside a Python callback crash tkinter)
            u.PostMessageW(self.hwnd, 0x00A1, hit, (e.y_root & 0xFFFF) << 16 | (e.x_root & 0xFFFF))  # WM_NCLBUTTONDOWN

        def toggle(_e) -> None:
            app.state("normal" if app.state() == "zoomed" else "zoomed")

        for w in widgets:
            w.bind("<ButtonPress-1>", press, add="+")
            w.bind("<Double-Button-1>", toggle, add="+")

    def _proc(self, h, msg, wp, lp):
        u = self.u
        if msg == 0x0083 and wp:  # WM_NCCALCSIZE
            p = self.P.from_address(lp)
            top = p.rgrc[0].top
            r = u.CallWindowProcW(self.old, h, msg, wp, lp)
            if u.IsZoomed(h):  # a maximised window hangs its frame off the screen edge
                top += u.GetSystemMetrics(33) + u.GetSystemMetrics(92)
            p.rgrc[0].top = top
            return r
        if msg == 0x0084:  # WM_NCHITTEST
            r = u.CallWindowProcW(self.old, h, msg, wp, lp)
            if r != 1:
                return r
            pt = self.wt.POINT(self.ctypes.c_short(lp & 0xFFFF).value, self.ctypes.c_short((lp >> 16) & 0xFFFF).value)
            u.ScreenToClient(h, self.ctypes.byref(pt))
            if pt.y < 5 and not u.IsZoomed(h):
                return 13 if pt.x < 12 else 14 if pt.x > self.width - 12 else 12  # HTTOPLEFT / HTTOPRIGHT / HTTOP
            if pt.y < TITLE_H and not any(x0 <= pt.x < x1 for x0, x1 in self.ranges):
                return 2  # HTCAPTION
            return r
        return u.CallWindowProcW(self.old, h, msg, wp, lp)


def install(app: tk.Tk, info: AppInfo, ui, can_restart: Callable[[], bool], busy_text: str,
            on_close: Optional[Callable] = None) -> UpdateManager:
    """Title bar with About / Check for updates, F1 = About, and an automatic update check 3 s after start.
    Call before packing the rest of the window (the bar packs itself at the top)."""
    updates = UpdateManager(app, info, ui, can_restart, busy_text)
    about = lambda: show_about(app, info, ui, updates)  # noqa: E731
    items = [("About", about), ("Check for updates", lambda: updates.check(manual=True))]
    close = on_close or app.destroy
    drawn = sys.platform == "win32"
    bar = TitleBar(app, info, ui, items, close, caption_buttons=drawn)
    bar.pack(side="top", fill="x")
    if drawn:
        try:
            app._caption_hook = _CaptionHook(app, bar)
            app._caption_hook.bind_drag(app, [bar, bar.logo, bar.name])
        except Exception:  # noqa: BLE001 -- keep the native caption, without a second set of buttons
            bar.destroy()
            bar = TitleBar(app, info, ui, items, close, caption_buttons=False)
            bar.pack(side="top", fill="x")
    app.bind_all("<F1>", lambda _e: about())
    app.after(3000, lambda: updates.check(manual=False))
    return updates
