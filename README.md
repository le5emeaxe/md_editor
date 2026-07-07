# Documentation Technique et Fonctionnelle

> Éditeur de notes Markdown GTK4, mono-fichier Python  
> Juillet 2026 · ~7 400 lignes · Python 3.12 · GTK 4 · WebKitGTK 6.0  
> Auteur : Nitrix 

---
<img width="1920" height="1051" alt="image" src="https://github.com/user-attachments/assets/6a402e8d-890b-491b-a012-e444cf215489" />

---
## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Installation et dépendances](#2-installation-et-dépendances)
3. [Fonctionnalités](#3-fonctionnalités)
4. [Interface utilisateur](#4-interface-utilisateur)
5. [Exports](#5-exports)
6. [Synchronisation et sauvegarde](#6-synchronisation-et-sauvegarde)
7. [Chiffrement](#7-chiffrement)
8. [Architecture technique](#8-architecture-technique)
9. [Base de données](#9-base-de-données)
10. [Configuration](#10-configuration)
11. [Raccourcis clavier](#11-raccourcis-clavier)
12. [Flux de données principaux](#12-flux-de-données-principaux)

---

## 1. Vue d'ensemble

`md_editor.py` est un éditeur de notes Markdown à fichier unique (~7 400 lignes) développé pour Linux avec GTK4. Il combine édition Markdown, prévisualisation temps réel, gestion des notes par étiquettes, exports multi-formats, synchronisation distante chiffrée et correction orthographique via LanguageTool.

### Philosophie

- **Fichier unique** : aucune installation complexe, un seul `.py` suffit
- **Fichiers locaux** : les notes sont des fichiers `.md` standards sur le disque
- **Pas de cloud propriétaire** : sync via SCP vers n'importe quel serveur SSH
- **Chiffrement optionnel** : AES-256-CBC local et sur le backup distant

---

## 2. Installation et dépendances

### Dépendances Python

```bash
pip install markdown pygments weasyprint cryptography --break-system-packages
```

| Package | Usage |
|---|---|
| `markdown` | Rendu HTML de la prévisualisation |
| `pygments` | Coloration syntaxique dans la preview |
| `weasyprint` | Export PDF |
| `cryptography` | Chiffrement AES-256-CBC local et SCP |

### Dépendances système (Manjaro / Arch)

```bash
sudo pacman -S python-gobject gtk4 webkitgtk-6.0 pandoc texlive-most sshpass
```

| Paquet | Usage |
|---|---|
| `python-gobject` | Bindings GTK4 pour Python |
| `gtk4` | Interface graphique |
| `webkitgtk-6.0` | Prévisualisation HTML inline |
| `pandoc` | Export ODT, LaTeX |
| `texlive-most` | Compilation LaTeX → PDF (xelatex) |
| `sshpass` | Authentification SCP par mot de passe |

### Lancement

```bash
python3 md_editor.py
```

---

## 3. Fonctionnalités

### 3.1 Édition Markdown

- **Buffer GTK4** (`GtkSourceView`) avec coloration syntaxique Markdown
- **Toolbar de formatage** : gras, italique, barré, code inline, H1/H2/H3, listes à puces, listes numérotées, cases à cocher, liens, images, blocs de code, citations, tableaux, règles horizontales, retrait, avance
- **Gouttière** : numéros de lignes dessinés via Cairo, synchronisés avec le scroll
- **Indentation** : `Tab` / `Shift+Tab` pour indenter/désindenter la sélection
- **Auto-paires** : fermeture automatique des parenthèses, crochets, guillemets
- **Drag & drop** : glisser une image → insère `![alt](chemin)` automatiquement

### 3.2 Prévisualisation temps réel

- Rendu HTML via **WebKitGTK 6.0** dans un panneau latéral
- Mise à jour après **150 ms** de pause de frappe (debounce)
- Mise à jour **incrémentale du DOM** via `morphdom` : pas de rechargement de page
- Synchronisation du scroll preview avec la position du curseur
- **Compteur de pages A4** sous la preview : `Page X / Y pages A4 estimées`
- Thème dark/light suivant le thème GTK système

### 3.3 Auto-save

- Sauvegarde automatique toutes les **30 secondes** si des modifications non sauvegardées existent
- Indicateur `●` orange dans la liste des notes pour les modifications en cours
- Dès la sauvegarde (manuelle ou auto), le statut sync passe à `✘` (non synchronisé)

### 3.4 Gestion des notes

- **Répertoire de notes** configurable (défaut : `~/Documents/notes/`)
- **Arborescence par étiquettes** : TreeView GTK4 hiérarchique
- Séparateur visuel `─── sans étiquette ───` entre les groupes et les fichiers libres
- **Renommage inline** : double-clic sur un fichier dans la liste pour renommer
- **Tri** par date de modification (plus récent en premier)
- **Favoris** : marquage `★` et filtre dédié
- **Épinglage** : notes `📌` affichées en tête de liste

### 3.5 Étiquettes

- Création, suppression, modification de couleur
- Assignation multiple (une note peut avoir plusieurs étiquettes)
- Filtrage par étiquette dans la liste des notes
- Recherche d'étiquettes dans le panneau dédié (masqué par défaut)
- Export de la carte étiquettes → `notes_meta.json` lors de la sync SCP

### 3.6 Recherche

- **Recherche locale** (`Ctrl+F`) : surlignage des occurrences dans la note courante, navigation précédent/suivant
- **Recherche globale** (`Ctrl+G`) : cherche dans tous les fichiers `.md`, affiche les résultats avec contexte, clic → ouverture et positionnement du curseur

### 3.7 Correction orthographique (LanguageTool)

- Connexion à un serveur LanguageTool (URL configurable)
- Déclenchement **one-shot** manuel via bouton dans la toolbar
- Surlignage des erreurs dans le buffer (fond rouge)
- **Popup de correction au clic** : message d'erreur, suggestions de remplacement, bouton Ignorer
- Algorithme de mapping `plain → Markdown` : calcule les offsets exacts dans le texte Markdown brut pour appliquer les corrections sans décalage
- Langue configurable (fr, en, de…)

### 3.8 Menu contextuel (clic droit / bouton `⋮`)

Les deux points d'accès partagent le même menu :

| Icône GTK | Action |
|---|---|
| `mail-send-symbolic` | Envoyer par mail |
| `emblem-web-symbolic` | Publier sur Hugo |
| `office-calendar-symbolic` | Changer la date |
| `view-pin-symbolic` | Épingler / Désépingler |
| `starred-symbolic` | Ajouter / Retirer des favoris |
| `changes-prevent-symbolic` | Chiffrer la note (case à cocher) |
| `document-save-as-symbolic` | Exporter en Markdown clair |
| `user-trash-symbolic` | Mettre à la corbeille |

Positionnement adaptatif : si le clic est dans le bas de l'écran, le menu est décalé vers le haut pour rester entièrement visible.

---

## 4. Interface utilisateur

### 4.1 Layout général

```
┌─────────────────────────────────────────────────────────┐
│  HeaderBar : titre note | boutons actions | sync ☁      │
├──────────────┬──────────────────────┬───────────────────┤
│  Liste notes │   Éditeur Markdown   │  Panneau étiquett │
│  (TreeView)  │   (GtkSourceView)    │  (masqué défaut)  │
│              ├──────────────────────┤                   │
│              │   Toolbar formatage  │                   │
│              ├──────────────────────┤                   │
│              │  Preview WebKit      │                   │
│              │  [Page X / Y]        │                   │
├──────────────┴──────────────────────┴───────────────────┤
│  Barre de statut                                        │
└─────────────────────────────────────────────────────────┘
```

### 4.2 HeaderBar

Boutons de gauche à droite :
- ⚙ Paramètres
- 🗑 Corbeille (nouvelle note)
- ★ Favoris
- 💾 Sauvegarder
- 📦 Export (menu : PDF, ODT, LaTeX, HTML, Markdown, ZIP)
- ☐ Plein écran preview
- `fr ▾` Langue LanguageTool
- ↻ Vérification orthographique
- ⛶ Mode focus
- 🏷 Afficher/masquer panneau étiquettes
- ✏ Mode édition

### 4.3 Indicateurs visuels dans la liste des notes

| Indicateur | Signification |
|---|---|
| `●` orange | Modifications non sauvegardées |
| `✔` vert | Note synchronisée avec le serveur distant |
| `✘` rouge | Note modifiée depuis la dernière sync |
| `🔒` jaune | Note chiffrée localement |
| `📌` | Note épinglée |
| `★` | Note favorite |

### 4.4 Panneau étiquettes (masqué par défaut)

- Accessible via le bouton `🏷` en haut à droite
- Champ de recherche pour filtrer les étiquettes
- Chips colorés cliquables pour filtrer les notes
- Bouton `+` pour créer une étiquette
- Clic droit sur une étiquette → renommer, changer couleur, supprimer

---

## 5. Exports

### 5.1 PDF (weasyprint)

- Moteur : **weasyprint** (rendu HTML → PDF)
- `@page` A4, marges 8mm (haut/côtés), 14mm (bas)
- Pied de page : titre du document à gauche, `page / total` à droite
- Tableaux avec `table-layout: fixed`, colonnes adaptatives
- Blocs de code : police monospace 7pt, fond gris
- Images limitées à la largeur de page

```bash
# Lancement dans le terminal pour voir les logs :
[PDF] weasyprint → /chemin/fichier.pdf
[PDF] OK -- 48320 octets
```

### 5.2 ODT (pandoc + patch ZIP)

- Pandoc génère l'ODT de base
- Patch `content.xml` : bordures de cellules `0.05pt solid #999999`, fond d'en-tête `#eeeeee`
- Patch `styles.xml` : Liberation Serif → Liberation Sans
- Tableaux : colonnes avec largeurs calculées

### 5.3 LaTeX (pandoc direct)

- Pandoc génère directement le `.tex` via `--standalone --no-highlight --pdf-engine=xelatex`
- Variables passées : titre, mainfont, monofont, fontsize, geometry
- Police : Liberation Sans (corps), Liberation Mono (code)
- Marges : top 15mm, bottom 20mm, left/right 12mm

### 5.4 HTML

- Rendu via `python-markdown` avec extensions `tables`, `fenced_code`, `toc`, `nl2br`
- CSS intégré, thème clair uniquement
- Page autonome (aucune dépendance externe)
- Mode sombre supprimé (export toujours en clair)

### 5.5 Markdown

- Copie du fichier `.md` en clair (déchiffre automatiquement si la note est chiffrée)
- Dialog de sauvegarde standard GTK4

### 5.6 ZIP

- Archive de toutes les notes du répertoire en un fichier `.zip`

---

## 6. Synchronisation et sauvegarde

### 6.1 Accès

Bouton `☁` dans la HeaderBar → popover avec 3 options :

- **☁ Sauvegarder (backup)** : sync différentielle vers serveur distant
- **⬇ Restaurer depuis le serveur** : liste les fichiers distants, restauration sélective
- **⚙ Configurer Backup et chiffrement...** : paramètres hôte, utilisateur, répertoire, mot de passe

### 6.2 Algorithme de sync différentielle

```
Pour chaque fichier .md :
  sha256_local  = sha256(file.read_bytes())
  sha256_stored = DB.get_synced_sha256(path)
  
  si sha256_local == sha256_stored → skip (inchangé depuis dernière sync)
  sinon :
    si password configuré → chiffrer (AES-256-CBC)
    scp upload vers serveur distant
    DB.set_synced(ok=True, sha256=sha256_local)
```

### 6.3 Notes de synchronisation

- `notes_meta.json` généré avant la sync : contient les étiquettes (label + couleur) par note
- Ce fichier permet de ré-appliquer les étiquettes lors de la restauration
- Détection `sshpass` dynamique, fallback sur clé SSH si absent

### 6.4 Restauration

- Liste les fichiers distants via `ssh find remote -type f | sort`
- Télécharge `notes_meta.json` (ou `.enc` chiffré) pour récupérer les métadonnées
- TreeView avec cases à cocher, colonnes : fichier + étiquettes colorées
- Comparaison SHA-256 local vs distant : skip si identiques
- Ré-application des étiquettes après restauration

---

## 7. Chiffrement

### 7.1 Algorithme

- **AES-256-CBC** via la bibliothèque `cryptography`
- Clé dérivée via `SHA-256` du mot de passe SCP
- IV aléatoire de 16 bytes préfixé au fichier chiffré
- Padding PKCS7

### 7.2 Chiffrement local des notes

- Activé via le menu contextuel → `🔒 Chiffrer cette note`
- Utilise le même mot de passe que la configuration SCP
- Le fichier `.md` sur disque est chiffré en binaire
- L'éditeur déchiffre à la volée à l'ouverture et rechiffre à la sauvegarde
- L'auto-save rechiffre automatiquement
- Indicateur `🔒` jaune dans la liste des notes
- Export Markdown → déchiffre avant export pour obtenir un `.md` lisible

### 7.3 Chiffrement SCP (backup distant)

- Chaque fichier est chiffré **avant** l'envoi si un mot de passe est configuré
- Le fichier distant porte l'extension `.enc`
- La restauration déchiffre automatiquement les `.enc`

---

## 8. Architecture technique

### 8.1 Structure du fichier

```
md_editor.py  (~7 400 lignes, mono-fichier)
│
├── Imports et constantes globales
├── Utilitaires module (fonctions libres)
│   ├── EMOJI_MAP + replace_emojis()
│   ├── compute_col_widths()
│   ├── inject_col_widths()          ← tableaux HTML
│   ├── inject_col_widths_odt()      ← tableaux ODT
│   ├── _build_plain_and_map()       ← mapping LT
│   ├── check_languagetool()
│   ├── _make_preview_css()
│   ├── md_to_html() / md_to_html_print()
│   └── PRINT_CSS
│
├── class NotesDB
├── class ChipWidget
├── class SettingsDialog
├── class _ScpSettingsDialog
├── class MdEditorWindow  (~242 méthodes)
└── class MdEditorApp
```

### 8.2 Classes principales

| Classe | Rôle |
|---|---|
| `NotesDB` | SQLite — étiquettes, favoris, épinglés, sync, chiffrement |
| `ChipWidget` | Widget GTK4 Cairo — pill coloré pour les étiquettes |
| `SettingsDialog` | Dialogue de configuration générale |
| `_ScpSettingsDialog` | Dialogue de configuration SCP/chiffrement |
| `MdEditorWindow` | Fenêtre principale, ~242 méthodes |
| `MdEditorApp` | Point d'entrée `Gtk.Application` |

### 8.3 Cycle de vie d'une frappe

```
Frappe clavier
    → Buffer.changed
    → debounce 150ms (GLib.timeout)
    → _refresh_preview()
        → md_to_html(text)
        → WebView.evaluate_javascript(morphdom diff)
    → _update_page_info()
        → calcul proportion curseur/contenu
        → label "Page X / Y"
    → _update_title()
        → window.set_title()
```

### 8.4 Auto-save

```
GLib.timeout_add(30_000, _auto_save_tick)
    → si current_file IN unsaved_files :
        _write_note(path, text)   ← rechiffre si nécessaire
        DB.set_synced(ok=False)   ← invalide le statut sync
        _update_title()
        _refresh_file_list()
```

### 8.5 Flux de correction LanguageTool

```
Clic bouton LT
    → Thread _lt_worker()
    → _build_plain_and_map(md_text)
        → texte sans balises Markdown
        → md_offsets[i] = position dans le MD original
    → POST /v2/check { text: plain, language: fr }
    → Pour chaque match LT :
        md_start = md_offsets[lt_offset]
        md_end   = md_offsets[lt_offset + lt_length - 1] + 1
    → GLib.idle_add → buffer.apply_tag("lt_error", start, end)
    → Clic sur erreur → popup suggestions
```

### 8.6 Flux SCP chiffré

```
Bouton ☁ → Sauvegarder
    → Générer notes_meta.json
    → Pour chaque .md :
        sha256_local != sha256_stored ?
        → data = path.read_bytes()
        → si password : data = encrypt_local(data, password)
        → scp upload (sshpass ou clé SSH)
        → DB.set_synced(ok=True, sha256=sha256_local)
    → Indicateurs ✔/✘ mis à jour dans la liste
```

---

## 9. Base de données

Fichier : `~/.config/md-editor/notes.db` (SQLite 3)

### Schéma

```sql
-- Étiquettes
CREATE TABLE tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#7c8cf8'
);

-- Association note ↔ étiquette
CREATE TABLE note_tags (
    note_path TEXT NOT NULL,
    tag_id    INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (note_path, tag_id)
);

-- Métadonnées diverses
CREATE TABLE note_meta (
    note_path TEXT PRIMARY KEY,
    pinned    INTEGER DEFAULT 0,
    favorite  INTEGER DEFAULT 0
);

-- Statut de synchronisation SCP
CREATE TABLE sync_status (
    note_path  TEXT PRIMARY KEY,
    synced_at  TEXT,
    remote_ok  INTEGER DEFAULT 0,
    sha256     TEXT
);

-- Notes chiffrées localement
CREATE TABLE encrypted_notes (
    note_path TEXT PRIMARY KEY
);
```

### Méthodes NotesDB (principales)

| Méthode | Description |
|---|---|
| `get_tags()` | Liste toutes les étiquettes |
| `get_tag_ids_for_note(path)` | Étiquettes d'une note |
| `set_note_tags(path, tag_ids)` | Assigner des étiquettes |
| `is_pinned(path)` / `set_pinned()` | Gestion épinglage |
| `is_favorite(path)` / `set_favorite()` | Gestion favoris |
| `get_synced_sha256(path)` | Dernier SHA-256 synchronisé |
| `set_synced(path, ok, sha256)` | Mettre à jour le statut sync |
| `is_note_encrypted(path)` | Note chiffrée ? |
| `set_note_encrypted(path, bool)` | Marquer/démarquer comme chiffrée |
| `rename_note_path(old, new)` | Renommer dans toutes les tables |

---

## 10. Configuration

Fichier : `~/.config/md-editor/config.json`

```json
{
  "notes_dir":      "/home/user/Documents/notes",
  "font_size":      14,
  "font_family":    "Liberation Mono",
  "lt_enabled":     true,
  "lt_language":    "fr",
  "lt_url":         "http://localhost:8010",
  "scp_host":       "mon-serveur.example.com",
  "scp_user":       "user",
  "scp_remote_dir": "/backup/notes",
  "scp_password":   "mot_de_passe",
  "win_width":      1400,
  "win_height":     900,
  "last_file":      "/home/user/Documents/notes/ma_note.md"
}
```

| Clé | Type | Description |
|---|---|---|
| `notes_dir` | string | Répertoire racine des notes |
| `font_size` | int | Taille de police de l'éditeur (pt) |
| `font_family` | string | Police de l'éditeur |
| `lt_enabled` | bool | LanguageTool activé au démarrage |
| `lt_language` | string | Langue pour la correction (fr, en...) |
| `lt_url` | string | URL du serveur LanguageTool |
| `scp_host` | string | Hôte du serveur de backup |
| `scp_user` | string | Utilisateur SSH |
| `scp_remote_dir` | string | Répertoire distant |
| `scp_password` | string | Mot de passe SCP (= clé chiffrement) |
| `win_width` / `win_height` | int | Taille de la fenêtre |
| `last_file` | string | Dernière note ouverte |

---

## 11. Raccourcis clavier

| Raccourci | Action |
|---|---|
| `Ctrl+S` | Sauvegarder |
| `Ctrl+B` | Gras (`**texte**`) |
| `Ctrl+I` | Italique (`*texte*`) |
| `Ctrl+F` | Recherche locale |
| `Ctrl+G` | Recherche globale |
| `Ctrl+Z` | Annuler |
| `Ctrl+Y` | Rétablir |
| `Tab` | Indenter la sélection |
| `Shift+Tab` | Désindenter la sélection |
| `Ctrl++` | Augmenter la taille de police |
| `Ctrl+-` | Diminuer la taille de police |
| `Ctrl+N` | Nouvelle note |

---

## 12. Flux de données principaux

### 12.1 Ouverture d'une note

```
Clic sur un fichier dans la liste
    → _on_tree_activated()
    → _read_note(path)
        → si note chiffrée :
            data = path.read_bytes()
            plain = decrypt_local(data, password)
            return plain.decode('utf-8')
        → sinon : path.read_text('utf-8')
    → buffer.set_text(content)
    → _refresh_preview()
    → _update_title()
    → config['last_file'] = path
```

### 12.2 Sauvegarde d'une note

```
Ctrl+S ou Auto-save (30s)
    → _on_save() ou _auto_save_tick()
    → _write_note(path, text)
        → data = text.encode('utf-8')
        → si note chiffrée :
            enc = encrypt_local(data, password)
            path.write_bytes(enc)
        → sinon : path.write_bytes(data)
    → DB.set_synced(ok=False, sha256=None)   ← invalide sync
    → _unsaved_files.discard(path)
    → _update_title()
    → _refresh_file_list()
```

### 12.3 Export PDF

```
Menu Export → PDF
    → FileDialog.save()
    → _on_export_pdf_done()
    → text = _get_text()
    → html = md_to_html_print(text)   ← PRINT_CSS intégré
    → weasyprint.HTML(string=html, base_url=notes_dir).write_pdf(path)
    → log shell : [PDF] OK -- N octets
```

### 12.4 Export LaTeX

```
Menu Export → LaTeX
    → FileDialog.save()
    → _on_export_latex_done()
    → text = replace_emojis(_get_text())
    → pandoc(tmp.md → output.tex)
        --standalone --no-highlight --pdf-engine=xelatex
        --variable title=... mainfont=... geometry=...
    → log shell : [LaTeX] OK -- N octets
```

### 12.5 Chiffrement local d'une note

```
Menu contextuel → 🔒 Chiffrer cette note
    → _on_toggle_note_encrypt()
    → si déjà chiffrée :
        data = path.read_bytes()
        plain = decrypt_local(data, password)
        path.write_bytes(plain)
        DB.set_note_encrypted(path, False)
    → sinon :
        data = path.read_bytes()
        enc = encrypt_local(data, password)  ← AES-256-CBC
        path.write_bytes(enc)
        DB.set_note_encrypted(path, True)
    → _refresh_file_list()   ← met à jour indicateur 🔒
```

---

## Annexe A — Fonctions utilitaires module

| Fonction | Description |
|---|---|
| `replace_emojis(text)` | Supprime les emojis Unicode > U+2500 pour les exports |
| `compute_col_widths(md_tables)` | Calcule les largeurs de colonnes proportionnelles |
| `inject_col_widths(html, md_tables)` | Injecte les largeurs dans les `<table>` HTML |
| `inject_col_widths_odt(xml, md_tables)` | Injecte les largeurs dans les tableaux ODT |
| `md_to_html(text)` | Rendu HTML pour la preview (avec CSS dark/light) |
| `md_to_html_print(text)` | Rendu HTML pour l'export PDF (CSS blanc, A4) |
| `check_languagetool(url)` | Vérifie la disponibilité du serveur LT |
| `extract_title(text)` | Extrait le titre H1 pour nommer le fichier |

## Annexe B — Améliorations prévues

- **Recherche + remplacement** (Ctrl+H)
- **Historique des versions** (snapshots SQLite)
- **Backlinks** : quelles notes citent la note courante
- **Liens `[[wiki]]`** avec autocomplétion
- **Plan du document** (outline H1/H2/H3 cliquable)
- **Statistiques** : mots, caractères, temps de lecture
- **Mot de passe distinct** chiffrement local / SCP
- **Diagrammes Mermaid** dans la preview
- **Équations KaTeX** dans la preview

---

*md_editor.py — Juillet 2026 — Nitrix
