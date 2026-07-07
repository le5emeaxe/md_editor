def _iter_at_line(buf, lineno):
    """GTK4 : get_iter_at_line retourne (bool, TextIter) au lieu de TextIter."""
    result = buf.get_iter_at_line(lineno)
    return result[1] if isinstance(result, tuple) else result


#!/usr/bin/env python3
"""
md_editor.py — Éditeur Markdown GTK4
Dépendances :
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-webkit2-4.1 python3-markdown
"""

import sys

import re, threading, urllib.request, urllib.parse, json
import os, sqlite3, math, subprocess, zipfile, warnings
import cairo

# Supprimer les DeprecationWarning GTK4 liés à TreeView/TreeStore
# (fonctionnels mais deprecated - migration Gtk.ListView prévue plus tard)
warnings.filterwarnings('ignore', category=DeprecationWarning)
from pathlib import Path
from datetime import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
except ValueError:
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit
    except ValueError:
        print("WebKit non trouvé."); sys.exit(1)

from gi.repository import Gtk, Gdk, GLib, Pango, Gio, GdkPixbuf
import markdown

# ── Constantes ────────────────────────────────────────────────────────────────

LT_URL           = "https://monserveurlanguagetool.fr/v2/check"
LT_LANGUAGE      = "fr"
LT_DEBOUNCE      = 1200
PREVIEW_DEBOUNCE = 150
CONFIG_DIR       = Path.home() / ".config" / "md-editor"
CONFIG_PATH      = CONFIG_DIR / "config.json"
DB_PATH          = CONFIG_DIR / "notes.db"
SCAN_INTERVAL_MS = 3000

# ── Helpers texte ─────────────────────────────────────────────────────────────

def new_note_content():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return "# Nouvelle note " + now + "\n\nCommencez a ecrire votre note en Markdown.\n"

def extract_title(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            for ch in '/:*?"<>|\\':
                title = title.replace(ch, "-")
            title = "_".join(title.split())
            title = title.strip("._-")
            if title:
                return title
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"notes_dir": str(Path.home() / "Documents" / "notes"),
                "lt_language": "fr", "lt_enabled": False, "font_size": 14, "font_family": "Noto Sans",
                "scp_host": "", "scp_user": "", "scp_remote_dir": "", "scp_password": ""}

def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ── Base SQLite ───────────────────────────────────────────────────────────────

class NotesDB:
    """Gère les étiquettes et leur association aux notes."""

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self._con.executescript("""
            CREATE TABLE IF NOT EXISTS tags (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                label   TEXT NOT NULL UNIQUE,
                color   TEXT NOT NULL DEFAULT '#89b4fa'
            );
            CREATE TABLE IF NOT EXISTS note_tags (
                note_path TEXT NOT NULL,
                tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (note_path, tag_id)
            );
            CREATE TABLE IF NOT EXISTS note_meta (
                note_path TEXT PRIMARY KEY,
                pinned    INTEGER NOT NULL DEFAULT 0,
                favorite  INTEGER NOT NULL DEFAULT 0,
                trashed   INTEGER NOT NULL DEFAULT 0,
                trash_date TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_note_tags_path ON note_tags(note_path);
            CREATE INDEX IF NOT EXISTS idx_note_tags_tag  ON note_tags(tag_id);
            CREATE TABLE IF NOT EXISTS sync_status (
                note_path  TEXT PRIMARY KEY,
                synced_at  TEXT,
                remote_ok  INTEGER NOT NULL DEFAULT 0,
                sha256     TEXT
            );
        """)
        # Migration : ajouter sha256 si absent
        try:
            self._con.execute('ALTER TABLE sync_status ADD COLUMN sha256 TEXT')
            self._con.commit()
        except Exception:
            pass  # colonne déjà présente
        self._con.commit()

    # ── Chiffrement local des notes ─────────────────────────────────────────
    def _ensure_encrypted_table(self):
        self._con.execute(
            'CREATE TABLE IF NOT EXISTS encrypted_notes (note_path TEXT PRIMARY KEY)')
        self._con.commit()

    def is_note_encrypted(self, note_path):
        self._ensure_encrypted_table()
        row = self._con.execute(
            'SELECT 1 FROM encrypted_notes WHERE note_path=?',
            (str(note_path),)).fetchone()
        return row is not None

    def set_note_encrypted(self, note_path, encrypted=True):
        self._ensure_encrypted_table()
        if encrypted:
            self._con.execute(
                'INSERT OR IGNORE INTO encrypted_notes(note_path) VALUES(?)',
                (str(note_path),))
        else:
            self._con.execute(
                'DELETE FROM encrypted_notes WHERE note_path=?',
                (str(note_path),))
        self._con.commit()

    # ── Sync SCP ─────────────────────────────────────────────────────────────
    def set_synced(self, note_path, ok=True, sha256=None):
        from datetime import datetime
        self._con.execute(
            'INSERT OR REPLACE INTO sync_status(note_path, synced_at, remote_ok, sha256) VALUES(?,?,?,?)',
            (str(note_path), datetime.now().isoformat(timespec='seconds'), 1 if ok else 0, sha256))
        self._con.commit()

    def get_synced_sha256(self, note_path):
        """Retourne le sha256 stocké lors de la dernière sync réussie, ou None."""
        row = self._con.execute(
            'SELECT sha256 FROM sync_status WHERE note_path=? AND remote_ok=1',
            (str(note_path),)).fetchone()
        return row['sha256'] if row else None

    def get_sync_status(self, note_path):
        row = self._con.execute(
            'SELECT remote_ok, synced_at FROM sync_status WHERE note_path=?',
            (str(note_path),)).fetchone()
        if row is None: return None, None
        return bool(row['remote_ok']), row['synced_at']

    def clear_sync_status(self):
        self._con.execute('DELETE FROM sync_status')
        self._con.commit()

    # ── Tags CRUD ─────────────────────────────────────────────────────────────
    def get_tags(self):
        return list(self._con.execute("SELECT * FROM tags ORDER BY label"))

    def add_tag(self, label, color="#89b4fa"):
        try:
            self._con.execute("INSERT INTO tags(label,color) VALUES(?,?)", (label, color))
            self._con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_tag(self, tag_id, label, color):
        self._con.execute("UPDATE tags SET label=?,color=? WHERE id=?", (label, color, tag_id))
        self._con.commit()

    def delete_tag(self, tag_id):
        self._con.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        self._con.commit()

    # ── Association note ↔ tags ───────────────────────────────────────────────
    def get_tags_for_note(self, note_path):
        return list(self._con.execute(
            "SELECT t.* FROM tags t JOIN note_tags nt ON t.id=nt.tag_id WHERE nt.note_path=?",
            (str(note_path),)))

    def get_tag_ids_for_note(self, note_path):
        rows = self._con.execute(
            "SELECT tag_id FROM note_tags WHERE note_path=?", (str(note_path),))
        return {r[0] for r in rows}

    def set_tags_for_note(self, note_path, tag_ids):
        p = str(note_path)
        self._con.execute("DELETE FROM note_tags WHERE note_path=?", (p,))
        for tid in tag_ids:
            self._con.execute("INSERT OR IGNORE INTO note_tags VALUES(?,?)", (p, tid))
        self._con.commit()

    def rename_note_path(self, old_path, new_path):
        self._con.execute("UPDATE note_tags SET note_path=? WHERE note_path=?",
                          (str(new_path), str(old_path)))
        self._con.commit()

    def search_by_tags(self, tag_ids):
        """Retourne les chemins de notes ayant TOUS les tag_ids donnés."""
        if not tag_ids:
            return None  # pas de filtre
        placeholders = ",".join("?" * len(tag_ids))
        rows = self._con.execute(
            f"SELECT note_path FROM note_tags WHERE tag_id IN ({placeholders})"
            f" GROUP BY note_path HAVING COUNT(DISTINCT tag_id)=?",
            list(tag_ids) + [len(tag_ids)])
        return {Path(r[0]) for r in rows}

    # ── Note meta (pinned, favorite, trash) ──────────────────────────────
    def _get_meta(self, path):
        row = self._con.execute(
            'SELECT * FROM note_meta WHERE note_path=?', (str(path),)).fetchone()
        return row

    def _ensure_meta(self, path):
        self._con.execute(
            'INSERT OR IGNORE INTO note_meta(note_path) VALUES(?)', (str(path),))

    def is_pinned(self, path):
        r = self._get_meta(path)
        return bool(r['pinned']) if r else False

    def is_favorite(self, path):
        r = self._get_meta(path)
        return bool(r['favorite']) if r else False

    def is_trashed(self, path):
        r = self._get_meta(path)
        return bool(r['trashed']) if r else False

    def set_pinned(self, path, val):
        self._ensure_meta(path)
        self._con.execute(
            'UPDATE note_meta SET pinned=? WHERE note_path=?', (1 if val else 0, str(path)))
        self._con.commit()

    def set_favorite(self, path, val):
        self._ensure_meta(path)
        self._con.execute(
            'UPDATE note_meta SET favorite=? WHERE note_path=?', (1 if val else 0, str(path)))
        self._con.commit()

    def trash_note(self, path):
        self._ensure_meta(path)
        self._con.execute(
            'UPDATE note_meta SET trashed=1, trash_date=? WHERE note_path=?',
            (datetime.now().isoformat(), str(path)))
        self._con.commit()

    def restore_note(self, path):
        self._con.execute(
            'UPDATE note_meta SET trashed=0, trash_date=NULL WHERE note_path=?', (str(path),))
        self._con.commit()

    def get_trashed(self):
        rows = self._con.execute(
            'SELECT note_path, trash_date FROM note_meta WHERE trashed=1 ORDER BY trash_date DESC')
        return list(rows)

    def get_pinned_paths(self):
        rows = self._con.execute(
            'SELECT note_path FROM note_meta WHERE pinned=1')
        return {Path(r[0]) for r in rows}

    def get_favorite_paths(self):
        rows = self._con.execute(
            'SELECT note_path FROM note_meta WHERE favorite=1')
        return {Path(r[0]) for r in rows}

    def rename_note_meta(self, old_path, new_path):
        self._con.execute(
            'UPDATE note_meta SET note_path=? WHERE note_path=?',
            (str(new_path), str(old_path)))
        self._con.commit()

    def close(self):
        self._con.close()

# ── Utilitaire chip colorée ──────────────────────────────────────────────────

_chip_counter = 0

def _parse_color(hex_color: str):
    """Convertit #rrggbb en (r, g, b) floats 0..1."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0.5, 0.5, 0.5)
    return (int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255)


def make_colored_chip(label_text: str, color: str) -> 'ChipWidget':
    """Chip colorée dessinée en Cairo — couleur garantie indépendamment du thème GTK."""
    return ChipWidget(label_text, color, clickable=False)


def make_colored_button_chip(label_text: str, color: str, is_active: bool = False) -> 'ChipWidget':
    """Bouton chip coloré dessiné en Cairo."""
    return ChipWidget(label_text, color, clickable=True, active=is_active)


class ChipWidget(Gtk.DrawingArea):
    """
    Widget chip colorée dessinée entièrement en Cairo.
    Contourne tous les problèmes de CSS GTK4 en dessinant directement
    le fond arrondi et le texte blanc.
    """
    PADDING_X = 10
    PADDING_Y = 4
    RADIUS    = 9

    @property
    def FONT_SIZE(self):
        """Taille de police dynamique : 70% de la taille UI globale."""
        return max(8, int(_CURRENT_FONT_SIZE * 0.75))

    def __init__(self, label: str, color: str, clickable: bool = False, active: bool = False):
        super().__init__()
        self._label     = label
        self._color     = _parse_color(color)
        self._hex_color = color
        self._clickable = clickable
        self._active    = active
        self._hovered   = False

        self.set_draw_func(self._draw)
        self._measure_size()

        if clickable:
            gc = Gtk.GestureClick()
            gc.set_button(1)
            gc.connect("pressed", self._on_click)
            self.add_controller(gc)
            mc = Gtk.EventControllerMotion()
            mc.connect("enter", lambda *_: self._set_hover(True))
            mc.connect("leave", lambda *_: self._set_hover(False))
            self.add_controller(mc)
            self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        # Signal custom pour le clic (connecté depuis l'extérieur)
        self._click_callbacks = []

    def _measure_size(self):
        """Calcule la taille minimale selon le texte."""
        import cairo
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        ctx  = cairo.Context(surf)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(self.FONT_SIZE)
        ext = ctx.text_extents(self._label)
        w = int(ext.width  + self.PADDING_X * 2) + 2
        h = int(self.FONT_SIZE + self.PADDING_Y * 2) + 2
        self.set_content_width(max(w, 30))
        self.set_content_height(max(h, 20))

    def _draw(self, area, ctx, width, height, *_):
        import math
        # Recalculer la taille si la police a changé
        self._measure_size()
        r, g, b = self._color
        alpha = 0.75 if (self._hovered and self._clickable) else 1.0

        # Fond arrondi
        x0, y0 = 0.5, 0.5
        x1, y1 = width - 0.5, height - 0.5
        rad = self.RADIUS
        ctx.new_sub_path()
        ctx.arc(x1 - rad, y0 + rad, rad, -math.pi/2, 0)
        ctx.arc(x1 - rad, y1 - rad, rad,  0,          math.pi/2)
        ctx.arc(x0 + rad, y1 - rad, rad,  math.pi/2,  math.pi)
        ctx.arc(x0 + rad, y0 + rad, rad,  math.pi,    3*math.pi/2)
        ctx.close_path()
        ctx.set_source_rgba(r, g, b, alpha)
        ctx.fill()

        # Bordure dorée si actif
        if self._active:
            ctx.new_sub_path()
            ctx.arc(x1 - rad, y0 + rad, rad, -math.pi/2, 0)
            ctx.arc(x1 - rad, y1 - rad, rad,  0,          math.pi/2)
            ctx.arc(x0 + rad, y1 - rad, rad,  math.pi/2,  math.pi)
            ctx.arc(x0 + rad, y0 + rad, rad,  math.pi,    3*math.pi/2)
            ctx.close_path()
            ctx.set_source_rgb(0.976, 0.886, 0.686)  # #f9e2af
            ctx.set_line_width(2)
            ctx.stroke()

        # Texte blanc centré
        ctx.select_font_face("Sans", 0, 1)  # normal, bold
        ctx.set_font_size(self.FONT_SIZE)
        ext = ctx.text_extents(self._label)
        tx = (width  - ext.width)  / 2 - ext.x_bearing
        ty = (height - ext.height) / 2 - ext.y_bearing
        ctx.set_source_rgb(1, 1, 1)
        ctx.move_to(tx, ty)
        ctx.show_text(self._label)

    def set_active(self, active: bool):
        self._active = active
        self.queue_draw()

    def _set_hover(self, val: bool):
        self._hovered = val
        self.queue_draw()

    def _on_click(self, *_):
        for cb in self._click_callbacks:
            cb()

    def connect_click(self, cb):
        """Connecte un callback appelé au clic."""
        self._click_callbacks.append(cb)

    def get_hex_color(self):
        return self._hex_color

    def get_label_text(self):
        return self._label


# ── CSS preview WebKit ────────────────────────────────────────────────────────

def _make_preview_css(font_size=15):
    return """
:root {
    --bg:#1e1e2e;--bg-code:#2a2a3e;--bg-table:#252535;--fg:#cdd6f4;
    --fg-dim:#a6adc8;--accent:#89b4fa;--green:#a6e3a1;--yellow:#f9e2af;
    --red:#f38ba8;--border:#45475a;--radius:6px;
    --font-mono:'Monospace',monospace;
    --font-sans:'Noto Sans',system-ui,sans-serif;
}
*{box-sizing:border-box;}
body{background:var(--bg);color:var(--fg);font-family:var(--font-sans);
    font-size:""" + str(font_size) + """px;line-height:1.7;padding:24px 32px;margin:0;max-width:860px;}
h1,h2,h3,h4,h5,h6{color:var(--accent);font-weight:600;line-height:1.3;
    margin-top:1.6em;margin-bottom:.4em;}
h1{font-size:2em;border-bottom:2px solid var(--border);padding-bottom:.3em;}
h2{font-size:1.5em;border-bottom:1px solid var(--border);padding-bottom:.2em;}
h3{font-size:1.2em;}
p{margin:.6em 0 1em;}
a{color:var(--accent);}
strong{color:var(--yellow);font-weight:700;}
em{color:var(--fg-dim);font-style:italic;}
code{font-family:var(--font-mono);font-size:.88em;background:var(--bg-code);
    color:var(--green);padding:2px 6px;border-radius:4px;border:1px solid var(--border);}
pre{background:var(--bg-code);border:1px solid var(--border);
    border-radius:var(--radius);padding:16px 20px;overflow-x:auto;overflow-wrap:break-word;white-space:pre-wrap;line-height:1.5;}
pre code{background:none;border:none;padding:0;color:var(--fg);font-size:.9em;}
blockquote{border-left:4px solid var(--accent);margin:1em 0;padding:8px 16px;
    background:var(--bg-code);border-radius:0 var(--radius) var(--radius) 0;
    color:var(--fg-dim);font-style:italic;}
ul,ol{padding-left:1.6em;margin:.5em 0 1em;}
li{margin:.25em 0;}
hr{border:none;border-top:1px solid var(--border);margin:2em 0;}
table{border-collapse:collapse;width:100%;margin:1em 0;background:var(--bg-table);
    border-radius:var(--radius);overflow:hidden;font-size:.93em;}
th{background:var(--bg-code);color:var(--accent);font-weight:600;
    text-align:left;padding:10px 14px;border-bottom:2px solid var(--border);}
td{padding:8px 14px;border-bottom:1px solid var(--border);}
tr:last-child td{border-bottom:none;}
tr:hover td{background:rgba(137,180,250,.05);}
img{max-width:100%;border-radius:var(--radius);}
"""

_CURRENT_FONT_SIZE   = 15               # modifié par les paramètres
_CURRENT_FONT_FAMILY = 'Noto Sans'       # modifié par les paramètres
PREVIEW_CSS = _make_preview_css()
MD_EXT = ["markdown.extensions.tables","markdown.extensions.fenced_code",
          "markdown.extensions.codehilite","markdown.extensions.toc",
          "markdown.extensions.nl2br","markdown.extensions.sane_lists"]
MD_CFG = {"codehilite":{"guess_lang":False,"noclasses":True,"pygments_style":"monokai"}}

MORPHDOM_JS = "(function(global,factory){typeof exports===\"object\"&&typeof module!==\"undefined\"?module.exports=factory():typeof define===\"function\"&&define.amd?define(factory):(global=global||self,global.morphdom=factory())})(this,function(){\"use strict\";var DOCUMENT_FRAGMENT_NODE=11;function morphAttrs(fromNode,toNode){var toNodeAttrs=toNode.attributes;var attr;var attrName;var attrNamespaceURI;var attrValue;var fromValue;if(toNode.nodeType===DOCUMENT_FRAGMENT_NODE||fromNode.nodeType===DOCUMENT_FRAGMENT_NODE){return}for(var i=toNodeAttrs.length-1;i>=0;i--){attr=toNodeAttrs[i];attrName=attr.name;attrNamespaceURI=attr.namespaceURI;attrValue=attr.value;if(attrNamespaceURI){attrName=attr.localName||attrName;fromValue=fromNode.getAttributeNS(attrNamespaceURI,attrName);if(fromValue!==attrValue){if(attr.prefix===\"xmlns\"){attrName=attr.name}fromNode.setAttributeNS(attrNamespaceURI,attrName,attrValue)}}else{fromValue=fromNode.getAttribute(attrName);if(fromValue!==attrValue){fromNode.setAttribute(attrName,attrValue)}}}var fromNodeAttrs=fromNode.attributes;for(var d=fromNodeAttrs.length-1;d>=0;d--){attr=fromNodeAttrs[d];attrName=attr.name;attrNamespaceURI=attr.namespaceURI;if(attrNamespaceURI){attrName=attr.localName||attrName;if(!toNode.hasAttributeNS(attrNamespaceURI,attrName)){fromNode.removeAttributeNS(attrNamespaceURI,attrName)}}else{if(!toNode.hasAttribute(attrName)){fromNode.removeAttribute(attrName)}}}}var range;var NS_XHTML=\"http://www.w3.org/1999/xhtml\";var doc=typeof document===\"undefined\"?undefined:document;var HAS_TEMPLATE_SUPPORT=!!doc&&\"content\"in doc.createElement(\"template\");var HAS_RANGE_SUPPORT=!!doc&&doc.createRange&&\"createContextualFragment\"in doc.createRange();function createFragmentFromTemplate(str){var template=doc.createElement(\"template\");template.innerHTML=str;return template.content.childNodes[0]}function createFragmentFromRange(str){if(!range){range=doc.createRange();range.selectNode(doc.body)}var fragment=range.createContextualFragment(str);return fragment.childNodes[0]}function createFragmentFromWrap(str){var fragment=doc.createElement(\"body\");fragment.innerHTML=str;return fragment.childNodes[0]}function toElement(str){str=str.trim();if(HAS_TEMPLATE_SUPPORT){return createFragmentFromTemplate(str)}else if(HAS_RANGE_SUPPORT){return createFragmentFromRange(str)}return createFragmentFromWrap(str)}function compareNodeNames(fromEl,toEl){var fromNodeName=fromEl.nodeName;var toNodeName=toEl.nodeName;var fromCodeStart,toCodeStart;if(fromNodeName===toNodeName){return true}fromCodeStart=fromNodeName.charCodeAt(0);toCodeStart=toNodeName.charCodeAt(0);if(fromCodeStart<=90&&toCodeStart>=97){return fromNodeName===toNodeName.toUpperCase()}else if(toCodeStart<=90&&fromCodeStart>=97){return toNodeName===fromNodeName.toUpperCase()}else{return false}}function createElementNS(name,namespaceURI){return!namespaceURI||namespaceURI===NS_XHTML?doc.createElement(name):doc.createElementNS(namespaceURI,name)}function moveChildren(fromEl,toEl){var curChild=fromEl.firstChild;while(curChild){var nextChild=curChild.nextSibling;toEl.appendChild(curChild);curChild=nextChild}return toEl}function syncBooleanAttrProp(fromEl,toEl,name){if(fromEl[name]!==toEl[name]){fromEl[name]=toEl[name];if(fromEl[name]){fromEl.setAttribute(name,\"\")}else{fromEl.removeAttribute(name)}}}var specialElHandlers={OPTION:function(fromEl,toEl){var parentNode=fromEl.parentNode;if(parentNode){var parentName=parentNode.nodeName.toUpperCase();if(parentName===\"OPTGROUP\"){parentNode=parentNode.parentNode;parentName=parentNode&&parentNode.nodeName.toUpperCase()}if(parentName===\"SELECT\"&&!parentNode.hasAttribute(\"multiple\")){if(fromEl.hasAttribute(\"selected\")&&!toEl.selected){fromEl.setAttribute(\"selected\",\"selected\");fromEl.removeAttribute(\"selected\")}parentNode.selectedIndex=-1}}syncBooleanAttrProp(fromEl,toEl,\"selected\")},INPUT:function(fromEl,toEl){syncBooleanAttrProp(fromEl,toEl,\"checked\");syncBooleanAttrProp(fromEl,toEl,\"disabled\");if(fromEl.value!==toEl.value){fromEl.value=toEl.value}if(!toEl.hasAttribute(\"value\")){fromEl.removeAttribute(\"value\")}},TEXTAREA:function(fromEl,toEl){var newValue=toEl.value;if(fromEl.value!==newValue){fromEl.value=newValue}var firstChild=fromEl.firstChild;if(firstChild){var oldValue=firstChild.nodeValue;if(oldValue==newValue||!newValue&&oldValue==fromEl.placeholder){return}firstChild.nodeValue=newValue}},SELECT:function(fromEl,toEl){if(!toEl.hasAttribute(\"multiple\")){var selectedIndex=-1;var i=0;var curChild=fromEl.firstChild;var optgroup;var nodeName;while(curChild){nodeName=curChild.nodeName&&curChild.nodeName.toUpperCase();if(nodeName===\"OPTGROUP\"){optgroup=curChild;curChild=optgroup.firstChild;if(!curChild){curChild=optgroup.nextSibling;optgroup=null}}else{if(nodeName===\"OPTION\"){if(curChild.hasAttribute(\"selected\")){selectedIndex=i;break}i++}curChild=curChild.nextSibling;if(!curChild&&optgroup){curChild=optgroup.nextSibling;optgroup=null}}}fromEl.selectedIndex=selectedIndex}}};var ELEMENT_NODE=1;var DOCUMENT_FRAGMENT_NODE$1=11;var TEXT_NODE=3;var COMMENT_NODE=8;function noop(){}function defaultGetNodeKey(node){if(node){return node.getAttribute&&node.getAttribute(\"id\")||node.id}}function morphdomFactory(morphAttrs){return function morphdom(fromNode,toNode,options){if(!options){options={}}if(typeof toNode===\"string\"){if(fromNode.nodeName===\"#document\"||fromNode.nodeName===\"HTML\"){var toNodeHtml=toNode;toNode=doc.createElement(\"html\");toNode.innerHTML=toNodeHtml}else if(fromNode.nodeName===\"BODY\"){var toNodeBody=toNode;toNode=doc.createElement(\"html\");toNode.innerHTML=toNodeBody;var bodyElement=toNode.querySelector(\"body\");if(bodyElement){toNode=bodyElement}}else{toNode=toElement(toNode)}}else if(toNode.nodeType===DOCUMENT_FRAGMENT_NODE$1){toNode=toNode.firstElementChild}var getNodeKey=options.getNodeKey||defaultGetNodeKey;var onBeforeNodeAdded=options.onBeforeNodeAdded||noop;var onNodeAdded=options.onNodeAdded||noop;var onBeforeElUpdated=options.onBeforeElUpdated||noop;var onElUpdated=options.onElUpdated||noop;var onBeforeNodeDiscarded=options.onBeforeNodeDiscarded||noop;var onNodeDiscarded=options.onNodeDiscarded||noop;var onBeforeElChildrenUpdated=options.onBeforeElChildrenUpdated||noop;var skipFromChildren=options.skipFromChildren||noop;var addChild=options.addChild||function(parent,child){return parent.appendChild(child)};var childrenOnly=options.childrenOnly===true;var fromNodesLookup=Object.create(null);var keyedRemovalList=[];function addKeyedRemoval(key){keyedRemovalList.push(key)}function walkDiscardedChildNodes(node,skipKeyedNodes){if(node.nodeType===ELEMENT_NODE){var curChild=node.firstChild;while(curChild){var key=undefined;if(skipKeyedNodes&&(key=getNodeKey(curChild))){addKeyedRemoval(key)}else{onNodeDiscarded(curChild);if(curChild.firstChild){walkDiscardedChildNodes(curChild,skipKeyedNodes)}}curChild=curChild.nextSibling}}}function removeNode(node,parentNode,skipKeyedNodes){if(onBeforeNodeDiscarded(node)===false){return}if(parentNode){parentNode.removeChild(node)}onNodeDiscarded(node);walkDiscardedChildNodes(node,skipKeyedNodes)}function indexTree(node){if(node.nodeType===ELEMENT_NODE||node.nodeType===DOCUMENT_FRAGMENT_NODE$1){var curChild=node.firstChild;while(curChild){var key=getNodeKey(curChild);if(key){fromNodesLookup[key]=curChild}indexTree(curChild);curChild=curChild.nextSibling}}}indexTree(fromNode);function handleNodeAdded(el){onNodeAdded(el);var curChild=el.firstChild;while(curChild){var nextSibling=curChild.nextSibling;var key=getNodeKey(curChild);if(key){var unmatchedFromEl=fromNodesLookup[key];if(unmatchedFromEl&&compareNodeNames(curChild,unmatchedFromEl)){curChild.parentNode.replaceChild(unmatchedFromEl,curChild);morphEl(unmatchedFromEl,curChild)}else{handleNodeAdded(curChild)}}else{handleNodeAdded(curChild)}curChild=nextSibling}}function cleanupFromEl(fromEl,curFromNodeChild,curFromNodeKey){while(curFromNodeChild){var fromNextSibling=curFromNodeChild.nextSibling;if(curFromNodeKey=getNodeKey(curFromNodeChild)){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=fromNextSibling}}function morphEl(fromEl,toEl,childrenOnly){var toElKey=getNodeKey(toEl);if(toElKey){delete fromNodesLookup[toElKey]}if(!childrenOnly){var beforeUpdateResult=onBeforeElUpdated(fromEl,toEl);if(beforeUpdateResult===false){return}else if(beforeUpdateResult instanceof HTMLElement){fromEl=beforeUpdateResult;indexTree(fromEl)}morphAttrs(fromEl,toEl);onElUpdated(fromEl);if(onBeforeElChildrenUpdated(fromEl,toEl)===false){return}}if(fromEl.nodeName!==\"TEXTAREA\"){morphChildren(fromEl,toEl)}else{specialElHandlers.TEXTAREA(fromEl,toEl)}}function morphChildren(fromEl,toEl){var skipFrom=skipFromChildren(fromEl,toEl);var curToNodeChild=toEl.firstChild;var curFromNodeChild=fromEl.firstChild;var curToNodeKey;var curFromNodeKey;var fromNextSibling;var toNextSibling;var matchingFromEl;outer:while(curToNodeChild){toNextSibling=curToNodeChild.nextSibling;curToNodeKey=getNodeKey(curToNodeChild);while(!skipFrom&&curFromNodeChild){fromNextSibling=curFromNodeChild.nextSibling;if(curToNodeChild.isSameNode&&curToNodeChild.isSameNode(curFromNodeChild)){curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling;continue outer}curFromNodeKey=getNodeKey(curFromNodeChild);var curFromNodeType=curFromNodeChild.nodeType;var isCompatible=undefined;if(curFromNodeType===curToNodeChild.nodeType){if(curFromNodeType===ELEMENT_NODE){if(curToNodeKey){if(curToNodeKey!==curFromNodeKey){if(matchingFromEl=fromNodesLookup[curToNodeKey]){if(fromNextSibling===matchingFromEl){isCompatible=false}else{fromEl.insertBefore(matchingFromEl,curFromNodeChild);if(curFromNodeKey){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=matchingFromEl;curFromNodeKey=getNodeKey(curFromNodeChild)}}else{isCompatible=false}}}else if(curFromNodeKey){isCompatible=false}isCompatible=isCompatible!==false&&compareNodeNames(curFromNodeChild,curToNodeChild);if(isCompatible){morphEl(curFromNodeChild,curToNodeChild)}}else if(curFromNodeType===TEXT_NODE||curFromNodeType==COMMENT_NODE){isCompatible=true;if(curFromNodeChild.nodeValue!==curToNodeChild.nodeValue){curFromNodeChild.nodeValue=curToNodeChild.nodeValue}}}if(isCompatible){curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling;continue outer}if(curFromNodeKey){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=fromNextSibling}if(curToNodeKey&&(matchingFromEl=fromNodesLookup[curToNodeKey])&&compareNodeNames(matchingFromEl,curToNodeChild)){if(!skipFrom){addChild(fromEl,matchingFromEl)}morphEl(matchingFromEl,curToNodeChild)}else{var onBeforeNodeAddedResult=onBeforeNodeAdded(curToNodeChild);if(onBeforeNodeAddedResult!==false){if(onBeforeNodeAddedResult){curToNodeChild=onBeforeNodeAddedResult}if(curToNodeChild.actualize){curToNodeChild=curToNodeChild.actualize(fromEl.ownerDocument||doc)}addChild(fromEl,curToNodeChild);handleNodeAdded(curToNodeChild)}}curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling}cleanupFromEl(fromEl,curFromNodeChild,curFromNodeKey);var specialElHandler=specialElHandlers[fromEl.nodeName];if(specialElHandler){specialElHandler(fromEl,toEl)}}var morphedNode=fromNode;var morphedNodeType=morphedNode.nodeType;var toNodeType=toNode.nodeType;if(!childrenOnly){if(morphedNodeType===ELEMENT_NODE){if(toNodeType===ELEMENT_NODE){if(!compareNodeNames(fromNode,toNode)){onNodeDiscarded(fromNode);morphedNode=moveChildren(fromNode,createElementNS(toNode.nodeName,toNode.namespaceURI))}}else{morphedNode=toNode}}else if(morphedNodeType===TEXT_NODE||morphedNodeType===COMMENT_NODE){if(toNodeType===morphedNodeType){if(morphedNode.nodeValue!==toNode.nodeValue){morphedNode.nodeValue=toNode.nodeValue}return morphedNode}else{morphedNode=toNode}}}if(morphedNode===toNode){onNodeDiscarded(fromNode)}else{if(toNode.isSameNode&&toNode.isSameNode(morphedNode)){return}morphEl(morphedNode,toNode,childrenOnly);if(keyedRemovalList){for(var i=0,len=keyedRemovalList.length;i<len;i++){var elToRemove=fromNodesLookup[keyedRemovalList[i]];if(elToRemove){removeNode(elToRemove,elToRemove.parentNode,false)}}}}if(!childrenOnly&&morphedNode!==fromNode&&fromNode.parentNode){if(morphedNode.actualize){morphedNode=morphedNode.actualize(fromNode.ownerDocument||doc)}fromNode.parentNode.replaceChild(morphedNode,fromNode)}return morphedNode}}var morphdom=morphdomFactory(morphAttrs);return morphdom});"

def md_to_html(text, note_tags=None):
    try:    body = markdown.markdown(text, extensions=MD_EXT, extension_configs=MD_CFG)
    except: body = markdown.markdown(text)
    # Bandeau d'étiquettes en haut si la note en a
    tags_html = ''
    if note_tags:
        chips = ''
        for t in note_tags:
            bg  = t['color']
            r,g,b = int(bg[1:3],16)/255, int(bg[3:5],16)/255, int(bg[5:7],16)/255
            fg  = '#1e1e2e' if (0.299*r+0.587*g+0.114*b) > 0.5 else '#ffffff'
            chips += ('<span style="background:' + bg + ';color:' + fg + ';'
                      'border-radius:10px;padding:2px 10px;font-size:11px;'
                      'font-weight:600;margin-right:6px;">' + t['label'] + '</span>')
        tags_html = ('<div style="padding:8px 0 12px;border-bottom:1px solid #45475a;'
                     'margin-bottom:16px;">'
                     + chips + '</div>')
    progress_css = (
        '#read-progress{'
        'position:fixed;top:0;left:0;width:0%;height:3px;'
        'background:linear-gradient(90deg,#7c8cf8,#89b4fa,#a6e3a1);'
        'z-index:9999;transition:width 0.1s ease;'
        'border-radius:0 2px 2px 0;'
        'box-shadow:0 0 8px rgba(124,140,248,0.6);}'
    )
    progress_js = (
        '<script>'
        'window.addEventListener("scroll",function(){'
        '  var h=document.body.scrollHeight-window.innerHeight;'
        '  var p=h>0?Math.round(window.scrollY/h*100):0;'
        '  var bar=document.getElementById("read-progress");'
        '  if(bar)bar.style.width=p+"%";'
        '});'
        '</script>'
    )
    return ('<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">'
            '<style>' + _make_preview_css(_CURRENT_FONT_SIZE) + progress_css + '</style>'
            '<script>' + MORPHDOM_JS + '</script>'
            '</head><body id="md-body">'
            '<div id="read-progress"></div>'
            + tags_html + body + progress_js + '</body></html>')

# ── CSS et HTML pour export impression/PDF ──────────────────────────────────


# ── Utilitaires export (PDF / ODT / LaTeX / HTML) ────────────────────────────

EMOJI_MAP = {
    '🔴': '[!!]', '🟠': '[!]',  '🟡': '[~]',  '🟢': '[ok]', '⚪': '[-]',
    '✅': '[v]',  '❌': '[x]',  '⚠️': '[!]',  '⚡': '[*]',  '🔥': '[!]',
    '✔️': '[v]',  '❗': '[!]',  '❓': '[?]',  '➡️': '->',   '⬆️': '^',
    '⬇️': 'v',   '✨': '*',    '📌': '[>]',  '📋': '[=]',  '📊': '[#]',
    '🛡️': '[s]', '🔒': '[=]',  '🔓': '[o]',  '💡': '[i]',  '🔧': '[t]',
    '📝': '[e]',  '🎯': '[*]',  '🚀': '[>]',  '⭐': '*',    '🔗': '[l]',
}

def replace_emojis(text):
    import unicodedata
    for emoji, repl in EMOJI_MAP.items():
        text = text.replace(emoji, repl)
    # Supprimer les emojis restants non mappés
    result = []
    for c in text:
        cp = ord(c)
        if cp > 0x2500:
            cat = unicodedata.category(c)
            if cat.startswith('S') or cat.startswith('C'):
                continue
        result.append(c)
    return ''.join(result)


def compute_col_widths(md_table, min_pct=10, max_pct=55):
    """
    Calcule les largeurs % de chaque colonne proportionnellement au contenu.
    Utilise sqrt(0.6*max + 0.4*avg) pour équilibrer sans qu'une colonne
    très longue ne domine trop les autres.
    """
    lines = md_table.strip().split('\n')
    data = [l for l in lines
            if l.strip() and not re.match(r'^[|\s:\-]+$', l.strip().replace('|', ''))]
    if not data:
        return []

    col_max = []
    col_vals = []
    for line in data:
        cells = [c.strip() for c in line.split('|')[1:-1]]
        clean = []
        for c in cells:
            c = re.sub(r'https?://\S+', 'URL', c)   # URLs → token court
            c = re.sub(r'[*_`\[\]!]', '', c)          # balises MD
            c = re.sub(r'[^\x00-\x7F\xc0-\xff\s\w.,;:!?+\-/()]', '', c).strip()
            clean.append(c)
        for j, c in enumerate(clean):
            if j >= len(col_max):
                col_max.append(0)
                col_vals.append([])
            col_max[j] = max(col_max[j], len(c))
            col_vals[j].append(len(c))

    if not col_max:
        return []

    col_avg = [sum(v) / len(v) if v else 0 for v in col_vals]
    scores  = [math.sqrt(0.6 * m + 0.4 * a + 1) for m, a in zip(col_max, col_avg)]
    total   = sum(scores) or 1
    pcts    = [max(min_pct, min(max_pct, int(round(s / total * 100)))) for s in scores]

    # Renormaliser à 100%
    diff = 100 - sum(pcts)
    pcts[pcts.index(max(pcts))] += diff
    return pcts

def inject_col_widths(html, md_tables):
    """
    Injecte <colgroup> dans chaque <table> du HTML généré.
    Ajoute aussi page-break-inside:avoid pour les tableaux courts (≤ 20 lignes).
    """
    idx = [0]

    def replace_table(m):
        i = idx[0]; idx[0] += 1
        table_html = m.group(0)

        # Injecter les largeurs de colonnes
        if i < len(md_tables):
            widths = compute_col_widths(md_tables[i])
            if widths:
                cols = ''.join(f'<col style="width:{w}%">' for w in widths)
                table_html = table_html.replace(
                    '<table>', f'<table><colgroup>{cols}</colgroup>', 1)

        return table_html

    return re.sub(r'<table>[\s\S]*?</table>', replace_table, html)


# ── Conversion Markdown → HTML ────────────────────────────────────────────────

def inject_col_widths_odt(content_xml, md_tables):
    """Modifie style:rel-column-width dans automatic-styles du content.xml ODT."""
    TOTAL = 65535

    # Trouver l'ordre des tableaux et leur nom
    table_names = re.findall(r'<table:table[^>]*table:name="(Table\d+)"', content_xml)

    for i, tname in enumerate(table_names):
        if i >= len(md_tables): break
        pcts = compute_col_widths(md_tables[i])
        if not pcts: continue
        for j, pct in enumerate(pcts):
            letter = chr(65 + j)
            style_name = f'{tname}.{letter}'
            rel_w = int(TOTAL * pct / 100)
            content_xml = re.sub(
                rf'(<style:style style:name="{re.escape(style_name)}"[^>]*>[^<]*<style:table-column-properties style:rel-column-width=")[^"]*("\ */>[^<]*</style:style>)',
                rf'\g<1>{rel_w}*\g<2>',
                content_xml
            )
    return content_xml


# ── Génération via pandoc ─────────────────────────────────────────────────────
# Extraire les tableaux MD pour le calcul des largeurs

PRINT_CSS = """
@page { size: A4; margin: 8mm 8mm 14mm 8mm; }
body { background: #fff; color: #1a1a1a; font-family: 'Noto Sans',sans-serif;
    font-size: 10pt; line-height: 1.5; margin: 0; padding: 0; }
h1,h2,h3,h4 { color: #1a1a1a; font-weight: bold; margin-top: 0.9em; margin-bottom: 0.3em; }
h1 { font-size: 1.7em; border-bottom: 2px solid #ccc; padding-bottom: .2em; }
h2 { font-size: 1.3em; border-bottom: 1px solid #eee; }
h3 { font-size: 1.1em; } h4 { font-size: 1.0em; }
p { margin: 0.4em 0; }
ul,ol { padding-left: 1.3em; } li { margin-bottom: 0.1em; }
code { background: #f4f4f4; color: #333; padding: 1px 3px;
    border-radius: 2px; font-family: 'DejaVu Sans Mono','Courier New',monospace; font-size: 7.5pt; }
pre { background: #f4f4f4; padding: 6px 10px; border-radius: 3px; margin: 0.4em 0;
    overflow-wrap: break-word; white-space: pre-wrap; word-break: break-all;
    font-size: 7pt; line-height: 1.3; border-left: 3px solid #ccc; }
pre code { background: none; padding: 0; font-size: 7pt; }
blockquote { border-left: 3px solid #ccc; margin: 0.5em 0;
    padding: 3px 10px; color: #555; font-style: italic; }
a { color: #2255aa; word-break: break-all; }
table { border-collapse: collapse; width: 100%; margin: 0.5em 0;
    table-layout: fixed; font-size: 7.5pt; line-height: 1.2; }
th { background: #eee; border: 1px solid #ccc; padding: 3px 5px; text-align: left;
    word-wrap: break-word; overflow-wrap: break-word; hyphens: auto; }
td { border: 1px solid #ccc; padding: 3px 5px;
    word-wrap: break-word; overflow-wrap: break-word; hyphens: auto; }
tr { page-break-inside: avoid; }
img { max-width: 100%; }
hr { border: none; border-top: 1px solid #ccc; margin: 0.8em 0; }
"""

def md_to_html_print(text):
    """HTML pour export PDF : fond blanc, sans étiquettes, sans front matter."""
    clean = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()
    # Convertir les chemins d'images absolus en URI file://
    # Markdown : ![alt](/chemin/absolu) → ![alt](file:///chemin/absolu)
    def fix_img_src(m):
        alt  = m.group(1)
        path = m.group(2)
        if path.startswith('/'):
            path = 'file://' + path
        elif path.startswith('~'):
            path = 'file://' + str(Path(path).expanduser())
        return '![' + alt + '](' + path + ')'
    clean = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', fix_img_src, clean)
    try:    body = markdown.markdown(clean, extensions=MD_EXT, extension_configs=MD_CFG)
    except: body = markdown.markdown(clean)
    return ('<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">'
            '<style>' + PRINT_CSS + '</style></head><body>' + body + '</body></html>')


# ── LanguageTool ──────────────────────────────────────────────────────────────

_RE_FENCE   = re.compile(r"(?:```|~~~)[\s\S]*?(?:```|~~~)")
# Front matter Hugo/Jekyll (--- ... ---) en début de fichier
_RE_FRONTMATTER = re.compile(r"^---\n[\s\S]*?\n---", re.MULTILINE)
_RE_INLINE  = re.compile(r"`[^`\n]+`")
_RE_BOLD    = re.compile(r"(\*\*|__)(.+?)(\1)")
_RE_ITALIC  = re.compile(r"(?<!\*)(\*)(?!\*)(.+?)(?<!\*)\1(?!\*)|(?<!_)(_)(?!_)(.+?)(?<!_)\3(?!_)")
_RE_HEADING = re.compile(r"^(#{1,6} )", re.MULTILINE)
_RE_IMG     = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK    = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_TABLE   = re.compile(r"^\|.*\|[ \t]*$", re.MULTILINE)
_RE_TBLSEP  = re.compile(r"^[ \t]*[\|:\-]+[ \t]*$", re.MULTILINE)
_RE_QUOTE   = re.compile(r"^> ?", re.MULTILINE)
_RE_HR      = re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.MULTILINE)
_RE_LI      = re.compile(r"^[ \t]*([-*+]|\d+\.)( )", re.MULTILINE)

def build_plain_and_map(md_text):
    code_zones=[]; erase_zones=[]
    def in_code(s): return any(cs<=s<ce for cs,ce in code_zones)
    for m in _RE_FENCE.finditer(md_text): code_zones.append((m.start(),m.end()))
    # Front matter YAML (Hugo/Jekyll) → exclure du check LT
    for m in _RE_FRONTMATTER.finditer(md_text):
        if m.start() == 0:  # seulement en début de fichier
            code_zones.append((m.start(), m.end()))
    for m in _RE_INLINE.finditer(md_text):
        if not in_code(m.start()): code_zones.append((m.start(),m.end()))
    def add_erase(s,e):
        if not in_code(s): erase_zones.append((s,e))
    for m in _RE_HEADING.finditer(md_text): add_erase(m.start(1),m.end(1))
    for m in _RE_BOLD.finditer(md_text): add_erase(m.start(1),m.end(1)); add_erase(m.start(3),m.end(3))
    for m in _RE_ITALIC.finditer(md_text): add_erase(m.start(),m.start()+1); add_erase(m.end()-1,m.end())
    for m in _RE_IMG.finditer(md_text): add_erase(m.start(),m.end())
    for m in _RE_LINK.finditer(md_text):
        add_erase(m.start(),m.start()+1); add_erase(m.end(1),m.end(1)+1); add_erase(m.end(1)+1,m.end())
    for m in _RE_TABLE.finditer(md_text): add_erase(m.start(),m.end())
    for m in _RE_TBLSEP.finditer(md_text): add_erase(m.start(),m.end())
    for m in _RE_QUOTE.finditer(md_text): add_erase(m.start(),m.end())
    for m in _RE_HR.finditer(md_text): add_erase(m.start(),m.end())
    for m in _RE_LI.finditer(md_text): add_erase(m.start(1),m.end(1))
    all_zones=[(s,e,'code') for s,e in code_zones]+[(s,e,'erase') for s,e in erase_zones]
    all_zones.sort(key=lambda z:z[0])
    merged=[]
    for z in all_zones:
        if merged and z[0]<merged[-1][1]: merged[-1]=(merged[-1][0],max(merged[-1][1],z[1]),merged[-1][2])
        else: merged.append(list(z))
    plain_chars=[]; offset_map=[]; skip_plain=[]
    pos=0
    for s,e,kind in merged:
        for i in range(pos,s): plain_chars.append(md_text[i]); offset_map.append(i)
        if kind=='code':
            p=len(plain_chars); plain_chars.append('X'); offset_map.append(s); skip_plain.append((p,p+1))
        pos=max(pos,e)
    for i in range(pos,len(md_text)): plain_chars.append(md_text[i]); offset_map.append(i)
    return ''.join(plain_chars), offset_map, skip_plain

def plain_to_md_range(plain_offset, plain_length, offset_map, md_text):
    if plain_offset >= len(offset_map): return None
    md_start = offset_map[plain_offset]
    plain_end = plain_offset + plain_length - 1
    if plain_end >= len(offset_map): plain_end = len(offset_map)-1
    if plain_end+1 < len(offset_map): md_end_real = offset_map[plain_end+1]
    else: md_end_real = len(md_text)
    return md_start, md_end_real - md_start

def _build_plain_and_map(md):
    """Texte pur sans balises MD. md_offsets[i] = position dans md du char i de plain.
    Les lignes sautées (tableaux, séparateurs) sont remplacées par \n."""
    import re as _r
    plain_chars = []; md_offsets = []; i = 0; n = len(md)
    while i < n:
        # Blocs de code fencés → remplacer par autant de \n
        if (i == 0 or md[i-1] == '\n') and md[i:i+3] in ('```', '~~~'):
            fence = md[i:i+3]
            end = md.find('\n' + fence, i + 3)
            end = (end + 1 + 3) if end != -1 else n
            for j, c in enumerate(md[i:end]):
                if c == '\n': plain_chars.append('\n'); md_offsets.append(i + j)
            i = end
            if i < n and md[i] == '\n': plain_chars.append('\n'); md_offsets.append(i); i += 1
            continue
        # Début de ligne
        if i == 0 or md[i-1] == '\n':
            m = _r.match(r'#{1,6} ', md[i:])
            if m: i += len(m.group()); continue
            m = _r.match(r'[ \t]*(?:[-*+]|\d+\.)\s', md[i:])
            if m: i += len(m.group()); continue
            if md[i] == '>':
                m = _r.match(r'> ?', md[i:])
                if m: i += len(m.group()); continue
            if md[i] == '|':
                nl = md.find('\n', i)
                if nl != -1: plain_chars.append('\n'); md_offsets.append(nl); i = nl + 1
                else: i = n
                continue
            m = _r.match(r'[-*_]{3,}[ \t]*(?:\n|$)', md[i:])
            if m:
                nl = md.find('\n', i)
                if nl != -1: plain_chars.append('\n'); md_offsets.append(nl); i = nl + 1
                else: i += len(m.group())
                continue
        # Inline
        if md[i:i+2] in ('**', '__', '~~'): i += 2; continue
        if md[i] in ('*', '_'):
            prev = md[i-1] if i > 0 else ' '; nxt = md[i+1] if i+1 < n else ' '
            if prev in ' \n\t*_([' or nxt in ' \n\t*_)]': i += 1; continue
        if md[i] == '!' and i+1 < n and md[i+1] == '[':
            eb = md.find(']', i+2)
            if eb != -1 and eb+1 < n and md[eb+1] == '(':
                ep = md.find(')', eb+2)
                if ep != -1: i = ep + 1; continue
        if md[i] == '[':
            eb = md.find(']', i+1)
            if eb != -1 and eb+1 < n and md[eb+1] == '(':
                ep = md.find(')', eb+2)
                if ep != -1:
                    for k in range(i+1, eb): plain_chars.append(md[k]); md_offsets.append(k)
                    i = ep + 1; continue
        if md[i] == '`':
            end = md.find('`', i+1)
            if end != -1: i = end + 1; continue
        if md[i:i+4] in ('http', 'ftp:'):
            m = _r.match(r'https?://\S+|ftp://\S+', md[i:])
            if m: i += len(m.group()); continue
        if ord(md[i]) > 0x2500: i += 1; continue
        plain_chars.append(md[i]); md_offsets.append(i); i += 1
    md_offsets.append(n)
    return ''.join(plain_chars), md_offsets


def check_languagetool(text, language=LT_LANGUAGE):
    if not text.strip(): return [], {}
    plain, md_offsets = _build_plain_and_map(text)
    if not plain.strip(): return [], {}
    data = urllib.parse.urlencode({"text": plain, "language": language}).encode()
    req = urllib.request.Request(LT_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Connection": "close"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode()).get("matches", [])
    except Exception as ex:
        return [{"_error": str(ex)}], {}
    result = []
    for m in raw:
        lo = m.get("offset", 0); ll = m.get("length", 1)
        if lo + ll - 1 >= len(md_offsets): continue
        md_s = md_offsets[lo]
        md_e = md_offsets[lo + ll - 1] + 1
        if md_e <= md_s: continue
        m2 = dict(m); m2["offset"] = md_s; m2["length"] = md_e - md_s
        result.append(m2)
    return result, {}





# ── Dialogue paramètres ───────────────────────────────────────────────────────

class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, config):
        super().__init__(title="Parametres", transient_for=parent, modal=True)
        self.set_default_size(520, 380)
        self._config = dict(config)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16); box.set_margin_bottom(16)
        box.set_margin_start(16); box.set_margin_end(16)
        dir_lbl = Gtk.Label(label="Repertoire des notes :"); dir_lbl.set_xalign(0)
        box.append(dir_lbl)
        dir_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._dir_entry = Gtk.Entry()
        self._dir_entry.set_text(config.get("notes_dir", ""))
        self._dir_entry.set_hexpand(True); dir_row.append(self._dir_entry)
        btn_browse = Gtk.Button(label="Parcourir...")
        btn_browse.connect("clicked", self._on_browse); dir_row.append(btn_browse)
        box.append(dir_row)
        # Police de caractères
        fam_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fam_lbl = Gtk.Label(label="Police :")
        fam_lbl.set_xalign(0); fam_lbl.set_width_chars(16); fam_row.append(fam_lbl)
        font_families = [
            "JetBrains Mono", "Fira Code", "Cascadia Code", "Source Code Pro",
            "Ubuntu Mono", "DejaVu Sans Mono", "Monospace",
            "Inter", "Ubuntu", "Cantarell", "DejaVu Sans", "Sans"
        ]
        self._font_combo = Gtk.DropDown.new_from_strings(font_families)
        cur_fam = config.get("font_family", "Noto Sans")
        if cur_fam in font_families:
            self._font_combo.set_selected(font_families.index(cur_fam))
        self._font_combo.set_hexpand(True)
        fam_row.append(self._font_combo)
        box.append(fam_row)

        # Taille de police
        font_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        font_lbl = Gtk.Label(label="Taille :")
        font_lbl.set_xalign(0); font_lbl.set_width_chars(16); font_row.append(font_lbl)
        self._font_spin = Gtk.SpinButton.new_with_range(8, 32, 1)
        self._font_spin.set_value(config.get("font_size", 14))
        self._font_spin.set_width_chars(4)
        font_row.append(self._font_spin)
        font_row.append(Gtk.Label(label="px"))
        box.append(font_row)

        lang_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lang_lbl = Gtk.Label(label="Langue LanguageTool :"); lang_lbl.set_xalign(0)
        lang_row.append(lang_lbl)
        langs = ["fr","fr-FR","en-US","en-GB","de-DE","es","it","auto"]
        self._lang_combo = Gtk.DropDown.new_from_strings(langs)
        cur = config.get("lt_language","fr")
        if cur in langs: self._lang_combo.set_selected(langs.index(cur))
        lang_row.append(self._lang_combo); box.append(lang_row)
        # Séparateur
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # À propos
        about = Gtk.Label()
        about.set_markup(
            '<span size="small" foreground="#9090a8">'
            '📝  <span foreground="#7c8cf8"><b>md-editor</b></span>'
            ' <span foreground="#5a5a7a">v0.5</span>'
            ' — Éditeur de notes Markdown avec étiquettes, LanguageTool,\n'
            '    export <span foreground="#f2c94c">PDF</span>'
            ' · <span foreground="#6fcf97">ODT</span>'
            ' · <span foreground="#bb86fc">LaTeX</span>'
            ' et publication Hugo.\n'
            '    Interface GTK4 · Python 3 · SQLite · WebKit · Cairo\n'
            '\n'
            '🤖  Développeur principal : <span foreground="#7c8cf8"><b>Claude</b></span>'
            ' <span foreground="#5a5a7a">(Anthropic)</span>\n'
            '🏛️  Architecte : <span foreground="#f2c94c"><b>Nitrix</b></span>'
            '</span>'
        )
        about.set_xalign(0)
        about.set_wrap(True)
        about.set_margin_top(4)
        box.append(about)

        self.get_content_area().append(box)
        self.add_button("Annuler", Gtk.ResponseType.CANCEL)
        self.add_button("Enregistrer", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

    def _on_browse(self, _):
        dialog = Gtk.FileDialog(); dialog.set_title("Choisir le repertoire")
        dialog.select_folder(self.get_transient_for(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder: self._dir_entry.set_text(folder.get_path())
        except Exception: pass

    def get_config(self):
        langs = ["fr","fr-FR","en-US","en-GB","de-DE","es","it","auto"]
        self._config["notes_dir"]   = self._dir_entry.get_text().strip()
        self._config["lt_language"] = langs[min(self._lang_combo.get_selected(), len(langs)-1)]
        self._config["font_size"]   = int(self._font_spin.get_value())
        font_families = [
            "JetBrains Mono", "Fira Code", "Cascadia Code", "Source Code Pro",
            "Ubuntu Mono", "DejaVu Sans Mono", "Monospace",
            "Inter", "Ubuntu", "Cantarell", "DejaVu Sans", "Sans"
        ]
        idx = min(self._font_combo.get_selected(), len(font_families)-1)
        self._config["font_family"] = font_families[idx]
        return self._config

# ── Fenêtre principale ────────────────────────────────────────────────────────

class MdEditorWindow(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="md-editor")
        self._config          = load_config()
        self._db              = NotesDB()
        self._current_file    = None
        self._preview_pending         = False
        self._lt_pending              = False
        self._syn_pending             = False
        self._preview_scroll_fraction  = 0.0
        self._preview_anchor           = None
        self._preview_loaded           = False
        self._preview_scroll_sync      = True
        self._calendar_date_filter     = None
        self._calendar_date_files      = []
        self._unsaved_files            = set()   # fichiers modifiés non sauvegardés
        self._unsaved_contents         = {}      # chemin → contenu en mémoire
        self._loading_file             = False   # True pendant set_text → ignorer changed
        self._lt_enabled      = self._config.get("lt_enabled", False)
        self._lt_matches      = []
        self._lt_language     = self._config.get("lt_language", LT_LANGUAGE)
        self._lt_popover      = None
        self._outside_gc      = None
        self._known_files     = set()
        self._scan_source_id  = None
        self._paned_ready     = False
        self._active_tag_filter = set()   # tag_ids filtre actif

        # Taille fenêtre
        w = self._config.get("win_width", 1500)
        h = self._config.get("win_height", 860)
        self.set_default_size(w, h)
        # Icône de l'application
        self.set_icon_name("md-editor")

        self._build_ui()
        self._apply_css()
        self._btn_lt.set_active(self._lt_enabled)
        GLib.idle_add(self._apply_font_size, self._config.get("font_size", 14))
        # Ouvrir le dernier fichier édité, sinon ne rien afficher
        GLib.idle_add(self._open_last_file)
        self._start_scan()

    def _open_last_file(self):
        """Ouvre le dernier fichier édité, ou ne rien afficher si aucun."""
        last = self._config.get('last_file')
        if last:
            p = Path(last)
            if p.exists():
                try:
                    content = self._read_note(p)
                    self._loading_file = True
                    self._buffer.set_text(content)
                    self._loading_file = False
                    self._current_file = p
                    self._preview_loaded = False
                    GLib.idle_add(self._refresh_preview)
                    GLib.idle_add(self._apply_syntax_highlight)

                    self._update_header_note_title()
                    self._set_status('Dernier fichier : ' + p.name, 'ok')
                    return False
                except Exception:
                    pass
        # Aucun dernier fichier : buffer vide, preview vide
        self._loading_file = True
        self._buffer.set_text('')
        self._loading_file = False
        self._current_file = None
        self._webview.load_html(
            '<!DOCTYPE html><html><head><meta charset="UTF-8">'
            '<style>body{background:#0d0d14;margin:0;padding:0;}</style>'
            '</head><body></body></html>', 'file:///')
        return False

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # HeaderBar
        header = Gtk.HeaderBar()
        # Avatar tout à droite = PREMIER pack_end
        avatar_widget = self._build_avatar()
        header.pack_end(avatar_widget)
        header.set_show_title_buttons(True)
        # Titre via widget custom pour afficher le nom de la note
        self.set_title("md-editor")
        self._header_subtitle = Gtk.Label(label='md-editor v0.5')
        self._header_subtitle.add_css_class('header-note-title')
        self._header_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self._header_subtitle.set_max_width_chars(45)
        header.set_title_widget(self._header_subtitle)

        for icon, tip, cb in [
            ("document-open-symbolic",    "Ouvrir",           self._on_open),
            ("document-save-symbolic",    "Enregistrer",      self._on_save),
            ("document-save-as-symbolic", "Sauver dans notes",self._on_save_to_notes),
        ]:
            b = Gtk.Button(icon_name=icon); b.set_tooltip_text(tip)
            b.connect("clicked", cb); header.pack_start(b)

        self._btn_lt = Gtk.ToggleButton(icon_name='document-edit-symbolic')
        self._btn_lt.set_tooltip_text('LanguageTool — vérification orthographique')
        # css class ajoutée dynamiquement via _on_lt_toggle
        self._btn_lt.connect("toggled", self._on_lt_toggle)
        header.pack_end(self._btn_lt)
        btn_tags_panel = Gtk.ToggleButton(icon_name='tag-symbolic')
        btn_tags_panel.set_tooltip_text('Afficher/masquer le panneau étiquettes')
        btn_tags_panel.set_active(True)
        btn_tags_panel.connect('toggled', self._on_toggle_tags_panel)
        header.pack_end(btn_tags_panel)
        self._btn_focus = Gtk.ToggleButton(icon_name='view-fullscreen-symbolic')
        self._btn_focus.set_tooltip_text('Mode focus : éditeur + preview uniquement')
        self._btn_focus.set_active(False)
        self._btn_focus.connect('toggled', self._on_toggle_focus_mode)
        header.pack_end(self._btn_focus)

        # Bouton sync SCP → popover Backup / Restaurer
        self._btn_sync = Gtk.MenuButton(icon_name='emblem-synchronizing-symbolic')
        self._btn_sync.set_tooltip_text('Synchronisation SCP')

        self._sync_popover = Gtk.Popover()
        self._sync_popover.set_has_arrow(True)
        _sync_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        _sync_vbox.set_margin_start(4); _sync_vbox.set_margin_end(4)
        _sync_vbox.set_margin_top(4); _sync_vbox.set_margin_bottom(4)
        _btn_backup = Gtk.Button(label='☁  Sauvegarder (backup)')
        _btn_backup.add_css_class('flat')
        _btn_backup.connect('clicked', self._on_sync_popup_backup)
        _sync_vbox.append(_btn_backup)
        _btn_restore = Gtk.Button(label='⬇  Restaurer depuis le serveur')
        _btn_restore.add_css_class('flat')
        _btn_restore.connect('clicked', self._on_sync_popup_restore)
        _sync_vbox.append(_btn_restore)
        _sync_vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        _btn_cfg = Gtk.Button(label='⚙  Configurer Backup et chiffrement...')
        _btn_cfg.add_css_class('flat')
        _btn_cfg.connect('clicked', self._on_sync_popup_config)
        _sync_vbox.append(_btn_cfg)
        self._sync_popover.set_child(_sync_vbox)
        self._btn_sync.set_popover(self._sync_popover)
        header.pack_end(self._btn_sync)


        langs = ["fr","fr-FR","en-US","en-GB","de-DE","es","it","auto"]
        self._lang_combo = Gtk.DropDown.new_from_strings(langs)
        idx = langs.index(self._lt_language) if self._lt_language in langs else 0
        self._lang_combo.set_selected(idx)
        self._lang_combo.connect("notify::selected", self._on_lang_changed)
        header.pack_end(self._lang_combo)

        btn_cal = Gtk.Button(icon_name="x-office-calendar-symbolic")
        btn_cal.set_tooltip_text("Calendrier des notes")
        btn_cal.connect("clicked", self._on_show_calendar); header.pack_end(btn_cal)

        btn_zip = Gtk.Button(icon_name="package-x-generic-symbolic")
        btn_zip.set_tooltip_text("Exporter toutes les notes en ZIP")
        btn_zip.connect("clicked", self._on_export_zip); header.pack_end(btn_zip)

        btn_export = Gtk.Button(icon_name="document-save-as-symbolic")
        btn_export.set_tooltip_text("Exporter la note (PDF, ODT, LaTeX)")
        btn_export.connect("clicked", self._on_show_export_menu)
        header.pack_end(btn_export)

        btn_fav = Gtk.Button(icon_name="starred-symbolic")
        btn_fav.set_tooltip_text("Voir les favoris")
        btn_fav.connect("clicked", self._on_show_favorites); header.pack_end(btn_fav)

        btn_trash = Gtk.Button(icon_name="user-trash-symbolic")
        btn_trash.set_tooltip_text("Corbeille")
        btn_trash.connect("clicked", self._on_show_trash); header.pack_end(btn_trash)

        btn_settings = Gtk.Button(icon_name="preferences-system-symbolic")
        btn_settings.set_tooltip_text("Parametres")
        btn_settings.connect("clicked", self._on_settings); header.pack_end(btn_settings)
        self.set_titlebar(header)

        # Layout : fichiers(15%) | éditeur(75%) | preview | étiquettes(droite)
        # Structure : _outer_paned [ _files_paned [ fichiers | _edit_paned ] | étiquettes ]
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        # Paned externe : zone principale | panneau étiquettes
        self._outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned.set_wide_handle(True)
        self._outer_paned.set_vexpand(True)
        root.append(self._outer_paned)

        # Barre de statut
        self._status_bar = Gtk.Label(label="Pret")
        self._status_bar.set_xalign(0); self._status_bar.add_css_class("status-bar")
        root.append(self._status_bar)

        # Paned principal : fichiers | edit_paned
        self._main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._main_paned.set_wide_handle(True); self._main_paned.set_hexpand(True)

        # ── Panneau fichiers (gauche) ──────────────────────────────────────
        self._files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        files_box = self._files_box

        fb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        fb.add_css_class("panel-bar")
        fl = Gtk.Label(label="  Notes"); fl.add_css_class("panel-label-files")
        fl.set_xalign(0); fl.set_hexpand(True); fb.append(fl)
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        btn_refresh.add_css_class("panel-icon-btn")
        btn_refresh.set_tooltip_text("Rafraichir")
        btn_refresh.connect("clicked", lambda _: self._refresh_file_list())
        fb.append(btn_refresh)
        btn_gsearch_toggle = Gtk.Button(icon_name="system-search-symbolic")
        btn_gsearch_toggle.add_css_class("panel-icon-btn")
        btn_gsearch_toggle.set_tooltip_text("Rechercher dans toutes les notes")
        btn_gsearch_toggle.connect("clicked", self._on_global_search_toggle)
        fb.append(btn_gsearch_toggle)
        btn_collapse = Gtk.Button(icon_name="pan-end-symbolic")
        btn_collapse.add_css_class("panel-icon-btn")
        btn_collapse.set_tooltip_text("Replier tous les groupes")
        btn_collapse.connect("clicked", lambda _: self._tree_view.collapse_all())
        fb.append(btn_collapse)
        files_box.append(fb)
        files_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        dir_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        _dir_ico = Gtk.Image.new_from_icon_name('folder-symbolic')
        _dir_ico.set_pixel_size(12)
        _dir_ico.add_css_class('dir-label')
        dir_row.append(_dir_ico)
        self._dir_lbl = Gtk.Label(label=self._config.get("notes_dir","—"))
        self._dir_lbl.add_css_class("dir-label"); self._dir_lbl.set_xalign(0)
        dir_row.append(self._dir_lbl)
        dir_row.set_margin_start(8); dir_row.set_margin_end(4)
        dir_row.set_margin_top(4); dir_row.set_margin_bottom(4)
        files_box.append(dir_row)
        files_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        files_scroll = Gtk.ScrolledWindow(); files_scroll.set_vexpand(True)

        # TreeStore : col 0 = nom, col 1 = chemin, col 2 = est_fichier,
        #             col 3 = date, col 4 = is_new, col 5 = couleur groupe
        self._tree_store = Gtk.TreeStore(str, str, bool, str, bool, str)
        self._tree_view  = Gtk.TreeView(model=self._tree_store)
        self._tree_view.set_headers_visible(False)
        self._tree_view.set_activate_on_single_click(True)
        self._tree_view.set_level_indentation(8)
        self._tree_view.add_css_class("files-listbox")
        self._tree_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        # Colonne unique - texte seul, chip simulée via Pango markup
        col = Gtk.TreeViewColumn()
        r_text = Gtk.CellRendererText()
        r_text.set_property('ellipsize', Pango.EllipsizeMode.END)
        r_text.set_property('xpad', 4)
        r_text.set_property('ypad', 4)
        col.pack_start(r_text, True)
        col.set_cell_data_func(r_text, self._tree_cell_data)
        self._tree_view.append_column(col)

        self._tree_view.connect("row-activated", self._on_tree_activated)
        # Détection du double-clic via GestureClick bouton=1
        self._last_click_path = None
        self._last_click_time = 0
        gc_dbl = Gtk.GestureClick()
        gc_dbl.set_button(1)
        gc_dbl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gc_dbl.connect("pressed", self._on_tree_btn_pressed)
        self._tree_view.add_controller(gc_dbl)

        # Clic droit → menu étiquettes
        gc_files = Gtk.GestureClick(); gc_files.set_button(3)
        gc_files.connect("pressed", self._on_file_right_click)
        self._tree_view.add_controller(gc_files)


        files_scroll.set_child(self._tree_view); files_box.append(files_scroll)

        # Barre de recherche globale (dans toutes les notes)
        self._global_search_bar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._global_search_bar.set_visible(False)

        gse_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gse_box.set_margin_start(6); gse_box.set_margin_end(6)
        gse_box.set_margin_top(4); gse_box.set_margin_bottom(4)
        self._global_search_entry = Gtk.SearchEntry()
        self._global_search_entry.set_hexpand(True)
        self._global_search_entry.set_placeholder_text("Rechercher dans toutes les notes...")
        self._global_search_entry.connect("activate", self._on_global_search)
        self._global_search_entry.connect("search-changed", self._on_global_search)
        gse_box.append(self._global_search_entry)
        btn_gsearch = Gtk.Button(icon_name="window-close-symbolic")
        btn_gsearch.add_css_class("panel-icon-btn")
        btn_gsearch.connect("clicked", self._on_global_search_close)
        gse_box.append(btn_gsearch)
        self._global_search_bar.append(gse_box)

        self._global_search_results = Gtk.ListBox()
        self._global_search_results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._global_search_results.add_css_class("files-listbox")
        self._global_search_results.connect("row-activated",
            self._on_global_search_result_activated)
        gsr_scroll = Gtk.ScrolledWindow(); gsr_scroll.set_vexpand(True)
        gsr_scroll.set_child(self._global_search_results)
        self._global_search_bar.append(gsr_scroll)
        files_box.append(self._global_search_bar)

        btn_new = Gtk.Button(); btn_new.add_css_class("new-note-btn")
        btn_new.connect("clicked", self._on_new_note)
        btn_new_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_new_box.set_halign(Gtk.Align.CENTER)
        _ico_note = Gtk.Image.new_from_icon_name('document-new-symbolic')
        _ico_note.set_pixel_size(14)
        btn_new_box.append(_ico_note)
        btn_new_box.append(Gtk.Label(label='Nouvelle note'))
        btn_new.set_child(btn_new_box)
        files_box.append(btn_new)

        self._main_paned.set_start_child(files_box)
        self._main_paned.set_shrink_start_child(False)

        # ── Paned éditeur/preview ──────────────────────────────────────────
        self._edit_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._edit_paned.set_wide_handle(True); self._edit_paned.set_hexpand(True)

        # Éditeur
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL); lb.add_css_class("panel-bar")
        ll = Gtk.Label(label="  Markdown"); ll.add_css_class("panel-label-edit"); ll.set_xalign(0)
        ll.set_hexpand(True); lb.append(ll)
        btn_toc = Gtk.Button(label="ToC")
        btn_toc.add_css_class("panel-icon-btn")
        btn_toc.set_tooltip_text("Table des matieres")
        btn_toc.connect("clicked", self._on_show_toc)
        lb.append(btn_toc)
        btn_note_menu = Gtk.Button(icon_name='open-menu-symbolic')
        btn_note_menu.add_css_class('panel-icon-btn')
        btn_note_menu.set_tooltip_text('Actions sur la note courante')
        btn_note_menu.connect('clicked', self._on_show_note_menu)
        lb.append(btn_note_menu)
        self._btn_md_help = Gtk.Button(label='?')
        self._btn_md_help.add_css_class('panel-icon-btn')
        self._btn_md_help.set_tooltip_text('Aide Markdown')
        self._btn_md_help.connect('clicked', self._on_toggle_md_help)
        lb.append(self._btn_md_help)
        btn_copy_menu = Gtk.Button(icon_name='edit-copy-symbolic')
        btn_copy_menu.add_css_class('panel-icon-btn')
        btn_copy_menu.set_tooltip_text('Copier la note')
        btn_copy_menu.connect('clicked', self._on_show_copy_menu)
        lb.append(btn_copy_menu)

        self._btn_gutter = Gtk.ToggleButton()
        self._btn_gutter.set_icon_name("view-list-symbolic")
        self._btn_gutter.add_css_class("panel-icon-btn")
        self._btn_gutter.set_tooltip_text("Afficher/masquer les numéros de ligne")
        self._btn_gutter.set_active(True)
        self._btn_gutter.connect("toggled", self._on_toggle_gutter)
        lb.append(self._btn_gutter)
        btn_tb = Gtk.ToggleButton()
        btn_tb.set_icon_name('format-text-rich-symbolic')
        btn_tb.add_css_class('panel-icon-btn')
        btn_tb.set_tooltip_text("Afficher/masquer la barre d'outils Markdown")
        btn_tb.set_active(True)
        btn_tb.connect('toggled', lambda b: self._md_toolbar_wrap.set_visible(b.get_active()))
        lb.append(btn_tb)
        left_box.append(lb)
        left_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Barre d'outils Markdown ────────────────────────────────────
        self._md_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        self._md_toolbar.add_css_class('md-toolbar')
        self._md_toolbar.set_margin_start(6); self._md_toolbar.set_margin_end(6)
        self._md_toolbar.set_margin_top(3); self._md_toolbar.set_margin_bottom(3)

        def _tb(label_or_icon, tip, cb, icon=True):
            b = Gtk.Button(); b.add_css_class('md-tool-btn'); b.set_tooltip_text(tip)
            if icon:
                try: b.set_icon_name(label_or_icon)
                except: b.set_label(label_or_icon)
            else: b.set_label(label_or_icon)
            b.connect('clicked', cb); return b

        def _tbsep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(3); s.set_margin_end(3)
            s.set_margin_top(4); s.set_margin_bottom(4); return s

        for w in [
            _tb('format-text-bold-symbolic',          'Gras (Ctrl+B)',          self._md_bold,        True),
            _tb('format-text-italic-symbolic',        'Italique (Ctrl+I)',       self._md_italic,      True),
            _tb('format-text-strikethrough-symbolic', 'Barré (~~)',              self._md_strike,      True),
            _tb('`·`',                               'Code inline',             self._md_code_inline, False),
            _tbsep(),
            _tb('H1', 'Titre H1 (#)',                  self._md_h1,   False),
            _tb('H2', 'Titre H2 (##)',                 self._md_h2,   False),
            _tb('H3', 'Titre H3 (###)',                self._md_h3,   False),
            _tbsep(),
            _tb('view-list-bullet-symbolic',  'Liste à puces (-)',      self._md_ul,       True),
            _tb('view-list-ordered-symbolic', 'Liste numérotée (1.)',   self._md_ol,       True),
            _tb('emblem-ok-symbolic',         'Case à cocher (- [ ])',  self._md_checkbox, True),
            _tbsep(),
            _tb('insert-link-symbolic',        'Lien [texte](url)',      self._md_link,       True),
            _tb('insert-image-symbolic',       'Image ![alt](url)',      self._md_image,      True),
            _tb('utilities-terminal-symbolic', 'Bloc de code (```)',     self._md_code_block, True),
            _tb('mail-forward-symbolic',       'Citation (>)',           self._md_quote,      True),
            _tb('—',                           'Règle horizontale (---)',self._md_hr,         False),
            _tbsep(),
            _tb('x-office-spreadsheet-symbolic','Tableau Markdown',      self._md_table,      True),
            _tbsep(),
            _tb('insert-image-symbolic', 'Insérer image du disque (copie dans data/)', self._md_insert_image_file, True),
        ]:
            self._md_toolbar.append(w)

        self._md_toolbar_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._md_toolbar_wrap.append(self._md_toolbar)
        self._md_toolbar_wrap.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        left_box.append(self._md_toolbar_wrap)

        se = Gtk.ScrolledWindow(); se.set_vexpand(True); se.set_hexpand(True)

        # Conteneur horizontal : gouttière numéros | éditeur
        editor_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # ── Gouttière numéros de lignes ───────────────────────────────
        self._gutter = Gtk.DrawingArea()
        self._gutter.set_content_width(44)
        self._gutter.add_css_class('line-gutter')
        self._gutter.set_draw_func(self._draw_gutter)
        editor_hbox.append(self._gutter)

        # Séparateur visuel gutter/éditeur
        sep_gutter = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep_gutter.add_css_class('gutter-sep')
        editor_hbox.append(sep_gutter)

        self._view = Gtk.TextView()
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_left_margin(14); self._view.set_right_margin(12)
        self._view.set_top_margin(10); self._view.set_bottom_margin(10)
        self._view.set_hexpand(True)
        self._view.add_css_class("editor-view")
        self._buffer = self._view.get_buffer()
        self._tag_error   = self._buffer.create_tag("lt_error",
            underline=Pango.Underline.ERROR, underline_rgba=Gdk.RGBA(.9,.3,.3,1),
            background='#3a1a2a', foreground='#ffffff')
        self._tag_warning = self._buffer.create_tag("lt_warning",
            underline=Pango.Underline.ERROR, underline_rgba=Gdk.RGBA(.95,.75,.1,1),
            background='#2e2a10', foreground='#ffffff')
        self._tag_style   = self._buffer.create_tag("lt_style",
            underline=Pango.Underline.ERROR, underline_rgba=Gdk.RGBA(.3,.7,1,1),
            background='#0e2030', foreground='#ffffff')
        # ── Tags de coloration syntaxique Markdown ──────────────────────
        # Note : pas de 'scale' ni 'paragraph_background' — ignorés en GTK4
        def mk(name, **kw): return self._buffer.create_tag(name, **kw)
        W = Pango.Weight; S = Pango.Style
        self._syn_tags = {
            # Couleur + gras sur toute la ligne (incluant le marqueur #)
            'h1':          mk('syn_h1', foreground='#89b4fa', weight=W.ULTRABOLD),
            'h2':          mk('syn_h2', foreground='#89b4fa', weight=W.BOLD),
            'h3':          mk('syn_h3', foreground='#89b4fa', weight=W.BOLD),
            'h4':          mk('syn_h4', foreground='#89b4fa', weight=W.SEMIBOLD),
            'bold':        mk('syn_bold',   foreground='#f9e2af', weight=W.BOLD),
            'italic':      mk('syn_italic', foreground='#f5c2e7', style=S.ITALIC),
            'code':        mk('syn_code',   foreground='#a6e3a1',
                               family='Monospace', background='#2a2a3e'),
            'fence':       mk('syn_fence',  foreground='#a6e3a1',
                               family='Monospace', background='#1a1a2e'),
            'marker':      mk('syn_marker', foreground='#45475a'),
            'quote':       mk('syn_quote',  foreground='#a6adc8', style=S.ITALIC),
            'link':        mk('syn_link',   foreground='#89dceb',
                               underline=Pango.Underline.SINGLE),
            'hr':          mk('syn_hr',     foreground='#45475a'),
            'strike':      mk('syn_strike', foreground='#a6adc8',
                               strikethrough=True),
            'img':         mk('syn_img',    foreground='#f2c94c'),
            'frontmatter': mk('syn_frontmatter', foreground='#cba6f7',
                               family='Monospace', background='#252535'),
        }
        self._syn_pending = False
        self._tag_search_hl = self._buffer.create_tag(
            "search_match", background="#f9e2af", foreground="#1e1e2e")
        self._search_matches = []
        self._search_current = -1
        self._buffer.connect("changed", self._on_text_changed)
        self._buffer.connect("notify::cursor-position", self._on_cursor_moved)
        self._buffer.connect("notify::cursor-position", lambda *_: self._update_page_info())
        self._buffer.connect("notify::cursor-position", self._on_cursor_for_gutter)
        self._buffer.connect("changed", lambda _: self._gutter.queue_draw())
        # Sauvegarder le scroll avant focus + restaurer après
        self._scroll_before_focus = None
        gc_focus_save = Gtk.GestureClick()
        gc_focus_save.set_button(0)  # tous les boutons
        gc_focus_save.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gc_focus_save.connect('pressed', self._on_editor_click_capture)
        self._view.add_controller(gc_focus_save)
        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect('enter', self._on_editor_focus_in)
        self._view.add_controller(focus_ctrl)
        gc = Gtk.GestureClick(); gc.set_button(1)
        gc.connect("pressed", self._on_left_click); self._view.add_controller(gc)
        # Clic droit éditeur → menu templates
        gc_right = Gtk.GestureClick(); gc_right.set_button(2)  # bouton milieu
        gc_right.connect("pressed", self._on_editor_right_click)
        self._view.add_controller(gc_right)
        mc = Gtk.EventControllerMotion()
        mc.connect("motion", self._on_motion); self._view.add_controller(mc)
        editor_hbox.append(self._view)
        se.set_child(editor_hbox); left_box.append(se)
        self._scroll_edit = se
        # Capturer les touches de navigation sur le ScrolledWindow
        nav_ctrl = Gtk.EventControllerKey()
        nav_ctrl.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        nav_ctrl.connect('key-released', self._on_nav_key_released)
        se.add_controller(nav_ctrl)

        # ── Barre de recherche (masquée par défaut) ───────────────────
        self._search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._search_bar.set_margin_start(6); self._search_bar.set_margin_end(6)
        self._search_bar.set_margin_top(4); self._search_bar.set_margin_bottom(4)
        self._search_bar.set_visible(False)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_placeholder_text("Rechercher...")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate",       self._on_search_next)
        self._search_bar.append(self._search_entry)

        self._search_match_lbl = Gtk.Label(label="")
        self._search_match_lbl.add_css_class("search-match-lbl")
        self._search_bar.append(self._search_match_lbl)

        btn_prev = Gtk.Button(icon_name="go-up-symbolic")
        btn_prev.set_tooltip_text("Occurrence précédente")
        btn_prev.add_css_class("panel-icon-btn")
        btn_prev.connect("clicked", self._on_search_prev)
        self._search_bar.append(btn_prev)

        btn_next = Gtk.Button(icon_name="go-down-symbolic")
        btn_next.set_tooltip_text("Occurrence suivante")
        btn_next.add_css_class("panel-icon-btn")
        btn_next.connect("clicked", self._on_search_next)
        self._search_bar.append(btn_next)

        btn_close_search = Gtk.Button(icon_name="window-close-symbolic")
        btn_close_search.add_css_class("panel-icon-btn")
        btn_close_search.set_tooltip_text("Fermer la recherche")
        btn_close_search.connect("clicked", self._on_search_close)
        self._search_bar.append(btn_close_search)

        left_box.append(self._search_bar)

        self._edit_paned.set_start_child(left_box)
        self._edit_paned.set_shrink_start_child(False)

        # Preview
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        rb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL); rb.add_css_class("panel-bar")
        rl = Gtk.Label(label="  Apercu"); rl.add_css_class("panel-label-preview")
        rl.set_xalign(0); rl.set_hexpand(True); rb.append(rl)
        btn_sync = Gtk.ToggleButton()
        btn_sync.set_icon_name('view-continuous-symbolic')
        btn_sync.add_css_class('panel-icon-btn')
        btn_sync.set_tooltip_text('Synchroniser le défilement éditeur/preview')
        btn_sync.set_active(True)
        btn_sync.connect('toggled', lambda b: setattr(self, '_preview_scroll_sync', b.get_active()))
        rb.append(btn_sync)
        right_box.append(rb)
        right_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self._webview = WebKit.WebView()
        self._webview.set_vexpand(True); self._webview.set_hexpand(True)
        self._webview.connect("load-changed", self._on_webview_load_changed)
        sw = Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.set_child(self._webview)
        right_box.append(sw)
        self._page_info_lbl = Gtk.Label(label='')
        self._page_info_lbl.set_halign(Gtk.Align.CENTER)
        self._page_info_lbl.add_css_class('page-info-bar')
        right_box.append(self._page_info_lbl)
        self._edit_paned.set_end_child(right_box)
        self._edit_paned.set_shrink_end_child(False)

        self._main_paned.set_end_child(self._edit_paned)
        self._main_paned.set_shrink_end_child(False)

        self._outer_paned.set_start_child(self._main_paned)
        self._outer_paned.set_shrink_start_child(False)

        # ── Panneau étiquettes (droite) ────────────────────────────────────
        self._tags_panel_widget = self._build_tags_panel()
        self._outer_paned.set_end_child(self._tags_panel_widget)
        self._outer_paned.set_shrink_end_child(False)
        self._tags_panel_visible    = True
        self._outer_paned_saved_pos = None
        self._focus_mode            = False
        self._toc_window            = None
        self._md_help_window        = None
        self._focus_saved_outer     = None
        self._focus_saved_main      = None

        # Connecteurs paned pour sauvegarde temps réel
        self._main_paned.connect("notify::position", self._on_paned_moved)
        self._edit_paned.connect("notify::position", self._on_paned_moved)
        self._outer_paned.connect("notify::position", self._on_paned_moved)
        self.connect("close-request", self._on_close_request)
        self.connect("map", self._on_window_mapped)
        # Controller sur la fenêtre pour Ctrl+S, Ctrl+F, etc.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)
        # Controller sur le TextView pour les touches de navigation
        nav_key_ctrl = Gtk.EventControllerKey()
        nav_key_ctrl.connect("key-pressed", self._on_nav_key_pressed)
        self._view.add_controller(nav_key_ctrl)

    def _build_tags_panel(self):
        """Panneau étiquettes : liste + gestion + recherche."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Barre de titre
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tb.add_css_class("panel-bar")
        tl = Gtk.Label(label="  Etiquettes"); tl.add_css_class("panel-label-tags")
        tl.set_xalign(0); tl.set_hexpand(True); tb.append(tl)
        btn_clear = Gtk.Button(icon_name="edit-clear-symbolic")
        btn_clear.add_css_class("panel-icon-btn"); btn_clear.set_tooltip_text("Effacer filtre")
        btn_clear.connect("clicked", self._on_clear_filter); tb.append(btn_clear)
        btn_new_hdr = Gtk.Button(icon_name="tag-symbolic")
        btn_new_hdr.add_css_class("panel-icon-btn")
        btn_new_hdr.set_tooltip_text("Nouvelle étiquette")
        btn_new_hdr.connect("clicked", self._on_show_new_tag_dialog)
        tb.append(btn_new_hdr)
        box.append(tb)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Barre de recherche d'étiquettes
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.set_margin_start(6); search_box.set_margin_end(6)
        search_box.set_margin_top(6); search_box.set_margin_bottom(4)
        self._tag_search = Gtk.SearchEntry(); self._tag_search.set_hexpand(True)
        self._tag_search.set_placeholder_text("Filtrer etiquettes...")
        self._tag_search.connect("search-changed", self._on_tag_search_changed)
        search_box.append(self._tag_search); box.append(search_box)

        # Label "Filtre actif"
        self._filter_lbl = Gtk.Label(label="")
        self._filter_lbl.add_css_class("filter-label"); self._filter_lbl.set_xalign(0)
        self._filter_lbl.set_margin_start(8); self._filter_lbl.set_margin_bottom(4)
        box.append(self._filter_lbl)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Liste des étiquettes (cliquables pour filtrer)
        tags_scroll = Gtk.ScrolledWindow(); tags_scroll.set_vexpand(True)
        self._tags_listbox = Gtk.ListBox()
        self._tags_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._tags_listbox.add_css_class("files-listbox")
        self._tags_listbox.set_show_separators(True)
        tags_scroll.set_child(self._tags_listbox); box.append(tags_scroll)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Bouton "+ Nouvelle étiquette" en bas
        btn_new_tag = Gtk.Button()
        btn_new_tag.add_css_class("new-note-btn")
        btn_new_tag.set_tooltip_text("Créer une nouvelle étiquette")
        btn_new_tag.connect("clicked", self._on_show_new_tag_dialog)
        btn_new_tag_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_new_tag_box.set_halign(Gtk.Align.CENTER)
        _ico = Gtk.Image.new_from_icon_name('tag-symbolic')
        _ico.set_pixel_size(14)
        btn_new_tag_box.append(_ico)
        btn_new_tag_box.append(Gtk.Label(label='Nouvelle étiquette'))
        btn_new_tag.set_child(btn_new_tag_box)
        box.append(btn_new_tag)

        # Clic droit sur la liste des étiquettes → menu contextuel
        gc_tags = Gtk.GestureClick(); gc_tags.set_button(3)
        gc_tags.connect("pressed", self._on_tags_right_click)
        self._tags_listbox.add_controller(gc_tags)

        return box

    # ── Restauration positions ─────────────────────────────────────────────────

    def _connect_gutter_scroll(self):
        """Connecte le scroll pour rafraîchir la gouttière."""
        vadj = self._scroll_edit.get_vadjustment()
        if vadj:
            vadj.connect('value-changed', lambda _: self._gutter.queue_draw())
            vadj.connect('value-changed', self._on_editor_scrolled)
            vadj.connect('value-changed', lambda _: self._sync_preview_scroll())

    def _on_window_mapped(self, *_):
        self._paned_ready = False
        GLib.timeout_add(80, self._apply_main_paned)

    def _apply_main_paned(self):
        w = self.get_width()
        if w <= 0: return True
        frac_outer = self._config.get("paned_frac_outer", 0.85)
        frac_main  = self._config.get("paned_frac_main",  0.15)
        self._outer_paned.set_position(int(w * frac_outer))
        self._main_paned.set_position(int(w * frac_main))
        GLib.timeout_add(80, self._apply_edit_paned)
        return False

    def _apply_edit_paned(self):
        w          = self.get_width()
        outer_pos  = self._outer_paned.get_position()
        main_pos   = self._main_paned.get_position()
        edit_w     = outer_pos - main_pos
        frac_edit  = self._config.get("paned_frac_edit", 0.882)
        self._edit_paned.set_position(int(edit_w * frac_edit))
        self._paned_ready = True
        self._refresh_file_list()
        self._refresh_tags_list()
        # Connecter le scroll de la gouttière
        self._connect_gutter_scroll()
        return False

    # ── Recherche ─────────────────────────────────────────────────────────

    def _toggle_search(self):
        visible = not self._search_bar.get_visible()
        self._search_bar.set_visible(visible)
        if visible:
            self._search_entry.grab_focus()
            self._search_entry.select_region(0, -1)
        else:
            self._clear_search_highlights()
            self._view.grab_focus()

    def _on_search_close(self, _):
        self._search_bar.set_visible(False)
        self._clear_search_highlights()
        self._search_match_lbl.set_text("")
        self._view.grab_focus()

    def _on_search_changed(self, entry):
        """Surligne toutes les occurrences et affiche le compteur."""
        self._clear_search_highlights()
        query = entry.get_text()
        if not query:
            self._search_match_lbl.set_text("")
            return
        text    = self._get_text()
        matches = []
        start   = 0
        low_text  = text.lower()
        low_query = query.lower()
        while True:
            idx = low_text.find(low_query, start)
            if idx < 0: break
            matches.append(idx)
            start = idx + 1
        self._search_matches  = matches
        self._search_current  = 0 if matches else -1
        # Surligner toutes les occurrences
        for idx in matches:
            s = self._buffer.get_iter_at_offset(idx)
            e = self._buffer.get_iter_at_offset(idx + len(query))
            self._buffer.apply_tag(self._tag_search_hl, s, e)
        # Aller à la première
        if matches:
            self._search_goto(0)
        n = len(matches)
        self._search_match_lbl.set_text(
            str(n) + " résultat" + ("s" if n > 1 else "") if n else "Aucun résultat")
        if n == 0:
            self._search_entry.add_css_class("search-no-match")
        else:
            self._search_entry.remove_css_class("search-no-match")

    def _on_search_next(self, _):
        if not getattr(self, '_search_matches', []): return
        self._search_current = (self._search_current + 1) % len(self._search_matches)
        self._search_goto(self._search_current)

    def _on_search_prev(self, _):
        if not getattr(self, '_search_matches', []): return
        self._search_current = (self._search_current - 1) % len(self._search_matches)
        self._search_goto(self._search_current)

    def _search_goto(self, idx):
        """Déplace le curseur et scrolle vers l'occurrence idx."""
        query   = self._search_entry.get_text()
        offset  = self._search_matches[idx]
        s_iter  = self._buffer.get_iter_at_offset(offset)
        e_iter  = self._buffer.get_iter_at_offset(offset + len(query))
        self._buffer.select_range(s_iter, e_iter)
        self._buffer.place_cursor(s_iter)
        self._view.scroll_to_iter(s_iter, 0.1, True, 0.0, 0.5)
        n = len(self._search_matches)
        self._search_match_lbl.set_text(
            str(idx + 1) + "/" + str(n))

    def _clear_search_highlights(self):
        if hasattr(self, '_tag_search_hl'):
            s, e = self._buffer.get_start_iter(), self._buffer.get_end_iter()
            self._buffer.remove_tag(self._tag_search_hl, s, e)
        self._search_matches = []
        self._search_current = -1

    # ── Recherche globale ─────────────────────────────────────────────────

    def _on_global_search_toggle(self, _):
        visible = not self._global_search_bar.get_visible()
        self._global_search_bar.set_visible(visible)
        if visible:
            self._global_search_entry.grab_focus()
        else:
            self._global_search_entry.set_text("")
            self._clear_global_results()

    def _on_global_search_close(self, _):
        self._global_search_bar.set_visible(False)
        self._global_search_entry.set_text("")
        self._clear_global_results()

    def _on_global_search(self, entry):
        """Recherche le texte dans toutes les notes du répertoire."""
        query = entry.get_text().strip()
        self._clear_global_results()
        if len(query) < 2: return

        files   = self._list_notes()
        results = []  # [(Path, ligne_num, ligne_texte)]
        low_q   = query.lower()
        for f in files:
            try:
                for i, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
                    if low_q in line.lower():
                        results.append((f, i, line.strip()))
            except Exception:
                continue

        if not results:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label='Aucun résultat')
            lbl.add_css_class('lt-fix-btn-noop'); row.set_child(lbl)
            self._global_search_results.append(row)
            return

        for filepath, lineno, line_text in results[:100]:  # max 100
            row = Gtk.ListBoxRow()
            row.add_css_class('file-row')
            row._search_filepath = filepath
            row._search_lineno   = lineno
            row._search_query    = query
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            # Nom du fichier
            name_lbl = Gtk.Label(label=filepath.stem)
            name_lbl.add_css_class('file-name'); name_lbl.set_xalign(0)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.append(name_lbl)

            # Extrait de la ligne avec la correspondance
            excerpt = line_text[:80] + ('...' if len(line_text) > 80 else '')
            ex_lbl = Gtk.Label(label='L' + str(lineno) + ': ' + excerpt)
            ex_lbl.add_css_class('file-date'); ex_lbl.set_xalign(0)
            ex_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.append(ex_lbl)

            row.set_child(vbox)
            self._global_search_results.append(row)

        if len(results) >= 100:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label='... (100 résultats max affichés)')
            lbl.add_css_class('lt-fix-btn-noop'); row.set_child(lbl)
            self._global_search_results.append(row)

    def _on_global_search_result_activated(self, listbox, row):
        """Ouvre la note et positionne le curseur sur la ligne."""
        if not hasattr(row, '_search_filepath'): return
        filepath = row._search_filepath
        lineno   = row._search_lineno
        query    = row._search_query
        try:
            content = filepath.read_text(encoding='utf-8')
            self._loading_file = True
            self._buffer.set_text(content)
            self._loading_file = False
            self._current_file = filepath
            self._refresh_file_list(); self._refresh_note_tags()
            # Aller à la ligne et ouvrir la recherche locale sur le terme
            it = _iter_at_line(self._buffer, lineno - 1)
            self._buffer.place_cursor(it)
            self._view.scroll_to_iter(it, 0.1, True, 0.0, 0.3)
            # Ouvrir la recherche locale avec le même terme
            self._search_bar.set_visible(True)
            self._search_entry.set_text(query)
            self._search_entry.grab_focus()
        except Exception as ex:
            self._set_status('Erreur ouverture : ' + str(ex), 'err')

    def _clear_global_results(self):
        while self._global_search_results.get_row_at_index(0):
            self._global_search_results.remove(
                self._global_search_results.get_row_at_index(0))

    def _on_paned_moved(self, *_):
        if not self._paned_ready: return
        w = self.get_width()
        if w <= 0: return
        outer_pos = self._outer_paned.get_position()
        main_pos  = self._main_paned.get_position()
        edit_pos  = self._edit_paned.get_position()
        edit_w    = outer_pos - main_pos
        self._config["paned_frac_outer"] = outer_pos / w
        self._config["paned_frac_main"]  = main_pos  / w
        self._config["paned_frac_edit"]  = edit_pos  / edit_w if edit_w > 0 else 0.882

    # ── Fermeture ─────────────────────────────────────────────────────────────

    def _on_show_calendar(self, _):
        """Ouvre une fenêtre avec un calendrier marquant les jours avec des notes."""
        win = Gtk.ApplicationWindow(application=self.get_application())
        win.set_title("Calendrier des notes")
        win.set_transient_for(self)
        win.set_modal(False)
        win.set_default_size(380, 420)
        win.set_show_menubar(False)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(vbox)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.add_css_class("panel-bar")
        lbl = Gtk.Label(label="  Calendrier des notes")
        lbl.add_css_class("panel-label-preview"); lbl.set_xalign(0)
        bar.append(lbl); vbox.append(bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        cal = Gtk.Calendar()
        cal.set_margin_start(12); cal.set_margin_end(12)
        cal.set_margin_top(12); cal.set_margin_bottom(12)
        vbox.append(cal)

        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Label info
        self._cal_info_lbl = Gtk.Label(label="Cliquez sur un jour surligne")
        self._cal_info_lbl.add_css_class("dir-label")
        self._cal_info_lbl.set_margin_top(6); self._cal_info_lbl.set_margin_bottom(6)
        vbox.append(self._cal_info_lbl)

        # Construire l'index date → [Path]
        date_index = {}   # (year, month, day) → [Path]
        for f in self._list_notes():
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            key   = (mtime.year, mtime.month, mtime.day)
            date_index.setdefault(key, []).append(f)

        # Marquer les jours avec des notes (mois courant)
        def _mark_month(cal):
            gdt   = cal.get_date()
            year  = gdt.get_year()
            month = gdt.get_month()
            for day in range(1, 32):
                key = (year, month, day)
                if key in date_index:
                    try:
                        cal.mark_day(day)
                    except Exception:
                        pass

        _mark_month(cal)
        cal.connect("next-month",  lambda c: _mark_month(c))
        cal.connect("prev-month",  lambda c: _mark_month(c))
        cal.connect("next-year",   lambda c: _mark_month(c))
        cal.connect("prev-year",   lambda c: _mark_month(c))

        def _on_day_selected(cal):
            gdt   = cal.get_date()
            year  = gdt.get_year()
            month = gdt.get_month()
            day   = gdt.get_day_of_month()
            key   = (year, month, day)
            files = date_index.get(key, [])
            if not files:
                self._cal_info_lbl.set_text(
                    "Aucune note le " + str(day) + "/" + str(month) + "/" + str(year))
                return
            # Filtrer le panneau fichiers sur cette date
            self._calendar_date_filter = key
            self._calendar_date_files  = files
            self._refresh_file_list_by_date(files)
            n = len(files)
            self._cal_info_lbl.set_text(
                str(n) + " note(s) le " + str(day) + "/" + str(month) + "/" + str(year))

        cal.connect("day-selected", _on_day_selected)
        win.connect("close-request", self._on_calendar_closed)
        win.present()

    def _on_calendar_closed(self, win):
        """Restaurer la liste normale à la fermeture du calendrier."""
        self._calendar_date_filter = None
        self._refresh_file_list()
        return False

    def _refresh_file_list_by_date(self, files):
        """Affiche uniquement les fichiers d'une date donnée dans le TreeView."""
        # Sauvegarder l'état déplié
        expanded_labels = set()
        def _walk_save2(model, parent=None):
            it = model.iter_children(parent)
            while it:
                path = model.get_path(it)
                if not model[it][2] and self._tree_view.row_expanded(path):
                    expanded_labels.add(model[it][0].strip())
                _walk_save2(model, it)
                it = model.iter_next(it)
        _walk_save2(self._tree_store)

        while self._tree_store.get_iter_first():
            self._tree_store.remove(self._tree_store.get_iter_first())

        if not files:
            self._tree_store.append(None, ['Aucune note', '', False, '', False, ''])
            return

        # Regrouper par étiquette comme d'habitude
        all_tags  = {t['id']: t for t in self._db.get_tags()}
        file_tags = {f: self._db.get_tag_ids_for_note(f) for f in files}
        groups    = {}
        no_tag    = []
        for f in files:
            tids = file_tags[f]
            if not tids: no_tag.append(f)
            else:
                for tid in sorted(tids):
                    groups.setdefault(tid, []).append(f)

        def add_row(parent, f):
            mtime  = datetime.fromtimestamp(f.stat().st_mtime)
            date_s = mtime.strftime('%d/%m/%Y %H:%M')
            self._tree_store.append(parent, [f.stem, str(f), True, date_s, False, ''])

        for tid in sorted(groups, key=lambda i: all_tags[i]['label'].lower()):
            tag = all_tags[tid]
            parent = self._tree_store.append(
                None, ['  ' + tag['label'].capitalize(), '', False, '', False, tag['color']])
            for f in groups[tid]: add_row(parent, f)

        for f in no_tag: add_row(None, f)

        current_str = str(self._current_file) if self._current_file else None
        def _walk_restore2(model, parent=None):
            it = model.iter_children(parent)
            while it:
                path = model.get_path(it)
                if not model[it][2]:
                    if model[it][0].strip() in expanded_labels:
                        self._tree_view.expand_to_path(path)
                elif current_str and model[it][1] == current_str:
                    self._tree_view.expand_to_path(path)
                    self._tree_view.get_selection().select_path(path)
                    self._tree_view.scroll_to_cell(path, None, True, 0.5, 0.0)
                _walk_restore2(model, it)
                it = model.iter_next(it)
        _walk_restore2(self._tree_store)

    def _on_export_zip(self, _):
        """Exporte toutes les notes du répertoire dans un fichier ZIP."""
        files = self._list_notes()
        if not files:
            self._set_status("Aucune note a exporter.", "err"); return

        # Proposer l'emplacement de sauvegarde
        dialog = Gtk.FileDialog()
        dialog.set_title("Enregistrer le ZIP")
        dialog.set_initial_name(
            "notes_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".zip")
        f = Gtk.FileFilter(); f.set_name("Archives ZIP"); f.add_pattern("*.zip")
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_zip_done)

    def _on_export_zip_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception:
            return
        if not file: return
        zip_path = Path(file.get_path())
        if not zip_path.suffix: zip_path = zip_path.with_suffix(".zip")
        files = self._list_notes()
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, arcname=f.name)
            self._set_status(
                str(len(files)) + " note(s) exportee(s) : " + zip_path.name, "ok")
        except Exception as ex:
            self._set_status("Erreur ZIP : " + str(ex), "err")

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        ctrl_mask = Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_b and state & ctrl_mask:
            self._md_bold(None); return True
        if keyval == Gdk.KEY_i and state & ctrl_mask:
            self._md_italic(None); return True
        if keyval == Gdk.KEY_s and state & ctrl_mask:
            self._on_save(None); return True
        if keyval == Gdk.KEY_f and state & ctrl_mask:
            self._toggle_search(); return True
        if keyval == Gdk.KEY_g and state & ctrl_mask:
            self._on_global_search_toggle(None); return True
        if keyval == Gdk.KEY_Escape:
            if self._search_bar.get_visible():
                self._on_search_close(None); return True


        return False

    def _on_close_request(self, *_):
        # Si des fichiers sont en cours d'édition, demander confirmation
        # Vérifier que les fichiers sont réellement différents du disque
        unsaved = []
        for f in list(self._unsaved_files):
            if f == '__new__' or not hasattr(f, 'name'): continue
            if not f.exists(): self._unsaved_files.discard(f); continue
            # Comparer le contenu mémorisé avec le disque
            try:
                disk = f.read_text(encoding='utf-8')
                mem  = self._unsaved_contents.get(f)
                if mem is None and f == self._current_file:
                    mem = self._get_text()
                if mem is not None and mem == disk:
                    self._unsaved_files.discard(f)
                    continue
            except Exception:
                pass
            unsaved.append(f)
        if unsaved:
            dialog = Gtk.AlertDialog()
            dialog.set_message('Fichiers non sauvegardés')
            n = len(unsaved)
            names = '\n'.join('  • ' + f.name for f in unsaved[:5])
            if n > 5: names += f'\n  ... et {n-5} autre(s)'
            dialog.set_detail(
                str(n) + ' fichier(s) en cours d\'édition :\n' + names +
                '\n\nQuitter sans sauvegarder ?')
            dialog.set_buttons(['Annuler', 'Quitter sans sauvegarder'])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)
            dialog.set_modal(True)
            dialog.choose(self, None, self._on_close_confirmed)
            return True  # bloquer la fermeture
        self._do_quit()
        return True

    def _on_close_confirmed(self, dialog, result):
        try:
            idx = dialog.choose_finish(result)
        except Exception:
            return
        if idx == 1:  # 'Quitter sans sauvegarder'
            self._do_quit()
        # idx == 0 → Annuler, ne rien faire


    # ── Chiffrement local des notes ─────────────────────────────────────────
    def _ensure_encrypted_table(self):
        self._con.execute(
            'CREATE TABLE IF NOT EXISTS encrypted_notes (note_path TEXT PRIMARY KEY)')
        self._con.commit()

    def is_note_encrypted(self, note_path):
        self._ensure_encrypted_table()
        row = self._con.execute(
            'SELECT 1 FROM encrypted_notes WHERE note_path=?',
            (str(note_path),)).fetchone()
        return row is not None

    def set_note_encrypted(self, note_path, encrypted=True):
        self._ensure_encrypted_table()
        if encrypted:
            self._con.execute(
                'INSERT OR IGNORE INTO encrypted_notes(note_path) VALUES(?)',
                (str(note_path),))
        else:
            self._con.execute(
                'DELETE FROM encrypted_notes WHERE note_path=?',
                (str(note_path),))
        self._con.commit()

    # ── Sync SCP ─────────────────────────────────────────────────────────────

    def _notes_scp_configured(self):
        """Vérifie que la config SCP est complète."""
        c = self._config
        return all(c.get(k) for k in ('scp_host', 'scp_user', 'scp_remote_dir'))

    def _on_sync_popup_backup(self, _):
        """Backup : ferme le popover et lance la sync."""
        self._sync_popover.popdown()
        if not self._notes_scp_configured():
            self._show_scp_settings(); return
        self._on_save(None)
        self._show_sync_log_window()

    def _on_sync_popup_restore(self, _):
        """Restaurer : ferme le popover et ouvre la fenêtre de restauration."""
        self._sync_popover.popdown()
        if not self._notes_scp_configured():
            self._show_scp_settings(); return
        self._show_restore_window()

    def _on_sync_popup_config(self, _):
        """Config : ferme le popover et ouvre les paramètres SCP."""
        self._sync_popover.popdown()
        self._show_scp_settings()

    def _on_sync_all(self, _):
        """Alias maintenu pour compatibilité."""
        self._on_sync_popup_backup(None)

    def _show_scp_settings(self):
        """Ouvre le dialog de configuration SCP."""
        dlg = _ScpSettingsDialog(self, self._config)
        dlg.connect('response', self._on_scp_settings_response)
        dlg.present()

    def _on_scp_settings_response(self, dlg, response):
        if response == Gtk.ResponseType.OK:
            self._config.update(dlg.get_values())
            save_config(self._config)
            # Si maintenant configuré, lancer la sync
            if self._notes_scp_configured():
                dlg.destroy()
                self._on_save(None)
                self._show_sync_log_window()
                return
        dlg.destroy()

    def _show_sync_log_window(self):
        """Fenêtre de log de synchronisation SCP."""
        win = Gtk.Window(title='Synchronisation SCP', transient_for=self, modal=False)
        win.set_default_size(700, 500)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # En-tête
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr_box.set_margin_start(12); hdr_box.set_margin_end(12)
        hdr_box.set_margin_top(10); hdr_box.set_margin_bottom(8)
        cfg = self._config
        info = Gtk.Label(label=f'→ {cfg.get("scp_user","")}@{cfg.get("scp_host","")}:{cfg.get("scp_remote_dir","")}')
        info.add_css_class('lt-popup-cat'); info.set_xalign(0); info.set_hexpand(True)
        hdr_box.append(info)
        btn_cfg = Gtk.Button(label='⚙ Configurer')
        btn_cfg.connect('clicked', lambda _: self._show_scp_settings())
        hdr_box.append(btn_cfg)
        vbox.append(hdr_box)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Zone de log (TextView)
        sw = Gtk.ScrolledWindow(); sw.set_vexpand(True)
        log_view = Gtk.TextView(); log_view.set_editable(False)
        log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_view.add_css_class('sync-log-view')
        log_buf = log_view.get_buffer()
        tag_ok   = log_buf.create_tag('ok',   foreground='#a6e3a1')
        tag_err  = log_buf.create_tag('err',  foreground='#f38ba8')
        tag_info = log_buf.create_tag('info', foreground='#89b4fa')
        sw.set_child(log_view); vbox.append(sw)

        # Barre de progression
        prog = Gtk.ProgressBar(); prog.set_show_text(True)
        prog.set_margin_start(12); prog.set_margin_end(12)
        prog.set_margin_top(6); prog.set_margin_bottom(6)
        vbox.append(prog)

        # Boutons bas
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_margin_start(12); btn_box.set_margin_end(12)
        btn_box.set_margin_bottom(10)
        self._btn_sync_cancel = Gtk.Button(label='Annuler')
        self._btn_sync_cancel.connect('clicked', lambda _: setattr(self, '_sync_cancelled', True))
        btn_box.append(self._btn_sync_cancel)
        btn_restore = Gtk.Button(label='⬇ Restaurer...')
        btn_restore.set_tooltip_text('Restaurer des notes depuis le serveur distant')
        btn_restore.connect('clicked', lambda _: self._show_restore_window())
        btn_box.append(btn_restore)
        spacer = Gtk.Box(); spacer.set_hexpand(True); btn_box.append(spacer)
        btn_close = Gtk.Button(label='Fermer')
        btn_close.connect('clicked', lambda _: win.destroy())
        btn_box.append(btn_close)
        vbox.append(btn_box)

        win.set_child(vbox)
        win.present()

        # Lancer la sync dans un thread
        self._sync_cancelled = False
        self._sync_log_buf = log_buf
        self._sync_log_tags = {'ok': tag_ok, 'err': tag_err, 'info': tag_info}
        self._sync_prog = prog

        import threading
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_log(self, msg, tag_name='info'):
        """Ajoute une ligne au log (thread-safe via GLib.idle_add)."""
        def _append():
            buf = self._sync_log_buf
            end_it = buf.get_end_iter()
            tag = self._sync_log_tags.get(tag_name)
            if tag:
                buf.insert_with_tags(end_it, msg + '\n', tag)
            else:
                buf.insert(end_it, msg + '\n')
        GLib.idle_add(_append)

    def _sync_worker(self):
        """Thread de synchronisation SCP."""
        import subprocess as _sp, hashlib, base64
        from pathlib import Path

        cfg = self._config
        host      = cfg.get('scp_host', '')
        user      = cfg.get('scp_user', '')
        remote    = cfg.get('scp_remote_dir', '').rstrip('/')
        password  = cfg.get('scp_password', '')
        notes_dir = self._notes_dir()

        # Collecter tous les fichiers à synchroniser
        # Notes .md + images référencées (dossier data/)
        all_files = []
        for md in notes_dir.glob('*.md'):
            all_files.append(md)
            for img in self._get_note_images(md):
                if img not in all_files:
                    all_files.append(img)
        # Aussi les fichiers du dossier data/
        data_dir = notes_dir / 'data'
        if data_dir.exists():
            for f in data_dir.rglob('*'):
                if f.is_file() and f not in all_files:
                    all_files.append(f)

        total = len(all_files)
        # Générer notes_meta.json avec les étiquettes de chaque note
        import json as _json
        meta = {}
        for md in notes_dir.glob('*.md'):
            tags = self._db.get_tags_for_note(str(md))
            if tags:
                meta[md.name] = [{'label': t['label'], 'color': t['color']} for t in tags]
        meta_path = notes_dir / 'notes_meta.json'
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        if meta_path not in all_files:
            all_files.insert(0, meta_path)
            total = len(all_files)

        self._sync_log(f'Synchronisation de {total} fichiers vers {user}@{host}:{remote}', 'info')
        self._sync_log('─' * 50, 'info')

        # Fonction pour chiffrer un fichier
        def encrypt_file(filepath, password):
            """Chiffre le contenu avec AES-256 (via cryptography ou fallback openssl)."""
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.backends import default_backend
                import os
                key = hashlib.sha256(password.encode()).digest()
                iv  = os.urandom(16)
                data = filepath.read_bytes()
                padder = padding.PKCS7(128).padder()
                padded = padder.update(data) + padder.finalize()
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
                return iv + enc
            except ImportError:
                # Fallback : openssl en ligne de commande
                return None

        # Vérifier sshpass disponible si mot de passe fourni
        use_sshpass = False
        if password:
            r_check = _sp.run(['which', 'sshpass'], capture_output=True)
            if r_check.returncode == 0:
                use_sshpass = True
            else:
                self._sync_log('⚠ sshpass non installé — utilisation clé SSH (mot de passe ignoré)', 'err')
                self._sync_log('  → sudo pacman -S sshpass  pour l\'auth par mot de passe', 'info')

        def _prefix(cmd):
            """Préfixe la commande avec sshpass si disponible."""
            if password and use_sshpass:
                return ['sshpass', '-p', password] + cmd
            return cmd

        def scp_send(local_path, remote_path, data=None):
            """Envoie un fichier (ou des bytes) via SCP."""
            import tempfile
            tmp = None
            try:
                if data is not None:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.enc') as tf:
                        tf.write(data); tmp = tf.name
                    src_path = tmp
                else:
                    src_path = str(local_path)
                cmd = _prefix(['scp', '-o', 'StrictHostKeyChecking=no',
                               '-o', 'ConnectTimeout=10',
                               src_path, f'{user}@{host}:{remote_path}'])
                r = _sp.run(cmd, capture_output=True, text=True, timeout=30)
                return r.returncode == 0, r.stderr.strip()
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)

        # Créer le dossier distant si nécessaire
        mkdir_cmd = _prefix(['ssh', '-o', 'StrictHostKeyChecking=no',
                             '-o', 'ConnectTimeout=10',
                             f'{user}@{host}', f'mkdir -p {remote}/data'])
        r_mkdir = _sp.run(mkdir_cmd, capture_output=True, timeout=20)
        if r_mkdir.returncode != 0:
            self._sync_log(f'⚠ Impossible de créer {remote}/data : {r_mkdir.stderr.strip()}', 'err')

        def sha256_file(path):
            """Calcule le SHA256 du contenu d'un fichier."""
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()

        ok_count = 0; err_count = 0; skip_count = 0
        for i, fpath in enumerate(all_files):
            if self._sync_cancelled:
                self._sync_log('⚠ Synchronisation annulée.', 'err')
                break

            # Chemin distant relatif
            try:
                rel = fpath.relative_to(notes_dir)
            except ValueError:
                rel = Path(fpath.name)
            remote_path = f'{remote}/{rel}'

            # Calculer SHA256 local
            try:
                local_sha = sha256_file(fpath)
            except Exception:
                local_sha = None

            # Comparer avec le SHA256 de la dernière sync réussie
            stored_sha = self._db.get_synced_sha256(str(fpath))
            if local_sha and stored_sha and local_sha == stored_sha:
                skip_count += 1
                self._sync_log(f'  = {rel} (inchangé)', 'info')
                GLib.idle_add(self._sync_prog.set_fraction, (i + 1) / total)
                GLib.idle_add(self._sync_prog.set_text, f'{i+1}/{total}')
                continue

            # Chiffrement si mot de passe
            if password:
                enc_data = encrypt_file(fpath, password)
                ok, err_msg = scp_send(fpath, remote_path + '.enc', data=enc_data)
                label = str(rel) + '.enc'
            else:
                ok, err_msg = scp_send(fpath, remote_path)
                label = str(rel)

            if ok:
                ok_count += 1
                self._sync_log(f'  ✓ {label}', 'ok')
                GLib.idle_add(self._db.set_synced, str(fpath), True, local_sha)
            else:
                err_count += 1
                self._sync_log(f'  ✗ {label} — {err_msg}', 'err')
                GLib.idle_add(self._db.set_synced, str(fpath), False, None)

            # Progression
            frac = (i + 1) / total
            GLib.idle_add(self._sync_prog.set_fraction, frac)
            GLib.idle_add(self._sync_prog.set_text, f'{i+1}/{total}')

        if not self._sync_cancelled:
            self._sync_log('─' * 50, 'info')
            self._sync_log(
                f'Terminé : {ok_count} envoyé(s) ✓  {skip_count} inchangé(s) =  {err_count} erreur(s) ✗',
                'ok' if err_count == 0 else 'err')
            GLib.idle_add(self._sync_prog.set_fraction, 1.0)
            GLib.idle_add(self._sync_prog.set_text, 'Terminé')

        # Rafraîchir la liste pour afficher les indicateurs ☁
        GLib.idle_add(self._refresh_file_list)

    # ── Restauration SCP ─────────────────────────────────────────────────────

    def _show_restore_window(self):
        """Fenêtre de restauration : liste les fichiers distants et permet de les rapatrier."""
        if not self._notes_scp_configured():
            self._show_scp_settings(); return

        win = Gtk.Window(title='Restaurer depuis le serveur', transient_for=self, modal=True)
        win.set_default_size(650, 500)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # En-tête
        cfg = self._config
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.set_margin_start(12); hdr.set_margin_end(12)
        hdr.set_margin_top(10); hdr.set_margin_bottom(8)
        lbl = Gtk.Label(label=f'Serveur : {cfg.get("scp_user","")}@{cfg.get("scp_host","")}:{cfg.get("scp_remote_dir","")}')
        lbl.add_css_class('lt-popup-cat'); lbl.set_xalign(0); lbl.set_hexpand(True)
        hdr.append(lbl)
        btn_refresh = Gtk.Button(label='↻ Actualiser')
        hdr.append(btn_refresh)
        vbox.append(hdr)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Liste des fichiers distants
        store = Gtk.ListStore(bool, str, str, str, str)  # [sélectionné, nom_brut, chemin distant, nom_brut_dup, markup]
        tree = Gtk.TreeView(model=store)
        tree.set_vexpand(True)

        # Colonne case à cocher
        r_toggle = Gtk.CellRendererToggle()
        r_toggle.set_activatable(True)
        r_toggle.connect('toggled', lambda r, path: store.__setitem__(
            store.get_iter(path), [not store[path][0], store[path][1], store[path][2], store[path][3], store[path][4]]))
        col_chk = Gtk.TreeViewColumn('', r_toggle, active=0)
        col_chk.set_min_width(40)
        tree.append_column(col_chk)

        # Colonne nom fichier avec étiquettes colorées (markup)
        r_name = Gtk.CellRendererText()
        r_name.set_property('ellipsize', Pango.EllipsizeMode.END)
        col_name = Gtk.TreeViewColumn('Fichier', r_name, markup=4)
        col_name.set_expand(True)
        tree.append_column(col_name)

        sw = Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.set_child(tree)
        vbox.append(sw)

        # Barre statut liste
        self._restore_status_lbl = Gtk.Label(label='Chargement de la liste...')
        self._restore_status_lbl.add_css_class('lt-popup-cat')
        self._restore_status_lbl.set_margin_start(12)
        self._restore_status_lbl.set_xalign(0)
        vbox.append(self._restore_status_lbl)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Boutons bas
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_start(12); btn_row.set_margin_end(12)
        btn_row.set_margin_top(8); btn_row.set_margin_bottom(10)

        btn_all = Gtk.Button(label='Tout sélectionner')
        btn_all.connect('clicked', lambda _: [store.__setitem__(
            store.get_iter(i), [True, store[i][1], store[i][2], store[i][3], store[i][4]])
            for i in range(len(store))])
        btn_row.append(btn_all)

        btn_none = Gtk.Button(label='Tout désélectionner')
        btn_none.connect('clicked', lambda _: [store.__setitem__(
            store.get_iter(i), [False, store[i][1], store[i][2], store[i][3], store[i][4]])
            for i in range(len(store))])
        btn_row.append(btn_none)

        spacer = Gtk.Box(); spacer.set_hexpand(True); btn_row.append(spacer)

        btn_restore = Gtk.Button(label='⬇ Restaurer la sélection')
        btn_restore.add_css_class('suggested-action')
        btn_restore.connect('clicked', lambda _: self._do_restore(store, win))
        btn_row.append(btn_restore)

        btn_close = Gtk.Button(label='Annuler')
        btn_close.connect('clicked', lambda _: win.destroy())
        btn_row.append(btn_close)

        vbox.append(btn_row)
        win.set_child(vbox)
        win.present()

        # Charger la liste distante
        self._restore_file_store = store
        self._restore_status_lbl_ref = self._restore_status_lbl

        def load_list():
            import threading
            threading.Thread(target=self._load_remote_list,
                             args=(store,), daemon=True).start()

        btn_refresh.connect('clicked', lambda _: load_list())
        load_list()

    def _load_remote_list(self, store):
        """Thread : liste les fichiers distants via SSH."""
        import subprocess as _sp
        cfg = self._config
        host = cfg.get('scp_host', '')
        user = cfg.get('scp_user', '')
        remote = cfg.get('scp_remote_dir', '').rstrip('/')
        password = cfg.get('scp_password', '')

        use_sshpass = False
        if password:
            r = _sp.run(['which', 'sshpass'], capture_output=True)
            use_sshpass = r.returncode == 0

        def prefix(cmd):
            return (['sshpass', '-p', password] + cmd) if use_sshpass else cmd

        # Lister récursivement les fichiers distants
        cmd = prefix(['ssh', '-o', 'StrictHostKeyChecking=no',
                      '-o', 'ConnectTimeout=10',
                      f'{user}@{host}',
                      f'find {remote} -type f | sort'])
        try:
            r = _sp.run(cmd, capture_output=True, text=True, timeout=20)
        except Exception as ex:
            GLib.idle_add(self._restore_status_lbl_ref.set_text, f'Erreur : {ex}')
            return

        if r.returncode != 0:
            GLib.idle_add(self._restore_status_lbl_ref.set_text,
                          f'Erreur SSH : {r.stderr.strip()}')
            return

        files = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]

        # Télécharger notes_meta.json (en clair ou chiffré .enc)
        import json as _json, tempfile as _tf2, hashlib as _hl
        meta = {}

        def _decrypt_meta(data, pwd):
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.backends import default_backend
                key = _hl.sha256(pwd.encode()).digest()
                iv = data[:16]; enc = data[16:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                padded = cipher.decryptor().update(enc) + cipher.decryptor().finalize()
                unpadder = padding.PKCS7(128).unpadder()
                return unpadder.update(padded) + unpadder.finalize()
            except Exception:
                return None

        found_meta = False
        for meta_remote_name in ('notes_meta.json.enc', 'notes_meta.json'):
            meta_remote = remote.rstrip('/') + '/' + meta_remote_name
            with _tf2.NamedTemporaryFile(delete=False, suffix='.tmp') as tf:
                tmp_meta = tf.name
            try:
                cmd_meta = prefix(['scp', '-o', 'StrictHostKeyChecking=no',
                                   '-o', 'ConnectTimeout=10',
                                   f'{user}@{host}:{meta_remote}', tmp_meta])
                r_meta = _sp.run(cmd_meta, capture_output=True, timeout=15)
                if r_meta.returncode == 0:
                    raw = Path(tmp_meta).read_bytes()
                    if meta_remote_name.endswith('.enc') and password:
                        raw = _decrypt_meta(raw, password) or b'{}'
                    meta = _json.loads(raw.decode('utf-8'))
                    found_meta = True
                    break
            except Exception:
                pass
            finally:
                Path(tmp_meta).unlink(missing_ok=True)

        # Si pas de meta distant : construire depuis la DB locale
        # (les étiquettes locales correspondent probablement aux notes distantes)
        if not found_meta:
            notes_dir = self._notes_dir()
            for md in notes_dir.glob('*.md'):
                tags = self._db.get_tags_for_note(str(md))
                if tags:
                    meta[md.name] = [{'label': t['label'], 'color': t['color']} for t in tags]

        def populate():
            store.clear()
            for fpath in files:
                name = fpath.replace(remote + '/', '')
                base = Path(name).name.removesuffix('.enc')
                tags = meta.get(base, [])
                # Markup : nom en blanc + chips colorées pour chaque étiquette
                name_esc = GLib.markup_escape_text(name)
                chips = ''
                for t in tags:
                    color  = t.get('color', '#89b4fa')
                    label  = GLib.markup_escape_text(t.get('label', ''))
                    # Fond semi-transparent simulé par la couleur de texte
                    chips += (f' <span foreground="{color}" '
                              f'background="{color}22" '
                              f'size="small"> {label} </span>')
                markup = f'<span foreground="#cdd6f4">{name_esc}</span>{chips}'
                store.append([False, name, fpath, name, markup])
            meta_src = 'serveur' if found_meta else 'DB locale'
            notes_with_tags = sum(1 for f in files
                if meta.get(Path(f.replace(remote + '/', '')).name.removesuffix('.enc')))
            self._restore_status_lbl_ref.set_text(
                f'{len(files)} fichier(s) — étiquettes : {notes_with_tags} note(s) '
                f'(source : {meta_src})')

        GLib.idle_add(populate)

    def _do_restore(self, store, win):
        """Restaure les fichiers sélectionnés depuis le serveur."""
        selected = [(store[i][3], store[i][2])
                    for i in range(len(store)) if store[i][0]]
        if not selected:
            self._restore_status_lbl_ref.set_text('Aucun fichier sélectionné.')
            return

        win.destroy()

        # Fenêtre de log de restauration
        log_win = Gtk.Window(title='Restauration en cours', transient_for=self, modal=False)
        log_win.set_default_size(600, 400)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        sw = Gtk.ScrolledWindow(); sw.set_vexpand(True)
        log_view = Gtk.TextView(); log_view.set_editable(False)
        log_view.add_css_class('sync-log-view')
        log_buf = log_view.get_buffer()
        tag_ok  = log_buf.create_tag('ok',  foreground='#a6e3a1')
        tag_err = log_buf.create_tag('err', foreground='#f38ba8')
        tag_inf = log_buf.create_tag('inf', foreground='#89b4fa')
        sw.set_child(log_view); vbox.append(sw)

        prog = Gtk.ProgressBar(); prog.set_show_text(True)
        prog.set_margin_start(12); prog.set_margin_end(12)
        prog.set_margin_top(6); prog.set_margin_bottom(6)
        vbox.append(prog)

        btn_close = Gtk.Button(label='Fermer')
        btn_close.set_margin_start(12); btn_close.set_margin_end(12)
        btn_close.set_margin_bottom(10)
        btn_close.connect('clicked', lambda _: log_win.destroy())
        vbox.append(btn_close)

        log_win.set_child(vbox)
        log_win.present()

        # Lancer la restauration dans un thread
        import threading
        threading.Thread(
            target=self._restore_worker,
            args=(selected, log_buf, {'ok': tag_ok, 'err': tag_err, 'inf': tag_inf}, prog),
            daemon=True).start()

    def _restore_log(self, log_buf, tags, msg, tag_name='inf'):
        def _do():
            end = log_buf.get_end_iter()
            tag = tags.get(tag_name)
            if tag:
                log_buf.insert_with_tags(end, msg + '\n', tag)
            else:
                log_buf.insert(end, msg + '\n')
        GLib.idle_add(_do)

    def _restore_worker(self, selected, log_buf, tags, prog):
        """Thread : télécharge et déchiffre les fichiers sélectionnés."""
        import subprocess as _sp, tempfile, hashlib
        from pathlib import Path

        cfg = self._config
        host     = cfg.get('scp_host', '')
        user     = cfg.get('scp_user', '')
        remote   = cfg.get('scp_remote_dir', '').rstrip('/')
        password = cfg.get('scp_password', '')
        notes_dir = self._notes_dir()

        use_sshpass = False
        if password:
            r = _sp.run(['which', 'sshpass'], capture_output=True)
            use_sshpass = r.returncode == 0

        def _prefix(cmd):
            return (['sshpass', '-p', password] + cmd) if use_sshpass else cmd

        def decrypt_data(data, password):
            """Déchiffre des données AES-256-CBC."""
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.backends import default_backend
                key = hashlib.sha256(password.encode()).digest()
                iv  = data[:16]; enc = data[16:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                padded = cipher.decryptor().update(enc) + cipher.decryptor().finalize()
                unpadder = padding.PKCS7(128).unpadder()
                return unpadder.update(padded) + unpadder.finalize()
            except Exception as ex:
                return None

        total = len(selected)
        self._restore_log(log_buf, tags,
            f'Restauration de {total} fichier(s) depuis {user}@{host}:{remote}', 'inf')
        self._restore_log(log_buf, tags, '─' * 50, 'inf')

        # Télécharger notes_meta.json pour ré-appliquer les étiquettes
        import json as _json2, tempfile as _tf3
        meta = {}
        meta_remote = remote.rstrip('/') + '/notes_meta.json'
        with _tf3.NamedTemporaryFile(delete=False, suffix='.json') as tf:
            tmp_meta = tf.name
        try:
            cmd_meta = _prefix(['scp', '-o', 'StrictHostKeyChecking=no',
                                '-o', 'ConnectTimeout=10',
                                f'{user}@{host}:{meta_remote}', tmp_meta])
            r_meta = _sp.run(cmd_meta, capture_output=True, timeout=15)
            if r_meta.returncode == 0:
                meta = _json2.loads(Path(tmp_meta).read_text(encoding='utf-8'))
                self._restore_log(log_buf, tags,
                    f'  Métadonnées chargées ({len(meta)} note(s) avec étiquettes)', 'inf')
        except Exception:
            pass
        finally:
            Path(tmp_meta).unlink(missing_ok=True)

        ok_count = 0; err_count = 0; skip_count = 0
        for i, (rel_name, remote_path) in enumerate(selected):
            # Destination locale
            local_path = notes_dir / rel_name
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Télécharger dans un fichier temporaire
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tf:
                tmp_path = tf.name

            try:
                # SHA256 du fichier local actuel (avant téléchargement)
                local_dest = notes_dir / rel_name.removesuffix('.enc')
                local_sha_before = None
                if local_dest.exists():
                    h = hashlib.sha256()
                    with open(local_dest, 'rb') as f:
                        for chunk in iter(lambda: f.read(65536), b''):
                            h.update(chunk)
                    local_sha_before = h.hexdigest()

                cmd = _prefix(['scp', '-o', 'StrictHostKeyChecking=no',
                              '-o', 'ConnectTimeout=10',
                              f'{user}@{host}:{remote_path}', tmp_path])
                r = _sp.run(cmd, capture_output=True, text=True, timeout=30)

                if r.returncode != 0:
                    self._restore_log(log_buf, tags,
                        f'  ✗ {rel_name} — {r.stderr.strip()}', 'err')
                    err_count += 1
                    continue

                data = Path(tmp_path).read_bytes()

                # Déchiffrer si fichier .enc et mot de passe configuré
                if remote_path.endswith('.enc') and password:
                    decrypted = decrypt_data(data, password)
                    if decrypted is None:
                        self._restore_log(log_buf, tags,
                            f'  ✗ {rel_name} — Échec du déchiffrement', 'err')
                        err_count += 1
                        continue
                    final_data = decrypted
                else:
                    final_data = data

                # Comparer SHA256 distant avec local
                remote_sha = hashlib.sha256(final_data).hexdigest()
                if local_sha_before and local_sha_before == remote_sha:
                    self._restore_log(log_buf, tags,
                        f'  = {rel_name.removesuffix(".enc")} (identique, ignoré)', 'inf')
                    skip_count += 1
                    continue

                local_path = notes_dir / rel_name.removesuffix('.enc')
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(final_data)

                # Ré-appliquer les étiquettes depuis notes_meta.json si disponible
                note_name = local_path.name
                if note_name in meta:
                    for tag_info in meta[note_name]:
                        label = tag_info.get('label', '')
                        color = tag_info.get('color', '#89b4fa')
                        if not label: continue
                        # Créer l'étiquette si elle n'existe pas
                        existing = [t for t in self._db.get_tags() if t['label'] == label]
                        if existing:
                            tag_id = existing[0]['id']
                        else:
                            self._db.add_tag(label, color)
                            new_tags = [t for t in self._db.get_tags() if t['label'] == label]
                            tag_id = new_tags[0]['id'] if new_tags else None
                        if tag_id:
                            cur_ids = [t['id'] for t in self._db.get_tags_for_note(str(local_path))]
                            if tag_id not in cur_ids:
                                self._db.set_tags_for_note(str(local_path), cur_ids + [tag_id])

                tag_str = ''
                if note_name in meta:
                    tag_str = '  ' + ' '.join(f'[{t["label"]}]' for t in meta[note_name])
                self._restore_log(log_buf, tags,
                    f'  ✓ {local_path.name}{tag_str}', 'ok')
                ok_count += 1

            except Exception as ex:
                self._restore_log(log_buf, tags, f'  ✗ {rel_name} — {ex}', 'err')
                err_count += 1
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            GLib.idle_add(prog.set_fraction, (i + 1) / total)
            GLib.idle_add(prog.set_text, f'{i+1}/{total}')

        self._restore_log(log_buf, tags, '─' * 50, 'inf')
        self._restore_log(log_buf, tags,
            f'Terminé : {ok_count} restauré(s) ✓  {skip_count} identique(s) =  {err_count} erreur(s) ✗',
            'ok' if err_count == 0 else 'err')
        GLib.idle_add(prog.set_fraction, 1.0)
        GLib.idle_add(prog.set_text, 'Terminé')
        GLib.idle_add(self._refresh_file_list)

    # ── Chiffrement local + export MD ────────────────────────────────────────

    def _on_toggle_note_encrypt(self, btn, fp, cb, popover):
        """Bascule le chiffrement local de la note."""
        password = self._config.get('scp_password', '')
        if not password:
            self._set_status('Aucun mot de passe SCP configuré — utilisé pour le chiffrement', 'err')
            popover.popdown()
            return
        currently = self._db.is_note_encrypted(fp)
        if currently:
            # Déchiffrer : lire le fichier .enc, déchiffrer, réécrire en clair
            try:
                data = fp.read_bytes()
                plain = self._decrypt_local(data, password)
                if plain is None:
                    self._set_status('Échec du déchiffrement — mot de passe incorrect ?', 'err')
                    popover.popdown(); return
                fp.write_bytes(plain)
                self._db.set_note_encrypted(fp, False)
                self._set_status(f'{fp.name} : déchiffré ✓', 'ok')
            except Exception as ex:
                self._set_status(f'Erreur déchiffrement : {ex}', 'err')
        else:
            # Chiffrer : lire le contenu, chiffrer, réécrire
            try:
                data = fp.read_bytes()
                enc = self._encrypt_local(data, password)
                if enc is None:
                    self._set_status('cryptography non installé : pip install cryptography', 'err')
                    popover.popdown(); return
                fp.write_bytes(enc)
                self._db.set_note_encrypted(fp, True)
                self._set_status(f'{fp.name} : chiffré 🔒 ✓', 'ok')
            except Exception as ex:
                self._set_status(f'Erreur chiffrement : {ex}', 'err')
        popover.popdown()
        self._refresh_file_list()

    def _encrypt_local(self, data, password):
        """Chiffre des bytes avec AES-256-CBC."""
        try:
            import hashlib, os
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.backends import default_backend
            key = hashlib.sha256(password.encode()).digest()
            iv  = os.urandom(16)
            padder = padding.PKCS7(128).padder()
            padded = padder.update(data) + padder.finalize()
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
            return iv + enc
        except ImportError:
            return None

    def _decrypt_local(self, data, password):
        """Déchiffre des bytes AES-256-CBC."""
        try:
            import hashlib
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.backends import default_backend
            key = hashlib.sha256(password.encode()).digest()
            iv  = data[:16]; enc = data[16:]
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            padded = cipher.decryptor().update(enc) + cipher.decryptor().finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        except Exception:
            return None

    def _on_export_md_plain(self, btn, fp, popover):
        """Export de la note en Markdown clair (déchiffré si nécessaire)."""
        popover.popdown()
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en Markdown')
        dialog.set_initial_name(fp.stem + '_export.md')
        f = Gtk.FileFilter(); f.set_name('Markdown'); f.add_pattern('*.md')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_md_plain_done, fp)

    def _on_export_md_plain_done(self, dialog, result, source_fp):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        out_path = Path(file.get_path())
        if not out_path.suffix: out_path = out_path.with_suffix('.md')
        try:
            data = source_fp.read_bytes()
            # Si la note est chiffrée, déchiffrer avant export
            if self._db.is_note_encrypted(source_fp):
                password = self._config.get('scp_password', '')
                if not password:
                    self._set_status('Mot de passe SCP requis pour déchiffrer', 'err'); return
                plain = self._decrypt_local(data, password)
                if plain is None:
                    self._set_status('Échec du déchiffrement', 'err'); return
                out_path.write_bytes(plain)
            else:
                out_path.write_bytes(data)
            self._set_status(f'Exporté : {out_path.name} ✓', 'ok')
        except Exception as ex:
            self._set_status(f'Erreur export : {ex}', 'err')

    def _read_note(self, path):
        """Lit une note depuis le disque, déchiffre si nécessaire."""
        data = path.read_bytes()
        if self._db.is_note_encrypted(path):
            password = self._config.get('scp_password', '')
            if not password:
                raise ValueError('Note chiffrée mais aucun mot de passe SCP configuré')
            plain = self._decrypt_local(data, password)
            if plain is None:
                raise ValueError('Échec du déchiffrement — mot de passe incorrect ?')
            return plain.decode('utf-8')
        return data.decode('utf-8')

    def _write_note(self, path, text):
        """Écrit une note sur le disque, chiffre si nécessaire."""
        data = text.encode('utf-8')
        if self._db.is_note_encrypted(path):
            password = self._config.get('scp_password', '')
            if not password:
                raise ValueError('Note chiffrée mais aucun mot de passe SCP configuré')
            enc = self._encrypt_local(data, password)
            if enc is None:
                raise ValueError('cryptography non installé : pip install cryptography')
            path.write_bytes(enc)
        else:
            path.write_bytes(data)

    # ── Export Markdown ──────────────────────────────────────────────────────

    def _on_export_md(self, _):
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en Markdown')
        dialog.set_initial_name(self._current_file.stem + '_export.md')
        f = Gtk.FileFilter(); f.set_name('Markdown'); f.add_pattern('*.md')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_md_done)

    def _on_export_md_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        out_path = Path(file.get_path())
        if not out_path.suffix: out_path = out_path.with_suffix('.md')
        try:
            # Lire via _read_note pour déchiffrer si nécessaire
            text = self._read_note(self._current_file)
            out_path.write_text(text, encoding='utf-8')
            self._set_status('Markdown exporté : ' + out_path.name, 'ok')
        except Exception as ex:
            self._set_status('Erreur export MD : ' + str(ex), 'err')

    # ── Export HTML ──────────────────────────────────────────────────────────

    def _on_export_html(self, _):
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en HTML')
        dialog.set_initial_name(self._current_file.stem + '.html')
        f = Gtk.FileFilter(); f.set_name('Fichier HTML'); f.add_pattern('*.html')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_html_done)

    def _on_export_html_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        html_path = Path(file.get_path())
        if not html_path.suffix: html_path = html_path.with_suffix('.html')

        text = self._get_text()
        text = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()
        text = replace_emojis(text)

        m = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
        doc_title = re.sub(r'[*_`]', '', m.group(1)).strip() if m else (
            self._current_file.stem if self._current_file else 'Note')

        import markdown as _md
        md_tables = re.findall(r'(?:^\|.+\n)+', text, re.MULTILINE)
        try:
            body = _md.markdown(text, extensions=['tables','fenced_code','toc','nl2br','sane_lists'])
        except Exception:
            body = _md.markdown(text, extensions=['tables','fenced_code'])
        body = inject_col_widths(body, md_tables)

        html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{GLib.markup_escape_text(doc_title)}</title>
<style>
  body {{ max-width: 860px; margin: 40px auto; padding: 0 20px;
         font-family: 'Noto Sans', Arial, sans-serif; font-size: 15px;
         line-height: 1.7; color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 2em; border-bottom: 2px solid #ccc; padding-bottom: .3em; margin-top: 1.4em; }}
  h2 {{ font-size: 1.5em; border-bottom: 1px solid #eee; margin-top: 1.2em; }}
  h3 {{ font-size: 1.2em; margin-top: 1em; }}
  h4 {{ font-size: 1.05em; margin-top: 1em; }}
  p {{ margin: 0.7em 0; }}
  ul, ol {{ padding-left: 1.5em; }} li {{ margin-bottom: 0.2em; }}
  code {{ background: #f4f4f4; color: #333; padding: 2px 5px;
          border-radius: 3px; font-family: 'DejaVu Sans Mono', monospace; font-size: 0.88em; }}
  pre {{ background: #f4f4f4; padding: 14px 18px; border-radius: 5px;
         overflow-x: auto; border-left: 4px solid #ccc; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ border-left: 4px solid #ccc; margin: 1em 0;
                padding: 6px 16px; color: #555; font-style: italic; }}
  a {{ color: #2255aa; }} a:hover {{ text-decoration: underline; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th {{ background: #f0f0f0; border: 1px solid #ccc; padding: 8px 10px; text-align: left; }}
  td {{ border: 1px solid #ccc; padding: 7px 10px; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  img {{ max-width: 100%; border-radius: 4px; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}

</style>
</head>
<body>
{body}
</body>
</html>'''

        try:
            html_path.write_text(html, encoding='utf-8')
            self._set_status('HTML exporté : ' + html_path.name, 'ok')
        except Exception as ex:
            self._set_status('Erreur HTML : ' + str(ex), 'err')

    # ── Renommage inline ─────────────────────────────────────────────────────

    def _on_tree_btn_pressed(self, gesture, n_press, x, y):
        """Détecte le double-clic sur la liste de fichiers."""
        import time
        now = time.monotonic()

        bx, by = int(x), int(y)
        res = self._tree_view.get_path_at_pos(bx, by)
        if not res:
            self._last_click_path = None
            return

        tree_path = res[0]
        path_str  = tree_path.to_string()

        is_double = (self._last_click_path == path_str
                     and now - self._last_click_time < 0.45)

        if is_double:
            self._last_click_path = None
            self._last_click_time = 0
            it = self._tree_store.get_iter(tree_path)
            if it is None: return
            if self._tree_store[it][0].startswith('─'): return
            if not self._tree_store[it][2]: return  # groupe
            path_s = self._tree_store[it][1]
            if not path_s: return
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._start_inline_rename(Path(path_s), tree_path, x, y)
        else:
            self._last_click_path = path_str
            self._last_click_time = now

    def _start_inline_rename(self, filepath, tree_path, x, y):
        """Affiche un popover avec Entry pour renommer la note."""
        # Fermer un renommage en cours
        if hasattr(self, '_rename_popover') and self._rename_popover:
            try: self._rename_popover.popdown()
            except Exception: pass

        pop = Gtk.Popover()
        pop.set_parent(self._tree_view)
        pop.set_has_arrow(True)
        pop.set_autohide(True)
        pop.set_position(Gtk.PositionType.RIGHT)
        self._rename_popover = pop

        # Positionner sur la cellule
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)

        # Contenu du popover
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_start(10); vbox.set_margin_end(10)
        vbox.set_margin_top(10);   vbox.set_margin_bottom(10)

        lbl = Gtk.Label(label='Renommer la note')
        lbl.add_css_class('lt-popup-msg'); lbl.set_xalign(0)
        vbox.append(lbl)

        entry = Gtk.Entry()
        entry.set_text(filepath.stem)
        entry.set_width_chars(30)
        entry.select_region(0, -1)
        vbox.append(entry)

        hint = Gtk.Label(label='Extension .md ajoutée automatiquement')
        hint.add_css_class('lt-popup-cat'); hint.set_xalign(0)
        vbox.append(hint)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_ok  = Gtk.Button(label='Renommer')
        btn_ok.add_css_class('suggested-action')
        btn_cancel = Gtk.Button(label='Annuler')
        btn_cancel.connect('clicked', lambda _: pop.popdown())
        btn_box.append(btn_cancel)
        spacer = Gtk.Box(); spacer.set_hexpand(True); btn_box.append(spacer)
        btn_box.append(btn_ok)
        vbox.append(btn_box)

        def do_rename(*_):
            new_stem = entry.get_text().strip()
            if not new_stem: return
            # Nettoyer le nom (pas de / ni de caractères dangereux)
            import re as _re
            new_stem = _re.sub(r'[/\\:*?"<>|]', '_', new_stem)
            new_path = filepath.parent / (new_stem + '.md')
            if new_path == filepath:
                pop.popdown(); return
            if new_path.exists():
                hint.set_markup('<span foreground="#f38ba8">Ce nom existe déjà !</span>')
                return
            try:
                # Renommer le fichier
                filepath.rename(new_path)
                # Mettre à jour la DB (étiquettes, sync, chiffrement)
                for table, col in [('note_tags','note_path'),
                                   ('sync_status','note_path'),
                                   ('encrypted_notes','note_path'),
                                   ('pinned','note_path'),
                                   ('favorites','note_path')]:
                    try:
                        self._db._con.execute(
                            f'UPDATE {table} SET {col}=? WHERE {col}=?',
                            (str(new_path), str(filepath)))
                    except Exception: pass
                self._db._con.commit()
                # Mettre à jour la note courante si c'est elle
                if self._current_file == filepath:
                    self._current_file = new_path
                    self._config['last_file'] = str(new_path)
                    self._update_header_note_title()
                    self._update_title()
                pop.popdown()
                self._refresh_file_list()
                self._set_status(f'Renommé : {filepath.name} → {new_path.name}', 'ok')
            except Exception as ex:
                hint.set_markup(f'<span foreground="#f38ba8">Erreur : {ex}</span>')

        btn_ok.connect('clicked', do_rename)
        entry.connect('activate', do_rename)  # Entrée valide

        pop.set_child(vbox)
        pop.popup()
        entry.grab_focus()

    def _do_quit(self):
        """Sauvegarde la config et quitte."""
        if self._scan_source_id: GLib.source_remove(self._scan_source_id)
        self._config["lt_enabled"]  = self._lt_enabled
        self._config["lt_language"] = self._lt_language
        self._config["win_width"]   = self.get_width()
        self._config["win_height"]  = self.get_height()
        save_config(self._config)
        self._db.close()
        self.get_application().quit()

    def _apply_css(self):
        css = (
            "window { background-color: #13131f; }"
            "headerbar { background: linear-gradient(180deg,#1a1a28 0%,#13131f 100%);"
            "    color: #e0e0f0; border: none; border-bottom: 1px solid #2e2e45;"
            "    box-shadow: 0 2px 8px rgba(0,0,0,0.5); min-height: 46px; }"
            "headerbar * { border: none; box-shadow: none; }"
            "headerbar windowhandle { background: transparent; }"
            "headerbar button, headerbar menubutton > button { background: rgba(255,255,255,0.06);"
            "    border-radius: 8px; color: #e0e0f0; padding: 4px 8px; margin: 2px;"
            "    min-width: 34px; min-height: 34px; }"
            "headerbar menubutton { padding: 0; margin: 2px; }"
            "headerbar button:hover, headerbar menubutton > button:hover { background: rgba(124,140,248,0.25); color: #7c8cf8; }"
            ".titlebar { background: #1a1a28; border: none; box-shadow: none; }"
            "paned > separator { background-color: #2e2e45; min-width: 3px; min-height: 3px; }"
            "paned > separator:hover { background-color: #7c8cf8; }"
            ".panel-bar { background: linear-gradient(180deg,#1a1a28 0%,#161624 100%);"
            "    min-height: 34px; padding: 0 8px; border-bottom: 1px solid #2e2e45; }"
            ".panel-label-edit { color: #7c8cf8; font-size: 11px; font-weight: 700; padding: 4px 6px; }"
            ".panel-label-preview { color: #6fcf97; font-size: 11px; font-weight: 700; padding: 4px 6px; }"
            ".panel-label-files { color: #bb86fc; font-size: 11px; font-weight: 700; padding: 4px 6px; }"
            ".panel-label-tags { color: #f2c94c; font-size: 11px; font-weight: 700; padding: 4px 6px; }"
            ".panel-icon-btn { padding: 3px 7px; min-height: 0; border: none;"
            "    background: none; border-radius: 6px; color: #b0b0c8; }"
            ".panel-icon-btn:hover { background: rgba(124,140,248,0.15); color: #7c8cf8; }"
            ".section-label { color: #a0a0b8; font-size: 10px; font-weight: 700; padding: 0 8px; }"
            ".editor-view { background-color: #0d0d14; color: #e0e0f0;"
            "    font-family: 'Noto Sans',sans-serif; font-size: 14px; }"
            ".editor-view text { background-color: #0d0d14; color: #e0e0f0; }"
            "textview { background-color: #0d0d14; }"
            ".lt-btn-on { color: #6fcf97; font-weight: 700; }"
            ".lt-btn-off { color: #5a5a7a; font-weight: 700; }"
            ".status-bar { background: #0d0d14; color: #5a5a7a; font-size: 11px;"
            "    padding: 3px 12px; min-height: 24px; border-top: 1px solid #2e2e45; }"
            ".status-ok { color: #6fcf97; } .status-err { color: #eb5757; } .status-busy { color: #f2c94c; }"
            ".dir-label { color: #9090a8; font-size: 10px; font-style: italic; }"
            ".filter-label { color: #f2c94c; font-weight: 600; font-size: 10px; }"
            ".files-listbox { background-color: #13131f; color: #e0e0f0; }"
            "treeview { background-color: #13131f; color: #e0e0f0; }"
            "treeview:selected { background-color: rgba(124,140,248,0.18); }"
            "treeview header button { background-color: #13131f; }"
            ".file-row { padding: 6px 12px; }"
            ".file-name { color: #e0e0f0; font-size: 12px; }"
            ".file-name-active { color: #7c8cf8; font-size: 12px; font-weight: 700; }"
            ".file-date { color: #9090a8; font-size: 10px; }"
            ".file-new-badge { color: #6fcf97; font-size: 10px; font-weight: 700; }"
            ".tag-chip { border-radius: 12px; padding: 2px 10px; font-size: 10px; font-weight: 600; }"
            ".chip-text { color: #ffffff; font-size: 10px; font-weight: 600; background: none; }"
            ".tag-row { padding: 5px 10px; } .tag-name { color: #ffffff; font-size: 12px; }"
            ".tag-active { font-weight: 700; }"
            ".new-note-btn { background: rgba(124,140,248,0.08); color: #7c8cf8;"
            "    border: none; border-top: 1px solid #2e2e45; border-radius: 0;"
            "    padding: 10px; font-size: 12px; font-weight: 600; }"
            ".new-note-btn:hover { background: rgba(124,140,248,0.2); color: #a0aeff; }"
            ".search-match-lbl { color: #9090a8; font-size: 11px; min-width: 60px; }"
            ".search-no-match { background-color: rgba(235,87,87,0.15); }"
            ".search-no-match text { color: #eb5757; }"
            "popover.lt-popover > contents { background-color: #1a1a28;"
            "    border: 1px solid #2e2e45; border-radius: 12px; padding: 0;"
            "    min-width: 280px; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }"
            ".lt-popup-header { background: #252535; border-radius: 11px 11px 0 0; padding: 10px 14px; }"
            ".lt-popup-msg { color: #e0e0f0; font-size: 13px; font-weight: 600; }"
            ".lt-popup-cat { color: #9090a8; font-size: 10px; }"
            ".lt-popup-fixes { padding: 8px 10px; }"
            ".lt-fix-btn { background: rgba(124,140,248,0.1); color: #7c8cf8;"
            "    border: 1px solid rgba(124,140,248,0.3);"
            "    border-radius: 8px; padding: 5px 12px; font-size: 12px; margin: 3px; }"
            ".lt-fix-btn:hover { background: rgba(124,140,248,0.25); border-color: #7c8cf8; }"
            ".lt-fix-btn-noop { color: #9090a8; font-size: 12px; padding: 8px 14px; }"
            ".lt-ignore-btn { color: #9090a8; font-size: 11px; padding: 4px 14px 10px; }"
            ".sync-ok  { color: #a6e3a1; }"
            ".sync-err { color: #f38ba8; }"
            ".sync-log-view { font-family: monospace; font-size: 11px; background: #11111b; color: #cdd6f4; padding: 8px; }"
            ".sync-log-ok  { color: #a6e3a1; }"
            ".sync-log-err { color: #f38ba8; }"
            ".sync-log-info { color: #89b4fa; }"
            ".lt-ignore-btn:hover { color: #eb5757; }"
            ".tmpl-btn { background: none; color: #e0e0f0; border: none;"
            "    border-radius: 6px; padding: 6px 16px; font-size: 12px; }"
            ".tmpl-btn:hover { background: rgba(124,140,248,0.15); color: #7c8cf8; }"
            ".tmpl-btn-mail { color: #6fcf97; }"
            ".tmpl-btn-mail:hover { background: rgba(111,207,151,0.12); }"
            ".file-delete-btn { background: none; color: #eb5757; border: none;"
            "    border-radius: 6px; padding: 7px 16px; font-size: 12px; }"
            ".file-delete-btn:hover { background: rgba(235,87,87,0.12); }"
            ".hugo-publish-btn { color: #6fcf97; }"
            ".hugo-publish-btn:hover { background: rgba(111,207,151,0.12); color: #6fcf97; }"
            "@keyframes fadeIn { from { opacity:0; } to { opacity:1; } }"
            ".editor-view { animation: fadeIn 0.15s ease-in; }"
            "textview text { caret-color: #7c8cf8; }"
            "@keyframes pulse { 0%,100%{ opacity:1; } 50%{ opacity:0.4; } }"
            ".badge-unsaved { animation: pulse 2s ease-in-out infinite; }"
            ".avatar-initials { background: #7c8cf8; color: #fff; font-weight:700; font-size:11px; padding:5px 7px; border-radius:50%; min-width:28px; min-height:28px; margin:2px 4px; }"
            ".header-note-title { font-size:13px; color:#c0c0d0; font-weight:500; }"

            ".line-gutter { background-color: #0d0d14; min-width: 44px; }"
            ".gutter-sep { background-color: #2e2e45; min-width: 1px; }"
            ".md-toolbar { background: #0f0f1a; }"
            ".md-tool-btn { background: none; border: none; border-radius: 6px; padding: 4px 8px; min-height: 0; min-width: 0; color: #c0c0d0; font-size: 11px; }"
            ".md-tool-btn:hover { background: rgba(124,140,248,0.18); color: #7c8cf8; }"
            ".md-tool-btn:active { background: rgba(124,140,248,0.35); color: #7c8cf8; }"
            ".md-help-syntax { font-family: Monospace; font-size: 12px; color: #a6e3a1; background: #1a1a2e; border-radius: 4px; padding: 2px 6px; }"
            ".tag-filter-btn { background: none; border: none; border-radius: 6px; padding: 4px 8px; min-height:0; }"
            ".tag-filter-btn:hover { background: rgba(124,140,248,0.10); }"
            ".tag-filter-active { background: rgba(124,140,248,0.18); }"
            ".tag-row { background: none; border-radius: 6px; }"
            ".tag-row:hover { background: rgba(255,255,255,0.04); }"
            ".page-info-bar { font-size: 11px; color: #585b70; padding: 2px 8px; background: #181825; border-top: 1px solid #313244; }"

        ).encode()
        p = Gtk.CssProvider(); p.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── Gestion des étiquettes ────────────────────────────────────────────────

    def _refresh_tags_list(self, search=""):
        """Rafraîchit la liste des étiquettes avec filtre texte optionnel."""
        while self._tags_listbox.get_row_at_index(0):
            self._tags_listbox.remove(self._tags_listbox.get_row_at_index(0))

        tags = self._db.get_tags()
        search_lower = search.lower()
        for tag in tags:
            if search_lower and search_lower not in tag["label"].lower():
                continue
            row = Gtk.ListBoxRow(); row.add_css_class("tag-row")
            row._tag_id = tag["id"]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            # Pastille couleur cliquable (toggle filtre)
            is_active = tag["id"] in self._active_tag_filter
            color_btn = make_colored_button_chip(tag["label"].capitalize(), tag["color"], is_active)
            color_btn.connect_click(lambda tid=tag["id"]: self._on_toggle_tag_filter(None, tid))
            color_btn.set_tooltip_text("Cliquer pour filtrer")
            hbox.append(color_btn)

            hbox.append(Gtk.Label()); hbox.get_last_child().set_hexpand(True)

            # Bouton éditer
            btn_edit = Gtk.Button(icon_name="document-edit-symbolic")
            btn_edit.add_css_class("panel-icon-btn")
            btn_edit.set_tooltip_text("Modifier")
            btn_edit.connect("clicked", self._on_edit_tag, tag["id"], tag["label"], tag["color"])
            hbox.append(btn_edit)

            # Bouton supprimer
            btn_del = Gtk.Button(icon_name="user-trash-symbolic")
            btn_del.add_css_class("panel-icon-btn")
            btn_del.set_tooltip_text("Supprimer")
            btn_del.connect("clicked", self._on_delete_tag, tag["id"])
            hbox.append(btn_del)

            row.set_child(hbox)
            self._tags_listbox.append(row)

    def _refresh_note_tags(self):
        """Mise à jour de la preview avec les étiquettes (le panneau droit n'existe plus)."""
        # Les étiquettes sont maintenant affichées dans la preview WebKit
        if not self._preview_pending:
            self._preview_pending = True
            GLib.timeout_add(PREVIEW_DEBOUNCE, self._refresh_preview)
    def _on_file_right_click(self, gesture, n, x, y):
        """Clic droit : menu sur la sélection multiple ou le fichier sous le curseur."""
        # Récupérer les chemins sélectionnés
        selection = self._tree_view.get_selection()
        model, paths = selection.get_selected_rows()

        # Collecter les fichiers sélectionnés
        selected_files = []
        for tp in paths:
            it = self._tree_store.get_iter(tp)
            if it and self._tree_store[it][2] and self._tree_store[it][1]:
                selected_files.append(Path(self._tree_store[it][1]))

        # Si rien de sélectionné, utiliser get_dest_row_at_pos qui couvre
        # toute la largeur de la ligne (fix pour les noms ellipsés trop longs)
        if not selected_files:
            result = self._tree_view.get_dest_row_at_pos(int(x), int(y))
            if result is not None:
                tp2, _ = result
            else:
                res2 = self._tree_view.get_path_at_pos(1, int(y))
                tp2 = res2[0] if res2 else None
            if tp2 is None: return
            it2 = self._tree_store.get_iter(tp2)
            if it2 and self._tree_store[it2][2] and self._tree_store[it2][1]:
                selected_files.append(Path(self._tree_store[it2][1]))

        if not selected_files: return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_file_tag_menu(selected_files, x, y)

    def _show_file_tag_menu(self, filepaths, x, y):
        """
        Popover d'attribution d'étiquettes pour une sélection de fichiers.
        filepaths : liste de Path
        - Checkbox cochée  = tag présent sur TOUS les fichiers sélectionnés
        - Checkbox tiret   = tag présent sur CERTAINS (état intermédiaire)
        - Checkbox vide    = tag absent de tous
        Appliquer/retirer un tag l'applique/retire sur TOUTE la sélection.
        """
        if hasattr(self, '_file_menu_popover') and self._file_menu_popover:
            self._file_menu_popover.unparent()
            self._file_menu_popover = None

        # Normaliser en liste
        if isinstance(filepaths, Path): filepaths = [filepaths]
        multi = len(filepaths) > 1

        all_tags = self._db.get_tags()
        # tag_id → set des fichiers sélectionnés qui l'ont
        tag_coverage = {}
        for fp in filepaths:
            for tid in self._db.get_tag_ids_for_note(fp):
                tag_coverage.setdefault(tid, set()).add(fp)

        _override_btn = getattr(self, '_ftm_override_widget', None)
        win_h = self.get_height() or 800

        popover = Gtk.Popover()
        if _override_btn is not None:
            # Bouton ⋮ : parent = le bouton lui-même, pas le tree_view
            # → GTK4 calcule les coords dans le référentiel du bouton
            popover.set_parent(_override_btn)
            popover.set_has_arrow(False)
            popover.set_autohide(True)
            popover.set_position(Gtk.PositionType.BOTTOM)
            # pointing_to sur toute la surface du bouton → centré dessous
            bw = _override_btn.get_width() or 32
            bh = _override_btn.get_height() or 32
            r0 = Gdk.Rectangle()
            r0.x, r0.y, r0.width, r0.height = 0, 0, bw, bh
            popover.set_pointing_to(r0)
        else:
            # Clic droit : parent = tree_view, ajuster Y pour tenir dans l'écran
            popover.set_parent(self._tree_view)
            popover.set_has_arrow(True)
            popover.set_autohide(True)
            MENU_EST_H = 480
            adjusted_y = int(y)
            if adjusted_y + MENU_EST_H > win_h - 10:
                adjusted_y = max(5, win_h - MENU_EST_H - 10)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), adjusted_y, 1, 1
            popover.set_pointing_to(rect)
            popover.set_position(Gtk.PositionType.RIGHT)

        self._file_menu_popover = popover

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # En-tête
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hdr.add_css_class("lt-popup-header")
        if multi:
            title_lbl = Gtk.Label(label=str(len(filepaths)) + " notes selectionnees")
        else:
            title_lbl = Gtk.Label(label=filepaths[0].stem)
            title_lbl.set_max_width_chars(30)
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.add_css_class("lt-popup-msg"); title_lbl.set_xalign(0)
        hdr.append(title_lbl)
        sub_lbl = Gtk.Label(label="Attribuer des etiquettes")
        sub_lbl.add_css_class("lt-popup-cat"); sub_lbl.set_xalign(0)
        hdr.append(sub_lbl)
        vbox.append(hdr)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        if not all_tags:
            empty = Gtk.Label(label="Aucune etiquette creee")
            empty.add_css_class("lt-fix-btn-noop"); vbox.append(empty)
        else:
            scroll = Gtk.ScrolledWindow()
            # Hauteur adaptée au nombre d'étiquettes (36px par ligne)
            row_h    = 36
            n_tags   = len(all_tags)
            content_h = n_tags * row_h
            # Limiter à 80% de la hauteur de l'écran
            screen_h  = self.get_height() or 800
            max_h     = int(screen_h * 0.8)
            scroll.set_min_content_height(min(content_h, max_h))
            scroll.set_max_content_height(min(content_h, max_h))
            scroll.set_propagate_natural_height(True)
            tags_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            for tag in all_tags:
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row_box.set_margin_start(8); row_box.set_margin_end(8)
                row_box.set_margin_top(4); row_box.set_margin_bottom(4)

                coverage = tag_coverage.get(tag["id"], set())
                all_have  = len(coverage) == len(filepaths)  # tous ont ce tag
                some_have = 0 < len(coverage) < len(filepaths)  # seulement certains

                cb = Gtk.CheckButton()
                cb.set_active(all_have)
                if some_have:
                    cb.set_inconsistent(True)  # état intermédiaire (tiret)
                cb.connect("toggled", self._on_multi_tag_toggled,
                           filepaths, tag["id"])
                row_box.append(cb)

                chip = make_colored_chip(tag["label"].capitalize(), tag["color"])
                row_box.append(chip)
                tags_box.append(row_box)

            scroll.set_child(tags_box); vbox.append(scroll)

        # ── Helper icône GTK ──────────────────────────────────────────────────
        def _ibtn(icon_name, label, css, callback, *args):
            b = Gtk.Button(); b.add_css_class(css); b.set_halign(Gtk.Align.FILL)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_start(6); row.set_margin_end(8)
            row.set_margin_top(3); row.set_margin_bottom(3)
            ico = Gtk.Image.new_from_icon_name(icon_name)
            ico.set_pixel_size(16); row.append(ico)
            lbl = Gtk.Label(label=label); lbl.set_xalign(0); lbl.set_hexpand(True)
            row.append(lbl); b.set_child(row)
            b.connect('clicked', callback, *args)
            vbox.append(b)

        # ── Actions fichier unique ─────────────────────────────────────────
        if not multi:
            fp = filepaths[0]
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            _ibtn('mail-send-symbolic', 'Envoyer par mail',
                  'tmpl-btn', self._on_send_mail_file, fp, popover)
            try:
                has_hugo = fp.read_bytes()[:3] == b'---'
            except Exception:
                has_hugo = False
            if has_hugo:
                _ibtn('emblem-web-symbolic', 'Publier sur Hugo',
                      'hugo-publish-btn', self._on_publish_hugo, fp, popover)
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            _ibtn('office-calendar-symbolic', 'Changer la date',
                  'tmpl-btn', self._on_change_date, fp, popover)
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            is_pin = self._db.is_pinned(fp)
            _ibtn('view-pin-symbolic',
                  'Désépingler' if is_pin else 'Épingler',
                  'tmpl-btn', self._on_toggle_pin, fp, popover)
            is_fav = self._db.is_favorite(fp)
            _ibtn('starred-symbolic' if is_fav else 'non-starred-symbolic',
                  'Retirer des favoris' if is_fav else 'Ajouter aux favoris',
                  'tmpl-btn', self._on_toggle_favorite, fp, popover)
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            # Chiffrement : bouton avec case à cocher intégrée
            is_enc = self._db.is_note_encrypted(fp)
            b_enc = Gtk.Button(); b_enc.add_css_class('tmpl-btn'); b_enc.set_halign(Gtk.Align.FILL)
            r_enc = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            r_enc.set_margin_start(6); r_enc.set_margin_end(8)
            r_enc.set_margin_top(3); r_enc.set_margin_bottom(3)
            ico_enc = Gtk.Image.new_from_icon_name('changes-prevent-symbolic')
            ico_enc.set_pixel_size(16); r_enc.append(ico_enc)
            lbl_enc = Gtk.Label(label='Chiffrer cette note')
            lbl_enc.set_xalign(0); lbl_enc.set_hexpand(True); r_enc.append(lbl_enc)
            cb_enc = Gtk.CheckButton(); cb_enc.set_active(is_enc); r_enc.append(cb_enc)
            b_enc.set_child(r_enc)
            b_enc.connect('clicked', self._on_toggle_note_encrypt, fp, cb_enc, popover)
            vbox.append(b_enc)
            # Export MD clair
            _ibtn('document-save-as-symbolic', 'Exporter en Markdown (clair)',
                  'tmpl-btn', self._on_export_md_plain, fp, popover)

        # ── Corbeille ─────────────────────────────────────────────────────
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        n_files = len(filepaths)
        _trash_lbl = ('Mettre à la corbeille (' + str(n_files) + ' notes)'
                      if multi else 'Mettre à la corbeille')
        _ibtn('user-trash-symbolic', _trash_lbl, 'file-delete-btn',
              self._on_trash_files, filepaths, popover)

        popover.set_child(vbox); popover.popup()

    def _on_change_date(self, _, filepath, popover):
        """Ouvre un calendrier pour changer la date de modification du fichier."""
        popover.popdown(); popover.unparent()
        self._file_menu_popover = None

        # Date courante du fichier
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime)

        # Fenêtre calendrier
        win = Gtk.ApplicationWindow(application=self.get_application())
        win.set_title("Changer la date - " + filepath.stem)
        win.set_transient_for(self)
        win.set_modal(True)
        win.set_default_size(320, 380)
        win.set_show_menubar(False)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(vbox)

        # Barre de titre
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.add_css_class("panel-bar")
        lbl = Gtk.Label(label="  Choisir une date")
        lbl.add_css_class("panel-label-edit"); lbl.set_xalign(0)
        bar.append(lbl); vbox.append(bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Calendrier
        cal = Gtk.Calendar()
        cal.set_margin_start(12); cal.set_margin_end(12)
        cal.set_margin_top(12); cal.set_margin_bottom(8)
        # Positionner sur la date courante du fichier
        cal.select_day(GLib.DateTime.new_local(
            mtime.year, mtime.month, mtime.day, 0, 0, 0))
        vbox.append(cal)

        # Sélecteur d'heure
        time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        time_box.set_halign(Gtk.Align.CENTER)
        time_box.set_margin_bottom(12)

        time_lbl = Gtk.Label(label="Heure :")
        time_lbl.add_css_class("section-label"); time_box.append(time_lbl)

        spin_h = Gtk.SpinButton.new_with_range(0, 23, 1)
        spin_h.set_value(mtime.hour); spin_h.set_width_chars(2)
        time_box.append(spin_h)
        time_box.append(Gtk.Label(label=":"))
        spin_m = Gtk.SpinButton.new_with_range(0, 59, 1)
        spin_m.set_value(mtime.minute); spin_m.set_width_chars(2)
        time_box.append(spin_m)
        vbox.append(time_box)

        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Boutons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8); btn_box.set_margin_bottom(8)
        btn_box.set_margin_end(12)

        btn_cancel = Gtk.Button(label="Annuler")
        btn_cancel.connect("clicked", lambda _: win.destroy())
        btn_box.append(btn_cancel)

        btn_ok = Gtk.Button(label="Appliquer")
        btn_ok.add_css_class("lt-fix-btn")
        btn_ok.connect("clicked", self._on_apply_date,
                       filepath, cal, spin_h, spin_m, win)
        btn_box.append(btn_ok)
        vbox.append(btn_box)

        win.present()

    def _on_apply_date(self, _, filepath, cal, spin_h, spin_m, win):
        """Applique la date sélectionnée au fichier (mtime) et dans le front matter."""
        # Récupérer la date du calendrier
        gdt  = cal.get_date()
        year = gdt.get_year()
        month = gdt.get_month()
        day   = gdt.get_day_of_month()
        hour  = int(spin_h.get_value())
        minute = int(spin_m.get_value())

        new_dt = datetime(year, month, day, hour, minute, 0)
        import os, time
        try:
            # 1. Modifier le mtime du fichier
            ts = new_dt.timestamp()
            os.utime(str(filepath), (ts, ts))

            # 2. Si front matter Hugo, mettre à jour le champ 'date:'
            content = filepath.read_text(encoding='utf-8')
            if content.startswith('---'):
                iso = new_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                import re
                content = re.sub(
                    r'^date:.*$', 'date: ' + iso,
                    content, flags=re.MULTILINE)
                filepath.write_text(content, encoding='utf-8')
                # Recharger si c'est la note courante
                if self._current_file == filepath:
                    self._loading_file = True
                    self._buffer.set_text(content)
                    self._loading_file = False

            win.destroy()
            self._refresh_file_list()
            self._set_status(
                'Date modifiee : ' + new_dt.strftime('%d/%m/%Y %H:%M'), 'ok')
        except Exception as ex:
            self._set_status('Erreur : ' + str(ex), 'err')

    # ── Épingler / Favoris ────────────────────────────────────────────────

    def _on_toggle_pin(self, _, filepath, popover):
        popover.popdown(); popover.unparent(); self._file_menu_popover = None
        self._db.set_pinned(filepath, not self._db.is_pinned(filepath))
        self._refresh_file_list()
        self._set_status('Epingle : ' + filepath.stem if self._db.is_pinned(filepath)
                         else 'Desepingle : ' + filepath.stem, 'ok')

    def _on_toggle_favorite(self, _, filepath, popover):
        popover.popdown(); popover.unparent(); self._file_menu_popover = None
        self._db.set_favorite(filepath, not self._db.is_favorite(filepath))
        self._refresh_file_list()
        self._set_status('Favori ajoute : ' + filepath.stem if self._db.is_favorite(filepath)
                         else 'Favori retire : ' + filepath.stem, 'ok')

    def _on_show_favorites(self, _):
        """Affiche uniquement les notes favorites dans le panneau fichiers."""
        favs = self._db.get_favorite_paths()
        all_files = self._list_notes()
        fav_files = [f for f in all_files if f in favs]
        if not fav_files:
            self._set_status('Aucun favori', 'err'); return
        # Ouvrir une fenêtre dédiée
        win = Gtk.ApplicationWindow(application=self.get_application())
        win.set_title('Favoris'); win.set_transient_for(self)
        win.set_default_size(400, 500); win.set_modal(False)
        win.set_show_menubar(False)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(vbox)
        bar = Gtk.Box(); bar.add_css_class('panel-bar')
        lbl = Gtk.Label(label='  ★ Favoris (' + str(len(fav_files)) + ')')
        lbl.add_css_class('panel-label-tags'); lbl.set_xalign(0); bar.append(lbl)
        vbox.append(bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        scroll = Gtk.ScrolledWindow(); scroll.set_vexpand(True)
        lb = Gtk.ListBox(); lb.add_css_class('files-listbox')
        lb.set_show_separators(True)
        for f in fav_files:
            row = Gtk.ListBoxRow(); row._fav_path = f
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            b.set_margin_start(10); b.set_margin_end(10)
            b.set_margin_top(6); b.set_margin_bottom(6)
            nl = Gtk.Label(label='★  ' + f.stem); nl.set_xalign(0)
            nl.add_css_class('file-name'); b.append(nl)
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            dl = Gtk.Label(label=mtime.strftime('%d/%m/%Y %H:%M'))
            dl.add_css_class('file-date'); dl.set_xalign(0); b.append(dl)
            row.set_child(b); lb.append(row)
        def _fav_activated(listbox, row):
            if not hasattr(row, '_fav_path'): return
            p = row._fav_path
            try:
                self._loading_file = True
                self._buffer.set_text(p.read_text(encoding='utf-8'))
                self._loading_file = False
                self._current_file = p
                self._unsaved_files.discard(p)
                self._update_title(); self._refresh_file_list()
                self._set_status('Ouvert : ' + p.name, 'ok')
            except Exception as ex: self._set_status(str(ex), 'err')
        lb.connect('row-activated', _fav_activated)
        scroll.set_child(lb); vbox.append(scroll)
        win.present()

    # ── Corbeille ─────────────────────────────────────────────────────────

    def _get_note_images(self, filepath):
        """Retourne la liste des Path d'images référencées dans une note
        qui se trouvent dans le répertoire data/ voisin."""
        try:
            text = filepath.read_text(encoding='utf-8')
        except Exception:
            return []
        data_dir = self._notes_dir() / 'data'
        imgs = []
        for m in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', text):
            p = m.group(1)
            # Nettoyer file:// prefix
            for pfx in ('file:///', 'file://'):
                if p.startswith(pfx): p = p[len(pfx):]; break
            img_path = Path(p)
            # Ne supprimer que les images dans notre répertoire data/
            try:
                img_path.relative_to(data_dir)
                if img_path.exists():
                    imgs.append(img_path)
            except ValueError:
                pass  # image externe, on ne touche pas
        return imgs

    def _on_trash_files(self, _, filepaths, popover):
        popover.popdown(); popover.unparent(); self._file_menu_popover = None
        trash_dir = self._notes_dir() / '.trash'
        trash_dir.mkdir(exist_ok=True)
        for fp in filepaths:
            try:
                dest = trash_dir / fp.name
                fp.rename(dest)
                self._db.trash_note(dest)
                self._db._con.execute(
                    'UPDATE note_tags SET note_path=? WHERE note_path=?',
                    (str(dest), str(fp)))
                self._db._con.commit()
                if self._current_file == fp:
                    self._current_file = None
                    self._loading_file = True
                    self._buffer.set_text('')
                    self._loading_file = False
                    self._update_title()
                    # Vider la preview immédiatement
                    self._webview.load_html(
                        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                        '<style>body{background:#0d0d14;margin:0;padding:0;}</style>'
                        '</head><body></body></html>', 'file:///')
            except Exception as ex:
                self._set_status('Erreur corbeille : ' + str(ex), 'err')
        self._refresh_file_list()
        self._set_status(str(len(filepaths)) + ' note(s) mise(s) a la corbeille', 'ok')

    def _on_show_trash(self, _):
        trashed = self._db.get_trashed()
        win = Gtk.ApplicationWindow(application=self.get_application())
        win.set_title('Corbeille'); win.set_transient_for(self)
        win.set_default_size(460, 520); win.set_modal(False)
        win.set_show_menubar(False)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(vbox)
        bar = Gtk.Box(); bar.add_css_class('panel-bar')
        lbl = Gtk.Label(label='  Corbeille (' + str(len(trashed)) + ')')
        lbl.add_css_class('panel-label-files'); lbl.set_xalign(0)
        lbl.set_hexpand(True); bar.append(lbl)
        btn_empty = Gtk.Button(label='Vider')
        btn_empty.add_css_class('file-delete-btn')
        btn_empty.connect('clicked', self._on_empty_trash, win)
        bar.append(btn_empty); vbox.append(bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        scroll = Gtk.ScrolledWindow(); scroll.set_vexpand(True)
        lb = Gtk.ListBox(); lb.add_css_class('files-listbox')
        lb.set_show_separators(True)
        if not trashed:
            r = Gtk.ListBoxRow(); l = Gtk.Label(label='  Corbeille vide')
            l.add_css_class('file-date'); r.set_child(l); lb.append(r)
        for note_path, trash_date in trashed:
            fp = Path(note_path)
            if not fp.exists(): continue
            row = Gtk.ListBoxRow(); row._trash_path = fp
            b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            b.set_margin_start(10); b.set_margin_end(8)
            b.set_margin_top(6); b.set_margin_bottom(6)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            nl = Gtk.Label(label=fp.stem); nl.set_xalign(0)
            nl.add_css_class('file-name'); info.append(nl)
            dl = Gtk.Label(label='Supprime le ' + (trash_date[:10] if trash_date else '?'))
            dl.add_css_class('file-date'); dl.set_xalign(0); info.append(dl)
            b.append(info)
            btn_restore = Gtk.Button(label='Restaurer')
            btn_restore.add_css_class('lt-fix-btn')
            btn_restore.connect('clicked', self._on_restore_from_trash, fp, win)
            b.append(btn_restore)
            btn_del = Gtk.Button(icon_name='user-trash-symbolic')
            btn_del.add_css_class('panel-icon-btn')
            btn_del.connect('clicked', self._on_delete_permanently, fp, win)
            b.append(btn_del)
            row.set_child(b); lb.append(row)
        scroll.set_child(lb); vbox.append(scroll)
        win.present()

    def _on_restore_from_trash(self, _, fp, win):
        try:
            dest = self._notes_dir() / fp.name
            fp.rename(dest)
            self._db.restore_note(dest)
            self._db._con.execute('UPDATE note_tags SET note_path=? WHERE note_path=?',
                                  (str(dest), str(fp)))
            self._db._con.commit()
            self._refresh_file_list()
            self._set_status('Restaure : ' + fp.stem, 'ok')
            win.destroy(); self._on_show_trash(None)
        except Exception as ex: self._set_status(str(ex), 'err')

    def _on_delete_permanently(self, _, fp, win):
        dialog = Gtk.AlertDialog()
        dialog.set_message('Supprimer definitivement ?')
        dialog.set_detail(fp.name)
        dialog.set_buttons(['Annuler', 'Supprimer'])
        dialog.set_cancel_button(0); dialog.set_default_button(0)
        dialog.set_modal(True)
        def on_r(d, res):
            try:
                if d.choose_finish(res) != 1: return
                # Supprimer les images du dossier data/
                for img in self._get_note_images(fp):
                    try: img.unlink()
                    except Exception: pass
                fp.unlink(missing_ok=True)
                self._db._con.execute('DELETE FROM note_meta WHERE note_path=?', (str(fp),))
                self._db._con.execute('DELETE FROM note_tags WHERE note_path=?', (str(fp),))
                self._db._con.commit()
                self._set_status('Supprime : ' + fp.stem, 'ok')
                win.destroy(); self._on_show_trash(None)
            except Exception as ex: self._set_status(str(ex), 'err')
        dialog.choose(self, None, on_r)

    def _on_empty_trash(self, _, win):
        dialog = Gtk.AlertDialog()
        dialog.set_message('Vider la corbeille ?')
        dialog.set_detail('Cette action est irreversible.')
        dialog.set_buttons(['Annuler', 'Vider'])
        dialog.set_cancel_button(0); dialog.set_default_button(0)
        dialog.set_modal(True)
        def on_r(d, res):
            try:
                if d.choose_finish(res) != 1: return
                for note_path, _ in self._db.get_trashed():
                    fp = Path(note_path)
                    fp.unlink(missing_ok=True)
                    self._db._con.execute('DELETE FROM note_meta WHERE note_path=?', (str(fp),))
                    self._db._con.execute('DELETE FROM note_tags WHERE note_path=?', (str(fp),))
                self._db._con.commit()
                self._set_status('Corbeille videe', 'ok')
                win.destroy()
            except Exception as ex: self._set_status(str(ex), 'err')
        dialog.choose(self, None, on_r)

    # ── Menu Export ──────────────────────────────────────────────────────

    def _on_show_export_menu(self, btn):
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        popover = Gtk.Popover()
        popover.set_parent(btn)
        popover.set_has_arrow(True); popover.set_autohide(True)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hdr = Gtk.Box(); hdr.add_css_class('lt-popup-header')
        lbl = Gtk.Label(label='Exporter la note')
        lbl.add_css_class('lt-popup-msg'); lbl.set_xalign(0); hdr.append(lbl)
        vbox.append(hdr)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.add_css_class('lt-popup-fixes')
        formats = [
            ('document-print-symbolic',    'PDF',      'Fond blanc, mise en page A4', self._on_export_pdf),
            ('x-office-document-symbolic', 'ODT',      'LibreOffice Writer',          self._on_export_odt),
            ('text-x-script-symbolic',     'LaTeX',    'Document LaTeX (.tex)',        self._on_export_latex),
            ('text-html-symbolic',         'HTML',     'Page web autonome (.html)',    self._on_export_html),
            ('text-x-generic-symbolic',    'Markdown', 'Copie du fichier .md',         self._on_export_md),
        ]
        for icon, label, desc, cb in formats:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_start(4); row.set_margin_end(8)
            row.set_margin_top(3); row.set_margin_bottom(3)
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(18); row.append(img)
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            tb.set_hexpand(True)
            lf = Gtk.Label(label=label); lf.set_xalign(0)
            lf.add_css_class('lt-popup-msg'); tb.append(lf)
            ld = Gtk.Label(label=desc); ld.set_xalign(0)
            ld.add_css_class('lt-popup-cat'); tb.append(ld)
            row.append(tb)
            br = Gtk.Button(); br.set_child(row)
            br.add_css_class('tmpl-btn'); br.set_halign(Gtk.Align.FILL)
            br.connect('clicked', lambda _, c=cb, p=popover: (p.popdown(), c(None)))
            inner.append(br)
        vbox.append(inner)
        popover.set_child(vbox); popover.popup()

    # ── Export PDF ────────────────────────────────────────────────────────

    # ── Export PDF ────────────────────────────────────────────────────────

    def _on_export_pdf(self, _):
        """Exporte la note courante en PDF (fond blanc, sans étiquettes)."""
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en PDF')
        dialog.set_initial_name(self._current_file.stem + '.pdf')
        f = Gtk.FileFilter(); f.set_name('PDF'); f.add_pattern('*.pdf')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_pdf_done)

    def _on_export_pdf_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        pdf_path = Path(file.get_path())
        if not pdf_path.suffix: pdf_path = pdf_path.with_suffix('.pdf')

        # Générer le HTML propre (fond blanc, sans étiquettes)
        html = md_to_html_print(self._get_text())

        import shutil, tempfile

        # ── Méthode 1 : wkhtmltopdf ──────────────────────────────────────
        if shutil.which('wkhtmltopdf'):
            try:
                with tempfile.NamedTemporaryFile(
                        mode='w', suffix='.html', delete=False,
                        encoding='utf-8') as tmp:
                    tmp.write(html); tmp_html = tmp.name
                # Ajouter --allow pour les répertoires des images
                allow_dirs = set()
                allow_dirs.add(str(self._notes_dir()))
                allow_dirs.add(str(self._notes_dir() / 'data'))
                allow_args = []
                for d in allow_dirs:
                    allow_args += ['--allow', d]
                r = subprocess.run(
                    ['wkhtmltopdf',
                     '--encoding', 'utf-8',
                     '--page-size', 'A4',
                     '--margin-top',    '8mm',
                     '--margin-bottom', '14mm',
                     '--margin-left',   '8mm',
                     '--margin-right',  '8mm',
                     '--enable-local-file-access',
                     '--load-error-handling', 'ignore',
                     '--quiet',
                     ] + allow_args + [tmp_html, str(pdf_path)],
                    capture_output=True, text=True, timeout=60)
                Path(tmp_html).unlink(missing_ok=True)
                if r.returncode == 0 and pdf_path.exists():
                    self._set_status('PDF exporte : ' + pdf_path.name, 'ok')
                    return
                raise Exception(r.stderr.strip() or 'wkhtmltopdf erreur')
            except Exception as ex:
                self._set_status('wkhtmltopdf : ' + str(ex) + ' — essai weasyprint...', 'busy')

        # ── Méthode 2 : weasyprint ────────────────────────────────────────
        try:
            import weasyprint
            weasyprint.HTML(string=html).write_pdf(str(pdf_path))
            self._set_status('PDF exporte (weasyprint) : ' + pdf_path.name, 'ok')
            return
        except ImportError:
            pass
        except Exception as ex:
            self._set_status('weasyprint : ' + str(ex), 'err'); return

        # ── Méthode 3 : WebKit sur WebView temporaire ────────────────────
        self._set_status('Export PDF via WebKit...', 'busy')
        tmp_view = WebKit.WebView()
        self._pdf_tmp_view = tmp_view  # garder la référence
        self._pdf_path     = pdf_path

        def _on_load(view, load_event):
            if int(load_event) != 3: return
            try:
                op = WebKit.PrintOperation.new(view)
                ps = Gtk.PrintSettings.new()
                ps.set(Gtk.PRINT_SETTINGS_PRINTER, 'Print to File')
                ps.set(Gtk.PRINT_SETTINGS_OUTPUT_FORMAT, 'pdf')
                ps.set(Gtk.PRINT_SETTINGS_OUTPUT_URI, pdf_path.as_uri())
                op.set_print_settings(ps)
                def _done(op):
                    GLib.idle_add(self._set_status,
                        'PDF exporte : ' + pdf_path.name, 'ok')
                    self._pdf_tmp_view = None
                try: op.connect('finished', _done)
                except Exception: pass
                op.print_()
            except Exception as ex:
                GLib.idle_add(self._set_status, 'Erreur PDF : ' + str(ex), 'err')

        tmp_view.connect('load-changed', _on_load)
        # base_uri = répertoire des notes pour que les chemins relatifs/absolus fonctionnent
        base_uri = 'file://' + str(self._notes_dir()) + '/'
        tmp_view.load_html(html, base_uri)

    # ── Export ODT        # ── Export ODT ───────────────────────────────────────────────────────

    def _on_export_odt(self, _):
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en ODT')
        dialog.set_initial_name(self._current_file.stem + '.odt')
        f = Gtk.FileFilter(); f.set_name('Document ODT'); f.add_pattern('*.odt')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_odt_done)

    def _on_export_odt_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        odt_path = Path(file.get_path())
        if not odt_path.suffix: odt_path = odt_path.with_suffix('.odt')

        self._set_status('Export ODT en cours...', 'busy')
        if subprocess.run(['which', 'pandoc'], capture_output=True).returncode != 0:
            self._set_status('pandoc non installé', 'err'); return

        try:
            import tempfile as _tf, zipfile as _zf

            text = self._get_text()
            text = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()
            text = replace_emojis(text)

            with _tf.NamedTemporaryFile(mode='w', suffix='.md',
                                         delete=False, encoding='utf-8') as tf:
                tf.write(text); tmp_md = tf.name
            with _tf.NamedTemporaryFile(suffix='.odt', delete=False) as tf:
                tmp_odt = tf.name

            r = subprocess.run(['pandoc', tmp_md, '-f', 'markdown', '-t', 'odt',
                                '-o', tmp_odt, '--standalone'],
                               capture_output=True, text=True, timeout=30)
            Path(tmp_md).unlink(missing_ok=True)
            if r.returncode != 0: raise Exception(r.stderr)

            # Patcher l'ODT : bordures tableaux + police sans-serif
            with _tf.TemporaryDirectory() as tmpdir:
                with _zf.ZipFile(tmp_odt) as z:
                    z.extractall(tmpdir)
                Path(tmp_odt).unlink(missing_ok=True)

                # content.xml : bordures sur les cellules tableau
                content_path = Path(tmpdir) / 'content.xml'
                if content_path.exists():
                    c = content_path.read_text(encoding='utf-8')
                    # Bordures sur toutes les cellules
                    c = c.replace(
                        '<style:table-cell-properties fo:border="none" />',
                        '<style:table-cell-properties fo:border="0.05pt solid #999999"'
                        ' fo:padding="0.1cm"/>')
                    # Fond gris sur l'en-tête
                    c = c.replace(
                        'style:name="TableHeaderRowCell" style:family="table-cell">'
                        '\n      <style:table-cell-properties fo:border="0.05pt solid #999999"'
                        ' fo:padding="0.1cm"/>',
                        'style:name="TableHeaderRowCell" style:family="table-cell">'
                        '\n      <style:table-cell-properties fo:border="0.05pt solid #999999"'
                        ' fo:padding="0.1cm" fo:background-color="#eeeeee"/>')
                    content_path.write_text(c, encoding='utf-8')

                # styles.xml : police Liberation Sans + marges page
                styles_path = Path(tmpdir) / 'styles.xml'
                if styles_path.exists():
                    s = styles_path.read_text(encoding='utf-8')
                    s = s.replace('fo:font-family="Liberation Serif"',
                                  'fo:font-family="Liberation Sans"')
                    s = s.replace("fo:font-family='Liberation Serif'",
                                  "fo:font-family='Liberation Sans'")
                    styles_path.write_text(s, encoding='utf-8')

                # Rezipper
                with _zf.ZipFile(str(odt_path), 'w', _zf.ZIP_DEFLATED) as z:
                    mime = Path(tmpdir) / 'mimetype'
                    if mime.exists():
                        z.write(mime, 'mimetype', compress_type=_zf.ZIP_STORED)
                    for f in Path(tmpdir).rglob('*'):
                        if f.is_file() and f.name != 'mimetype':
                            z.write(f, f.relative_to(tmpdir))

            self._set_status('ODT exporté : ' + odt_path.name, 'ok')
        except Exception as ex:
            self._set_status('Erreur ODT : ' + str(ex), 'err')

    def _make_odt_html(self):
        """
        Génère un HTML A4 avec images redimensionnées en base64.
        LibreOffice --convert-to odt respecte les largeurs CSS.
        """
        import base64
        try: from PIL import Image as PILImage; _pil = True
        except ImportError: _pil = False

        text  = self._get_text()
        clean = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()

        # Redimensionner et encoder les images en base64
        MAX_PX = 1200  # largeur cible en pixels pour A4 @ 150dpi

        def img_to_b64(path_str):
            p = path_str
            for pfx in ('file:///', 'file://'): 
                if p.startswith(pfx): p = p[len(pfx):]; break
            img_path = Path(p)
            if not img_path.exists(): return None, None
            if _pil:
                try:
                    with PILImage.open(str(img_path)) as im:
                        if im.mode not in ('RGB', 'RGBA'): im = im.convert('RGB')
                        pw2, ph2 = im.width, im.height
                        MAX_H2 = int(MAX_PX * (297 - 50) / (210 - 50))  # ratio A4
                        scale2 = min(MAX_PX / pw2, MAX_H2 / ph2) if ph2 > 0 else 1
                        if scale2 < 1:
                            im = im.resize((int(pw2*scale2), int(ph2*scale2)), PILImage.LANCZOS)
                        import io
                        buf = io.BytesIO()
                        im.save(buf, format='PNG')
                        return base64.b64encode(buf.getvalue()).decode(), 'image/png'
                except Exception: pass
            # Sans PIL : lire raw
            try:
                import mimetypes
                data = img_path.read_bytes()
                mime = mimetypes.guess_type(str(img_path))[0] or 'image/png'
                return base64.b64encode(data).decode(), mime
            except Exception: return None, None

        # Remplacer ![alt](path) par <img> avec base64 et width:100%
        def replace_img(m):
            alt, path = m.group(1), m.group(2)
            b64, mime = img_to_b64(path)
            if not b64: return f'[image manquante: {alt}]'
            return (f'![{alt}](data:{mime};base64,{b64})')

        md_with_b64 = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_img, clean)

        try:    body = markdown.markdown(md_with_b64, extensions=MD_EXT, extension_configs=MD_CFG)
        except: body = markdown.markdown(md_with_b64)

        # CSS : images à 100% de la zone de texte
        css = '''
        @page { size: A4; margin: 2.5cm; }
        body { font-family: Georgia, serif; font-size: 12pt; line-height: 1.6;
               color: #1a1a1a; background: white; margin: 0; padding: 0; }
        img { width: 100%; max-width: 100%; height: auto;
              display: block; margin: 0.5cm auto; }
        h1 { font-size: 2em; border-bottom: 2px solid #ccc; }
        h2 { font-size: 1.5em; } h3 { font-size: 1.2em; }
        code { font-family: monospace; background: #f4f4f4; padding: 1px 4px; }
        pre  { background: #f4f4f4; padding: 10px; white-space: pre-wrap; }
        blockquote { border-left: 3px solid #ccc; margin-left: 0; padding-left: 1em; }
        '''
        return (f'<!DOCTYPE html><html lang="fr"><head>'
                f'<meta charset="UTF-8">'
                f'<style>{css}</style></head><body>{body}</body></html>')

    def _write_odt_native(self, odt_path):
        """
        ODT natif avec images scalées physiquement (validé par test LO).
        605px @ 96dpi = 16.007cm = zone texte A4 avec marges 2.5cm.
        Anchor as-char : LO respecte svg:width/height quand l'image a la bonne DPI.
        """
        import zipfile as zf, io
        try: from PIL import Image as PILImage; _pil = True
        except ImportError: _pil = False

        text  = self._get_text()
        clean = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()

        DPI      = 96
        TARGET_W = int((210 - 50) / 25.4 * DPI)  # 604px = 160mm @ 96dpi
        images   = {}
        img_idx  = [0]

        def scale_img(path_str):
            p = path_str
            for pfx in ('file:///', 'file://'):
                if p.startswith(pfx): p = p[len(pfx):]; break
            fp = Path(p)
            if not fp.exists(): return None
            img_idx[0] += 1
            name = 'Pictures/img' + str(img_idx[0]) + '.png'
            if _pil:
                try:
                    with PILImage.open(str(fp)) as im:
                        if im.mode not in ('RGB', 'RGBA'): im = im.convert('RGB')
                        pw, ph = im.width, im.height
                        # Contraindre largeur ET hauteur
                        # MAX_H = A4 zone texte hauteur : 297-50=247mm
                        MAX_H = int((297 - 50) / 25.4 * DPI)  # ~934px
                        # Calculer le scale en respectant les deux limites
                        scale = min(TARGET_W / pw, MAX_H / ph) if ph > 0 else TARGET_W / pw
                        nw = int(pw * scale)
                        nh = int(ph * scale)
                        scaled = im.resize((nw, nh), PILImage.LANCZOS)
                        buf = io.BytesIO()
                        scaled.save(buf, format='PNG', dpi=(DPI, DPI))
                        images[name] = buf.getvalue()
                        w_s = '{:.4f}cm'.format(nw / DPI * 2.54)
                        h_s = '{:.4f}cm'.format(nh / DPI * 2.54)
                        return name, w_s, h_s
                except Exception as e:
                    print('[ODT] PIL:', e)
            try:
                images[name] = fp.read_bytes()
                return name, '16.0cm', '9.0cm'
            except Exception:
                return None

        def esc(s):
            return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

        def frame(alt, path_str):
            res = scale_img(path_str)
            if not res: return esc(alt or 'image')
            nm, ws, hs = res
            return ('<draw:frame text:anchor-type="as-char"'
                    ' svg:width="' + ws + '" svg:height="' + hs + '">'
                    '<draw:image xlink:href="' + nm + '"'
                    ' xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>'
                    '</draw:frame>')

        def proc_img(line):
            return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',
                lambda m: frame(m.group(1), m.group(2)), line)

        def inline(line):
            line = esc(line)
            line = re.sub(r'\*\*(.+?)\*\*', r'<text:span text:style-name="Bold">\1</text:span>', line)
            line = re.sub(r'\*(.+?)\*',     r'<text:span text:style-name="Italic">\1</text:span>', line)
            line = re.sub(r'`([^`]+)`',     r'<text:span text:style-name="Code">\1</text:span>', line)
            line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)
            return line

        def proc(line):
            return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',
                lambda m: frame(m.group(1), m.group(2)), inline(line))

        paras = []; in_code = False; code_buf = []
        table_buf = []

        extra_auto_styles = []
        tbl_counter = [0]

        def flush_table(rows):
            data = [r for r in rows if not re.match(r'^\|[-:| ]+\|\s*$', r)]
            if not data: return ''
            header = data[0]
            body_rows = data[1:]
            cols = [c.strip() for c in header.strip().strip('|').split('|')]
            n = max(1, len(cols))
            col_w = round(16.0 / n, 3)
            tbl_counter[0] += 1
            tid = tbl_counter[0]
            for i in range(n):
                extra_auto_styles.append(
                    f'<style:style style:name="MDCol{tid}c{i}" style:family="table-column">'
                    f'<style:table-column-properties style:column-width="{col_w}cm"/>'
                    f'</style:style>')
            col_tags = ''.join(
                f'<table:table-column table:style-name="MDCol{tid}c{i}"/>'
                for i in range(n))
            def make_cells(cells, hdr):
                cs = 'MDTableHdrCell' if hdr else 'MDTableCell'
                ps = 'MDTableHdrPara' if hdr else 'MDTablePara'
                xml = ''
                for i in range(n):
                    txt = esc(cells[i].strip()) if i < len(cells) else ''
                    xml += (f'<table:table-cell table:style-name="{cs}"'
                            f' office:value-type="string">'
                            f'<text:p text:style-name="{ps}">{txt}</text:p>'
                            f'</table:table-cell>')
                return xml
            hdr_cells = [c.strip() for c in header.strip().strip('|').split('|')]
            rows_xml = (f'<table:table-header-rows>'
                        f'<table:table-row>{make_cells(hdr_cells, True)}</table:table-row>'
                        f'</table:table-header-rows>')
            for r in body_rows:
                cells = [c.strip() for c in r.strip().strip('|').split('|')]
                rows_xml += f'<table:table-row>{make_cells(cells, False)}</table:table-row>'
            return (f'<table:table table:name="T{tid}" table:style-name="MDTable">'
                    f'{col_tags}{rows_xml}</table:table>')


        for line in clean.splitlines():
            if line.strip().startswith('|') and '|' in line.strip()[1:]:
                table_buf.append(line)
                continue
            elif table_buf:
                paras.append(flush_table(table_buf))
                table_buf = []

            if line.startswith('```') or line.startswith('~~~'):
                if in_code:
                    paras.append('<text:p text:style-name="Preformatted Text">'
                        + esc('\n'.join(code_buf)) + '</text:p>')
                    code_buf = []; in_code = False
                else: in_code = True
                continue
            if in_code: code_buf.append(line); continue
            # Image seule sur sa ligne
            im = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', line.strip())
            if im:
                paras.append('<text:p text:style-name="ImgPara">'
                    + frame(im.group(1), im.group(2)) + '</text:p>'); continue
            hm = re.match(r'^(#{1,4}) (.+)', line)
            if hm:
                paras.append('<text:h text:style-name="Heading '
                    + str(len(hm.group(1))) + '">' + esc(hm.group(2)) + '</text:h>')
            elif line.startswith('> '):
                paras.append('<text:p text:style-name="Quotations">' + proc(line[2:]) + '</text:p>')
            elif re.match(r'^\s*[-*+] ', line):
                paras.append('<text:p text:style-name="List Bullet">'
                    + proc(line.lstrip('-*+ ').strip()) + '</text:p>')
            elif line.strip():
                paras.append('<text:p text:style-name="Text Body">' + proc(line) + '</text:p>')
            else:
                paras.append('<text:p text:style-name="Text Body"/>')

        if table_buf:
            paras.append(flush_table(table_buf))
            table_buf = []

        manifest_img = ''.join(
            '<manifest:file-entry manifest:full-path="' + n + '" manifest:media-type="image/png"/>'
            for n in images)
        manifest = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            + manifest_img + '</manifest:manifest>')

        content = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document-content'
            ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
            ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
            ' xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"'
            ' xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'
            ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink"'
            ' xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"'
            ' office:version="1.3">'
            '<office:automatic-styles>'
            '<style:style style:name="Bold" style:family="text">'
            '<style:text-properties fo:font-weight="bold"/></style:style>'
            '<style:style style:name="Italic" style:family="text">'
            '<style:text-properties fo:font-style="italic"/></style:style>'
            '<style:style style:name="Code" style:family="text">'
            '<style:text-properties style:font-name="Courier New"/></style:style>'
            '<style:style style:name="ImgPara" style:family="paragraph">'
            '<style:paragraph-properties fo:text-align="center"/></style:style>'
            '<style:style style:name="MDTableCell" style:family="table-cell">'
            '<style:table-cell-properties fo:border="0.05pt solid #aaaaaa" fo:padding="0.1cm"/>'
            '</style:style>'
            '<style:style style:name="MDTableHdrCell" style:family="table-cell">'
            '<style:table-cell-properties fo:border="0.05pt solid #aaaaaa" fo:padding="0.1cm"'
            ' fo:background-color="#eeeeee"/>'
            '</style:style>'
            '<style:style style:name="MDTablePara" style:family="paragraph">'
            '<style:text-properties fo:font-size="8.5pt"/>'
            '</style:style>'
            '<style:style style:name="MDTableHdrPara" style:family="paragraph">'
            '<style:text-properties fo:font-size="8.5pt" fo:font-weight="bold"/>'
            '</style:style>'
            '<style:style style:name="MDTable" style:family="table">'
            '<style:table-properties table:align="margins" style:width="16.0cm"/>'
            '</style:style>'
            '</office:automatic-styles>'
            + ''.join(extra_auto_styles)
            + '<office:body><office:text>'
            + '\n'.join(paras)
            + '</office:text></office:body></office:document-content>')

        with zf.ZipFile(str(odt_path), 'w', zf.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/vnd.oasis.opendocument.text', zf.ZIP_STORED)
            z.writestr('META-INF/manifest.xml', manifest)
            z.writestr('content.xml', content)
            for name, data in images.items():
                z.writestr(name, data)


    # ── Export LaTeX ─────────────────────────────────────────────────────

    def _on_export_latex(self, _):
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exporter en LaTeX')
        dialog.set_initial_name(self._current_file.stem + '.tex')
        f = Gtk.FileFilter(); f.set_name('LaTeX'); f.add_pattern('*.tex')
        store = Gio.ListStore.new(Gtk.FileFilter); store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._on_export_latex_done)

    def _on_export_latex_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except Exception: return
        if not file: return
        tex_path = Path(file.get_path())
        if not tex_path.suffix: tex_path = tex_path.with_suffix('.tex')

        self._set_status('Export LaTeX en cours...', 'busy')
        if subprocess.run(['which', 'pandoc'], capture_output=True).returncode != 0:
            self._set_status('pandoc non installé', 'err'); return
        try:
            import tempfile as _tf
            text = self._get_text()
            text = re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()
            text = replace_emojis(text)
            m = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
            doc_title = re.sub(r'[*_`]', '', m.group(1)).strip() if m else (
                self._current_file.stem if self._current_file else 'Note')
            dtl = doc_title.replace('&', r'\&').replace('%', r'\%').replace('#', r'\#')

            with _tf.NamedTemporaryFile(mode='w', suffix='.md',
                                         delete=False, encoding='utf-8') as tf:
                tf.write(text); tmp_md = tf.name
            r = subprocess.run(['pandoc', tmp_md, '-f', 'markdown', '-t', 'latex',
                                '--standalone', '--no-highlight', '--pdf-engine=xelatex'],
                               capture_output=True, timeout=30,
                               env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8', 'LANG': 'fr_FR.UTF-8'})
            Path(tmp_md).unlink(missing_ok=True)
            if r.returncode != 0: raise Exception(r.stderr.decode('utf-8', errors='replace'))
            latex = r.stdout.decode('utf-8')

            import re as _rl
            old_doc = _rl.search(r'\\documentclass.*?\\begin\{document\}', latex, _rl.DOTALL)
            if old_doc:
                preamble = (
                    r'\documentclass[11pt,a4paper]{article}' + '\n'
                    r'\usepackage{fontspec}\usepackage{unicode-math}' + '\n'
                    r'\setmainfont{Liberation Sans}' + '\n'
                    r'\setmonofont{Liberation Mono}[Scale=0.85]' + '\n'
                    r'\usepackage[a4paper,top=15mm,bottom=20mm,left=12mm,right=12mm]{geometry}' + '\n'
                    r'\usepackage{fancyhdr}\pagestyle{fancy}\fancyhf{}' + '\n'
                    r'\renewcommand{\headrulewidth}{0pt}' + '\n'
                    r'\renewcommand{\footrulewidth}{0.4pt}' + '\n'
                    + f'\\fancyfoot[L]{{\\small\\color{{gray}}{dtl}}}' + '\n'
                    + r'\fancyfoot[R]{\small\color{gray}\thepage\ /\ \pageref{LastPage}}' + '\n'
                    r'\usepackage{lastpage}\usepackage{xcolor}\usepackage{calc}' + '\n'
                    r'\usepackage{longtable}\usepackage{booktabs}\usepackage{array}' + '\n'
                    r'\usepackage{colortbl}\usepackage{tabularx}' + '\n'
                    r'\renewcommand{\arraystretch}{1.3}' + '\n'
                    r'\usepackage{etoolbox}' + '\n'
                    r'\makeatletter\patchcmd\longtable{\par}{\if@noskipsec\mbox{}\fi\par}{}{}' + '\n'
                    r'\makeatother' + '\n'
                    r'\usepackage{listings}' + '\n'
                    r'\lstset{basicstyle=\ttfamily\footnotesize,breaklines=true,' + '\n'
                    r'  frame=single,backgroundcolor=\color{gray!10},rulecolor=\color{gray!40}}' + '\n'
                    + r'\usepackage{graphicx}\usepackage{adjustbox}' + '\n'
                    r'\usepackage{hyperref}' + '\n'
                    r'\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue!70!black}' + '\n'
                    r'\usepackage{parskip}\setlength{\parskip}{6pt}' + '\n'
                    # titlesec supprimé : incompatible avec accents UTF-8 dans xelatex

                    + f'\\title{{{dtl}}}' + '\n'
                    + r'\author{}\date{}' + '\n'
                    + r'\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}' + '\n'
                    + r'\newcommand{\pandocbounded}[1]{\adjustbox{max width=\linewidth,max height=0.8\textheight}{#1}}' + '\n'
                    + r'\begin{document}\maketitle\thispagestyle{fancy}' + '\n'
                )
                latex = latex[:old_doc.start()] + preamble + latex[old_doc.end():]

            # Post-traitements
            # 1. Fusionner les titres coupés sur 2 lignes
            latex = _rl.sub(
                r'(\\(?:sub)*section\{[^}]*?)\n([^}]*\})',
                lambda m: m.group(1) + ' ' + m.group(2), latex)
            # 2. Supprimer \label{}
            latex = _rl.sub(r'\\label\{[^}]*\}', '', latex)
            # 3. Supprimer \hypertarget wrapper
            latex = _rl.sub(r'\\hypertarget\{[^}]*\}\{%?\n?', '', latex)
            # 4. Supprimer la } résiduelle après \section{...}}
            latex = _rl.sub(
                r'(\\(?:sub)*section\{[^}]*\})\}',
                lambda m: m.group(1), latex)
            # 5. verbatim → lstlisting pour style cohérent
            latex = latex.replace('\\begin{verbatim}', '\\begin{lstlisting}')
            latex = latex.replace('\\end{verbatim}', '\\end{lstlisting}')
            # Fallback Shaded (au cas où)
            latex = latex.replace('\\begin{Shaded}', '\\begin{lstlisting}')
            latex = latex.replace('\\end{Shaded}', '\\end{lstlisting}')
            latex = latex.replace('\\begin{Highlighting}[]', '')
            latex = latex.replace('\\end{Highlighting}', '')
            # Tokens de coloration pandoc (fallback) → texte brut
            latex = _rl.sub(
                r'\\[A-Za-z]+Tok\{([^}]*)\}',
                lambda m: m.group(1), latex)
            # 7. Images : wrapper adjustbox pour contraindre la taille
            import re as _ri
            latex = _ri.sub(
                r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}',
                lambda m: r'\adjustbox{max width=\linewidth,max height=0.8\textheight}{\includegraphics{' + m.group(1) + '}}',
                latex)

            # 7. \real{X} → X
            latex = _rl.sub(
                r'\\real\{([0-9.]+)\}',
                lambda m: '{:.4f}'.format(float(m.group(1))), latex)
            # 8. Convertir longtable pandoc → tabular propre
            def _fix_tables(ltx):
                import re as _r2
                def _repl(m):
                    full = m.group(0)
                    # Compter les colonnes (soit lll soit >{}p{} patterns)
                    simple = _r2.search(r'@\{\}([lcr]+)@\{\}', full)
                    fancy  = _r2.findall(r'>\{[^}]*\}p\{[^}]*\}', full)
                    n = len(simple.group(1)) if simple else len(fancy)
                    if n == 0: return full
                    spec = '|' + '|'.join(['l'] * n) + '|'
                    # En-tête : extraire contenu des minipage ou texte brut
                    hdr_m = _r2.search(
                        r'\\toprule[^\n]*\n(.*?)\\midrule', full, _r2.DOTALL)
                    hdr_raw = hdr_m.group(1).strip() if hdr_m else ''
                    # Supprimer \begin{minipage}...\end{minipage}
                    hdr_raw = _r2.sub(
                        r'\\begin\{minipage\}[^}]*\}[^\\]*',
                        '', hdr_raw)
                    hdr_raw = hdr_raw.replace('\\end{minipage}', '')
                    hdr_raw = hdr_raw.replace('\\raggedright', '')
                    # Extraire les cellules
                    hdr_row = hdr_raw.split('\\\\')[0].strip()
                    cells = [c.strip() for c in hdr_row.split('&')]
                    # Données
                    data_m = _r2.search(
                        r'\\endlastfoot\n(.*?)\\end\{longtable\}', full, _r2.DOTALL)
                    data = data_m.group(1).strip() if data_m else ''
                    out = ['\\begin{tabular}{' + spec + '}', '\\hline']
                    if cells and any(c for c in cells):
                        out.append(' & '.join('\\textbf{'+c+'}' for c in cells if c) + ' \\\\')
                        out.append('\\hline')
                    for row in data.split('\\\\'):
                        row = row.strip().lstrip('\n')
                        if row and '&' in row:
                            out.append(row.strip() + ' \\\\')
                            out.append('\\hline')
                    out.append('\\end{tabular}')
                    return '\n'.join(out)
                return _r2.sub(
                    r'\\begin\{longtable\}.*?\\end\{longtable\}',
                    _repl, ltx, flags=_r2.DOTALL)
            latex = _fix_tables(latex)


            tex_path.write_text(latex, encoding='utf-8')
            self._set_status('LaTeX exporté : ' + tex_path.name, 'ok')
        except Exception as ex:
            self._set_status('Erreur LaTeX : ' + str(ex), 'err')
    def _write_latex_native(self, tex_path):
        text  = self._get_text()
        clean = re.sub(r'^---\n[\s\S]*?\n---\n?','',text).strip()
        title = (self._current_file.stem if self._current_file else 'Note').replace('_',' ')
        def esc(s):
            for o,n in [('\\','\\textbackslash{}'),('&','\\&'),
                        ('%','\\%'),('$','\\$'),('#','\\#'),
                        ('{','\\{'),('}','\\}'),('~','\\textasciitilde{}'),
                        ('^','\\textasciicircum{}'),('_','\\_')]: s=s.replace(o,n)
            return s
        def inline(l):
            l=re.sub(r'\*\*(.+?)\*\*', lambda m:'\\textbf{'+esc(m.group(1))+'}',l)
            l=re.sub(r'\*(.+?)\*',    lambda m:'\\textit{'+esc(m.group(1))+'}',l)
            l=re.sub(r'`([^`]+)`',     lambda m:'\\texttt{'+esc(m.group(1))+'}',l)
            l=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                     lambda m:'\\href{'+m.group(2)+'}{'+esc(m.group(1))+'}',l)
            return l
        cmds={1:'\\section',2:'\\subsection',3:'\\subsubsection',4:'\\paragraph'}
        out=[]; in_code=False; in_list=False; cbuf=[]
        table_buf = []

        def flush_table(rows):
            """Génère un longtable LaTeX depuis des lignes Markdown."""
            data = [r for r in rows if not re.match(r'^\|[-:| ]+\|\s*$', r)]
            if not data: return ''
            header = data[0]
            body_rows = data[1:]
            cols = [c.strip() for c in header.strip().strip('|').split('|')]
            n = max(1, len(cols))
            col_spec = '|' + '|'.join(['p{' + f'{round(14.0/n, 1)}cm' + '}'] * n) + '|'
            def make_row(cells, hdr):
                row_cells = []
                for i in range(n):
                    txt = cells[i].strip() if i < len(cells) else ''
                    if hdr:
                        txt = '\\textbf{' + txt + '}'
                    row_cells.append(txt)
                return ' & '.join(row_cells) + ' \\\\'
            hdr_cells = [c.strip() for c in header.strip().strip('|').split('|')]
            lines_tex = [
                '\\begin{longtable}{' + col_spec + '}',
                '\\hline',
                make_row(hdr_cells, True),
                '\\hline',
                '\\endfirsthead',
                '\\hline',
                make_row(hdr_cells, True),
                '\\hline',
                '\\endhead',
                '\\hline',
            ]
            for r in body_rows:
                cells = [c.strip() for c in r.strip().strip('|').split('|')]
                lines_tex.append(make_row(cells, False))
                lines_tex.append('\\hline')
            lines_tex.append('\\end{longtable}')
            return '\n'.join(lines_tex)


        for line in clean.splitlines():
            if line.strip().startswith('|') and '|' in line.strip()[1:]:
                table_buf.append(line)
                continue
            elif table_buf:
                paras.append(flush_table(table_buf))
                table_buf = []

            if line.startswith('```') or line.startswith('~~~'):
                if in_code:
                    out+=['\\begin{verbatim}']+cbuf+['\\end{verbatim}']
                    cbuf=[]; in_code=False
                else: in_code=True
                continue
            if in_code: cbuf.append(line); continue
            if in_list and not re.match(r'^\s*[-*+] ',line):
                out.append('\\end{itemize}'); in_list=False
            hm=re.match(r'^(#{1,4}) (.+)',line)
            if hm: out.append(cmds.get(len(hm.group(1)),'\\paragraph')+'{'+esc(hm.group(2))+'}')
            elif line.startswith('> '): out.append('\\begin{quote}'+inline(line[2:])+'\\end{quote}')
            elif re.match(r'^\s*[-*+] ',line):
                if not in_list: out.append('\\begin{itemize}'); in_list=True
                out.append('\\item '+inline(re.sub(r'^\s*[-*+] ','',line)))
            elif re.match(r'^---+$',line.strip()): out.append('\\hrule')
            elif line.strip(): out.append(inline(line)+' \\\\')
            else: out.append('')
        if in_list: out.append('\\end{itemize}')
        doc=('\\documentclass[12pt,a4paper]{article}\n'
             '\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n'
             '\\usepackage[french]{babel}\n\\usepackage{hyperref}\n'
             '\\usepackage{geometry}\n\\geometry{margin=2.5cm}\n'
             '\\usepackage{parskip}\n'
             '\\title{'+esc(title)+'}\n\\date{\\today}\n'
             '\\begin{document}\n\\maketitle\n\n'
             +'\n'.join(out)+'\n\n\\end{document}\n')
        tex_path.write_text(doc,encoding='utf-8')

    # ── Table des matières ────────────────────────────────────────────────

    def _on_show_note_menu(self, btn):
        """Menu contextuel de la note courante via le bouton ⋮."""
        if not self._current_file:
            self._set_status('Aucune note ouverte', 'err'); return
        if self._current_file in self._unsaved_files:
            self._on_save(None)
        # Ancrer le popover directement au bouton ⋮ (parent = btn)
        self._ftm_override_widget = btn
        self._ftm_override_position = Gtk.PositionType.BOTTOM
        self._show_file_tag_menu([self._current_file], 0, 0)
        self._ftm_override_widget = None
        self._ftm_override_position = None

    def _show_file_tag_menu_on_widget(self, widget, filepaths, x, y):
        """Affiche le menu fichier attaché à widget (au lieu de self._tree_view)."""
        if hasattr(self, '_file_menu_popover') and self._file_menu_popover:
            try: self._file_menu_popover.popdown(); self._file_menu_popover.unparent()
            except Exception: pass
            self._file_menu_popover = None
        # Sauvegarder le parent d'origine et le substituer temporairement
        orig_parent_fn = self._show_file_tag_menu.__func__ if hasattr(self._show_file_tag_menu, '__func__') else None
        # Appel direct avec monkey-patch du parent du popover
        self._ftm_override_widget = widget
        self._show_file_tag_menu(filepaths, x, y)
        self._ftm_override_widget = None

    def _on_toggle_md_help(self, _=None):
        """Affiche ou ferme la fenêtre d'aide Markdown avec preview WebKit."""
        if self._md_help_window is not None:
            try: self._md_help_window.destroy()
            except Exception: pass
            self._md_help_window = None
            return

        win = Gtk.Window()
        win.set_title('Référence Markdown')
        win.set_transient_for(self)
        win.set_default_size(900, 720)
        win.set_modal(False)
        win.connect('destroy', lambda _: setattr(self, '_md_help_window', None))

        # Layout : liste à gauche | preview à droite
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        # ── Panneau gauche : liste des syntaxes ───────────────────────────
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_min_content_width(340)
        left_scroll.set_vexpand(True)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_box.set_margin_start(16); left_box.set_margin_end(16)
        left_box.set_margin_top(12); left_box.set_margin_bottom(12)

        # ── Panneau droit : preview WebKit ────────────────────────────────
        import gi
        gi.require_version('WebKit', '6.0')
        from gi.repository import WebKit as WK
        preview_web = WK.WebView()
        preview_web.set_hexpand(True)
        preview_web.set_vexpand(True)
        preview_web.set_size_request(400, -1)

        CSS_HELP = """
        body{background:#0d0d14;color:#e0e0f0;font-family:'Noto Sans',sans-serif;
             font-size:15px;padding:24px 32px;margin:0;line-height:1.7;}
        h1,h2,h3,h4{color:#89b4fa;margin:0.3em 0;}
        h1{font-size:1.8em;} h2{font-size:1.4em;} h3{font-size:1.2em;}
        code,pre{font-family:Monospace;background:#1a1a2e;color:#a6e3a1;
                 border-radius:4px;padding:2px 6px;}
        pre{padding:12px 16px;white-space:pre-wrap;}
        blockquote{border-left:4px solid #7c8cf8;margin:0;padding-left:16px;
                   color:#a0a0c0;font-style:italic;}
        a{color:#7c8cf8;}
        table{border-collapse:collapse;width:100%;}
        th,td{border:1px solid #2e2e45;padding:6px 12px;}
        th{background:#1a1a2e;color:#89b4fa;}
        strong{color:#f9e2af;} em{color:#f5c2e7;}
        del{color:#a6adc8;}
        hr{border:none;border-top:1px solid #45475a;margin:1em 0;}
        input[type=checkbox]{margin-right:6px;}
        """

        def show_preview(md_text):
            try:
                body = markdown.markdown(
                    md_text, extensions=MD_EXT, extension_configs=MD_CFG)
            except Exception:
                body = markdown.markdown(md_text)
            html = (
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<style>' + CSS_HELP + '</style></head>'
                '<body>' + body + '</body></html>'
            )
            preview_web.load_html(html, 'file:///')

        # Afficher un exemple au démarrage
        show_preview('# Bienvenue\n\nSélectionnez une syntaxe pour voir son rendu.')

        sections = [
            ('Titres', [
                ('# Titre 1',       'Titre H1',       '# Titre de niveau 1\n\nTexte sous le titre.'),
                ('## Titre 2',      'Titre H2',       '## Titre de niveau 2\n\nTexte sous le titre.'),
                ('### Titre 3',     'Titre H3',       '### Titre de niveau 3\n\nTexte sous le titre.'),
                ('#### Titre 4',    'Titre H4',       '#### Titre de niveau 4\n\nTexte sous le titre.'),
            ]),
            ('Mise en forme', [
                ('**gras**',        'Gras',           'Du texte **en gras** ici.'),
                ('*italique*',      'Italique',       'Du texte *en italique* ici.'),
                ('~~barré~~',       'Barré',          'Du texte ~~barré~~ ici.'),
                ('`code`',          'Code inline',    'Utilisez `print("hello")` en Python.'),
                ('**_gras+ital_**', 'Gras + italique','Texte **_gras et italique_** combinés.'),
            ]),
            ('Listes', [
                ('- élément',       'Liste à puces',
                 '- Premier élément\n- Deuxième élément\n  - Sous-élément indenté\n- Troisième'),
                ('1. élément',      'Liste numérotée',
                 '1. Premier\n2. Deuxième\n3. Troisième'),
                ('- [ ] tâche',     'Cases à cocher',
                 '- [x] Tâche terminée\n- [ ] Tâche en cours\n- [ ] À faire'),
                ('+ élément',       'Liste alternative',
                 '+ Alpha\n+ Beta\n+ Gamma'),
            ]),
            ('Liens & images', [
                ('[texte](url)',     'Lien simple',
                 'Visitez [Claude](https://claude.ai) pour en savoir plus.'),
                ('[texte](url "titre")', 'Lien avec titre',
                 'Voir [la doc](https://docs.anthropic.com "Documentation Anthropic").'),
                ('<url>',           'Lien automatique',
                 'Site web : <https://anthropic.com>'),
                ('![alt](url)',      'Image',
                 '![Placeholder](https://dummyimage.com/300x120/7c8cf8/fff&text=Image)'),
                ('[![alt](img)](url)', 'Image cliquable',
                 '[![Cliquez](https://dummyimage.com/200x80/89b4fa/fff&text=Clic)](https://anthropic.com)'),
            ]),
            ('Blocs', [
                ('> texte',         'Citation simple',
                 '> Ceci est une citation.\n> Elle peut s\'étendre sur plusieurs lignes.'),
                ('> > imbriqué',    'Citation imbriquée',
                 '> Niveau 1\n>\n> > Niveau 2 (imbriqué)'),
                ('```lang\n```',   'Bloc de code',
                 '```python\ndef bonjour(nom):\n    return f"Bonjour {nom} !"\n\nprint(bonjour("Claude"))\n```'),
                ('```bash\n```',   'Bloc bash',
                 '```bash\npacman -Syu\nsystemctl restart nginx\n```'),
                ('---',             'Règle horizontale',
                 'Avant la règle\n\n---\n\nAprès la règle'),
            ]),
            ('Tableau (extension)', [
                ('| Col | Col |',   'Tableau simple',
                 '| Nom | Âge | Ville |\n|-----|-----|-------|\n| Alice | 30 | Paris |\n| Bob | 25 | Lyon |'),
                ('| :--- | ---: |', 'Alignement colonnes',
                 '| Gauche | Centre | Droite |\n|:-------|:------:|-------:|\n| A | B | C |\n| texte long | centré | 42 |'),
            ]),
            ('Texte avancé', [
                ('\\*échappé\\*',  'Échapper un caractère',
                 'Afficher \\*littéralement\\* sans mise en forme.'),
                ('texte  \n',       'Saut de ligne forcé',
                 'Ligne 1  \nLigne 2 (deux espaces avant \\n)'),
                ('&nbsp;',           'Espace insécable HTML',
                 'Mot1&nbsp;&nbsp;&nbsp;Mot2 (espaces multiples)'),
                ('<mark>texte</mark>', 'Texte surligné (HTML)',
                 'Texte normal et <mark>texte surligné</mark> en HTML.'),
                ('<sub>texte</sub>', 'Exposant / indice',
                 'H<sub>2</sub>O et E=mc<sup>2</sup>'),
            ]),
            ('Table des matières (extension toc)', [
                ('[TOC]',            'Table des matières auto',
                 '[TOC]\n\n# Chapitre 1\n\n## Section 1.1\n\n## Section 1.2\n\n# Chapitre 2'),
            ]),
            ('Front matter Hugo', [
                ('---\ntitle:\n---', 'En-tête minimal',
                 '---\ntitle: "Mon article"\ndate: 2026-06-17\ndraft: false\n---\n\nContenu...'),
                ('categories:\n  -', 'Avec catégories',
                 '---\ntitle: "Tutoriel Python"\ndate: 2026-06-17\ndraft: false\ncategories:\n  - Dev\n  - Python\ntags:\n  - tutorial\n---'),
                ('slug: "url"',     'Avec slug et description',
                 '---\ntitle: "Mon article"\nslug: "mon-article"\ndescription: "Une description courte"\ndate: 2026-06-17\ndraft: false\n---'),
            ]),
            ('Raccourcis clavier', [
                ('Ctrl+S',   'Sauvegarder',        '## Raccourcis principaux\n\n- **Ctrl+S** — Sauvegarder\n- **Ctrl+B** — Gras\n- **Ctrl+I** — Italique'),
                ('Ctrl+F',   'Recherche locale',   '## Recherche\n\n- **Ctrl+F** — Dans la note courante\n- **Ctrl+G** — Dans toutes les notes'),
                ('Ctrl+Z',   'Annuler',            '## Édition\n\n- **Ctrl+Z** — Annuler\n- **Ctrl+Y** — Rétablir\n- **Tab** — Indenter'),
            ]),
        ]

        for section_title, items in sections:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(10); sep.set_margin_bottom(6)
            left_box.append(sep)
            lbl_sec = Gtk.Label(label=section_title)
            lbl_sec.set_xalign(0); lbl_sec.add_css_class('section-label')
            lbl_sec.set_margin_bottom(4); left_box.append(lbl_sec)

            for syntax, description, preview_md in items:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.set_margin_bottom(3)

                lbl_syn = Gtk.Label(label=syntax)
                lbl_syn.set_xalign(0); lbl_syn.set_width_chars(18)
                lbl_syn.set_max_width_chars(18)
                lbl_syn.set_ellipsize(Pango.EllipsizeMode.END)
                lbl_syn.add_css_class('md-help-syntax')
                row.append(lbl_syn)

                lbl_desc = Gtk.Label(label=description)
                lbl_desc.set_xalign(0); lbl_desc.set_hexpand(True)
                lbl_desc.add_css_class('lt-popup-cat')
                row.append(lbl_desc)

                # Bouton aperçu
                btn_eye = Gtk.Button(icon_name='view-reveal-symbolic')
                btn_eye.add_css_class('panel-icon-btn')
                btn_eye.set_tooltip_text('Aperçu')
                btn_eye.connect('clicked', lambda _, md=preview_md: show_preview(md))
                row.append(btn_eye)

                # Bouton insérer
                btn_ins = Gtk.Button(icon_name='list-add-symbolic')
                btn_ins.add_css_class('panel-icon-btn')
                btn_ins.set_tooltip_text('Insérer dans l\'éditeur')
                btn_ins.connect('clicked', lambda _, s=syntax: (
                    self._buffer.insert_at_cursor(s),
                    self._view.grab_focus()
                ))
                row.append(btn_ins)
                left_box.append(row)

        left_scroll.set_child(left_box)

        sep_v = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        hbox.append(left_scroll)
        hbox.append(sep_v)
        hbox.append(preview_web)
        win.set_child(hbox)

        self._md_help_window = win
        win.present()


    def _on_show_copy_menu(self, btn):
        """Popover : copier le Markdown ou le HTML dans le presse-papier."""
        popover = Gtk.Popover()
        popover.set_parent(btn)
        popover.set_has_arrow(True)
        popover.set_autohide(True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # En-tête
        hdr = Gtk.Box(); hdr.add_css_class('lt-popup-header')
        lbl = Gtk.Label(label='Copier dans le presse-papier')
        lbl.add_css_class('lt-popup-msg'); lbl.set_xalign(0); hdr.append(lbl)
        vbox.append(hdr)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        items = [
            ('edit-copy-symbolic',          'Markdown',
             'Copier le source Markdown',   self._copy_markdown),
            ('text-html-symbolic',          'HTML',
             'Copier le rendu HTML',         self._copy_html),
        ]

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.add_css_class('lt-popup-fixes')
        for icon, label, desc, cb in items:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_start(4); row.set_margin_end(8)
            row.set_margin_top(3); row.set_margin_bottom(3)
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(18); row.append(img)
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            tb.set_hexpand(True)
            lf = Gtk.Label(label=label); lf.set_xalign(0)
            lf.add_css_class('lt-popup-msg'); tb.append(lf)
            ld = Gtk.Label(label=desc); ld.set_xalign(0)
            ld.add_css_class('lt-popup-cat'); tb.append(ld)
            row.append(tb)
            br = Gtk.Button(); br.set_child(row)
            br.add_css_class('tmpl-btn'); br.set_halign(Gtk.Align.FILL)
            br.connect('clicked', lambda _, c=cb, p=popover: (p.popdown(), c()))
            inner.append(br)
        vbox.append(inner)
        popover.set_child(vbox)
        popover.popup()

    def _copy_markdown(self):
        """Copie le source Markdown dans le presse-papier."""
        text = self._get_text()
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        self._set_status('Markdown copié dans le presse-papier', 'ok')

    def _copy_html(self):
        """Copie le rendu HTML dans le presse-papier."""
        text = self._get_text()
        import re as _re
        clean = _re.sub(r'^---\n[\s\S]*?\n---\n?', '', text).strip()
        try:
            body = markdown.markdown(clean, extensions=MD_EXT, extension_configs=MD_CFG)
        except Exception:
            body = markdown.markdown(clean)
        html = ('<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
                '<body>' + body + '</body></html>')
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(html)
        self._set_status('HTML copié dans le presse-papier', 'ok')

    def _on_show_toc(self, _=None):
        """Affiche ou ferme la table des matières."""
        # Si déjà ouverte, la fermer
        if self._toc_window is not None:
            try: self._toc_window.destroy()
            except Exception: pass
            self._toc_window = None
            return
        text = self._get_text()
        headings = []
        for i, line in enumerate(text.splitlines()):
            m = re.match(r'^(#{1,4}) (.+)', line.strip())
            if m:
                headings.append((i, len(m.group(1)), m.group(2).strip()))
        if not headings:
            self._set_status('Aucun titre dans cette note', 'err'); return

        win = Gtk.ApplicationWindow(application=self.get_application())
        win.set_title('Table des matieres')
        win.set_transient_for(self)
        win.set_modal(False)
        # Taille adaptée au nombre de titres
        h = min(max(len(headings) * 38 + 80, 200), 700)
        win.set_default_size(420, h)
        win.set_show_menubar(False)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(vbox)

        bar = Gtk.Box(); bar.add_css_class('panel-bar')
        note_name = self._current_file.stem if self._current_file else 'Note'
        lbl = Gtk.Label(label='  ToC — ' + note_name)
        lbl.add_css_class('panel-label-edit'); lbl.set_xalign(0)
        bar.append(lbl); vbox.append(bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        scroll = Gtk.ScrolledWindow(); scroll.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner.set_margin_top(8); inner.set_margin_bottom(8)
        inner.set_margin_start(8); inner.set_margin_end(8)

        colors = ['#7c8cf8', '#bb86fc', '#6fcf97', '#f2c94c']
        for lineno, level, title in headings:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.set_margin_start((level - 1) * 20)
            # Indicateur de niveau coloré
            dot = Gtk.Label(label='\u25cf')
            dot_prov = Gtk.CssProvider()
            dot_prov.load_from_data(
                ('label { color: ' + colors[level-1] + '; font-size: 10px; }').encode())
            dot.get_style_context().add_provider(
                dot_prov, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            row.append(dot)
            btn = Gtk.Button(label=title)
            btn.add_css_class('tmpl-btn'); btn.set_hexpand(True)
            btn.set_halign(Gtk.Align.FILL)
            c = btn.get_child()
            if c: c.set_xalign(0)
            btn.connect('clicked', self._on_toc_goto, lineno, win)
            row.append(btn)
            inner.append(row)

        scroll.set_child(inner); vbox.append(scroll)
        win.connect('destroy', lambda _: setattr(self, '_toc_window', None))
        self._toc_window = win
        win.present()

    def _on_toc_goto(self, _, lineno, win=None):
        """Déplace le curseur vers la ligne du titre cliqué."""
        if win: win.destroy()
        it = _iter_at_line(self._buffer, lineno)
        self._buffer.place_cursor(it)
        self._view.scroll_to_iter(it, 0.1, True, 0.0, 0.3)
        self._view.grab_focus()

    def _on_delete_notes_multi(self, _, filepaths, popover):
        """Supprime plusieurs notes après confirmation."""
        popover.popdown(); popover.unparent()
        self._file_menu_popover = None

        dialog = Gtk.AlertDialog()
        dialog.set_message("Supprimer " + str(len(filepaths)) + " notes ?")
        detail = "\n".join(f.name for f in filepaths[:10])
        if len(filepaths) > 10:
            detail += "\n... et " + str(len(filepaths) - 10) + " autre(s)"
        dialog.set_detail(detail)
        dialog.set_buttons(["Annuler", "Supprimer"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.set_modal(True)

        def on_response(d, result):
            try:
                idx = d.choose_finish(result)
            except Exception:
                return
            if idx != 1: return
            deleted = []
            errors  = []
            for fp in filepaths:
                try:
                    fp.unlink()
                    self._db._con.execute(
                        'DELETE FROM note_tags WHERE note_path=?', (str(fp),))
                    deleted.append(fp)
                except Exception as ex:
                    errors.append(fp.name + ': ' + str(ex))
            self._db._con.commit()
            if self._current_file in deleted:
                self._current_file = None
                self._buffer.set_text(new_note_content())
                self._update_title()
            self._refresh_file_list()
            msg = str(len(deleted)) + ' note(s) supprimee(s)'
            if errors: msg += ' | Erreurs: ' + ', '.join(errors)
            self._set_status(msg, 'ok' if not errors else 'err')

        dialog.choose(self, None, on_response)

    def _on_send_mail_file(self, _, filepath, popover):
        """Envoie le fichier par mail via Thunderbird."""
        popover.popdown(); popover.unparent()
        self._file_menu_popover = None
        self._do_send_mail(
            filepath.stem.replace('_', ' '),
            filepath.read_text(encoding='utf-8'))

    def _on_publish_hugo(self, _, filepath, popover):
        """Ouvre une fenêtre de log puis publie sur Hugo en arrière-plan."""
        popover.popdown(); popover.unparent()
        self._file_menu_popover = None

        REMOTE_USER = "user"
        REMOTE_HOST = "blog"
        REMOTE_PATH = "/home/user/hugo/content/posts/"
        HUGO_DIR    = "/home/user/hugo"

        # ── Fenêtre de log ────────────────────────────────────────────────────
        log_win = Gtk.ApplicationWindow(application=self.get_application())
        log_win.set_title("Publication Hugo - " + filepath.name)
        log_win.set_transient_for(self)
        log_win.set_default_size(700, 420)
        log_win.set_modal(False)
        log_win.set_show_menubar(False)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        log_win.set_child(vbox)

        # Barre de titre interne
        title_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_bar.add_css_class("panel-bar")
        title_lbl = Gtk.Label(label="  Publication Hugo")
        title_lbl.add_css_class("panel-label-preview"); title_lbl.set_xalign(0)
        title_lbl.set_hexpand(True); title_bar.append(title_lbl)
        self._hugo_spinner = Gtk.Spinner(); self._hugo_spinner.start()
        self._hugo_spinner.set_margin_end(10); title_bar.append(self._hugo_spinner)
        vbox.append(title_bar)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Zone de texte scrollable pour les logs
        scroll = Gtk.ScrolledWindow(); scroll.set_vexpand(True)
        log_view = Gtk.TextView()
        log_view.set_editable(False); log_view.set_cursor_visible(False)
        log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_view.set_left_margin(10); log_view.set_right_margin(10)
        log_view.set_top_margin(8); log_view.set_bottom_margin(8)
        log_view.add_css_class("editor-view")
        log_buf = log_view.get_buffer()
        # Tags de couleur pour les logs
        tag_cmd  = log_buf.create_tag("cmd",  foreground="#89b4fa", weight=700)
        tag_ok   = log_buf.create_tag("ok",   foreground="#a6e3a1")
        tag_err  = log_buf.create_tag("err",  foreground="#f38ba8")
        tag_info = log_buf.create_tag("info", foreground="#a6adc8")
        scroll.set_child(log_view); vbox.append(scroll)

        # Bouton fermer (désactivé pendant l'opération)
        btn_close = Gtk.Button(label="Fermer")
        btn_close.set_sensitive(False)
        btn_close.set_margin_top(8); btn_close.set_margin_bottom(8)
        btn_close.set_margin_start(8); btn_close.set_margin_end(8)
        btn_close.connect("clicked", lambda _: log_win.destroy())
        vbox.append(btn_close)

        log_win.present()

        def log(text, tag_name="info"):
            """Ajoute une ligne dans le TextView (thread-safe via idle_add)."""
            def _append():
                end = log_buf.get_end_iter()
                nl = "\n"
                log_buf.insert_with_tags_by_name(end, text + nl, tag_name)
                # Auto-scroll vers le bas
                mark = log_buf.create_mark(None, log_buf.get_end_iter(), False)
                log_view.scroll_to_mark(mark, 0.0, False, 0.0, 1.0)
            GLib.idle_add(_append)

        def finish(success):
            """Appelé à la fin pour mettre à jour l'UI."""
            def _done():
                self._hugo_spinner.stop()
                btn_close.set_sensitive(True)
                if success:
                    self._set_status("Publie : " + filepath.name, "ok")
                else:
                    self._set_status("Erreur publication Hugo", "err")
            GLib.idle_add(_done)

        # ── Thread de publication ─────────────────────────────────────────────
        def _publish():
            import tempfile, re as _re
            REMOTE_IMG_DIR  = '/home/user/hugo/static/images'
            REMOTE_IMG_PATH = '/images/'  # chemin Hugo dans les md
            remote_file = REMOTE_PATH + filepath.name

            # Étape 0 : vérifier si le fichier existe déjà sur le serveur
            log('── Vérification fichier distant ──', 'info')
            cmd_check = ['ssh', REMOTE_USER + '@' + REMOTE_HOST,
                         'test -f ' + remote_file + ' && echo EXISTS || echo NEW']
            log('$ ' + ' '.join(cmd_check), 'cmd')
            try:
                r_check = subprocess.run(
                    cmd_check, capture_output=True, text=True, timeout=15)
                exists = r_check.stdout.strip() == 'EXISTS'
                if exists:
                    log('ℹ Mise à jour : le fichier existait déjà sur le serveur', 'info')
                    cmd_rm = ['ssh', REMOTE_USER + '@' + REMOTE_HOST,
                              'rm -f ' + remote_file]
                    log('$ ' + ' '.join(cmd_rm), 'cmd')
                    r_rm = subprocess.run(
                        cmd_rm, capture_output=True, text=True, timeout=15)
                    if r_rm.returncode == 0:
                        log('✓ Ancien fichier supprimé', 'ok')
                    else:
                        log('⚠ Impossible de supprimer : ' + r_rm.stderr.strip(), 'err')
                else:
                    log('★ Nouveau fichier : première publication', 'ok')
            except Exception as ex:
                log('⚠ Vérification impossible : ' + str(ex), 'err')

            # Étape 1 : Copier les images vers hugo/static/images
            log('', 'info')
            log('── Copie des images ──', 'info')
            text_orig = filepath.read_text(encoding='utf-8')
            images_found = _re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text_orig)
            image_names = []  # noms des images copiées
            for img_path_str in images_found:
                p = img_path_str
                for pfx in ('file:///', 'file://'):
                    if p.startswith(pfx): p = p[len(pfx):]; break
                img_path = Path(p)
                if not img_path.exists():
                    log('⚠ Image introuvable : ' + p, 'err'); continue
                # Créer le répertoire distant si nécessaire
                subprocess.run(
                    ['ssh', REMOTE_USER + '@' + REMOTE_HOST,
                     'mkdir -p ' + REMOTE_IMG_DIR],
                    capture_output=True, timeout=10)
                cmd_img = ['scp', str(img_path),
                           REMOTE_USER + '@' + REMOTE_HOST + ':' + REMOTE_IMG_DIR + '/']
                log('$ ' + ' '.join(cmd_img), 'cmd')
                r_img = subprocess.run(
                    cmd_img, capture_output=True, text=True, timeout=30)
                if r_img.returncode == 0:
                    log('✓ Image copiée : ' + img_path.name, 'ok')
                    image_names.append((img_path_str, img_path.name))
                else:
                    log('✗ Erreur image ' + img_path.name + ' : ' + r_img.stderr.strip(), 'err')

            # Étape 2 : Créer une copie du .md avec chemins images réécrits
            log('', 'info')
            log('── Réécriture des chemins images ──', 'info')
            text_hugo = text_orig
            for orig_path, img_name in image_names:
                hugo_path = REMOTE_IMG_PATH + img_name
                text_hugo = text_hugo.replace(orig_path, hugo_path)
                log('  ' + img_name + ' → ' + hugo_path, 'info')

            # Écrire le fichier temporaire
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                    delete=False, encoding='utf-8',
                    prefix=filepath.stem + '_') as tmp:
                tmp.write(text_hugo)
                tmp_md = Path(tmp.name)

            # Étape 3 : SCP du fichier .md (version réécrité)
            log('', 'info')
            log('── Copie du fichier Markdown ──', 'info')
            # Renommer le tmp avec le bon nom pour Hugo
            tmp_named = tmp_md.parent / filepath.name
            tmp_md.rename(tmp_named)
            cmd_scp = ['scp', str(tmp_named),
                       REMOTE_USER + '@' + REMOTE_HOST + ':' + REMOTE_PATH]
            log('$ ' + ' '.join(cmd_scp), 'cmd')
            try:
                result = subprocess.run(
                    cmd_scp, capture_output=True, text=True, timeout=30)
                tmp_named.unlink(missing_ok=True)
                if result.stdout: log(result.stdout.strip(), 'info')
                if result.stderr: log(result.stderr.strip(),
                                      'err' if result.returncode != 0 else 'info')
                if result.returncode != 0:
                    log('✗ SCP échoué (code ' + str(result.returncode) + ')', 'err')
                    finish(False); return
                log('✓ Fichier copié vers ' + REMOTE_HOST, 'ok')
            except subprocess.TimeoutExpired:
                tmp_named.unlink(missing_ok=True)
                log('✗ SCP timeout (30s)', 'err'); finish(False); return
            except Exception as ex:
                tmp_named.unlink(missing_ok=True)
                log('✗ SCP : ' + str(ex), 'err'); finish(False); return

            # Étape 2 : SSH hugo
            log("", "info")
            log("── Reconstruction Hugo ──", "info")
            cmd_ssh = ["ssh", REMOTE_USER + "@" + REMOTE_HOST,
                       "cd " + HUGO_DIR + " && hugo"]
            log("$ " + " ".join(cmd_ssh), "cmd")
            try:
                result = subprocess.run(
                    cmd_ssh, capture_output=True, text=True, timeout=120)
                for line in (result.stdout + result.stderr).splitlines():
                    tag = "err" if ("error" in line.lower() or "warn" in line.lower()) else "info"
                    if "Built in" in line or "Total in" in line: tag = "ok"
                    log(line, tag)
                if result.returncode != 0:
                    log("✗ Hugo échoué (code " + str(result.returncode) + ")", "err")
                    finish(False)
                else:
                    log("", "info")
                    log("✓ Site Hugo reconstruit avec succès", "ok")
                    finish(True)
            except subprocess.TimeoutExpired:
                log("✗ SSH hugo timeout (120s)", "err"); finish(False)
            except Exception as ex:
                log("✗ SSH : " + str(ex), "err"); finish(False)

        threading.Thread(target=_publish, daemon=True).start()

    def _on_delete_note(self, _, filepath, popover):
        """Supprime le fichier après confirmation via Gtk.AlertDialog (GTK4)."""
        popover.popdown(); popover.unparent()
        self._file_menu_popover = None

        dialog = Gtk.AlertDialog()
        dialog.set_message("Supprimer cette note ?")
        dialog.set_detail(filepath.name)
        dialog.set_buttons(["Annuler", "Supprimer"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.set_modal(True)

        def _has_hugo_frontmatter(fp):
            """Retourne True si le fichier contient un front matter Hugo (--- ... ---)"""
            try:
                content = fp.read_text(encoding='utf-8')
                return content.startswith('---')
            except Exception:
                return False

        def _delete_remote(fp, log_win_ref):
            """Supprime le fichier puis relance hugo, avec logs dans la fenêtre."""
            REMOTE_USER = 'user'
            REMOTE_HOST = 'blog'
            REMOTE_PATH = '/home/user/hugo/content/posts/'
            HUGO_DIR    = '/home/user/hugo'
            log_fn, finish_fn = log_win_ref

            # Étape 1 : rm sur le serveur
            cmd_rm = ['ssh', REMOTE_USER + '@' + REMOTE_HOST,
                      'rm -f ' + REMOTE_PATH + fp.name]
            log_fn('$ ' + ' '.join(cmd_rm), 'cmd')
            try:
                result = subprocess.run(cmd_rm, capture_output=True,
                                        text=True, timeout=15)
                if result.stderr: log_fn(result.stderr.strip(),
                    'err' if result.returncode != 0 else 'info')
                if result.returncode != 0:
                    log_fn('x Suppression distante echouee', 'err')
                    finish_fn(False); return
                log_fn('v Fichier supprime sur ' + REMOTE_HOST, 'ok')
            except Exception as ex:
                log_fn('x SSH rm : ' + str(ex), 'err')
                finish_fn(False); return

            # Étape 2 : relancer hugo
            cmd_hugo = ['ssh', REMOTE_USER + '@' + REMOTE_HOST,
                        'cd ' + HUGO_DIR + ' && hugo']
            log_fn('', 'info')
            log_fn('$ ' + ' '.join(cmd_hugo), 'cmd')
            try:
                result = subprocess.run(cmd_hugo, capture_output=True,
                                        text=True, timeout=120)
                for line in (result.stdout + result.stderr).splitlines():
                    tag = 'err' if ('error' in line.lower() or 'warn' in line.lower()) else 'info'
                    if 'Built in' in line or 'Total in' in line: tag = 'ok'
                    log_fn(line, tag)
                if result.returncode != 0:
                    log_fn('x Hugo echoue (code ' + str(result.returncode) + ')', 'err')
                    finish_fn(False)
                else:
                    log_fn('', 'info')
                    log_fn('v Site Hugo reconstruit avec succes', 'ok')
                    finish_fn(True)
            except subprocess.TimeoutExpired:
                log_fn('x SSH hugo timeout (120s)', 'err'); finish_fn(False)
            except Exception as ex:
                log_fn('x SSH hugo : ' + str(ex), 'err'); finish_fn(False)


        def on_response(d, result):
            try:
                idx = d.choose_finish(result)
            except Exception:
                return
            if idx != 1: return  # 0 = Annuler, 1 = Supprimer
            try:
                # Vérifier si c'est un article Hugo avant de supprimer
                is_hugo = _has_hugo_frontmatter(filepath)
                filepath.unlink()
                self._db._con.execute(
                    'DELETE FROM note_tags WHERE note_path=?', (str(filepath),))
                self._db._con.commit()
                if self._current_file == filepath:
                    self._current_file = None
                    self._buffer.set_text(new_note_content())
                    self._update_title()
                    self._refresh_note_tags()
                self._refresh_file_list()
                # Supprimer aussi sur le serveur Hugo si front matter présent
                if is_hugo:
                    # Ouvrir la fenêtre de log (même logique que _on_publish_hugo)
                    log_win2 = Gtk.ApplicationWindow(application=self.get_application())
                    log_win2.set_title('Suppression Hugo - ' + filepath.name)
                    log_win2.set_transient_for(self)
                    log_win2.set_default_size(700, 380)
                    log_win2.set_modal(False)
                    log_win2.set_show_menubar(False)
                    vb2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                    log_win2.set_child(vb2)
                    tb2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                    tb2.add_css_class('panel-bar')
                    tl2 = Gtk.Label(label='  Suppression Hugo')
                    tl2.add_css_class('panel-label-preview'); tl2.set_xalign(0)
                    tl2.set_hexpand(True); tb2.append(tl2)
                    sp2 = Gtk.Spinner(); sp2.start()
                    sp2.set_margin_end(10); tb2.append(sp2)
                    vb2.append(tb2)
                    vb2.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                    sc2 = Gtk.ScrolledWindow(); sc2.set_vexpand(True)
                    lv2 = Gtk.TextView()
                    lv2.set_editable(False); lv2.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                    lv2.set_left_margin(10); lv2.set_right_margin(10)
                    lv2.set_top_margin(8); lv2.add_css_class('editor-view')
                    lb2 = lv2.get_buffer()
                    lb2.create_tag('cmd',  foreground='#89b4fa', weight=700)
                    lb2.create_tag('ok',   foreground='#a6e3a1')
                    lb2.create_tag('err',  foreground='#f38ba8')
                    lb2.create_tag('info', foreground='#a6adc8')
                    sc2.set_child(lv2); vb2.append(sc2)
                    bc2 = Gtk.Button(label='Fermer')
                    bc2.set_sensitive(False)
                    bc2.set_margin_top(8); bc2.set_margin_bottom(8)
                    bc2.set_margin_start(8); bc2.set_margin_end(8)
                    bc2.connect('clicked', lambda _: log_win2.destroy())
                    vb2.append(bc2)
                    log_win2.present()
                    def _log2(text, tag_name='info'):
                        def _a():
                            end = lb2.get_end_iter()
                            lb2.insert_with_tags_by_name(end, text + chr(10), tag_name)
                            mk = lb2.create_mark(None, lb2.get_end_iter(), False)
                            lv2.scroll_to_mark(mk, 0.0, False, 0.0, 1.0)
                        GLib.idle_add(_a)
                    def _finish2(success):
                        def _d():
                            sp2.stop(); bc2.set_sensitive(True)
                            if success:
                                self._set_status('Supprime et site reconstruit : ' + filepath.name, 'ok')
                            else:
                                self._set_status('Erreur suppression Hugo', 'err')
                        GLib.idle_add(_d)
                    threading.Thread(target=_delete_remote,
                        args=(filepath, (_log2, _finish2)), daemon=True).start()
                else:
                    self._set_status('Note supprimee : ' + filepath.name, 'ok')
            except Exception as ex:
                self._set_status('Erreur suppression : ' + str(ex), 'err')

        dialog.choose(self, None, on_response)

    def _on_multi_tag_toggled(self, cb, filepaths, tag_id):
        """
        Applique/retire un tag sur toute la sélection.
        - Si la checkbox était en état intermédiaire (inconsistent), cocher = appliquer à tous.
        - Sinon comportement normal toggle.
        """
        cb.set_inconsistent(False)  # sortir de l'état intermédiaire
        apply_tag = cb.get_active()
        for filepath in filepaths:
            ids = self._db.get_tag_ids_for_note(filepath)
            if apply_tag: ids.add(tag_id)
            else:         ids.discard(tag_id)
            self._db.set_tags_for_note(filepath, ids)
        if self._current_file in filepaths:
            self._refresh_note_tags()
        self._refresh_file_list()

    def _on_tags_right_click(self, gesture, n, x, y):
        """Clic droit sur le panneau étiquettes → menu contextuel."""
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # Vérifier si le clic est sur une étiquette existante
        row = self._tags_listbox.get_row_at_y(int(y))

        popover = Gtk.Popover()
        popover.set_parent(self._tags_listbox)
        popover.set_has_arrow(True); popover.set_autohide(True)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Entrée : Nouvelle étiquette
        btn_new = Gtk.Button(label="  Nouvelle etiquette")
        btn_new.add_css_class("tmpl-btn"); btn_new.set_halign(Gtk.Align.FILL)
        child = btn_new.get_child()
        if child: child.set_xalign(0)
        btn_new.connect("clicked", lambda _: (popover.popdown(), self._on_show_new_tag_dialog(None)))
        vbox.append(btn_new)

        # Si clic sur une ligne d'étiquette, ajouter Modifier et Supprimer
        if row and hasattr(row, '_tag_id'):
            tag_id = row._tag_id
            tags   = {t['id']: t for t in self._db.get_tags()}
            tag    = tags.get(tag_id)
            if tag:
                vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                btn_edit = Gtk.Button(label="  Modifier \"" + tag['label'].capitalize() + "\"")
                btn_edit.add_css_class("tmpl-btn"); btn_edit.set_halign(Gtk.Align.FILL)
                child = btn_edit.get_child()
                if child: child.set_xalign(0)
                btn_edit.connect("clicked",
                    lambda _, tid=tag_id, lbl=tag['label'], col=tag['color']: (
                        popover.popdown(),
                        self._on_edit_tag(None, tid, lbl, col)))
                vbox.append(btn_edit)

                btn_del = Gtk.Button(label="  Supprimer \"" + tag['label'].capitalize() + "\"")
                btn_del.add_css_class("file-delete-btn"); btn_del.set_halign(Gtk.Align.FILL)
                child = btn_del.get_child()
                if child: child.set_xalign(0)
                btn_del.connect("clicked",
                    lambda _, tid=tag_id: (popover.popdown(), self._on_delete_tag(None, tid)))
                vbox.append(btn_del)

        popover.set_child(vbox)
        popover.popup()

    def _on_show_new_tag_dialog(self, _):
        """Ouvre une fenêtre de création d'étiquette."""
        dialog = Gtk.Dialog(title="Nouvelle etiquette", transient_for=self, modal=True)
        dialog.set_default_size(360, 160)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16); box.set_margin_bottom(8)
        box.set_margin_start(16); box.set_margin_end(16)

        # Libellé
        lbl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_lbl = Gtk.Label(label="Libelle :"); lbl_lbl.set_xalign(0)
        lbl_lbl.set_width_chars(10); lbl_row.append(lbl_lbl)
        entry = Gtk.Entry(); entry.set_hexpand(True)
        entry.set_placeholder_text("Nom de l'etiquette...")
        entry.connect("activate", lambda _: dialog.response(Gtk.ResponseType.OK))
        lbl_row.append(entry); box.append(lbl_row)

        # Couleur
        col_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        col_lbl = Gtk.Label(label="Couleur :"); col_lbl.set_xalign(0)
        col_lbl.set_width_chars(10); col_row.append(col_lbl)
        color_dlg = Gtk.ColorDialog()
        color_btn = Gtk.ColorDialogButton(dialog=color_dlg)
        init_rgba = Gdk.RGBA(); init_rgba.parse("#89b4fa")
        color_btn.set_rgba(init_rgba)
        col_row.append(color_btn)

        # Aperçu de la chip
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preview_box.set_halign(Gtk.Align.CENTER)
        preview_box.set_margin_start(8)
        self._preview_chip = make_colored_chip("Apercu", "#89b4fa")
        preview_box.append(self._preview_chip)
        col_row.append(preview_box)

        # Mettre à jour l'aperçu quand la couleur change
        def _update_preview(*_):
            rgba = color_btn.get_rgba()
            hex_col = "#{:02x}{:02x}{:02x}".format(
                int(rgba.red*255), int(rgba.green*255), int(rgba.blue*255))
            lbl = entry.get_text().strip() or "Apercu"
            new_chip = make_colored_chip(lbl, hex_col)
            # Remplacer l'ancien chip
            old = preview_box.get_first_child()
            if old: preview_box.remove(old)
            preview_box.append(new_chip)
        color_btn.connect("notify::rgba", _update_preview)
        entry.connect("changed", _update_preview)

        box.append(col_row)
        dialog.get_content_area().append(box)
        dialog.add_button("Annuler", Gtk.ResponseType.CANCEL)
        btn_ok = dialog.add_button("Creer", Gtk.ResponseType.OK)
        btn_ok.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        def on_response(d, r):
            if r == Gtk.ResponseType.OK:
                label = entry.get_text().strip()
                if label:
                    rgba  = color_btn.get_rgba()
                    color = "#{:02x}{:02x}{:02x}".format(
                        int(rgba.red*255), int(rgba.green*255), int(rgba.blue*255))
                    if self._db.add_tag(label, color):
                        self._refresh_tags_list()
                        self._refresh_file_list()
                        self._set_status("Etiquette creee : " + label, "ok")
                    else:
                        # Fenêtre d'erreur doublon
                        err = Gtk.AlertDialog()
                        err.set_message("Etiquette deja existante")
                        err.set_detail(
                            'Une etiquette nommee "' + label + '" existe deja.\n'
                            'Choisissez un nom different.')
                        err.set_buttons(["OK"])
                        err.set_default_button(0)
                        err.set_modal(True)
                        err.choose(self, None, None)
                        return  # garder le dialogue ouvert
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()
        entry.grab_focus()

    def _on_tag_search_changed(self, entry):
        self._refresh_tags_list(entry.get_text())


    def _on_edit_tag(self, _, tag_id, label, color):
        """Dialogue d'édition inline d'une étiquette."""
        dialog = Gtk.Dialog(title="Modifier etiquette", transient_for=self, modal=True)
        dialog.set_default_size(320, 120)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        entry = Gtk.Entry(); entry.set_text(label); entry.set_hexpand(True); box.append(entry)
        _dlg = Gtk.ColorDialog()
        color_btn = Gtk.ColorDialogButton(dialog=_dlg)
        _r = Gdk.RGBA(); _r.parse(color); color_btn.set_rgba(_r)
        box.append(color_btn)
        dialog.get_content_area().append(box)
        dialog.add_button("Annuler", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        def on_response(d, r):
            if r == Gtk.ResponseType.OK:
                new_label = entry.get_text().strip()
                new_rgba  = color_btn.get_rgba()
                new_color = "#{:02x}{:02x}{:02x}".format(
                    int(new_rgba.red*255), int(new_rgba.green*255), int(new_rgba.blue*255))
                if new_label:
                    self._db.update_tag(tag_id, new_label, new_color)
                    self._refresh_tags_list(); self._refresh_note_tags()
                    self._refresh_file_list()
            d.destroy()
        dialog.connect("response", on_response); dialog.present()

    def _on_delete_tag(self, _, tag_id):
        self._db.delete_tag(tag_id)
        self._active_tag_filter.discard(tag_id)
        self._refresh_tags_list(); self._refresh_note_tags()
        self._update_filter_label(); self._refresh_file_list()

    def _on_toggle_tag_filter(self, _, tag_id):
        """Active/désactive un tag dans le filtre de recherche."""
        if tag_id in self._active_tag_filter:
            self._active_tag_filter.discard(tag_id)
        else:
            self._active_tag_filter.add(tag_id)
        self._update_filter_label()
        self._refresh_tags_list(self._tag_search.get_text())
        self._refresh_file_list()

    def _on_clear_filter(self, _):
        self._active_tag_filter.clear()
        self._update_filter_label()
        self._refresh_tags_list(self._tag_search.get_text())
        self._refresh_file_list()

    def _update_filter_label(self):
        if not self._active_tag_filter:
            self._filter_lbl.set_text("")
            return
        tags = {t["id"]: t["label"] for t in self._db.get_tags()}
        names = ", ".join(tags.get(tid,"?") for tid in self._active_tag_filter)
        self._filter_lbl.set_text("Filtre : " + names)

    def _on_note_tag_toggled(self, cb, tag_id):
        if not self._current_file: return
        ids = self._db.get_tag_ids_for_note(self._current_file)
        if cb.get_active(): ids.add(tag_id)
        else:               ids.discard(tag_id)
        self._db.set_tags_for_note(self._current_file, ids)
        self._refresh_file_list()

    # ── Gestionnaire de fichiers ───────────────────────────────────────────────

    def _notes_dir(self): return Path(self._config.get("notes_dir", str(Path.home()/"Documents"/"notes")))

    def _ensure_notes_dir(self):
        try: self._notes_dir().mkdir(parents=True, exist_ok=True); return True
        except Exception as ex: self._set_status("Erreur : " + str(ex), "err"); return False

    def _list_notes(self):
        d = self._notes_dir()
        if not d.exists(): return []
        return sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _make_chip_pixbuf(self, label, color_hex, font_size=None):
        """Chip Cairo → PNG bytes → GdkPixbuf.new_from_stream (fiable)."""
        import cairo as _cairo, math, array, io
        fs     = font_size or max(13, int(_CURRENT_FONT_SIZE * 1.2))
        pad_x  = 16; pad_y = 7; radius = min(14, fs)

        # Mesurer le texte
        s0  = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, 1, 1)
        c0  = _cairo.Context(s0)
        c0.select_font_face('sans-serif', _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_BOLD)
        c0.set_font_size(fs)
        ext = c0.text_extents(label)
        w   = max(int(ext.width  + pad_x * 2) + 2, 20)
        h   = max(int(fs         + pad_y * 2) + 2, 16)

        surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, w, h)
        ctx  = _cairo.Context(surf)
        ctx.set_antialias(_cairo.ANTIALIAS_BEST)

        # Couleur fond
        try:
            r = int(color_hex[1:3],16)/255
            g = int(color_hex[3:5],16)/255
            b = int(color_hex[5:7],16)/255
        except Exception:
            r, g, b = 0.54, 0.55, 0.98

        # Fond arrondi
        ctx.set_source_rgb(r, g, b)
        ctx.arc(radius,   radius,   radius, math.pi,       3*math.pi/2)
        ctx.arc(w-radius, radius,   radius, 3*math.pi/2,   0)
        ctx.arc(w-radius, h-radius, radius, 0,             math.pi/2)
        ctx.arc(radius,   h-radius, radius, math.pi/2,     math.pi)
        ctx.close_path(); ctx.fill()

        # Texte
        lum = 0.299*r + 0.587*g + 0.114*b
        ctx.set_source_rgb(0.1,0.1,0.1) if lum > 0.55 else ctx.set_source_rgb(1,1,1)
        ctx.select_font_face('sans-serif', _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(fs)
        ctx.move_to(pad_x - ext.x_bearing,
                    (h - ext.height) / 2 - ext.y_bearing)
        ctx.show_text(label)

        # Cairo ARGB32 (BGRA) → PNG → GdkPixbuf
        buf = io.BytesIO()
        surf.write_to_png(buf)
        buf.seek(0)
        loader = GdkPixbuf.PixbufLoader.new_with_type('png')
        loader.write(buf.read())
        loader.close()
        return loader.get_pixbuf()

    def _tree_cell_pixbuf(self, col, renderer, model, it, _):
        """Affiche une chip colorée pour les nœuds groupe, rien pour les fichiers."""
        is_file = model[it][2]
        if is_file:
            renderer.set_property('pixbuf', None)
            renderer.set_property('width', 0)
            renderer.set_property('xpad', 0)
            return
        name  = model[it][0].strip().capitalize()
        color = model[it][5] if model[it][5] else '#89b4fa'
        try:
            pb = self._make_chip_pixbuf(name, color)
            renderer.set_property('pixbuf', pb)
            renderer.set_property('width', -1)
            renderer.set_property('xpad', 6)
        except Exception as e:
            print('[chip]', e)
            renderer.set_property('pixbuf', None)
            renderer.set_property('width', 0)

    def _tree_cell_data(self, col, renderer, model, it, _):
        """Formate chaque cellule du TreeView selon son type (groupe ou fichier)."""
        name    = model[it][0]
        path_s  = model[it][1]
        is_file = model[it][2]
        date_s  = model[it][3]
        is_new  = model[it][4]

        if not is_file:
            color = model[it][5] if model[it][5] else '#89b4fa'
            from pathlib import Path as _Path
            child_it = model.iter_children(it)
            group_unsaved = False
            while child_it:
                child_path = model[child_it][1]
                if child_path:
                    _cf = _Path(child_path)
                    if _cf in self._unsaved_files:
                        group_unsaved = True
                        break
                child_it = model.iter_next(child_it)
            # Style pill : fond semi-transparent, texte couleur de l'étiquette
            fs_pt = max(8, int(_CURRENT_FONT_SIZE * 0.75))
            bg_pill = (color + '33') if (color and len(color) == 7) else '#7c8cf833'
            renderer.set_property('background', bg_pill)
            renderer.set_property('background-set', True)
            renderer.set_property('foreground', color or '#7c8cf8')
            renderer.set_property('foreground-set', True)
            renderer.set_property('weight', 700)
            renderer.set_property('weight-set', True)
            renderer.set_property('font', 'sans bold ' + str(fs_pt))
            name_cap = name.strip().capitalize()
            label = GLib.markup_escape_text(name_cap)
            badge = ('  <span foreground="#f9e2af">●</span>' if group_unsaved else '')
            # Icône tag unicode avant le libellé, de la couleur de l'étiquette
            tag_icon = '<span foreground="' + (color or '#7c8cf8') + '">🏷️ </span>'
            renderer.set_property('markup', tag_icon + label + badge)
            renderer.set_property('xpad', 10)
            renderer.set_property('ypad', 4)
        else:
            # Fichier
            from pathlib import Path
            fpath = Path(path_s) if path_s else None
            is_active = fpath == self._current_file if fpath else False
            badge = '  <span foreground="#a6e3a1" size="small"><b>NEW</b></span>' if is_new else ''
            # Icône non-sauvegardé : ● orange si ce fichier est dans _unsaved_files
            _fpath = Path(path_s) if path_s else None
            _is_unsaved = (_fpath in self._unsaved_files) if _fpath else False
            unsaved_badge = ('  <span foreground="#f9e2af" size="small">&#x25CF;</span>'
                             if _is_unsaved else '')
            date_part = '\n<span foreground="#a6adc8" size="small">' + date_s + '</span>' if date_s else ''
            # Indicateur sync SCP
            sync_ok, sync_at = self._db.get_sync_status(path_s) if path_s else (None, None)
            if sync_ok is True:
                sync_badge = '  <span foreground="#a6e3a1" size="small">✔</span>'
            elif sync_ok is False:
                sync_badge = '  <span foreground="#f38ba8" size="small">✘</span>'
            else:
                sync_badge = ''
            color = '#89b4fa' if is_active else '#cdd6f4'
            weight = 700 if is_active else 400
            renderer.set_property('background-set', False)
            renderer.set_property('foreground-set', False)
            renderer.set_property('weight-set', False)
            renderer.set_property('font', 'sans ' + str(max(8, int(_CURRENT_FONT_SIZE * 0.85))))
            renderer.set_property('xpad', 4)
            renderer.set_property('ypad', 2)
            # Indicateur chiffrement local
            enc_badge = ''
            if path_s and self._db.is_note_encrypted(path_s):
                enc_badge = ' <span foreground="#f9e2af" size="small">🔒</span>'
            renderer.set_property('markup',
                '<span foreground="' + color + '" weight="' + str(weight) + '">'
                + GLib.markup_escape_text(name) + badge + unsaved_badge + enc_badge + sync_badge + '</span>' + date_part)

    def _refresh_file_list(self, highlight_new=None):
        self._dir_lbl.set_text(str(self._notes_dir()))
        all_files = self._list_notes()

        # Appliquer le filtre par étiquettes
        if self._active_tag_filter:
            matching = self._db.search_by_tags(self._active_tag_filter) or set()
            files = [f for f in all_files if f in matching]
        else:
            files = all_files

        # Sauvegarder les nœuds groupes actuellement dépliés (par label)
        expanded_labels = set()
        def _walk_save(model, parent=None):
            it = model.iter_children(parent)
            while it:
                path = model.get_path(it)
                if not model[it][2] and self._tree_view.row_expanded(path):
                    expanded_labels.add(model[it][0].strip())
                _walk_save(model, it)
                it = model.iter_next(it)
        _walk_save(self._tree_store)

        # TreeStore.clear() deprecated → supprimer les enfants manuellement
        while self._tree_store.get_iter_first():
            it = self._tree_store.get_iter_first()
            self._tree_store.remove(it)

        if not files:
            self._tree_store.append(None, ['Aucune note', '', False, '', False, ''])
            return

        # Construire l'arborescence : tag → [fichiers]
        all_tags     = {t['id']: t for t in self._db.get_tags()}
        # Pour chaque fichier, récupérer ses tags
        file_tags    = {f: self._db.get_tag_ids_for_note(f) for f in files}
        # Grouper par tag (un fichier peut apparaître dans plusieurs groupes)
        groups       = {}   # tag_id → [Path]
        no_tag_files = []
        for f in files:
            tags = file_tags[f]
            if not tags:
                no_tag_files.append(f)
            else:
                # Trier par tag id pour ordre stable
                for tid in sorted(tags):
                    groups.setdefault(tid, []).append(f)

        pinned_paths   = self._db.get_pinned_paths()
        favorite_paths = self._db.get_favorite_paths()

        def add_file_row(parent, f):
            is_new   = highlight_new is not None and f in highlight_new
            mtime    = datetime.fromtimestamp(f.stat().st_mtime)
            date_s   = mtime.strftime('%d/%m/%Y %H:%M')
            is_pin   = f in pinned_paths
            is_fav   = f in favorite_paths
            prefix   = ('★ ' if is_fav else '') + ('📌 ' if is_pin else '')
            self._tree_store.append(parent,
                [prefix + f.stem, str(f), True, date_s, is_new, ''])

        def sort_files(flist):
            return sorted(flist,
                key=lambda f: (0 if f in pinned_paths else 1, -f.stat().st_mtime))

        # Groupes par étiquette (triés par label)
        for tid in sorted(groups, key=lambda i: all_tags[i]['label'].lower()):
            tag = all_tags[tid]
            parent = self._tree_store.append(None,
                ['  ' + tag['label'].capitalize(), '', False, '', False, tag['color']])
            for f in sort_files(groups[tid]):
                add_file_row(parent, f)

        # Fichiers sans étiquette : pas de groupe, directement à la racine
        for f in sort_files(no_tag_files):
            add_file_row(None, f)


        # Restaurer les nœuds dépliés + déplier le fichier courant
        current_str = str(self._current_file) if self._current_file else None
        def _walk_restore(model, parent=None):
            it = model.iter_children(parent)
            while it:
                path = model.get_path(it)
                if not model[it][2]:
                    if model[it][0].strip() in expanded_labels:
                        self._tree_view.expand_to_path(path)
                else:
                    if current_str and model[it][1] == current_str:
                        self._tree_view.expand_to_path(path)
                        sel = self._tree_view.get_selection()
                        sel.select_path(path)
                        self._tree_view.scroll_to_cell(path, None, True, 0.5, 0.0)
                _walk_restore(model, it)
                it = model.iter_next(it)
        _walk_restore(self._tree_store)

    def _on_tree_activated(self, treeview, tree_path, col):
        it = self._tree_store.get_iter(tree_path)
        if it is None: return
        is_file = self._tree_store[it][2]
        path_s  = self._tree_store[it][1]
        if not is_file or not path_s: return
        path = Path(path_s)
        if self._current_file == path: return
        # Mémoriser le contenu en cours d'édition du fichier courant
        if self._current_file and self._current_file in self._unsaved_files:
            # Stocker le contenu non sauvegardé pour pouvoir le restaurer
            self._unsaved_contents[self._current_file] = self._get_text()
        try:
            # Charger le nouveau fichier :
            # - Si ce fichier était en cours d'édition, restaurer son contenu
            # - Sinon lire depuis le disque
            if path in self._unsaved_files and path in self._unsaved_contents:
                content = self._unsaved_contents[path]
            else:
                content = self._read_note(path)
                # Fichier propre depuis le disque
                self._unsaved_files.discard(path)
                self._unsaved_contents.pop(path, None)
            self._loading_file = True
            self._buffer.set_text(content)
            self._loading_file = False
            self._current_file = path
            self._config['last_file'] = str(path)
            self._preview_loaded = False  # forcer rechargement complet
            GLib.idle_add(self._apply_syntax_highlight)
            self._update_header_note_title()
            self._update_title()
            self._tree_view.queue_draw()
            self._refresh_file_list(); self._refresh_note_tags()
            self._set_status("Ouvert : " + path.name, "ok")
        except Exception as ex: self._set_status("Erreur : " + str(ex), "err")

    def _on_new_note(self, _):
        self._current_file = None
        content = new_note_content()
        self._loading_file = True
        self._buffer.set_text(content)
        self._loading_file = False
        # Sauvegarder immédiatement dans le répertoire de notes
        if self._ensure_notes_dir():
            title    = extract_title(content)
            filename = title + ".md"
            path     = self._notes_dir() / filename
            if path.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = title + "_" + ts + ".md"
                path = self._notes_dir() / filename
            try:
                path.write_text(content, encoding="utf-8")
                self._current_file = path
                self._unsaved_files.discard('__new__')
                self._set_status("Nouvelle note creee : " + filename, "ok")
            except Exception as ex:
                self._set_status("Erreur creation note : " + str(ex), "err")
        self._update_title()
        self._refresh_note_tags()
        self._refresh_file_list()
        # Forcer la preview sur la nouvelle note
        self._preview_loaded = False
        GLib.idle_add(self._refresh_preview)
        GLib.idle_add(self._apply_syntax_highlight)

    def _on_save_to_notes(self, _):
        if not self._ensure_notes_dir(): return
        text  = self._get_text(); title = extract_title(text)
        filename = title + ".md"; path = self._notes_dir() / filename
        if path.exists() and path != self._current_file:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = title + "_" + ts + ".md"; path = self._notes_dir() / filename
        try:
            path.write_text(text, encoding="utf-8")
            self._current_file = path; self._update_title()
            self._refresh_file_list(); self._refresh_note_tags()
            self._set_status("Sauvegarde : " + filename, "ok")
        except Exception as ex: self._set_status("Erreur : " + str(ex), "err")

    def _update_title(self):
        pass  # titre de fenêtre non modifié (artefact rendu GNOME Shell)


    def _build_avatar(self):
        """
        Cherche l'avatar de l'utilisateur GNOME dans cet ordre :
        1. /var/lib/AccountsService/icons/<user>
        2. ~/.face  ~/.face.icon
        3. Via D-Bus org.freedesktop.Accounts
        4. Fallback : cercle avec initiales
        """
        import os, subprocess as _sp
        user = os.environ.get('USER', os.environ.get('LOGNAME', 'user'))

        # Chercher le fichier avatar
        avatar_paths = [
            f'/var/lib/AccountsService/icons/{user}',
            os.path.expanduser('~/.face'),
            os.path.expanduser('~/.face.icon'),
        ]

        # Essayer D-Bus AccountsService
        try:
            r = _sp.run(
                ['gdbus', 'call', '--system',
                 '--dest', 'org.freedesktop.Accounts',
                 '--object-path', '/org/freedesktop/Accounts',
                 '--method', 'org.freedesktop.Accounts.FindUserByName', user],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                obj_path = r.stdout.strip().strip("()'").strip()
                r2 = _sp.run(
                    ['gdbus', 'call', '--system',
                     '--dest', 'org.freedesktop.Accounts',
                     '--object-path', obj_path,
                     '--method', 'org.freedesktop.DBus.Properties.Get',
                     'org.freedesktop.Accounts.User', 'IconFile'],
                    capture_output=True, text=True, timeout=3)
                if r2.returncode == 0:
                    import re as _re
                    m = _re.search(r"'([^']+)'", r2.stdout)
                    if m: avatar_paths.insert(0, m.group(1))
        except Exception:
            pass

        # Trouver le premier fichier qui existe
        avatar_file = None
        for p in avatar_paths:
            if p and Path(p).exists() and Path(p).stat().st_size > 100:
                avatar_file = p; break

        SIZE = 32
        if avatar_file:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    avatar_file, SIZE, SIZE, True)
                # Rogner en cercle via Cairo
                import cairo as _cairo, math as _math
                surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, SIZE, SIZE)
                ctx  = _cairo.Context(surf)
                ctx.arc(SIZE/2, SIZE/2, SIZE/2, 0, 2*_math.pi)
                ctx.clip()
                Gdk.cairo_set_source_pixbuf(ctx, pb, 0, 0)
                ctx.paint()
                import io, array as _arr
                data = bytes(surf.get_data())
                raw  = _arr.array('B', data)
                rgba = bytearray(len(raw))
                for i in range(0, len(raw), 4):
                    b2,g2,r2,a2 = raw[i],raw[i+1],raw[i+2],raw[i+3]
                    rgba[i],rgba[i+1],rgba[i+2],rgba[i+3] = r2,g2,b2,a2
                loader = GdkPixbuf.PixbufLoader.new_with_type('png')
                buf2 = io.BytesIO()
                surf.write_to_png(buf2)
                loader.write(buf2.getvalue()); loader.close()
                pb_round = loader.get_pixbuf()
                img = Gtk.Image.new_from_pixbuf(pb_round)
                img.set_pixel_size(SIZE)
                img.set_size_request(SIZE, SIZE)
                img.set_tooltip_text(user)
                img.set_margin_start(4); img.set_margin_end(2)
                return img
            except Exception as e:
                print('[avatar]', e)

        # Fallback : initiales
        initials = ''.join(w[0].upper() for w in user.split('_')[:2]) or user[:2].upper()
        lbl = Gtk.Label(label=initials)
        lbl.add_css_class('avatar-initials')
        lbl.set_tooltip_text(user)
        return lbl

    def _update_header_note_title(self):
        if hasattr(self, '_header_subtitle'):
            if self._current_file:
                name = self._current_file.stem.replace('_', ' ')
                self._header_subtitle.set_text(name)
            else:
                self._header_subtitle.set_text('md-editor v0.5')

    def _start_scan(self):
        self._known_files = set(self._list_notes())
        self._scan_source_id = GLib.timeout_add(SCAN_INTERVAL_MS, self._scan_notes_dir)

    def _scan_notes_dir(self):
        current = set(self._list_notes())
        new_files = current - self._known_files
        if new_files:
            self._known_files = current
            self._refresh_file_list(highlight_new=new_files)
            self._set_status("Nouveau fichier : " + ", ".join(f.name for f in new_files), "ok")
        elif current != self._known_files:
            self._known_files = current; self._refresh_file_list()
        return True

    # ── Paramètres ────────────────────────────────────────────────────────────

    def _apply_font_size(self, size):
        """Applique la taille et la famille de police à l'UI GTK et à la preview."""
        global _CURRENT_FONT_SIZE, _CURRENT_FONT_FAMILY
        _CURRENT_FONT_SIZE = size
        fam = self._config.get('font_family', 'Noto Sans')
        _CURRENT_FONT_FAMILY = fam
        css = (
            "* { font-size: " + str(size) + "px; }"
            " textview text { font-family: '" + fam + "',sans-serif; }"
            " .editor-view { font-size: " + str(size) + "px;"
            " font-family: '" + fam + "',sans-serif; }"
            " .editor-view text { font-size: " + str(size) + "px;"
            " font-family: '" + fam + "',sans-serif; }"
            " .file-name { font-size: " + str(max(size-2,8)) + "px; }"
            " .file-name-active { font-size: " + str(max(size-2,8)) + "px; }"
            " .file-date { font-size: " + str(max(size-4,7)) + "px; }"
            " .panel-label-edit { font-size: " + str(max(size-2,8)) + "px; }"
            " .panel-label-preview { font-size: " + str(max(size-2,8)) + "px; }"
            " .panel-label-files { font-size: " + str(max(size-2,8)) + "px; }"
            " .panel-label-tags { font-size: " + str(max(size-2,8)) + "px; }"
        ).encode()
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Rafraîchir les chips Cairo (elles lisent _CURRENT_FONT_SIZE dynamiquement)
        self.queue_draw()
        # Rafraîchir les listes d'étiquettes pour recréer les chips
        self._refresh_tags_list()
        self._refresh_file_list()
        if not self._preview_pending:
            self._preview_pending = True
            GLib.timeout_add(50, self._refresh_preview)

    def _on_settings(self, _):
        dialog = SettingsDialog(self, self._config)
        dialog.connect("response", self._on_settings_response); dialog.present()

    def _on_settings_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            self._config.update(dialog.get_config())
            self._lt_language = self._config["lt_language"]
            save_config(self._config); self._known_files = set()
            # Réappliquer police + taille
            self._apply_font_size(self._config.get("font_size", 14))
            self._refresh_file_list(); self._set_status("Parametres enregistres.", "ok")
        dialog.destroy()

    # ── Texte ─────────────────────────────────────────────────────────────────

    def _on_text_changed(self, _):
        if self._loading_file:
            return  # set_text en cours — pas une vraie modification
        if self._current_file:
            self._unsaved_files.add(self._current_file)
        else:
            self._unsaved_files.add('__new__')
        # Redessiner le TreeView pour l'icône ●
        self._tree_view.queue_draw()
        if not self._preview_pending:
            self._preview_pending = True
            GLib.timeout_add(PREVIEW_DEBOUNCE, self._refresh_preview)

        if not self._syn_pending:
            self._syn_pending = True
            GLib.timeout_add(150, self._apply_syntax_highlight)

    def _apply_syntax_highlight(self):
        """Coloration syntaxique Markdown."""
        self._syn_pending = False
        buf  = self._buffer
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        n    = len(text)
        if not n: return False

        s0, e0 = buf.get_start_iter(), buf.get_end_iter()
        for tag in self._syn_tags.values():
            buf.remove_tag(tag, s0, e0)

        def ap(tag_name, s, e):
            s = max(0, min(s, n-1)); e = max(s+1, min(e, n))
            if s >= e or tag_name not in self._syn_tags: return
            try:
                buf.apply_tag(self._syn_tags[tag_name],
                    buf.get_iter_at_offset(s), buf.get_iter_at_offset(e))
            except Exception: pass

        import re as _re

        # Front matter YAML
        fm = _re.match(r'(?s)^---\n.*?\n---', text)
        if fm:
            ap('frontmatter', 0, fm.end())
            body_start = fm.end()
        else:
            body_start = 0

        body   = text[body_start:]
        offset = body_start

        # Blocs de code fencés
        fence_ranges = []
        for m in _re.finditer(r'(?ms)^(?:```|~~~)[^\n]*\n.*?^(?:```|~~~)[ \t]*$', body):
            ap('fence', offset+m.start(), offset+m.end())
            fence_ranges.append((m.start(), m.end()))

        def in_fence(p): return any(fs <= p < fe for fs, fe in fence_ranges)

        def inline(line_text, ls):
            o = offset + ls
            # Gras **
            for m in _re.finditer(r'\*\*(.+?)\*\*', line_text):
                ap('bold', o+m.start(), o+m.end())
                ap('marker', o+m.start(), o+m.start()+2)
                ap('marker', o+m.end()-2, o+m.end())
            # Italique * (pas **)
            for m in _re.finditer(r'(?<![*])\*(?![*\s])(.+?)(?<![\s*])\*(?![*])', line_text):
                ap('italic', o+m.start(), o+m.end())
                ap('marker', o+m.start(), o+m.start()+1)
                ap('marker', o+m.end()-1, o+m.end())
            # Barré ~~
            for m in _re.finditer(r'~~(.+?)~~', line_text):
                ap('strike', o+m.start(), o+m.end())
            # Code inline `
            for m in _re.finditer(r'`[^`\n]+`', line_text):
                ap('code', o+m.start(), o+m.end())
            # Image ![alt](url)
            for m in _re.finditer(r'!\[[^\]]*\]\([^)]+\)', line_text):
                ap('img', o+m.start(), o+m.end())
            # Lien [texte](url)
            for m in _re.finditer(r'(?<!!)\[[^\]]+\]\([^)]+\)', line_text):
                ap('link', o+m.start(), o+m.end())

        pos = 0
        for line in body.splitlines(keepends=True):
            ls       = pos
            le       = pos + len(line)
            pos     += len(line)
            stripped = line.rstrip('\n')
            if in_fence(ls): continue

            hm = _re.match(r'^(#{1,4}) (.+)', stripped)
            if hm:
                lvl = len(hm.group(1))
                ap('h'+str(lvl), offset+ls, offset+le)
                ap('marker', offset+ls, offset+ls+lvl+1)
            elif _re.match(r'^[-*_]{3,}\s*$', stripped):
                ap('hr', offset+ls, offset+le)
            elif stripped.startswith('> '):
                ap('quote', offset+ls, offset+le)
                ap('marker', offset+ls, offset+ls+2)
            elif _re.match(r'^\s*([-*+]|\d+\.) ', stripped):
                lm = _re.match(r'^(\s*(?:[-*+]|\d+\.) )', stripped)
                if lm: ap('marker', offset+ls, offset+ls+len(lm.group(1)))

            inline(line, ls)

        GLib.idle_add(self._highlight_current_line)
        return False


    def _on_webview_load_changed(self, webview, load_event):
        """Rescroller la preview à la bonne position après chargement du HTML."""
        if int(load_event) == 3:
            self._preview_loaded = True
            anchor   = getattr(self, '_preview_anchor', None)
            fraction = getattr(self, '_preview_scroll_fraction', 0.0)
            def _restore():
                anchor = getattr(self, '_preview_anchor', None)
                if anchor is not None:  # anchor = numéro de ligne
                    js2 = (
                        "(function(){"
                        "var el,i;"
                        "for(i=" + str(anchor) + ";i>=0;i--){"
                        "  el=document.getElementById('mdl-'+i);"
                        "  if(el){el.scrollIntoView({block:'start',behavior:'instant'});break;}"
                        "}"
                        "})();"
                    )
                    self._run_js(js2)
                elif fraction > 0.0:
                    self._scroll_preview_to_fraction(fraction)
                return False
            GLib.timeout_add(60, _restore)

    def _draw_gutter(self, area, ctx, width, height, _=None):
        """
        Dessine la gouttière des numéros de ligne en Cairo.
        Synchronisée avec le scroll du TextView.
        """
        import math

        # Couleurs
        bg_r, bg_g, bg_b = 0x0d/255, 0x0d/255, 0x14/255   # #0d0d14
        fg_r, fg_g, fg_b = 0xaa/255, 0xaa/255, 0xbb/255   # numéros normaux
        hl_r, hl_g, hl_b = 0x7c/255, 0x8c/255, 0xf8/255   # numéro ligne courante

        # Fond de la gouttière
        ctx.set_source_rgb(bg_r, bg_g, bg_b)
        ctx.rectangle(0, 0, width, height)
        ctx.fill()

        buf = self._buffer
        n_lines = buf.get_line_count()
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()

        # Obtenir le décalage vertical du TextView dans la ScrolledWindow
        vadj = self._scroll_edit.get_vadjustment()
        scroll_y = vadj.get_value() if vadj else 0

        # Police
        ctx.select_font_face('monospace', 0, 0)
        font_sz = max(9, int(_CURRENT_FONT_SIZE * 0.82))
        ctx.set_font_size(font_sz)
        line_h = font_sz * 1.7  # approximation hauteur ligne

        top_margin = self._view.get_top_margin()

        for lineno in range(n_lines):
            # Position Y de cette ligne dans le TextView
            it = _iter_at_line(buf, lineno)
            rect = self._view.get_iter_location(it)
            # Convertir en coordonnées fenêtre
            wx, wy = self._view.buffer_to_window_coords(
                Gtk.TextWindowType.TEXT, rect.x, rect.y)
            # Position Y dans la gouttière (même scroll)
            y_gutter = wy + top_margin
            if y_gutter < -line_h or y_gutter > height + line_h:
                continue

            is_current = (lineno == cursor_line)

            if is_current:
                # Fond accent sur la ligne courante
                ctx.set_source_rgba(hl_r, hl_g, hl_b, 0.12)
                ctx.rectangle(0, y_gutter, width, line_h)
                ctx.fill()
                # Numéro en couleur accent vif
                ctx.set_source_rgb(hl_r, hl_g, hl_b)
            else:
                ctx.set_source_rgb(fg_r, fg_g, fg_b)

            # Numéro
            label = str(lineno + 1)
            ext   = ctx.text_extents(label)
            tx    = width - ext.width - 8
            ty    = y_gutter + (line_h - ext.height) / 2 - ext.y_bearing
            ctx.move_to(tx, ty)
            ctx.show_text(label)

    # ── Barre d'outils Markdown ───────────────────────────────────────────

    def _md_wrap(self, marker_start, marker_end=None):
        me = marker_end or marker_start
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
            t = buf.get_text(s, e, False)
            buf.delete(s, e)
            buf.insert_at_cursor(marker_start + t + me)
        else:
            buf.insert_at_cursor(marker_start + me)
            it = buf.get_iter_at_mark(buf.get_insert())
            it.backward_chars(len(me))
            buf.place_cursor(it)
        self._view.grab_focus()

    def _md_prefix_lines(self, prefix):
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
        else:
            s = buf.get_iter_at_mark(buf.get_insert()); e = s.copy()
        s.set_line_offset(0)
        if not e.starts_line(): e.forward_to_line_end()
        text = buf.get_text(s, e, False)
        new  = '\n'.join(prefix + l for l in text.split('\n'))
        buf.delete(s, e); buf.insert(s, new)
        self._view.grab_focus()

    def _md_bold(self, _):        self._md_wrap('**')
    def _md_italic(self, _):      self._md_wrap('*')
    def _md_strike(self, _):      self._md_wrap('~~')
    def _md_code_inline(self, _): self._md_wrap('`')
    def _md_quote(self, _):       self._md_prefix_lines('> ')
    def _md_h1(self, _):          self._md_prefix_lines('# ')
    def _md_h2(self, _):          self._md_prefix_lines('## ')
    def _md_h3(self, _):          self._md_prefix_lines('### ')
    def _md_checkbox(self, _):    self._md_prefix_lines('- [ ] ')

    def _md_ul(self, _):
        self._md_prefix_lines('- ')

    def _md_ol(self, _):
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
        else:
            s = buf.get_iter_at_mark(buf.get_insert()); e = s.copy()
        s.set_line_offset(0)
        if not e.starts_line(): e.forward_to_line_end()
        text = buf.get_text(s, e, False)
        new  = '\n'.join(str(i+1)+'. '+l for i,l in enumerate(text.split('\n')))
        buf.delete(s, e); buf.insert(s, new)
        self._view.grab_focus()

    def _md_hr(self, _):
        buf = self._buffer
        it  = buf.get_iter_at_mark(buf.get_insert())
        it.forward_to_line_end(); buf.place_cursor(it)
        buf.insert_at_cursor('\n\n---\n\n')
        self._view.grab_focus()

    def _md_link(self, _):
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
            t = buf.get_text(s, e, False)
            buf.delete(s, e)
            buf.insert_at_cursor('[' + t + '](https://)')
        else:
            buf.insert_at_cursor('[texte](https://)')
        self._view.grab_focus()

    def _md_image(self, _):
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
            t = buf.get_text(s, e, False)
            buf.delete(s, e)
            buf.insert_at_cursor('![' + t + '](chemin/image.png)')
        else:
            buf.insert_at_cursor('![description](chemin/image.png)')
        self._view.grab_focus()

    def _md_code_block(self, _):
        buf = self._buffer
        if buf.get_has_selection():
            s, e = buf.get_selection_bounds()
            t = buf.get_text(s, e, False)
            buf.delete(s, e)
            buf.insert_at_cursor('```\n' + t + '\n```')
        else:
            buf.insert_at_cursor('```python\n\n```')
            it = buf.get_iter_at_mark(buf.get_insert())
            it.backward_chars(4); buf.place_cursor(it)
        self._view.grab_focus()

    def _md_insert_image_file(self, _):
        """Ouvre un sélecteur de fichier image, copie dans notes/data/ et insère."""
        dialog = Gtk.FileDialog()
        dialog.set_title('Choisir une image')
        # Filtre images
        f = Gtk.FileFilter()
        f.set_name('Images')
        for pat in ['*.png','*.jpg','*.jpeg','*.gif','*.webp','*.svg','*.bmp']:
            f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        dialog.open(self, None, self._md_insert_image_done)

    def _md_insert_image_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except Exception: return
        if not file: return
        import shutil
        src_path = Path(file.get_path())

        # Répertoire de destination : notes/data/
        data_dir = self._notes_dir() / 'data'
        data_dir.mkdir(exist_ok=True)

        # Nom de base = stem du fichier Markdown en cours
        base = self._current_file.stem if self._current_file else 'note'
        ext  = src_path.suffix.lower()

        # Trouver le prochain indice : base__image<N>
        idx = 1
        while True:
            dest_name = base + '__image' + str(idx) + ext
            dest_path = data_dir / dest_name
            if not dest_path.exists():
                break
            idx += 1

        try:
            shutil.copy2(str(src_path), str(dest_path))
        except Exception as ex:
            self._set_status('Erreur copie image : ' + str(ex), 'err')
            return

        # Chemin absolu dans le Markdown
        abs_path = str(dest_path)
        alt = src_path.stem.replace('_', ' ')
        md_img = '![' + alt + '](' + abs_path + ')'
        self._buffer.insert_at_cursor(md_img)
        self._set_status('Image copiee : ' + dest_name, 'ok')
        self._view.grab_focus()

    def _md_table(self, _):
        self._buffer.insert_at_cursor(
            '| Colonne 1 | Colonne 2 | Colonne 3 |\n'
            '|-----------|-----------|-----------|\n'
            '| cellule   | cellule   | cellule   |\n'
            '| cellule   | cellule   | cellule   |')
        self._view.grab_focus()

    def _on_toggle_gutter(self, btn):
        """Affiche ou masque la gouttière des numéros de ligne."""
        visible = btn.get_active()
        self._gutter.set_visible(visible)
        # Masquer aussi le séparateur gutter/éditeur
        sep = self._gutter.get_next_sibling()
        if sep:
            sep.set_visible(visible)

    def _on_nav_key_pressed(self, ctrl, keyval, keycode, state):
        """Scroll éditeur + déplacement curseur ligne par ligne + sync preview."""
        vadj = self._scroll_edit.get_vadjustment()
        if not vadj: return False

        cur  = vadj.get_value()
        pgsz = vadj.get_page_size()
        top  = vadj.get_lower()
        bot  = vadj.get_upper() - pgsz
        buf  = self._buffer
        it   = buf.get_iter_at_mark(buf.get_insert())

        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            # Déplacer curseur d'une ligne vers le haut
            it.backward_line()
            buf.place_cursor(it)
            self._view.scroll_to_iter(it, 0.0, True, 0.0, 0.3)
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            # Déplacer curseur d'une ligne vers le bas
            it.forward_line()
            buf.place_cursor(it)
            self._view.scroll_to_iter(it, 0.0, True, 0.0, 0.7)
        elif keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            new_val = max(top, cur - pgsz)
            vadj.set_value(new_val)
            r = self._view.get_iter_at_location(10, int(new_val) + int(pgsz * 0.5))
            it2 = r[1] if isinstance(r, tuple) else r
            if it2: buf.place_cursor(it2)
        elif keyval in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down):
            new_val = min(bot, cur + pgsz)
            vadj.set_value(new_val)
            r = self._view.get_iter_at_location(10, int(new_val) + int(pgsz * 0.5))
            it2 = r[1] if isinstance(r, tuple) else r
            if it2: buf.place_cursor(it2)
        elif keyval == Gdk.KEY_Home:
            # Début de la ligne courante
            it.set_line_offset(0)
            buf.place_cursor(it)
        elif keyval == Gdk.KEY_End:
            # Fin de la ligne courante
            if not it.ends_line(): it.forward_to_line_end()
            buf.place_cursor(it)
        else:
            return False

        GLib.timeout_add(30, self._sync_preview_scroll)
        return True

    def _on_nav_key_released(self, ctrl, keyval, keycode, state):
        """Déclenche la sync preview après relâchement d'une touche de navigation."""
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down,
                      Gdk.KEY_Page_Up, Gdk.KEY_Page_Down,
                      Gdk.KEY_Home, Gdk.KEY_End,
                      Gdk.KEY_KP_Up, Gdk.KEY_KP_Down,
                      Gdk.KEY_KP_Page_Up, Gdk.KEY_KP_Page_Down):
            GLib.timeout_add(50, self._sync_preview_scroll)

    def _sync_preview_scroll(self):
        """Synchronise la preview via fraction curseur/total lignes."""
        if not self._preview_scroll_sync: return False
        buf = self._buffer
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        total = max(1, buf.get_line_count() - 1)
        fraction = cursor_line / total
        js = (
            "(function(){"
            "var h=document.body.scrollHeight-window.innerHeight;"
            "if(h>0)window.scrollTo(0,h*" + str(fraction) + ");"
            "})();"
        )
        self._run_js(js)
        return False

    def _on_editor_click_capture(self, gesture, n, x, y):
        """Capture le clic AVANT le focus pour sauvegarder la position de scroll."""
        vadj = self._scroll_edit.get_vadjustment()
        if vadj:
            self._scroll_before_focus = vadj.get_value()

    def _on_editor_focus_in(self, _):
        """
        Restaure la position de scroll après que GTK l'a réinitialisée au focus.
        """
        vadj = self._scroll_edit.get_vadjustment()
        if not vadj: return
        saved = getattr(self, '_scroll_before_focus', None)
        if saved is not None and saved > 10:
            # Restaurer après que GTK a fini de scroller
            def _restore_scroll():
                vadj.set_value(saved)
                return False
            GLib.idle_add(_restore_scroll)

    def _on_cursor_for_gutter(self, buf, _):
        """Rafraîchit la gouttière et surligne la ligne courante."""
        self._gutter.queue_draw()
        # Surlignage ligne courante dans le buffer
        self._highlight_current_line()

    def _highlight_current_line(self):
        """Applique un fond subtil sur la ligne du curseur.
        Priorité basse : créé avant les syn_tags donc écrasé par eux."""
        if not hasattr(self, '_tag_current_line') or self._tag_current_line is None:
            # Priorité basse : insérer au début de la table de tags
            self._tag_current_line = self._buffer.create_tag(
                'current_line', background='#16162a',
                paragraph_background='#16162a')
            # Mettre ce tag en priorité basse via set_priority
            try:
                self._tag_current_line.set_priority(0)
            except Exception:
                pass
        # Effacer l'ancien surlignage
        s = self._buffer.get_start_iter()
        e = self._buffer.get_end_iter()
        self._buffer.remove_tag(self._tag_current_line, s, e)
        # Appliquer sur la ligne courante
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        line_start = cursor.copy(); line_start.set_line_offset(0)
        line_end   = cursor.copy()
        if not line_end.ends_line(): line_end.forward_to_line_end()
        self._buffer.apply_tag(self._tag_current_line, line_start, line_end)

    def _on_cursor_moved(self, buf, _):
        """Synchronise le scroll de la preview avec la position du curseur."""


    def _run_js(self, js):
        try:
            self._webview.evaluate_javascript(js, -1, None, None, None, None)
        except Exception:
            try: self._webview.run_javascript(js, None, None, None)
            except Exception: pass

    def _scroll_preview_to_fraction(self, fraction):
        js = ("(function(){var h=document.body.scrollHeight-window.innerHeight;"
              "if(h>0)window.scrollTo(0,h*" + str(fraction) + ");})();")
        self._run_js(js)

    def _on_editor_scrolled(self, vadj):
        """Synchronise la preview avec le scroll de l'éditeur."""
        if not self._preview_scroll_sync: return
        adj_val  = vadj.get_value()
        adj_max  = max(1, vadj.get_upper() - vadj.get_page_size())
        fraction = min(1.0, adj_val / adj_max)
        # Titre le plus proche au-dessus de la zone visible
        import re as _re
        buf   = self._buffer
        result = self._view.get_iter_at_location(0, int(adj_val))
        top_line = (result[1] if isinstance(result, tuple) else result).get_line()
        text  = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        lines = text.split('\n')
        anchor = None
        for i in range(min(top_line, len(lines)-1), -1, -1):
            hm = _re.match(r'^(#{1,4}) (.+)', lines[i])
            if hm:
                t = hm.group(2).strip().lower()
                t = _re.sub(r'[^\w\s-]', '', t)
                t = _re.sub(r'\s+', '-', t.strip())
                anchor = t; break
        if anchor:
            js = ("(function(){"
                  "var e=document.getElementById('" + anchor + "');"
                  "if(e){e.scrollIntoView({block:'start',behavior:'instant'});"
                  "window.scrollBy(0,window.innerHeight*0.1);}"
                  "else{var h=document.body.scrollHeight-window.innerHeight;"
                  "if(h>0)window.scrollTo(0,h*" + str(fraction) + ");}"
                  "})();")
        else:
            js = ("(function(){var h=document.body.scrollHeight-window.innerHeight;"
                  "if(h>0)window.scrollTo(0,h*" + str(fraction) + ");})();")
        self._run_js(js)

    def _update_page_info(self):
        """Estime le nombre de pages A4 et la page courante."""
        if not hasattr(self, '_page_info_lbl'): return
        text = self._get_text()
        if not text.strip():
            self._page_info_lbl.set_text(''); return
        CHARS_PER_PAGE = 3000
        plain = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        plain = re.sub(r'[#*_`\[\]|~>]', '', plain)
        plain = re.sub(r'https?://\S+', '', plain)
        plain = plain.strip()
        total_pages = max(1, int(len(plain) / CHARS_PER_PAGE) + 1)
        buf = self._buffer
        # Page courante par proportion de texte plain avant le curseur
        cur_iter = buf.get_iter_at_mark(buf.get_insert())
        text_before = buf.get_text(buf.get_start_iter(), cur_iter, False)
        plain_before = re.sub(r'```.*?```', '', text_before, flags=re.DOTALL)
        plain_before = re.sub(r'[#*_`\[\]|~>]', '', plain_before)
        plain_before = re.sub(r'https?://\S+', '', plain_before)
        frac = len(plain_before) / max(1, len(plain))
        cur_page = max(1, min(total_pages, int(frac * total_pages) + 1))
        self._page_info_lbl.set_markup(
            f'<span foreground="#6c7086">Page </span>'
            f'<span foreground="#89b4fa"><b>{cur_page}</b></span>'
            f'<span foreground="#6c7086"> / </span>'
            f'<span foreground="#cdd6f4"><b>{total_pages}</b></span>'
            f'<span foreground="#6c7086"> pages A4 estimées</span>'
        )

    def _refresh_preview(self):
        self._preview_pending = False
        # Si aucune note ouverte → preview vide
        if not self._current_file:
            self._webview.load_html(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<style>body{background:#0d0d14;margin:0;padding:0;}</style>'
                '</head><body></body></html>',
                'file:///')
            return False

        # Sauvegarder la fraction de scroll depuis la position de l'éditeur
        vadj = self._scroll_edit.get_vadjustment()
        if vadj:
            adj_max = vadj.get_upper() - vadj.get_page_size()
            self._preview_scroll_fraction = (
                vadj.get_value() / adj_max if adj_max > 0 else 0.0)
        # Sauvegarder la ligne visible en haut pour le premier chargement
        if vadj:
            r3 = self._view.get_iter_at_location(0, int(vadj.get_value()) + 5)
            it3 = r3[1] if isinstance(r3, tuple) else r3
            self._preview_anchor = it3.get_line() if it3 else None
        else:
            self._preview_anchor = None

        # Récupérer les étiquettes de la note courante
        note_tags = None
        rows = self._db.get_tags_for_note(self._current_file)
        if rows:
            note_tags = [dict(r) for r in rows]

        if getattr(self, '_preview_loaded', False):
            # Preview déjà chargée : mettre à jour via JS sans recharger
            html = md_to_html(self._get_text(), note_tags)
            # Extraire uniquement le body
            import re as _re
            body_m = _re.search(r'<body>(.*)</body>', html, _re.DOTALL)
            body_content = body_m.group(1) if body_m else html
            # Échapper pour JS
            body_escaped = body_content.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
            # Fraction du curseur dans le document
            buf_c    = self._buffer
            cur_line = buf_c.get_iter_at_mark(buf_c.get_insert()).get_line()
            total    = max(1, buf_c.get_line_count() - 1)
            frac     = cur_line / total
            # Si on est dans les 15% du bas → forcer scroll en bas
            if frac >= 0.85:
                scroll_cmd = 'window.scrollTo(0,document.body.scrollHeight);'
            else:
                scroll_cmd = ('var h=document.body.scrollHeight-window.innerHeight;'
                              'if(h>0)window.scrollTo(0,h*' + str(frac) + ');')
            js = (
                '(function(){'
                'var tmp=document.createElement("body");'
                'tmp.innerHTML=`' + body_escaped + '`;'
                'morphdom(document.body,tmp,{childrenOnly:true});'
                'requestAnimationFrame(function(){'  
                + scroll_cmd +
                '});'
                '})();'
            )
            self._run_js(js)
        else:
            # Premier chargement
            _base = 'file://' + str(self._notes_dir()) + '/'
            self._webview.load_html(md_to_html(self._get_text(), note_tags), _base)
        GLib.idle_add(self._update_page_info)
        return False

    # ── LanguageTool ──────────────────────────────────────────────────────────

    def _on_toggle_focus_mode(self, btn):
        """Mode focus : masque le panneau fichiers et étiquettes."""
        if btn.get_active():
            # Sauvegarder positions
            self._focus_saved_outer = self._outer_paned.get_position()
            self._focus_saved_main  = self._main_paned.get_position()
            self._focus_mode = True
            # Masquer panneau fichiers via set_visible
            self._files_box.set_visible(False)
            # Masquer panneau étiquettes
            self._tags_panel_widget.set_visible(False)
            self._outer_paned.set_position(
                self._outer_paned.get_width() or 9999)
            btn.set_icon_name('view-restore-symbolic')
            btn.set_tooltip_text('Quitter le mode focus')
        else:
            # Restaurer
            self._focus_mode = False
            self._files_box.set_visible(True)
            if self._focus_saved_main is not None:
                self._main_paned.set_position(self._focus_saved_main)
            if self._focus_saved_outer is not None:
                self._outer_paned.set_position(self._focus_saved_outer)
            if self._tags_panel_visible:
                self._tags_panel_widget.set_visible(True)
            btn.set_icon_name('view-fullscreen-symbolic')
            btn.set_tooltip_text('Mode focus : éditeur + preview uniquement')

    def _on_toggle_tags_panel(self, btn):
        """Affiche ou masque le panneau étiquettes à droite."""
        if btn.get_active():
            # Afficher
            self._tags_panel_widget.set_visible(True)
            if self._outer_paned_saved_pos is not None:
                self._outer_paned.set_position(self._outer_paned_saved_pos)
            self._tags_panel_visible = True
        else:
            # Masquer : sauvegarder la position et pousser au maximum
            self._outer_paned_saved_pos = self._outer_paned.get_position()
            self._outer_paned.set_position(self._outer_paned.get_width())
            self._tags_panel_widget.set_visible(False)
            self._tags_panel_visible = False

    def _on_lt_toggle(self, btn):
        if btn.get_active():
            btn.add_css_class("lt-btn-on")
            self._lt_enabled = True
            self._trigger_lt_check()
        else:
            btn.remove_css_class("lt-btn-on")
            self._lt_enabled = False
            self._clear_lt_highlights()
            self._lt_matches = []
            self._set_status("LanguageTool désactivé", "")

    def _on_lang_changed(self, combo, _):
        langs = ["fr","fr-FR","en-US","en-GB","de-DE","es","it","auto"]
        self._lt_language = langs[min(combo.get_selected(), len(langs)-1)]
        if self._lt_enabled: self._trigger_lt_check()

    def _trigger_lt_check(self):
        self._lt_pending = False
        if not self._lt_enabled: return False
        text = self._get_text()
        self._set_status("LanguageTool : verification...", "busy")
        threading.Thread(target=self._lt_worker, args=(text,), daemon=True).start()
        return False

    def _lt_worker(self, text):
        if not text.strip():
            GLib.idle_add(self._lt_done, text, [])
            return
        import traceback
        try:
            matches, _ = check_languagetool(text, self._lt_language)
        except Exception:
            traceback.print_exc()
            matches = []
        GLib.idle_add(self._lt_done, text, matches)
    def _lt_done(self, orig, matches):
        self._lt_matches = matches; self._apply_lt_highlights(matches)
        api_err = [m for m in matches if m.get("_error")]
        real    = [m for m in matches if not m.get("_error")]
        errors  = [m for m in real if m.get("rule",{}).get("issueType") not in ("style","whitespace")]
        if api_err: self._set_status("LT erreur : " + api_err[0]["_error"], "err")
        elif not real: self._set_status("LanguageTool : aucun probleme", "ok")
        else: self._set_status(
            "LT : " + str(len(errors)) + " erreur(s), " + str(len(real)-len(errors)) + " suggestion(s)",
            "err" if errors else "ok")
        # Repasser le bouton en inactif une fois la correction terminée
        self._lt_enabled = False
        self._btn_lt.handler_block_by_func(self._on_lt_toggle)
        self._btn_lt.set_active(False)
        self._btn_lt.remove_css_class("lt-btn-on")
        self._btn_lt.handler_unblock_by_func(self._on_lt_toggle)
        return False

    def _apply_lt_highlights(self, matches):
        self._clear_lt_highlights()
        for m in matches:
            if m.get("_error"): continue
            off=m.get("offset",0); lng=m.get("length",0)
            issue=m.get("rule",{}).get("issueType","grammar")
            tag=(self._tag_style if issue=="style" else
                 self._tag_warning if issue in ("hint","suggestion") else self._tag_error)
            self._buffer.apply_tag(tag,
                self._buffer.get_iter_at_offset(off),
                self._buffer.get_iter_at_offset(off+lng))

    def _clear_lt_highlights(self):
        s,e=self._buffer.get_start_iter(),self._buffer.get_end_iter()
        for t in (self._tag_error,self._tag_warning,self._tag_style):
            self._buffer.remove_tag(t,s,e)

    # ── Menu contextuel LT ────────────────────────────────────────────────────


    # ── Templates ────────────────────────────────────────────────────────────

    TEMPLATES = [
        ("Article Hugo", "hugo_article",
         '---\ntitle: "{title}"\ndate: {dateonly}T00:00:00Z\ndraft: false\n'
         'description: ""\nslug: "{slug}"\ncategories:\n  - ""\n---\n\n'),
        ("Tableau Markdown", "table",
         '| Colonne 1 | Colonne 2 | Colonne 3 |\n'
         '|-----------|-----------|-----------|\n'
         '| valeur    | valeur    | valeur    |\n'),
        ("Bloc de code", "code",
         '```python\n# code ici\n```\n'),
        ("Liste a cocher", "checklist",
         '- [ ] Tache 1\n- [ ] Tache 2\n- [ ] Tache 3\n'),
        ("Citation", "quote",
         '> \n'),
        ("Lien", "link",
         '[texte du lien](https://)'),
        ("Image", "image",
         '![description](chemin/vers/image.png)'),
        ("Front matter YAML", "yaml",
         '---\nkey: value\n---\n\n'),
    ]

    def _on_editor_right_click(self, gesture, n, x, y):
        """Clic milieu dans l'éditeur : menu templates (ferme LT si ouvert)."""
        # Fermer le menu LT s'il est ouvert
        if getattr(self, '_lt_popover', None):
            self._lt_popover.popdown(); self._lt_popover.unparent(); self._lt_popover = None
        if getattr(self, '_outside_gc', None):
            self.remove_controller(self._outside_gc); self._outside_gc = None
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_template_menu(x, y)

    def _show_template_menu(self, x, y):
        """Popover menu de templates insérable à la position curseur."""
        if hasattr(self, '_tmpl_popover') and self._tmpl_popover:
            self._tmpl_popover.unparent()
            self._tmpl_popover = None

        popover = Gtk.Popover()
        popover.set_parent(self._scroll_edit)
        popover.set_has_arrow(True)
        popover.set_autohide(False)   # évite le warning 'non-top most parent'
        self._tmpl_popover = popover

        res = self._view.translate_coordinates(self._scroll_edit, int(x), int(y))
        if res is None:       tx, ty = int(x), int(y)
        elif len(res) == 3:   _, tx, ty = res
        else:                 tx, ty = int(res[0]), int(res[1])
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = tx, ty, 1, 1
        popover.set_pointing_to(rect)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # En-tête
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hdr.add_css_class("lt-popup-header")
        title = Gtk.Label(label="Inserer un template")
        title.add_css_class("lt-popup-msg"); title.set_xalign(0)
        hdr.append(title)
        vbox.append(hdr)
        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        fixes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        fixes.add_css_class("lt-popup-fixes")

        for label, key, _ in self.TEMPLATES:
            if label == "---":  # séparateur
                fixes.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("tmpl-btn")
            btn.set_halign(Gtk.Align.FILL)
            child = btn.get_child()
            if child: child.set_xalign(0)
            btn.connect("clicked", self._on_insert_template, key)
            fixes.append(btn)

        vbox.append(fixes)
        popover.set_child(vbox)
        popover.popup()

    def _on_insert_template(self, _, key):
        """Insère le template ou exécute une action spéciale."""
        # Fermer le menu D'ABORD
        if getattr(self, '_tmpl_popover', None):
            self._tmpl_popover.popdown()
            self._tmpl_popover.unparent()
            self._tmpl_popover = None

        # Trouver le template
        tmpl_text = None
        for _, k, t in self.TEMPLATES:
            if k == key:
                tmpl_text = t
                break
        if tmpl_text is None:
            return

        # Substitutions dynamiques
        now = datetime.now()
        raw_title = extract_title(self._get_text())  # underscores
        title     = raw_title.replace("_", " ")       # espaces pour affichage
        slug      = raw_title.lower().replace("_", "-")
        tmpl_text = tmpl_text.replace("{title}",    title)
        tmpl_text = tmpl_text.replace("{slug}",     slug)
        tmpl_text = tmpl_text.replace("{date}",     now.strftime("%Y-%m-%d"))
        tmpl_text = tmpl_text.replace("{dateonly}", now.strftime("%Y-%m-%d"))
        tmpl_text = tmpl_text.replace("{datetime}", now.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Remplacer \n par vrais sauts de ligne
        tmpl_text = tmpl_text.replace("\\n", "\n")

        # Insérer à la position du curseur (ou remplacer la sélection)
        self._buffer.begin_user_action()
        if self._buffer.get_has_selection():
            self._buffer.delete_selection(True, True)
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        self._buffer.insert(cursor, tmpl_text)
        self._buffer.end_user_action()

    def _do_send_mail(self, subject, text):
        """
        Ouvre Thunderbird avec subject et body.
        Stratégie : fichier temporaire pour le body (évite les problèmes
        d'échappement des virgules/guillemets dans -compose).
        """
        import shutil

        # Copier dans le presse-papier
        try:
            clipboard = Gdk.Display.get_default().get_clipboard()
            clipboard.set(text)
        except Exception:
            pass

        try:
            if shutil.which('thunderbird'):
                # Thunderbird -compose : toutes les valeurs doivent être
                # URL-encodées (safe='') pour que les virgules dans le texte
                # ne soient pas interprétées comme séparateurs de champs
                compose = (
                    'to=,subject=' + urllib.parse.quote(subject, safe='') +
                    ',body='       + urllib.parse.quote(text,    safe=''))
                subprocess.Popen(['thunderbird', '-compose', compose])
            elif shutil.which('xdg-email'):
                subprocess.Popen(['xdg-email', '--subject', subject, '--body', text])
            else:
                mailto = ('mailto:?subject=' + urllib.parse.quote(subject) +
                          '&body=' + urllib.parse.quote(text))
                subprocess.Popen(['xdg-open', mailto])
            self._set_status(
                'Mail ouvert - contenu copie dans le presse-papier', 'ok')
        except Exception as ex:
            self._set_status('Erreur mail : ' + str(ex), 'err')

    def _send_to_thunderbird(self):
        """Envoi mail depuis le menu template (note courante)."""
        text    = self._get_text()
        subject = extract_title(text).replace('_', ' ')
        self._do_send_mail(subject, text)

    def _on_left_click(self, gesture, _, x, y):
        # Fermer le menu template s'il est ouvert
        if getattr(self, '_tmpl_popover', None):
            self._tmpl_popover.popdown(); self._tmpl_popover.unparent(); self._tmpl_popover = None
        bx,by=self._view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET,int(x),int(y))
        res=self._view.get_iter_at_location(bx,by)
        it=res[1] if isinstance(res,tuple) else res
        match=self._find_match_at(it.get_offset())
        if match is not None:
            self._show_lt_popover(match,x,y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_motion(self, controller, x, y):
        bx,by=self._view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET,int(x),int(y))
        res=self._view.get_iter_at_location(bx,by)
        it=res[1] if isinstance(res,tuple) else res
        self._view.set_cursor(Gdk.Cursor.new_from_name("pointer")
                              if self._find_match_at(it.get_offset()) else None)

    def _find_match_at(self, offset):
        for m in self._lt_matches:
            if m.get("_error"): continue
            s=m.get("offset",0)
            if s<=offset<s+m.get("length",0): return m
        return None

    def _show_lt_popover(self, match, x, y):
        if self._lt_popover: self._lt_popover.unparent(); self._lt_popover=None
        popover=Gtk.Popover(); popover.set_parent(self._scroll_edit)
        popover.set_has_arrow(True); popover.set_autohide(False)
        self._lt_popover=popover
        res=self._view.translate_coordinates(self._scroll_edit,int(x),int(y))
        if res is None: tx,ty=int(x),int(y)
        elif len(res)==3: _,tx,ty=res
        else: tx,ty=int(res[0]),int(res[1])
        rect=Gdk.Rectangle(); rect.x,rect.y,rect.width,rect.height=tx,ty,1,1
        popover.set_pointing_to(rect)
        vbox=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=0)
        hdr=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=2); hdr.add_css_class("lt-popup-header")
        msg=Gtk.Label(label=match.get("message","")); msg.add_css_class("lt-popup-msg")
        msg.set_xalign(0); msg.set_wrap(True); msg.set_max_width_chars(42); hdr.append(msg)
        cat=Gtk.Label(label=match.get("rule",{}).get("issueType","").upper())
        cat.add_css_class("lt-popup-cat"); cat.set_xalign(0); hdr.append(cat)
        vbox.append(hdr); vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        fixes=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=0); fixes.add_css_class("lt-popup-fixes")
        reps=[r["value"] for r in match.get("replacements",[])[:6]]
        off=match.get("offset",0); lng=match.get("length",0)
        if reps:
            for rep in reps:
                btn=Gtk.Button(label="->  "+rep); btn.add_css_class("lt-fix-btn")
                btn.set_halign(Gtk.Align.START)
                btn.connect("clicked",self._on_apply_fix,off,lng,str(rep)); fixes.append(btn)
        else:
            fixes.append(Gtk.Label(label="Aucune suggestion"))
        vbox.append(fixes); vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        ign=Gtk.Button(label="Ignorer"); ign.add_css_class("lt-ignore-btn")
        ign.connect("clicked",self._on_ignore_match,match); vbox.append(ign)
        popover.set_child(vbox)
        gc=Gtk.GestureClick(); gc.set_button(0)
        gc.connect("pressed",self._on_outside_click); self.add_controller(gc)
        self._outside_gc=gc; popover.popup()

    def _on_outside_click(self,*_):
        if self._lt_popover: self._lt_popover.popdown(); self._lt_popover.unparent(); self._lt_popover=None
        if self._outside_gc: self.remove_controller(self._outside_gc); self._outside_gc=None

    def _on_apply_fix(self, _, off, lng, rep):
        if self._lt_popover:
            self._lt_popover.popdown(); self._lt_popover.unparent()
            self._lt_popover = None
        # Appliquer la correction
        s = self._buffer.get_iter_at_offset(off)
        e = self._buffer.get_iter_at_offset(off + lng)
        self._buffer.begin_user_action()
        self._buffer.delete(s, e)
        self._buffer.insert(self._buffer.get_iter_at_offset(off), rep)
        self._buffer.end_user_action()
        # Recalculer les offsets de tous les matches suivants
        delta = len(rep) - lng
        updated = []
        for m in self._lt_matches:
            m_off = m.get('offset', 0)
            m_lng = m.get('length', 0)
            if m_off == off and m_lng == lng:
                continue  # supprimer le match corrigé
            if m_off > off:
                m = dict(m)  # copie pour ne pas modifier l'original
                m['offset'] = m_off + delta
            updated.append(m)
        self._lt_matches = updated
        self._apply_lt_highlights(self._lt_matches)

    def _on_ignore_match(self,_,match):
        if self._lt_popover: self._lt_popover.popdown(); self._lt_popover.unparent(); self._lt_popover=None
        if match in self._lt_matches: self._lt_matches.remove(match)
        self._apply_lt_highlights(self._lt_matches)

    # ── Ouvrir / Sauvegarder ─────────────────────────────────────────────────

    def _on_open(self,_):
        d=Gtk.FileDialog(); f=Gtk.FileFilter(); f.set_name("Markdown")
        for p in ("*.md","*.markdown","*.txt"): f.add_pattern(p)
        s=Gio.ListStore.new(Gtk.FileFilter); s.append(f); d.set_filters(s)
        d.open(self,None,self._on_open_done)

    def _on_open_done(self,d,res):
        try:
            file=d.open_finish(res)
            if file:
                path=Path(file.get_path())
                self._loading_file = True
                self._buffer.set_text(path.read_text(encoding="utf-8"))
                self._loading_file = False
                self._current_file = path
                self._unsaved_files.discard(path)
                self._update_title()
                self._tree_view.queue_draw()
                self._refresh_file_list(); self._refresh_note_tags()
        except Exception: pass

    def _on_save(self, _):
        text = self._get_text()
        if self._current_file:
            try:
                new_name = extract_title(text) + ".md"
                new_path = self._current_file.parent / new_name
                if new_path != self._current_file and not new_path.exists():
                    old = self._current_file
                    self._current_file.rename(new_path)
                    self._unsaved_files.discard(old)  # retirer l'ancien
                    self._unsaved_contents.pop(old, None)
                    self._current_file = new_path
                    self._db.rename_note_path(old, new_path)
                self._write_note(self._current_file, text)
                self._unsaved_files.discard(self._current_file)
                self._unsaved_contents.pop(self._current_file, None)
                # Invalider le statut sync : le fichier local diverge du backup
                self._db.set_synced(str(self._current_file), ok=False, sha256=None)
                self._update_title()
                self._tree_view.queue_draw()
                self._set_status("Sauvegarde : " + self._current_file.name, "ok")
                self._refresh_file_list()
            except Exception as ex: self._set_status("Erreur : " + str(ex), "err")
        else:
            self._on_save_to_notes(None)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_text(self):
        return self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), True)

    def _set_status(self, msg, level=""):
        self._status_bar.set_label(msg)
        for c in ("status-ok","status-err","status-busy"): self._status_bar.remove_css_class(c)
        if level: self._status_bar.add_css_class("status-" + level)

# ── App ───────────────────────────────────────────────────────────────────────

class _ScpSettingsDialog(Gtk.Dialog):
    """Dialog de configuration SCP pour la synchronisation distante."""

    def __init__(self, parent, config):
        super().__init__(title='Configuration SCP', transient_for=parent, modal=True)
        self.set_default_size(480, 300)
        self.add_button('Annuler', Gtk.ResponseType.CANCEL)
        self.add_button('Enregistrer', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_start(16); grid.set_margin_end(16)
        grid.set_margin_top(16); grid.set_margin_bottom(16)

        def row(label, widget, r):
            lbl = Gtk.Label(label=label); lbl.set_xalign(1); lbl.set_xalign(0)
            grid.attach(lbl, 0, r, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, r, 1, 1)

        self._host_entry = Gtk.Entry(); self._host_entry.set_placeholder_text('exemple.com ou 192.168.1.1')
        self._host_entry.set_text(config.get('scp_host', ''))
        row('Serveur (host) :', self._host_entry, 0)

        self._user_entry = Gtk.Entry(); self._user_entry.set_placeholder_text('utilisateur')
        self._user_entry.set_text(config.get('scp_user', ''))
        row('Utilisateur :', self._user_entry, 1)

        self._dir_entry = Gtk.Entry(); self._dir_entry.set_placeholder_text('/home/user/notes')
        self._dir_entry.set_text(config.get('scp_remote_dir', ''))
        row('Répertoire distant :', self._dir_entry, 2)

        pwd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._pwd_entry = Gtk.PasswordEntry(); self._pwd_entry.set_show_peek_icon(True)
        self._pwd_entry.set_text(config.get('scp_password', ''))
        self._pwd_entry.set_hexpand(True); pwd_box.append(self._pwd_entry)
        row('Mot de passe :', pwd_box, 3)

        info = Gtk.Label()
        info.set_markup(
            '<span foreground="#6c7086" size="small">'
            'Si mot de passe vide, la connexion utilisera la clé SSH.\n'
            'Si renseigné, les notes seront chiffrées (AES-256) avant envoi.\n'
            'Requis : sshpass installé pour auth par mot de passe.'
            '</span>')
        info.set_xalign(0); info.set_wrap(True)
        grid.attach(info, 0, 4, 2, 1)

        self.get_content_area().append(grid)

    def get_values(self):
        return {
            'scp_host':       self._host_entry.get_text().strip(),
            'scp_user':       self._user_entry.get_text().strip(),
            'scp_remote_dir': self._dir_entry.get_text().strip(),
            'scp_password':   self._pwd_entry.get_text(),
        }


class MdEditorApp(Gtk.Application):
    def __init__(self): super().__init__(application_id="fr.ghis.md-editor")

    def do_activate(self): MdEditorWindow(self).present()


def main(): return MdEditorApp().run(sys.argv)


if __name__ == "__main__": sys.exit(main())
