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
  3. Maintain the exclude-module list (global catalog, shown unchecked
     by default; checking an entry only affects the current project)
  4. Maintain the add-binary list (same global-catalog behavior, keyed
     by DLL filename so the same DLL can be re-pathed per machine by
     double-clicking its entry)
  5. "Build Project" validates that all enabled add-binary source paths
     exist, clears the log view, then automatically switches to the Live
     Log tab and starts the build

  Global catalog vs. per-project selection: exclude-module names and
  add-binary entries that have ever been used across ANY project are
  accumulated in a global catalog file
  (pyinstaller_builder.catalog.json, next to this program), independent
  of whether they are currently checked. Loading a project only decides
  which of these globally known entries are CHECKED for that particular
  project. Add-binary entries are keyed by DLL filename (not full path),
  since the same DLL often lives at different absolute paths on
  different machines; double-clicking an entry lets you edit its stored
  path/target in place. The catalog is written whenever a build starts,
  and once more on program exit if a project was loaded.

  A note on locating the catalog file when frozen with PyInstaller: the
  catalog must live next to the actual .exe the user runs, not next to
  a temporary, per-run extraction folder. Using __file__ for this would
  break once this tool itself is packaged into a onefile EXE, because
  __file__ then points into a fresh temporary _MEIxxxxxx folder that is
  recreated (and later deleted) on every single launch - the catalog
  would silently reset itself every time the program starts. The
  correct, PyInstaller-recommended approach is to branch on sys.frozen
  and use os.path.dirname(sys.executable) instead, which always points
  to wherever the actual .exe file has been placed and therefore stays
  stable across restarts.

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
Global catalog (accumulated excludes/add-binaries across all projects)
is stored next to this program (the .py file, or the .exe if this tool
itself has been packaged with PyInstaller):
    pyinstaller_builder.catalog.json

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

PYINSTALLER_SWITCHES = [
    ("--onefile", "Onefile (single EXE)", True),
    ("--onedir", "Onedir (folder with EXE + dependencies)", False),
    ("--windowed", "Windowed / --noconsole (suppress console window)", False),
    ("--noconfirm", "Overwrite existing output without prompting", True),
    ("--clean", "Clean build cache before running", False),
    ("--noupx", "Disable UPX compression", False),
    ("--debug=all", "Debug build (bootloader diagnostics)", False),
]

GRID_COLUMNS = 3

CATALOG_FILENAME = "pyinstaller_builder.catalog.json"


def get_app_dir() -> str:
    """Returns the folder this program itself lives in - stable across
    restarts whether run as a plain .py script or as a PyInstaller-frozen
    .exe. Uses sys.executable when frozen instead of __file__, since
    __file__ would otherwise point into a temporary, per-launch
    extraction folder in onefile mode."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def dll_catalog_key(path: str) -> str:
    """Identity used for add-binary catalog entries: the bare filename,
    lower-cased. The same DLL frequently lives at different absolute
    paths on different machines (e.g. NumPy build-hash filenames), so
    keying by filename lets one catalog entry represent "this DLL",
    editable in place, rather than accumulating duplicate near-identical
    entries per machine."""
    return os.path.basename(path.replace("\\", "/")).strip().lower()


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

RE_NOISE_DEP_PROCESSING = re.compile(r"Processing dependency, name:", re.IGNORECASE)
RE_NOISE_DEP_SKIP = re.compile(
    r"Skipping dependency .* due to (global exclusion rules|prior processing)", re.IGNORECASE
)

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
# Global catalog: accumulates every exclude-module name and add-binary
# entry ever used across all projects, independent of per-project
# checked state.
# ----------------------------------------------------------------------


class GlobalCatalog:
    def __init__(self):
        self.path = self._catalog_path()
        self.exclude_names: list[str] = list(dict.fromkeys(DEFAULT_EXCLUDES + SUGGESTED_EXTRA_EXCLUDES))
        self.add_binaries: dict[str, dict[str, str]] = {
            dll_catalog_key(b["src"]): {"src": b["src"], "dest": b["dest"]} for b in DEFAULT_ADD_BINARIES
        }
        self.load_if_exists()

    @staticmethod
    def _catalog_path() -> str:
        return os.path.join(get_app_dir(), CATALOG_FILENAME)

    def load_if_exists(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_excludes = data.get("exclude_names", [])
                for name in loaded_excludes:
                    if name not in self.exclude_names:
                        self.exclude_names.append(name)
                loaded_binaries = data.get("add_binaries", {})
                for key, entry in loaded_binaries.items():
                    self.add_binaries[key] = {"src": entry["src"], "dest": entry["dest"]}
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                messagebox.showwarning(
                    "Could Not Load Global Catalog",
                    f"The file\n{self.path}\ncould not be read:\n{exc}\n\n"
                    "Falling back to built-in defaults only.",
                )

    def save(self) -> bool:
        """Returns True on success. On failure, shows a visible error
        dialog and returns False, so callers can react if needed."""
        data = {
            "exclude_names": self.exclude_names,
            "add_binaries": self.add_binaries,
        }
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except OSError as exc:
            messagebox.showerror(
                "Could Not Save Global Catalog",
                f"Failed to write the global catalog to:\n{self.path}\n\n"
                f"Error: {exc}\n\n"
                "This usually means the folder is not writable from here (e.g. the "
                "EXE is located under a protected folder such as 'Program Files', "
                "or security software is blocking the write). Move the EXE to a "
                "folder you have full write access to, or run it elevated.",
            )
            return False

    def register_exclude(self, name: str) -> None:
        if name not in self.exclude_names:
            self.exclude_names.append(name)

    def register_add_binary(self, src: str, dest: str) -> None:
        key = dll_catalog_key(src)
        self.add_binaries[key] = {"src": src, "dest": dest}

    def update_add_binary(self, key: str, src: str, dest: str) -> None:
        self.add_binaries[key] = {"src": src, "dest": dest}


# ----------------------------------------------------------------------
# Project configuration (JSON next to the source file)
# ----------------------------------------------------------------------


class ProjectConfig:
    def __init__(self, script_path: str, catalog: GlobalCatalog):
        self.script_path = script_path
        self.json_path = self._json_path_for(script_path)
        self.catalog = catalog
        self.switches: dict[str, bool] = {name: default for name, _, default in PYINSTALLER_SWITCHES}
        self.checked_excludes: set[str] = set(DEFAULT_EXCLUDES)
        self.checked_binary_keys: set[str] = set()
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
                self.distpath = data.get("distpath", self.distpath)

                if "checked_excludes" in data or "checked_binary_keys" in data:
                    self.checked_excludes = set(data.get("checked_excludes", []))
                    self.checked_binary_keys = set(data.get("checked_binary_keys", []))
                else:
                    old_excludes = data.get("excludes", {})
                    self.checked_excludes = {name for name, on in old_excludes.items() if on}
                    old_binaries = data.get("add_binaries", [])
                    self.checked_binary_keys = set()
                    for b in old_binaries:
                        if b.get("enabled", False):
                            key = dll_catalog_key(b["src"])
                            self.catalog.register_add_binary(b["src"], b["dest"])
                            self.checked_binary_keys.add(key)
                    for name in old_excludes.keys():
                        self.catalog.register_exclude(name)

                for name in self.checked_excludes:
                    self.catalog.register_exclude(name)

                self.loaded_from_disk = True
            except (json.JSONDecodeError, OSError) as exc:
                messagebox.showwarning(
                    "Could Not Load Configuration",
                    f"The file\n{self.json_path}\ncould not be read:\n{exc}\n\n"
                    "Default values will be used.",
                )

    def save(self) -> bool:
        data = {
            "script_path": self.script_path,
            "switches": self.switches,
            "checked_excludes": sorted(self.checked_excludes),
            "checked_binary_keys": sorted(self.checked_binary_keys),
            "distpath": self.distpath,
        }
        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except OSError as exc:
            messagebox.showerror(
                "Could Not Save Project Configuration",
                f"Failed to write the project configuration to:\n{self.json_path}\n\n"
                f"Error: {exc}",
            )
            return False

    def active_switches(self) -> list[str]:
        return [name for name, on in self.switches.items() if on]

    def active_excludes(self) -> list[str]:
        return sorted(self.checked_excludes)

    def active_add_binaries(self) -> list[AddBinaryEntry]:
        result: list[AddBinaryEntry] = []
        for key in self.checked_binary_keys:
            entry = self.catalog.add_binaries.get(key)
            if entry:
                result.append({"src": entry["src"], "dest": entry["dest"], "enabled": True})
        return result

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
        return self.switches.get("--onefile", False) or not self.switches.get("--onedir", False)

    def expected_exe_path(self) -> str:
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

        self.catalog = GlobalCatalog()
        self.config_obj: Optional[ProjectConfig] = None
        self.runner: Optional[BuildRunner] = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self.full_log_file = None

        self.switch_vars: dict[str, tk.BooleanVar] = {}
        self.exclude_vars: dict[str, tk.BooleanVar] = {}
        self.binary_rows: dict[str, dict] = {}

        self._build_ui()
        self._populate_from_catalog()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_log_queue)

    def _require_config(self) -> ProjectConfig:
        """Ensures a project configuration exists before it is accessed.
        Creates a new one from the currently chosen source if needed."""
        if self.config_obj is None:
            source = self.source_var.get().strip()
            if not source:
                raise ValueError("No source script has been selected yet.")
            self.config_obj = ProjectConfig(source, self.catalog)
            self._apply_config_to_ui()
        return self.config_obj

    def _make_scrollable_grid(self, parent) -> ttk.Frame:
        """Creates a vertically scrollable area (canvas + inner frame) and
        returns the inner frame that content should be gridded into."""
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

    def _build_ui(self):
        frame_source = ttk.LabelFrame(self, text="1. Project Source")
        frame_source.pack(fill="x", padx=10, pady=(10, 5))

        self.source_var = tk.StringVar()
        ttk.Entry(frame_source, textvariable=self.source_var, width=90).pack(
            side="left", padx=5, pady=5, fill="x", expand=True
        )
        ttk.Button(frame_source, text="Browse...", command=self.on_choose_source).pack(
            side="left", padx=5, pady=5
        )

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

        self.result_frame = ttk.Frame(self)
        self.result_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.result_var = tk.StringVar(value="")
        self.result_label = ttk.Label(self.result_frame, textvariable=self.result_var, foreground="#2e7d32")
        self.result_label.pack(side="left", padx=5)
        self.btn_open_folder = ttk.Button(
            self.result_frame, text="Open Output Folder", command=self.on_open_dist_folder
        )
        self.btn_open_folder.pack_forget()

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

        # Ensure the main window (and thus its widgets, e.g. the exclude
        # entry field) reliably has keyboard focus right from the start.
        self.after(50, lambda: (self.focus_force(), self.lift()))

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
            text=(
                "Shows every exclude-module name ever used across all projects (global catalog), "
                "unchecked by default. Checking one here only affects the currently loaded project."
            ),
        ).pack(anchor="w")

        self.excludes_list_frame = self._make_scrollable_grid(self.tab_excludes)
        self._exclude_grid_count = 0

        add_frame = ttk.Frame(self.tab_excludes)
        add_frame.pack(fill="x", padx=5, pady=8)
        self.new_exclude_var = tk.StringVar()
        exclude_entry = ttk.Entry(add_frame, textvariable=self.new_exclude_var, width=40)
        exclude_entry.pack(side="left", padx=5)
        ttk.Button(add_frame, text="Add Module", command=self.on_add_exclude_manual).pack(
            side="left", padx=5
        )
        # Enter key as a convenient shortcut, and an explicit click-to-focus
        # binding as a defensive measure against any stray focus grabs.
        exclude_entry.bind("<Return>", lambda e: self.on_add_exclude_manual())
        exclude_entry.bind("<Button-1>", lambda e: exclude_entry.focus_set())

    def _add_exclude_row(self, name: str, checked: bool):
        if name in self.exclude_vars:
            self.exclude_vars[name].set(checked)
            return
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
                "Shows every add-binary entry ever used across all projects (global catalog), keyed by "
                "DLL filename, unchecked by default. Checking one here only affects the currently loaded "
                "project. Double-click an entry to edit its source path / target folder in place (e.g. "
                "after moving to a different machine).\n"
                "Note: the target folder is not just a label - it must match the folder the owning "
                "package expects the DLL in at runtime."
            ),
            justify="left",
        ).pack(anchor="w")

        self.binaries_list_frame = self._make_scrollable_grid(self.tab_binaries)
        self._binary_grid_count = 0

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

    def _add_or_update_binary_row(self, key: str, src: str, dest: str, checked: bool):
        if key in self.binary_rows:
            row = self.binary_rows[key]
            row["src"] = src
            row["dest"] = dest
            row["label_var"].set(f"{src}\n->  {dest}")
            row["enabled_var"].set(checked)
            return

        enabled_var = tk.BooleanVar(value=checked)
        label_var = tk.StringVar(value=f"{src}\n->  {dest}")

        row_idx, col = divmod(self._binary_grid_count, GRID_COLUMNS)
        cell = ttk.Frame(self.binaries_list_frame)
        cell.grid(row=row_idx, column=col, sticky="w", padx=10, pady=2)

        cb = ttk.Checkbutton(cell, variable=enabled_var)
        cb.pack(side="left", anchor="n")
        lbl = ttk.Label(cell, textvariable=label_var, wraplength=280, justify="left", cursor="hand2")
        lbl.pack(side="left", padx=5)
        lbl.bind("<Double-Button-1>", lambda e, k=key: self.on_edit_binary_entry(k))

        self._binary_grid_count += 1

        self.binary_rows[key] = {
            "enabled_var": enabled_var,
            "label_var": label_var,
            "src": src,
            "dest": dest,
            "cell": cell,
        }

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
        self.log_text.bind(
            "<Shift-MouseWheel>",
            lambda e: self.log_text.xview_scroll(-1 * int(e.delta / 120) if e.delta else 0, "units"),
        )

    def _populate_from_catalog(self):
        for name in self.catalog.exclude_names:
            self._add_exclude_row(name, checked=False)
        for key, entry in self.catalog.add_binaries.items():
            self._add_or_update_binary_row(key, entry["src"], entry["dest"], checked=False)

    def _refresh_catalog_widgets(self):
        for name in self.catalog.exclude_names:
            if name not in self.exclude_vars:
                self._add_exclude_row(name, checked=False)
        for key, entry in self.catalog.add_binaries.items():
            if key not in self.binary_rows:
                self._add_or_update_binary_row(key, entry["src"], entry["dest"], checked=False)

    def on_choose_source(self):
        path = filedialog.askopenfilename(
            title="Choose Python main script", filetypes=[("Python files", "*.py"), ("All files", "*.*")]
        )
        if not path:
            return
        self.source_var.set(path)
        self.config_obj = ProjectConfig(path, self.catalog)
        self._refresh_catalog_widgets()
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

        for var in self.exclude_vars.values():
            var.set(False)
        for name in cfg.checked_excludes:
            if name not in self.exclude_vars:
                self._add_exclude_row(name, checked=True)
            else:
                self.exclude_vars[name].set(True)

        for row in self.binary_rows.values():
            row["enabled_var"].set(False)
        for key in cfg.checked_binary_keys:
            if key in self.binary_rows:
                self.binary_rows[key]["enabled_var"].set(True)

    def on_add_exclude_manual(self):
        name = self.new_exclude_var.get().strip()
        if not name:
            return
        self.catalog.register_exclude(name)
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
        key = dll_catalog_key(src)
        self.catalog.register_add_binary(src, dest)
        self._add_or_update_binary_row(key, src, dest, checked=True)
        self.new_binary_src_var.set("")
        self.new_binary_dest_var.set("")

    def on_edit_binary_entry(self, key: str):
        row = self.binary_rows.get(key)
        if row is None:
            return
        new_src = simpledialog.askstring(
            "Edit Source Path",
            "Adjust the source path for this DLL (e.g. after moving to a different machine):",
            initialvalue=row["src"],
        )
        if new_src is None:
            return
        new_dest = simpledialog.askstring(
            "Edit Target Folder",
            "Adjust the target folder inside the bundle:",
            initialvalue=row["dest"],
        )
        if new_dest is None:
            return
        new_src = new_src.strip()
        new_dest = new_dest.strip()
        if not new_src or not new_dest:
            return

        new_key = dll_catalog_key(new_src)
        checked = row["enabled_var"].get()

        if new_key != key:
            self.catalog.add_binaries.pop(key, None)
            row["cell"].destroy()
            del self.binary_rows[key]
            self.catalog.register_add_binary(new_src, new_dest)
            self._add_or_update_binary_row(new_key, new_src, new_dest, checked=checked)
        else:
            self.catalog.update_add_binary(key, new_src, new_dest)
            self._add_or_update_binary_row(key, new_src, new_dest, checked=checked)

        self.status_var.set(f"Updated catalog entry for '{os.path.basename(new_src)}'.")

    def on_toggle_full_log(self):
        mode = "Full" if self.full_log_var.get() else "Filtered"
        self.status_var.set(f"Log view: {mode}")

    def _sync_config_from_ui(self):
        cfg = self._require_config()
        cfg.switches = {name: var.get() for name, var in self.switch_vars.items()}
        cfg.distpath = self.distpath_var.get().strip()
        cfg.checked_excludes = {name for name, var in self.exclude_vars.items() if var.get()}
        cfg.checked_binary_keys = {key for key, row in self.binary_rows.items() if row["enabled_var"].get()}

    def _reset_result_display(self):
        self.result_var.set("")
        self.btn_open_folder.pack_forget()

    def _validate_add_binary_paths(self, cfg: ProjectConfig) -> list[str]:
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
        self.catalog.save()
        cmd = cfg.build_command()

        self._reset_result_display()
        self.log_text.delete("1.0", "end")
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

        self.runner = BuildRunner(cmd, cwd=log_dir, line_queue=self.log_queue)
        self.runner.start()

        self.btn_build.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status_var.set("Build running...")

    def on_cancel_clicked(self):
        if self.runner:
            self.runner.kill()
            self.status_var.set("Cancellation requested (hard kill)...")

    def on_close(self):
        try:
            if self.config_obj is not None:
                self._sync_config_from_ui()
                self.config_obj.save()
            self.catalog.save()
        except Exception:
            pass
        self.destroy()

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
            self.catalog.register_exclude(module_name)
            self._add_exclude_row(module_name, checked=True)
            self.status_var.set(f"'{module_name}' added to excludes. You can restart the build.")

    def _offer_add_binary(self, dll_path: str):
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
            key = dll_catalog_key(dll_path)
            self.catalog.register_add_binary(dll_path, dest_guess.strip())
            self._add_or_update_binary_row(key, dll_path, dest_guess.strip(), checked=True)
            self.status_var.set(f"DLL '{os.path.basename(dll_path)}' added to add-binary.")

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
