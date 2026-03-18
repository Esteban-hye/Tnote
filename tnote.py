#!/usr/bin/env python3
"""TNote v2 — Formatage riche, couleur par note, recherche, poignée bas-gauche."""

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
import json, os, re, sys, subprocess, threading, ctypes, ctypes.wintypes
import uuid as uuid_mod
import pystray
from PIL import Image, ImageDraw, ImageGrab, ImageTk

VERSION = "2.0.0"

BRISTOL = [
    "#F7E66A", "#89C4E1", "#98D9A4", "#F4A7B9", "#FFBE76",
    "#B39DDB", "#80CBC4", "#FFCC80", "#EF9A9A", "#A5D6A7",
]

TRANSPARENT_BG = "#010101"
WINDOW_W, WINDOW_H = 300, 410
MARGIN_RIGHT, MARGIN_BOTTOM = 20, 55
STACK_OFFSET = 4

BASE_DIR  = os.path.join(os.path.expanduser("~"), ".tnote2")
DATA_FILE = os.path.join(BASE_DIR, "notes.json")
CFG_FILE  = os.path.join(BASE_DIR, "config.json")
IMG_DIR   = os.path.join(BASE_DIR, "images")

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_CTRL, MOD_ALT, MOD_SHIFT = 0x0002, 0x0001, 0x0004
HOTKEY_ID = 1

IMG_RE   = re.compile(r"\[\[IMG:([a-f0-9]+)\]\]")
RICH_TAGS = ("bold", "italic", "underline", "overstrike")


def darken(h: str, f: float = 0.82) -> str:
    r = int(int(h[1:3], 16) * f)
    g = int(int(h[3:5], 16) * f)
    b = int(int(h[5:7], 16) * f)
    return f"#{r:02x}{g:02x}{b:02x}"


def empty_note() -> dict:
    return {"title": "", "segments": [], "images": [], "color": None}


def migrate(note) -> dict:
    """Convertit l'ancien format v1 vers le format v2 (segments)."""
    if isinstance(note, str):
        note = {"title": "", "text": note, "images": []}
    if "text" in note and "segments" not in note:
        text  = note.pop("text", "")
        imgs  = note.get("images", [])
        ids   = {i["id"] for i in imgs}
        parts = IMG_RE.split(text)
        segs  = []
        for i, p in enumerate(parts):
            if i % 2 == 0:
                if p:
                    segs.append({"type": "text", "text": p, "tags": []})
            elif p in ids:
                segs.append({"type": "image", "id": p})
        note["segments"] = segs
    note.setdefault("color",    None)
    note.setdefault("segments", [])
    note.setdefault("images",   [])
    note.setdefault("title",    "")
    return note


# ─── Application principale ───────────────────────────────────────────────────
class TNote:
    def __init__(self):
        os.makedirs(BASE_DIR, exist_ok=True)
        os.makedirs(IMG_DIR,  exist_ok=True)

        self.notes: list[dict]      = [empty_note()]
        self.current_index: int     = 0
        self.visible: bool          = False
        self._drag_offset           = (0, 0)
        self._photo_refs: dict      = {}
        self._hotkey_thread_id: int = 0
        self._configured_tags: set  = set()
        self._drag_img_x0           = None
        self._drag_img_moved        = False

        self.hotkey_vk    = ord("N")
        self.hotkey_mods  = MOD_CTRL | MOD_ALT
        self.hotkey_label = "Ctrl+Alt+N"
        self.win_w        = WINDOW_W
        self.win_h        = WINDOW_H

        self._load_config()
        self._load_notes()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg=TRANSPARENT_BG)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT_BG)
        self.root.geometry(f"{self.win_w + STACK_OFFSET*2}x{self.win_h + STACK_OFFSET*2}")

        self._build_ui()
        self._set_position()
        self._start_hotkey_thread()
        self._setup_tray()
        self._autosave_loop()

    # ─── Données ──────────────────────────────────────────────────────────────

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

    def _capture_segments(self) -> list:
        """Sérialise le Text widget en liste de segments (texte+tags, images)."""
        segments   = []
        active     = set()
        cur_text   = ""

        items = list(self.text_area.dump(
            "1.0", "end-1c", tag=True, text=True, image=True
        ))

        def flush():
            nonlocal cur_text
            if cur_text:
                tags = [t for t in active if t in RICH_TAGS or t.startswith("fg_")]
                segments.append({"type": "text", "text": cur_text, "tags": tags})
                cur_text = ""

        for key, value, pos in items:
            if key == "tagon":
                if value in RICH_TAGS or value.startswith("fg_"):
                    flush(); active.add(value)
            elif key == "tagoff":
                if value in active:
                    flush(); active.discard(value)
            elif key == "text":
                cur_text += value
            elif key == "image":
                flush()
                # Détecter l'alignement de la ligne de l'image
                align = "left"
                for t in self.text_area.tag_names(f"{pos} linestart"):
                    if t in ("img_center", "img_right"):
                        align = t[4:]  # "center" ou "right"
                        break
                segments.append({"type": "image", "id": value, "align": align})

        flush()
        return segments

    def _capture_current(self):
        if not hasattr(self, "text_area"):
            return
        note = self.notes[self.current_index]
        note["title"]    = self._get_real_title()
        note["segments"] = self._capture_segments()
        ids_used = {s["id"] for s in note["segments"] if s.get("type") == "image"}
        note["images"]   = [i for i in note.get("images", []) if i["id"] in ids_used]

    def _save_notes(self):
        self._capture_current()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"notes": self.notes, "current_index": self.current_index},
                f, ensure_ascii=False, indent=2,
            )

    def _load_into_widget(self):
        note      = self.notes[self.current_index]
        real_title = note.get("title", "")

        # Titre + placeholder
        if hasattr(self, "_show_ph"):
            if real_title:
                self.title_entry.config(fg="#111")
                self.title_entry.delete(0, "end")
                self.title_entry.insert(0, real_title)
            else:
                self._show_ph()
        else:
            self.title_var.set(real_title)

        self.text_area.delete("1.0", "end")
        self._setup_text_tags()

        imgs_by_id = {i["id"]: i for i in note.get("images", [])}

        for seg in note.get("segments", []):
            if seg.get("type") == "image":
                img_id = seg["id"]
                if img_id in imgs_by_id:
                    fpath = os.path.join(IMG_DIR, imgs_by_id[img_id]["file"])
                    if os.path.exists(fpath):
                        try:
                            pil   = Image.open(fpath)
                            photo = ImageTk.PhotoImage(pil)
                            self._photo_refs[img_id] = photo
                            self.text_area.image_create("end", image=photo, name=img_id)
                            # Restaurer l'alignement
                            align = seg.get("align", "left")
                            if align != "left":
                                tag = f"img_{align}"
                                self.text_area.tag_configure(tag, justify=align)
                                img_pos = self.text_area.index("end-2c")
                                self.text_area.tag_add(
                                    tag,
                                    f"{img_pos} linestart",
                                    f"{img_pos} lineend +1c",
                                )
                        except Exception:
                            pass
            else:
                text = seg.get("text", "")
                tags = seg.get("tags", [])
                # Configurer les tags de couleur dynamiques
                for t in tags:
                    if t.startswith("fg_") and t not in self._configured_tags:
                        self.text_area.tag_configure(t, foreground=t[3:])
                        self._configured_tags.add(t)
                if tags:
                    self.text_area.insert("end", text, *tags)
                else:
                    self.text_area.insert("end", text)

    def _load_config(self):
        if os.path.exists(CFG_FILE):
            try:
                with open(CFG_FILE, "r") as f:
                    c = json.load(f)
                self.hotkey_vk    = c.get("vk",    ord("N"))
                self.hotkey_mods  = c.get("mods",  MOD_CTRL | MOD_ALT)
                self.hotkey_label = c.get("label", "Ctrl+Alt+N")
                self.win_w        = c.get("win_w", WINDOW_W)
                self.win_h        = c.get("win_h", WINDOW_H)
            except Exception:
                pass

    def _save_config(self):
        with open(CFG_FILE, "w") as f:
            json.dump(
                {"vk": self.hotkey_vk, "mods": self.hotkey_mods,
                 "label": self.hotkey_label,
                 "win_w": self.win_w, "win_h": self.win_h},
                f,
            )

    # ─── Interface ────────────────────────────────────────────────────────────

    def _note_color(self) -> str:
        c = self.notes[self.current_index].get("color")
        return c if c else BRISTOL[self.current_index % len(BRISTOL)]

    def _stack_color(self, offset: int) -> str:
        return BRISTOL[(self.current_index + offset) % len(BRISTOL)]

    def _note_label(self) -> str:
        return f"  {self.current_index + 1}/{len(self.notes)}"

    def _build_ui(self):
        c    = self._note_color()
        dark = darken(c)

        # Cartes empilées
        self.stack2 = tk.Frame(self.root, bg=darken(self._stack_color(2), 0.88))
        self.stack2.place(x=STACK_OFFSET*2, y=STACK_OFFSET*2,
                          width=self.win_w, height=self.win_h)
        self.stack1 = tk.Frame(self.root, bg=darken(self._stack_color(1), 0.92))
        self.stack1.place(x=STACK_OFFSET, y=STACK_OFFSET,
                          width=self.win_w, height=self.win_h)

        # Carte principale
        self.card = tk.Frame(self.root, bg=c)
        self.card.place(x=0, y=0, width=self.win_w, height=self.win_h)

        # ── Header ──
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

        # Boutons droite → gauche
        self.btn_del = tk.Button(self.header, text="×",
                                 command=self._delete_note, **hbtn)
        self.btn_del.pack(side="right", padx=(0, 6))
        self.btn_add = tk.Button(self.header, text="+",
                                 command=self._add_note, **hbtn)
        self.btn_add.pack(side="right", padx=2)
        # Pastille couleur — clique pour cycler
        self.color_dot = tk.Canvas(
            self.header, width=16, height=16,
            bg=dark, highlightthickness=0, cursor="hand2",
        )
        self.color_dot.create_oval(2, 2, 14, 14,
                                   fill=self._note_color(), outline="", tags="dot")
        self.color_dot.pack(side="right", padx=6)
        self.color_dot.bind("<Button-1>", lambda e: self._cycle_note_color())

        # ── Titre ──
        self.title_var = tk.StringVar(
            value=self.notes[self.current_index].get("title", "")
        )
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

        # ── Barre de formatage (cachée, apparaît à la sélection) ──
        self.fmt_bar = tk.Frame(self.card, bg=c, height=28)
        self.fmt_bar.pack_propagate(False)
        # pas packée maintenant — apparaît seulement lors d'une sélection
        self._fmt_bar_visible = False
        self._build_fmt_toolbar(c)

        # ── Zone de texte ──
        self.text_area = tk.Text(
            self.card, bg=c, fg="#1a1a1a",
            font=("Segoe UI", 10), bd=0,
            padx=12, pady=8, wrap="word",
            insertbackground="#333",
            selectbackground="#aaaaaa",
            relief="flat", undo=True,
            spacing1=3, spacing2=2,
        )
        self.text_area.pack(fill="both", expand=True)
        self._setup_text_tags()
        self._load_into_widget()

        # ── Footer (bande déco bas) ──
        self.footer = tk.Frame(self.card, bg=dark, height=6)
        self.footer.pack(fill="x", side="bottom")

        # ── Bindings ──
        for w in (self.header, self.lbl_index):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.root.bind_all("<Control-MouseWheel>", self._on_scroll)
        self.text_area.bind("<KeyRelease>",    self._on_key_release)
        self.title_entry.bind("<KeyRelease>",  lambda _: self._save_notes())
        self.text_area.bind("<Control-v>",     self._on_paste)
        self.text_area.bind("<Button-3>",      self._on_right_click)
        self.text_area.bind("<Button-1>",        self._on_text_click)
        self.text_area.bind("<B1-Motion>",       self._on_text_drag)
        self.text_area.bind("<ButtonRelease-1>", self._on_text_release)
        self.text_area.bind("<Control-b>",       lambda e: (self._toggle_tag("bold"),      "break"))
        self.text_area.bind("<Control-u>",       lambda e: (self._toggle_tag("underline"), "break"))
        self.text_area.bind("<ButtonRelease>",   self._check_selection)

        # ── Bandes de redimensionnement sur tous les bords ──
        self._setup_resize_edges(c, dark)
        # ── Poignées image ──
        self._setup_img_handles()

    def _build_fmt_toolbar(self, c: str):
        self.fmt_btns = {}
        fbtn = dict(
            bg=c, fg="#1a1a1a", bd=0, relief="flat",
            cursor="hand2", width=2, pady=1,
            activebackground=darken(c, 0.85), activeforeground="#000",
        )
        for tag, label, extra_font in [
            ("bold",      "G", "bold"),
            ("underline", "S", "underline"),
        ]:
            btn = tk.Button(
                self.fmt_bar, text=label,
                font=("Segoe UI", 10, extra_font),
                command=lambda t=tag: self._toggle_tag(t),
                **fbtn,
            )
            btn.pack(side="left", padx=2)
            self.fmt_btns[tag] = btn

        # Séparateur
        tk.Frame(self.fmt_bar, bg=darken(c, 0.75), width=1).pack(
            side="left", fill="y", pady=3, padx=2
        )
        # Bouton liste à cocher
        tk.Button(
            self.fmt_bar, text="☐",
            command=self._insert_checkbox,
            font=("Segoe UI", 10), **fbtn,
        ).pack(side="left", padx=2)

    def _on_key_release(self, event):
        self._save_notes()
        self._check_selection(event)

    def _check_selection(self, event=None):
        """Affiche la mini-barre juste au-dessus du texte sélectionné."""
        try:
            sel_start = self.text_area.index("sel.first")
            has_sel   = True
        except tk.TclError:
            has_sel = False

        if has_sel:
            self.root.update_idletasks()
            bbox = self.text_area.bbox(sel_start)
            if bbox:
                bx, by, _, bh = bbox
                tx = self.text_area.winfo_x()
                ty = self.text_area.winfo_y()
                BAR_W, BAR_H = 90, 26
                px = max(0, min(tx + bx, self.win_w - BAR_W))
                py = max(0, ty + by - BAR_H - 2)
                self.fmt_bar.place(in_=self.card, x=px, y=py,
                                   width=BAR_W, height=BAR_H)
                self.fmt_bar.lift()
                self._fmt_bar_visible = True
        elif self._fmt_bar_visible:
            self.fmt_bar.place_forget()
            self._fmt_bar_visible = False

    def _setup_text_tags(self):
        self.text_area.tag_configure("bold",       font=("Segoe UI", 10, "bold"))
        self.text_area.tag_configure("italic",     font=("Segoe UI", 10, "italic"))
        self.text_area.tag_configure("underline",  underline=True)
        self.text_area.tag_configure("overstrike", overstrike=True)

    # ── Placeholder titre ──────────────────────────────────────────────────────

    def _setup_title_placeholder(self, ph: str):
        ph_fg, normal_fg = "#888", "#111"

        def show(e=None):
            if not self._get_real_title():
                self.title_entry.config(fg=ph_fg)
                self.title_entry.delete(0, "end")
                self.title_entry.insert(0, ph)

        def hide(e=None):
            if (self.title_entry.get() == ph
                    and self.title_entry.cget("fg") == ph_fg):
                self.title_entry.config(fg=normal_fg)
                self.title_entry.delete(0, "end")

        self.title_entry.bind("<FocusIn>",  hide)
        self.title_entry.bind("<FocusOut>", show)
        self._show_ph = show

        _orig = self.title_var.get
        def safe_get():
            v = _orig()
            return "" if (v == ph and self.title_entry.cget("fg") == ph_fg) else v
        self.title_var.get = safe_get
        show()

    def _get_real_title(self) -> str:
        return self.title_var.get()

    # ── Formatage texte ────────────────────────────────────────────────────────

    def _toggle_tag(self, tag: str):
        try:
            s = self.text_area.index("sel.first")
            e = self.text_area.index("sel.last")
        except tk.TclError:
            return
        ranges  = self.text_area.tag_ranges(tag)
        covered = any(
            self.text_area.compare(ranges[i], "<=", s)
            and self.text_area.compare(ranges[i+1], ">=", e)
            for i in range(0, len(ranges), 2)
        )
        if covered:
            self.text_area.tag_remove(tag, s, e)
        else:
            self.text_area.tag_add(tag, s, e)
        self._save_notes()

    def _pick_text_color(self):
        color = colorchooser.askcolor(title="Couleur du texte", parent=self.root)
        if color and color[1]:
            hex_c    = color[1]
            tag_name = f"fg_{hex_c}"
            if tag_name not in self._configured_tags:
                self.text_area.tag_configure(tag_name, foreground=hex_c)
                self._configured_tags.add(tag_name)
            try:
                s = self.text_area.index("sel.first")
                e = self.text_area.index("sel.last")
                self.text_area.tag_add(tag_name, s, e)
                self._save_notes()
            except tk.TclError:
                pass

    # ── Couleur de la note ─────────────────────────────────────────────────────

    def _cycle_note_color(self):
        """Passe à la couleur BRISTOL suivante au clic sur la pastille."""
        note    = self.notes[self.current_index]
        current = note.get("color") or self._note_color()
        try:
            idx = BRISTOL.index(current)
        except ValueError:
            idx = -1
        note["color"] = BRISTOL[(idx + 1) % len(BRISTOL)]
        self._refresh_ui()
        self._save_notes()

    # ── Images ────────────────────────────────────────────────────────────────

    def _on_paste(self, event):
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None
        if isinstance(img, Image.Image):
            self._insert_image(img)
            return "break"

    def _insert_image(self, pil_img: Image.Image):
        max_w = self.win_w - 28
        if pil_img.width > max_w:
            ratio   = max_w / pil_img.width
            pil_img = pil_img.resize(
                (max_w, int(pil_img.height * ratio)), Image.LANCZOS
            )
        img_id = uuid_mod.uuid4().hex[:12]
        fname  = f"{img_id}.png"
        fpath  = os.path.join(IMG_DIR, fname)
        pil_img.save(fpath, "PNG")

        self.notes[self.current_index].setdefault("images", []).append(
            {"id": img_id, "file": fname}
        )
        photo = ImageTk.PhotoImage(pil_img)
        self._photo_refs[img_id] = photo
        self.text_area.image_create(tk.INSERT, image=photo, name=img_id)
        self._save_notes()

    def _insert_image_from_file(self):
        path = filedialog.askopenfilename(
            title="Choisir une image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff"),
                ("Tous",   "*.*"),
            ],
        )
        if path:
            try:
                self._insert_image(Image.open(path))
            except Exception as exc:
                messagebox.showerror("Erreur", f"Impossible d'ouvrir:\n{exc}")

    def _on_right_click(self, event):
        img_id, img_idx = self._find_image_at_pixel(event.x, event.y)
        if not img_id:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="← Gauche",
                         command=lambda: self._align_image(img_id, "left"))
        menu.add_command(label="↔ Centre",
                         command=lambda: self._align_image(img_id, "center"))
        menu.add_command(label="→ Droite",
                         command=lambda: self._align_image(img_id, "right"))
        menu.add_separator()
        menu.add_command(label="Supprimer",
                         command=lambda: self._delete_image(img_id, img_idx))
        menu.tk_popup(event.x_root, event.y_root)

    def _align_image(self, img_id: str, align: str):
        """Aligne l'image (left/center/right) en configurant le tag justify sur sa ligne."""
        # Trouver la position de l'image dans le widget
        for key, name, pos in self.text_area.dump("1.0", "end", image=True):
            if key == "image" and name == img_id:
                line_start = self.text_area.index(f"{pos} linestart")
                line_end   = self.text_area.index(f"{pos} lineend +1c")
                # Retirer les anciens tags d'alignement
                for t in ("img_left", "img_center", "img_right"):
                    self.text_area.tag_remove(t, line_start, line_end)
                # Appliquer le nouveau
                tag = f"img_{align}"
                self.text_area.tag_configure(tag, justify=align)
                self.text_area.tag_add(tag, line_start, line_end)
                self._save_notes()
                break

    def _delete_image(self, img_id: str, idx: str):
        self.text_area.delete(idx, f"{idx}+1c")
        self._save_notes()

    # ── Redimensionnement image à la souris ────────────────────────────────────

    def _setup_img_handles(self):
        """Crée les 4 poignées de coin pour redimensionner les images."""
        self._sel_img_id  = None
        self._sel_img_idx = None
        self._img_handles = {}
        cursors = {
            "nw": "size_nw_se", "ne": "size_ne_sw",
            "sw": "size_ne_sw", "se": "size_nw_se",
        }
        HS = 9  # taille poignée
        for pos, cur in cursors.items():
            h = tk.Frame(self.card, bg="#2a6099", cursor=cur,
                         width=HS, height=HS)
            h.bind("<Button-1>",      lambda e, p=pos: self._img_rsz_start(e, p))
            h.bind("<B1-Motion>",     lambda e, p=pos: self._img_rsz_drag(e, p))
            h.bind("<ButtonRelease-1>", self._img_rsz_end)
            self._img_handles[pos] = h
        # Bordure sélection (4 lignes fines)
        self._img_borders = {
            d: tk.Frame(self.card, bg="#2a6099")
            for d in ("n", "s", "e", "w")
        }

    def _find_image_at_pixel(self, x, y):
        """Cherche une image dont le bounding box contient le pixel (x,y)."""
        try:
            items = self.text_area.dump("1.0", "end", image=True)
        except tk.TclError:
            return None, None
        for key, name, idx in items:
            if key != "image":
                continue
            bbox = self.text_area.bbox(idx)
            if not bbox:
                continue
            bx, by, bw, bh = bbox
            if bx <= x <= bx + bw and by <= y <= by + bh:
                return name, idx
        return None, None

    def _on_text_click(self, event):
        """Gère clic : case à cocher, image (sélection + début drag), ou déselection."""
        idx  = self.text_area.index(f"@{event.x},{event.y}")
        char = self.text_area.get(idx)

        # Toggle case à cocher
        if char in ("☐", "☑"):
            new = "☑" if char == "☐" else "☐"
            self.text_area.delete(idx, f"{idx}+1c")
            self.text_area.insert(idx, new)
            self._save_notes()
            return "break"

        # Image ? — détection par bounding box pixel (plus fiable que @x,y)
        img_id, img_idx = self._find_image_at_pixel(event.x, event.y)

        if img_id:
            self._sel_img_id    = img_id
            self._sel_img_idx   = img_idx
            self._drag_img_x0   = event.x_root
            self._drag_img_y0   = event.y_root
            self._drag_img_moved = False
            self._update_img_handles()
        else:
            self._hide_img_handles()
            self._drag_img_x0 = None

    def _on_text_drag(self, event):
        """Déplace l'image si on la glisse."""
        if not self._sel_img_id or self._drag_img_x0 is None:
            return
        dx = abs(event.x_root - self._drag_img_x0)
        dy = abs(event.y_root - self._drag_img_y0)
        if dx > 4 or dy > 4:
            self._drag_img_moved = True
            self.text_area.config(cursor="fleur")
        if self._drag_img_moved:
            return "break"  # Empêche tkinter de déplacer le curseur texte

    def _on_text_release(self, event):
        """Au relâchement, déplace l'image à la nouvelle position."""
        if self._sel_img_id and self._drag_img_moved:
            drop_idx = self.text_area.index(f"@{event.x},{event.y}")
            self._move_image(self._sel_img_id, self._sel_img_idx, drop_idx)
        self._drag_img_moved = False
        self._drag_img_x0    = None
        self.text_area.config(cursor="")

    def _move_image(self, img_id: str, from_idx: str, to_idx: str):
        photo = self._photo_refs.get(img_id)
        if not photo:
            return
        if self.text_area.compare(from_idx, "==", to_idx):
            return
        # Marquer la destination avant delete (les colonnes bougent après)
        self.text_area.mark_set("_drop", to_idx)
        self.text_area.mark_gravity("_drop", "left")
        self.text_area.delete(from_idx, f"{from_idx}+1c")
        # Le nom est libéré après delete, on peut le réutiliser
        self.text_area.image_create("_drop", image=photo, name=img_id)
        self.text_area.mark_unset("_drop")
        self._hide_img_handles()
        self._save_notes()

    def _insert_checkbox(self):
        """Insère une case à cocher au curseur."""
        self.text_area.insert(tk.INSERT, "☐ ")
        self._save_notes()

    def _update_img_handles(self):
        if not self._sel_img_idx:
            return
        self.root.update_idletasks()
        bbox = self.text_area.bbox(self._sel_img_idx)
        if not bbox:
            return
        bx, by, bw, bh = bbox
        tx = self.text_area.winfo_x()
        ty = self.text_area.winfo_y()
        x, y, w, h = tx + bx, ty + by, bw, bh
        HS = 9

        pos_coords = {
            "nw": (x - HS//2,     y - HS//2),
            "ne": (x + w - HS//2, y - HS//2),
            "sw": (x - HS//2,     y + h - HS//2),
            "se": (x + w - HS//2, y + h - HS//2),
        }
        for pos, (px, py) in pos_coords.items():
            self._img_handles[pos].place(in_=self.card, x=px, y=py,
                                         width=HS, height=HS)
            self._img_handles[pos].lift()

        # Bordure
        self._img_borders["n"].place(in_=self.card, x=x,     y=y,     width=w,  height=1)
        self._img_borders["s"].place(in_=self.card, x=x,     y=y+h-1, width=w,  height=1)
        self._img_borders["w"].place(in_=self.card, x=x,     y=y,     width=1,  height=h)
        self._img_borders["e"].place(in_=self.card, x=x+w-1, y=y,     width=1,  height=h)
        for b in self._img_borders.values():
            b.lift()

    def _hide_img_handles(self):
        self._sel_img_id  = None
        self._sel_img_idx = None
        for h in self._img_handles.values():
            h.place_forget()
        for b in self._img_borders.values():
            b.place_forget()

    def _img_rsz_start(self, event, pos: str):
        if not self._sel_img_id:
            return
        note = self.notes[self.current_index]
        info = next((i for i in note.get("images", [])
                     if i["id"] == self._sel_img_id), None)
        if not info:
            return
        fpath = os.path.join(IMG_DIR, info["file"])
        self._rsz_pil      = Image.open(fpath)
        self._rsz_orig_w   = self._rsz_pil.width
        self._rsz_orig_h   = self._rsz_pil.height
        self._rsz_pos      = pos
        self._rsz_x0       = event.x_root
        self._rsz_y0       = event.y_root

    def _img_rsz_drag(self, event, pos: str):
        if not hasattr(self, "_rsz_pil") or self._rsz_pil is None:
            return
        dx = event.x_root - self._rsz_x0
        dy = event.y_root - self._rsz_y0

        # Utiliser le plus grand des deux deltas (proportionnel)
        if "e" in pos:
            delta = dx
        else:
            delta = -dx

        new_w = max(20, self._rsz_orig_w + delta)
        ratio = new_w / self._rsz_orig_w
        new_h = int(self._rsz_orig_h * ratio)

        resized = self._rsz_pil.resize((new_w, new_h), Image.LANCZOS)
        photo   = ImageTk.PhotoImage(resized)
        self._photo_refs[self._sel_img_id] = photo
        self.text_area.image_configure(self._sel_img_id, image=photo)
        self.root.update_idletasks()
        self._update_img_handles()

    def _img_rsz_end(self, event):
        if not self._sel_img_id or not hasattr(self, "_rsz_pil"):
            return
        # Sauvegarder l'image redimensionnée sur disque
        photo = self._photo_refs.get(self._sel_img_id)
        if photo:
            note = self.notes[self.current_index]
            info = next((i for i in note.get("images", [])
                         if i["id"] == self._sel_img_id), None)
            if info:
                fpath   = os.path.join(IMG_DIR, info["file"])
                new_w   = photo.width()
                new_h   = photo.height()
                resized = self._rsz_pil.resize((new_w, new_h), Image.LANCZOS)
                resized.save(fpath, "PNG")
                self._save_notes()
        self._rsz_pil = None

    # ── Fenêtre ───────────────────────────────────────────────────────────────

    def _set_position(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = sw - self.win_w - MARGIN_RIGHT - STACK_OFFSET * 2
        y  = sh - self.win_h - MARGIN_BOTTOM - STACK_OFFSET * 2
        self.root.geometry(f"+{x}+{y}")

    # ── Redimensionnement fenêtre (bords et coins) ────────────────────────────

    def _setup_resize_edges(self, c: str, dark: str):
        """Bandes invisibles sur tous les bords/coins pour resize standard."""
        E = 5   # épaisseur bande
        C = 10  # taille coin
        # (nom, bg, cursor, relx, rely, anchor, kw_place)
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
            f.bind("<Button-1>",      lambda e, n=name: self._win_rsz_start(e, n))
            f.bind("<B1-Motion>",     lambda e, n=name: self._win_rsz_drag(e, n))
            f.bind("<ButtonRelease-1>", self._win_rsz_end)
            self._rsz_edges[name] = f
        self._wrsz_zone = None

    def _win_rsz_start(self, event, zone: str):
        self._wrsz_zone = zone
        self._wrsz_x0   = event.x_root
        self._wrsz_y0   = event.y_root
        self._wrsz_wx   = self.root.winfo_x()
        self._wrsz_wy   = self.root.winfo_y()
        self._wrsz_w0   = self.win_w
        self._wrsz_h0   = self.win_h

    def _win_rsz_drag(self, event, zone: str):
        dx = event.x_root - self._wrsz_x0
        dy = event.y_root - self._wrsz_y0
        new_x, new_y = self._wrsz_wx, self._wrsz_wy
        new_w, new_h  = self._wrsz_w0, self._wrsz_h0

        if "e" in zone: new_w = max(220, self._wrsz_w0 + dx)
        if "s" in zone: new_h = max(180, self._wrsz_h0 + dy)
        if "w" in zone:
            new_w = max(220, self._wrsz_w0 - dx)
            new_x = self._wrsz_wx + (self._wrsz_w0 - new_w)
        if "n" in zone:
            new_h = max(180, self._wrsz_h0 - dy)
            new_y = self._wrsz_wy + (self._wrsz_h0 - new_h)

        self.win_w = new_w
        self.win_h = new_h
        self.root.geometry(
            f"{new_w + STACK_OFFSET*2}x{new_h + STACK_OFFSET*2}+{new_x}+{new_y}"
        )
        self.card.place(width=new_w, height=new_h)
        self.stack1.place(width=new_w, height=new_h)
        self.stack2.place(width=new_w, height=new_h)

    def _win_rsz_end(self, event):
        self._wrsz_zone = None
        self._save_config()

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

    def toggle(self):
        if self.visible:
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
        self._save_notes()
        self.notes.append(empty_note())
        self.current_index = len(self.notes) - 1
        self._refresh_ui()
        self._save_notes()

    def _delete_note(self):
        if len(self.notes) == 1:
            self.notes[0] = empty_note()
            self._refresh_ui()
            return
        self.notes.pop(self.current_index)
        self.current_index = min(self.current_index, len(self.notes) - 1)
        self._refresh_ui()
        self._save_notes()

    def _on_scroll(self, event):
        self._save_notes()
        direction = -1 if event.delta > 0 else 1
        self.current_index = (self.current_index + direction) % len(self.notes)
        self._refresh_ui()
        return "break"

    def _refresh_ui(self):
        c    = self._note_color()
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
        self.footer.config(bg=dark)
        self.fmt_bar.config(bg=c)

        for btn in (self.btn_add, self.btn_del):
            btn.config(bg=dark, activebackground=darken(dark, 0.88))
        # Pastille couleur
        self.color_dot.config(bg=dark)
        self.color_dot.itemconfig("dot", fill=c)
        # Bandes de resize
        for name, f in self._rsz_edges.items():
            f.config(bg=dark if name in ("n", "s", "nw", "ne", "sw", "se") else c)
            f.lift()

        fbtn_kw = dict(bg=c, activebackground=darken(c, 0.85))
        for btn in self.fmt_btns.values():
            btn.config(**fbtn_kw)
        self.fmt_bar.config(bg=c)

        self._configured_tags = set()
        self._fmt_bar_visible = False
        self.fmt_bar.place_forget()
        if hasattr(self, "_img_handles"):
            self._hide_img_handles()
        self._setup_text_tags()
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
        self.hotkey_vk    = vk
        self.hotkey_mods  = mods
        self.hotkey_label = label
        self._save_config()
        self._start_hotkey_thread()

    def _open_hotkey_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Raccourci clavier")
        dlg.geometry("340x185")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.update_idletasks()
        dlg.geometry(
            f"+{(dlg.winfo_screenwidth()-340)//2}"
            f"+{(dlg.winfo_screenheight()-185)//2}"
        )

        captured = {"vk": self.hotkey_vk, "mods": self.hotkey_mods,
                    "label": self.hotkey_label}

        tk.Label(dlg, text="Appuie sur ton nouveau raccourci :",
                 font=("Segoe UI", 10)).pack(pady=(18, 8))
        lbl = tk.Label(dlg, text=self.hotkey_label,
                       font=("Segoe UI", 14, "bold"), fg="#2a6099",
                       relief="groove", width=22, pady=7)
        lbl.pack(padx=20, fill="x")

        IGNORE = {"Control_L", "Control_R", "Shift_L", "Shift_R",
                  "Alt_L", "Alt_R", "Super_L", "Super_R"}

        def on_key(ev):
            if ev.keysym in IGNORE:
                return
            mods, parts = 0, []
            if ev.state & 0x4:     mods |= MOD_CTRL;  parts.append("Ctrl")
            if ev.state & 0x1:     mods |= MOD_SHIFT; parts.append("Shift")
            if ev.state & 0x20000: mods |= MOD_ALT;   parts.append("Alt")
            parts.append(
                ev.keysym.upper() if len(ev.keysym) == 1 else ev.keysym
            )
            label = "+".join(parts)
            captured.update({"vk": ev.keycode, "mods": mods, "label": label})
            lbl.config(text=label)

        dlg.bind("<KeyPress>", on_key)
        dlg.focus_set()

        bf = tk.Frame(dlg); bf.pack(pady=14)
        tk.Button(bf, text="Annuler", command=dlg.destroy,
                  font=("Segoe UI", 9), width=10).pack(side="left", padx=6)

        def apply():
            self._apply_hotkey(captured["vk"], captured["mods"], captured["label"])
            dlg.destroy()

        tk.Button(bf, text="Appliquer", command=apply,
                  font=("Segoe UI", 9, "bold"), width=10,
                  bg="#2a6099", fg="white").pack(side="left", padx=6)

    # ── Systray ───────────────────────────────────────────────────────────────

    def _make_tray_icon(self) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.rectangle([12, 16, 56, 58], fill="#98D9A4", outline="#6ab07a", width=1)
        d.rectangle([8,  12, 52, 54], fill="#89C4E1", outline="#5a9cbe", width=1)
        d.rectangle([4,   8, 48, 50], fill="#F7E66A", outline="#c4b240", width=1)
        d.line([11, 19, 41, 19], fill="#777", width=2)
        d.line([11, 26, 41, 26], fill="#777", width=2)
        d.line([11, 33, 28, 33], fill="#777", width=2)
        return img

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(
                "Afficher / Masquer",
                lambda: self.root.after(0, self.toggle),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Changer le raccourci",
                lambda: self.root.after(0, self._open_hotkey_dialog),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quitter TNote v2",
                lambda: self.root.after(0, self._quit),
            ),
        )
        self.tray = pystray.Icon("TNote2", self._make_tray_icon(), "TNote v2", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _autosave_loop(self):
        self._save_notes()
        self.root.after(30_000, self._autosave_loop)

    def _quit(self):
        self._save_notes()
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TNote()
    app.run()
