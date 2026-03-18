import tkinter as tk
import json
import os
import re
import sys
import subprocess
import threading
import urllib.request
import ctypes
import ctypes.wintypes
import uuid as uuid_mod
import pystray
from PIL import Image, ImageDraw, ImageGrab, ImageTk

# ─── Version & mise à jour automatique ──────────────────────────────────────
VERSION = "1.1.0"
# Mets ici l'URL brute de ton repo GitHub (voir README pour setup)
# Exemple : "https://raw.githubusercontent.com/fred/tnote/main/"
UPDATE_URL = "https://raw.githubusercontent.com/Esteban-hye/Tnote/refs/heads/main/"

# ─── Couleurs bristol ────────────────────────────────────────────────────────
BRISTOL = [
    "#F7E66A",  # Jaune
    "#89C4E1",  # Bleu
    "#98D9A4",  # Vert
    "#F4A7B9",  # Rose
    "#FFBE76",  # Orange
    "#B39DDB",  # Violet
    "#80CBC4",  # Turquoise
    "#FFCC80",  # Pêche
    "#EF9A9A",  # Rouge pastel
    "#A5D6A7",  # Vert clair
]

TRANSPARENT_BG = "#010101"
WINDOW_W       = 290
WINDOW_H       = 370
MARGIN_RIGHT   = 20
MARGIN_BOTTOM  = 55
STACK_OFFSET   = 4
MAX_IMG_W      = WINDOW_W - 28

BASE_DIR  = os.path.join(os.path.expanduser("~"), ".tnote")
DATA_FILE = os.path.join(BASE_DIR, "notes.json")
CFG_FILE  = os.path.join(BASE_DIR, "config.json")
IMG_DIR   = os.path.join(BASE_DIR, "images")

WM_HOTKEY  = 0x0312
WM_QUIT    = 0x0012
MOD_CTRL   = 0x0002
MOD_ALT    = 0x0001
MOD_SHIFT  = 0x0004
HOTKEY_ID  = 1

IMG_RE = re.compile(r"\[\[IMG:([a-f0-9]+)\]\]")


def darken(hex_color: str, factor: float = 0.82) -> str:
    r = int(int(hex_color[1:3], 16) * factor)
    g = int(int(hex_color[3:5], 16) * factor)
    b = int(int(hex_color[5:7], 16) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def empty_note() -> dict:
    return {"title": "", "text": "", "images": []}


def migrate(note) -> dict:
    """Convertit l'ancien format (string) vers le nouveau (dict)."""
    if isinstance(note, str):
        return {"title": "", "text": note, "images": []}
    return note


# ─── App principale ──────────────────────────────────────────────────────────
class TNote:
    def __init__(self):
        os.makedirs(BASE_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)

        self.notes: list[dict]          = [empty_note()]
        self.current_index: int         = 0
        self.visible: bool              = False
        self._drag_offset               = (0, 0)
        self._photo_refs: dict          = {}   # img_id → PhotoImage (anti-GC)
        self._hotkey_thread_id: int     = 0

        self.hotkey_vk    = ord("N")
        self.hotkey_mods  = MOD_CTRL | MOD_ALT
        self.hotkey_label = "Ctrl+Alt+N"
        self.win_w = WINDOW_W
        self.win_h = WINDOW_H

        self._load_config()
        self._load_notes()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg=TRANSPARENT_BG)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT_BG)
        self.root.geometry(
            f"{self.win_w + STACK_OFFSET*2}x{self.win_h + STACK_OFFSET*2}"
        )

        self._build_ui()
        self._set_position()
        self._start_hotkey_thread()
        self._setup_tray()
        self._autosave_loop()
        self._check_update()  # vérification silencieuse en arrière-plan

    # ── Données ──────────────────────────────────────────────────────────────

    def _load_notes(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.notes = [migrate(n) for n in d.get("notes", [empty_note()])]
                self.current_index = min(
                    d.get("current_index", 0), len(self.notes) - 1
                )
                return
            except Exception:
                pass
        self.notes = [empty_note()]
        self.current_index = 0

    def _capture_current(self):
        """Capture le contenu de l'UI dans self.notes[current_index]."""
        if not hasattr(self, "text_area"):
            return
        note = self.notes[self.current_index]
        note["title"] = self.title_var.get()

        # Dump text + images en ordre
        items = self.text_area.dump("1.0", "end-1c", image=True, text=True)
        parts, ids_found = [], []
        for key, value, _ in items:
            if key == "text":
                parts.append(value)
            elif key == "image":
                parts.append(f"[[IMG:{value}]]")
                ids_found.append(value)

        note["text"] = "".join(parts)
        # Ne garder que les images encore présentes dans le texte
        note["images"] = [
            img for img in note.get("images", []) if img["id"] in ids_found
        ]

    def _save_notes(self):
        self._capture_current()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"notes": self.notes, "current_index": self.current_index},
                f, ensure_ascii=False, indent=2,
            )

    def _load_into_widget(self):
        """Charge la note courante dans les widgets."""
        note = self.notes[self.current_index]
        self.title_var.set(note.get("title", ""))

        self.text_area.delete("1.0", "end")
        imgs_by_id = {i["id"]: i for i in note.get("images", [])}

        text = note.get("text", "")
        parts = IMG_RE.split(text)  # alterne: texte, img_id, texte, …
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part:
                    self.text_area.insert("end", part)
            else:
                img_id = part
                if img_id in imgs_by_id:
                    fpath = os.path.join(IMG_DIR, imgs_by_id[img_id]["file"])
                    if os.path.exists(fpath):
                        try:
                            pil = Image.open(fpath)
                            photo = ImageTk.PhotoImage(pil)
                            self._photo_refs[img_id] = photo
                            self.text_area.image_create(
                                "end", image=photo, name=img_id
                            )
                        except Exception:
                            pass

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

    # ── Interface ────────────────────────────────────────────────────────────

    def _color(self, offset: int = 0) -> str:
        return BRISTOL[(self.current_index + offset) % len(BRISTOL)]

    def _build_ui(self):
        c    = self._color()
        dark = darken(c)

        # Cartes empilées derrière
        self.stack2 = tk.Frame(self.root, bg=darken(self._color(2), 0.88))
        self.stack2.place(x=STACK_OFFSET*2, y=STACK_OFFSET*2,
                          width=self.win_w, height=self.win_h)
        self.stack1 = tk.Frame(self.root, bg=darken(self._color(1), 0.92))
        self.stack1.place(x=STACK_OFFSET, y=STACK_OFFSET,
                          width=self.win_w, height=self.win_h)

        # Carte principale
        self.card = tk.Frame(self.root, bg=c)
        self.card.place(x=0, y=0, width=self.win_w, height=self.win_h)

        # ── Header ──
        self.header = tk.Frame(self.card, bg=dark, height=32)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.lbl_index = tk.Label(
            self.header, text=self._note_label(),
            bg=dark, fg="#1a1a1a", font=("Segoe UI", 8, "bold"),
        )
        self.lbl_index.pack(side="left", padx=10)

        btn_kw = dict(
            bg=dark, fg="#1a1a1a", bd=0, relief="flat",
            font=("Segoe UI", 13), cursor="hand2",
            activebackground=darken(dark, 0.9), activeforeground="#000",
        )
        self.btn_del = tk.Button(self.header, text="×",
                                 command=self._delete_note, **btn_kw)
        self.btn_del.pack(side="right", padx=(0, 8))
        self.btn_add = tk.Button(self.header, text="+",
                                 command=self._add_note, **btn_kw)
        self.btn_add.pack(side="right", padx=2)

        # ── Titre ──
        self.title_var = tk.StringVar(
            value=self.notes[self.current_index].get("title", "")
        )
        self.title_entry = tk.Entry(
            self.card, textvariable=self.title_var,
            bg=darken(c, 0.91), fg="#111", bd=0,
            font=("Segoe UI", 10, "bold"),
            insertbackground="#333",
            relief="flat",
        )
        self.title_entry.pack(fill="x", padx=10, pady=(7, 0), ipady=5)

        # Placeholder pour le titre
        self._title_placeholder("Titre (optionnel)")

        # Séparateur
        self.sep = tk.Frame(self.card, bg=darken(c, 0.83), height=1)
        self.sep.pack(fill="x", padx=10, pady=(5, 0))

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
        self._load_into_widget()

        # ── Footer ──
        self.footer = tk.Frame(self.card, bg=dark, height=20)
        self.footer.pack(fill="x", side="bottom")
        self.footer.pack_propagate(False)
        self.lbl_hint = tk.Label(
            self.footer,
            text="Ctrl+↕ changer note  •  Ctrl+V image",
            bg=dark, fg="#444", font=("Segoe UI", 7),
        )
        self.lbl_hint.pack(expand=True)

        # Poignée de redimensionnement — coin bas-droite, par-dessus tout
        self.grip = tk.Frame(
            self.card, bg=darken(dark, 0.85), cursor="size_nw_se",
            width=18, height=18,
        )
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<Button-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)

        # ── Bindings ──
        self.header.bind("<Button-1>", self._drag_start)
        self.header.bind("<B1-Motion>", self._drag_move)
        # Ctrl+molette = changer de note, molette seule = scroller le texte
        self.root.bind_all("<Control-MouseWheel>", self._on_scroll)
        self.text_area.bind("<KeyRelease>", lambda _: self._save_notes())
        self.title_entry.bind("<KeyRelease>", lambda _: self._save_notes())
        self.text_area.bind("<Control-v>", self._on_paste)

    def _title_placeholder(self, text: str):
        """Gère le placeholder grisé dans le champ titre."""
        ph_color = "#888"
        normal_color = "#111"

        def show_ph(e=None):
            if not self.title_var.get():
                self.title_entry.config(fg=ph_color)
                self.title_entry.insert(0, text)

        def hide_ph(e=None):
            if self.title_entry.get() == text and self.title_entry.cget("fg") == ph_color:
                self.title_entry.config(fg=normal_color)
                self.title_entry.delete(0, "end")

        self.title_entry.bind("<FocusIn>", hide_ph)
        self.title_entry.bind("<FocusOut>", show_ph)

        # Surcharge title_var.get pour filtrer le placeholder
        _orig_get = self.title_var.get

        def safe_get():
            val = _orig_get()
            return "" if (val == text and self.title_entry.cget("fg") == ph_color) else val

        self.title_var.get = safe_get
        show_ph()

    def _note_label(self) -> str:
        return f"  Note {self.current_index + 1} / {len(self.notes)}"

    def _refresh_ui(self):
        c    = self._color()
        dark = darken(c)

        self.stack2.config(bg=darken(self._color(2), 0.88))
        self.stack1.config(bg=darken(self._color(1), 0.92))
        self.card.config(bg=c)
        self.header.config(bg=dark)
        self.lbl_index.config(bg=dark, text=self._note_label())
        self.title_entry.config(bg=darken(c, 0.91))
        self.sep.config(bg=darken(c, 0.83))
        self.text_area.config(bg=c)
        self.footer.config(bg=dark)
        self.lbl_hint.config(bg=dark)
        for btn in (self.btn_add, self.btn_del):
            btn.config(bg=dark, activebackground=darken(dark, 0.9))
        self.grip.config(bg=darken(dark, 0.85))

        self._load_into_widget()
        self.text_area.focus_set()

    # ── Actions notes ────────────────────────────────────────────────────────

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

    # ── Images ───────────────────────────────────────────────────────────────

    def _on_paste(self, event):
        """Ctrl+V : image depuis le presse-papier, sinon texte normal."""
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None

        if isinstance(img, Image.Image):
            self._insert_image(img)
            return "break"
        # laisser le comportement par défaut pour le texte

    def _insert_image(self, pil_img: Image.Image):
        # Redimensionner si trop large
        if pil_img.width > MAX_IMG_W:
            ratio = MAX_IMG_W / pil_img.width
            pil_img = pil_img.resize(
                (MAX_IMG_W, int(pil_img.height * ratio)), Image.LANCZOS
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

    # ── Fenêtre ──────────────────────────────────────────────────────────────

    def _set_position(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = sw - self.win_w - MARGIN_RIGHT - STACK_OFFSET * 2
        y  = sh - self.win_h - MARGIN_BOTTOM - STACK_OFFSET * 2
        self.root.geometry(f"+{x}+{y}")

    def _resize_start(self, e):
        self._rsz_x = e.x_root
        self._rsz_y = e.y_root
        self._rsz_w = self.win_w
        self._rsz_h = self.win_h

    def _resize_move(self, e):
        new_w = max(220, self._rsz_w + (e.x_root - self._rsz_x))
        new_h = max(180, self._rsz_h + (e.y_root - self._rsz_y))
        self.win_w = new_w
        self.win_h = new_h
        self.root.geometry(
            f"{new_w + STACK_OFFSET*2}x{new_h + STACK_OFFSET*2}"
        )
        self.card.place(width=new_w, height=new_h)
        self.stack1.place(width=new_w, height=new_h)
        self.stack2.place(width=new_w, height=new_h)
        self._save_config()

    def _drag_start(self, e):
        self._drag_offset = (
            e.x_root - self.root.winfo_x(),
            e.y_root - self.root.winfo_y(),
        )

    def _drag_move(self, e):
        self.root.geometry(
            f"+{e.x_root - self._drag_offset[0]}+{e.y_root - self._drag_offset[1]}"
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

    # ── Raccourci clavier (Windows RegisterHotKey) ───────────────────────────

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
        """Arrête l'ancien thread, enregistre le nouveau raccourci."""
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
        """Fenêtre de capture du nouveau raccourci."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Raccourci clavier")
        dlg.geometry("340x185")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.update_idletasks()
        dlg.geometry(
            f"+{(dlg.winfo_screenwidth()-340)//2}+{(dlg.winfo_screenheight()-185)//2}"
        )

        captured = {"vk": self.hotkey_vk, "mods": self.hotkey_mods,
                    "label": self.hotkey_label}

        tk.Label(dlg, text="Appuie sur ton nouveau raccourci :",
                 font=("Segoe UI", 10)).pack(pady=(18, 8))

        lbl = tk.Label(dlg, text=self.hotkey_label,
                       font=("Segoe UI", 14, "bold"), fg="#2a6099",
                       relief="groove", width=22, pady=7)
        lbl.pack(padx=20, fill="x")

        MODS_IGNORE = {
            "Control_L", "Control_R", "Shift_L", "Shift_R",
            "Alt_L", "Alt_R", "Super_L", "Super_R",
        }

        def on_key(ev):
            if ev.keysym in MODS_IGNORE:
                return
            mods, parts = 0, []
            if ev.state & 0x4:
                mods |= MOD_CTRL;  parts.append("Ctrl")
            if ev.state & 0x1:
                mods |= MOD_SHIFT; parts.append("Shift")
            if ev.state & 0x20000 or ev.state & 0x8:
                mods |= MOD_ALT;   parts.append("Alt")
            parts.append(ev.keysym.upper() if len(ev.keysym) == 1 else ev.keysym)
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

    # ── Systray ──────────────────────────────────────────────────────────────

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
                "Quitter TNote",
                lambda: self.root.after(0, self._quit),
            ),
        )
        self.tray = pystray.Icon("TNote", self._make_tray_icon(), "TNote", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ── Mise à jour automatique ──────────────────────────────────────────────

    def _check_update(self):
        """Vérifie silencieusement GitHub au démarrage."""
        if not UPDATE_URL:
            return

        def run():
            try:
                remote_ver = urllib.request.urlopen(
                    UPDATE_URL + "version.txt", timeout=5
                ).read().decode().strip()

                if remote_ver == VERSION:
                    return  # déjà à jour

                new_code = urllib.request.urlopen(
                    UPDATE_URL + "tnote.py", timeout=15
                ).read()

                script = os.path.abspath(__file__)
                tmp = script + ".update"
                with open(tmp, "wb") as f:
                    f.write(new_code)
                os.replace(tmp, script)

                self.root.after(0, self._restart)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _restart(self):
        self._save_notes()
        subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        self.root.after(300, self._force_quit)

    def _force_quit(self):
        self.tray.stop()
        self.root.destroy()

    # ── Lifecycle ────────────────────────────────────────────────────────────

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
