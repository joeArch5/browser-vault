from __future__ import annotations

import os
import functools
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
from types import SimpleNamespace

from browser_backup import applock, config, library
from browser_backup.browser import close_browser, is_running
from browser_backup.browsers import BROWSERS, BrowserSpec, browser_containing, get
from browser_backup.manager import create_backup, restore_backup, wipe_browser
from browser_backup.passwords import check_backup_password

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

FONT = "Segoe UI" if sys.platform == "win32" else None
FILETYPES = [("Browser Backup Vault", "*.bbvault"), ("All files", "*.*")]
CARD_MAX_WIDTH = 820  # forms stay readable on very wide windows
AUTO_LOCK_SECONDS = 10 * 60  # the app locks itself after this much idle time (lock must be on)


_open_dialogs = 0  # native dialogs currently open; the idle auto-lock waits for them


def _tracked(func):
    """Wrap a blocking tkinter dialog so the app knows one is open."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _open_dialogs
        _open_dialogs += 1
        try:
            return func(*args, **kwargs)
        finally:
            _open_dialogs -= 1
    return wrapper


dialogs = SimpleNamespace(**{name: _tracked(getattr(module, name)) for module, name in (
    (messagebox, "showinfo"), (messagebox, "showwarning"), (messagebox, "showerror"),
    (messagebox, "askyesno"), (filedialog, "askdirectory"), (filedialog, "askopenfilename"))})


def resource_path(relative_path: str) -> str:
    """Return a bundled resource path (works in development and PyInstaller builds)."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(bundle_dir / relative_path)


C = {
    "bg": "#0A0E16", "panel": "#111827", "card": "#172033", "hover": "#1F2D44",
    "line": "#243047", "text": "#F3F4F6", "muted": "#9CA3AF", "dim": "#64748B",
    "green": "#059669", "green_h": "#047857", "accent": "#34D399", "sel": "#0E3B31",
    "red": "#B91C1C", "red_h": "#991B1B", "red_text": "#FCA5A5",
}


def font(size: int = 12, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def open_folder(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except OSError as exc:
        dialogs.showerror("Could not open folder", str(exc))


class AppButton(ctk.CTkButton):
    """A button that remembers its normal colours so they can be restored after disabling it."""

    def __init__(self, master, *, enabled_colors: tuple[str, str], **kwargs):
        super().__init__(master, fg_color=enabled_colors[0], hover_color=enabled_colors[1],
                         **kwargs)
        self.enabled_colors = enabled_colors


class TextDialog(ctk.CTkToplevel):
    """Modal text prompt (masked or plain). .result is None if cancelled."""

    def __init__(self, parent, title: str, prompt: str, button: str, placeholder: str = "",
                 masked: bool = True, color: str = C["green"], hover: str = C["green_h"]):
        super().__init__(parent)
        self.title(title)
        self.configure(fg_color=C["panel"])
        self.resizable(False, False)
        self.result: str | None = None
        w, h = 460, 220
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        self.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        ctk.CTkLabel(self, text=prompt, text_color=C["text"], wraplength=w - 44, justify="left",
                     font=font(13)).pack(padx=22, pady=(22, 12), anchor="w")
        self.entry = ctk.CTkEntry(self, show="•" if masked else "", height=42,
                                  placeholder_text=placeholder, fg_color=C["bg"],
                                  border_color=C["line"], font=font(13))
        self.entry.pack(padx=22, fill="x")
        self.entry.bind("<Return>", lambda _e: self._ok())
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=22, pady=20, fill="x")
        row.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(row, text="Cancel", height=38, fg_color=C["line"], hover_color=C["hover"],
                      font=font(12, True), command=self.destroy
                      ).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(row, text=button, height=38, fg_color=color, hover_color=hover,
                      font=font(12, True), command=self._ok
                      ).grid(row=0, column=1, padx=(6, 0), sticky="ew")
        self.transient(parent)
        self.after(150, lambda: (self.grab_set(), self.entry.focus_set()))

    def _ok(self):
        self.result = self.entry.get()
        self.destroy()


class LockScreen(ctk.CTkFrame):
    """Full-window password gate. mode: "setup" (first run, choose a password) or "login"."""

    def __init__(self, master, mode: str, on_unlock):
        super().__init__(master, fg_color=C["bg"], corner_radius=0)
        self.mode = mode
        self.on_unlock = on_unlock
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure((0, 2), weight=1)

        card = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=18, width=460,
                            border_width=1, border_color=C["line"])
        card.grid(row=1, column=0)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text="BROWSER", font=font(24, True), text_color=C["text"]
                     ).grid(row=0, column=0, padx=36, pady=(34, 0))
        ctk.CTkLabel(card, text="BACKUP MANAGER", font=font(24, True), text_color=C["accent"]
                     ).grid(row=1, column=0, padx=36)
        setup = mode == "setup"
        ctk.CTkLabel(
            card, font=font(12), text_color=C["muted"], wraplength=380, justify="center",
            text=("Choose an app password that will be asked every time you open this app, or "
                  "continue without a lock. You can turn it on or off later in Security. "
                  "It is separate from the encryption password of your backups."
                  if setup else "Enter your app password to continue.")
        ).grid(row=2, column=0, padx=36, pady=(12, 18))

        self.entry = ctk.CTkEntry(card, height=44, show="•", font=font(14), fg_color=C["bg"],
                                  border_color=C["line"], width=380,
                                  placeholder_text="App password" if not setup else
                                  f"Choose a password ({applock.MIN_LENGTH}+ characters)")
        self.entry.grid(row=3, column=0, padx=36)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.confirm: ctk.CTkEntry | None = None
        if setup:
            self.confirm = ctk.CTkEntry(card, height=44, show="•", font=font(14), width=380,
                                        fg_color=C["bg"], border_color=C["line"],
                                        placeholder_text="Confirm password")
            self.confirm.grid(row=4, column=0, padx=36, pady=(8, 0))
            self.confirm.bind("<Return>", lambda _e: self._submit())
        self.show_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Show password", variable=self.show_var, text_color=C["muted"],
                        font=font(12), checkbox_width=18, checkbox_height=18, corner_radius=5,
                        border_width=2, fg_color=C["green"], hover_color=C["green_h"],
                        command=self._toggle
                        ).grid(row=5, column=0, padx=36, pady=(12, 0), sticky="w")
        self.message = ctk.CTkLabel(card, text="", font=font(12), text_color=C["red_text"],
                                    wraplength=380, justify="center")
        self.message.grid(row=6, column=0, padx=36, pady=(10, 0))
        self.button = ctk.CTkButton(card, text="Create password and open" if setup else "Unlock",
                                    height=46, corner_radius=10, fg_color=C["green"],
                                    hover_color=C["green_h"], font=font(13, True),
                                    text_color=C["text"], text_color_disabled=C["dim"],
                                    command=self._submit)
        self.button.grid(row=7, column=0, padx=36, pady=(14, 10 if setup else 34), sticky="ew")
        if setup:
            ctk.CTkButton(card, text="Continue without app lock", height=40, corner_radius=10,
                          fg_color="transparent", hover_color=C["hover"], text_color=C["muted"],
                          font=font(12, True), command=self._skip
                          ).grid(row=8, column=0, padx=36, pady=(0, 30), sticky="ew")

        self._ticking = False
        if not setup:
            try:
                if applock.seconds_locked():
                    self._countdown()
            except applock.LockFileError as exc:
                self._fail(str(exc) + "\n\nFix or delete this file to continue.", lock=True)
        self.after(200, self.entry.focus_set)

    def _toggle(self):
        show = "" if self.show_var.get() else "•"
        self.entry.configure(show=show)
        if self.confirm is not None:
            self.confirm.configure(show=show)

    def _fail(self, text: str, lock: bool = False):
        self.message.configure(text=text, text_color=C["red_text"])
        if lock:
            self.button.configure(state="disabled")

    def _countdown(self):
        """Disable the button and show the remaining wait after too many wrong attempts."""
        try:
            remaining = applock.seconds_locked()
        except applock.LockFileError:
            remaining = 0
        if remaining <= 0:
            self._ticking = False
            self.button.configure(state="normal")
            self.message.configure(text="")
            return
        self._ticking = True
        self.button.configure(state="disabled")
        self.message.configure(text=f"Too many wrong attempts. Try again in {remaining} s.",
                               text_color=C["red_text"])
        self.after(1000, self._countdown)

    def _skip(self):
        """First run only: use the app without a lock (turn it on later in Security)."""
        try:
            applock.skip_setup()
        except (OSError, ValueError) as exc:
            return self._fail(f"Could not save your choice: {exc}")
        self.master.after(0, self.on_unlock)

    def _submit(self):
        if self._ticking:
            return
        password = self.entry.get()
        if self.mode == "setup":
            problem = applock.check_new_password(password)
            if problem:
                return self._fail(problem)
            if self.confirm is None or password != self.confirm.get():
                return self._fail("The two passwords do not match.")
            try:
                applock.set_password(password)
            except OSError as exc:
                return self._fail(f"Could not save the password: {exc}")
        else:
            try:
                ok, wait = applock.verify(password)
            except applock.LockFileError as exc:
                return self._fail(str(exc), lock=True)
            except OSError as exc:
                return self._fail(f"Could not check the password: {exc}")
            if not ok:
                self.entry.delete(0, "end")
                if wait:
                    return self._countdown()
                return self._fail("Incorrect password.")
        self.entry.delete(0, "end")
        self.master.after(0, self.on_unlock)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Set the actual window icon; PyInstaller's --icon only sets the EXE file icon.
        # Keep a native HICON handle alive so Windows uses the custom icon in the
        # title bar and taskbar rather than falling back to Tk's default icon.
        self._native_app_icon = None
        try:
            icon_path = resource_path("app.ico")
            self.iconbitmap(icon_path)
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.LoadImageW.restype = wintypes.HANDLE
                user32.LoadImageW.argtypes = (
                    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                    ctypes.c_int, ctypes.c_int, wintypes.UINT
                )
                hicon = user32.LoadImageW(
                    None, icon_path, 1, 0, 0, 0x0010 | 0x0040
                )  # IMAGE_ICON, LR_LOADFROMFILE | LR_DEFAULTSIZE
                if hicon:
                    self._native_app_icon = hicon
                    hwnd = wintypes.HWND(self.winfo_id())
                    user32.SendMessageW(hwnd, 0x0080, 0, hicon)  # WM_SETICON, ICON_SMALL
                    user32.SendMessageW(hwnd, 0x0080, 1, hicon)  # WM_SETICON, ICON_BIG
        except Exception:
            # Keep the app usable if the optional icon file is missing or invalid.
            pass
        self.title("Browser Backup Manager")
        self.geometry("1120x720")
        self.minsize(860, 560)
        self.configure(fg_color=C["bg"])

        self.backup_dir: Path = config.get_backup_dir()
        self._ui_queue: queue.Queue = queue.Queue()  # worker threads never touch widgets
        self._last_activity = time.monotonic()
        self._init_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        for event in ("<Key>", "<Button>", "<Motion>"):
            self.bind_all(event, self._touch, add="+")
        self.after(100, self._pump)
        self.after(15_000, self._idle_check)
        self._show_lock()

    # ---------------------------------------------------------------- thread-safe UI plumbing
    def _post(self, fn):
        """Called from worker threads: run `fn` later on the main (UI) thread."""
        self._ui_queue.put(fn)

    def _pump(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()
        self.after(100, self._pump)

    def _on_close(self):
        if self.busy:
            dialogs.showwarning("Operation running",
                                "A backup, restore or reset is still running. Closing now "
                                "could damage your data. Please wait until it finishes.")
            return
        self.destroy()

    def _touch(self, _event=None):
        self._last_activity = time.monotonic()

    def _idle_check(self):
        if (self.ready and not self.busy and applock.is_set() and not _open_dialogs
                and time.monotonic() - self._last_activity > AUTO_LOCK_SECONDS
                and not any(isinstance(w, ctk.CTkToplevel) for w in self.winfo_children())):
            self._show_lock()
        self.after(15_000, self._idle_check)

    def _init_state(self):
        self.selected: library.BackupEntry | None = None
        self.rows: dict[Path, ctk.CTkFrame] = {}
        self.ready = False
        self.busy = False
        self.current_page = ""
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.pages: dict[str, ctk.CTkFrame] = {}

    def _clear(self):
        for widget in self.winfo_children():
            widget.destroy()

    def _show_lock(self):
        """Show the password gate. Nothing of the real UI exists until it is unlocked."""
        self._clear()
        self._init_state()
        if applock.is_set():
            mode = "login"
        elif applock.is_disabled():
            self._start_app()  # the user turned the lock off: no gate
            return
        else:
            mode = "setup"
        self.lock_view = LockScreen(self, mode, self._start_app)
        self.lock_view.pack(fill="both", expand=True)

    def _start_app(self):
        self._clear()
        self._init_state()
        self.shell = ctk.CTkFrame(self, fg_color="transparent")
        self.shell.pack(fill="both", expand=True)
        self._build()
        self.ready = True
        self._on_browser_change()
        self.show_page("backups")

    def lock_now(self):
        if not applock.is_set():
            dialogs.showinfo("App lock is off", "Turn the app lock on in Security first.")
            return
        if self.busy:
            dialogs.showinfo("Please wait",
                             "An operation is running. Lock the app when it finishes.")
            return
        self._show_lock()

    # ================================================================== layout
    def _build(self):
        self.shell.grid_columnconfigure(1, weight=1)
        self.shell.grid_rowconfigure(0, weight=1)
        self._build_sidebar()

        self.content = ctk.CTkFrame(self.shell, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(26, 22), pady=(22, 10))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.pages["backups"] = self._build_backups_page()
        self.pages["create"] = self._build_create_page()
        self.pages["reset"] = self._build_reset_page()
        self.pages["security"] = self._build_security_page()
        self._build_statusbar()
        self._refresh_security()

    def _build_sidebar(self):
        side = ctk.CTkFrame(self.shell, fg_color=C["panel"], corner_radius=0, width=252,
                            border_width=0)
        side.grid(row=0, column=0, rowspan=2, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(side, text="BROWSER", font=font(20, True), text_color=C["text"]
                     ).grid(row=0, column=0, padx=22, pady=(26, 0), sticky="w")
        ctk.CTkLabel(side, text="BACKUP MANAGER", font=font(20, True), text_color=C["accent"]
                     ).grid(row=1, column=0, padx=22, pady=(0, 2), sticky="w")
        ctk.CTkLabel(side, text="Local  •  Encrypted  •  AES-256-GCM", font=font(10),
                     text_color=C["dim"]).grid(row=2, column=0, padx=23, pady=(0, 22), sticky="w")

        ctk.CTkLabel(side, text="BROWSER", font=font(10, True), text_color=C["dim"]
                     ).grid(row=3, column=0, padx=24, sticky="w")
        self.labels = {self._label(b): b for b in BROWSERS}
        first = next((label for label, b in self.labels.items() if b.installed),
                     next(iter(self.labels)))
        self.browser_var = ctk.StringVar(value=first)
        ctk.CTkOptionMenu(side, values=list(self.labels), variable=self.browser_var, height=40,
                          font=font(13), dropdown_font=font(13), fg_color=C["card"],
                          button_color=C["line"], button_hover_color=C["hover"],
                          dropdown_fg_color=C["card"], dropdown_hover_color=C["hover"],
                          command=self._on_browser_change
                          ).grid(row=4, column=0, padx=18, pady=(4, 20), sticky="ew")

        nav = ctk.CTkFrame(side, fg_color="transparent")
        nav.grid(row=5, column=0, padx=12, sticky="new")
        nav.grid_columnconfigure(0, weight=1)
        for i, (key, text) in enumerate([("backups", "Backups"), ("create", "Create backup"),
                                         ("reset", "Reset browser"),
                                         ("security", "Security")]):
            btn = ctk.CTkButton(nav, text=text, anchor="w", height=42, corner_radius=10,
                                fg_color="transparent", hover_color=C["hover"],
                                text_color=C["muted"], font=font(13, True),
                                command=lambda k=key: self.show_page(k))
            btn.grid(row=i, column=0, sticky="ew", pady=2)
            self.nav_buttons[key] = btn
        self.lock_btn = ctk.CTkButton(nav, text="Lock now", anchor="w", height=42,
                                      corner_radius=10, fg_color="transparent",
                                      hover_color=C["hover"], text_color=C["muted"],
                                      font=font(13, True), command=self.lock_now)
        self.lock_btn.grid(row=len(self.nav_buttons), column=0, sticky="ew", pady=(16, 2))

        folder = ctk.CTkFrame(side, fg_color=C["card"], corner_radius=12,
                              border_width=1, border_color=C["line"])
        folder.grid(row=6, column=0, padx=16, pady=18, sticky="ew")
        folder.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(folder, text="BACKUP FOLDER", font=font(10, True), text_color=C["dim"]
                     ).grid(row=0, column=0, columnspan=2, padx=14, pady=(12, 3), sticky="w")
        self.folder_label = ctk.CTkLabel(folder, text="", wraplength=185, justify="left",
                                         anchor="w", text_color=C["muted"], font=font(11))
        self.folder_label.grid(row=1, column=0, columnspan=2, padx=14, sticky="w")
        ctk.CTkButton(folder, text="Change…", height=32, fg_color=C["line"],
                      hover_color=C["hover"], font=font(12), command=self.on_change_folder
                      ).grid(row=2, column=0, padx=(14, 4), pady=14, sticky="ew")
        ctk.CTkButton(folder, text="Open", height=32, fg_color=C["line"],
                      hover_color=C["hover"], font=font(12),
                      command=lambda: open_folder(self.backup_dir)
                      ).grid(row=2, column=1, padx=(4, 14), pady=14, sticky="ew")
        self._update_folder_label()

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self.shell, fg_color=C["panel"], corner_radius=10, height=38,
                           border_width=1, border_color=C["line"])
        bar.grid(row=1, column=1, sticky="ew", padx=(26, 22), pady=(0, 20))
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="", text_color=C["muted"], anchor="w", font=font(11))
        self.status.grid(row=0, column=0, padx=16, pady=9, sticky="ew")
        self.progress = ctk.CTkProgressBar(bar, mode="indeterminate", width=150, height=6,
                                           progress_color=C["accent"], fg_color=C["line"])
        self.progress.grid(row=0, column=1, padx=16)
        self.progress.grid_remove()

    # ---------------------------------------------------------------- pages
    def _title(self, parent, title: str, subtitle: str):
        ctk.CTkLabel(parent, text=title, font=font(26, True), text_color=C["text"]
                     ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(parent, text=subtitle, text_color=C["muted"], wraplength=640,
                     justify="left", anchor="w", font=font(12)
                     ).grid(row=1, column=0, sticky="w", pady=(2, 16))

    def _build_backups_page(self):
        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self._title(head, "Backups", "Pick a backup, then restore it. The encryption password "
                                     "is asked when you restore.")
        ctk.CTkButton(head, text="Refresh", width=96, height=34, fg_color=C["card"],
                      hover_color=C["hover"], border_width=1, border_color=C["line"],
                      font=font(12, True), command=self.refresh_list
                      ).grid(row=0, column=1, rowspan=2, sticky="ne")

        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        bar.grid_columnconfigure(1, weight=1)
        self.show_all = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="Show backups of all browsers", variable=self.show_all,
                        text_color=C["muted"], font=font(12), checkbox_width=18,
                        checkbox_height=18, corner_radius=5, border_width=2,
                        fg_color=C["green"], hover_color=C["green_h"], command=self.refresh_list
                        ).grid(row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(bar, text="", text_color=C["dim"], anchor="e",
                                        font=font(12))
        self.count_label.grid(row=0, column=1, sticky="e")

        box = ctk.CTkFrame(page, fg_color=C["panel"], corner_radius=14,
                           border_width=1, border_color=C["line"])
        box.grid(row=2, column=0, sticky="nsew")
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)
        self.list_frame = ctk.CTkScrollableFrame(box, fg_color="transparent", corner_radius=14)
        self.list_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.empty_label = ctk.CTkLabel(box, text="", text_color=C["dim"], justify="center",
                                        font=font(14))

        self.detail = ctk.CTkLabel(page, text="No backup selected.", text_color=C["dim"],
                                   anchor="w", justify="left", wraplength=760, font=font(11))
        self.detail.grid(row=3, column=0, sticky="ew", pady=(12, 8))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew")
        actions.grid_columnconfigure(0, weight=2)
        actions.grid_columnconfigure((1, 2), weight=1)
        self.restore_btn = self._button(actions, "Restore selected backup", self.on_restore,
                                        C["green"], C["green_h"], height=46, size=13)
        self.restore_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.remove_btn = self._button(actions, "Delete backup file", self.on_remove_backup,
                                       C["red"], C["red_h"], height=46)
        self.remove_btn.grid(row=0, column=1, padx=6, sticky="ew")
        self.browse_btn = self._button(actions, "Restore from file…", self.on_browse_restore,
                                       C["line"], C["hover"], height=46)
        self.browse_btn.grid(row=0, column=2, padx=(6, 0), sticky="ew")
        return page

    def _button(self, parent, text, command, color, hover, height=44, size=12):
        return AppButton(parent, text=text, command=command, height=height, corner_radius=10,
                         enabled_colors=(color, hover), font=font(size, True),
                         text_color=C["text"], text_color_disabled=C["dim"])

    def _set_enabled(self, btn: AppButton, enabled: bool):
        color, hover = btn.enabled_colors
        if enabled:
            btn.configure(state="normal", fg_color=color, hover_color=hover)
        else:
            btn.configure(state="disabled", fg_color=C["card"], hover_color=C["card"])

    def _refresh_buttons(self):
        can_use = not self.busy
        has_sel = can_use and self.selected is not None
        self._set_enabled(self.restore_btn, has_sel)
        self._set_enabled(self.remove_btn, has_sel)
        self._set_enabled(self.browse_btn, can_use)
        self._set_enabled(self.create_btn, can_use)
        self._set_enabled(self.reset_btn, can_use)

    def _card_page(self, title: str, subtitle: str):
        """A scrollable form page. The scroll area sits inside a plain frame so the page can be
        shown/hidden reliably (raising a scrollable frame only raises its inner part)."""
        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self._title(scroll, title, subtitle)
        card = ctk.CTkFrame(scroll, fg_color=C["panel"], corner_radius=14,
                            border_width=1, border_color=C["line"])
        card.grid(row=2, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        page.bind("<Configure>", lambda ev, c=card: c.grid_configure(
            padx=(0, max(0, ev.width - CARD_MAX_WIDTH - 30))), add=True)
        return page, card

    def _field_label(self, card, row: int, text: str):
        ctk.CTkLabel(card, text=text, font=font(10, True), text_color=C["dim"]
                     ).grid(row=row, column=0, padx=24, pady=(18, 5), sticky="w")

    def _entry(self, card, row: int, placeholder: str, masked: bool = False, pady=(0, 0)):
        entry = ctk.CTkEntry(card, height=42, show="•" if masked else "", font=font(13),
                             placeholder_text=placeholder, fg_color=C["bg"],
                             border_color=C["line"])
        entry.grid(row=row, column=0, padx=24, pady=pady, sticky="ew")
        return entry

    def _build_create_page(self):
        page, card = self._card_page(
            "Create backup",
            "Saves the selected browser's whole profile (all profiles, cookies, logins, "
            "extensions, bookmarks, history, site data) into an encrypted file in your backup "
            "folder.")
        self._field_label(card, 0, "BACKUP NAME (OPTIONAL)")
        self.name_entry = self._entry(card, 1, "e.g. Before reinstall")
        self._field_label(card, 2, "ENCRYPTION PASSWORD (12+ CHARACTERS)")
        self.password = self._entry(card, 3, "Choose a strong password", masked=True)
        self.confirm = self._entry(card, 4, "Confirm password", masked=True, pady=(8, 0))
        self.show_pw = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Show password", variable=self.show_pw, text_color=C["muted"],
                        font=font(12), checkbox_width=18, checkbox_height=18, corner_radius=5,
                        border_width=2, fg_color=C["green"], hover_color=C["green_h"],
                        command=self._toggle_pw
                        ).grid(row=5, column=0, padx=24, pady=(12, 0), sticky="w")
        self.wipe_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(card, text="Reset the browser to fresh after the backup is verified",
                        variable=self.wipe_var, text_color=C["text"], font=font(12),
                        checkbox_width=18, checkbox_height=18, corner_radius=5, border_width=2,
                        fg_color=C["green"], hover_color=C["green_h"]
                        ).grid(row=6, column=0, padx=24, pady=(14, 0), sticky="w")
        self.create_btn = self._button(card, "Create backup", self.on_backup, C["green"],
                                       C["green_h"], height=48, size=13)
        self.create_btn.grid(row=7, column=0, padx=24, pady=(22, 8), sticky="ew")
        self.create_hint = ctk.CTkLabel(card, text="", text_color=C["dim"], anchor="w",
                                        justify="left", wraplength=640, font=font(11))
        self.create_hint.grid(row=8, column=0, padx=24, pady=(0, 20), sticky="w")
        return page

    def _build_reset_page(self):
        page, card = self._card_page(
            "Reset browser",
            "Deletes everything of the selected browser so it starts like a fresh install.")
        ctk.CTkLabel(card, text="Danger zone", text_color=C["red_text"], font=font(15, True)
                     ).grid(row=0, column=0, padx=24, pady=(20, 6), sticky="w")
        ctk.CTkLabel(card, text="All profiles, bookmarks, history, cookies, saved passwords, "
                                "extensions and settings are permanently deleted. No backup "
                                "is made by this button. To keep your data, use Create backup "
                                "instead.",
                     text_color=C["muted"], wraplength=640, justify="left", anchor="w",
                     font=font(12)).grid(row=1, column=0, padx=24, sticky="w")
        self.reset_btn = self._button(card, "Delete ALL data of selected browser", self.on_delete,
                                      C["red"], C["red_h"], height=48, size=13)
        self.reset_btn.grid(row=2, column=0, padx=24, pady=24, sticky="ew")
        return page

    def _build_security_page(self):
        page, card = self._card_page(
            "Security",
            "Optional app lock. When it is on, the app asks for this password every time it "
            "starts. It is separate from the encryption password of your backups and is stored "
            "only as a salted hash.")
        self.sec_status = ctk.CTkLabel(card, text="", font=font(15, True), text_color=C["text"])
        self.sec_status.grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        # ---- shown while the lock is ON
        on = self.sec_on = ctk.CTkFrame(card, fg_color="transparent")
        on.grid(row=1, column=0, sticky="ew")
        on.grid_columnconfigure(0, weight=1)
        self._field_label(on, 0, "CURRENT APP PASSWORD")
        self.app_old = self._entry(on, 1, "Current password", masked=True)
        self._field_label(on, 2, f"NEW APP PASSWORD ({applock.MIN_LENGTH}+ CHARACTERS)")
        self.app_new = self._entry(on, 3, "New password", masked=True)
        self.app_confirm = self._entry(on, 4, "Confirm new password", masked=True, pady=(8, 0))
        self.change_pw_btn = self._button(on, "Change app password", self.on_change_app_password,
                                          C["green"], C["green_h"], height=46, size=13)
        self.change_pw_btn.grid(row=5, column=0, padx=24, pady=(20, 8), sticky="ew")
        self._button(on, "Lock the app now", self.lock_now, C["line"], C["hover"], height=46
                     ).grid(row=6, column=0, padx=24, pady=(0, 8), sticky="ew")
        self._button(on, "Turn off app lock (needs the current password)", self.on_disable_lock,
                     C["red"], C["red_h"], height=46
                     ).grid(row=7, column=0, padx=24, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(on, text=f"The app also locks itself after {AUTO_LOCK_SECONDS // 60} minutes "
                              "without use. Forgot the app password? It cannot be recovered. "
                              "Deleting lock.json "
                              f"in {applock.lock_path().parent} resets it. This lock only guards "
                              "the app: your backups stay protected by their own passwords.",
                     text_color=C["dim"], anchor="w", justify="left", wraplength=640,
                     font=font(11)).grid(row=8, column=0, padx=24, pady=(4, 20), sticky="w")

        # ---- shown while the lock is OFF
        off = self.sec_off = ctk.CTkFrame(card, fg_color="transparent")
        off.grid(row=1, column=0, sticky="ew")
        off.grid_columnconfigure(0, weight=1)
        self._field_label(off, 0, f"NEW APP PASSWORD ({applock.MIN_LENGTH}+ CHARACTERS)")
        self.lock_new = self._entry(off, 1, "Choose a password", masked=True)
        self.lock_confirm = self._entry(off, 2, "Confirm password", masked=True, pady=(8, 0))
        self._button(off, "Turn on app lock", self.on_enable_lock, C["green"], C["green_h"],
                     height=46, size=13).grid(row=3, column=0, padx=24, pady=(20, 8), sticky="ew")
        ctk.CTkLabel(off, text="While the lock is off, anyone who can open this app on this PC "
                               "can use it. Your backups stay protected by their own encryption "
                               "passwords either way.",
                     text_color=C["dim"], anchor="w", justify="left", wraplength=640,
                     font=font(11)).grid(row=4, column=0, padx=24, pady=(4, 20), sticky="w")
        return page

    def _refresh_security(self):
        """Show the controls that match the lock state (ON / OFF)."""
        locked_on = applock.is_set()
        if locked_on:
            self.sec_status.configure(text="App lock: ON", text_color=C["accent"])
            self.sec_off.grid_remove()
            self.sec_on.grid()
            self.lock_btn.grid()
        else:
            self.sec_status.configure(text="App lock: OFF", text_color=C["muted"])
            self.sec_on.grid_remove()
            self.sec_off.grid()
            self.lock_btn.grid_remove()

    def on_change_app_password(self):
        old, new, again = self.app_old.get(), self.app_new.get(), self.app_confirm.get()
        if new != again:
            dialogs.showwarning("Passwords do not match", "Enter the same new password twice.")
            return
        try:
            changed, message = applock.change_password(old, new)
        except (applock.LockFileError, OSError) as exc:
            dialogs.showerror("Could not change password", str(exc))
            return
        if not changed:
            dialogs.showwarning("Not changed", message)
            return
        for entry in (self.app_old, self.app_new, self.app_confirm):
            entry.delete(0, "end")
        dialogs.showinfo("Password changed", message)

    def on_enable_lock(self):
        new, again = self.lock_new.get(), self.lock_confirm.get()
        problem = applock.check_new_password(new)
        if problem:
            dialogs.showwarning("Password too short", problem)
            return
        if new != again:
            dialogs.showwarning("Passwords do not match", "Enter the same password twice.")
            return
        try:
            applock.enable(new)
        except (ValueError, OSError) as exc:
            dialogs.showerror("Could not turn on the lock", str(exc))
            return
        for entry in (self.lock_new, self.lock_confirm):
            entry.delete(0, "end")
        self._refresh_security()
        dialogs.showinfo("App lock on", "The app will ask for this password the next "
                                        "time it starts.")

    def on_disable_lock(self):
        old = self.app_old.get()
        if not old:
            dialogs.showwarning("Password needed",
                                "Enter your current app password above to turn the lock off.")
            return
        if not dialogs.askyesno(
                "Turn off app lock?",
                "Anyone who can open this app on this PC will be able to use it without a "
                "password. Your backups stay protected by their own encryption passwords.\n\n"
                "Turn the lock off?", icon="warning"):
            return
        try:
            done, message = applock.disable(old)
        except (applock.LockFileError, OSError) as exc:
            dialogs.showerror("Could not turn off the lock", str(exc))
            return
        if not done:
            dialogs.showwarning("Not changed", message)
            return
        for entry in (self.app_old, self.app_new, self.app_confirm):
            entry.delete(0, "end")
        self._refresh_security()
        dialogs.showinfo("App lock off", message)

    # ================================================================== helpers
    @staticmethod
    def _label(b: BrowserSpec) -> str:
        return b.name if b.installed else f"{b.name} (not found)"

    @property
    def spec(self) -> BrowserSpec:
        return self.labels[self.browser_var.get()]

    def show_page(self, key: str):
        """Show exactly one page. Pages are gridded/un-gridded, which works for every widget."""
        self.current_page = key
        for name, page in self.pages.items():
            if name == key:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()
        for name, btn in self.nav_buttons.items():
            active = name == key
            btn.configure(fg_color=C["sel"] if active else "transparent",
                          text_color=C["accent"] if active else C["muted"])

    def _toggle_pw(self):
        show = "" if self.show_pw.get() else "•"
        self.password.configure(show=show)
        self.confirm.configure(show=show)

    def _update_folder_label(self):
        self.folder_label.configure(text=str(self.backup_dir))

    def _on_browser_change(self, _value=None):
        if not self.ready:  # still building the window
            return
        if not self.busy:
            self.status.configure(text=f"{self.spec.name} data folder: {self.spec.root}")
        self.refresh_list()
        hint = f"Saved to {self.backup_dir}. Keep your password safe: it cannot be recovered."
        cloud = config.cloud_sync_name(self.backup_dir)
        if cloud:
            hint += (f"\nThis folder is synced by {cloud}: the encrypted file and its small "
                     "name/date file will be uploaded. Choose another folder to keep it local.")
        self.create_hint.configure(text=hint)

    def _set_busy(self, busy: bool, message: str):
        self.busy = busy
        self.status.configure(text=message)
        if busy:
            self.progress.grid()
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()
        self._refresh_buttons()

    def _progress(self, message: str):
        self._post(lambda: self.status.configure(text=message))

    def _error(self, message: str):
        self._set_busy(False, "Operation failed.")
        dialogs.showerror("Operation failed", message)

    def _ensure_closed(self, spec: BrowserSpec) -> bool:
        if not is_running(spec):
            return True
        extra = ("\n\nOpera and Opera GX share one program, so both will close."
                 if spec.key.startswith("opera") else "")
        if dialogs.askyesno(f"{spec.name} is running",
                            f"{spec.name} must be closed first. Close it now?\n\n"
                            f"(Unsaved tabs/forms will be lost.){extra}"):
            if close_browser(spec):
                return True
            dialogs.showerror("Could not close browser",
                              f"Please close {spec.name} manually and try again.")
        return False

    def _require_installed(self, spec: BrowserSpec) -> bool:
        if spec.installed:
            return True
        dialogs.showerror("Browser not found", f"{spec.name} data folder not found:\n{spec.root}")
        return False

    # ================================================================== backup folder + list
    def on_change_folder(self):
        chosen = dialogs.askdirectory(title="Choose backup folder", initialdir=str(self.backup_dir))
        if not chosen:
            return
        owner = browser_containing(Path(chosen))
        if owner:
            dialogs.showerror("Folder not allowed",
                              f"That folder is inside the {owner.name} data folder. A reset "
                              "would delete your backups too.\n\nChoose a different folder.")
            return
        try:
            config.set_backup_dir(Path(chosen))
        except OSError as exc:
            dialogs.showerror("Folder not usable", str(exc))
            return
        self.backup_dir = Path(chosen)
        self._update_folder_label()
        self._on_browser_change()

    def refresh_list(self):
        for frame in self.rows.values():
            frame.destroy()
        self.rows.clear()

        entries = library.list_backups(self.backup_dir)
        if not self.show_all.get():
            key = self.spec.key
            entries = [e for e in entries if e.browser_key in (key, None)]
        self.count_label.configure(text=f"{len(entries)} backup{'s' if len(entries) != 1 else ''}")

        for entry in entries:
            self._make_row(entry)
        if entries:
            self.empty_label.place_forget()
        else:
            what = "any browser" if self.show_all.get() else self.spec.name
            self.empty_label.configure(
                text=f"No backups for {what} yet\nUse “Create backup” to make your first one.")
            self.empty_label.place(relx=0.5, rely=0.45, anchor="center")

        keep = next((e for e in entries if self.selected and e.path == self.selected.path), None)
        self.select(keep)

    def _make_row(self, e: library.BackupEntry):
        row = ctk.CTkFrame(self.list_frame, fg_color=C["card"], corner_radius=10,
                           border_width=1, border_color=C["line"])
        row.pack(fill="x", padx=6, pady=4)
        row.grid_columnconfigure(1, weight=1)
        browser = get(e.browser_key).name if e.browser_key else "Unknown"
        ctk.CTkLabel(row, text=browser, width=104, height=26, corner_radius=8,
                     fg_color=C["line"], text_color=C["accent"], font=font(11, True)
                     ).grid(row=0, column=0, rowspan=2, padx=(14, 12), pady=12)
        ctk.CTkLabel(row, text=e.label or e.path.stem, anchor="w", text_color=C["text"],
                     font=font(13, True)).grid(row=0, column=1, sticky="ew", pady=(11, 0))
        when = e.created.astimezone().strftime("%d %b %Y, %H:%M")
        info = f"{when}   •   {fmt_size(e.size)}"
        if e.files is not None:
            info += f"   •   {e.files:,} files"
        ctk.CTkLabel(row, text=info, anchor="w", text_color=C["muted"], font=font(11)
                     ).grid(row=1, column=1, sticky="ew", pady=(0, 11))
        for widget in (row, *row.winfo_children()):
            widget.bind("<Button-1>", lambda _ev, ent=e: self._on_row_click(ent))
        self.rows[e.path] = row

    def _on_row_click(self, entry: library.BackupEntry):
        if not self.busy:
            self.select(entry)

    def select(self, entry: library.BackupEntry | None):
        self.selected = entry
        for path, frame in self.rows.items():
            chosen = entry is not None and path == entry.path
            frame.configure(fg_color=C["sel"] if chosen else C["card"],
                            border_color=C["accent"] if chosen else C["line"])
        self.detail.configure(text="No backup selected." if entry is None
                              else f"Selected: {entry.path}")
        self._refresh_buttons()

    def on_remove_backup(self):
        entry = self.selected
        if not entry or self.busy:
            return
        if not dialogs.askyesno(
                "Delete backup file?",
                f"Permanently delete this backup file?\n\n{entry.path.name}\n\n"
                "You will not be able to restore from it afterwards.", icon="warning"):
            return
        try:
            library.delete_backup(entry)
        except OSError as exc:
            dialogs.showerror("Could not delete", str(exc))
            return
        self.selected = None
        self.refresh_list()

    # ================================================================== create
    def on_backup(self):
        if self.busy:
            return
        spec = self.spec
        if not self._require_installed(spec):
            return
        p1, p2 = self.password.get(), self.confirm.get()
        problem = check_backup_password(p1)
        if problem:
            dialogs.showwarning("Choose a stronger password", problem)
            return
        if p1 != p2:
            dialogs.showwarning("Passwords do not match", "Enter the same password in both fields.")
            return
        owner = browser_containing(self.backup_dir)
        if owner:
            dialogs.showerror("Backup folder not allowed",
                              f"The backup folder is inside the {owner.name} data folder, so a "
                              "reset would delete the backups too.\n\nUse Change... in the "
                              "sidebar to pick another folder.")
            return
        wipe = self.wipe_var.get()
        if wipe and not dialogs.askyesno(
                f"Reset {spec.name} after backup?",
                f"After the backup is created and verified, ALL {spec.name} data on this PC "
                "(every profile, bookmarks, history, cookies, saved passwords, extensions) "
                f"will be deleted.\n\nThe backup will be saved in:\n{self.backup_dir}\n\nContinue?",
                icon="warning"):
            return
        if not self._ensure_closed(spec):
            return
        target = library.new_backup_path(self.backup_dir, spec)
        label = self.name_entry.get().strip()
        self._set_busy(True, f"Creating encrypted {spec.name} backup…")
        threading.Thread(target=self._backup_worker, args=(spec, target, p1, wipe, label),
                         daemon=True).start()

    def _backup_worker(self, spec, target: Path, password: str, wipe: bool, label: str):
        try:
            count, wiped, skipped = create_backup(spec, target, password, wipe, self._progress,
                                                  label)
            self._post(lambda: self._backup_done(target, count, wiped, skipped))
        except Exception as exc:
            self._post(lambda error=str(exc): self._error(error))

    def _backup_done(self, target: Path, count: int, wiped: bool, skipped: list[str]):
        self._set_busy(False, f"Backup created: {target.name}")
        self.password.delete(0, "end")
        self.confirm.delete(0, "end")
        self.name_entry.delete(0, "end")
        self.selected = None
        self.refresh_list()
        entry = next((e for e in library.list_backups(self.backup_dir) if e.path == target), None)
        if entry:
            self.select(entry)
        self.show_page("backups")
        extra = "\n\nThe browser has been reset to a fresh state." if wiped else ""
        if skipped:
            shown = "\n".join(skipped[:8]) + (f"\n… and {len(skipped) - 8} more"
                                              if len(skipped) > 8 else "")
            dialogs.showwarning(
                "Backup incomplete",
                f"{len(skipped)} file(s) could not be read and are NOT in this backup:\n\n{shown}"
                "\n\nMake sure the browser is fully closed and run the backup again before you "
                "rely on it.")
        dialogs.showinfo("Backup complete",
                         f"Encrypted and verified backup created.\n\nFiles: {count}\n"
                         f"Saved to: {target}{extra}")

    # ================================================================== restore
    def on_restore(self):
        if self.selected and not self.busy:
            self._start_restore(self.selected)

    def on_browse_restore(self):
        if self.busy:
            return
        source = dialogs.askopenfilename(title="Choose encrypted backup",
                                         initialdir=str(self.backup_dir), filetypes=FILETYPES)
        if not source:
            return
        try:
            entry = library.entry_for(Path(source))
        except (ValueError, OSError) as exc:
            dialogs.showerror("Not a backup file", str(exc))
            return
        self._start_restore(entry)

    def _start_restore(self, entry: library.BackupEntry):
        spec = get(entry.browser_key) if entry.browser_key else self.spec
        name = entry.label or entry.path.name
        if not dialogs.askyesno(
                "Confirm restore",
                f"Restore “{name}” into {spec.name}?\n\n"
                f"This replaces ALL current {spec.name} data with the backup.", icon="warning"):
            return
        keep_old = dialogs.askyesno(
            "Keep a safety copy?",
            f"Keep your current {spec.name} data in a folder next to it until you have checked "
            "the restored browser?\n\nYes = keep it (uses disk space and is NOT encrypted; "
            "delete it yourself later).\nNo = delete it right after a successful restore.")
        dlg = TextDialog(self, "Encryption password required",
                         "Enter the encryption password for this backup:", "Restore",
                         placeholder="Encryption password")
        self.wait_window(dlg)
        if not dlg.result:
            return
        if not self._ensure_closed(spec):
            return
        self._set_busy(True, "Decrypting and restoring…")
        threading.Thread(target=self._restore_worker,
                         args=(spec, entry.path, dlg.result, keep_old), daemon=True).start()

    def _restore_worker(self, spec, source: Path, password: str, keep_old: bool):
        try:
            count, warning, kept = restore_backup(spec, source, password, self._progress,
                                                  keep_old)
            self._post(lambda: self._restore_done(spec, count, warning, kept))
        except Exception as exc:
            self._post(lambda error=str(exc): self._error(error))

    def _restore_done(self, spec: BrowserSpec, count: int, warning: str | None,
                      kept: Path | None):
        self._set_busy(False, f"{spec.name} restored.")
        msg = f"Restored {count:,} files into {spec.name}. You can open it now."
        if kept:
            msg += (f"\n\nYour previous data is still here (not encrypted):\n{kept}\n"
                    "Delete that folder once you have checked the restored browser.")
        if warning:
            msg += "\n\n" + warning
        dialogs.showinfo("Restore complete", msg)

    # ================================================================== delete all browser data
    def on_delete(self):
        if self.busy:
            return
        spec = self.spec
        if not self._require_installed(spec):
            return
        if not dialogs.askyesno(
                f"Delete all {spec.name} data?",
                f"This PERMANENTLY deletes everything in {spec.name}: all profiles, bookmarks, "
                "history, cookies, saved passwords, extensions and settings. It will be like a "
                "fresh install.\n\nNo backup is made by this button.\n\nContinue?", icon="warning"):
            return
        dlg = TextDialog(self, "Final confirmation",
                         f"Type  {spec.name}  to confirm deleting all its data:",
                         "Delete everything", placeholder=spec.name, masked=False,
                         color=C["red"], hover=C["red_h"])
        self.wait_window(dlg)
        if dlg.result is None:
            return
        if dlg.result.strip().lower() != spec.name.lower():
            dialogs.showinfo("Cancelled", "The name did not match. Nothing was deleted.")
            return
        if not self._ensure_closed(spec):
            return
        self._set_busy(True, f"Deleting all {spec.name} data…")
        threading.Thread(target=self._delete_worker, args=(spec,), daemon=True).start()

    def _delete_worker(self, spec: BrowserSpec):
        try:
            wipe_browser(spec, self._progress)
            self._post(lambda: self._delete_done(spec))
        except Exception as exc:
            self._post(lambda error=str(exc): self._error(error))

    def _delete_done(self, spec: BrowserSpec):
        self._set_busy(False, f"{spec.name} reset.")
        dialogs.showinfo("Done", f"All {spec.name} data was deleted. It will start fresh.")


if __name__ == "__main__":
    if sys.platform == "win32":
        # Give Windows a stable app identity for taskbar grouping and icon selection.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "BrowserBackupManager"
            )
        except (AttributeError, OSError):
            pass
    App().mainloop()
