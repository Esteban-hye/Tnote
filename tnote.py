#!/usr/bin/env python3
"""TNote v4 — Bloc-notes sticky minimaliste avec Firebase."""

import tkinter as tk
from tkinter import messagebox
import json, os, time, threading, ctypes, ctypes.wintypes
import uuid as uuid_mod
import urllib.request
import urllib.error
import pystray
from PIL import Image, ImageDraw

VERSION = "4.1.0"

# ─── Firebase par defaut (ton projet) ────────────────────────────────────────
FIREBASE_API_KEY = ""
FIREBASE_DB_URL  = ""

BRISTOL = [
    "#FFFFFF", "#F7E66A", "#89C4E1", "#98D9A4", "#F4A7B9", "#FFBE76",
    "#B39DDB", "#80CBC4", "#FFCC80", "#EF9A9A", "#A5D6A7",
]

FONT_SIZES = (8, 9, 10, 11, 12, 14, 16, 18, 20)
DEFAULT_SIZE = 10

WINDOW_W, WINDOW_H = 300, 410
STACK_OFFSET = 4

BASE_DIR  = os.path.join(os.path.expanduser("~"), ".tnote4")
DATA_FILE = os.path.join(BASE_DIR, "notes.json")
CFG_FILE  = os.path.join(BASE_DIR, "config.json")

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_CTRL, MOD_ALT, MOD_SHIFT = 0x0002, 0x0001, 0x0004
HOTKEY_ID = 1


def darken(h: str, f: float = 0.82) -> str:
    r = int(int(h[1:3], 16) * f)
    g = int(int(h[3:5], 16) * f)
    b = int(int(h[5:7], 16) * f)
    return f"#{r:02x}{g:02x}{b:02x}"


def empty_note() -> dict:
    return {
        "id": uuid_mod.uuid4().hex,
        "title": "",
        "segments": [],
        "color": None,
        "updated_at": time.time(),
        "deleted": False,
    }


def migrate(note) -> dict:
    if isinstance(note, str):
        note = {"title": "", "segments": [{"text": note, "tags": []}]}
    if "text" in note and "segments" not in note:
        text = note.pop("text", "")
        note["segments"] = [{"text": text, "tags": []}] if text else []
    elif "text" in note and "segments" in note:
        note.pop("text", None)
    note.setdefault("id", uuid_mod.uuid4().hex)
    note.setdefault("title", "")
    note.setdefault("segments", [])
    note.setdefault("color", None)
    note.setdefault("updated_at", time.time())
    note.setdefault("deleted", False)
    return note


# ─────────────────────────────────────────────────────────────────────────────
class TNote:
    def __init__(self):
        os.makedirs(BASE_DIR, exist_ok=True)

        self.notes: list[dict] = [empty_note()]
        self.current_index: int = 0
        self.visible: bool = False
        self._drag_offset = (0, 0)
        self._hotkey_thread_id: int = 0

        self.hotkey_vk = ord("N")
        self.hotkey_mods = MOD_CTRL | MOD_ALT
        self.hotkey_label = "Ctrl+Alt+N"
        self.win_w = WINDOW_W
        self.win_h = WINDOW_H
        self.win_x = -1
        self.win_y = -1

        self._save_job = None
        self._dirty = False
        self._font_cache: set = set()

        self._load_config()
        self._load_notes()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)

        self._build_ui()
        self._set_position()
        self._start_hotkey_thread()
        self._setup_tray()
        self._autosave_loop()
        self._fb_sync_background()

    # ─── Donnees ──────────────────────────────────────────────────────────────

    def _load_notes(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.notes = [migrate(n) for n in d.get("notes", [empty_note()])]
                self.current_index = min(d.get("current_index", 0), len(self.notes) - 1)
                return
            except Exception:
                pass
        self.notes = [empty_note()]
        self.current_index = 0

    # ── Font tag system (compound tags) ───────────────────────────────────────

    def _make_font_tag(self, size: int, bold: bool, italic: bool) -> str:
        """Cree/retourne un tag Tkinter pour cette combinaison taille+style."""
        name = f"f_{size}{'_b' if bold else ''}{'_i' if italic else ''}"
        if name not in self._font_cache:
            parts = []
            if bold:   parts.append("bold")
            if italic: parts.append("italic")
            style = " ".join(parts)
            if style:
                self.text_area.tag_configure(name, font=("Segoe UI", size, style))
            else:
                self.text_area.tag_configure(name, font=("Segoe UI", size))
            self._font_cache.add(name)
        return name

    def _get_format_at(self, index: str) -> tuple:
        """Retourne (size, bold, italic) au point donne."""
        size, bold, italic = DEFAULT_SIZE, False, False
        for t in self.text_area.tag_names(index):
            if t.startswith("f_"):
                parts = t.split("_")
                try:
                    size = int(parts[1])
                except (IndexError, ValueError):
                    pass
                bold = "_b" in t
                italic = "_i" in t
        return size, bold, italic

    def _logical_to_visual(self, tags: list) -> list:
        """Convertit les tags logiques en tags visuels Tkinter."""
        bold = "bold" in tags
        italic = "italic" in tags
        underline = "underline" in tags
        size = DEFAULT_SIZE
        for t in tags:
            if t.startswith("size_"):
                try:
                    size = int(t.split("_")[1])
                except (IndexError, ValueError):
                    pass

        visual = []
        if bold or italic or size != DEFAULT_SIZE:
            visual.append(self._make_font_tag(size, bold, italic))
        if underline:
            visual.append("underline")
        return visual

    def _visual_to_logical(self, tag: str) -> list:
        """Convertit un tag visuel f_* en tags logiques."""
        tags = []
        if tag.startswith("f_"):
            parts = tag.split("_")
            try:
                sz = int(parts[1])
                if sz != DEFAULT_SIZE:
                    tags.append(f"size_{sz}")
            except (IndexError, ValueError):
                pass
            if "_b" in tag:
                tags.append("bold")
            if "_i" in tag:
                tags.append("italic")
        return tags

    # ── Capture / Load ────────────────────────────────────────────────────────

    def _capture_segments(self) -> list:
        """Serialise le Text widget en segments avec tags logiques."""
        segments = []
        active_tags = set()
        cur_text = ""

        items = list(self.text_area.dump("1.0", "end-1c", tag=True, text=True))

        def flush():
            nonlocal cur_text
            if cur_text:
                logical = set()
                for t in active_tags:
                    if t == "underline":
                        logical.add("underline")
                    elif t.startswith("f_"):
                        logical.update(self._visual_to_logical(t))
                segments.append({"text": cur_text, "tags": sorted(logical)})
                cur_text = ""

        for key, value, pos in items:
            if key == "tagon" and (value.startswith("f_") or value == "underline"):
                flush()
                active_tags.add(value)
            elif key == "tagoff" and value in active_tags:
                flush()
                active_tags.discard(value)
            elif key == "text":
                cur_text += value
        flush()
        return segments

    def _capture_current(self):
        if not hasattr(self, "text_area"):
            return
        note = self.notes[self.current_index]
        note["title"] = self._get_real_title()
        note["segments"] = self._capture_segments()
        note["updated_at"] = time.time()

    def _save_notes(self):
        self._capture_current()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"notes": self.notes, "current_index": self.current_index},
                f, ensure_ascii=False, indent=2,
            )
        self._dirty = False

    def _schedule_save(self):
        self._dirty = True
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, self._save_notes)

    def _load_into_widget(self):
        note = self.notes[self.current_index]

        self.title_entry.config(fg="#111")
        self.title_entry.delete(0, "end")
        real_title = note.get("title", "")
        if real_title:
            self.title_entry.insert(0, real_title)
        else:
            self._show_placeholder()

        self.text_area.delete("1.0", "end")
        self.text_area.tag_configure("underline", underline=True)
        for seg in note.get("segments", []):
            text = seg.get("text", "")
            tags = seg.get("tags", [])
            visual = self._logical_to_visual(tags)
            if visual:
                self.text_area.insert("end", text, *visual)
            else:
                self.text_area.insert("end", text)

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        self.fb_api_key = ""
        self.fb_db_url = ""
        self.fb_id_token = ""
        self.fb_refresh_token = ""
        self.fb_uid = ""
        self.fb_email = ""
        if os.path.exists(CFG_FILE):
            try:
                with open(CFG_FILE, "r") as f:
                    c = json.load(f)
                self.hotkey_vk = c.get("vk", ord("N"))
                self.hotkey_mods = c.get("mods", MOD_CTRL | MOD_ALT)
                self.hotkey_label = c.get("label", "Ctrl+Alt+N")
                self.win_w = c.get("win_w", WINDOW_W)
                self.win_h = c.get("win_h", WINDOW_H)
                self.win_x = c.get("win_x", -1)
                self.win_y = c.get("win_y", -1)
                self.fb_api_key = c.get("fb_api_key", "")
                self.fb_db_url = c.get("fb_db_url", "")
                self.fb_id_token = c.get("fb_id_token", "")
                self.fb_refresh_token = c.get("fb_refresh_token", "")
                self.fb_uid = c.get("fb_uid", "")
                self.fb_email = c.get("fb_email", "")
            except Exception:
                pass

    def _save_config(self):
        with open(CFG_FILE, "w") as f:
            json.dump({
                "vk": self.hotkey_vk, "mods": self.hotkey_mods,
                "label": self.hotkey_label,
                "win_w": self.win_w, "win_h": self.win_h,
                "win_x": self.win_x, "win_y": self.win_y,
                "fb_api_key": self.fb_api_key,
                "fb_db_url": self.fb_db_url,
                "fb_id_token": self.fb_id_token,
                "fb_refresh_token": self.fb_refresh_token,
                "fb_uid": self.fb_uid,
                "fb_email": self.fb_email,
            }, f)

    # ─── Interface ────────────────────────────────────────────────────────────

    def _note_color(self) -> str:
        c = self.notes[self.current_index].get("color")
        return c if c else BRISTOL[self.current_index % len(BRISTOL)]

    def _stack_color(self, offset: int) -> str:
        return BRISTOL[(self.current_index + offset) % len(BRISTOL)]

    def _note_label(self) -> str:
        return f"  {self.current_index + 1}/{len(self.notes)}"

    def _build_ui(self):
        c = self._note_color()
        dark = darken(c)

        self.stack2 = tk.Frame(self.root, bg=darken(self._stack_color(2), 0.88))
        self.stack2.place(x=STACK_OFFSET * 2, y=STACK_OFFSET * 2,
                          width=self.win_w, height=self.win_h)
        self.stack1 = tk.Frame(self.root, bg=darken(self._stack_color(1), 0.92))
        self.stack1.place(x=STACK_OFFSET, y=STACK_OFFSET,
                          width=self.win_w, height=self.win_h)

        self.card = tk.Frame(self.root, bg=c)
        self.card.place(x=0, y=0, width=self.win_w, height=self.win_h)

        # Header
        self.header = tk.Frame(self.card, bg=dark, height=32)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        hbtn = dict(bg=dark, fg="#1a1a1a", bd=0, relief="flat",
                    font=("Segoe UI", 11), cursor="hand2",
                    activebackground=darken(dark, 0.88), activeforeground="#000")

        self.lbl_index = tk.Label(
            self.header, text=self._note_label(),
            bg=dark, fg="#1a1a1a", font=("Segoe UI", 8, "bold"),
        )
        self.lbl_index.pack(side="left", padx=(8, 2))

        self.btn_del = tk.Button(self.header, text="\u00d7",
                                 command=self._delete_note, **hbtn)
        self.btn_del.pack(side="right", padx=(0, 6))
        self.btn_add = tk.Button(self.header, text="+",
                                 command=self._add_note, **hbtn)
        self.btn_add.pack(side="right", padx=2)

        # Pastille couleur → ouvre le picker
        self.color_dot = tk.Canvas(
            self.header, width=16, height=16,
            bg=dark, highlightthickness=0, cursor="hand2",
        )
        self.color_dot.create_oval(2, 2, 14, 14,
                                   fill=self._note_color(), outline="", tags="dot")
        self.color_dot.pack(side="right", padx=6)
        self.color_dot.bind("<Button-1>", self._open_color_picker)

        # Titre
        self.title_var = tk.StringVar()
        self.title_entry = tk.Entry(
            self.card, textvariable=self.title_var,
            bg=darken(c, 0.91), fg="#111", bd=0,
            font=("Segoe UI", 10, "bold"),
            insertbackground="#333", relief="flat",
        )
        self.title_entry.pack(fill="x", padx=10, pady=(7, 0), ipady=5)
        self._setup_title_placeholder("Titre (optionnel)")

        self.sep = tk.Frame(self.card, bg=darken(c, 0.83), height=1)
        self.sep.pack(fill="x", padx=10, pady=(5, 0))

        # Zone de texte
        self.text_area = tk.Text(
            self.card, bg=c, fg="#1a1a1a",
            font=("Segoe UI", DEFAULT_SIZE), bd=0,
            padx=12, pady=8, wrap="word",
            insertbackground="#333",
            selectbackground="#4a90d9",
            selectforeground="#ffffff",
            relief="flat", undo=True,
            spacing1=3, spacing2=2,
        )
        self.text_area.pack(fill="both", expand=True)
        self.text_area.tag_configure("underline", underline=True)
        self._load_into_widget()

        # Toolbar flottante
        self._fmt_visible = False
        self._color_picker_open = False
        self._build_fmt_toolbar()

        # Footer
        self.footer = tk.Frame(self.card, bg=dark, height=6)
        self.footer.pack(fill="x", side="bottom")

        # Bindings
        for w in (self.header, self.lbl_index):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.root.bind_all("<Control-MouseWheel>", self._on_scroll)
        self.text_area.bind("<KeyRelease>", self._on_key_release)
        self.title_entry.bind("<KeyRelease>", lambda e: self._schedule_save())
        self.text_area.bind("<ButtonRelease-1>", self._check_selection)
        self.text_area.bind("<Control-b>", lambda e: (self._toggle_tag("bold"), "break"))
        self.text_area.bind("<Control-i>", lambda e: (self._toggle_tag("italic"), "break"))
        self.text_area.bind("<Control-u>", lambda e: (self._toggle_tag("underline"), "break"))

        self._setup_resize_edges(c, dark)

    # ── Placeholder titre ─────────────────────────────────────────────────────

    def _setup_title_placeholder(self, ph: str):
        self._ph_text = ph
        self._ph_fg = "#888"
        self._normal_fg = "#111"
        self.title_entry.bind("<FocusIn>", self._hide_placeholder)
        self.title_entry.bind("<FocusOut>", lambda e: self._show_placeholder())
        self._show_placeholder()

    def _show_placeholder(self):
        if not self.title_entry.get() or self.title_entry.get() == self._ph_text:
            self.title_entry.config(fg=self._ph_fg)
            self.title_entry.delete(0, "end")
            self.title_entry.insert(0, self._ph_text)

    def _hide_placeholder(self, e=None):
        if self.title_entry.cget("fg") == self._ph_fg:
            self.title_entry.config(fg=self._normal_fg)
            self.title_entry.delete(0, "end")

    def _get_real_title(self) -> str:
        txt = self.title_entry.get()
        if txt == self._ph_text and self.title_entry.cget("fg") == self._ph_fg:
            return ""
        return txt

    # ── Formatage texte ───────────────────────────────────────────────────────

    def _build_fmt_toolbar(self):
        self.fmt_bar = tk.Frame(
            self.card, bg="#2c2c2c",
            highlightbackground="#444", highlightthickness=1,
        )
        btn_style = dict(
            bg="#2c2c2c", fg="#ffffff", bd=0, relief="flat",
            cursor="hand2", width=3, pady=2,
            activebackground="#505050", activeforeground="#ffffff",
        )

        # Taille -
        tk.Button(
            self.fmt_bar, text="-", font=("Segoe UI", 10, "bold"),
            command=lambda: self._change_size(-1), **btn_style,
        ).pack(side="left", padx=1, pady=2)

        self.size_lbl = tk.Label(
            self.fmt_bar, text=str(DEFAULT_SIZE), bg="#2c2c2c", fg="#aaaaaa",
            font=("Segoe UI", 8), width=2,
        )
        self.size_lbl.pack(side="left", padx=0, pady=2)

        # Taille +
        tk.Button(
            self.fmt_bar, text="+", font=("Segoe UI", 10, "bold"),
            command=lambda: self._change_size(1), **btn_style,
        ).pack(side="left", padx=1, pady=2)

        tk.Frame(self.fmt_bar, bg="#555", width=1).pack(
            side="left", fill="y", pady=5, padx=3,
        )

        for tag, label, font_style in [
            ("bold",      "B",  ("Segoe UI", 10, "bold")),
            ("italic",    "I",  ("Segoe UI", 10, "italic")),
            ("underline", "U",  ("Segoe UI", 10, "underline")),
        ]:
            btn = tk.Button(
                self.fmt_bar, text=label, font=font_style,
                command=lambda t=tag: self._toggle_tag(t),
                **btn_style,
            )
            btn.pack(side="left", padx=1, pady=2)

    def _toggle_tag(self, tag: str):
        try:
            s = self.text_area.index("sel.first")
            e = self.text_area.index("sel.last")
        except tk.TclError:
            return

        if tag == "underline":
            ranges = self.text_area.tag_ranges("underline")
            covered = any(
                self.text_area.compare(ranges[i], "<=", s)
                and self.text_area.compare(ranges[i + 1], ">=", e)
                for i in range(0, len(ranges), 2)
            )
            if covered:
                self.text_area.tag_remove("underline", s, e)
            else:
                self.text_area.tag_add("underline", s, e)
            self._schedule_save()
            return

        # Bold / Italic : compound font tag
        size, bold, italic = self._get_format_at(s)
        if tag == "bold":
            bold = not bold
        elif tag == "italic":
            italic = not italic

        # Retirer tous les anciens tags font du range
        for t in list(self.text_area.tag_names()):
            if t.startswith("f_"):
                self.text_area.tag_remove(t, s, e)

        # Appliquer le nouveau tag compose
        if bold or italic or size != DEFAULT_SIZE:
            new_tag = self._make_font_tag(size, bold, italic)
            self.text_area.tag_add(new_tag, s, e)

        self._schedule_save()

    def _change_size(self, direction: int):
        try:
            s = self.text_area.index("sel.first")
            e = self.text_area.index("sel.last")
        except tk.TclError:
            return

        size, bold, italic = self._get_format_at(s)
        idx = FONT_SIZES.index(size) if size in FONT_SIZES else FONT_SIZES.index(DEFAULT_SIZE)
        new_idx = max(0, min(len(FONT_SIZES) - 1, idx + direction))
        new_size = FONT_SIZES[new_idx]

        for t in list(self.text_area.tag_names()):
            if t.startswith("f_"):
                self.text_area.tag_remove(t, s, e)

        if bold or italic or new_size != DEFAULT_SIZE:
            new_tag = self._make_font_tag(new_size, bold, italic)
            self.text_area.tag_add(new_tag, s, e)

        self.size_lbl.config(text=str(new_size))
        self._schedule_save()

    def _on_key_release(self, event):
        self._schedule_save()
        self._check_selection(event)

    def _check_selection(self, event=None):
        try:
            sel_start = self.text_area.index("sel.first")
            has_sel = True
        except tk.TclError:
            has_sel = False

        if has_sel:
            self.root.update_idletasks()
            bbox = self.text_area.bbox(sel_start)
            if bbox:
                bx, by, _, bh = bbox
                tx = self.text_area.winfo_x()
                ty = self.text_area.winfo_y()
                BAR_W, BAR_H = 175, 30
                px = max(4, min(tx + bx - BAR_W // 2, self.win_w - BAR_W - 4))
                py = max(0, ty + by - BAR_H - 6)
                self.fmt_bar.place(in_=self.card, x=px, y=py,
                                   width=BAR_W, height=BAR_H)
                self.fmt_bar.lift()
                size, _, _ = self._get_format_at(sel_start)
                self.size_lbl.config(text=str(size))
                self._fmt_visible = True
        elif self._fmt_visible:
            self.fmt_bar.place_forget()
            self._fmt_visible = False

    # ── Color picker (roue de couleurs) ───────────────────────────────────────

    def _open_color_picker(self, event=None):
        if self._color_picker_open:
            return
        self._color_picker_open = True

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)

        # Position a cote de la pastille
        dot_x = self.color_dot.winfo_rootx()
        dot_y = self.color_dot.winfo_rooty() + 20
        popup.geometry(f"+{dot_x}+{dot_y}")

        frame = tk.Frame(popup, bg="#2c2c2c", padx=6, pady=6,
                         highlightbackground="#444", highlightthickness=1)
        frame.pack()

        # Grille de couleurs (ronds)
        DOT_SIZE = 22
        cols = 4
        for i, color in enumerate(BRISTOL):
            row, col = divmod(i, cols)
            cv = tk.Canvas(frame, width=DOT_SIZE, height=DOT_SIZE,
                           bg="#2c2c2c", highlightthickness=0, cursor="hand2")
            outline = "#666" if color != "#FFFFFF" else "#999"
            cv.create_oval(3, 3, DOT_SIZE - 3, DOT_SIZE - 3,
                           fill=color, outline=outline, width=1, tags="dot")
            cv.grid(row=row, column=col, padx=2, pady=2)

            def on_click(e, c=color):
                self.notes[self.current_index]["color"] = c
                self._refresh_ui()
                self._save_notes()
                popup.destroy()
                self._color_picker_open = False

            cv.bind("<Button-1>", on_click)

        def on_close(e=None):
            popup.destroy()
            self._color_picker_open = False

        popup.bind("<FocusOut>", on_close)
        popup.focus_set()

    # ── Fenetre (position sauvegardee) ────────────────────────────────────────

    def _set_position(self):
        total_w = self.win_w + STACK_OFFSET * 2
        total_h = self.win_h + STACK_OFFSET * 2
        if self.win_x >= 0 and self.win_y >= 0:
            x, y = self.win_x, self.win_y
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - total_w - 20
            y = sh - total_h - 55
        self.root.geometry(f"{total_w}x{total_h}+{x}+{y}")

    def _save_position(self):
        self.win_x = self.root.winfo_x()
        self.win_y = self.root.winfo_y()
        self._save_config()

    def _setup_resize_edges(self, c: str, dark: str):
        E, C = 5, 10
        defs = [
            ("n",  dark, "size_ns",    0.0, 0.0, "nw", dict(relwidth=1.0, height=E)),
            ("s",  dark, "size_ns",    0.0, 1.0, "sw", dict(relwidth=1.0, height=E)),
            ("e",  c,    "size_we",    1.0, 0.0, "ne", dict(width=E, relheight=1.0)),
            ("w",  c,    "size_we",    0.0, 0.0, "nw", dict(width=E, relheight=1.0)),
            ("nw", dark, "size_nw_se", 0.0, 0.0, "nw", dict(width=C, height=C)),
            ("ne", dark, "size_ne_sw", 1.0, 0.0, "ne", dict(width=C, height=C)),
            ("sw", dark, "size_ne_sw", 0.0, 1.0, "sw", dict(width=C, height=C)),
            ("se", dark, "size_nw_se", 1.0, 1.0, "se", dict(width=C, height=C)),
        ]
        self._rsz_edges = {}
        for name, bg, cur, rx, ry, anchor, kw in defs:
            f = tk.Frame(self.card, bg=bg, cursor=cur)
            f.place(relx=rx, rely=ry, anchor=anchor, **kw)
            f.lift()
            f.bind("<Button-1>", lambda e, n=name: self._rsz_start(e, n))
            f.bind("<B1-Motion>", lambda e, n=name: self._rsz_drag(e, n))
            f.bind("<ButtonRelease-1>", self._rsz_end)
            self._rsz_edges[name] = f

    def _rsz_start(self, event, zone: str):
        self._rsz_zone = zone
        self._rsz_x0 = event.x_root
        self._rsz_y0 = event.y_root
        self._rsz_wx = self.root.winfo_x()
        self._rsz_wy = self.root.winfo_y()
        self._rsz_w0 = self.win_w
        self._rsz_h0 = self.win_h

    def _rsz_drag(self, event, zone: str):
        dx = event.x_root - self._rsz_x0
        dy = event.y_root - self._rsz_y0
        new_x, new_y = self._rsz_wx, self._rsz_wy
        new_w, new_h = self._rsz_w0, self._rsz_h0

        if "e" in zone: new_w = max(220, self._rsz_w0 + dx)
        if "s" in zone: new_h = max(180, self._rsz_h0 + dy)
        if "w" in zone:
            new_w = max(220, self._rsz_w0 - dx)
            new_x = self._rsz_wx + (self._rsz_w0 - new_w)
        if "n" in zone:
            new_h = max(180, self._rsz_h0 - dy)
            new_y = self._rsz_wy + (self._rsz_h0 - new_h)

        self.win_w, self.win_h = new_w, new_h
        self.root.geometry(
            f"{new_w + STACK_OFFSET * 2}x{new_h + STACK_OFFSET * 2}+{new_x}+{new_y}"
        )
        self.card.place(width=new_w, height=new_h)
        self.stack1.place(width=new_w, height=new_h)
        self.stack2.place(width=new_w, height=new_h)

    def _rsz_end(self, event):
        self._save_position()

    def _drag_start(self, e):
        self._drag_offset = (
            e.x_root - self.root.winfo_x(),
            e.y_root - self.root.winfo_y(),
        )

    def _drag_move(self, e):
        self.root.geometry(
            f"+{e.x_root - self._drag_offset[0]}"
            f"+{e.y_root - self._drag_offset[1]}"
        )

    def _drag_end(self, e):
        self._save_position()

    def toggle(self):
        if self.visible:
            self._save_position()
            self.root.withdraw()
            self.visible = False
        else:
            self._set_position()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.text_area.focus_set()
            self.visible = True

    # ── Notes ─────────────────────────────────────────────────────────────────

    def _add_note(self):
        self._capture_current()
        self._save_notes()
        self.notes.append(empty_note())
        self.current_index = len(self.notes) - 1
        self._refresh_ui()

    def _delete_note(self):
        ok = messagebox.askyesno(
            "Supprimer la note",
            "Es-tu sur de vouloir supprimer cette note ?",
            parent=self.root,
        )
        if not ok:
            return
        if len(self.notes) == 1:
            self.notes[0] = empty_note()
            self._refresh_ui()
            self._save_notes()
            return
        self.notes.pop(self.current_index)
        self.current_index = min(self.current_index, len(self.notes) - 1)
        self._refresh_ui()
        self._save_notes()

    def _on_scroll(self, event):
        self._capture_current()
        direction = -1 if event.delta > 0 else 1
        self.current_index = (self.current_index + direction) % len(self.notes)
        self._refresh_ui()
        self._save_notes()
        return "break"

    def _refresh_ui(self):
        c = self._note_color()
        dark = darken(c)

        self.stack2.config(bg=darken(self._stack_color(2), 0.88))
        self.stack1.config(bg=darken(self._stack_color(1), 0.92))
        self.card.config(bg=c)
        self.header.config(bg=dark)
        self.lbl_index.config(bg=dark, text=self._note_label())
        self.title_entry.config(bg=darken(c, 0.91))
        self.sep.config(bg=darken(c, 0.83))
        self.text_area.config(bg=c)
        self.footer.config(bg=dark)

        for btn in (self.btn_add, self.btn_del):
            btn.config(bg=dark, activebackground=darken(dark, 0.88))

        self.color_dot.config(bg=dark)
        self.color_dot.itemconfig("dot", fill=c)

        for name, f in self._rsz_edges.items():
            f.config(bg=dark if name in ("n", "s", "nw", "ne", "sw", "se") else c)
            f.lift()

        self._fmt_visible = False
        self.fmt_bar.place_forget()
        self._load_into_widget()
        self.text_area.focus_set()

    # ── Raccourci clavier ─────────────────────────────────────────────────────

    def _start_hotkey_thread(self):
        def run():
            self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            u = ctypes.windll.user32
            u.RegisterHotKey(None, HOTKEY_ID, self.hotkey_mods, self.hotkey_vk)
            msg = ctypes.wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY:
                    self.root.after(0, self.toggle)
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            u.UnregisterHotKey(None, HOTKEY_ID)

        threading.Thread(target=run, daemon=True).start()

    def _apply_hotkey(self, vk: int, mods: int, label: str):
        if self._hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._hotkey_thread_id, WM_QUIT, 0, 0
            )
        self.hotkey_vk = vk
        self.hotkey_mods = mods
        self.hotkey_label = label
        self._save_config()
        self._start_hotkey_thread()

    # ── Firebase ──────────────────────────────────────────────────────────────

    def _fb_connected(self) -> bool:
        return bool(self.fb_api_key and self.fb_db_url and self.fb_id_token)

    def _fb_request(self, url: str, data: dict | None = None, method: str = "POST") -> dict:
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _fb_refresh_id_token(self):
        if not self.fb_refresh_token or not self.fb_api_key:
            return False
        try:
            url = f"https://securetoken.googleapis.com/v1/token?key={self.fb_api_key}"
            result = self._fb_request(url, {
                "grant_type": "refresh_token",
                "refresh_token": self.fb_refresh_token,
            })
            self.fb_id_token = result["id_token"]
            self.fb_refresh_token = result["refresh_token"]
            self._save_config()
            return True
        except Exception:
            return False

    def _fb_auth(self, email: str, password: str, register: bool) -> str | None:
        endpoint = "signUp" if register else "signInWithPassword"
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:{endpoint}?key={self.fb_api_key}"
        try:
            result = self._fb_request(url, {
                "email": email, "password": password,
                "returnSecureToken": True,
            })
            self.fb_id_token = result["idToken"]
            self.fb_refresh_token = result["refreshToken"]
            self.fb_uid = result["localId"]
            self.fb_email = email
            self._save_config()
            return None
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
                code = detail.get("error", {}).get("message", str(e))
                msgs = {
                    "EMAIL_EXISTS": "Cet email est deja utilise",
                    "INVALID_EMAIL": "Email invalide",
                    "WEAK_PASSWORD": "Mot de passe trop faible (6 chars min)",
                    "EMAIL_NOT_FOUND": "Email non trouve",
                    "INVALID_PASSWORD": "Mot de passe incorrect",
                    "INVALID_LOGIN_CREDENTIALS": "Email ou mot de passe incorrect",
                }
                return msgs.get(code, code)
            except Exception:
                return str(e)
        except Exception as e:
            return str(e)

    def _fb_sync_now(self):
        if not self._fb_connected():
            return
        self._capture_current()
        db_url = self.fb_db_url.rstrip("/")
        notes_url = f"{db_url}/users/{self.fb_uid}/notes.json?auth={self.fb_id_token}"

        try:
            req = urllib.request.Request(notes_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                server_data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                if self._fb_refresh_id_token():
                    self._fb_sync_now()
                return
            return
        except Exception:
            return

        server_notes = {}
        if server_data and isinstance(server_data, dict):
            server_notes = server_data

        local_map = {n["id"]: n for n in self.notes}
        merged = {}
        for nid in set(local_map.keys()) | set(server_notes.keys()):
            local = local_map.get(nid)
            remote = server_notes.get(nid)
            if local and remote:
                merged[nid] = local if local.get("updated_at", 0) >= remote.get("updated_at", 0) else remote
            else:
                merged[nid] = local or remote

        try:
            put_url = f"{db_url}/users/{self.fb_uid}/notes.json?auth={self.fb_id_token}"
            req = urllib.request.Request(put_url, method="PUT",
                                         data=json.dumps(merged, ensure_ascii=False).encode())
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            return

        active = [n for n in merged.values() if not n.get("deleted", False)]
        if not active:
            active = [empty_note()]
        self.notes = sorted(active, key=lambda n: n.get("updated_at", 0))
        self.current_index = min(self.current_index, len(self.notes) - 1)
        self._save_notes()
        if hasattr(self, "text_area"):
            self.root.after(0, self._refresh_ui)

    def _fb_sync_background(self):
        threading.Thread(target=self._fb_sync_now, daemon=True).start()

    # ── Parametres ────────────────────────────────────────────────────────────

    def _open_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("TNote — Parametres")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()

        section_font = ("Segoe UI", 10, "bold")
        label_font = ("Segoe UI", 9)
        small_font = ("Segoe UI", 8)

        container = tk.Frame(dlg)
        container.pack(fill="both", expand=True, padx=20, pady=15)

        # ── Cloud ─────────────────────────────────────────────────────────────
        tk.Label(container, text="Cloud", font=section_font).pack(anchor="w")
        tk.Frame(container, bg="#ccc", height=1).pack(fill="x", pady=(2, 8))

        connected = self._fb_connected()
        status_txt = f"Connecte : {self.fb_email}" if connected else "Non connecte"
        status_clr = "#2a9d2a" if connected else "#999"
        status_lbl = tk.Label(container, text=status_txt, font=small_font, fg=status_clr)
        status_lbl.pack(anchor="w")

        msg_lbl = tk.Label(container, text="", font=small_font, fg="#cc3333")
        msg_lbl.pack(anchor="w", pady=(2, 0))

        if connected:
            bf_cloud = tk.Frame(container)
            bf_cloud.pack(anchor="w", pady=(6, 0))

            def do_sync():
                msg_lbl.config(text="Sync en cours...", fg="#2a6099")
                self._fb_sync_background()
                dlg.after(2000, lambda: msg_lbl.config(text="Sync terminee", fg="#2a9d2a"))

            tk.Button(bf_cloud, text="Synchroniser maintenant", command=do_sync,
                      font=label_font, width=22).pack(side="left", padx=(0, 6))

            def do_disconnect():
                self.fb_id_token = ""
                self.fb_refresh_token = ""
                self.fb_uid = ""
                self.fb_email = ""
                self._save_config()
                msg_lbl.config(text="Deconnecte.", fg="#888")
                status_lbl.config(text="Non connecte", fg="#999")

            tk.Button(bf_cloud, text="Deconnecter", command=do_disconnect,
                      font=label_font, fg="#cc3333").pack(side="left")
        else:
            tk.Label(container, text="Email :", font=label_font).pack(anchor="w", pady=(6, 2))
            email_var = tk.StringVar()
            tk.Entry(container, textvariable=email_var, font=label_font, width=40).pack(fill="x")

            tk.Label(container, text="Mot de passe :", font=label_font).pack(anchor="w", pady=(6, 2))
            pw_var = tk.StringVar()
            tk.Entry(container, textvariable=pw_var, show="*", font=label_font, width=40).pack(fill="x")

            has_defaults = bool(FIREBASE_API_KEY and FIREBASE_DB_URL)
            adv_frame = tk.Frame(container)
            apikey_var = tk.StringVar(value=self.fb_api_key or FIREBASE_API_KEY)
            dburl_var = tk.StringVar(value=self.fb_db_url or FIREBASE_DB_URL)

            def build_advanced():
                tk.Label(adv_frame, text="API Key :", font=small_font).pack(anchor="w", pady=(4, 1))
                tk.Entry(adv_frame, textvariable=apikey_var, font=small_font, width=45).pack(fill="x")
                tk.Label(adv_frame, text="Database URL :", font=small_font).pack(anchor="w", pady=(4, 1))
                tk.Entry(adv_frame, textvariable=dburl_var, font=small_font, width=45).pack(fill="x")

            if has_defaults:
                def show_advanced():
                    adv_btn.pack_forget()
                    adv_frame.pack(fill="x", pady=(4, 0))
                    build_advanced()
                    dlg.update_idletasks()
                    _resize_dlg()

                adv_btn = tk.Label(container, text="Parametres avances...",
                                   font=small_font, fg="#2a6099", cursor="hand2")
                adv_btn.pack(anchor="w", pady=(6, 0))
                adv_btn.bind("<Button-1>", lambda e: show_advanced())
            else:
                adv_frame.pack(fill="x", pady=(4, 0))
                build_advanced()

            bf_auth = tk.Frame(container)
            bf_auth.pack(anchor="w", pady=(10, 0))

            def do_auth(register: bool):
                api_key = apikey_var.get().strip()
                db_url = dburl_var.get().strip()
                email = email_var.get().strip()
                pw = pw_var.get().strip()
                if not email or not pw:
                    msg_lbl.config(text="Email et mot de passe requis", fg="#cc3333")
                    return
                if not api_key or not db_url:
                    msg_lbl.config(text="API Key et Database URL requis", fg="#cc3333")
                    return
                self.fb_api_key = api_key
                self.fb_db_url = db_url
                self._save_config()
                msg_lbl.config(text="Connexion...", fg="#2a6099")
                dlg.update()
                err = self._fb_auth(email, pw, register)
                if err:
                    msg_lbl.config(text=err, fg="#cc3333")
                else:
                    action = "Compte cree" if register else "Connecte"
                    status_lbl.config(text=f"Connecte : {email}", fg="#2a9d2a")
                    msg_lbl.config(text=f"{action} ! Sync en cours...", fg="#2a9d2a")
                    self._fb_sync_background()

            tk.Button(bf_auth, text="Se connecter", command=lambda: do_auth(False),
                      font=("Segoe UI", 9, "bold"), width=14,
                      bg="#2a6099", fg="white").pack(side="left", padx=(0, 6))
            tk.Button(bf_auth, text="Creer un compte", command=lambda: do_auth(True),
                      font=label_font, width=14).pack(side="left")

        # ── Raccourci ─────────────────────────────────────────────────────────
        tk.Label(container, text="").pack()
        tk.Label(container, text="Raccourci", font=section_font).pack(anchor="w")
        tk.Frame(container, bg="#ccc", height=1).pack(fill="x", pady=(2, 8))

        hk_frame = tk.Frame(container)
        hk_frame.pack(anchor="w")

        tk.Label(hk_frame, text="Afficher/Masquer :", font=label_font).pack(side="left")
        hk_lbl = tk.Label(hk_frame, text=self.hotkey_label,
                          font=("Segoe UI", 10, "bold"), fg="#2a6099",
                          relief="groove", padx=12, pady=4)
        hk_lbl.pack(side="left", padx=(8, 0))

        captured = {"vk": self.hotkey_vk, "mods": self.hotkey_mods, "label": self.hotkey_label}
        IGNORE = {"Control_L", "Control_R", "Shift_L", "Shift_R",
                  "Alt_L", "Alt_R", "Super_L", "Super_R"}
        recording = {"active": False}

        def start_record():
            recording["active"] = True
            hk_lbl.config(text="...", fg="#cc3333")
            rec_btn.config(text="Appuie sur une touche", state="disabled")

        def on_key(ev):
            if not recording["active"]:
                return
            if ev.keysym in IGNORE:
                return
            mods, parts = 0, []
            if ev.state & 0x4:     mods |= MOD_CTRL;  parts.append("Ctrl")
            if ev.state & 0x1:     mods |= MOD_SHIFT; parts.append("Shift")
            if ev.state & 0x20000: mods |= MOD_ALT;   parts.append("Alt")
            parts.append(ev.keysym.upper() if len(ev.keysym) == 1 else ev.keysym)
            label = "+".join(parts)
            captured.update({"vk": ev.keycode, "mods": mods, "label": label})
            hk_lbl.config(text=label, fg="#2a6099")
            recording["active"] = False
            rec_btn.config(text="Modifier", state="normal")
            self._apply_hotkey(captured["vk"], captured["mods"], captured["label"])

        dlg.bind("<KeyPress>", on_key)
        rec_btn = tk.Button(hk_frame, text="Modifier", command=start_record,
                            font=small_font, cursor="hand2")
        rec_btn.pack(side="left", padx=(8, 0))

        # ── A propos ──────────────────────────────────────────────────────────
        tk.Label(container, text="").pack()
        tk.Label(container, text="A propos", font=section_font).pack(anchor="w")
        tk.Frame(container, bg="#ccc", height=1).pack(fill="x", pady=(2, 6))
        tk.Label(container, text=f"TNote v{VERSION}", font=small_font, fg="#888").pack(anchor="w")

        def _resize_dlg():
            dlg.update_idletasks()
            w = container.winfo_reqwidth() + 40
            h = container.winfo_reqheight() + 30
            x = (dlg.winfo_screenwidth() - w) // 2
            y = (dlg.winfo_screenheight() - h) // 2
            dlg.geometry(f"{w}x{h}+{x}+{y}")

        _resize_dlg()

    # ── Systray ───────────────────────────────────────────────────────────────

    def _make_tray_icon(self) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([12, 16, 56, 58], fill="#98D9A4", outline="#6ab07a", width=1)
        d.rectangle([8, 12, 52, 54], fill="#89C4E1", outline="#5a9cbe", width=1)
        d.rectangle([4, 8, 48, 50], fill="#F7E66A", outline="#c4b240", width=1)
        d.line([11, 19, 41, 19], fill="#777", width=2)
        d.line([11, 26, 41, 26], fill="#777", width=2)
        d.line([11, 33, 28, 33], fill="#777", width=2)
        return img

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Afficher / Masquer",
                             lambda: self.root.after(0, self.toggle), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Parametres",
                             lambda: self.root.after(0, self._open_settings)),
            pystray.MenuItem("Synchroniser",
                             lambda: self.root.after(0, self._fb_sync_background)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter TNote v4",
                             lambda: self.root.after(0, self._quit)),
        )
        self.tray = pystray.Icon("TNote4", self._make_tray_icon(), "TNote v4", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _autosave_loop(self):
        if self._dirty:
            self._save_notes()
        self._fb_sync_background()
        self.root.after(30_000, self._autosave_loop)

    def _quit(self):
        self._save_position()
        self._save_notes()
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TNote()
    app.run()
