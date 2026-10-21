"""Desktop GUI: drop a schematic PDF, click one button, get the analysis files.

Work runs on a background thread and reports back through a queue drained on the Tk main
thread (widgets must only be touched there). Drag-and-drop needs the optional `tkinterdnd2`
package; without it the Browse button still works.
"""
from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import pdfplumber
from PIL import Image, ImageDraw, ImageTk

from .analyze import OUTPUTS, analyze_pdf
from .netlist import ExtractionCancelled

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

_BaseTk = TkinterDnD.Tk if _DND_AVAILABLE else tk.Tk

APP_NAME = "SchematicNetlist"
APP_ID = "SchematicNetlist.App"
ICON = Path(__file__).resolve().parent / "assets" / "icon.ico"  # drawn by src/make_assets.py
RUN_TEXT = "Analyze schematic"
OUTPUT_LABELS = {
    "excel": "Excel analysis report (<name>_analysis.xlsx)",
    "spice": "HSPICE netlist (<name>.sp)",
    "netlist": "Flat netlist (<name>_netlist.txt / .json)",
}

# Dark theme, used by every widget.
BG = "#222831"
SURFACE = "#2d333d"
BORDER = "#3a414c"
FG = "#ffffff"
MUTED = "#aab4c0"
LOG_FG = "#d8dee9"
DISABLED_FG = "#6b7480"
ACCENT = "#4da3ff"
ACCENT_ACTIVE = "#7bbcff"
WARN = "#e0a030"
WARN_BG = "#3d3522"
WARN_FG = "#ffd27a"
PILL = "#5c6470"
PILL_HOVER = "#7a828e"
FONT = ("Segoe UI", 10)

LABEL = {"bg": BG, "fg": FG, "font": FONT}
FIELD = {
    "bg": SURFACE, "fg": FG, "insertbackground": FG, "relief": "flat", "bd": 0, "highlightthickness": 0,
    "font": FONT,
}

_CORNERS: dict = {}


def _corner_images(r: int, fill: str, outline: Optional[str], bg: str) -> list:
    """Anti-aliased corner tiles (nw, ne, sw, se) of a rounded rectangle, cached per style."""
    key = (r, fill, outline, bg)
    if key not in _CORNERS:
        s, d = 4, 2 * r + 2
        img = Image.new("RGB", (d * s, d * s), bg)
        ImageDraw.Draw(img).rounded_rectangle(
            (0, 0, d * s - 1, d * s - 1), radius=r * s, fill=fill, outline=outline, width=s
        )
        img = img.resize((d, d), Image.LANCZOS)
        c = r + 1
        _CORNERS[key] = [
            ImageTk.PhotoImage(img.crop(box)) for box in ((0, 0, c, c), (c, 0, d, c), (0, c, c, d), (c, c, d, d))
        ]
    return _CORNERS[key]


def _paint_round_rect(canvas: tk.Canvas, w: int, h: int, r: int, fill: str, outline: Optional[str]) -> None:
    canvas.delete("shape")
    r = max(1, min(r, w // 2 - 1, h // 2 - 1))
    c = r + 1
    canvas.create_rectangle(c, 0, w - c, h, fill=fill, width=0, tags="shape")
    canvas.create_rectangle(0, c, w, h - c, fill=fill, width=0, tags="shape")
    if outline:
        for box in ((c, 0, w - c, 1), (c, h - 1, w - c, h), (0, c, 1, h - c), (w - 1, c, w, h - c)):
            canvas.create_rectangle(*box, fill=outline, width=0, tags="shape")
    nw, ne, sw, se = _corner_images(r, fill, outline, canvas.cget("bg"))
    for img, x, y, anchor in ((nw, 0, 0, "nw"), (ne, w, 0, "ne"), (sw, 0, h, "sw"), (se, w, h, "se")):
        canvas.create_image(x, y, image=img, anchor=anchor, tags="shape")
    canvas.tag_lower("shape")


def _pill_image(color: str, width: int = 10, thickness: int = 6, height: int = 20) -> ImageTk.PhotoImage:
    s = 4
    img = Image.new("RGBA", (width * s, height * s), (0, 0, 0, 0))
    x0 = (width - thickness) * s // 2
    ImageDraw.Draw(img).rounded_rectangle(
        (x0, 0, x0 + thickness * s - 1, height * s - 1), radius=thickness * s // 2, fill=color
    )
    return ImageTk.PhotoImage(img.resize((width, height), Image.LANCZOS))


class _RoundedBox(tk.Canvas):
    """Rounded panel; put children in `.inner` (packed into the canvas so its size follows them)."""

    def __init__(
        self, parent: tk.Misc, fill: str, outline: Optional[str] = BORDER, radius: int = 12, pad: int = 8
    ) -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._fill, self._outline, self._radius = fill, outline, radius
        self.inner = tk.Frame(self, bg=fill)
        self.inner.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.bind("<Configure>", lambda _e: self._repaint(), add="+")

    def set_outline(self, color: Optional[str]) -> None:
        self._outline = color
        self._repaint()

    def _repaint(self) -> None:
        _paint_round_rect(self, self.winfo_width(), self.winfo_height(), self._radius, self._fill, self._outline)


class _RoundButton(tk.Canvas):
    """Rounded, hover-highlighted button; `config(text=..., state=...)` works like tk.Button."""

    _KINDS = {
        "primary": (ACCENT, ACCENT_ACTIVE, BG),
        "warn": (WARN, WARN_FG, BG),
        "secondary": (BORDER, "#4a525e", FG),
    }

    def __init__(
        self, parent: tk.Misc, text: str, command, kind: str = "secondary", font=FONT,
        width: Optional[int] = None, height: int = 1, state: str = "normal",
    ) -> None:
        self._text, self._state, self._command, self._hover = text, state, command, False
        self._fill, self._hover_fill, self._fg = self._KINDS[kind]
        self._font = tkfont.Font(font=font)
        w = (self._font.measure("0") * width if width else self._font.measure(text)) + 32
        h = self._font.metrics("linespace") * height + 12
        super().__init__(parent, bg=parent.cget("bg"), width=w, height=h, highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_release)

    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        changed = False
        for key in ("text", "state"):
            if key in kw:
                setattr(self, f"_{key}", kw.pop(key))
                changed = True
        result = super().configure(**kw) if kw else None
        if changed:
            self._draw()
        return result

    config = configure

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._draw()

    def _on_release(self, event) -> None:
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if inside and self._state != "disabled":
            self._command()

    def _draw(self) -> None:
        w = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        h = self.winfo_height() if self.winfo_height() > 1 else int(self.cget("height"))
        enabled = self._state != "disabled"
        fill = (self._hover_fill if self._hover else self._fill) if enabled else "#333a45"
        super().configure(cursor="hand2" if enabled else "arrow")
        _paint_round_rect(self, w, h, 8, fill, None)
        self.delete("label")
        self.create_text(
            w / 2, h / 2, text=self._text, fill=self._fg if enabled else DISABLED_FG, font=self._font, tags="label"
        )


class _Check(tk.Frame):
    """Themed check box (tk.Checkbutton draws a native grey square)."""

    def __init__(self, parent: tk.Misc, text: str, variable: tk.BooleanVar) -> None:
        super().__init__(parent, bg=parent.cget("bg"))
        self._var = variable
        self._box = tk.Canvas(self, width=18, height=18, bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        self._box.pack(side="left")
        label = tk.Label(self, text=text, bg=parent.cget("bg"), fg=FG, font=FONT, cursor="hand2")
        label.pack(side="left", padx=(6, 0))
        for w in (self, self._box, label):
            w.bind("<Button-1>", lambda _e: self._var.set(not self._var.get()))
        self._var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    def _draw(self) -> None:
        on = self._var.get()
        _paint_round_rect(self._box, 18, 18, 4, ACCENT if on else SURFACE, None if on else BORDER)
        self._box.delete("tick")
        if on:
            self._box.create_line(4, 9, 8, 13, 14, 5, fill=BG, width=2, capstyle="round", joinstyle="round", tags="tick")


def _boxed(parent: tk.Misc, widget_cls, **kw) -> tuple[_RoundedBox, tk.Widget]:
    """Input widget inside a rounded box whose outline turns accent-blue on focus."""
    box = _RoundedBox(parent, SURFACE, radius=8, pad=5)
    widget = widget_cls(box.inner, **kw)
    widget.pack(side="left", fill="x", expand=True)
    widget.bind("<FocusIn>", lambda _e: box.set_outline(ACCENT), add="+")
    widget.bind("<FocusOut>", lambda _e: box.set_outline(BORDER), add="+")
    return box, widget


def _folder_writable(folder: Path) -> bool:
    # os.access ignores Windows ACLs and read-only shares, so actually try to create a file
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def _warn_read_only(path: Path) -> None:
    messagebox.showwarning(
        "Read-only folder",
        f"This file is in a folder that can't be written to:\n\n{path.parent}\n\n"
        "The results are saved next to the file, so move it to a folder you can write to "
        "(for example Documents or Desktop) and select it again.",
    )


class _SpinnerOverlay(tk.Frame):
    """Full-window busy overlay: spinner, live status text and a Cancel button."""

    def __init__(self, parent: tk.Misc, on_cancel) -> None:
        super().__init__(parent, bg=BG)
        self._angle = 0
        self._spinning = False
        self._after_id: Optional[str] = None

        center = tk.Frame(self, bg=BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        self.canvas = tk.Canvas(center, width=64, height=64, bg=BG, highlightthickness=0)
        self.canvas.pack(pady=(0, 14))
        self._arc = self.canvas.create_arc(
            4, 4, 60, 60, start=0, extent=120, style="arc", width=6, outline=ACCENT
        )

        self.status_var = tk.StringVar(value="Analyzing...")
        tk.Label(
            center, textvariable=self.status_var, fg=FG, bg=BG, font=("Segoe UI", 11), wraplength=420,
            justify="center",
        ).pack()

        _RoundButton(center, "Cancel", on_cancel, width=10).pack(pady=(16, 0))

    def show(self) -> None:
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()
        self._spinning = True
        self._spin()

    def hide(self) -> None:
        self._spinning = False
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        self.place_forget()

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _spin(self) -> None:
        if not self._spinning:
            return
        self._angle = (self._angle + 18) % 360
        self.canvas.itemconfig(self._arc, start=self._angle)
        self._after_id = self.after(40, self._spin)


class App(_BaseTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("640x640")
        self.minsize(640, 600)
        self._apply_theme()
        self._set_icon()

        self.pdf_path: Optional[Path] = None
        self._out_dir: Optional[Path] = None
        self._cancel_event: Optional[threading.Event] = None
        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._build_ui()
        self._overlay = _SpinnerOverlay(self, on_cancel=self._on_cancel)
        self.after(100, self._drain_queue)

    def _apply_theme(self) -> None:
        self.configure(bg=BG)
        style = ttk.Style(self)
        style.theme_use("clam")  # the native Windows theme ignores ttk colours
        # Thin grey rounded thumb, no arrows, trough blends into the background.
        self._pill_images = (_pill_image(PILL), _pill_image(PILL_HOVER))
        style.element_create(
            "pill.thumb", "image", self._pill_images[0],
            ("pressed", self._pill_images[1]), ("active", self._pill_images[1]), border=5, sticky="ns",
        )
        style.layout(
            "Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("pill.thumb", {"expand": "1", "sticky": "ns"})]})],
        )
        for name, trough in (("Vertical.TScrollbar", BG), ("Log.Vertical.TScrollbar", SURFACE)):
            style.configure(name, troughcolor=trough, bordercolor=trough, lightcolor=trough, darkcolor=trough)
        if sys.platform == "win32":
            import ctypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dark = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE; ignored on Windows versions without it.
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))

    def _set_icon(self) -> None:
        if sys.platform == "win32":
            import ctypes

            # own taskbar identity: without it a source run is grouped under (and shows) python.exe's icon
            with contextlib.suppress(OSError, AttributeError):
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        if ICON.exists():
            with contextlib.suppress(tk.TclError):
                self.iconbitmap(default=str(ICON))  # title bar + taskbar, inherited by dialogs

    def _build_ui(self) -> None:
        pad = {"padx": 15, "pady": 5}

        self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0, yscrollincrement=1)
        page_scroll = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(self._canvas, bg=BG)
        self._body = body
        self._body_item = self._canvas.create_window(0, 0, window=body, anchor="nw")
        self._canvas.bind("<Configure>", lambda _e: self._fit_body())
        self.bind_all("<MouseWheel>", self._on_mousewheel)
        with contextlib.suppress(tk.TclError):  # <TouchpadScroll> (two-finger trackpad) exists from Tk 9
            self.bind_all("<TouchpadScroll>", self._on_touchpad)

        self.drop_zone = _RoundedBox(body, SURFACE, pad=10)
        self.drop_zone.pack(fill="x", padx=15, pady=(15, 10))
        drop = self.drop_zone.inner
        hint = tk.Label(
            drop,
            text="Drag & drop a schematic PDF here, or" if _DND_AVAILABLE else "Choose a schematic PDF",
            bg=SURFACE,
            fg=FG,
            font=("Segoe UI", 11),
        )
        hint.pack(pady=(20, 2))
        browse_button = _RoundButton(drop, "Browse...", self._on_browse, width=12)
        browse_button.pack(pady=10)
        self.preview_label = tk.Label(drop, bg=SURFACE)
        self.preview_label.pack(pady=4)
        self.file_label = tk.Label(drop, text="No file selected.", bg=SURFACE, fg=MUTED, font=FONT)
        self.file_label.pack(pady=(0, 8))
        if _DND_AVAILABLE:
            # Drops land on the innermost widget, so every child must be a target too.
            for widget in (self.drop_zone, drop, hint, browse_button, self.preview_label, self.file_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        form = tk.Frame(body, bg=BG)
        form.pack(fill="x", **pad)
        form.columnconfigure(1, weight=1)

        tk.Label(form, text="Project name:", **LABEL).grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        name_box, _ = _boxed(form, tk.Entry, textvariable=self.name_var, **FIELD)
        name_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        tk.Label(form, text="Outputs:", **LABEL).grid(row=1, column=0, sticky="nw", pady=(8, 0))
        checks = tk.Frame(form, bg=BG)
        checks.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        self.output_vars = {k: tk.BooleanVar(value=True) for k in OUTPUTS}
        for k in OUTPUTS:
            _Check(checks, OUTPUT_LABELS[k], self.output_vars[k]).pack(anchor="w", pady=1)

        self.run_button = _RoundButton(
            body, RUN_TEXT, self._on_run, kind="primary",
            font=("Segoe UI", 11, "bold"), height=2,
        )
        self.run_button.pack(fill="x", **pad)

        result_row = tk.Frame(body, bg=BG)
        result_row.pack(fill="x", **pad)
        self.open_folder_button = _RoundButton(
            result_row, "Open output folder", self._on_open_folder, state="disabled"
        )
        self.open_folder_button.pack(side="left")
        tk.Label(
            result_row, text="Results go to a folder named after the PDF, next to it.", bg=BG, fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=10)

        self.log_frame = _RoundedBox(body, SURFACE)
        self.log_frame.pack(fill="both", expand=True, padx=15, pady=(10, 15))
        # Content above changed size (preview) -> refit the scroll area.
        self.log_frame.bind("<Configure>", lambda _e: self._fit_body(), add="+")
        self.log_box = tk.Text(
            self.log_frame.inner, height=12, state="disabled", font=("Consolas", 9), bg=SURFACE, fg=LOG_FG,
            insertbackground=FG, selectbackground=ACCENT, selectforeground=BG, relief="flat", bd=0,
            highlightthickness=0, padx=6, pady=4,
        )
        log_scroll = ttk.Scrollbar(
            self.log_frame.inner, orient="vertical", command=self.log_box.yview, style="Log.Vertical.TScrollbar"
        )
        self.log_box.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)

    def _fit_body(self) -> None:
        """Body fills the window when it fits (log takes the slack), otherwise scrolls."""
        width = self._canvas.winfo_width()
        height = max(self._body.winfo_reqheight(), self._canvas.winfo_height())
        self._canvas.itemconfigure(self._body_item, width=width, height=height)
        self._canvas.configure(scrollregion=(0, 0, width, height))

    def _page_scrolls(self, widget) -> bool:
        """Scroll the page unless the pointer is over something that scrolls itself."""
        if not isinstance(widget, tk.Misc) or isinstance(widget, tk.Listbox):
            return False
        if isinstance(widget, tk.Text) and widget.yview() != (0.0, 1.0):
            return False
        return self._body.winfo_reqheight() > self._canvas.winfo_height()

    def _on_mousewheel(self, event) -> None:
        if self._page_scrolls(event.widget):
            self._canvas.yview_scroll(int(-event.delta / 3), "units")

    def _on_touchpad(self, event) -> None:
        if self._page_scrolls(event.widget):
            _dx, dy = self.tk.splitlist(self.tk.call("tk::PreciseScrollDeltas", event.delta))
            self._canvas.yview_scroll(-int(dy), "units")

    # -- file selection -----------------------------------------------------

    def _on_drop(self, event) -> None:
        paths = self.tk.splitlist(event.data)
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        if not pdfs:
            messagebox.showwarning("Not a PDF", "Please drop a .pdf file.")
            return
        self._set_pdf(Path(pdfs[0]))

    def _on_browse(self) -> None:
        chosen = filedialog.askopenfilename(title="Choose a schematic PDF", filetypes=[("PDF files", "*.pdf")])
        if chosen:
            self._set_pdf(Path(chosen))

    def _set_pdf(self, path: Path) -> None:
        if not _folder_writable(path.parent):
            _warn_read_only(path)
            return
        self.pdf_path = path
        self.file_label.config(text=path.name)
        if not self.name_var.get():
            self.name_var.set(path.stem)
        self._load_preview(path)

    def _load_preview(self, path: Path) -> None:
        def work() -> None:
            try:
                with pdfplumber.open(str(path)) as pdf:
                    img = pdf.pages[0].to_image(resolution=40).original
                img.thumbnail((420, 110))
                self._queue.put(("preview", img))
            except Exception as exc:
                self._queue.put(("log", f"Preview unavailable: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    # -- run ------------------------------------------------------------

    def _log(self, message: str) -> None:
        self._queue.put(("log", message))

    def _reset_buttons(self) -> None:
        self.run_button.config(state="normal", text=RUN_TEXT)
        self._overlay.hide()

    def _on_run(self) -> None:
        if self.pdf_path is None:
            messagebox.showwarning("No PDF", "Choose or drop a schematic PDF first.")
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Missing name", "Enter a project name first.")
            return
        outputs = [k for k in OUTPUTS if self.output_vars[k].get()]
        if not outputs:
            messagebox.showwarning("No outputs", "Tick at least one output.")
            return

        self._out_dir = self.pdf_path.parent / self.pdf_path.stem
        self.run_button.config(state="disabled", text="Working...")
        self.open_folder_button.config(state="disabled")
        self._set_log("")
        self._cancel_event = threading.Event()
        self._overlay.set_status("Starting...")
        self._overlay.show()
        threading.Thread(
            target=self._worker,
            args=(self.pdf_path, name, outputs, self._out_dir, self._cancel_event),
            daemon=True,
        ).start()

    def _on_cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._overlay.set_status("Cancelling... (finishing current page)")

    def _worker(self, pdf_path: Path, name: str, outputs: list[str], out_dir: Path, cancel_event: threading.Event) -> None:
        try:
            self._log(f"Extracting the circuit from {pdf_path.name} ...")

            def progress(current: int, total: int) -> None:
                self._queue.put(("progress", f"Reading circuit: {current}/{total} pages done"))

            res = analyze_pdf(pdf_path, name, out_dir, outputs, progress=progress, cancel_check=cancel_event.is_set)
            for line in res.circuit.log:
                self._log(line)
            for w in res.circuit.warnings:
                if w.level == "warn":
                    self._log("[warn] " + (f"page {w.page}: " if w.page else "") + w.text)
            self._queue.put(("done", res))
        except ExtractionCancelled:
            self._queue.put(("cancelled", None))
        except Exception as e:  # noqa: BLE001 -- top-level worker: surface every failure to the UI
            self._queue.put(("error", f"{e}\n\n{traceback.format_exc()}"))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    self._overlay.set_status(str(payload))
                elif kind == "preview":
                    # Keep a reference or Tk shows a blank image.
                    self._preview_img = ImageTk.PhotoImage(payload)
                    self.preview_label.config(image=self._preview_img)
                elif kind == "done":
                    c = payload.circuit
                    self._append_log("\nWrote: " + ", ".join(f.name for f in payload.files))
                    self._reset_buttons()
                    self.open_folder_button.config(state="normal")
                    messagebox.showinfo(
                        "Done",
                        f"{len(c.parts)} parts and {len(c.nets)} nets found on {len(c.pages)} page(s).\n\n"
                        + "\n".join(f.name for f in payload.files),
                    )
                elif kind == "cancelled":
                    self._append_log("\nCancelled by user.")
                    self._reset_buttons()
                elif kind == "error":
                    self._append_log(f"\nERROR: {payload}")
                    self._reset_buttons()
                    messagebox.showerror("Failed", str(payload).splitlines()[0])
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _on_open_folder(self) -> None:
        if self._out_dir is None:
            return
        if sys.platform == "win32":
            os.startfile(self._out_dir)  # noqa: S606 -- user-initiated, opening a folder they chose
        else:
            subprocess.run(["xdg-open" if sys.platform.startswith("linux") else "open", str(self._out_dir)])

    def _set_log(self, text: str) -> None:
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.insert(tk.END, text)
        self.log_box.config(state="disabled")

    def _append_log(self, line: str) -> None:
        self.log_box.config(state="normal")
        self.log_box.insert(tk.END, line + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state="disabled")


def _close_splash() -> None:
    """The exe shows a splash image while unpacking (see build_exe.py); drop it once the window is up."""
    try:
        import pyi_splash  # only exists inside the PyInstaller build
    except ImportError:
        return
    with contextlib.suppress(Exception):
        pyi_splash.close()


def main() -> None:
    app = App()
    app.update_idletasks()
    app.after(0, _close_splash)
    app.mainloop()


if __name__ == "__main__":
    main()
