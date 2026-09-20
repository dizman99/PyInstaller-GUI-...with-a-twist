#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyInstaller Build Assistant
============================

A Tkinter tool that assembles PyInstaller command lines on a per-project
basis, watches the build process LIVE (--log-level=DEBUG), and lets you
pull problematic modules/DLLs straight out of the live log into the
exclude / add-binary lists via double-click, so you can restart the build
right away.

Workflow:
  1. Choose the main project source file
  2. Pick PyInstaller switches via checkboxes; optionally set --distpath
  3. Maintain the exclude-module list (pre-populated with known trouble
     makers, enabled by default), laid out as a scrollable multi-column
     grid so large lists stay manageable
  4. Maintain the add-binary list (pre-populated with known DLL cases,
     DISABLED by default - the user enables exactly what the current
     project needs, since e.g. NumPy DLL hash filenames differ from
     machine to machine), same scrollable grid layout
  5. "Build Project" validates that all enabled add-binary source paths
     exist, clears the log view, then automatically switches to the Live
     Log tab and starts the build

  A note on --distpath/--workpath: PyInstaller resolves these paths
  relative to the CURRENT WORKING DIRECTORY of the calling process, not
  relative to the script. This tool therefore always launches PyInstaller
  with cwd=<script folder>, so "dist"/"build" reliably end up next to the
  source, regardless of whatever directory a terminal happened to be in.
  If --distpath is set explicitly, that path is used (relative to the
  script folder unless it is absolute).

  A note on console vs. windowed: --console and --windowed are mutually
  exclusive at the Windows-subsystem level (an EXE is compiled as either
  a CONSOLE or a WINDOWS subsystem binary, never both). There is no need
  for a third switch: simply leaving --windowed unchecked (the default)
  already gives you a console window *in addition to* any GUI window
  your own code opens (e.g. a Tkinter window) - which is the common
  pattern for a GUI app that still prints debug output to a console.

  A note on --add-binary's target folder (DEST): this is NOT just a
  cosmetic label. It is the actual relative folder inside the extracted
  bundle (under sys._MEIPASS at runtime) where the file is placed. Other
  code (e.g. the owning Python package) may look for the DLL at that
  exact relative location via ctypes or a path built from its own
  __file__, so DEST usually needs to match the package's own
  site-packages folder name for the DLL to be found at runtime. The
  add-binary dialog pre-fills a guess based on the source path's parent
  folder for this reason.

  6. Cancel any time -> double-click a log line -> entry lands in the
     matching list -> restart the build
  7. After the build: warn-<name>.txt is parsed and cross-checked against
     the active excludes (flags modules that may have been wrongly
     excluded). On success, the resulting EXE path is shown, with a
     button to open its folder.

  Log noise reduction: PyInstaller's DEBUG log is dominated by Windows
  system DLL dependency chatter ("Processing dependency, name: ...",
  "Skipping dependency ... due to global exclusion rules / due to prior
  processing"). These lines are filtered out of the default view since
  they never represent something actionable - they are Windows system
  libraries PyInstaller always skips, or DLLs already handled earlier.
  The truly informative line while a build is "stuck" is
  "Analyzing binary '...'", which marks what PyInstaller is currently
  working on; it is highlighted more prominently for that reason. The
  log view is cleared at the start of every build to avoid confusion
  with a previous run's output, and offers both a vertical and a
  horizontal scrollbar (plus mouse-wheel scrolling) since many DEBUG
  lines are very wide.

Project configuration is stored next to the source file:
    <scriptname>.pyinstaller.json

Tested for: Windows, Python 3.x, PyInstaller (current versions)
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import Optional, TypedDict

# ----------------------------------------------------------------------
# Type definitions
# ----------------------------------------------------------------------


class AddBinaryEntry(TypedDict):
    src: str
    dest: str
    enabled: bool


# ----------------------------------------------------------------------
# Default configuration
# ----------------------------------------------------------------------

DEFAULT_EXCLUDES = [
    "tensorflow",
    "keras",
    "torch",
    "scipy",
    "sympy",
    "pandas",
    "matplotlib",
    "pyarrow",
    "IPython",
    "easyocr",
]

# Further common candidates - NOT enabled by default, offered as
# ready-made entries in case they show up in a given project.
SUGGESTED_EXTRA_EXCLUDES = [
    "notebook",
    "jupyter",
    "jupyterlab",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "numba",
    "llvmlite",
]

# Pre-populated add-binary candidates. Disabled by default because e.g.
# NumPy DLL filenames contain a build hash that can differ between
# machines - the user enables exactly what fits and adjusts the path if
# needed.
DEFAULT_ADD_BINARIES: list[AddBinaryEntry] = [
    {
        "src": r"C:/Python/Lib/site-packages/numpy.libs/libscipy_openblas64_-43e11ff0749b8cbe0a615c9cf6737e0e.dll",
        "dest": "numpy.libs",
        "enabled": False,
    },
    {
        "src": r"C:/Python/Lib/site-packages/pymupdf/mupdfcpp64.dll",
        "dest": "pymupdf",
        "enabled": False,
    },
    {
        "src": r"C:/Python/Lib/site-packages/pyzbar/libiconv.dll",
        "dest": "pyzbar",
        "enabled": False,
    },
    {
        "src": r"C:/Python/Lib/site-packages/numpy.libs/msvcp140-d64049c6e3865410a7dda6a7e9f0c575.dll",
        "dest": "numpy.libs",
        "enabled": False,
    },
    {
        "src": r"C:/Python/Lib/site-packages/pyzbar/libzbar-64.dll",
        "dest": "pyzbar",
        "enabled": False,
    },
    {
        "src": r"C:/Python/Lib/site-packages/pylibdmtx/libdmtx-64.dll",
        "dest": "pylibdmtx",
        "enabled": False,
    },
]

# Common PyInstaller switches selectable via checkbox.
# (name, label, enabled_by_default)
PYINSTALLER_SWITCHES = [
    ("--onefile", "Onefile (single EXE)", True),
    ("--onedir", "Onedir (folder with EXE + dependencies)", False),
    ("--windowed", "Windowed / --noconsole (suppress console window)", False),
    ("--noconfirm", "Overwrite existing output without prompting", True),
    ("--clean", "Clean build cache before running", False),
    ("--noupx", "Disable UPX compression", False),
    ("--debug=all", "Debug build (bootloader diagnostics)", False),
]

# Number of columns used for the exclude-module and add-binary grids.
GRID_COLUMNS = 3

# ----------------------------------------------------------------------
# Log parsing: which DEBUG lines matter to the user?
# ----------------------------------------------------------------------

RE_SUBMODULES = re.compile(r"Collecting submodules for\s+([\w\.]+)", re.IGNORECASE)
RE_HOOK_LOADED = re.compile(r"Loading module hook\s+['\"]?hook-([\w\.]+)\.py", re.IGNORECASE)
RE_HOOK_FOR_MODULE = re.compile(
    r"Processing (?:pre-safe-import-module|pre-find-module-path)\s+hook\s+for\s+module\s+['\"]?([\w\.]+)['\"]?",
    re.IGNORECASE,
)
RE_ANALYZING = re.compile(r"Analyzing\s+(.+\.py)", re.IGNORECASE)
RE_ANALYZING_BINARY = re.compile(r"Analyzing binary\s+'(.+)'", re.IGNORECASE)
RE_DLL_PATH = re.compile(r"([A-Za-z]:[\\/][^\s\"']+\.(?:dll|pyd))", re.IGNORECASE)
RE_WARN_MISSING = re.compile(r"missing module named\s+([\w\.]+)", re.IGNORECASE)

# Noise lines that never carry actionable information: routine Windows
# system-DLL dependency bookkeeping that PyInstaller performs for every
# single binary it inspects. These clutter the log without ever pointing
# at something the user can act on, so they are suppressed even in the
# default filtered view.
RE_NOISE_DEP_PROCESSING = re.compile(r"Processing dependency, name:", re.IGNORECASE)
RE_NOISE_DEP_SKIP = re.compile(
    r"Skipping dependency .* due to (global exclusion rules|prior processing)", re.IGNORECASE
)

# Lines that should appear in the filtered view (substring check, applied
# before the more precise classification).
FILTER_KEYWORDS = (
    "collecting submodules",
    "loading module hook",
    "processing pre-",
    "analyzing",
    ".dll",
    ".pyd",
    "binary dependency",
    "error",
    "warning",
    "traceback",
)


def is_noise_line(line: str) -> bool:
    """Routine Windows system-DLL dependency chatter that is never
    actionable and should be suppressed even in the filtered view."""
    return bool(RE_NOISE_DEP_PROCESSING.search(line) or RE_NOISE_DEP_SKIP.search(line))


def classify_line(line: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (category, value) if the line carries a module or DLL
    reference relevant to the user, otherwise (None, None)."""
    m = RE_SUBMODULES.search(line)
    if m:
        return ("module", m.group(1))
    m = RE_HOOK_LOADED.search(line)
    if m:
        return ("module", m.group(1))
    m = RE_HOOK_FOR_MODULE.search(line)
    if m:
        return ("module", m.group(1))
    m = RE_ANALYZING_BINARY.search(line)
    if m:
        return ("analyzing_binary", m.group(1))
    m = RE_DLL_PATH.search(line)
    if m:
        return ("dll", m.group(1))
    m = RE_ANALYZING.search(line)
    if m:
        return ("file", m.group(1))
    return (None, None)


def line_is_relevant(line: str) -> bool:
    low = line.lower()
    return any(kw in low for kw in FILTER_KEYWORDS)


def guess_dest_from_source(dll_path: str) -> str:
    """Guesses a sensible --add-binary DEST from a DLL's source path.

    The immediate parent folder name in site-packages usually matches
    the Python package name that expects the DLL at exactly this
    relative location at runtime (e.g. .../site-packages/pyzbar/foo.dll
    -> 'pyzbar'). This is a heuristic, not a guarantee - always verify
    against how the owning package actually locates its DLL."""
    normalized = dll_path.replace("\\", "/")
    return os.path.basename(os.path.dirname(normalized))


# ----------------------------------------------------------------------
# Project configuration (JSON next to the source file)
# ----------------------------------------------------------------------


class ProjectConfig:
    def __init__(self, script_path: str):
        self.script_path = script_path
        self.json_path = self._json_path_for(script_path)
        self.switches: dict[str, bool] = {name: default for name, _, default in PYINSTALLER_SWITCHES}
        self.excludes: dict[str, bool] = {name: True for name in DEFAULT_EXCLUDES}
        # Extra excludes added by the user during a run also live here.
        self.add_binaries: list[AddBinaryEntry] = [dict(b) for b in DEFAULT_ADD_BINARIES]  # type: ignore[misc]
        # Optional explicit output folder (--distpath). Empty = PyInstaller default (./dist).
        self.distpath: str = ""
        self.loaded_from_disk = False
        self.load_if_exists()

    @staticmethod
    def _json_path_for(script_path: str) -> str:
        base, _ = os.path.splitext(script_path)
        return base + ".pyinstaller.json"

    def load_if_exists(self) -> None:
        if os.path.isfile(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.switches.update(data.get("switches", {}))
                self.excludes = data.get("excludes", self.excludes)
                self.add_binaries = data.get("add_binaries", self.add_binaries)
                self.distpath = data.get("distpath", self.distpath)
                self.loaded_from_disk = True
            except (json.JSONDecodeError, OSError) as exc:
                messagebox.showwarning(
                    "Could not load configuration",
                    f"The file\n{self.json_path}\ncould not be read:\n{exc}\n\n"
                    "Default values will be used.",
                )

    def save(self) -> None:
        data = {
            "script_path": self.script_path,
            "switches": self.switches,
            "excludes": self.excludes,
            "add_binaries": self.add_binaries,
            "distpath": self.distpath,
        }
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def active_switches(self) -> list[str]:
        return [name for name, on in self.switches.items() if on]

    def active_excludes(self) -> list[str]:
        return [name for name, on in self.excludes.items() if on]

    def active_add_binaries(self) -> list[AddBinaryEntry]:
        return [b for b in self.add_binaries if b.get("enabled", True)]

    def build_command(self) -> list[str]:
        cmd: list[str] = ["pyinstaller", "--log-level=DEBUG"]
        cmd.extend(self.active_switches())
        if self.distpath.strip():
            cmd.extend(["--distpath", self.distpath.strip()])
        for mod in self.active_excludes():
            cmd.extend(["--exclude-module", mod])
        for b in self.active_add_binaries():
            src = b["src"].replace("\\", "/")
            dest = b["dest"]
            cmd.extend(["--add-binary", f"{src};{dest}"])
        cmd.append(self.script_path)
        return cmd

    def script_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.script_path))

    def script_stem(self) -> str:
        return os.path.splitext(os.path.basename(self.script_path))[0]

    def resolved_distpath(self) -> str:
        """Resolves the output folder. Since this tool always launches
        PyInstaller with cwd=script_dir(), a relative --distpath is
        interpreted relative to the script folder; without --distpath,
        PyInstaller's own default './dist' applies (also relative to the
        script folder, since that is the process cwd)."""
        base = self.distpath.strip() or "dist"
        if os.path.isabs(base):
            return base
        return os.path.join(self.script_dir(), base)

    def warn_file_path(self) -> str:
        return os.path.join(self.script_dir(), "build", self.script_stem(), f"warn-{self.script_stem()}.txt")

    def is_onefile(self) -> bool:
        # --onefile takes precedence if both were (incorrectly) enabled.
        return self.switches.get("--onefile", False) or not self.switches.get("--onedir", False)

    def expected_exe_path(self) -> str:
        """Determines the path PyInstaller is expected to use for the
        resulting EXE, taking a possibly set --distpath into account."""
        stem = self.script_stem()
        dist_dir = self.resolved_distpath()
        if self.is_onefile():
            return os.path.join(dist_dir, f"{stem}.exe")
        return os.path.join(dist_dir, stem, f"{stem}.exe")


# ----------------------------------------------------------------------
# Build runner: runs PyInstaller in a thread, reports lines to the GUI
# via a queue.
# ----------------------------------------------------------------------


class BuildRunner:
    def __init__(self, cmd: list[str], cwd: str, line_queue: "queue.Queue"):
        self.cmd = cmd
        self.cwd = cwd
        self.line_queue = line_queue
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self._stop_requested = False

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self.process = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            self.line_queue.put(("__error__", f"Could not start PyInstaller: {exc}"))
            self.line_queue.put(("__done__", -1))
            return
        except Exception as exc:
            self.line_queue.put(("__error__", f"Unexpected error on startup: {exc}"))
            self.line_queue.put(("__done__", -1))
            return

        stdout = self.process.stdout
        if stdout is not None:
            for line in stdout:
                self.line_queue.put(("__line__", line.rstrip("\n")))
                if self._stop_requested:
                    break
            stdout.close()

        return_code = self.process.wait()
        self.line_queue.put(("__done__", return_code))

    def kill(self) -> None:
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
            except Exception:
                pass


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------


class PyInstallerBuilderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PyInstaller Build Assistant")
        self.geometry("980x800")

        self.config_obj: Optional[ProjectConfig] = None
        self.runner: Optional[BuildRunner] = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self.full_log_file = None

        self.switch_vars: dict[str, tk.BooleanVar] = {}
        self.exclude_vars: dict[str, tk.BooleanVar] = {}
        self.add_binary_rows: list[dict] = []  # {"enabled_var", "src", "dest", "frame"}

        self._build_ui()
        self.after(100, self._poll_log_queue)

    # ---------------- Helper: guarantee a config object exists ----------------

    def _require_config(self) -> ProjectConfig:
        """Ensures a project configuration exists before it is accessed.
        Creates a new one from the currently chosen source if needed."""
        if self.config_obj is None:
            source = self.source_var.get().strip()
            if not source:
                raise ValueError("No source script has been selected yet.")
            self.config_obj = ProjectConfig(source)
            self._apply_config_to_ui()
        return self.config_obj

    # ---------------- Reusable scrollable grid container ----------------

    def _make_scrollable_grid(self, parent) -> ttk.Frame:
        """Creates a vertically scrollable area (canvas + inner frame) and
        returns the inner frame that content should be gridded into. Used
        for the exclude-module and add-binary lists so they never run out
        of space even with many entries."""
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True, padx=5, pady=5)

        canvas = tk.Canvas(container, highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vscroll.set)

        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        def _on_mousewheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return inner

    # ---------------- UI construction ----------------

    def _build_ui(self):
        # --- Step 1: source ---
        frame_source = ttk.LabelFrame(self, text="1. Project Source")
        frame_source.pack(fill="x", padx=10, pady=(10, 5))

        self.source_var = tk.StringVar()
        ttk.Entry(frame_source, textvariable=self.source_var, width=90).pack(
            side="left", padx=5, pady=5, fill="x", expand=True
        )
        ttk.Button(frame_source, text="Browse...", command=self.on_choose_source).pack(
            side="left", padx=5, pady=5
        )

        # --- Notebook with steps 2-5 ---
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        self.tab_switches = ttk.Frame(self.notebook)
        self.tab_excludes = ttk.Frame(self.notebook)
        self.tab_binaries = ttk.Frame(self.notebook)
        self.tab_log = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_switches, text="2. PyInstaller Switches")
        self.notebook.add(self.tab_excludes, text="3. Exclude Modules")
        self.notebook.add(self.tab_binaries, text="4. Add Binary")
        self.notebook.add(self.tab_log, text="5. Build / Live Log")

        self._build_switches_tab()
        self._build_excludes_tab()
        self._build_binaries_tab()
        self._build_log_tab()

        # --- Result row (success / EXE path) ---
        self.result_frame = ttk.Frame(self)
        self.result_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.result_var = tk.StringVar(value="")
        self.result_label = ttk.Label(self.result_frame, textvariable=self.result_var, foreground="#2e7d32")
        self.result_label.pack(side="left", padx=5)
        self.btn_open_folder = ttk.Button(
            self.result_frame, text="Open Output Folder", command=self.on_open_dist_folder
        )
        # only shown after a successful build
        self.btn_open_folder.pack_forget()

        # --- Bottom action bar ---
        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.btn_build = ttk.Button(action_frame, text="Build Project", command=self.on_build_clicked)
        self.btn_build.pack(side="left", padx=5)

        self.btn_cancel = ttk.Button(
            action_frame, text="Cancel", command=self.on_cancel_clicked, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(action_frame, textvariable=self.status_var).pack(side="left", padx=15)

    def _build_switches_tab(self):
        for name, label, default in PYINSTALLER_SWITCHES:
            var = tk.BooleanVar(value=default)
            self.switch_vars[name] = var
            ttk.Checkbutton(self.tab_switches, text=f"{label}  ({name})", variable=var).pack(
                anchor="w", padx=10, pady=3
            )

        sep = ttk.Separator(self.tab_switches, orient="horizontal")
        sep.pack(fill="x", padx=10, pady=10)

        distpath_frame = ttk.LabelFrame(self.tab_switches, text="Output Folder (--distpath)")
        distpath_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(
            distpath_frame,
            text=(
                "Optional. Leave empty to use PyInstaller's default './dist' NEXT TO the source file.\n"
                "Note: PyInstaller resolves --distpath relative to the current working directory of\n"
                "the invoking process by default (not relative to the script) - this tool always starts\n"
                "the build with the script's folder as working directory, so output never ends up in an\n"
                "unrelated project folder by accident."
            ),
            justify="left",
        ).pack(anchor="w", padx=5, pady=(5, 5))

        row = ttk.Frame(distpath_frame)
        row.pack(fill="x", padx=5, pady=(0, 8))
        self.distpath_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.distpath_var, width=70).pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.on_choose_distpath).pack(side="left", padx=5)

    def _build_excludes_tab(self):
        top = ttk.Frame(self.tab_excludes)
        top.pack(fill="x", padx=5, pady=5)
        ttk.Label(
            top,
            text="Pre-populated with known trouble makers (large packages, or ones often only imported transitively).",
        ).pack(anchor="w")

        self.excludes_list_frame = self._make_scrollable_grid(self.tab_excludes)
        self._exclude_grid_count = 0

        for name in DEFAULT_EXCLUDES:
            self._add_exclude_row(name, checked=True)

        sep = ttk.Separator(self.tab_excludes, orient="horizontal")
        sep.pack(fill="x", padx=5, pady=5)
        ttk.Label(self.tab_excludes, text="Further suggestions (not enabled by default):").pack(
            anchor="w", padx=5
        )
        for name in SUGGESTED_EXTRA_EXCLUDES:
            self._add_exclude_row(name, checked=False)

        add_frame = ttk.Frame(self.tab_excludes)
        add_frame.pack(fill="x", padx=5, pady=8)
        self.new_exclude_var = tk.StringVar()
        ttk.Entry(add_frame, textvariable=self.new_exclude_var, width=40).pack(side="left", padx=5)
        ttk.Button(add_frame, text="Add Module", command=self.on_add_exclude_manual).pack(
            side="left", padx=5
        )

    def _add_exclude_row(self, name: str, checked: bool):
        var = tk.BooleanVar(value=checked)
        self.exclude_vars[name] = var
        row, col = divmod(self._exclude_grid_count, GRID_COLUMNS)
        cb = ttk.Checkbutton(self.excludes_list_frame, text=name, variable=var)
        cb.grid(row=row, column=col, sticky="w", padx=10, pady=2)
        self._exclude_grid_count += 1

    def _build_binaries_tab(self):
        top = ttk.Frame(self.tab_binaries)
        top.pack(fill="x", padx=5, pady=5)
        ttk.Label(
            top,
            text=(
                "Suggestion list of known DLL cases - disabled by default.\n"
                "Please enable exactly what the current project needs (paths can vary by machine, "
                "e.g. NumPy DLLs with a build hash in the filename). Source paths of enabled entries "
                "are validated before the build starts.\n"
                "Note: the target folder is not just a label - it must match the folder the owning "
                "package expects the DLL in at runtime (see the Add Binary dialog for details)."
            ),
            justify="left",
        ).pack(anchor="w")

        self.binaries_list_frame = self._make_scrollable_grid(self.tab_binaries)
        self._binary_grid_count = 0

        for entry in DEFAULT_ADD_BINARIES:
            self._add_binary_row(entry["src"], entry["dest"], enabled=entry["enabled"])

        add_frame = ttk.LabelFrame(self.tab_binaries, text="Add New DLL")
        add_frame.pack(fill="x", padx=5, pady=8)

        self.new_binary_src_var = tk.StringVar()
        self.new_binary_dest_var = tk.StringVar()

        row1 = ttk.Frame(add_frame)
        row1.pack(fill="x", padx=5, pady=2)
        ttk.Label(row1, text="Source path (DLL):", width=15).pack(side="left")
        ttk.Entry(row1, textvariable=self.new_binary_src_var, width=70).pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(row1, text="Browse...", command=self.on_choose_binary_src).pack(side="left", padx=5)

        row2 = ttk.Frame(add_frame)
        row2.pack(fill="x", padx=5, pady=2)
        ttk.Label(row2, text="Target folder in bundle:", width=20).pack(side="left")
        ttk.Entry(row2, textvariable=self.new_binary_dest_var, width=40).pack(side="left", padx=5)
        ttk.Button(row2, text="Add DLL", command=self.on_add_binary_manual).pack(side="left", padx=5)

    def _add_binary_row(self, src: str, dest: str, enabled: bool):
        enabled_var = tk.BooleanVar(value=enabled)
        label_text = f"{src}   ->   {dest}"

        row, col = divmod(self._binary_grid_count, GRID_COLUMNS)
        cell = ttk.Frame(self.binaries_list_frame)
        cell.grid(row=row, column=col, sticky="w", padx=10, pady=2)

        cb = ttk.Checkbutton(cell, variable=enabled_var)
        cb.pack(side="left")
        lbl = ttk.Label(cell, text=label_text, wraplength=280, justify="left")
        lbl.pack(side="left", padx=5)

        self._binary_grid_count += 1

        self.add_binary_rows.append(
            {"enabled_var": enabled_var, "src": src, "dest": dest, "frame": cell}
        )

    def _build_log_tab(self):
        top = ttk.Frame(self.tab_log)
        top.pack(fill="x", padx=5, pady=5)

        self.full_log_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="Show full log (instead of the filtered view)", variable=self.full_log_var,
            command=self.on_toggle_full_log
        ).pack(side="left")

        ttk.Label(
            top,
            text="   Double-click a recognized line to pull the module/DLL into the matching list.",
        ).pack(side="left", padx=10)

        log_container = ttk.Frame(self.tab_log)
        log_container.pack(fill="both", expand=True, padx=5, pady=5)
        log_container.rowconfigure(0, weight=1)
        log_container.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_container, wrap="none", height=30, bg="#111318", fg="#d8dee9")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        vscroll = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(log_container, orient="horizontal", command=self.log_text.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self.log_text.tag_configure("module_hit", foreground="#f6c177", underline=True)
        self.log_text.tag_configure("dll_hit", foreground="#87d3f2", underline=True)
        self.log_text.tag_configure("analyzing_hit", foreground="#b5f4a5", underline=False)
        self.log_text.tag_configure("error_hit", foreground="#ff6b6b")
        self.log_text.tag_configure("success_hit", foreground="#69db7c")
        self.log_text.bind("<Double-Button-1>", self.on_log_double_click)

        def _on_log_mousewheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else 0
            self.log_text.yview_scroll(delta, "units")

        self.log_text.bind("<Enter>", lambda e: self.log_text.bind_all("<MouseWheel>", _on_log_mousewheel))
        self.log_text.bind("<Leave>", lambda e: self.log_text.unbind_all("<MouseWheel>"))
        # Shift+MouseWheel for horizontal scrolling, standard convention on Windows.
        self.log_text.bind(
            "<Shift-MouseWheel>",
            lambda e: self.log_text.xview_scroll(-1 * int(e.delta / 120) if e.delta else 0, "units"),
        )

    # ---------------- Event handlers ----------------

    def on_choose_source(self):
        path = filedialog.askopenfilename(
            title="Choose Python main script", filetypes=[("Python files", "*.py"), ("All files", "*.*")]
        )
        if not path:
            return
        self.source_var.set(path)
        self.config_obj = ProjectConfig(path)
        self._apply_config_to_ui()
        self._reset_result_display()
        msg = "loaded from " + self.config_obj.json_path if self.config_obj.loaded_from_disk else "newly created (defaults)"
        self.status_var.set(f"Project configuration {msg}.")

    def _apply_config_to_ui(self):
        if self.config_obj is None:
            return
        cfg = self.config_obj
        for name, var in self.switch_vars.items():
            var.set(cfg.switches.get(name, False))

        self.distpath_var.set(cfg.distpath)

        # Excludes: update existing checkboxes, add new ones (from JSON)
        for name, on in cfg.excludes.items():
            if name not in self.exclude_vars:
                self._add_exclude_row(name, checked=on)
            else:
                self.exclude_vars[name].set(on)

        # Add-binaries: rebuild the list entirely from the config
        for row in self.add_binary_rows:
            row["frame"].destroy()
        self.add_binary_rows.clear()
        self._binary_grid_count = 0
        for b in cfg.add_binaries:
            self._add_binary_row(b["src"], b["dest"], enabled=b.get("enabled", False))

    def on_add_exclude_manual(self):
        name = self.new_exclude_var.get().strip()
        if not name:
            return
        if name in self.exclude_vars:
            self.exclude_vars[name].set(True)
        else:
            self._add_exclude_row(name, checked=True)
        self.new_exclude_var.set("")

    def on_choose_binary_src(self):
        path = filedialog.askopenfilename(title="Choose DLL", filetypes=[("DLL/PYD", "*.dll;*.pyd"), ("All files", "*.*")])
        if path:
            self.new_binary_src_var.set(path)
            if not self.new_binary_dest_var.get().strip():
                self.new_binary_dest_var.set(guess_dest_from_source(path))

    def on_choose_distpath(self):
        path = filedialog.askdirectory(title="Choose output folder for --distpath")
        if path:
            self.distpath_var.set(path)

    def on_add_binary_manual(self):
        src = self.new_binary_src_var.get().strip()
        dest = self.new_binary_dest_var.get().strip()
        if not src or not dest:
            messagebox.showinfo("Missing information", "Please provide both a source path and a target folder.")
            return
        self._add_binary_row(src, dest, enabled=True)
        self.new_binary_src_var.set("")
        self.new_binary_dest_var.set("")

    def on_toggle_full_log(self):
        # Only affects future lines; reflected in the status line.
        mode = "Full" if self.full_log_var.get() else "Filtered"
        self.status_var.set(f"Log view: {mode}")

    def _sync_config_from_ui(self):
        cfg = self._require_config()
        cfg.switches = {name: var.get() for name, var in self.switch_vars.items()}
        cfg.excludes = {name: var.get() for name, var in self.exclude_vars.items()}
        cfg.distpath = self.distpath_var.get().strip()
        cfg.add_binaries = [
            {"src": row["src"], "dest": row["dest"], "enabled": row["enabled_var"].get()}
            for row in self.add_binary_rows
        ]

    def _reset_result_display(self):
        self.result_var.set("")
        self.btn_open_folder.pack_forget()

    def _validate_add_binary_paths(self, cfg: ProjectConfig) -> list[str]:
        """Checks whether all enabled add-binary source paths exist.
        Returns a list of missing paths (empty = all good)."""
        missing = []
        for b in cfg.active_add_binaries():
            if not os.path.isfile(b["src"]):
                missing.append(b["src"])
        return missing

    def on_build_clicked(self):
        if not self.source_var.get():
            messagebox.showwarning("No Source", "Please select a Python main script first.")
            return

        try:
            self._sync_config_from_ui()
        except ValueError as exc:
            messagebox.showwarning("No Source", str(exc))
            return

        cfg = self._require_config()

        # Pre-check: do all enabled add-binary source paths exist?
        missing_paths = self._validate_add_binary_paths(cfg)
        if missing_paths:
            details = "\n".join(f"  - {p}" for p in missing_paths)
            proceed = messagebox.askyesno(
                "Missing DLL Paths",
                "The following enabled add-binary source paths were not found:\n\n"
                f"{details}\n\n"
                "Proceed anyway (PyInstaller will likely fail), or go back to "
                "step 4 to fix the paths?\n\n"
                "Yes = proceed anyway, No = cancel and review the list.",
            )
            if not proceed:
                self.notebook.select(self.tab_binaries)
                self.status_var.set("Build cancelled: please fix the add-binary paths.")
                return

        cfg.save()
        cmd = cfg.build_command()

        self._reset_result_display()

        # Clear the log view so a new build never mixes with a previous
        # run's output.
        self.log_text.delete("1.0", "end")

        # Immediately switch to the Live Log tab so the user sees the build.
        self.notebook.select(self.tab_log)

        self._log_line(f"$ {' '.join(cmd)}", tags=())
        self._log_line(f"(Build working directory: {cfg.script_dir()})", tags=())

        log_dir = cfg.script_dir()
        full_log_path = os.path.join(log_dir, f"{cfg.script_stem()}.build.full.log")
        try:
            self.full_log_file = open(full_log_path, "w", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("Log File", f"Could not create the full log file:\n{exc}")
            self.full_log_file = None

        # PyInstaller resolves --distpath/--workpath relative to the
        # process cwd - this is why the build ALWAYS starts in the
        # script's folder, regardless of the terminal's working directory.
        self.runner = BuildRunner(cmd, cwd=log_dir, line_queue=self.log_queue)
        self.runner.start()

        self.btn_build.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status_var.set("Build running...")

    def on_cancel_clicked(self):
        if self.runner:
            self.runner.kill()
            self.status_var.set("Cancellation requested (hard kill)...")

    def on_open_dist_folder(self):
        if self.config_obj is None:
            return
        exe_path = self.config_obj.expected_exe_path()
        folder = os.path.dirname(exe_path)
        if os.path.isdir(folder):
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                self.status_var.set(f"Folder: {folder}")
        else:
            messagebox.showinfo("Folder Not Found", f"The folder no longer exists:\n{folder}")

    def on_log_double_click(self, event):
        index = self.log_text.index(f"@{event.x},{event.y}")
        line_start = f"{index.split('.')[0]}.0"
        line_end = f"{index.split('.')[0]}.end"
        line_text = self.log_text.get(line_start, line_end)

        category, value = classify_line(line_text)
        if category == "module" and value is not None:
            self._offer_add_exclude(value)
        elif category in ("dll", "analyzing_binary") and value is not None:
            self._offer_add_binary(value)
        else:
            # Fallback: just copy to clipboard
            self.clipboard_clear()
            self.clipboard_append(line_text)
            self.status_var.set("No module/DLL recognized - line copied to clipboard.")

    def _offer_add_exclude(self, module_name: str):
        answer = messagebox.askyesno(
            "Add Exclude",
            f"Module '{module_name}' was recognized in the log.\n\n"
            f"Add it as an exclude-module entry and enable it?",
        )
        if answer:
            if module_name in self.exclude_vars:
                self.exclude_vars[module_name].set(True)
            else:
                self._add_exclude_row(module_name, checked=True)
            self.status_var.set(f"'{module_name}' added to excludes. You can restart the build.")

    def _offer_add_binary(self, dll_path: str):
        # Pre-fill with a guess: the immediate parent folder name in
        # site-packages usually matches the Python package name that
        # expects the DLL at this exact location at runtime (this is NOT
        # just a display label - it is the actual extraction path inside
        # the bundle; see --add-binary DEST semantics).
        suggested_dest = guess_dest_from_source(dll_path)
        dest_guess = simpledialog.askstring(
            "Add Add-Binary Entry",
            f"DLL recognized:\n{dll_path}\n\n"
            "Enter the target folder inside the bundle.\n"
            "This is NOT just a label - it must match the folder the owning "
            "Python package expects the DLL in at runtime (usually the "
            "package's own site-packages folder name, pre-filled below). "
            "A wrong folder here can mean the DLL is bundled but never found "
            "at runtime:",
            initialvalue=suggested_dest,
        )
        if dest_guess:
            self._add_binary_row(dll_path, dest_guess.strip(), enabled=True)
            self.status_var.set(f"DLL '{os.path.basename(dll_path)}' added to add-binary.")

    # ---------------- Log processing ----------------

    def _log_line(self, text: str, tags=()):
        self.log_text.insert("end", text + "\n", tags)
        self.log_text.see("end")

    def _poll_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "__line__":
                    self._handle_build_line(payload)
                elif kind == "__error__":
                    self._log_line(f"[ERROR] {payload}", tags=("error_hit",))
                elif kind == "__done__":
                    self._handle_build_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _handle_build_line(self, line: str):
        if self.full_log_file:
            try:
                self.full_log_file.write(line + "\n")
            except Exception:
                pass

        show_full = self.full_log_var.get()

        # Routine Windows system-DLL dependency bookkeeping is suppressed
        # even in full-log mode's "relevant" pre-filter, but full mode
        # still gets to see literally everything if explicitly requested.
        if not show_full and is_noise_line(line):
            return

        category, _value = classify_line(line)

        if show_full or line_is_relevant(line) or category:
            tags = ()
            if category == "analyzing_binary":
                tags = ("analyzing_hit",)
            elif category == "module":
                tags = ("module_hit",)
            elif category == "dll":
                tags = ("dll_hit",)
            elif "error" in line.lower() or "traceback" in line.lower():
                tags = ("error_hit",)
            self._log_line(line, tags=tags)

    def _handle_build_done(self, return_code: int):
        self.btn_build.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

        if self.full_log_file:
            try:
                self.full_log_file.close()
            except Exception:
                pass
            self.full_log_file = None

        if return_code == 0:
            self.status_var.set("Build completed successfully.")
            self._log_line("=== BUILD SUCCEEDED ===", tags=("success_hit",))
            self._show_success_result()
        elif return_code == -1:
            self.status_var.set("Build could not be started.")
        else:
            self.status_var.set(f"Build ended with code {return_code} (possibly cancelled).")
            self._log_line(f"=== BUILD ENDED (code {return_code}) ===", tags=("error_hit",))

        self._check_warn_file()

    def _show_success_result(self):
        if self.config_obj is None:
            return
        exe_path = self.config_obj.expected_exe_path()
        if os.path.isfile(exe_path):
            self.result_var.set(f"EXE created: {exe_path}")
            self._log_line(f"EXE is located at: {exe_path}", tags=("success_hit",))
            self.btn_open_folder.pack(side="left", padx=5)
        else:
            self.result_var.set(
                f"Build reported success, but the expected EXE was not found at: {exe_path}"
            )
            self._log_line(
                f"Note: expected EXE not found at {exe_path}. "
                "Please check the --distpath setting.",
                tags=("error_hit",),
            )

    def _check_warn_file(self):
        if self.config_obj is None:
            return
        cfg = self.config_obj
        warn_path = cfg.warn_file_path()
        if not os.path.isfile(warn_path):
            return

        active_excludes = set(cfg.active_excludes())
        flagged: list[str] = []
        try:
            with open(warn_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = RE_WARN_MISSING.search(line)
                    if m:
                        mod = m.group(1)
                        top_level = mod.split(".")[0]
                        if top_level in active_excludes:
                            flagged.append(mod)
        except OSError:
            return

        if flagged:
            unique_flagged = sorted(set(flagged))
            self._log_line(
                "--- Note: the following modules reported as 'missing' are "
                "likely a consequence of your active excludes ---",
                tags=("error_hit",),
            )
            for mod in unique_flagged:
                self._log_line(f"   {mod}", tags=("error_hit",))
            self._log_line(
                "If one of these is actually needed: uncheck the exclude and rebuild.",
                tags=(),
            )


if __name__ == "__main__":
    app = PyInstallerBuilderApp()
    app.mainloop()
