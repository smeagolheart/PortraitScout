#!/usr/bin/env python3
"""
portrait_scout.py - standalone tool: which creatures / speakers does an Infinity Engine mod have?

For an INSTALLED mod (WeiDU already ran on the game folder) it lists every speaking creature with
    Creature name | Lines | Portrait | Race | Gender | Animation
so you can see who needs a portrait, who already has one (and its file name, to spot duplicates), and what kind
of creature it is. No other files or tools needed - just Python 3.8+ (tkinter comes with it). Nothing is written to
the game folder.

How it works
  * reads chitin.key / the BIFs, the override folder and dialog.tlk of the game folder directly,
  * pulls every string out of the mod's source files (.tra .d .baf .tp2 ...), finds the same text in dialog.tlk,
  * keeps the texts that are NPC lines in a dialog (the player's reply options are not counted),
  * links each dialog to its creature (.CRE) to get name, portrait, race, gender and animation.

Run:   python portrait_scout.py                      (window)
       python portrait_scout.py --cli GAMEDIR MODDIR [--out creatures.txt] [--merge-names]
       python portrait_scout.py --source MODDIR --game GAMEDIR [--out report.txt]
           (reads the mod's own .d / .tra / .tp2 / .cre files; the mod does not have to be installed)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import os
import random
import re
import shutil
from fractions import Fraction

import struct
import subprocess
import sys
import time
import zipfile
import tempfile
import threading
import traceback
import wave
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

VERSION = "2.0"
T_BMP, T_WAV, T_BCS, T_IDS, T_CRE, T_DLG, T_2DA = 0x0001, 0x0004, 0x03EF, 0x03F0, 0x03F1, 0x03F3, 0x03F4
T_ARE, T_WMP = 0x03F2, 0x03F7
EXT = {T_BMP: ".bmp", T_WAV: ".wav", T_BCS: ".bcs", T_IDS: ".ids", T_CRE: ".cre", T_DLG: ".dlg", T_2DA: ".2da",
       T_ARE: ".are", T_WMP: ".wmp"}
SOURCE_EXTS = {".tra", ".d", ".baf", ".tph", ".tpa", ".tpp", ".tp2", ".tp3", ".tpl"}
NO_PORTRAIT = {"", "NONE", "NO_PORT", "NOPORT"}
SEX_NAMES = {1: "Male", 2: "Female", 3: "Other", 4: "Neither"}


def shown_sex(sex: object) -> str:
    """A creature's sex as shown: "Neither" says nothing worth showing."""
    s = str(sex or "")
    return "" if s.lower() == "neither" else s
RACE_FALLBACK = {1: "Human", 2: "Elf", 3: "Half-Elf", 4: "Dwarf", 5: "Halfling", 6: "Gnome", 7: "Half-Orc", 183: "Tiefling"}
Progress = Callable[[str], None]

# ---- .CRE (creature) file layout -------------------------------------------------------------
# Infinity Engine "CRE " V1.0 / "V1  " files are CRE_SIZE bytes. analyze() and read_mod_cre() both
# read the same struct, so the offsets are named here rather than written out twice as numbers.
# The color, portrait and script groups are the same fields in classic BG2 and in the EE
# re-encoding this tool reads.
CRE_SIZE = 0x2D4
CRE_NAME_LONG = 8          # strref (dialog.tlk) of the long name
CRE_NAME_SHORT = 12        # strref of the short name
CRE_ANIM = 0x28            # dword: sprite / animation id
CRE_COLOR_RGB = 0x2C       # 7 color bytes: metal, minor, major, skin, leather, armor, hair
CRE_PORTRAIT_SMALL = 0x34  # resref, small portrait
CRE_PORTRAIT_LARGE = 0x3C  # resref, large portrait
CRE_SCRIPTS = (0x248, 0x250, 0x258, 0x260, 0x268)  # default, override, ai, ambush, store
CRE_DV = 0x280             # "dv" name (0x280..0x29F)
CRE_RACE = 0x272
CRE_CLASS = 0x273
CRE_SEX = 0x275
CRE_DIALOG = 0x2CC         # resref, default dialog


# --------------------------------------------------------------------------------------------- game files
class Game:
    """Read access to a game folder: override first, then chitin.key + BIFs."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.over: Dict[str, Path] = {}
        od = self._ci(self.root, "override")
        if od and od.is_dir():
            for e in os.scandir(od):
                if e.is_file():
                    self.over[e.name.lower()] = Path(e.path)
        self.res: Dict[Tuple[str, int], Tuple[int, int]] = {}     # (NAME, type) -> (bif index, locator)
        self.bifs: List[str] = []
        self._bif_cache: Dict[int, Dict[int, Tuple[int, int, int]]] = {}
        self._bif_path: Dict[int, Optional[Path]] = {}             # resolved once, not on every read
        self._handles: Dict[int, object] = {}                      # open BIFs while an analysis runs
        self._hold = True                                          # close() turns this off: one open per read
        self._lock = threading.Lock()                              # a shared handle is seek + read: one at a time
        self._load_key()

    def _bif_file(self, idx: int) -> Optional[Path]:
        if idx not in self._bif_path:
            self._bif_path[idx] = self._path_ci(self.bifs[idx]) if idx < len(self.bifs) else None
        return self._bif_path[idx]

    def close(self) -> None:
        """Close the BIFs kept open for the analysis; later reads open and close the file each time."""
        with self._lock:
            self._hold = False
            for f in self._handles.values():
                try:
                    f.close()                                      # type: ignore[attr-defined]
                except OSError:
                    pass
            self._handles.clear()

    @staticmethod
    def _ci(base: Path, name: str) -> Optional[Path]:
        p = base / name
        if p.exists():
            return p
        try:
            for e in os.scandir(base):
                if e.name.lower() == name.lower():
                    return Path(e.path)
        except OSError:
            pass
        return None

    def _path_ci(self, rel: str) -> Optional[Path]:
        cur = self.root
        for part in rel.replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            nxt = self._ci(cur, part)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def _load_key(self) -> None:
        kp = self._ci(self.root, "chitin.key")
        if kp is None:
            raise FileNotFoundError("chitin.key not found - is this the game folder?")
        d = kp.read_bytes()
        if d[:8] != b"KEY V1  ":
            raise ValueError("unsupported chitin.key")
        nbif, nres, boff, roff = struct.unpack_from("<IIII", d, 8)
        for i in range(nbif):
            _ln, fo, fl, _loc = struct.unpack_from("<IIHH", d, boff + 12 * i)
            self.bifs.append(d[fo:fo + fl].split(b"\0")[0].decode("latin1"))
        wanted = set(EXT)
        for i in range(nres):
            nm, typ, loc = struct.unpack_from("<8sHI", d, roff + 14 * i)
            if typ in wanted:
                self.res[(nm.split(b"\0")[0].decode("latin1").upper(), typ)] = (loc >> 20, loc & 0x3FFF)

    def _bif_entries(self, idx: int) -> Optional[Dict[int, Tuple[int, int, int]]]:
        if idx in self._bif_cache:
            return self._bif_cache[idx]
        ents: Optional[Dict[int, Tuple[int, int, int]]] = None
        p = self._bif_file(idx)
        if p and p.is_file():
            with open(p, "rb") as f:
                h = f.read(20)
                if h[:8] == b"BIFFV1  ":
                    nf, _nt, off = struct.unpack_from("<III", h, 8)
                    f.seek(off)
                    raw = f.read(16 * nf)
                    ents = {}
                    for k in range(nf):
                        _l, o, s, _t, _u = struct.unpack_from("<IIIHH", raw, 16 * k)
                        ents[k] = (o, s, _t)
        self._bif_cache[idx] = ents or {}
        return self._bif_cache[idx]

    def get(self, name: str, typ: int, limit: int = -1) -> Optional[bytes]:
        fn = (name + EXT[typ]).lower()
        if fn in self.over:
            try:
                with open(self.over[fn], "rb") as f:
                    return f.read(limit if limit > 0 else -1)
            except OSError:
                return None
        loc = self.res.get((name.upper(), typ))
        if loc is None:
            return None
        ents = self._bif_entries(loc[0])
        if not ents or loc[1] not in ents:
            return None
        o, s, _t = ents[loc[1]]
        p = self._bif_file(loc[0])
        if p is None:
            return None
        n = min(s, limit) if limit > 0 else s
        with self._lock:
            if not self._hold:
                with open(p, "rb") as f:
                    f.seek(o)
                    return f.read(n)
            f = self._handles.get(loc[0])
            if f is None:
                f = self._handles[loc[0]] = open(p, "rb")
            f.seek(o)                                               # type: ignore[attr-defined]
            return f.read(n)                                        # type: ignore[attr-defined]

    def has(self, name: str, typ: int) -> bool:
        return (name + EXT[typ]).lower() in self.over or (name.upper(), typ) in self.res

    def names(self, typ: int) -> List[str]:
        ext = EXT[typ]
        out = {fn[:-len(ext)].upper() for fn in self.over if fn.endswith(ext)}
        out |= {n for (n, t) in self.res if t == typ}
        return sorted(out)

    def read_ids(self, name: str) -> Dict[int, str]:
        data = self.get(name, T_IDS)
        out: Dict[int, str] = {}
        if data:
            for line in data.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        out.setdefault(int(parts[0], 0), parts[1])
                    except ValueError:
                        pass
        return out


def find_tlk(game: Path) -> Optional[Path]:
    lang = Game._ci(game, "lang")
    cands: List[Path] = []
    if lang and lang.is_dir():
        for d in sorted(os.scandir(lang), key=lambda e: (e.name.lower() != "en_us", e.name.lower())):
            if d.is_dir():
                t = Game._ci(Path(d.path), "dialog.tlk")
                if t:
                    cands.append(t)
    t = Game._ci(game, "dialog.tlk")
    if t:
        cands.append(t)
    return cands[0] if cands else None


class Tlk:
    def __init__(self, path: Path) -> None:
        self.d = path.read_bytes()
        if self.d[:8] != b"TLK V1  ":
            raise ValueError("unsupported dialog.tlk")
        self.count, self.text_off = struct.unpack_from("<II", self.d, 10)

    def text(self, i: int) -> str:
        if i < 0 or i >= self.count:
            return ""
        _fl, _rs, _v, _p, off, ln = struct.unpack_from("<H8sIIII", self.d, 18 + 26 * i)
        raw = self.d[self.text_off + off:self.text_off + off + ln]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")

    def sound(self, i: int) -> str:
        """The sound resref of entry i ('' if none)."""
        if i < 0 or i >= self.count:
            return ""
        return self.d[20 + 26 * i:28 + 26 * i].split(b"\0", 1)[0].decode("latin1").strip().upper()


_WS = re.compile(r"\s+")
_COLOR = re.compile(r"\^0x[0-9a-fA-F]{8}(.*?)\^-")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.replace(" ", " ")).strip().casefold()


# --------------------------------------------------------------------------------------------- mod strings
_STR_RE = re.compile(r"~~~~~(.*?)~~~~~|~([^~]*)~|\"([^\"\r\n]*)\"", re.DOTALL)
_OTHER_LANG_WORDS = ("french", "german", "italian", "spanish", "polish", "russian", "portuguese", "brazilian", "czech",
                     "korean", "chinese", "japanese", "turkish", "ukrain", "hungarian", "swedish", "dutch", "castilian",
                     "norwegian", "danish", "finnish", "romanian")


_TOKEN = re.compile(r"[A-Za-z0-9_!#$%@&\-]{3,8}")


def mod_strings(mod_dir: str, progress: Progress) -> Tuple[Set[str], Set[str], Set[str]]:
    """(normalized strings, resref-like words used in the source files, names of .cre files the mod ships)."""
    found: Set[str] = set()
    tokens: Set[str] = set()
    shipped: Set[str] = set()
    files = 0
    for root, dirs, names in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.lower() not in {"backup", ".git", "__pycache__"}]
        parts = {x.casefold() for x in re.split(r"[\\/]", root)}
        other = any(w in x for x in parts for w in _OTHER_LANG_WORDS)
        if other and not parts & {"english", "en_us", "american"}:
            continue
        for n in names:
            if n.lower().endswith(".cre"):
                shipped.add(os.path.splitext(n)[0].upper())
            if os.path.splitext(n)[1].lower() not in SOURCE_EXTS:
                continue
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) > 12 * 1024 * 1024:
                    continue
                raw = open(p, "rb").read()
            except OSError:
                continue
            for enc in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            files += 1
            tokens.update(t.upper() for t in _TOKEN.findall(text))
            for m in _STR_RE.finditer(text):
                s = m.group(1) or m.group(2) or m.group(3) or ""
                if 3 <= len(s) <= 4000:
                    found.add(normalize(s))
        progress(f"Reading the mod's files... {files}")
    found.discard("")
    return found, tokens, shipped


def find_mentions(mod_dir: str, names: Set[str], per_name: int = 4) -> Dict[str, List[str]]:
    """Where in the mod's source files each of these resref names is mentioned: {NAME: ['path:line: text', ...]}."""
    out: Dict[str, List[str]] = defaultdict(list)
    if not names:
        return out
    rx = re.compile(r"(?<![A-Za-z0-9_!#$%@&\-])(" + "|".join(re.escape(n) for n in sorted(names)) + r")(?![A-Za-z0-9_!#$%@&\-])", re.I)
    for root, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.lower() not in {"backup", ".git", "__pycache__"}]
        for n in files:
            if os.path.splitext(n)[1].lower() not in SOURCE_EXTS:
                continue
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) > 12 * 1024 * 1024:
                    continue
                lines = open(p, "r", encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(p, mod_dir)
            for ln, line in enumerate(lines, 1):
                for k in {m.group(1).upper() for m in rx.finditer(line)}:
                    if len(out[k]) < per_name:
                        out[k].append(f"{rel}:{ln}: {line.strip()[:110]}")
    return out


# --------------------------------------------------------------------------------------------- analysis
def nice(name: str) -> str:
    return name.replace("_", " ").title() if name else ""


def _r8(b: bytes, off: int) -> str:
    return b[off:off + 8].split(b"\0", 1)[0].decode("latin1").strip()


def _snapshot(r: dict) -> dict:
    """A copy of a row as it is now, with its own lists, so later merging does not change it."""
    out = dict(r)
    for k, v in r.items():
        if isinstance(v, list):
            out[k] = list(v)
        elif isinstance(v, set):
            out[k] = set(v)
        elif isinstance(v, dict) and k != "parts":
            out[k] = dict(v)
    out.pop("parts", None)
    return out


def _split_by_dialog(r: dict, sr: dict) -> List[dict]:
    """One row per dialog, for a bunch of the game's own creatures the mod's files cannot tell apart (commoners and
    the like, found by name only): each dialog is one creature, so Declone can give each a portrait of its own."""
    out: List[dict] = []
    for dn, (_n, how) in sorted(sr["dialogs"].items()):
        ls = (sr.get("by_dlg") or {}).get(dn) or []
        one = _snapshot(r)
        one["lines"], one["_lines"] = len(ls), list(ls)
        one["dialogs"] = [dn]
        one["how"] = [(dn, "dialog" if how else "")]
        one["origin"] = [x for x in r["origin"] if dn in x]
        out.append(one)
    return out


def _join_row(g: dict, r: dict) -> None:
    """Fold one creature's row into the row of the bunch it was merged into (reading the mod's own files)."""
    g["lines"] += r["lines"]
    g["_lines"] += r["_lines"]
    g["_infos"] += r["_infos"]
    g["found"] = g["found"] or r["found"]
    g["cres"] += [c for c in r["cres"] if c not in g["cres"]]
    if not (r.get("_guess_port") and (g["large"] or g["small"])):    # a guessed portrait only fills a gap
        for f in ("large", "small"):
            for v in r[f].split("/"):
                if v and v.upper() not in (x.upper() for x in g[f].split("/")):
                    g[f] = f"{g[f]}/{v}" if g[f] else v
    if not g["race"]:
        g["race"], g["sex"], g["anim"] = r["race"], r["sex"], r["anim"]
    g["dialogs"] += r["dialogs"]
    g["how"] += r["how"]
    g["origin"] += r["origin"]
    g["refs"] |= r["refs"]
    g["via"].update(r["via"])


class NotInstalled(ValueError):
    """The mod's text is not in the game's dialog.tlk: it is not installed there."""


def analyze(game_dir: str, mod_dir: str, merge_names: bool = False, progress: Progress = lambda s: None) -> dict:
    progress("Opening the game folder...")
    game = Game(game_dir)
    tp = find_tlk(Path(game_dir))
    if tp is None:
        raise FileNotFoundError("dialog.tlk not found in the game folder")
    progress("Reading dialog.tlk...")
    tlk = Tlk(tp)

    # no mod folder, or the game folder itself = the whole install: every NPC line of every dialog
    whole = (not mod_dir) or os.path.normcase(os.path.abspath(mod_dir)) == os.path.normcase(os.path.abspath(game_dir))
    texts: Set[str] = set()
    tokens: Set[str] = set()
    shipped: Set[str] = set()
    hits: Dict[int, str] = {}                 # strref -> normalized text (mod lines)
    text_count: Counter = Counter()
    if not whole:
        texts, tokens, shipped = mod_strings(mod_dir, progress)
        if not texts:
            raise ValueError("No source files (.tra .d .baf .tp2 ...) found in the mod folder.")
        progress("Matching the mod's text against dialog.tlk...")
        for i in range(tlk.count):
            t = tlk.text(i)
            if not t:
                continue
            n = normalize(t)
            text_count[n] += 1
            if n in texts:
                hits[i] = n
        if not hits:
            raise NotInstalled("None of this mod's text was found in dialog.tlk - is the mod installed in this game folder?")
    else:
        progress("Whole install: reading every dialog...")

    # strref -> owning dialogs (NPC lines = dialog STATES; the player's reply options are not counted)
    progress("Reading dialogs...")
    owners: Dict[int, Set[str]] = defaultdict(set)
    dlgs = game.names(T_DLG)
    for k, dn in enumerate(dlgs):
        if k % 300 == 0:
            progress(f"Reading dialogs... {k}/{len(dlgs)}")
        d = game.get(dn, T_DLG)
        if not d or d[:8] != b"DLG V1.0":
            continue
        ns, so = struct.unpack_from("<II", d, 8)
        for j in range(ns):
            if so + 16 * j + 4 > len(d):
                break
            sr = struct.unpack_from("<i", d, so + 16 * j)[0]
            if sr >= 0 and (whole or sr in hits):
                owners[sr].add(dn)

    # same stock-line rule as the mod filters: short lines every mod shares only count inside this mod's own dialogs
    long_owner: Set[str] = set() if whole else {o for sr, os_ in owners.items() if len(hits[sr]) > 25 for o in os_}
    per_dlg: Dict[str, Set[int]] = defaultdict(set)
    for sr, os_ in owners.items():
        for o in os_:
            if not whole and len(hits[sr]) <= 25 and text_count[hits[sr]] > 1 and o not in long_owner:
                continue
            per_dlg[o].add(sr)

    # creatures
    progress("Reading creatures...")
    cre_by_dlg: Dict[str, List[dict]] = {}
    cre_by_name: Dict[str, dict] = {}
    cre_by_script: Dict[str, List[dict]] = {}
    cres = game.names(T_CRE)
    for k, cn in enumerate(cres):
        if k % 1000 == 0:
            progress(f"Reading creatures... {k}/{len(cres)}")
        b = game.get(cn, T_CRE, CRE_SIZE)
        if not b or len(b) < CRE_SIZE or b[:4] != b"CRE " or b[4:8] not in (b"V1.0", b"V1  "):
            continue
        info = {"cre": cn, "long": struct.unpack_from("<i", b, CRE_NAME_LONG)[0], "short": struct.unpack_from("<i", b, CRE_NAME_SHORT)[0],
                "anim": struct.unpack_from("<I", b, CRE_ANIM)[0], "small": _r8(b, CRE_PORTRAIT_SMALL), "large": _r8(b, CRE_PORTRAIT_LARGE),
                "race": b[CRE_RACE], "cls": b[CRE_CLASS], "sex": b[CRE_SEX], "dialog": _r8(b, CRE_DIALOG).upper(),
                "colors": list(b[CRE_COLOR_RGB:CRE_COLOR_RGB + 7]),                       # metal, minor, major, skin, leather, armor, hair
                "scripts": {_r8(b, o).upper() for o in CRE_SCRIPTS} - {""}}
        cre_by_name[cn] = info
        if info["dialog"]:
            cre_by_dlg.setdefault(info["dialog"], []).append(info)
        for sn in info["scripts"]:
            cre_by_script.setdefault(sn, []).append(info)
    cre_names_sorted = sorted(cre_by_name)

    # Some conversations are never named by a creature file: a script says StartDialogue("QBTREC") instead. Those
    # dialogs have no owner, so find the script that starts each one and credit the creature that runs the script.
    by_script: Dict[str, List[dict]] = {}
    script_of: Dict[str, str] = {}
    orphans = {d for d in per_dlg if d not in cre_by_dlg and d not in cre_by_name and not d.endswith("J")}
    #                                   a dialog ending in J is a companion's own joined-party conversation, and
    #                                   other creatures' scripts start it with the companion as the speaker
    if orphans:
        progress("Looking for dialogs that scripts start...")
        names = sorted(cre_by_script)
        if all((d + ".dlg").lower() in game.over for d in orphans):  # every one of them is a mod file, and a script
            names = [n for n in names if (n + ".bcs").lower() in game.over]   # still in the archives cannot name it
        for k, sn in enumerate(names):
            if k % 500 == 0:
                progress(f"Looking for dialogs that scripts start... {k}/{len(names)}")
            sb = game.get(sn, T_BCS)
            if not sb:
                continue
            for d in orphans.intersection(_QUOTED.findall(sb.decode("latin-1").upper())):
                for inf in cre_by_script[sn]:
                    if inf not in by_script.setdefault(d, []):
                        by_script[d].append(inf)
                script_of.setdefault(d, sn)

    def by_name(dlg: str) -> Tuple[List[dict], str, str]:
        """(creature files, how): 'dialog' = the creature's own dialog field points here, 'name' = same file name,
        'script' = no creature names it, but a script a creature runs starts it, 'scriptname' = a creature runs a
        script of the same name,
        'related' = the dialog of the same NPC (VALYGARJ -> VALYGAR), 'guess' = matched by name similarity,
        '' = not found. The third value is the related dialog."""
        if dlg in cre_by_dlg:
            return cre_by_dlg[dlg], "dialog", ""
        if dlg in cre_by_name:
            return [cre_by_name[dlg]], "name", ""
        cur = dlg

        def cascade(c: str) -> List[dict]:
            if c in cre_by_dlg:
                return cre_by_dlg[c]
            if c in cre_by_name:
                return [cre_by_name[c]]
            nd = re.sub(r"\d+$", "", c)
            if nd in cre_by_name:
                return [cre_by_name[nd]]
            if nd and nd[-1] in "AE" and nd[:-1] in cre_by_name:
                return [cre_by_name[nd[:-1]]]
            return []
        for step in (lambda c: c, lambda c: c.rstrip("_"), lambda c: c[2:] if c.startswith(("BD", "TB")) else c,
                     lambda c: c[1:] if c.startswith("B") else c, lambda c: c[:-1] if c and c[-1] in "JPBSD" else c,
                     lambda c: re.sub(r"\d.*$", "", c)):
            cur = step(cur)
            r = cascade(cur) if cur else []
            if r:
                return r, ("related" if cur in cre_by_dlg else "guess"), cur
        for cand in (cur, dlg[:-1], dlg, dlg[1:], dlg[2:]):                 # VALYGARJ -> VALYGA, BDYNAH -> DYNAH -> DYNAHE ...
            base = re.sub(r"\d+$", "", cand)
            for ln in range(len(base), 3, -1):                       # the longest creature name base starts with
                if base[:ln] in cre_by_name:
                    return [cre_by_name[base[:ln]]], "guess", ""
            if len(base) >= 4:                                       # else the first creature name starting with base
                k = bisect.bisect_left(cre_names_sorted, base)
                if k < len(cre_names_sorted) and cre_names_sorted[k].startswith(base):
                    return [cre_by_name[cre_names_sorted[k]]], "guess", ""
        return [], "", ""

    def resolve(dlg: str) -> Tuple[List[dict], str, str]:
        """What the creature files say first; the scripts only where a name match would be a guess. A companion's
        own dialog is named by plenty of other creatures' scripts, so the scripts must not outrank a real match -
        but a creature that runs the script is better evidence than a name that merely starts the same way."""
        got = by_name(dlg)
        if got[1] not in ("", "guess"):                              # dialog, name, related: nothing beats those
            return got
        if dlg in by_script:                                         # a script starts it; its owner is the speaker
            return by_script[dlg], "script", script_of.get(dlg, "")
        if dlg in cre_by_script:                                     # a creature runs a script of the same name
            return cre_by_script[dlg], "scriptname", dlg
        return got

    race_ids, anim_ids = game.read_ids("RACE"), game.read_ids("ANIMATE")
    pal = palette_rgb(game)                                          # the game's own color ramps, for what they wear
    rsets = random_color_sets(game)                                 # ... and the sets a high color byte means

    def strip_name(strref: int) -> str:
        return _COLOR.sub(r"\1", tlk.text(strref)).strip() if strref >= 0 else ""

    rows: Dict[str, dict] = {}
    for dn, refs in per_dlg.items():
        cs, how, via = resolve(dn)
        c = cs[0] if cs else None
        name = ""
        for cc in cs:
            name = name or strip_name(cc["long"]) or strip_name(cc["short"])
        name = name or f"Unknown ({dn})"
        key = name.casefold() if merge_names else dn
        r = rows.setdefault(key, {"name": name, "lines": 0, "large": "", "small": "", "race": "", "sex": "", "anim": "",
                                  "dialogs": [], "cres": [], "found": False, "refs": set(), "how": []})
        r["lines"] += len(refs)
        r["refs"] |= refs
        r.setdefault("ref_of", {}).update(dict.fromkeys(refs, dn))   # which dialog each line came out of
        r["dialogs"].append(dn)
        r["how"].append((dn, how))
        r.setdefault("via", {})[dn] = via
        if c:
            r["found"] = True
            for cc in cs:
                if cc["cre"] not in r["cres"]:
                    r["cres"].append(cc["cre"])
                for k in ("large", "small"):
                    v = cc[k]
                    if v.upper() not in NO_PORTRAIT and v.upper() not in (x.upper() for x in r[k].split("/")):
                        r[k] = f"{r[k]}/{v}" if r[k] else v
            if not r["race"]:
                r["race"] = nice(race_ids.get(c["race"], "")) or RACE_FALLBACK.get(c["race"], f"race {c['race']}" if c["race"] else "")
                r["sex"] = SEX_NAMES.get(c["sex"], "")
                an = anim_ids.get(c["anim"]) or anim_ids.get(c["anim"] & 0xFF00) or ""
                r["anim"] = nice(an) or (f"0x{c['anim']:04X}" if c["anim"] else "")
                r["colors"] = creature_colors(c.get("colors") or [], pal, rsets, str(c.get("cre") or ""))
    # one row per NPC: dialogs of the same-named creature are one row when they share a creature file or portrait, or
    # do not differ in race / gender / animation (two different "Guard" creatures stay apart)
    merged: Dict[str, List[dict]] = defaultdict(list)
    for key in list(rows):
        r = rows[key]
        if r["name"].startswith("Unknown ("):
            continue
        ports = {x.upper() for f in ("large", "small") for x in r[f].split("/") if x}
        look = (r["race"], r["sex"], r["anim"])
        for g in merged[r["name"].casefold()]:
            gports = {x.upper() for f in ("large", "small") for x in g[f].split("/") if x}
            glook = (g["race"], g["sex"], g["anim"])
            same_npc = bool(set(r["cres"]) & set(g["cres"]) or ports & gports)
            same_file = bool(set(r["cres"]) & set(g["cres"]))        # the same creature file: certainly one creature
            if same_npc or not any(look) or not any(glook) or look == glook:
                if not same_file:                                    # a shared portrait is what commoners have in
                    # common, not proof of one person, so these can still be split one by one
                    g.setdefault("parts", [_snapshot(g)]).append(_snapshot(r))
                g["lines"] += r["lines"]
                g["refs"] |= r["refs"]
                g.setdefault("ref_of", {}).update(r.get("ref_of") or {})
                g["dialogs"] += r["dialogs"]
                g["how"] += r["how"]
                g.setdefault("via", {}).update(r.get("via", {}))
                g["found"] = g["found"] or r["found"]
                g["cres"] += [c for c in r["cres"] if c not in g["cres"]]
                for f in ("large", "small"):
                    for v in r[f].split("/"):
                        if v and v.upper() not in (x.upper() for x in g[f].split("/")):
                            g[f] = f"{g[f]}/{v}" if g[f] else v
                if not any(glook):
                    g["race"], g["sex"], g["anim"] = look
                del rows[key]
                break
        else:
            merged[r["name"].casefold()].append(r)
    out = sorted(rows.values(), key=lambda r: (-r["lines"], r["name"].lower()))

    def bmp_dims(nm: str) -> str:
        b = game.get(nm, T_BMP, 64) if nm else None
        if b and b[:2] == b"BM" and len(b) >= 26:
            w, h = struct.unpack_from("<ii", b, 18)
            return f"{abs(w)}x{abs(h)}"
        return ""

    copies = clone_sources(mod_dir)                                  # creatures this mod builds by copying
    reuse: Dict[str, int] = {}                                       # a template used for many is a stock type,
    for _n, _t in copies.items():                                    # not a character the copy can be said to be
        reuse[_t] = reuse.get(_t, 0) + 1
    for r in [x for r_ in out for x in ([r_] + (r_.get("parts") or []))]:
        r["large_name"] = r["large"].split("/")[0] if r["large"] else ""
        r["small_name"] = r["small"].split("/")[0] if r["small"] else ""
        r["large_size"] = bmp_dims(r["large_name"])
        r["small_size"] = bmp_dims(r["small_name"])
        r["cre_list"] = sorted(set(r["cres"]))
        dset = {d.upper() for d in r["dialogs"]}
        known = [c for c in r["cre_list"] if c in tokens or c in shipped or c in dset]
        r["cre_known"] = known if len(known) < len(r["cre_list"]) else []      # bold only when it narrows things down
        r["cre"] = ", ".join(f"[{c}]" if c in r["cre_known"] else c for c in r["cre_list"])
        r["texts"] = [(sr, _COLOR.sub(r"\1", tlk.text(sr)).strip()) for sr in sorted(r["refs"])]
        r["sounds"] = {sr: snd for sr in r["refs"] for snd in (tlk.sound(sr),) if snd and game.has(snd, T_WAV)}
        shown = r["large"] or r["small"]
        first = shown.split("/")[0] if shown else ""
        r["portrait"] = shown if shown else ("(none)" if r["found"] else "(creature not found)")
        r["portrait_ok"] = (game.has(first, T_BMP) if first else False)
        if shown and not r["portrait_ok"]:
            r["portrait"] = shown + "  (file missing)"
        for c_ in r["cre_list"]:                                     # built by copying one of the game's own?
            was = copies.get(str(c_).upper())                         # then it wears, or would wear, that face
            port, ref_ = cre_portrait(game, was) if was else ("", -1)
            if port:
                # the same creature under another name keeps its face; a copy of a stock farmer or
                # merchant does not, because that picture is whatever each install happens to put there
                named = strip_name(ref_).casefold() == str(r["name"]).casefold() if ref_ >= 0 else False
                r["copy_of"], r["copy_port"] = was, port
                r["copy_named"] = bool(named) or reuse[was] == 1
                # worn: the copy kept the face. Not worn: the copy was made before the face was given, so
                # it shows nothing - the same borrowed face either way, visible only in the second case
                r["copy_worn"] = bool(first) and first.upper() == port.upper()
                if not shown:
                    r["portrait"] = f"(none) <- {port}"
                elif r["copy_worn"]:
                    r["portrait"] = f"{shown} <- {was}"
                break
    # where do the unknown / guessed speakers come from?
    need = {dn for r in out for dn, how in r["how"] if how in ("", "guess", "related")}
    progress("Looking up where the unknown speakers come from...")
    mentions = find_mentions(mod_dir, need) if (need and not whole) else {}
    for r in out:
        notes: List[str] = []
        for dn, how in r["how"]:
            if how not in ("", "guess", "related", "script", "scriptname"):
                continue
            if how == "scriptname":
                notes.append(f"No creature file points at dialog {dn}, but {', '.join(r['cre_list'])} runs a script "
                             f"of that same name, so the conversation is that creature's. Mods name a creature's "
                             "script and its extra dialog alike, and start the dialog from the script.")
                continue
            if how == "script":
                notes.append(f"No creature file points at dialog {dn}. The script {r['via'].get(dn, '')} starts it, "
                             f"and these creature files run that script: {', '.join(r['cre_list'])}. Scripts start "
                             "conversations this way for quest scenes and cutscenes, so the lines belong to that "
                             "creature even though its dialog field names a different file.")
                continue
            where = "override folder" if (dn + ".dlg").lower() in game.over else "the game's own archives"
            if how == "related":
                notes.append(f"Dialog {dn} is in {where}. No creature points at it directly, but it looks like the extra "
                             f"(for example joined-party) dialog of {r['via'].get(dn, '')}, which these creature files use: "
                             f"{', '.join(r['cre_list'])}. If several are listed they are the same NPC at different stages, "
                             "and the game picks the one that is on the map.")
            else:
                notes.append(f"Dialog {dn} is in {where}." + ("" if how == "" else
                             f" Matched to creature {', '.join(r['cre_list'])} only by name similarity (no creature points at this dialog)."))
            if how == "":
                base = re.sub(r"\d+$", "", dn)
                close = [c for c in cre_names_sorted if len(base) >= 4 and (c.startswith(base[:4]) or c.startswith(base[1:5]))][:6]
                notes.append("No creature file uses this dialog" + (f"; creature files with similar names: {', '.join(close)}." if close else "."))
            for m in mentions.get(dn, []):
                notes.append(f"Mentioned in the mod: {m}")
            if not mentions.get(dn) and not whole:
                notes.append("The mod's own files never mention this dialog name, so the mod probably adds lines to a "
                             "dialog that already exists in the game, or a script attaches it to a creature.")
        if notes:
            refs = r["texts"][:1]
            notes.append("Found because this text of the mod is a line spoken in that dialog"
                         + (f" (strref {refs[0][0]}: \"{refs[0][1][:80]}\")." if refs else "."))
        r["origin"] = notes
    parts = [x for r_ in out for x in (r_.get("parts") or [])]       # the rows a name-only merge swallowed
    for r in parts:
        r.setdefault("origin", [])
    add_areas(out + parts, game, tlk.text, progress)
    progress(f"Done: {len(out)} creature(s), {sum(r['lines'] for r in out):,} line(s).")
    return {"game": game, "rows": out, "whole": whole, "strings": len(texts),
            "mod": "whole-install" if whole else (os.path.basename(mod_dir.rstrip("/\\")) or "mod")}


# --------------------------------------------------------------------------------------------- mod source reader
# Experimental: read a mod's own files (.d .tra .tp2 .cre .baf) instead of the installed game, so it also works
# before the mod is installed. Run:  python portrait_scout.py --source MODDIR [--game GAMEDIR] [--out report.txt]
_D_TOK = re.compile(r"""(?P<ws>\s+)|(?P<lc>//[^\n]*)|(?P<bc>/\*.*?\*/)|(?P<s5>~~~~~.*?~~~~~)|(?P<st>~[^~]*~)|(?P<sq>"[^"]*")
    |(?P<snd>\[[^\]\n]*\])|(?P<at>@-?\d+)|(?P<hash>\#-?\d+)|(?P<eq2>==)|(?P<eq>=)|(?P<pp>\+\+)|(?P<p>\+)|(?P<w>[^\s~"=+]+)""",
                    re.S | re.X)
_D_TOP = {"BEGIN", "APPEND", "APPEND_EARLY", "CHAIN", "CHAIN2", "INTERJECT", "INTERJECT_COPY_TRANS", "INTERJECT_COPY_TRANS2",
          "INTERJECT_COPY_TRANS3", "INTERJECT_COPY_TRANS4", "EXTEND_TOP", "EXTEND_BOTTOM", "ADD_STATE_TRIGGER",
          "ADD_TRANS_TRIGGER", "ADD_TRANS_ACTION", "REPLACE_TRANS_ACTION", "REPLACE_TRANS_TRIGGER", "REPLACE_ACTION_TEXT",
          "REPLACE_ACTION_TEXT_REGEXP", "REPLACE_ACTION_TEXT_PROCESS", "REPLACE_SAY", "REPLACE", "SET_WEIGHT", "ALTER_TRANS",
          "REPLACE_STATE_TRIGGER", "REPLACE_TRIGGER_TEXT", "REPLACE_TRIGGER_TEXT_REGEXP", "R_A_T_P_R"}
_SETDLG = re.compile(r'(?:ActionOverride\(\s*"([^"]+)"\s*,\s*)?(?:SetDialog(?:ue)?|StartDialog(?:ue)?\w*)\(\s*"([^"]+)"', re.I)


def read_text_file(p: str) -> str:
    try:
        raw = open(p, "rb").read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _unq(s: str) -> str:
    if s.startswith("~~~~~"):
        return s[5:-5]
    return s[1:-1] if len(s) >= 2 and s[0] in "~\"" else s


def parse_d(text: str) -> Tuple[List[Tuple[str, str, str, str]], List[Tuple[str, str, str]]]:
    """A WeiDU .d file -> (NPC lines [(DIALOG, kind, value, sound)], SetDialog uses [(in DIALOG, actor, NEW DIALOG)]).
    kind is 'text' (value = the line), '@' (value = number in the .tra file) or '#' (value = strref in dialog.tlk).
    Player replies, journal entries, triggers and actions are not lines."""
    toks: List[Tuple[str, str]] = [(m.lastgroup or "", m.group()) for m in _D_TOK.finditer(text)
                                   if m.lastgroup not in ("ws", "lc", "bc")]
    n = len(toks)
    lines: List[Tuple[str, str, str, str]] = []
    setdlg: List[Tuple[str, str, str]] = []
    STR = ("st", "sq", "s5")

    def name(i: int) -> str:
        return _unq(toks[i][1]).strip().upper() if i < n else ""

    def is_w(i: int, *words: str) -> bool:
        return i < n and toks[i][0] == "w" and toks[i][1].upper() in words

    def take_text(i: int, who: str) -> int:
        """toks[i] is a text; record it for `who`, return the index after it (sound and female variant skipped)."""
        k, v = toks[i]
        snd = ""
        j = i + 1
        if j < n and toks[j][0] == "snd":
            snd = toks[j][1][1:-1].strip().upper()
            j += 1
        if j < n and toks[j][0] in STR + ("at",) and k != "hash":      # female variant of the same line
            j += 1
            if j < n and toks[j][0] == "snd":
                j += 1
        if who:
            lines.append((who, "@" if k == "at" else "#" if k == "hash" else "text",
                          v[1:] if k in ("at", "hash") else _unq(v), snd))
        return j

    def note_strings(i: int, who: str) -> None:
        for m in _SETDLG.finditer(toks[i][1]):
            setdlg.append((who, (m.group(1) or "").upper(), m.group(2).upper()))

    def chain(i: int, who: str) -> int:
        depth = 0
        while i < n:
            k, v = toks[i]
            u = v.upper() if k == "w" else ""
            if k in STR + ("at", "hash"):
                i = take_text(i, who)
            elif k == "eq2":
                i += 1
                if is_w(i, "IF_FILE_EXISTS"):
                    i += 1
                who = name(i)
                i += 1
                if is_w(i, "IF"):
                    i += 2 if i + 1 < n and toks[i + 1][0] in STR else 1
                    if is_w(i, "THEN"):
                        i += 1
            elif u == "DO":
                if i + 1 < n and toks[i + 1][0] in STR:
                    note_strings(i + 1, who)
                i += 2
            elif u == "BRANCH":
                i += 2
                if is_w(i, "BEGIN"):
                    i += 1
                depth += 1
            elif u == "END":
                i += 1
                if depth == 0:
                    return i
                depth -= 1
            elif u in ("EXIT", "EXTERN", "COPY_TRANS", "COPY_TRANS_LATE"):
                return i + 1
            elif u in _D_TOP:
                return i
            else:
                i += 1
        return i

    cur = ""
    i = 0
    while i < n:
        k, v = toks[i]
        if k in STR:
            note_strings(i, cur)
            i += 1
            continue
        if k != "w":
            i += 1
            continue
        u = v.upper()
        if u == "BEGIN":
            prev = toks[i - 1] if i else ("", "")
            before = toks[i - 2] if i > 1 else ("", "")
            state_begin = (prev[0] == "w" and prev[1].upper() == "THEN") or (
                prev[0] in STR and (before[0] == "hash" or (before[0] == "w" and before[1].upper() == "IF")))
            if not state_begin:
                cur = name(i + 1)
                i += 2
                continue
            i += 1
        elif u in ("APPEND", "APPEND_EARLY", "REPLACE"):
            i += 1
            if is_w(i, "IF_FILE_EXISTS"):
                i += 1
            cur = name(i)
            i += 1
        elif u in ("CHAIN", "CHAIN2"):
            i += 1
            if is_w(i, "IF"):
                i += 1
                if is_w(i, "WEIGHT"):
                    i += 2
                if i < n and toks[i][0] in STR:
                    i += 1
                if is_w(i, "THEN"):
                    i += 1
            if is_w(i, "IF_FILE_EXISTS"):
                i += 1
            cur = name(i)
            i = chain(i + 2, cur)
        elif u.startswith("INTERJECT"):
            i = chain(i + 4, "")
        elif u == "REPLACE_SAY":
            who = name(i + 1)
            i += 3
            if i < n and toks[i][0] in STR + ("at", "hash"):
                i = take_text(i, who)
        elif u == "SAY":
            i += 1
            while i < n and toks[i][0] in STR + ("at", "hash"):
                i = take_text(i, cur)
                if i < n and toks[i][0] == "eq":
                    i += 1
                else:
                    break
        else:
            i += 1
    return lines, setdlg


_TRA_RE = re.compile(r"@(-?\d+)\s*=\s*(~~~~~.*?~~~~~|~[^~]*~|\"[^\"]*\")(?:\s*\[([^\]\n]*)\])?", re.S)
_TP2_STR = r"(?:~[^~]*~|\"[^\"]*\")"
_COPY_RE = re.compile(r"\bCOPY(?:_EXISTING|_LARGE)?\b[ \t+\-]*((?:\s*" + _TP2_STR + r"){2,})", re.I)
_QUOTED = re.compile(r'"([A-Z0-9_#!\-]{2,8})"')
_RES_VAR = re.compile(r"%(DEST|SOURCE)_(?:RES|BASE)%", re.I)   # WeiDU: the copied file's own name
_DEFPM_RE = re.compile(r"\bDEFINE_PATCH_MACRO\s+[~\"]([^~\"]+)[~\"]", re.I)
_LPM_RE = re.compile(r"\b(?:LAUNCH_PATCH_MACRO|LPM)\s+[~\"]([^~\"]+)[~\"]", re.I)
_BEGIN_END = re.compile(r"\bBEGIN\b|\bEND\b", re.I)


def patch_macros(texts: object) -> Dict[str, str]:
    """name -> body of every DEFINE_PATCH_MACRO. Mods keep the lines that name a new creature's dialog and
    script in one of these and launch it from each COPY, so without them those creatures look nameless."""
    out: Dict[str, str] = {}
    for t in texts:
        for m in _DEFPM_RE.finditer(t):
            depth, start = 0, -1
            for tok in _BEGIN_END.finditer(t, m.end()):
                word = tok.group(0).upper()
                if word == "BEGIN":
                    if depth == 0:
                        start = tok.end()
                    depth += 1
                else:
                    depth -= 1
                    if depth <= 0:
                        if start >= 0:
                            out.setdefault(m.group(1).strip().lower(), t[start:tok.start()])
                        break
    return out


def expand_macros(body: str, macros: Dict[str, str]) -> str:
    """LAUNCH_PATCH_MACRO ~x~ -> what x does, a few levels deep."""
    for _ in range(3):
        if not _LPM_RE.search(body):
            break
        body = _LPM_RE.sub(lambda m: macros.get(m.group(1).strip().lower(), ""), body)
    return body

_NEXT_ACTION = re.compile(r"\b(?:COPY(?:_EXISTING|_LARGE|_EXISTING_REGEXP|_RANDOM)?|COMPILE|EXTEND_TOP|EXTEND_BOTTOM|"
                          r"APPEND(?:_COL)?|ADD_\w+|LAF|LAM|INCLUDE|STRING_SET|MKDIR|ACTION_\w+|OUTER_\w+)\b|\bBEGIN\s+[~\"@]")
_SAY_RE = re.compile(r"\bSAY\s+(NAME1|NAME2|0x0*8|0x0*c)\s+(@-?\d+|" + _TP2_STR + ")", re.I)
_WA_RE = re.compile(r"\bWRITE_(?:EVALUATED_)?ASCII[ET]?\s+(0x[0-9a-fA-F]+|\d+)\s+(" + _TP2_STR + ")", re.I)
_SPRINT_RE = re.compile(r"\b(?:OUTER_)?(?:TEXT_)?SPRINT\s+~?\"?([A-Za-z0-9_#!\-]+)\"?~?\s+(" + _TP2_STR + r"|[^\s~\"]+)", re.I)
_GAME_IS = re.compile(r"(NOT\s+|!\s*)?GAME_IS\s+[~\"]([^~\"]*)[~\"]", re.I)
_INLINE_D = re.compile(r"<<<<<<<<[ \t]*(\S[^\r\n]*?\.d)[ \t]*\r?\n(.*?)\r?\n>>>>>>>>", re.I | re.S)
_LOAD_TRA = re.compile(r"\b(?:LOAD_TRA|WITH_TRA)((?:\s+" + _TP2_STR + ")+)", re.I)


def read_are_actors(path: str) -> List[Tuple[str, str]]:
    """(CREATURE FILE, DIALOG) of the actors an area file places with a dialog of their own."""
    try:
        b = open(path, "rb").read()
    except OSError:
        return []
    if b[:8] != b"AREAV1.0" or len(b) < 0x5C:
        return []
    off, cnt = struct.unpack_from("<I", b, 0x54)[0], struct.unpack_from("<H", b, 0x58)[0]
    out = []
    for k in range(cnt):
        o = off + 0x110 * k
        if o + 0x110 > len(b):
            break
        dlg, cre = _r8(b, o + 0x48).upper(), _r8(b, o + 0x80).upper()
        if dlg and dlg != "NONE" and cre:
            out.append((cre, dlg))
    return out
_TABLE_RE = re.compile(r"\bAPPEND\s+[~\"][^~\"]*?(pdialog|interdia|bdialog)[^~\"]*\.2da[~\"]\s+(" + _TP2_STR + ")", re.I)
_USING_RE = re.compile(r"\bCOMPILE\b((?:\s+(?:EVAL(?:UATE_BUFFER)?|" + _TP2_STR + r"))+)\s*USING((?:\s+" + _TP2_STR + ")+)", re.I)
_VAR_RE = re.compile(r"%([A-Za-z0-9_#!\-]+)%")


def read_mod_cre(path: str) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            b = f.read(CRE_SIZE)
    except OSError:
        return None
    if len(b) < CRE_SIZE or b[:4] != b"CRE " or b[4:8] not in (b"V1.0", b"V1  "):
        return None
    return {"cre": os.path.splitext(os.path.basename(path))[0].upper(), "name": "", "anim": struct.unpack_from("<I", b, CRE_ANIM)[0],
            "small": _r8(b, CRE_PORTRAIT_SMALL), "large": _r8(b, CRE_PORTRAIT_LARGE), "race": b[CRE_RACE], "sex": b[CRE_SEX],
            "scripts": {_r8(b, o).upper() for o in CRE_SCRIPTS} - {"", "NONE"},
            "dv": b[CRE_DV:CRE_DV + 0x20].split(b"\0", 1)[0].decode("latin1").strip().upper(), "dialog": _r8(b, CRE_DIALOG).upper(),
            "long": struct.unpack_from("<i", b, CRE_NAME_LONG)[0], "file": path,
            "colors": list(b[CRE_COLOR_RGB:CRE_COLOR_RGB + 7])}      # metal, minor, major, skin, leather, armor, hair


def detect_eet(game_dir: str) -> bool:
    """True if WeiDU.log in the game folder lists the EET core component as installed."""
    p = Game._ci(Path(game_dir), "WeiDU.log") if game_dir and os.path.isdir(game_dir) else None
    return bool(p and re.search(r"^~EET[\\/]EET\.TP2~\s+#\d+\s+#0\b", read_text_file(str(p)), re.I | re.M))


def scan_source(mod_dir: str, game_dir: str = "", progress: Progress = lambda s: None) -> dict:
    """Speakers of a mod worked out from the mod's own files. Returns {'rows': [...], 'stats': {...}, 'mod': name}."""
    d_files: List[str] = []
    tra_files: List[str] = []
    tp_files: List[str] = []
    baf_files: List[str] = []
    cres: Dict[str, dict] = {}
    shipped_dlg: Set[str] = set()
    bmps: Set[str] = set()
    placed: List[Tuple[str, str]] = []
    for root, dirs, names in os.walk(mod_dir):                       # tutu / bgt folders: versions for the old game engines
        dirs[:] = [d for d in dirs if d.lower() not in {"backup", ".git", "__pycache__", "tutu", "bgt"}]
        parts = {x.casefold() for x in re.split(r"[\\/]", root)}
        other = any(w in x for x in parts for w in _OTHER_LANG_WORDS) and not parts & {"english", "en_us", "american"}
        for nm in names:
            ext = os.path.splitext(nm)[1].lower()
            p = os.path.join(root, nm)
            if ext == ".tra":
                if not other:
                    tra_files.append(p)
            elif ext == ".d":
                d_files.append(p)
            elif ext in (".tp2", ".tpa", ".tph", ".tpp"):
                tp_files.append(p)
            elif ext == ".baf":
                baf_files.append(p)
            elif ext == ".cre":
                c = read_mod_cre(p)
                if c:
                    cres.setdefault(c["cre"], c)
            elif ext == ".are":
                placed += read_are_actors(p)
            elif ext == ".dlg":
                shipped_dlg.add(os.path.splitext(nm)[0].upper())
            elif ext == ".bmp":
                bmps.add(os.path.splitext(nm)[0].upper())
    stem = lambda p: os.path.splitext(os.path.basename(p))[0].casefold()      # noqa: E731

    progress(f"Reading {len(tra_files)} translation file(s)...")
    tra_by_file: Dict[str, Dict[int, Tuple[str, str]]] = {}
    tra_by_stem: Dict[str, List[str]] = defaultdict(list)
    num_files: Dict[int, List[str]] = defaultdict(list)
    for p in tra_files:
        ent = {int(m.group(1)): (_unq(m.group(2)), (m.group(3) or "").strip().upper()) for m in _TRA_RE.finditer(read_text_file(p))}
        tra_by_file[p] = ent
        tra_by_stem[stem(p)].append(p)
        for k in ent:
            num_files[k].append(p)

    progress(f"Reading {len(tp_files)} installer file(s)...")
    tp_text = {p: read_text_file(p) for p in tp_files}
    variables: Dict[str, str] = {"MOD_FOLDER": os.path.basename(mod_dir.rstrip("/\\"))}
    # cross-platform name tables (cpmvars): on EET use EET's own table, otherwise the mod's BG:EE one; never Tutu/BGT
    eet = detect_eet(game_dir)
    var_texts: List[str] = []
    if eet:
        q: Optional[Path] = Path(game_dir)
        for part in ("EET", "other", "cpmvars", "eet_cpmvars.tpa"):
            q = Game._ci(q, part) if q else None
        if q:
            var_texts.append(read_text_file(str(q)))
    for p, t in tp_text.items():
        b = os.path.basename(p).lower()
        if ("cpmvars" in b or "_vars." in b) and ("eet" if eet else "bgee") not in b:
            continue
        var_texts.append(t)
    plat = "eet" if eet else "bgee"
    var_score: Dict[str, int] = {}
    for t in var_texts:
        for m in _SPRINT_RE.finditer(t):
            score = 1                                                # 2 = inside a GAME_IS block for this platform, 0 = another
            g = None
            for g in _GAME_IS.finditer(t, max(0, m.start() - 500), m.start()):
                pass
            if g and "END" not in t[g.end():m.start()].upper().split():
                listed = plat in g.group(2).lower().split()
                score = 2 if listed != bool(g.group(1)) else 0
            k_ = m.group(1).upper()
            if k_ not in variables or score > var_score.get(k_, 1):
                variables[k_] = _unq(m.group(2))
                var_score[k_] = score
    npc_of: Dict[str, str] = {}                                      # dialog -> NPC key, e.g. BJAHEI -> JAHEIRA
    for k_, v_ in variables.items():
        m_ = re.fullmatch(r"(.+)_(BANTER|JOINED|POST)", k_)
        if m_ and v_:
            npc_of.setdefault(v_.upper(), m_.group(1))

    def subst(s: str) -> str:
        for _ in range(3):
            s2 = _VAR_RE.sub(lambda m: variables.get(m.group(1).upper(), m.group(0)), s)
            if s2 == s:
                break
            s = s2
        return s

    using: Dict[str, List[str]] = defaultdict(list)                  # .d stem -> tra stems named by USING
    setup_tras: List[str] = []
    for p, t in tp_text.items():
        for m in _USING_RE.finditer(t):
            ds = [stem(_unq(x)) for x in re.findall(_TP2_STR, m.group(1)) if _unq(x).lower().endswith(".d")]
            ts = [stem(_unq(x)) for x in re.findall(_TP2_STR, m.group(2))]
            for dn in ds:
                using[dn] += ts
        for m in re.finditer(r"\bLANGUAGE\b((?:\s+" + _TP2_STR + r"){3,})", t):
            for x in re.findall(_TP2_STR, m.group(1))[2:]:
                for q in tra_by_stem.get(stem(_unq(x)), []):
                    if q not in setup_tras:
                        setup_tras.append(q)
    for q in tra_by_stem.get("setup", []):
        if q not in setup_tras:
            setup_tras.append(q)
    local_tras: Dict[str, List[str]] = defaultdict(list)             # folder of an installer file -> .tra it loads
    for p, t in tp_text.items():
        for m in _LOAD_TRA.finditer(t):
            for x in re.findall(_TP2_STR, m.group(1)):
                for q in tra_by_stem.get(stem(_unq(x)), []):
                    local_tras[os.path.dirname(p)].append(q)
    for qs in list(local_tras.values()):
        for q in qs:
            if q not in setup_tras:
                setup_tras.append(q)

    def near(cands: List[str], src: str) -> List[str]:
        """Same-named .tra files: the one sharing most of the source file's path first."""
        sp = re.split(r"[\\/]", os.path.dirname(src).casefold())
        return sorted(cands, key=lambda q: -len(set(sp) & set(re.split(r"[\\/]", os.path.dirname(q).casefold()))))

    stats: Counter = Counter()

    def tra_text(num: int, src: str, setup: bool = False) -> Tuple[str, str, bool]:
        order: List[str] = []
        if not setup:
            order += near(tra_by_stem.get(stem(src), []), src)
            for ts in using.get(stem(src), []):
                order += near(tra_by_stem.get(ts, []), src)
            folder = os.path.dirname(src)
            while len(folder) >= len(mod_dir.rstrip("/\\")):
                order += local_tras.get(folder, [])
                folder = os.path.dirname(folder)
        order += setup_tras
        for q in order:
            if num in tra_by_file[q]:
                return tra_by_file[q][num] + (True,)
        fs = num_files.get(num, [])
        ee = [q for q in fs if re.search(r"(^|_)(bg)?ee(_|$)", stem(q))]     # x.tra and x_ee.tra: this is an EE game
        if len(ee) == 1:
            return tra_by_file[ee[0]][num] + (True,)
        if len(fs) == 1 or (fs and len({tra_by_file[q][num][0] for q in fs}) == 1):
            return tra_by_file[fs[0]][num] + (True,)
        return f"@{num}", "", False

    # creature facts the installer writes: names, dialog, portraits, script name
    pmacros = patch_macros(tp_text.values())
    for p, t in tp_text.items():
        for m in _COPY_RE.finditer(t):
            strs = [subst(_unq(x)) for x in re.findall(_TP2_STR, m.group(1))]
            pairs = [(strs[j], strs[j + 1]) for j in range(0, len(strs) - 1, 2)]
            targets = []
            for src, dst in pairs:
                if src.lower().endswith(".cre"):
                    targets.append((os.path.splitext(os.path.basename(dst if dst.lower().endswith(".cre") else src))[0].upper(),
                                    os.path.splitext(os.path.basename(src))[0].upper()))
            if not targets:
                continue
            nx = _NEXT_ACTION.search(t, m.end())
            body = expand_macros(t[m.end():nx.start() if nx else len(t)], pmacros)
            for dst_name, src_name in targets:
                def res(v: str, _d: str = dst_name, _s: str = src_name) -> str:
                    """WeiDU writes the new file's own name as %DEST_RES%; without this a creature made by
                    COPY_EXISTING keeps the literal variable and never finds its dialog."""
                    return _RES_VAR.sub(lambda m: _d if m.group(1).upper() == "DEST" else _s,
                                        subst(_unq(v))).strip()
                c = cres.get(dst_name)
                if c is None and src_name in cres:
                    c = dict(cres[src_name], cre=dst_name)
                    cres[dst_name] = c
                if c is None:
                    c = {"cre": dst_name, "name": "", "anim": 0, "small": "", "large": "", "race": 0, "sex": 0, "scripts": set(),
                         "dv": "", "dialog": "", "long": -1, "file": ""}      # an existing game creature the mod patches
                    c["game_cre"] = True
                    cres[dst_name] = c
                for s in _SAY_RE.finditer(body):
                    val = s.group(2)
                    txt = tra_text(int(val[1:]), p, True)[0] if val.startswith("@") else res(val)
                    if s.group(1).upper() in ("NAME1", "0X8", "0X08") or not c["name"]:
                        c["name"] = _COLOR.sub(r"\1", txt).strip()
                for w in _WA_RE.finditer(body):
                    off, val = int(w.group(1), 0), res(w.group(2))
                    if off == 0x2CC:
                        c["dialog"] = val.upper()
                    elif off == 0x34:
                        c["small"] = val
                    elif off == 0x3C:
                        c["large"] = val
                    elif off == 0x280:
                        c["dv"] = val.upper()
    for t in tp_text.values():                                       # creatures the installer adds to an area
        for m in re.finditer(r"\bL[AP]F\s+~?fj_are_structure~?(.*?)\bEND\b", t, re.I | re.S):
            f = {k.lower(): subst(_unq(v)).upper() for k, v in
                 re.findall(r"\b(fj_cre_resref|fj_dlg_resref)\s*=\s*(" + _TP2_STR + r"|[^\s~\"]+)", m.group(1), re.I)}
            if f.get("fj_cre_resref") and f.get("fj_dlg_resref"):
                placed.append((f["fj_cre_resref"], f["fj_dlg_resref"]))
    party: Dict[str, Set[str]] = defaultdict(set)                    # script name -> dialogs from pdialog/interdia rows
    for t in tp_text.values():
        for m in _TABLE_RE.finditer(t):
            f = subst(_unq(m.group(2))).split()
            if len(f) >= 2:
                party[f[0].upper()] |= {x.upper() for x in f[1:] if x not in ("***", "*")}

    progress(f"Reading {len(d_files)} dialog source file(s)...")
    dlg_lines: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)       # DIALOG -> [(text, sound, file)]
    set_links: List[Tuple[str, str, str]] = []
    d_texts: List[Tuple[str, str]] = [(p, read_text_file(p)) for p in d_files]
    for p, t in tp_text.items():                                     # dialog code written inside the installer
        d_texts += [(p, m.group(2)) for m in _INLINE_D.finditer(t)]
    seen_line: Set[Tuple[str, str]] = set()
    for p, d_text in d_texts:
        lines, sd = parse_d(d_text)
        stats["d files"] += 1
        for who, kind, val, snd in lines:
            who = subst(who).upper()
            if kind == "@":
                txt, tsnd, ok = tra_text(int(val), p)
                snd = snd or tsnd
                stats["@ refs"] += 1
                stats["@ refs resolved"] += ok
            elif kind == "#":
                txt = f"#{val}"
            else:
                txt = val
            if (who, txt) in seen_line:                              # same line in another version of the file
                stats["repeated lines skipped"] += 1
                continue
            seen_line.add((who, txt))
            dlg_lines[who].append((txt, snd, os.path.relpath(p, mod_dir)))
        set_links += [(subst(a).upper(), b, subst(c).upper()) for a, b, c in sd]
    for p in baf_files:
        st_ = os.path.splitext(os.path.basename(p))[0].upper()
        for m in _SETDLG.finditer(subst(read_text_file(p))):
            set_links.append(("", (m.group(1) or "").upper() or "script:" + st_, m.group(2).upper()))

    # link dialogs to creatures
    by_dlg: Dict[str, List[dict]] = defaultdict(list)
    by_dv: Dict[str, List[dict]] = defaultdict(list)
    by_script: Dict[str, List[dict]] = defaultdict(list)
    for c in cres.values():
        if c["dialog"] and c["dialog"] != "NONE":
            by_dlg[c["dialog"]].append(c)
        if c["dv"]:
            by_dv[c["dv"]].append(c)
        for s in c["scripts"]:
            by_script[s].append(c)
    link: Dict[str, Tuple[List[dict], str]] = {}
    for dn in dlg_lines:
        if dn in by_dlg:
            link[dn] = (by_dlg[dn], "the creature file's dialog field")
    for dv, ds in party.items():
        for dn in ds:
            if dn not in link and dv in by_dv:
                link[dn] = (by_dv[dv], f"party dialog table row of {dv}")
    placed_game: Dict[str, str] = {}                                 # dialog -> creature file that is not in the mod
    for cre_, dn in placed:
        if dn in link:
            continue
        if cre_ in cres:
            link[dn] = ([cres[cre_]], "an area file places this creature with this dialog")
        else:
            placed_game.setdefault(dn, cre_)
    actor_dv: Dict[str, str] = {}                                    # dialog -> script name the mod's scripts give it to
    for _src, actor, new in set_links:
        if actor and not actor.startswith("script:") and actor not in by_dv:
            actor_dv.setdefault(new, actor)
    for _round in range(3):                                          # SetDialog chains
        for src_dlg, actor, new in set_links:
            if new in link:
                continue
            if actor.startswith("script:"):
                cs = by_script.get(actor[7:], [])
                if cs:
                    link[new] = (cs, f"SetDialog in script {actor[7:]}")
            elif actor and actor in by_dv:
                link[new] = (by_dv[actor], f"SetDialog on {actor}")
            elif not actor and src_dlg in link:
                link[new] = (link[src_dlg][0], f"SetDialog inside dialog {src_dlg}")
    names_sorted = sorted(cres)
    for dn in dlg_lines:
        if dn in link:
            continue
        if dn in cres and not cres[dn].get("game_cre"):
            link[dn] = ([cres[dn]], "same name as a creature file (guess)")
            continue
        if dn in by_dv:
            link[dn] = (by_dv[dn], "same name as a creature's script name (guess)")
            continue
        for cand in (dn[1:] if dn[:1] == "B" else "", dn[:-1] if dn[-1:] in "JPBSD" else "", dn[:-3] if dn.endswith(("25J", "25P", "25B")) else "",
                     dn[1:-2] if dn[:1] == "B" and dn.endswith("25") else "", dn[:-2] if dn.endswith("25") else ""):
            if not cand or len(cand) < 3:
                continue
            hit = by_dlg.get(cand) or by_dv.get(cand) or ([cres[cand]] if cand in cres else [])
            if not hit:
                pre = [x for x in names_sorted if x.startswith(cand) and len(cand) >= 4]
                hit = [cres[pre[0]]] if len(pre) == 1 else []
            if hit:
                link[dn] = (hit, f"name pattern {dn} -> {cand} (guess)")
                break

    # optional: creatures of the game folder, for dialogs of NPCs the mod does not ship (BAJANT, IMOEN2J ...)
    game_names: Dict[str, Tuple[str, str]] = {}
    game_how: Dict[str, str] = {}
    in_game: Set[str] = set()
    game_obj: Optional[Game] = None
    if game_dir and os.path.isdir(game_dir):
        try:
            progress("Reading the game's creatures...")
            game = Game(game_dir)
            game_obj = game
            tp = find_tlk(Path(game_dir))
            tlk = Tlk(tp) if tp else None
            in_game = set(game.names(T_DLG))
            need = {dn for dn in dlg_lines if dn not in link}
            table_why: Dict[str, str] = {}
            table_dv: Dict[str, str] = {}                            # dialog -> script name, from the game's party tables
            for tn in ("PDIALOG", "INTERDIA", "BDIALOG"):
                for row in (game.get(tn, T_2DA) or b"").decode("latin1").splitlines()[3:]:
                    f = row.split()
                    for x in f[1:]:
                        table_dv.setdefault(x.upper(), f[0].upper())
            for dn in need:                                          # EET renames BG1 dialogs: BJAHEI -> BJAHEI_
                if dn not in table_dv and dn + "_" in table_dv:
                    table_dv[dn] = table_dv[dn + "_"]
            if any(dn not in table_dv for dn in need):               # rows other installed mods add (their folders are here)
                for e in os.scandir(game_dir):
                    tps = [e.path] if e.is_file() and e.name.lower().endswith(".tp2") else []
                    if e.is_dir() and e.name.lower() not in ("override", "lang", "data", "movies", "music", "scripts"):
                        for root_, dirs_, names_ in os.walk(e.path):  # the installer and its library files, two levels deep
                            if root_[len(e.path):].count(os.sep) >= 2:
                                dirs_[:] = []
                            dirs_[:] = [d for d in dirs_ if d.lower() not in ("backup", ".git", "tutu", "bgt")]
                            tps += [os.path.join(root_, x) for x in names_ if x.lower().endswith((".tp2", ".tpa", ".tph"))]
                    for tp_ in tps:
                        for m in _TABLE_RE.finditer(read_text_file(tp_)):
                            f = _unq(m.group(2)).split()
                            for x in f[1:]:
                                if x.upper() in need and x.upper() not in table_dv:
                                    table_dv[x.upper()] = f[0].upper()
                                    table_why[x.upper()] = f"party dialog table row of {f[0].upper()} in {os.path.basename(tp_)}"
            for dn in need:
                if dn not in table_dv and dn in actor_dv:
                    table_dv[dn] = actor_dv[dn]
                    table_why[dn] = f"the mod's scripts make {actor_dv[dn]} use this dialog"
            for dn in need:                                          # the cross-platform name table: BJAHEI is JAHEIRA's banter
                if dn not in table_dv and dn in npc_of:
                    table_dv[dn] = variables.get(npc_of[dn] + "_DV", npc_of[dn]).upper()
                    table_why[dn] = f"{'EET' if eet else 'BG:EE'} name table: a dialog of {npc_of[dn]}"
            want_dv = {table_dv[dn] for dn in need if dn in table_dv}
            dv_names: Dict[str, Tuple[str, str]] = {}
            all_dlg: Dict[str, Tuple[str, str]] = {}
            all_dv: Dict[str, Tuple[int, str]] = {}
            if tlk:                                                  # installed: the game knows names the mod's files don't say
                for c in cres.values():
                    b = game.get(c["cre"], T_CRE, 0x2D4)
                    if b and len(b) >= 0x2D4:
                        c["name"] = c["name"] or _COLOR.sub(r"\1", tlk.text(struct.unpack_from("<i", b, 8)[0])).strip()
                        # installed: show the portrait the creature has NOW (another mod may have changed it)
                        c["mod_small"], c["mod_large"] = c["small"], c["large"]
                        c["small"], c["large"] = _r8(b, 0x34) or c["small"], _r8(b, 0x3C) or c["large"]
                        c["installed"] = True
                        if c.get("game_cre"):
                            c["anim"], c["race"], c["sex"] = struct.unpack_from("<I", b, 0x28)[0], b[0x272], b[0x275]
            pre3 = {dn[:3] for dn in need}
            votes: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
            if need and tlk:
                for cn in game.names(T_CRE):
                    b = game.get(cn, T_CRE, 0x2D4)
                    if not b or len(b) < 0x2D4 or b[:4] != b"CRE ":
                        continue
                    dn = _r8(b, 0x2CC).upper()
                    dv = b[0x280:0x2A0].split(b"\0", 1)[0].decode("latin1").strip().upper()
                    if dv:
                        all_dv.setdefault(dv, (struct.unpack_from("<i", b, 8)[0], _r8(b, 0x3C) + "|" + _r8(b, 0x34)))
                    if dn in need or dv in want_dv or (dn and dn not in all_dlg and dn[:3] in pre3):
                        nm_ = _COLOR.sub(r"\1", tlk.text(struct.unpack_from("<i", b, 8)[0])).strip()
                        val = (nm_ or cn, _r8(b, 0x3C) + "|" + _r8(b, 0x34))
                        all_dlg.setdefault(dn, val)
                        if dn in need:                               # several creatures can use it (Branwen and her
                            votes[("dlg", dn)][val] += 1             # statue): the name most of them have wins
                        if dv in want_dv:
                            votes[("dv", dv)][val] += 1
                for (kind_, k_), cnt in votes.items():
                    best = max(cnt, key=lambda v: sum(n for w, n in cnt.items() if w[0] == v[0]))
                    (game_names if kind_ == "dlg" else dv_names)[k_] = best
                for dn in need:                                      # a party-table row beats "some creature uses this dialog"
                    if table_dv.get(dn) in dv_names:                 # (Branwen's statue also points at her joined dialog)
                        game_names[dn] = dv_names[table_dv[dn]]
                        game_how[dn] = table_why.get(dn) or f"the game's party dialog table row of {table_dv[dn]}"
                for dn in need:
                    b = game.get(placed_game[dn], T_CRE, 16) if dn in placed_game and dn not in game_names else None
                    if b and len(b) >= 16:
                        game_names[dn] = (_COLOR.sub(r"\1", tlk.text(struct.unpack_from("<i", b, 8)[0])).strip() or placed_game[dn], "")
                        game_how[dn] = f"an area file places creature {placed_game[dn]} with this dialog"
                for dn in need:                                      # BDJAHEIJ -> the creature that uses BDJAHEIR
                    if dn in game_names or len(dn) < 6 or dn[-1] not in "JPB":
                        continue
                    cand = {v[0] for d_, v in all_dlg.items() if d_.startswith(dn[:-1]) or (len(dn) > 6 and d_.startswith(dn[:-2]))}
                    if len(cand) == 1:
                        game_names[dn] = next(v for d_, v in all_dlg.items() if v[0] in cand)
                        game_how[dn] = "game creature with a similar dialog name (guess)"
                left = {dn: f'"{dn}"'.encode("latin1") for dn in need if dn not in game_names and dn in in_game}
                if left:                                             # e.g. cutscene dialogs: a game script hands them out
                    progress("Searching the game's scripts for the dialogs that are still unknown...")
                    for sn in game.names(T_BCS):
                        b = game.get(sn, T_BCS)
                        ub = b.upper() if b else b""
                        for dn in [d_ for d_, pat in left.items() if pat in ub]:
                            for m in re.finditer(rb"RE\n(.*?)RE\n", b, re.S):       # type: ignore[arg-type]
                                if left[dn] not in m.group(1).upper():
                                    continue                         # the action's own actor first, then the cutscene's
                                parts_ = sorted(m.group(1).split(b"AC\n"), key=lambda x: left[dn] not in x.upper())
                                who = [x.decode("latin1").upper() for x in re.findall(rb'"([^"]*)"', b"".join(parts_))]
                                who = [x for x in who if x and x != dn and x in all_dv]
                                if who:
                                    sr_, port_ = all_dv[who[0]]
                                    game_names[dn] = (_COLOR.sub(r"\1", tlk.text(sr_)).strip() or who[0], port_)
                                    game_how[dn] = f"game script {sn} gives this dialog to {who[0]}"
                                    del left[dn]
                                    break
                        if not left:
                            break
        except (OSError, ValueError) as e:
            progress(f"Game folder not used: {e}")

    group: Dict[str, str] = {}                                       # creature file -> group leader (same NPC)

    def find(x: str) -> str:
        while group.setdefault(x, x) != x:
            x = group[x]
        return x
    for cs, _how in link.values():
        for c in cs[1:]:
            group[find(c["cre"])] = find(cs[0]["cre"])
    rows: Dict[str, dict] = {}
    for dn, ls in dlg_lines.items():
        cs, how = link.get(dn, ([], ""))
        if cs:
            c0 = next((c for c in cs if c["name"]), cs[0])
            key, nm_ = "cre:" + find(cs[0]["cre"]), c0["name"] or f"({c0['cre']})"
            port = next((c["large"] or c["small"] for c in cs if (c["large"] or c["small"]).upper() not in NO_PORTRAIT), "")
            src_kind = "mod"
        elif dn in game_names:
            g_large, _sep, g_small = game_names[dn][1].partition("|")
            g_large, g_small = ("" if x.upper() in NO_PORTRAIT else x for x in (g_large, g_small))
            key, nm_, port, cs, src_kind = "game:" + game_names[dn][0].casefold(), game_names[dn][0], g_large or g_small, [], "game"
            how = game_how.get(dn, "creature in the game folder")
        else:
            key, nm_, port, cs = "dlg:" + dn, f"Unknown ({dn})", "", []
            src_kind = "game dialog, no creature" if dn in in_game else "unknown"
        r = rows.setdefault(key, {"name": nm_, "lines": 0, "texts": [], "dialogs": {}, "cres": sorted({c["cre"] for c in cs}),
                                  "portrait": port, "portrait_in_mod": port.upper() in bmps if port else False,
                                  "kind": src_kind, "voiced": 0, "infos": [], "by_dlg": {},
                                  "race": cs[0]["race"] if cs else 0, "sex": SEX_NAMES.get(cs[0]["sex"], "") if cs else ""})
        if src_kind == "game" and not r.get("g_large") and not r.get("g_small"):
            r["g_large"], r["g_small"] = g_large, g_small
        r["cres"] = sorted(set(r["cres"]) | {c["cre"] for c in cs})
        r["infos"] += [c for c in cs if c not in r["infos"]]
        if nm_ and r["name"].startswith("("):
            r["name"] = nm_
        r["lines"] += len(ls)
        r["voiced"] += sum(1 for x in ls if x[1])
        r["texts"] += ls
        r["by_dlg"][dn] = ls
        r["dialogs"][dn] = (len(ls), how)
    out = sorted(rows.values(), key=lambda r: (-r["lines"], r["name"].lower()))
    stats["EET install"] = "yes" if eet else "no"
    stats["dialogs"] = len(dlg_lines)
    stats["dialogs linked by the mod's files"] = sum(1 for dn in dlg_lines if dn in link)
    stats["dialogs linked by the game folder"] = sum(1 for dn in dlg_lines if dn not in link and dn in game_names)
    stats["dialogs not linked"] = stats["dialogs"] - stats["dialogs linked by the mod's files"] - stats["dialogs linked by the game folder"]
    stats["lines"] = sum(len(v) for v in dlg_lines.values())
    stats["creature files in the mod"] = sum(1 for c in cres.values() if not c.get("game_cre"))
    stats["precompiled .dlg files (not read)"] = len(shipped_dlg - set(dlg_lines))
    return {"rows": out, "stats": dict(stats), "mod": os.path.basename(mod_dir.rstrip("/\\")) or "mod",
            "link": {dn: ([c["cre"] for c in cs], how) for dn, (cs, how) in link.items()}, "dlg_lines": dlg_lines,
            "game": game_obj, "eet": eet}


class SourceFiles:
    """Pictures and sounds for the window: the game folder's files (the installed state) first, then the mod folder's own."""

    def __init__(self, mod_dir: str, game: Optional[Game]) -> None:
        self.game = game
        self.files: Dict[Tuple[str, int], str] = {}
        tps: List[str] = []
        for root, dirs, names in os.walk(mod_dir):
            dirs[:] = [d for d in dirs if d.lower() not in {"backup", ".git", "__pycache__"}]
            for nm in names:
                st_, ext = os.path.splitext(nm)
                typ = {".bmp": T_BMP, ".wav": T_WAV, ".ogg": T_WAV}.get(ext.lower())
                if typ is not None:
                    self.files.setdefault((st_.upper(), typ), os.path.join(root, nm))
                elif ext.lower() in (".tp2", ".tpa", ".tph"):
                    tps.append(os.path.join(root, nm))
        for tp_ in tps:                                              # files the installer copies under another name
            for m in _COPY_RE.finditer(read_text_file(tp_)):
                strs = [_unq(x) for x in re.findall(_TP2_STR, m.group(1))]
                for src_, dst_ in zip(strs[0::2], strs[1::2]):
                    (s1, e1), (s2, e2) = (os.path.splitext(os.path.basename(x.replace("\\", "/"))) for x in (src_, dst_))
                    typ = {".bmp": T_BMP, ".wav": T_WAV, ".ogg": T_WAV}.get(e1.lower())
                    if typ is not None and e2.lower() == e1.lower() and (s1.upper(), typ) in self.files:
                        self.files.setdefault((s2.upper(), typ), self.files[(s1.upper(), typ)])

    def get(self, name: str, typ: int, limit: int = -1) -> Optional[bytes]:
        if self.game and self.game.has(name, typ):                    # installed: what the game has now (another mod may
            return self.game.get(name, typ, limit)                    # have replaced the mod's own picture)
        p = self.files.get((name.upper(), typ))
        if p:
            try:
                with open(p, "rb") as f:
                    return f.read(limit if limit > 0 else -1)
            except OSError:
                return None
        return None

    def get_mod(self, name: str, typ: int) -> Optional[bytes]:
        """The mod folder's own copy only."""
        p = self.files.get((name.upper(), typ))
        try:
            return open(p, "rb").read() if p else None
        except OSError:
            return None

    def has(self, name: str, typ: int) -> bool:
        return (name.upper(), typ) in self.files or bool(self.game and self.game.has(name, typ))


def analyze_source(game_dir: str, mod_dir: str, merge_names: bool = False, progress: Progress = lambda s: None) -> dict:
    """Same result shape as analyze(), but worked out from the mod's own files (the mod need not be installed)."""
    if not mod_dir or not os.path.isdir(mod_dir):
        raise ValueError("Reading the mod's own files needs a mod folder.")
    src = scan_source(mod_dir, game_dir, progress)
    if not src["rows"]:
        raise ValueError("No dialog source files (.d) with NPC lines were found in the mod folder.")
    game: Optional[Game] = src["game"]
    files = SourceFiles(mod_dir, game)
    race_ids, anim_ids = (game.read_ids("RACE"), game.read_ids("ANIMATE")) if game else ({}, {})
    try:                                                             # the colors a creature's sprite is painted in,
        pal_ = palette_rgb(game) if game else {}                     # as the install analysis has them: reading the
        rsets_ = random_color_sets(game) if game else {}             # mod's own files left them out, so its portraits
    except Exception:  # noqa: BLE001 - a half-read install: no colors   # got no clothes or hair color
        pal_, rsets_ = {}, {}
    # the same NPC with several creature files is one row. Same name is not enough on its own (two different "Guard"
    # creatures stay apart): the rows must also share a portrait or script name, or not differ in race/gender/animation.
    keys: Dict[int, str] = {}
    groups: Dict[str, List[Tuple[str, Set[Tuple[str, str]], Set[Tuple[int, int, int]]]]] = defaultdict(list)
    for k, sr in enumerate(src["rows"]):
        nm_ = sr["name"].casefold()
        ids = {("dv", c["dv"]) for c in sr["infos"] if c["dv"]}
        ids |= {("port", (c["large"] or c["small"]).upper()) for c in sr["infos"]} | {("port", sr["portrait"].upper())}
        ids = {(a, b) for a, b in ids if b and b not in NO_PORTRAIT}
        looks = {(c["race"], c["sex"], c["anim"]) for c in sr["infos"] if c["anim"]}
        keys[k] = nm_ if merge_names else str(k)
        if merge_names or sr["name"].startswith("Unknown ("):
            continue
        for gkey, gids, glooks in groups[nm_]:
            if ids & gids or not looks or not glooks or looks & glooks:
                keys[k] = gkey
                gids |= ids
                glooks |= looks
                break
        else:
            groups[nm_].append((keys[k], ids, looks))
    rows: Dict[str, dict] = {}
    bits: Dict[str, List[dict]] = {}                                 # the creatures each row stands for, one by one
    for k, sr in enumerate(src["rows"]):
        infos: List[dict] = sr["infos"]
        key = keys[k]
        r = {"name": sr["name"], "lines": 0, "large": "", "small": "", "race": "", "sex": "", "anim": "",
             "dialogs": [], "cres": [], "found": False, "refs": set(), "how": [], "via": {},
             "_lines": [], "origin": [], "_infos": []}
        r["lines"] += sr["lines"]
        r["_lines"] += sr["texts"]
        r["_infos"] += infos
        r["found"] = r["found"] or bool(infos) or sr["kind"] == "game"
        # lines added to a creature the game already has (an EXTEND, or a patched game creature file), as opposed
        # to a creature of the mod's own: Shandalar with more to say is still Shandalar, in his own portrait
        r["patched"] = bool(r.get("patched", True)) and (sr["kind"] == "game" or
                                                        (bool(infos) and all(c.get("game_cre") for c in infos)))
        r["cres"] += [c for c in sr["cres"] if c not in r["cres"]]
        for c in infos:
            for f in ("large", "small"):
                v = c[f]
                if v and v.upper() not in NO_PORTRAIT and v.upper() not in (x.upper() for x in r[f].split("/")):
                    r[f] = f"{r[f]}/{v}" if r[f] else v
        if not infos and not r["large"] and not r["small"]:
            r["large"], r["small"] = sr.get("g_large", ""), sr.get("g_small", "")
            r["_guess_port"] = True
        if infos and not r["race"]:
            c = infos[0]
            r["race"] = nice(race_ids.get(c["race"], "")) or RACE_FALLBACK.get(c["race"], f"race {c['race']}" if c["race"] else "")
            r["sex"] = SEX_NAMES.get(c["sex"], "")
            an = (anim_ids.get(c["anim"]) or anim_ids.get(c["anim"] & 0xFF00) or "") if c["anim"] else ""
            r["anim"] = nice(an) or (f"0x{c['anim']:04X}" if c["anim"] else "")
            if pal_ and c.get("colors"):
                r["colors"] = creature_colors(c.get("colors") or [], pal_, rsets_, str(c.get("cre") or ""))
        for c in infos:
            was = c.get("mod_large") or c.get("mod_small") or ""
            now = c["large"] or c["small"]
            if c.get("installed") and not c.get("game_cre") and was.upper() != now.upper():
                r["origin"].append(f"Creature {c['cre']}: the mod's own file has portrait {was or '(none)'}; the installed game "
                                   f"has {now or '(none)'} (changed by the installer or by another mod).")
        for dn, (n, how) in sorted(sr["dialogs"].items()):
            r["dialogs"].append(dn)
            r["how"].append((dn, "dialog" if how else ""))
            if how:
                r["origin"].append(f"Dialog {dn}, {n} line(s): {how}.")
            else:
                r["origin"].append(f"Dialog {dn}, {n} line(s): no creature found for it. " + (
                    "It is a dialog of the game, but no creature file or script there points at it."
                    if sr["kind"].startswith("game dialog") else
                    "Nothing in the mod's files says who uses it" + (
                        " and it is not in the game folder - probably an NPC of another mod that is not installed."
                        if game else "; give the game folder too, to look for it there.")))
        # the row stands for the whole bunch; each creature in it keeps a row of its own, so Declone can list them
        # one by one. Creatures found by name alone (the game's commoners) are one creature per dialog.
        subs = _split_by_dialog(r, sr) if (not r["cres"] and len(sr["dialogs"]) > 1) else [r]
        g = rows.get(key)
        if g is None:                                                # first creature of this name: it starts the row
            rows[key], bits[key] = _snapshot(r), list(subs)
            continue
        _join_row(g, r)
        for one in subs:
            same = next((p for p in bits[key] if set(p["cres"]) & set(one["cres"])), None) if one["cres"] else None
            if same is None:
                bits[key].append(one)
            else:
                _join_row(same, one)                                 # the same creature file: one creature, not two
    for key, g in rows.items():
        if len(bits[key]) > 1:
            g["parts"] = bits[key]
    out = sorted(rows.values(), key=lambda r: (-r["lines"], r["name"].lower()))
    every = out + [p for r_ in out for p in (r_.get("parts") or [])]

    def bmp_dims(nm: str) -> str:
        b = files.get(nm, T_BMP, 64) if nm else None
        if b and b[:2] == b"BM" and len(b) >= 26:
            w, h = struct.unpack_from("<ii", b, 18)
            return f"{abs(w)}x{abs(h)}"
        return ""

    for r in every:
        r.pop("_guess_port", None)
        ls = r.pop("_lines")
        r["texts"] = [(i + 1, _COLOR.sub(r"\1", t).strip()) for i, (t, _s, _f) in enumerate(ls)]
        r["line_src"] = {i + 1: f for i, (_t, _s, f) in enumerate(ls)}
        r["sounds"] = {i + 1: snd for i, (_t, snd, _f) in enumerate(ls) if snd and files.has(snd, T_WAV)}
        r["large_name"] = r["large"].split("/")[0] if r["large"] else ""
        r["small_name"] = r["small"].split("/")[0] if r["small"] else ""
        r["large_size"], r["small_size"] = bmp_dims(r["large_name"]), bmp_dims(r["small_name"])
        r["cre_list"] = sorted(set(r["cres"]))
        r["cre_known"] = []
        r["cre"] = ", ".join(r["cre_list"])
        shown = r["large"] or r["small"]
        first = shown.split("/")[0] if shown else ""
        r["portrait"] = shown if shown else ("(none)" if r["found"] else "(creature not found)")
        r["portrait_ok"] = files.has(first, T_BMP) if first else False
        if shown and not r["portrait_ok"]:
            r["portrait"] = shown + ("  (file missing)" if game else "  (not in the mod's files)")
        # the pictures the mod itself ships for this NPC (the installed game may show others: another mod replaced them)
        r["mod_ports"], r["mod_sizes"] = {}, {}
        for c in r.pop("_infos"):
            for f in ("small", "large"):
                nm = (c.get("mod_" + f) if c.get("installed") else c[f]) or ""
                mb = files.get_mod(nm, T_BMP) if nm and nm.upper() not in NO_PORTRAIT and f not in r["mod_ports"] else None
                if mb and mb[:2] == b"BM" and len(mb) >= 26:
                    w, h = struct.unpack_from("<ii", mb, 18)
                    r["mod_ports"][f], r["mod_sizes"][f] = nm, f"{abs(w)}x{abs(h)}"
                    now = r[f].split("/")[0] if r[f] else ""
                    if game and game.get(now, T_BMP) != mb:
                        r["origin"].append(f"The mod's own picture {nm} ({abs(w)}x{abs(h)}) is not what the installed game shows"
                                           + (f" ({now}, {r[f + '_size'] or 'size unknown'})" if now else " (no portrait)")
                                           + " - another mod or the installer replaced it.")
    text_ = None
    try:
        tp_ = find_tlk(Path(game_dir)) if game else None
        text_ = Tlk(tp_).text if tp_ else None
    except OSError:
        pass
    add_areas(every, game, text_, progress, mod_dir)
    st_ = src["stats"]
    progress(f"Done: {len(out)} creature(s), {sum(r['lines'] for r in out):,} line(s) from the mod's files.")
    return {"game": files, "rows": out, "whole": False, "strings": st_.get("lines", 0), "mod": src["mod"], "source": True,
            "stats": st_, "eet": src["eet"], "has_game": game is not None}


COLUMNS = [("name", "Creature"), ("lines", "Lines"), ("portrait", "Portrait"), ("race", "Race"), ("sex", "Gender"),
           ("anim", "Animation"), ("cre", "CRE file(s)"), ("area", "Area(s)")]


def as_text(res: dict, rows: Optional[List[dict]] = None) -> str:
    rows = res["rows"] if rows is None else rows
    cell = lambda r, k: shown_sex(r[k]) if k == "sex" else str(r[k])   # noqa: E731
    w = [max(len(h), *(len(cell(r, k)) for r in rows)) if rows else len(h) for k, h in COLUMNS]
    lines = [f"Creatures of {res['mod']}  ({len(rows)} creature(s), {sum(r['lines'] for r in rows):,} line(s))", ""]
    lines.append("  ".join(h.ljust(w[i]) if i != 1 else h.rjust(w[i]) for i, (k, h) in enumerate(COLUMNS)))
    lines.append("  ".join("-" * x for x in w))
    for r in rows:
        lines.append("  ".join(cell(r, k).ljust(w[i]) if i != 1 else cell(r, k).rjust(w[i]) for i, (k, h) in enumerate(COLUMNS)))
    users: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        for p in filter(None, r["large"].split("/")):
            users[p.upper()].append(r["name"])
    dups = {p: sorted(set(n)) for p, n in users.items() if len(set(n)) > 1}
    if dups:
        lines += ["", "Portraits used by several creatures:"] + [f"  {p}: {', '.join(n)}" for p, n in sorted(dups.items())]
    nop = sum(1 for r in rows if not (r["large"] or r["small"]))
    lines += ["", f"{nop} of {len(rows)} creature(s) have no portrait."]
    if res.get("source"):                                            # read from the mod's own files: say what that found
        vo = sum(r.get("voiced", 0) or 0 for r in rows)
        if vo:
            lines.append(f"{vo:,} of {sum(r['lines'] for r in rows):,} line(s) have a sound.")
        st_ = res.get("stats") or {}
        if st_:
            lines += ["", "Read from the mod's own files:"] + [
                f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}" for k, v in st_.items()]
    return "\n".join(lines) + "\n"


def write_csv(res: dict, path: str, rows: Optional[List[dict]] = None) -> None:
    rows = res["rows"] if rows is None else rows
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["creature", "lines", "portrait", "small_portrait", "large_size", "small_size", "race", "gender",
                    "animation", "dialog", "cre", "areas", "notes"])
        for r in rows:
            w.writerow([r["name"], r["lines"], r["large"] or r["small"], r["small"], r.get("large_size", ""),
                        r.get("small_size", ""), r["race"], shown_sex(r["sex"]), r["anim"],
                        ",".join(sorted(set(r["dialogs"]))), ",".join(sorted(set(r["cres"]))), areas_text(r),
                        " | ".join(x for x in r["origin"] if not x.startswith("Appears in: "))])


# --------------------------------------------------------------------------------------------- areas (maps)
def _are_creatures(b: bytes) -> Tuple[Set[str], Set[str], str]:
    """Creature files an area places, those its spawn points can make, and the area's script."""
    if b[:8] != b"AREAV1.0" or len(b) < 0x9C:
        return set(), set(), ""
    placed, spawned = set(), set()
    off, cnt = struct.unpack_from("<I", b, 0x54)[0], struct.unpack_from("<H", b, 0x58)[0]
    for k in range(cnt):
        o = off + 0x110 * k
        if o + 0x110 > len(b):
            break
        placed.add(_r8(b, o + 0x80).upper())
    off, cnt = struct.unpack_from("<II", b, 0x60)
    for k in range(min(cnt, 4096)):
        o = off + 0xC8 * k
        if o + 0xC8 > len(b):
            break
        n = min(10, struct.unpack_from("<H", b, o + 0x74)[0])
        spawned.update(_r8(b, o + 0x24 + 8 * j).upper() for j in range(n))
    return placed - {""}, spawned - {""}, _r8(b, 0x94).upper()


_BCS_ACTION = re.compile(rb"AC\s*(\d+)OB(.*?)AC\r?\n", re.S)
_BCS_STR = re.compile(rb'"([^"\r\n]{1,8})"')


def area_names(game: "Game", text: Optional[Callable[[int], str]]) -> Dict[str, str]:
    """Area code -> name, for the areas that are on a world map."""
    out: Dict[str, str] = {}
    for wn in game.names(T_WMP):
        b = game.get(wn, T_WMP) or b""
        if b[:8] != b"WMAPV1.0" or len(b) < 16:
            continue
        n, off = struct.unpack_from("<II", b, 8)
        for k in range(min(n, 64)):
            o = off + 184 * k
            if o + 184 > len(b):
                break
            an, ao = struct.unpack_from("<II", b, o + 0x20)
            for j in range(min(an, 2048)):
                a = ao + 240 * j
                if a + 240 > len(b):
                    break
                code = _r8(b, a).upper()
                name = ""
                for so in (0x40, 0x44):
                    sr = struct.unpack_from("<i", b, a + so)[0]
                    if text and sr > 0 and not name:
                        name = " ".join(text(sr).split())
                if not name:
                    name = b[a + 0x10:a + 0x30].split(b"\0")[0].decode("latin-1").strip()
                if code and name and name.upper() != code and code not in out:
                    out[code] = name
    return out


def area_index(game: Optional["Game"], wanted: Set[str], progress: Progress = lambda s: None,
               extra_are: Iterable[str] = ()) -> Dict[str, Dict[str, str]]:
    """Where creatures appear: {CRE: {AREA: how}}. how = placed (the area file puts it there), spawn point, or
    script (the area's own script creates it). Creatures made by dialogs, cutscenes or other scripts are not found."""
    found: Dict[str, Dict[str, str]] = defaultdict(dict)
    rank = {"placed": 0, "spawn point": 1, "script": 2}

    def note(cre: str, area: str, how: str) -> None:
        if cre in wanted and rank[how] < rank.get(found[cre].get(area, ""), 9):
            found[cre][area] = how
    create_ids = {b"7", b"227", b"228", b"231", b"232", b"233", b"238", b"250", b"252", b"295", b"323"}
    scripts: Dict[str, Set[str]] = {}
    if game:
        ids = (game.get("ACTION", T_IDS) or b"").decode("latin-1")
        got = {m.group(1).encode() for m in re.finditer(r"^\s*(\d+)\s+CreateCreature", ids, re.M)}
        create_ids = got or create_ids
        names = game.names(T_ARE)
        for i, an in enumerate(names):
            if i % 250 == 0:
                progress(f"Looking up where creatures appear: area {i + 1:,} of {len(names):,}...")
            try:
                placed, spawned, script = _are_creatures(game.get(an, T_ARE) or b"")
            except (OSError, struct.error):
                continue
            for c in placed:
                note(c, an, "placed")
            for c in spawned:
                note(c, an, "spawn point")
            if script and script != "NONE":
                if script not in scripts:
                    made: Set[str] = set()
                    try:
                        data = game.get(script, T_BCS) or b""
                    except OSError:
                        data = b""
                    for m in _BCS_ACTION.finditer(data + b"AC\n") if data else ():
                        if m.group(1) in create_ids:
                            made.update(x.decode("latin-1").upper() for x in _BCS_STR.findall(m.group(2)))
                    scripts[script] = made
                for c in scripts[script]:
                    note(c, an, "script")
    for pth in extra_are:                                            # area files a mod ships (it may not be installed)
        try:
            placed, spawned, _script = _are_creatures(open(pth, "rb").read())
        except (OSError, struct.error):
            continue
        an = Path(pth).stem.upper()
        for c in placed:
            note(c, an, "placed")
        for c in spawned:
            note(c, an, "spawn point")
    return found


def add_areas(rows: List[dict], game: Optional["Game"], text: Optional[Callable[[int], str]],
              progress: Progress = lambda s: None, mod_dir: str = "") -> None:
    """Give every row the areas (maps) its creature files appear in: r["areas"] = [(code, name, how)], r["area"] to show."""
    wanted = {c.upper() for r in rows for c in r.get("cre_list", [])}
    extra: List[str] = []
    if mod_dir:
        for root_, dirs, fns in os.walk(mod_dir):
            dirs[:] = [d for d in dirs if d.lower() not in ("tutu", "bgt", "backup")]
            extra += [os.path.join(root_, f) for f in fns if f.lower().endswith(".are")]
    try:
        idx = area_index(game, wanted, progress, extra)
        names = area_names(game, text) if game else {}
    except Exception:  # noqa: BLE001 - the area list is an extra; never lose the analysis over it
        idx, names = {}, {}
    for r in rows:
        best: Dict[str, str] = {}
        for c in r.get("cre_list", []):
            for a, how in idx.get(c.upper(), {}).items():
                if a not in best or how == "placed":
                    best[a] = how
        r["areas"] = [(a, names.get(a, ""), best[a]) for a in sorted(best)]
        shown = [a + (f" {nm}" if nm else "") for a, nm, _h in r["areas"]]
        r["area"] = ", ".join(shown[:3]) + (f" +{len(shown) - 3} more" if len(shown) > 3 else "")
        if r["areas"]:
            r.setdefault("origin", []).append("Appears in: " + ", ".join(
                f"{a}{' ' + nm if nm else ''} ({how})" for a, nm, how in r["areas"][:40])
                + (f" and {len(r['areas']) - 40} more" if len(r["areas"]) > 40 else ""))


_COLOR_SLOTS = ("metal", "minor", "major", "skin", "leather", "armor", "hair")


def _hsv(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    r, g, bl = rgb
    mx, mn = max(rgb), min(rgb)
    v, c = mx / 255.0, (mx - mn) / 255.0
    if mx == mn:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - bl) / (mx - mn))) % 360
    elif mx == g:
        h = 60 * (2 + (bl - r) / (mx - mn))
    else:
        h = 60 * (4 + (r - g) / (mx - mn))
    return h, c, v


def _rgb_word(rgb: Tuple[int, int, int], slot: str = "cloth") -> str:
    """A plain name for a color, worded for what wears it: hair, skin, metal or cloth."""
    h, c, v = _hsv(rgb)
    if slot == "skin":                                               # the ramp is a skin tone, not a garment
        if c > 0.18 and not 5 <= h < 50:                             # orcs, goblins, drow: skin that is not flesh
            w_ = _rgb_word(rgb)                                      # gold skin is painted as metal
            return {"gold": "sallow yellow", "orange": "ruddy orange"}.get(w_, w_) + " skin"
        if c < 0.07 and v < 0.55:                                    # a gray, bloodless ramp: drow, duergar, undead
            return ("deep charcoal-gray skin the color of wet slate" if v < 0.30 else
                    "charcoal-gray skin the color of wet slate" if v < 0.45 else
                    "ashen gray skin")                               # a bare color word reads as pallor, so the
        #                                                              dark end has to say what it looks like
        return ("deep brown skin, nearly black" if v < 0.30 else "dark brown skin" if v < 0.46 else
                "brown skin" if v < 0.60 else "olive skin" if v < 0.72 and h < 50 else
                "tanned skin" if v < 0.82 else "fair skin")
    if slot == "hair":
        if c < 0.12 or (not 0 <= h < 70 and c < 0.45):               # a cool, washed-out ramp is silver or white
            if 150 <= h < 260 and v > 0.45:                          # hair in a sprite, not a dye job
                return "silver-white hair" if v > 0.52 else "steel gray hair"
            return "black hair" if v < 0.30 else "gray hair" if v < 0.75 else "white hair"
        if v < 0.30:
            return "black hair"
        if h < 20:
            return "auburn hair" if v < 0.65 else "red hair"
        if h < 45:
            return "dark brown hair" if v < 0.55 else "brown hair" if v < 0.75 else "sandy hair"
        if h < 70:
            return "blond hair"
        return "hair dyed " + _rgb_word(rgb)                         # strong green or purple: a mod meant that
    if slot == "metal":
        if c < 0.12:
            return "dark iron" if v < 0.40 else "steel" if v < 0.78 else "silver"
        return ("bronze" if v < 0.55 else "gold") if h < 60 else _rgb_word(rgb) + " metal"
    if c < 0.10:                                                     # cloth and leather
        return "black" if v < 0.18 else "dark gray" if v < 0.40 else "gray" if v < 0.72 else "white"
    if 25 <= h < 55:
        base = "brown" if v < 0.55 else "gold"
    else:
        base = ("red" if h < 12 else "orange" if h < 25 else "yellow" if h < 70 else "green" if h < 160 else
                "teal" if h < 195 else "blue" if h < 255 else "purple" if h < 290 else
                "magenta" if h < 330 else "red")
    if base in ("brown", "gold"):
        return base
    shade = "dark " if v < 0.42 else "pale " if v > 0.80 and c < 0.45 else "deep " if c > 0.70 and v < 0.78 else ""
    return (shade + base).strip()


def palette_rgb(game: "Game") -> Dict[int, Tuple[int, int, int]]:
    """index -> RGB, read from the game's MPALETTE.BMP (12 shades a row, one row per index)."""
    out: Dict[int, Tuple[int, int, int]] = {}
    b = game.get("MPALETTE", T_BMP)
    try:
        if not b or b[:2] != b"BM":
            return out
        off = struct.unpack_from("<I", b, 10)[0]
        _hs, w, h, _pl, bpp, _c = struct.unpack_from("<IiiHHI", b, 14)
        if bpp != 24 or w < 1:
            return out
        stride = ((w * 3) + 3) // 4 * 4
        rows = abs(h)
        for idx in range(min(256, rows)):
            y = rows - 1 - idx if h > 0 else idx                     # bottom-up unless the height is negative
            i = off + y * stride + min(w - 1, 6) * 3                 # a middle shade of the ramp
            if i + 2 < len(b):
                out[idx] = (b[i + 2], b[i + 1], b[i])                # keep the color; the slot decides the words
    except (struct.error, IndexError, ValueError):
        return out
    return out


def random_color_sets(game: "Game") -> Dict[int, Tuple[str, List[int]]]:
    """index -> (name, the palette rows it picks from), out of RANDCOLR.2DA. A color byte of 200 or more is not
    a palette row at all: it names a set the game picks a row from, and the set says what it is for - DrowSkin,
    HobgoblinSkin, PirateMajor. Read as palette rows they come out as nonsense (DrowSkin reads olive yellow)."""
    out: Dict[int, Tuple[str, List[int]]] = {}
    try:
        b = game.get("RANDCOLR", T_2DA)
        if not b:
            return out
        rows = [r.split() for r in b.decode("latin-1", "replace").splitlines() if r.strip()]
        if len(rows) < 4:
            return out
        head, vals = rows[2], rows[3][1:]
        for i, v in enumerate(vals):
            if not v.isdigit():
                continue
            opts = [int(r[i + 1]) for r in rows[4:] if len(r) > i + 1 and r[i + 1].isdigit()]
            if opts:
                out[int(v)] = (head[i] if i < len(head) else "", opts)
    except (ValueError, IndexError, UnicodeDecodeError):
        return {}
    return out


_SET_KIND = {"drow": "drow", "elf": "elf", "kobold": "kobold", "hobgoblin": "hobgoblin", "ogre": "ogre",
             "duegar": "duergar", "duergar": "duergar"}                  # the game spells Duergar without the r


def random_pick(who: str, slot: str, opts: List[int],
                rsets: Optional[Dict[int, Tuple[str, List[int]]]] = None) -> int:
    """Which of a random set's colors stands for this creature: the same one every time, and the same one the
    sprite helper (SpriteShot) paints it in - CRC32 of "NAME:slot" - so the portrait and the sprite agree. A
    roll of 200 or more is itself a set (OgreSkin holds DrowMajor's number), rolled again, as the game does."""
    import zlib
    o = opts[zlib.crc32(f"{who.upper()}:{slot}".encode("latin-1")) % len(opts)]
    for depth_ in range(1, 8):
        nxt_ = (rsets or {}).get(o)
        if not nxt_ or not nxt_[1]:
            break
        o = nxt_[1][zlib.crc32(f"{who.upper()}:{slot}:{depth_}".encode("latin-1")) % len(nxt_[1])]
    return o


def creature_colors(raw: List[int], pal: Dict[int, Tuple[int, int, int]],
                     rsets: Optional[Dict[int, Tuple[str, List[int]]]] = None, who: str = "") -> Dict[str, str]:
    """The seven color bytes of a creature file as words: what it wears, its hair and its skin. The sprite the
    player sees is painted from these, so a portrait that matches them looks like the same character.

    A byte of 200 or more names a set in RANDCOLR.2DA rather than a palette row, so the word is the one most of
    that set's rows come out as - and the set's own name says what kind of creature it is for, which is how a
    drow whose race field only says ELF gives itself away."""
    out: Dict[str, str] = {}
    if not pal or len(raw) < 7 or len(set(raw[:7])) <= 1:
        return out                                                   # all the same (usually all zero): not set up
    for i, slot in enumerate(_COLOR_SLOTS):
        want = slot if slot in ("skin", "hair", "metal") else "cloth"
        named = (rsets or {}).get(raw[i])
        if named:
            name, opts = named
            got_ = [o for o in opts if o in pal]
            if who and got_:                                     # the game rolls one of the set when it makes the
                pk_ = random_pick(who, slot, got_, rsets)       # creature: this one stands in
                if pk_ in pal:
                    out[slot] = _rgb_word(pal[pk_], want)
                out["_random"] = ", ".join(x for x in (out.get("_random", ""), f"{slot} ({name})") if x)
                rough = []
            else:
                rough = [(o, _rgb_word(pal[o])) for o in got_]   # vote on the plain color name, so
            words = Counter(w for _, w in rough)                 # that shades of one color do not split it
            if words:                                            # the game picks one of these at random, so say
                word, n = words.most_common(1)[0]                # nothing unless nearly all of them agree
                if n >= len(rough) * 0.6:
                    same = sorted(o for o, w in rough if w == word)
                    out[slot] = _rgb_word(pal[same[len(same) // 2]], want)   # a middling one of them, in full
                elif slot in ("skin", "hair"):                   # no one color: any of them, as the game does -
                    out[slot + "_any"] = "|".join(sorted({_rgb_word(pal[o], want) for o, _ in rough}))   # the
                    #                                              generator picks one for each creature
            low = name.lower()
            for key, kind in _SET_KIND.items():                      # DrowSkin, HobgoblinSkin, OgreSkin...
                if low.startswith(key) and (slot in ("skin", "hair") or "kind" not in out):
                    if kind == "elf" and out.get("kind") not in (None, "elf"):
                        break                                        # elf hair on drow skin is still a drow
                    out["kind"] = kind
                    break
            continue
        rgb = pal.get(raw[i])
        if rgb is not None:
            out[slot] = _rgb_word(rgb, want)
    return out


def areas_text(r: dict, limit: int = 0) -> str:
    al = r.get("areas", [])
    out = "; ".join(f"{a}{' ' + nm if nm else ''} ({how})" for a, nm, how in (al[:limit] if limit else al))
    return out + (f"; +{len(al) - limit} more" if limit and len(al) > limit else "")


# --------------------------------------------------------------------------------------------- export as a WeiDU mod
MOD_MARK = "// Made by Portrait Scout"
MOD_VERSION = "1.0"
_TP2_FUNC = """ALWAYS
  // Gives one creature file a portrait. replace = 0: only if the creature has no portrait picture now.
  // ship = 0: the picture is not in this mod, so only point the creature at it, and only where it exists.
  DEFINE_ACTION_FUNCTION ps_portrait INT_VAR replace = 1 ship = 1 STR_VAR cre = ~~ bmp = ~~ set = ~~ BEGIN
    ACTION_IF FILE_EXISTS_IN_GAME ~%cre%.cre~ BEGIN
      OUTER_SET ps_go = 1
      ACTION_IF !replace BEGIN
        COPY_EXISTING ~%cre%.cre~ ~override~
          READ_ASCII 0x34 ps_small (8) NULL
          READ_ASCII 0x3c ps_large (8) NULL
          PATCH_IF (FILE_EXISTS_IN_GAME ~%ps_small%.bmp~) OR (FILE_EXISTS_IN_GAME ~%ps_large%.bmp~) BEGIN
            SET ps_go = 0
          END
        BUT_ONLY
      END
      ACTION_IF ps_go BEGIN
        ACTION_IF ship BEGIN
          COPY ~%MOD_FOLDER%/portraits/%set%/%bmp%.bmp~ ~override~
        END
        ACTION_IF ship = 0 BEGIN
          // not ours to install: the picture has to be in the game already, or the creature is left alone
          ACTION_IF !FILE_EXISTS_IN_GAME ~%bmp%.bmp~ BEGIN
            OUTER_SET ps_go = 0
          END
        END
        ACTION_IF ps_go BEGIN
          COPY_EXISTING ~%cre%.cre~ ~override~
            WRITE_ASCIIE 0x34 ~%bmp%~ #8
            WRITE_ASCIIE 0x3c ~%bmp%~ #8
          BUT_ONLY
        END
      END
    END
  END
END
"""


def _is_weidu(path: str) -> bool:
    """A real WeiDU program: a Windows executable of about the right size that says 'weidu' somewhere inside."""
    try:
        if not (700_000 < os.path.getsize(path) < 4_000_000):
            return False
        with open(path, "rb") as f:
            data = f.read(4_000_000)
        return data[:2] == b"MZ" and b"weidu" in data.lower()        # .exe, and WeiDU's own name is in it
    except OSError:
        return False


def _find_weidu(game_dir: str, *extra: str) -> str:
    """A WeiDU program to ship with the mod: one next to Portrait Scout, or the game folder's own setup-*.exe
    (every WeiDU mod installs with one). Checked, so a stray .debug or readme is not copied by mistake."""
    seen: List[str] = []
    for d in [*extra, game_dir]:
        if not d:
            continue
        for name in ("weidu.exe", "weidu"):                          # a copy of your own comes first
            seen.append(os.path.join(d, name))
            seen.append(os.path.join(d, "weidu", name))
        try:
            seen += sorted(e.path for e in os.scandir(d)
                           if e.is_file() and e.name.lower().startswith("setup-")
                           and os.path.splitext(e.name)[1].lower() == ".exe")
        except OSError:
            pass
    return next((p for p in seen if _is_weidu(p)), "")


def write_mod_tp2(folder: str) -> int:
    """(Re)write the installer, the notes and the list from every portraits/<set>/portraits.txt in the mod folder."""
    root = Path(folder)
    sets = []
    for listp in sorted((root / "portraits").glob("*/portraits.txt")):
        title, comp, rows = _read_set(listp)
        if rows and comp >= 0:
            sets.append((comp, listp.parent.name, title, rows))
    sets.sort()
    q = lambda x: x.replace("~", "'")  # noqa: E731
    out = [f"{MOD_MARK} - rewritten on every export. To change what is installed, edit",
           "// portraits/<set>/portraits.txt (creature file, picture, name) and export again.",
           f"BACKUP ~{root.name}/backup~", "AUTHOR ~made with Portrait Scout~", f"VERSION ~{MOD_VERSION}~",
           "README ~%MOD_FOLDER%/readme.txt~", "", _TP2_FUNC]
    for comp, sdir, title, rows in sets:
        npc = len({r[1].upper() for r in rows})
        group = f"Portraits for {q(title)} ({npc} portrait{'s' if npc != 1 else ''})"
        for k, (label, replace) in enumerate((("Give them to every creature listed, replacing portraits they have", 1),
                                              ("Only creatures that have no portrait now", 0))):
            out += [f"BEGIN ~{label}~ DESIGNATED {comp + k} SUBCOMPONENT ~{group}~",
                    f"LABEL ~{_safe_name(root.name)}-{sdir}-{'all' if replace else 'missing'}~"]
            for cre, bmp, name, _areas, ship in rows:
                out.append(f"  LAF ps_portrait INT_VAR replace = {replace} ship = {0 if ship == '0' else 1} "
                           f"STR_VAR cre = ~{cre}~ bmp = ~{bmp}~ set = ~{sdir}~ END"
                           + (f"   // {q(name)}" + (" (inherited)" if ship == "0" else "") if name else ""))
            out.append("")
    (root / f"setup-{root.name}.tp2").write_text("\n".join(out), encoding="utf-8", newline="\n")
    old = root / (root.name + ".tp2")                                # earlier exports used this name
    if old.is_file() and MOD_MARK in old.read_text(encoding="utf-8", errors="replace")[:300]:
        try:
            old.unlink()
        except OSError:
            pass
    _write_notes(root, sets)
    return len(sets)


def _write_notes(root: Path, sets: List[Tuple[int, str, str, List[List[str]]]]) -> None:
    """readme.txt (what it is and how to install it), portraits.txt (the whole list) and a Project Infinity .ini."""
    npc = len({(sd, r[1].upper()) for _c, sd, _t, rows in sets for r in rows})
    cres = sum(len(rows) for _c, _sd, _t, rows in sets)
    made = time.strftime("%d %b %Y")
    txt = [f"{root.name} - portraits made with Portrait Scout", "=" * (len(root.name) + 38), "",
           f"Mod version {MOD_VERSION}, made {made} with Portrait Scout {VERSION}.",
           f"{npc} portrait(s) for {cres} creature file(s), in {len(sets)} set(s).", "",
           "What it does", "------------",
           "Each picture is copied to the game's override folder and written into the creature's",
           "two portrait slots (small and large), the way the HPS Portraits Project does it.",
           "Creature files that are not in your game are skipped, so a set is safe to install",
           "even if you do not have every mod it was made for.", "",
           "Some of these creatures are copies the mod makes of one of the game's own - a second",
           "Bassilus is still Bassilus - and they are listed with the face that creature wears, so",
           "the copy keeps looking like it whatever order the mods were installed in.", ""]
    lent = sorted({r[1].upper() for _c, _sd, _t, rows_ in sets for r in rows_ if len(r) > 4 and r[4] == "0"})
    if lent:
        txt += ["Faces this mod does not carry", "-----------------------------",
                f"{len(lent)} of the portraits named here are already in the game and are not copied in:",
                "   " + ", ".join(lent[:16]) + (" ..." if len(lent) > 16 else ""),
                "They belong to whoever put them there - often another portrait mod - and the creatures",
                "that want them are left alone on a game that does not have them.", ""]
    txt += [
           "How to install", "--------------",
           f"1. Copy the folder '{root.name}' and 'setup-{root.name}.exe' into your game folder",
           "   (the one with chitin.key).",
           f"2. Run setup-{root.name}.exe and choose the components you want.", ""]
    if root.name != root.name.lower():
        txt += [f"On Linux or macOS, rename the folder and the .tp2 inside it to lower case first",
                f"('{root.name.lower()}' and 'setup-{root.name.lower()}.tp2'). WeiDU looks for lower-case",
                "names there and does nothing at all when they do not match. Windows does not mind.", ""]
    txt += [
           "Each set below is offered twice: give the portraits to every creature listed, or",
           "only to creatures that have no portrait yet. Install one of the two, not both.", "",
           "What is in it", "-------------"]
    for comp, sdir, title, rows in sets:
        n = len({r[1].upper() for r in rows})
        txt.append(f"  {title}  -  {n} portrait(s), {len(rows)} creature file(s)   [components {comp} and {comp + 1}]")
    txt += ["", "Changing it", "-----------",
            "portraits/<set>/portraits.txt lists creature file, picture name, creature and the",
            "areas it appears in. Add or remove lines (or pictures), then export again from",
            "Portrait Scout to rewrite the installer - or edit the .tp2 by hand if you prefer.", "",
            "Pictures are .bmp, one per creature file, named after it.", ""]
    (root / "readme.txt").write_text("\n".join(txt), encoding="utf-8", newline="\r\n")
    lst = ["# Every portrait in this mod.", "# set\tcreature file\tpicture\tcreature\tareas it appears in"]
    for _c, sdir, _t, rows in sets:
        lst += ["\t".join([sdir] + r) for r in rows]
    (root / "portraits.txt").write_text("\n".join(lst) + "\n", encoding="utf-8", newline="\r\n")
    ini = ["[Metadata]", f"Name={root.name}", "Author=made with Portrait Scout", "Type=Portraits",
           f"Readme=%MOD_FOLDER%/readme.txt", "Description=Portraits for creatures that have none, "
           "made with Portrait Scout.", ""]
    (root / f"{root.name}.ini").write_text("\n".join(ini), encoding="utf-8", newline="\r\n")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_#!-]+", "_", s).strip("_") or "set"


def _read_set(listp: Path) -> Tuple[str, int, List[List[str]]]:
    title, comp, rows = listp.parent.name, -1, []
    try:
        text = listp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return title, comp, rows
    for ln in text.splitlines():
        if ln.startswith("# set:"):
            title = ln[6:].strip() or title
        elif ln.startswith("# component:"):
            try:
                comp = int(ln[12:].strip())
            except ValueError:
                pass
        elif ln.strip() and not ln.startswith("#"):
            f = ln.split("\t")
            if len(f) >= 2 and f[0].strip() and f[1].strip():
                rows.append(([x.strip() for x in f] + [""] * 5)[:5])   # cre, picture, name, areas, ship
    return title, comp, rows


def preview_html(title: str, items: List[dict]) -> str:
    """A standalone web page of the chosen portraits, to look the set over or show it to someone: one card each,
    the portrait (as JPEG, inside the page), the creature's name and what it is. items: name, kind, jpeg, cre."""
    import base64
    import html as _h
    cards = []
    for it in items:
        src = "data:image/jpeg;base64," + base64.b64encode(it["jpeg"]).decode("ascii")
        nm, kd, cr = (_h.escape(str(it.get(k) or "")) for k in ("name", "kind", "cre"))
        cards.append(f'<div class="c" data-s="{(nm + " " + kd + " " + cr).lower()}"><img src="{src}" alt="{nm}">'
                     f'<div class="n">{nm}</div><div class="k">{kd}</div></div>')
    t = _h.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t} - portraits</title>
<style>
body{{margin:0;background:#1c1b19;color:#e8e2d6;font-family:Georgia,"Palatino Linotype",serif}}
header{{padding:18px 24px 8px;display:flex;flex-wrap:wrap;gap:16px;align-items:baseline}}
h1{{margin:0;font-size:26px;font-weight:normal;color:#e9c46a}}
.m{{color:#9a9384;font-size:14px}}
input{{margin-left:auto;padding:6px 10px;font-size:14px;background:#2a2824;color:#e8e2d6;border:1px solid #4a463e;border-radius:4px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:18px;padding:12px 24px 32px}}
.c{{background:#2a2824;border:1px solid #3d3a33;border-radius:6px;padding:10px;text-align:center}}
.c img{{width:169px;height:266px;display:block;margin:0 auto 8px;border:2px solid #4a463e;cursor:zoom-in}}
.c img.big{{position:fixed;inset:0;margin:auto;width:auto;height:90vh;z-index:9;cursor:zoom-out;border-color:#e9c46a;box-shadow:0 0 0 100vmax rgba(0,0,0,.8)}}
.n{{font-size:16px;color:#f2ead8}}
.k{{font-size:13px;color:#9a9384;margin-top:2px}}
</style></head><body>
<header><h1>{t}</h1><span class="m">{len(items)} portrait(s)</span>
<input id="q" placeholder="Search names, races..." oninput="f()"></header>
<div class="g" id="g">{"".join(cards)}</div>
<script>
function f(){{const q=document.getElementById('q').value.toLowerCase().split(/\\s+/).filter(Boolean);
document.querySelectorAll('.c').forEach(c=>{{c.style.display=q.every(w=>c.dataset.s.includes(w))?'':'none'}})}}
document.getElementById('g').addEventListener('click',e=>{{if(e.target.tagName==='IMG')e.target.classList.toggle('big')}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')document.querySelectorAll('img.big').forEach(i=>i.classList.remove('big'))}});
</script></body></html>
"""


def export_mod(folder: str, set_name: str, entries: List[dict], convert: Optional[Callable] = None,
               game_dir: str = "") -> dict:
    """Write the proposed portraits as a ready-to-install WeiDU mod: the pictures, the installer, a readme and the
    list. Exporting another set into the same folder adds it as another choice in the installer.
    entries: dicts with name, cres, image and areas. Like the HPS portraits mod, each picture is named after its
    creature file and used for both portrait sizes."""
    root = Path(folder)
    if re.search(r"[^A-Za-z0-9_#!-]", root.name):
        raise ValueError("The mod folder's name can only have letters, digits, _ # ! and - (WeiDU and the game need that).")
    made_here = any((root / n).is_file() and MOD_MARK in (root / n).read_text(encoding="utf-8", errors="replace")[:300]
                    for n in (f"setup-{root.name}.tp2", root.name + ".tp2"))
    if root.exists() and any(root.iterdir()) and not made_here:
        raise ValueError("That folder is not empty and was not made by Portrait Scout. Choose an empty or new folder.")
    sdir = root / "portraits" / _safe_name(set_name).lower()         # lower case: WeiDU on Linux / macOS wants it
    listp = sdir / "portraits.txt"
    comp = _read_set(listp)[1] if listp.is_file() else -1
    if comp < 0:
        used = [_read_set(p)[1] for p in (root / "portraits").glob("*/portraits.txt")] if (root / "portraits").is_dir() else []
        comp = max(used + [90]) // 10 * 10 + 10
    sdir.mkdir(parents=True, exist_ok=True)
    rows, skipped, files = [], [], set()
    for e in entries:
        cres = [c.upper() for c in e.get("cres", []) if c and len(c) <= 8 and not re.search(r'[~%"\\/:*?<>|\s.]', c)]
        src = str(e.get("image") or "")
        if not cres:
            skipped.append(f"{e['name']}: no creature file is known for it")
            continue
        bmp = cres[0]
        if bmp in files:
            skipped.append(f"{e['name']}: {bmp}.CRE already has a portrait in this export")
            continue
        if e.get("point_at"):                                        # a face it already wears in the game: the
            for c in cres:                                           # creature is pointed at it, nothing is copied
                rows.append([c, str(e["point_at"]).upper(), e["name"], str(e.get("areas", "")), "0"])
            continue
        dst = sdir / (bmp.lower() + ".bmp")
        try:
            if convert:
                box_ = e.get("box")
                convert(src, str(dst), box_) if box_ else convert(src, str(dst))   # a framing chosen by hand, or the middle
            elif open(src, "rb").read(2) == b"BM":
                shutil.copyfile(src, dst)
            else:
                raise ValueError("not a .bmp picture")
        except Exception as ex:  # noqa: BLE001
            skipped.append(f"{e['name']}: {Path(src).name or 'no picture'} could not be used ({ex})")
            continue
        files.add(bmp)
        for c in cres:
            rows.append([c, bmp, e["name"], str(e.get("areas", "")), "1"])
    for old in sdir.glob("*.bmp"):                                   # pictures of an earlier export, no longer proposed
        if old.stem.upper() not in files:
            try:
                old.unlink()
            except OSError:
                pass
    listp.write_text("\n".join([f"# set: {set_name}", f"# component: {comp}",
                                "# creature file<TAB>picture<TAB>creature<TAB>areas it appears in<TAB>"
                               "1 = the picture is in this mod, 0 = it is already in the game"]
                               + ["\t".join(r) for r in rows]) + "\n", encoding="utf-8")
    nsets = write_mod_tp2(str(root))
    exe = root.parent / f"setup-{root.name}.exe"
    src_exe = _find_weidu(game_dir, str(Path(__file__).resolve().parent))
    if src_exe and not (exe.is_file() and _is_weidu(str(exe))):      # replace a wrong one from an older export
        try:
            shutil.copyfile(src_exe, exe)
        except OSError:
            src_exe = ""
    return {"portraits": len({r[1].upper() for r in rows}), "carried": len(files),
            "creatures": len(rows), "skipped": skipped, "sets": nsets,
            "tp2": str(root / f"setup-{root.name}.tp2"), "component": comp, "exe": str(exe) if exe.is_file() else ""}


# --------------------------------------------------------------------------------------------- BMP -> tk image
def bmp_to_ppm(data: bytes, shrink: int = 1) -> Optional[bytes]:
    """Uncompressed 8/24/32-bit BMP -> binary PPM (tkinter can show PPM without Pillow)."""
    try:
        if data[:2] != b"BM":
            return None
        off = struct.unpack_from("<I", data, 10)[0]
        hs, w, h, _pl, bpp, comp = struct.unpack_from("<IiiHHI", data, 14)
        if comp not in (0, 3) or bpp not in (8, 24, 32):
            return None
        top_down = h < 0
        h = abs(h)
        pal: List[bytes] = []
        if bpp == 8:
            n = struct.unpack_from("<I", data, 46)[0] or 256
            base = 14 + hs
            pal = [bytes((data[base + 4 * i + 2], data[base + 4 * i + 1], data[base + 4 * i])) for i in range(n)]
        row = ((w * bpp + 31) // 32) * 4
        out_w, out_h = w // shrink, h // shrink
        buf = bytearray()
        for oy in range(out_h):
            y = oy * shrink
            src = y if top_down else (h - 1 - y)
            line = data[off + src * row: off + src * row + row]
            for ox in range(out_w):
                x = ox * shrink
                if bpp == 8:
                    buf += pal[line[x]] if line[x] < len(pal) else b"\0\0\0"
                else:
                    p = x * (bpp // 8)
                    buf += bytes((line[p + 2], line[p + 1], line[p]))
        return b"P6 %d %d 255\n" % (out_w, out_h) + bytes(buf)
    except (struct.error, IndexError):
        return None


# --------------------------------------------------------------------------------------------- portrait image checks
def image_info(path: str) -> Optional[dict]:
    """{'fmt', 'w', 'h', 'bpp'} read from the file header (BMP, PNG, GIF, JPEG), None if unknown."""
    try:
        with open(path, "rb") as f:
            d = f.read(262144)
    except OSError:
        return None
    try:
        if d[:2] == b"BM":
            w, h = struct.unpack_from("<ii", d, 18)
            return {"fmt": "BMP", "w": abs(w), "h": abs(h), "bpp": struct.unpack_from("<H", d, 28)[0]}
        if d[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack_from(">II", d, 16)
            return {"fmt": "PNG", "w": w, "h": h, "bpp": None}
        if d[:3] == b"GIF":
            w, h = struct.unpack_from("<HH", d, 6)
            return {"fmt": "GIF", "w": w, "h": h, "bpp": None}
        if d[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(d):
                if d[i] != 0xFF:
                    i += 1
                    continue
                m = d[i + 1]
                if m == 0xFF or m == 0x01 or 0xD0 <= m <= 0xD8:
                    i += 2 if m != 0xFF else 1
                    continue
                ln = struct.unpack_from(">H", d, i + 2)[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack_from(">HH", d, i + 5)
                    return {"fmt": "JPEG", "w": w, "h": h, "bpp": None}
                i += 2 + ln
    except struct.error:
        pass
    return None




def _write_bmp24(path: str, w: int, h: int, rgb: bytes) -> None:
    """A plain 24-bit BMP, the format the game reads."""
    pad = (4 - w * 3 % 4) % 4
    rows = bytearray()
    for y in range(h - 1, -1, -1):                                   # BMP rows run bottom-up, pixels B G R
        line = rgb[y * w * 3:(y + 1) * w * 3]
        rows += bytes(b for i in range(0, len(line), 3) for b in (line[i + 2], line[i + 1], line[i])) + b"\0" * pad
    head = b"BM" + struct.pack("<IHHI", 54 + len(rows), 0, 0, 54) + \
        struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(rows), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(head + bytes(rows))


def _resample(src: bytes, sw: int, sh: int, box: Tuple[int, int, int, int], w: int, h: int) -> bytes:
    """Cut `box` out of an RGB buffer and shrink it to w x h, averaging the pixels that fall in each one."""
    left, top, cw, ch = box
    xs = [left + cw * i // w for i in range(w + 1)]
    ys = [top + ch * j // h for j in range(h + 1)]
    out = bytearray()
    for j in range(h):
        y0, y1 = ys[j], max(ys[j] + 1, ys[j + 1])
        for i in range(w):
            x0, x1 = xs[i], max(xs[i] + 1, xs[i + 1])
            r = g = b = n = 0
            for y in range(y0, min(y1, sh)):
                base = (y * sw + x0) * 3
                for x in range(x0, min(x1, sw)):
                    r += src[base]
                    g += src[base + 1]
                    b += src[base + 2]
                    base += 3
                    n += 1
            out += bytes((r // n, g // n, b // n)) if n else b"\0\0\0"
    return bytes(out)


def to_portrait_bmp(src: str, dst: str, width: int, height: int, crop: Optional[Callable] = None,
                    box_px: Optional[Tuple[int, int, int, int]] = None) -> str:
    """Any picture -> a game portrait: cut to the portrait's shape from the middle, shrunk, saved as a 24-bit BMP.
    Uses Pillow when it is installed (sharper, and reads every format); otherwise BMP and PNG are handled here."""
    def box(sw: int, sh: int) -> Tuple[int, int, int, int]:
        if box_px:                                                   # framed by hand
            lf, tp, cw, ch = box_px
            return max(0, min(lf, sw - 1)), max(0, min(tp, sh - 1)), max(1, min(cw, sw - lf)), max(1, min(ch, sh - tp))
        if crop:
            return crop(sw, sh, width, height, 1.0, 0.0)             # the generator's own crop, if it is there
        cw = min(sw, round(sh * width / height))
        ch = min(sh, round(cw * height / width))
        return (sw - cw) // 2, (sh - ch) // 2, cw, ch
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        left, top, cw, ch = box(im.width, im.height)
        im.crop((left, top, left + cw, top + ch)).resize((width, height), Image.LANCZOS).save(dst, "BMP")
        return dst
    except ImportError:
        pass
    raw = Path(src).read_bytes()
    if raw[:2] == b"BM":
        ppm = bmp_to_ppm(raw, 1)
        if not ppm:
            raise ValueError("this BMP is compressed or in a format Portrait Scout cannot read")
        parts = ppm.split(b"\n", 1)[0].split()
        sw, sh = int(parts[1]), int(parts[2])
        pix = ppm[len(ppm.split(b"\n", 1)[0]) + 1:]
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        from importlib import import_module
        gen = Path(__file__).resolve().parent / "generator"
        sys.path.insert(0, str(gen))
        try:
            sw, sh, pix = import_module("portrait_generator")._png_rgb(raw)
        finally:
            sys.path.remove(str(gen))
    else:
        raise ValueError("only BMP and PNG can be converted without Pillow (pip install pillow for the rest)")
    _write_bmp24(dst, width, height, _resample(pix, sw, sh, box(sw, sh), width, height))
    return dst


def check_image(path: str) -> List[str]:
    """Things that may stop a proposed portrait from working in the game (empty list = looks fine).
    The file's own name is not one of them: 'Create mod...' names every picture after its creature file."""
    issues: List[str] = []
    info = image_info(path)
    if info is None:
        issues.append("could not read the image size (unknown format)")
        return issues
    if info["fmt"] != "BMP":
        issues.append(f"it is a {info['fmt']}; the game's portrait files are BMP, so it must be converted to a 24-bit BMP")
    elif info["bpp"] not in (8, 24):
        issues.append(f"the BMP is {info['bpp']}-bit; use a 24-bit BMP")
    w, h = info["w"], info["h"]
    if h and abs(w / h - 210 / 330) > 0.03:        # 210x330, 169x266 and 54x84 all have this shape
        issues.append(f"the shape is {w}x{h}; portraits are 210x330 (large), 169x266 (medium) or 54x84 (small), "
                      "so it will look squeezed or stretched")
    return issues


# --------------------------------------------------------------------------------------------- GUI
SETTINGS = Path(__file__).with_suffix(".settings.json")


# --------------------------------------------------------------------------------------------- sound
_ACM_SIG = b"\x97\x28\x03\x01"


def sound_to_wav(data: bytes) -> Tuple[Optional[bytes], str]:
    """(RIFF WAV bytes, error). Plain WAV is used as is; WAVC/ACM and Ogg Vorbis are converted with ffmpeg."""
    if data[:4] == b"RIFF":
        return data, ""
    if data[:4] == b"WAVC" and len(data) > 28:
        hsz = struct.unpack_from("<I", data, 16)[0] or 28
        ch = struct.unpack_from("<H", data, 20)[0]
        acm = bytearray(data[hsz:])
        if acm[:4] == _ACM_SIG and ch in (1, 2):
            acm[8:10] = struct.pack("<H", ch)        # the WAVC header has the true channel count, the ACM one may not
        src, fmt = bytes(acm), "acm"
    elif data[:4] == _ACM_SIG:
        src, fmt = data, "acm"
    elif data[:4] == b"OggS":
        src, fmt = data, "ogg"
    else:
        return None, "unknown sound format"
    ff = shutil.which("ffmpeg")
    if not ff:
        return None, f"this sound is {fmt.upper()}; ffmpeg is needed to play it (install it and put it on PATH)"
    try:
        p = subprocess.run([ff, "-v", "error", "-f", fmt, "-i", "pipe:0", "-f", "wav", "-acodec", "pcm_s16le", "pipe:1"],
                           input=src, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ffmpeg failed: {e}"
    if p.returncode or p.stdout[:4] != b"RIFF":
        return None, "ffmpeg could not convert it: " + p.stderr.decode("utf-8", "replace").strip()[:200]
    return fix_wav_sizes(p.stdout), ""


def fix_wav_sizes(data: bytes) -> bytes:
    """WAV piped out of ffmpeg has placeholder sizes; write the real ones so players and wave read it right."""
    out = bytearray(data)
    struct.pack_into("<I", out, 4, len(out) - 8)
    pos = 12
    while pos + 8 <= len(out):
        cid, size = out[pos:pos + 4], struct.unpack_from("<I", out, pos + 4)[0]
        if cid == b"data":
            struct.pack_into("<I", out, pos + 4, len(out) - pos - 8)
            break
        pos += 8 + size + (size & 1)
    return bytes(out)


def wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError, ValueError):
        return 0.0


_player: Dict[str, object] = {}


def play_file(path: str) -> bool:
    """Start playing a WAV file without waiting (stops whatever was playing)."""
    stop_sound()
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return True
    except ImportError:
        pass
    except RuntimeError:
        return False
    for cmd in ("afplay", "paplay", "aplay"):
        exe = shutil.which(cmd)
        if exe:
            _player["proc"] = subprocess.Popen([exe, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def stop_sound() -> None:
    try:
        import winsound
        winsound.PlaySound(None, 0)
    except (ImportError, RuntimeError):
        pass
    proc = _player.pop("proc", None)
    if proc is not None:
        try:
            proc.terminate()                                          # type: ignore[attr-defined]
        except OSError:
            pass


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


POOL_EXT = (".png", ".jpg", ".jpeg", ".webp", ".tga", ".gif", ".bmp")
POOL_MADE = re.compile(r"^(.*?)_([A-Za-z0-9#!-]{1,8})_(?:[ce]\d+_\d{8}-\d{6}|[fp]\d+|gnow)", re.I)


def _pool_rank(q: Path) -> Tuple[int, float]:
    """Which member of a family the pool shows: one that was worked on, else the newest."""
    try:
        when = q.stat().st_mtime
    except OSError:
        when = 0.0
    return (1 if q.stem.endswith(("_touch", "_skin", "_ghost")) else 0, when)


def spare_pictures(made_in: str, used: Set[str]) -> List[Path]:
    """Every generated picture nothing proposes: what is left over each time one of four is chosen.

    One entry per picture, not per version of it - a touched-up copy stands for the one it was made from."""
    best: Dict[str, Path] = {}
    base = Path(made_in)
    if not base.is_dir():
        return []
    walk: List[Path] = []
    for dir_, subs_, files_ in os.walk(base):                        # the creatures' sprites (_sprites) are
        subs_[:] = [d_ for d_ in subs_ if d_.lower() != "_sprites"]  # drawn, not generated: not spares
        walk += [Path(dir_) / f_ for f_ in files_]
    for q in walk:
        try:
            if not q.is_file() or q.suffix.lower() not in POOL_EXT:
                continue
            if "_touch_tmp" in q.stem:
                continue
            if q.suffix.lower() == ".bmp" and CUT_MARK.search(q.name):
                continue                                             # a crop cut from one of the pictures
            if "_gnow_" in q.stem:                                   # the portrait a creature wears in the game,
                continue                                             # copied out of it - not a spare of ours
            if os.path.normcase(str(q)) in used:
                continue
            fam = os.path.normcase(str(plain_of(str(q))))            # the plain picture and anything made from it
            now = best.get(fam)                                      # a worked-on copy stands for the family,
            if now is None or _pool_rank(q) > _pool_rank(now):       # otherwise the newest of them
                best[fam] = q
        except OSError:
            continue
    return sorted(best.values(), key=lambda q: (q.parent.name.lower(), q.name.lower()))


def proposed_pictures(every: Dict[str, Dict[str, str]],
                      from_map: Optional[Dict[str, str]] = None) -> Set[str]:
    """Every picture a proposal rests on, across all the mods analyzed: the game portrait that was cut, the
    picture it was cut from, the plain one behind that, and - when the picture was taken out of the pool -
    the spare it was copied from. Anything else in the folder is spare."""
    out: Set[str] = set()
    for pr in (every or {}).values():
        for v in (pr or {}).values():
            if not isinstance(v, str) or not v:
                continue
            q = Path(v)
            for x in (q, plain_of(str(q))):
                out.add(os.path.normcase(str(x)))
            stem = CUT_MARK.sub("", q.name)                          # "Bob_169x266_z11.bmp" -> "Bob"
            if stem != q.name:
                for e in POOL_EXT:
                    was = q.with_name(stem + e)
                    out.add(os.path.normcase(str(was)))
                    out.add(os.path.normcase(str(plain_of(str(was)))))
    for _ in range(4):                                               # a copy of a copy, up to a sane depth
        more = {v for k, v in (from_map or {}).items() if k in out} - out
        if not more:
            break
        out |= more
        for w in list(more):
            out.add(os.path.normcase(str(plain_of(w))))
    return out


def made_for(name: str) -> str:
    """The creature a generated picture was made for, read back out of its file name."""
    m = POOL_MADE.match(name)
    return (m.group(1).replace("_", " ").strip() if m else Path(name).stem.replace("_", " ")) or "?"


def made_from(name: str) -> str:
    """The creature file a generated picture was made for, which is what says who is in it."""
    m = POOL_MADE.match(name)
    return m.group(2).upper() if m else ""


def esc_closes(w: object) -> None:
    """Esc closes this window. The keyboard has to be put on it as well, or Esc goes to whatever had it."""
    def gone(_e: object = None) -> None:
        try:
            w.destroy()                                              # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - already closed
            pass

    def take() -> None:                                              # once it is on screen, not before
        try:
            if w.winfo_exists():                                     # type: ignore[attr-defined]
                w.focus_set()          # focus_set, not focus_force: that takes the keyboard off other programs
        except Exception:  # noqa: BLE001 - closed before it was ever shown
            pass
    try:
        w.bind("<Escape>", gone)                                     # type: ignore[attr-defined]
        w.after(1, take)                                             # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not a window
        pass


def image_seed(path: str) -> Optional[int]:
    """The seed a generated picture was painted from, read from what the engine wrote into the PNG
    ("Seed: 2238394058"). A touched-up copy or a game-size .bmp asks the picture it was made from."""
    q = plain_of(path)
    if q.suffix.lower() == ".bmp":                               # Name_c1_..._169x266_z12_m0_1.bmp -> its .png
        base = CUT_MARK.sub("", q.name)
        if base != q.name:
            cand = q.with_name(base + ".png")
            if cand.is_file():
                q = cand
    try:
        from PIL import Image
        with Image.open(q) as im:
            info = str(im.info.get("parameters") or "")
    except Exception:  # noqa: BLE001 - no Pillow, not a PNG, or no text in it
        return None
    m = re.search(r"\bSeed:\s*(\d+)", info)
    return int(m.group(1)) if m else None


def inherited_thumb(game: object, imgs: dict, name: str, thumb: Callable[[bytes], object]) -> object:
    """The game's own portrait a copied creature keeps, as a thumbnail (cached with the gallery's)."""
    key_ = "inherit:" + name.upper()
    if key_ not in imgs:
        try:
            data = game.get(name, T_BMP)                             # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            data = None
        imgs[key_] = thumb(data) if data else None
    return imgs[key_]


def plain_of(path: str) -> Path:
    """The picture as it came out of the engine, whatever has been done to it since."""
    q = Path(path)
    if not q.stem.endswith(("_ghost", "_skin", "_touch")):
        return q
    cut = next(len(x) for x in ("_ghost", "_touch", "_skin") if q.stem.endswith(x))
    return next((x for x in (q.with_name(q.stem[:-cut] + e) for e in
                             (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga", ".gif", ".tif"))
                 if x.is_file()), q)


_TIP_NOW: Dict[str, object] = {"win": None}


def tooltip(widget: object, text: str) -> None:
    """Show `text` in a small box while the mouse rests on the widget."""
    import tkinter as tk
    tip: Dict[str, object] = {"win": None, "job": None}

    def show() -> None:
        tip["job"] = None
        try:                                                     # the widget may be gone or hidden by now (a card
            if not widget.winfo_exists() or not widget.winfo_ismapped():   # redrawn under the mouse)
                return                                           # type: ignore[attr-defined]
        except tk.TclError:
            return
        old = _TIP_NOW.get("win")                                # one tooltip on screen at a time
        if old is not None:
            try:
                old.destroy()                                    # type: ignore[attr-defined]
            except tk.TclError:
                pass
        w = tk.Toplevel(widget.winfo_toplevel())   # type: ignore[attr-defined]
        #   not esc_closes(w): that puts the keyboard on the window it closes, and a tooltip that takes the
        #   keyboard leaves the keys going nowhere once it is gone (the Pool's arrow keys stopped dead)
        w.wm_overrideredirect(True)
        w.wm_geometry(f"+{widget.winfo_rootx() + 12}+{widget.winfo_rooty() + widget.winfo_height() + 4}")   # type: ignore[attr-defined]
        tk.Label(w, text=text, justify="left", wraplength=420, bg="#ffffe0", relief="solid", bd=1,
                 padx=6, pady=4).pack()
        tip["win"] = w
        _TIP_NOW["win"] = w

    def enter(_e: object) -> None:
        leave(_e)
        tip["job"] = widget.after(450, show)   # type: ignore[attr-defined]

    def leave(_e: object) -> None:
        if tip["job"]:
            widget.after_cancel(tip["job"])                      # type: ignore[attr-defined,arg-type]
            tip["job"] = None
        if tip["win"]:
            try:
                tip["win"].destroy()                             # type: ignore[attr-defined]
            except tk.TclError:
                pass
            if _TIP_NOW.get("win") is tip["win"]:
                _TIP_NOW["win"] = None
            tip["win"] = None
    widget.bind("<Enter>", enter, add="+")                       # type: ignore[attr-defined]
    widget.bind("<Destroy>", leave, add="+")                     # type: ignore[attr-defined]  never left behind
    widget.bind("<Leave>", leave, add="+")                       # type: ignore[attr-defined]
    widget.bind("<ButtonPress>", leave, add="+")                 # type: ignore[attr-defined]

CLONE_RE = re.compile(r"COPY_EXISTING\s+[~\"%]\s*([A-Za-z0-9_#!.-]+)\.CRE\s*[~\"%]\s*"
                      r"[~\"%]\s*(?:override[/\\\\])?([A-Za-z0-9_#!.-]+)\.CRE\s*[~\"%]", re.I)


def clone_sources(mod_dir: str) -> Dict[str, str]:
    """Creatures a mod makes by copying one of the game's own: the new name -> the one it was copied from.

    A copy keeps whatever the original had, portrait included, so a creature can end up wearing a face that
    belongs to somebody else - or none at all, when the mod was installed before whatever gave the original
    its portrait. Nothing of that link is kept in the creature file, so it is read from the mod's installer."""
    out: Dict[str, str] = {}
    if not mod_dir or not os.path.isdir(mod_dir):
        return out
    for root_, dirs, names in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("backup", "tutu", "bgt")]
        for n in names:
            if os.path.splitext(n)[1].lower() not in (".tp2", ".tph", ".tpa", ".tpp"):
                continue
            try:
                text = Path(root_, n).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for from_, to_ in CLONE_RE.findall(text):
                if from_.upper() != to_.upper():                     # X -> X is a patch, not a copy
                    out.setdefault(to_.upper(), from_.upper())
    return out


def cre_portrait(game: object, name: str) -> Tuple[str, int]:
    """(the portrait a creature file names, large first, its name's strref). ("", -1) when it names none."""
    try:
        b = game.get(name, T_CRE, 0x2D4)                             # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a half-read install reads no creatures
        return "", -1
    if not b or len(b) < 0x44 or b[:4] != b"CRE ":
        return "", -1
    ref = struct.unpack_from("<i", b, 8)[0]
    for off in (0x3C, 0x34):
        v = b[off:off + 8].split(b"\0", 1)[0].decode("latin1").strip()
        if v and v.upper() not in NO_PORTRAIT:
            return v, ref
    return "", ref


ANIMAL_WORDS = {"cat", "dog", "wolf", "warg", "jackal", "hound", "mastiff", "fox", "horse", "pony", "mule", "bear",
                "rat", "bird", "chicken", "hen", "cow", "ox", "deer", "stag", "boar", "pig", "goat", "sheep", "rabbit",
                "squirrel", "badger", "bat", "raven", "crow", "owl", "hawk", "eagle", "snake", "spider", "beetle",
                "panther", "leopard", "tiger", "lion", "ape", "baboon", "monkey"}


def is_animal(row: dict) -> bool:
    """A real animal, going by its race, its animation and its name: a cat, a dog, a wolf..."""
    words = set(re.findall(r"[a-z]+", " ".join(str(row.get(k) or "") for k in ("race", "anim", "name")).lower()))
    return bool(words & ANIMAL_WORDS)


def animal_style(name: str) -> bool:
    """A generator style meant for animals rather than people."""
    return "animal" in str(name).lower()


MAX_CANDIDATES = 100                                                 # how many pictures one Generate may paint
#                                                                      (generator/settings.json max_candidates wins)
CUT_MARK = re.compile(r"_\d+x\d+_z\d+(_m-?\d+_-?\d+)?\.bmp$", re.I)   # what "Use this" adds to a crop


def lost_proposals(props: Dict[str, str]) -> Dict[str, str]:
    """Proposals whose picture is no longer on the disk -> another crop of the same picture, where there is
    one. A re-crop deletes the cut-out it replaces, so the name in the proposal can be left pointing at it."""
    out: Dict[str, str] = {}
    for k, v in props.items():
        if not v or os.path.isfile(v):
            continue
        q = Path(v)
        base = CUT_MARK.sub("", q.name)
        sibs: List[Path] = []
        if base != q.name:                                   # it was a crop, so look for the newer one
            try:
                sibs = [x for x in q.parent.glob(base + "_*x*.bmp") if x.is_file()]
            except OSError:
                sibs = []
        out[k] = os.path.normpath(str(max(sibs, key=lambda x: x.stat().st_mtime))) if sibs else ""
    return out


TOUCH_MEMORY: Dict[str, dict] = {}        # what was last done to each picture, for as long as the program runs


def touch_up_window(dlg: object, path: str, *, pg: object, gs: dict, r: dict, desc: str,
                    say: Callable[[str], None], on_done: Callable[..., None],
                    touch_memory: Dict[str, dict], generic: bool = False,
                    change_it: Optional[Callable[[str], None]] = None) -> None:
    """Touch up a finished picture: the skin's color, how dark it is, the lips and the eyes. Opening it
    again on a picture already worked on starts from the plain one, with the sliders where they were -
    so it is the same touch up carried on, not one piled on top of another."""
    import tkinter as tk
    from tkinter import ttk
    q = plain_of(path)
    mem = touch_memory.get(str(q), {})
    try:
        from PIL import Image, ImageTk
        src = Image.open(q).convert("RGB")
    except Exception:  # noqa: BLE001 - no Pillow, or a picture it will not open
        say("Pillow is needed to touch a picture up, and it is not installed.")
        return
    skins = dict(getattr(pg, "SKIN_TINTS", {}) or {})
    lipset = dict(getattr(pg, "LIP_TINTS", {}) or {})
    eyeset = dict(getattr(pg, "SPOT_TINTS", None) or getattr(pg, "EYE_TINTS", {}) or {})
    bloods = dict(getattr(pg, "BLOOD_TINTS", {}) or {"Blood": ()})
    glows = dict(getattr(pg, "GLOW_TINTS", {}) or {"Fire": ()})
    paints = dict(getattr(pg, "PAINT_TINTS", {}) or {"Woad blue": ()})
    pstyles = list(getattr(pg, "PAINT_STYLES", ("None",)))
    if not skins or not lipset:
        return
    win = tk.Toplevel(dlg)
    esc_closes(win)
    win.title(f"Touch up - {r['name']}")
    win.transient(dlg)
    # the picture at its own size, with scrollbars when the screen has no room for it
    room_w = max(260, int(win.winfo_screenwidth() * 0.46))
    room_h = max(320, int(win.winfo_screenheight() * 0.78))
    small = src
    sbuf = io.BytesIO()
    small.save(sbuf, "PNG")
    thumb = sbuf.getvalue()
    rough = src.resize((max(1, src.width // 2), max(1, src.height // 2)), Image.BILINEAR)
    rbuf = io.BytesIO()                                      # half size, for while a slider is moving:
    rough.save(rbuf, "PNG")                                  # a full pass is too slow to follow a drag
    thumb_rough = rbuf.getvalue()
    body = ttk.Frame(win)
    body.pack(fill="both", expand=True, padx=10, pady=10)
    hold = ttk.Frame(body)
    hold.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="n")
    pane = ttk.Frame(hold)                                   # the picture and its scrollbars
    pane.pack(side="top")
    zrow = ttk.Frame(hold)                                   # how big it is shown
    zrow.pack(side="top", fill="x", pady=(5, 0))
    fit_k = min(room_w / max(1, small.width), room_h * 0.8 / max(1, small.height), 3.0)
    zoom_at = {"k": round(fit_k, 2) if fit_k >= 1.5 else 1.0}   # a game portrait opens big enough to work on
    view_w, view_h = min(small.width, room_w), min(small.height, room_h)
    lab = tk.Canvas(pane, width=view_w, height=view_h, highlightthickness=0, cursor="tcross",
                    scrollregion=(0, 0, small.width, small.height))
    pv = ttk.Scrollbar(pane, orient="vertical", command=lab.yview)
    ph = ttk.Scrollbar(pane, orient="horizontal", command=lab.xview)
    lab.configure(yscrollcommand=pv.set, xscrollcommand=ph.set)
    zcap = tk.StringVar(value="")

    def shown() -> Tuple[int, int]:
        """The picture's size on screen. The touch up itself is always done at the picture's own size."""
        k = zoom_at["k"]
        return max(1, int(small.width * k)), max(1, int(small.height * k))

    def fit_view() -> None:
        """The canvas, its scrollbars and the caption, for whatever zoom is set now."""
        nonlocal view_w, view_h
        sw_, sh_ = shown()
        view_w, view_h = min(sw_, room_w), min(sh_, room_h)
        lab.configure(width=view_w, height=view_h, scrollregion=(0, 0, sw_, sh_))
        for w__ in (pv, ph, lab):                            # packed in this order, so the bar under the
            w__.pack_forget()                                # picture does not run under the side one
        if sh_ > view_h:
            pv.pack(side="right", fill="y")
        if sw_ > view_w:
            ph.pack(side="bottom", fill="x")
        lab.pack(side="left")
        zcap.set(f"{small.width}x{small.height} at {int(zoom_at['k'] * 100)}%")

    def zoom_to(k: float) -> None:
        zoom_at["k"] = max(0.25, min(8.0, round(k, 2)))
        fit_view()
        draw()
    ttk.Label(zrow, text="Zoom:").pack(side="left")
    ttk.Button(zrow, text="-", width=3, command=lambda: zoom_to(zoom_at["k"] / 1.25)).pack(side="left", padx=(4, 0))
    ttk.Button(zrow, text="+", width=3, command=lambda: zoom_to(zoom_at["k"] * 1.25)).pack(side="left", padx=(4, 0))
    ttk.Button(zrow, text="1:1", width=4, command=lambda: zoom_to(1.0)).pack(side="left", padx=(4, 0))
    ttk.Button(zrow, text="Fit", width=4, command=lambda: zoom_to(fit_k)).pack(side="left", padx=(4, 8))
    ttk.Label(zrow, textvariable=zcap, foreground="#888").pack(side="left")
    fit_view()
    lab.bind("<MouseWheel>", lambda e: lab.yview_scroll(-1 if e.delta > 0 else 1, "units"))
    lab.bind("<Control-MouseWheel>", lambda e: zoom_to(zoom_at["k"] * (1.25 if e.delta > 0 else 1 / 1.25)))
    sidebox = ttk.Frame(body)                            # the settings scroll: there are more of them
    sidebox.grid(row=0, column=1, sticky="nsew")         # than a short screen has room for
    scan = tk.Canvas(sidebox, highlightthickness=0, borderwidth=0, width=1, height=min(room_h, 820))
    sbar = ttk.Scrollbar(sidebox, orient="vertical", command=scan.yview)
    scan.configure(yscrollcommand=sbar.set)
    sbar.pack(side="right", fill="y")
    scan.pack(side="left", fill="both", expand=True)
    side = ttk.Frame(scan)
    scan.create_window((0, 0), window=side, anchor="nw")

    def side_fits(_e: object = None) -> None:
        scan.configure(scrollregion=scan.bbox("all"))
        need, tall = side.winfo_reqwidth(), side.winfo_reqheight()
        if need > 1 and scan.winfo_width() != need:
            scan.configure(width=need)
        room = min(tall, room_h)                         # as tall as it needs, up to what the screen has
        if room > 1 and scan.winfo_height() != room:
            scan.configure(height=room)
        if tall <= room_h:                               # it all fits: no bar in the way
            sbar.pack_forget()
        elif not sbar.winfo_ismapped():
            sbar.pack(side="right", fill="y", before=scan)
    side.bind("<Configure>", side_fits)

    def side_wheel(e: object) -> None:
        scan.yview_scroll(-1 if getattr(e, "delta", 0) > 0 else 1, "units")

    def wheel_on(wid: tk.Misc) -> None:
        """Every setting answers the wheel, not only the gaps between them."""
        wid.bind("<MouseWheel>", side_wheel)
        for ch in wid.winfo_children():
            wheel_on(ch)
    keep: Dict[str, object] = {}
    start_ = pg.suggest_tint(desc) if hasattr(pg, "suggest_tint") else ""   # type: ignore[attr-defined]
    skin_v = tk.StringVar(value="Leave")                             # always: a remembered or guessed color was
    #                                                                  laid on before anything was asked for
    lips_v = tk.StringVar(value=mem.get("lips", "Leave"))
    amount = tk.DoubleVar(value=float(mem.get("amount", 0.9)))
    lipamt = tk.DoubleVar(value=float(mem.get("lipamt", 0.85)))
    light = tk.DoubleVar(value=float(mem.get("light", 0.0)))
    catch = tk.DoubleVar(value=float(mem.get("catch", gs.get("skin_catch", 0.6))))
    fade = tk.DoubleVar(value=float(mem.get("fade", 0.0)))
    grime = tk.DoubleVar(value=float(mem.get("grime", 0.0)))
    creases = tk.DoubleVar(value=float(mem.get("creases", 0.0)))
    speckle = tk.DoubleVar(value=float(mem.get("speckles", 0.0)))
    was_sc, was_wn = float(mem.get("scars", 0.0)), float(mem.get("wounds", 0.0))
    cut_v = tk.StringVar(value=mem.get("cut", "Wound" if was_wn > 0 else "Scar" if was_sc > 0 else "None"))
    cutamt = tk.DoubleVar(value=float(mem.get("cutamt", max(was_sc, was_wn) or 0.85)))
    flip_v = tk.BooleanVar(value=bool(mem.get("flip", False)))
    eyes_v = tk.StringVar(value=mem.get("eyes", "Leave"))
    marks_v = tk.BooleanVar(value=True)
    eyeamt = tk.DoubleVar(value=float(mem.get("eyeamt", 0.95)))
    eyesize = tk.DoubleVar(value=float(mem.get("eyesize", 0.035)))
    spots: List[Tuple[float, float]] = list(mem.get("spots", []))
    shapes: List[Tuple[float, float]] = list(mem.get("shapes", []))
    blots: List[Tuple[float, float]] = list(mem.get("blots", []))
    spkarea = tk.DoubleVar(value=float(mem.get("spkarea", 0.16)))
    spkdeep = tk.DoubleVar(value=float(mem.get("spkdeep", 0.6)))
    spkamt = tk.DoubleVar(value=float(mem.get("spkamt", 0.75)))
    mark_kind = tk.StringVar(value="color")
    warp = tk.DoubleVar(value=float(mem.get("warp", 0.0)))
    warpsize = tk.DoubleVar(value=float(mem.get("warpsize", 0.17)))
    ridge = tk.DoubleVar(value=float(mem.get("ridge", 0.5)))
    linew = tk.DoubleVar(value=float(mem.get("line", 1.0)))
    stretch = tk.DoubleVar(value=float(mem.get("stretch", 0.0)))
    scarlen = tk.DoubleVar(value=float(mem.get("scarlen", 0.17)))
    glow_v = tk.StringVar(value=mem.get("glow", "None"))
    glowclr = tk.StringVar(value=mem.get("glowtint", next(iter(glows), "Fire")))
    glowamt = tk.DoubleVar(value=float(mem.get("glowamt", 0.85)))
    glowsize = tk.DoubleVar(value=float(mem.get("glowsize", 0.028)))
    glowspill = tk.DoubleVar(value=float(mem.get("glowspill", 0.6)))
    glowround = tk.DoubleVar(value=float(mem.get("glowround", 0.40)))
    angle_v = tk.DoubleVar(value=float(mem.get("angle", -55.0)))
    edge_v = tk.DoubleVar(value=float(mem.get("edge", 0.5)))
    blood_v = tk.StringVar(value=mem.get("blood", next(iter(bloods), "Blood")))
    soot_v = tk.DoubleVar(value=float(mem.get("soot", 0.0)))
    mud_v = tk.DoubleVar(value=float(mem.get("mud", 0.0)))
    spat_v = tk.DoubleVar(value=float(mem.get("spatter", 0.0)))
    sweat_v = tk.DoubleVar(value=float(mem.get("sweat", 0.0)))
    dsize_v = tk.DoubleVar(value=float(mem.get("dirtsize", 1.0)))
    paint_v = tk.StringVar(value=mem.get("paint", "None"))
    paintclr = tk.StringVar(value=mem.get("paintclr", next(iter(paints), "Woad blue")))
    paintamt = tk.DoubleVar(value=float(mem.get("paintamt", 0.8)))
    paintsize = tk.DoubleVar(value=float(mem.get("paintsize", 1.0)))
    peyes: List[Tuple[float, float]] = list(mem.get("peyes", []))
    erases: List[Tuple[float, float, float]] = []          # (x, y, radius) as shares of the width: rubbed out
    erase_on = tk.BooleanVar(value=False)
    erase_size = tk.DoubleVar(value=0.025)

    def rub_out(before: bytes, after: bytes) -> bytes:
        """Put back what was under the eraser: this pass's changes are taken off there, nothing else."""
        if not erases:
            return after
        a_ = Image.open(io.BytesIO(after)).convert("RGB")
        b_ = Image.open(io.BytesIO(before)).convert("RGB")
        if b_.size != a_.size:
            b_ = b_.resize(a_.size, Image.BILINEAR)
        from PIL import ImageDraw as _ID, ImageFilter as _IF
        m_ = Image.new("L", a_.size, 0)
        d_ = _ID.Draw(m_)
        ww_ = a_.width
        for fx, fy, rr in erases:
            r_ = max(1.0, rr * ww_)
            d_.ellipse([fx * ww_ - r_, fy * a_.height - r_, fx * ww_ + r_, fy * a_.height + r_], fill=255)
        m_ = m_.filter(_IF.GaussianBlur(max(0.8, ww_ * 0.003)))   # soft edged, so no ring shows
        out_ = io.BytesIO()
        Image.composite(b_, a_, m_).save(out_, "PNG")
        return out_.getvalue()
    DIRT_VARS = ((soot_v, "soot"), (mud_v, "mud"), (spat_v, "spatter"), (sweat_v, "sweat"), (dsize_v, "dirtsize"),
                 (paintamt, "paintamt"), (paintsize, "paintsize"))

    def extra() -> Dict[str, object]:
        """The dirt and the face paint, handed to touch_up by name."""
        return {"soot": round(float(soot_v.get()), 2), "mud": round(float(mud_v.get()), 2),
                "spatter": round(float(spat_v.get()), 2), "sweat": round(float(sweat_v.get()), 2),
                "dirt_size": round(float(dsize_v.get()), 2), "paint": paint_v.get(), "paint_color": paintclr.get(),
                "paint_amount": round(float(paintamt.get()), 2) if paint_v.get() != "None" else 0.0,
                "paint_size": round(float(paintsize.get()), 2), "paint_eyes": list(peyes)}

    def extra_set() -> bool:
        x_ = extra()
        return bool(erases) or (any(float(x_[k]) > 0 for k in ("soot", "mud", "spatter", "sweat"))   # type: ignore[arg-type]
                or (x_["paint"] != "None" and len(peyes) >= 2))
    hist: List[dict] = []                                # one entry per step taken
    prev: Dict[str, object] = {}                         # how things stood after the last redraw

    def snapshot() -> dict:
        return {"skin": skin_v.get(), "lips": lips_v.get(), "eyes": eyes_v.get(), "kind": mark_kind.get(), "flip": flip_v.get(),
                "amount": amount.get(), "lipamt": lipamt.get(), "eyeamt": eyeamt.get(),
                "eyesize": eyesize.get(), "light": light.get(), "catch": catch.get(), "fade": fade.get(),
                "grime": grime.get(), "creases": creases.get(), "speckles": speckle.get(), "cut": cut_v.get(),
                "cutamt": cutamt.get(), "flip": flip_v.get(), "warp": warp.get(),
                "warpsize": warpsize.get(), "ridge": ridge.get(), "line": linew.get(), "scarlen": scarlen.get(), "angle": angle_v.get(),
                "stretch": stretch.get(),
                "edge": edge_v.get(), "blood": blood_v.get(), "glow": glow_v.get(),
                "glowtint": glowclr.get(), "glowamt": glowamt.get(), "glowsize": glowsize.get(),
                "glowspill": glowspill.get(), "glowround": glowround.get(),
                "spots": list(spots), "shapes": list(shapes), "blots": list(blots),
                "spkarea": spkarea.get(), "spkdeep": spkdeep.get(), "spkamt": spkamt.get(),
                "paint": paint_v.get(), "paintclr": paintclr.get(), "peyes": list(peyes), "erases": list(erases),
                **{k_: v_.get() for v_, k_ in DIRT_VARS}}

    def remember() -> None:
        """Put the state as it was before this change on the pile."""
        if prev:
            hist.append(dict(prev))
            del hist[:-40]

    def undo() -> None:
        if not hist:
            say("Nothing to undo.")
            return
        d = hist.pop()
        if d.get("base"):                                    # a pass that was painted in: take it off again
            set_base(d["base"])                              # type: ignore[arg-type]
        skin_v.set(d["skin"]), lips_v.set(d["lips"]), eyes_v.set(d["eyes"]), mark_kind.set(d["kind"])
        flip_v.set(bool(d.get("flip", False)))
        for var, key in ((amount, "amount"), (lipamt, "lipamt"), (eyeamt, "eyeamt"), (eyesize, "eyesize"),
                         (light, "light"), (catch, "catch"), (fade, "fade"), (grime, "grime"),
                         (creases, "creases"), (speckle, "speckles"), (cutamt, "cutamt"),
                         (warp, "warp"), (warpsize, "warpsize"), (ridge, "ridge"), (linew, "line"),
                         (angle_v, "angle"), (edge_v, "edge"), (scarlen, "scarlen"), (stretch, "stretch"),
                         (glowamt, "glowamt"), (glowsize, "glowsize"), (glowspill, "glowspill"),
                         (glowround, "glowround")):
            var.set(float(d[key]))                           # type: ignore[arg-type]
        blood_v.set(str(d.get("blood", blood_v.get())))
        cut_v.set(str(d.get("cut", cut_v.get())))
        glow_v.set(str(d.get("glow", glow_v.get())))
        glowclr.set(str(d.get("glowtint", glowclr.get())))
        spots.clear()
        spots.extend(d["spots"])                             # type: ignore[arg-type]
        shapes.clear()
        shapes.extend(d["shapes"])                           # type: ignore[arg-type]
        blots.clear()
        blots.extend(d.get("blots") or [])                   # type: ignore[arg-type]
        spkarea.set(float(d.get("spkarea", spkarea.get())))
        spkdeep.set(float(d.get("spkdeep", spkdeep.get())))
        spkamt.set(float(d.get("spkamt", spkamt.get())))
        for v_, k_ in DIRT_VARS:
            v_.set(float(d.get(k_, v_.get())))                # type: ignore[arg-type]
        paint_v.set(str(d.get("paint", paint_v.get())))
        paintclr.set(str(d.get("paintclr", paintclr.get())))
        peyes.clear()
        peyes.extend(d.get("peyes") or [])                   # type: ignore[arg-type]
        erases.clear()
        erases.extend(d.get("erases") or [])                 # type: ignore[arg-type]
        prev.clear()
        prev.update(d)
        tab_marks(redraw=False)                              # the tab, not the undone step, says where
        draw()                                               # draw() puts the button right again

    def set_base(png: bytes) -> None:
        """Make this the picture everything is worked out from (a pass painted in, or an undo of one)."""
        nonlocal thumb, thumb_rough, small
        thumb = png
        small = Image.open(io.BytesIO(png)).convert("RGB")
        rb = io.BytesIO()
        small.resize((max(1, small.width // 2), max(1, small.height // 2)), Image.BILINEAR).save(rb, "PNG")
        thumb_rough = rb.getvalue()
        fit_view()                                           # in case the painted-in pass is another size

    def values() -> Tuple[object, ...]:
        hue, sat = skins.get(skin_v.get(), (110, 1.3))
        lh, ls = lipset.get(lips_v.get(), (6, 1.7))
        eh, es, ev = eyeset.get(eyes_v.get(), (35, 185, -1))
        amt = 0.0 if skin_v.get() == "Leave" else round(float(amount.get()), 2)
        lam = 0.0 if lips_v.get() == "Leave" else round(float(lipamt.get()), 2)
        eam = 0.0 if eyes_v.get() == "Leave" or not spots else round(float(eyeamt.get()), 2)
        return (hue, amt, sat, round(float(catch.get()), 2), round(float(light.get()), 2), lh, lam, ls,
                eh, eam, es, round(float(eyesize.get()), 3), ev, round(float(fade.get()), 2),
                round(float(grime.get()), 2), round(float(warp.get()), 2), round(float(warpsize.get()), 3),
                round(float(creases.get()), 2),
                round(float(cutamt.get()), 2) if cut_v.get() == "Scar" else 0.0,
                round(float(cutamt.get()), 2) if cut_v.get() == "Wound" else 0.0, bool(flip_v.get()),
                round(float(ridge.get()), 2), blood_v.get(), round(float(linew.get()), 2),
                round(float(angle_v.get()), 1), round(float(edge_v.get()), 2),
                round(float(scarlen.get()), 3), glow_v.get(), glowclr.get(),
                0.0 if glow_v.get() == "None" or not spots else round(float(glowamt.get()), 2),
                round(float(glowsize.get()), 3), round(float(glowspill.get()), 2),
                round(float(glowround.get()), 2), round(float(stretch.get()), 2),
                round(float(speckle.get()), 2), round(float(spkarea.get()), 3),
                round(float(spkdeep.get()), 2), round(float(spkamt.get()), 2))

    def draw(fast: bool = False) -> None:
        keep.pop("job", None)
        (hue, amt, sat, cat, lig, lh, lam, ls, eh, eam, es, esz, ev, fad, gri, wrp, wsz,
         cre, scr, wnd, flp, rdg, bld, lnw, agl, edg, sln, glw, gtn, gam, gsz, gsp, grd, stc, spk, spa, spd, spm) = values()
        gpct2.config(text=f"{int(gri * 100)}%")
        cpct3.config(text=f"{int(max(scr, wnd) * 100)}%")
        for rb__ in bbtn:
            rb__.config(state="normal" if wnd > 0 else "disabled")
        rdpct.config(text=f"{int(rdg * 100)}%")
        lnpct.config(text=f"{lnw:.1f}x")
        agpct.config(text=f"{int(agl)}\N{DEGREE SIGN}")
        lgpct.config(text=f"{sln:.2f}")
        rch.config(text=f"{wsz:.2f}")
        stpct.config(text=("+" if stc > 0 else "") + f"{int(stc * 100)}%")
        edpct.config(text=f"{int(edg * 100)}%")
        cpct2.config(text=f"{int(cre * 100)}%")
        wpct.config(text=("+" if wrp > 0 else "") + f"{int(wrp * 100)}%")
        fpct.config(text=f"{int(fad * 100)}%")
        pct.config(text=f"{int(amt * 100)}%")
        lpct.config(text=f"{int(lam * 100)}%")
        cpct.config(text=f"{int(cat * 100)}%")
        spkpct.config(text=f"{int(spk * 100)}%")
        for w_, v_ in ((sopct, soot_v), (mupct, mud_v), (sppct, spat_v), (swpct, sweat_v), (papct, paintamt)):
            w_.config(text=f"{int(float(v_.get()) * 100)}%")
        dzpct.config(text=f"{float(dsize_v.get()):.1f}x")
        pzpct.config(text=f"{float(paintsize.get()):.1f}x")
        peyelab.config(text=(f"Eyes marked. Click twice more to place them again." if len(peyes) >= 2 else
                             "Click the two eyes on the picture: the paint is placed from them."
                             if not peyes else "One eye marked - click the other."),
                       foreground="#666" if len(peyes) >= 2 or paint_v.get() == "None" else "#b5452f")
        for w_, on_ in ((pct, skin_v.get() != "Leave"), (lpct, lips_v.get() != "Leave"),
                        (epct, eyes_v.get() != "Leave"), (arpct, spk > 0), (dppct, spk > 0), (smpct, spk > 0)):
            sc__ = keep.get(f"sc_{id(w_)}")              # a strength with nothing to be strong about
            if sc__ is not None:
                sc__.config(state="normal" if on_ else "disabled")   # type: ignore[union-attr]
            if not on_:
                w_.config(text="-")                      # not 0%, which its own handle contradicts
        arpct.config(text=f"{spa:.2f}")
        dppct.config(text=("+" if spd > 0 else "") + f"{int(spd * 100)}%")
        smpct.config(text=f"{int(spm * 100)}%")
        blotlab.config(text=(f"{len(blots)} patch(es) marked." if blots else
                             "Click a cheek to put the speckles there."),
                       foreground="#666")
        epct.config(text=f"{int(eam * 100)}%")
        for box_, on_, after_ in ((lit_, glw != "None", lrow), (cut_, cut_v.get() != "None", crow)):
            if on_ and not box_.winfo_ismapped():        # a kind's own settings are no use until it is
                box_.pack(fill="x", padx=(10, 0), after=after_)     # chosen, and take up half the column
            elif not on_ and box_.winfo_ismapped():
                box_.pack_forget()
        glpct.config(text=f"{int(gam * 100)}%")
        gspct.config(text=f"{gsz:.3f}")
        gppct.config(text=f"{int(gsp * 100)}%")
        grpct.config(text=f"{grd:.2f}")
        gpct.config(text=("+" if lig > 0 else "") + f"{int(lig * 100)}%")
        if shapes:
            shapelab.config(text=f"{len(shapes)} shape point(s) marked.", foreground="#666")
        elif abs(wrp) >= 0.01 or scr > 0 or wnd > 0:
            shapelab.config(text="Nothing marked yet - tick 'clicks mark these' above, then click "
                                 "the face.", foreground="#b5452f")
        else:
            shapelab.config(text="Click a nose, a jaw, an eye - tick 'clicks mark these' first.",
                            foreground="#666")
        eyelab.config(text="Click an eye, a gem, a buckle..." if not spots else
                      f"{len(spots)} marked. Click more; the 9th starts over."
                      if len(spots) < 8 else "Eight marked - clicking again starts over.")
        clr_.config(state="normal" if spots else "disabled")
        try:  # noqa: SIM105
            src_ = thumb_rough if fast else thumb
            if flp:                                          # mirrored first, so the marks you place
                src_ = pg.flip_image(src_)                   # land where you see them
            src_ = pg.stretch_image(src_, shapes, stc, wsz)   # type: ignore[attr-defined]
            src_ = pg.warp_image(src_, shapes, wrp, wsz)     # type: ignore[attr-defined]
            if (scr > 0 or wnd > 0) and shapes:              # a scar sits on a little swell of its own
                src_ = pg.warp_image(src_, shapes, 0.20 * max(scr, wnd * 0.6),   # type: ignore[attr-defined]
                                     max(0.03, sln * 0.32))
            done_ = pg.touch_up(src_, hue, amt, sat, cat, lig, lh, lam, ls,   # type: ignore[attr-defined]
                                spots, eh, eam, es, esz, ev, gri, cre, scr, shapes, wnd, sln, rdg, bld, lnw, agl, edg, spk, blots, spa, spd, spm,
                                glw, gtn, gam, gsz, gsp, grd, **extra())
            done_ = rub_out(src_, done_)
            im = Image.open(io.BytesIO(pg.ghost_image(done_, fad)))   # type: ignore[attr-defined]
            sw_, sh_ = shown()                               # the rough pass is half size: back up to the
            if (im.width, im.height) != (sw_, sh_):          # shown size too, so nothing jumps about
                im = im.resize((sw_, sh_), Image.BILINEAR if fast else
                               Image.NEAREST if zoom_at["k"] >= 3 else Image.LANCZOS)
        except Exception as e:  # noqa: BLE001 - say so rather than showing an empty box
            lab.delete("all")
            lab.create_text(view_w // 2, view_h // 2, width=view_w - 20, fill="#b5452f",
                            text=f"Could not draw the preview:\n{e}")
            return
        keep["img"] = ImageTk.PhotoImage(im)
        lab.delete("all")
        lab.create_image(0, 0, image=keep["img"], anchor="nw")
        prev.clear()
        prev.update(snapshot())
        if keep.get("undo"):
            keep["undo"].config(state="normal" if hist else "disabled")   # type: ignore[union-attr]
        if marks_v.get():                                    # where you clicked, and what for
            sw_, sh_ = shown()
            if eyes_v.get() != "Leave":
                for fx, fy in spots:
                    x_, y_ = fx * sw_, fy * sh_
                    rr = max(3.0, esz * sw_)
                    lab.create_oval(x_ - rr, y_ - rr * 0.8, x_ + rr, y_ + rr * 0.8, outline="#ffd000", width=1)
            if erase_on.get():
                for fx, fy, rr in erases[-200:]:
                    x_, y_, r_ = fx * sw_, fy * sh_, max(2.0, rr * sw_)
                    lab.create_oval(x_ - r_, y_ - r_, x_ + r_, y_ + r_, outline="#bbbbbb", width=1)
            if paint_v.get() != "None" or mark_kind.get() == "paint":
                for fx, fy in peyes:
                    x_, y_ = fx * sw_, fy * sh_
                    lab.create_line(x_ - 6, y_, x_ + 6, y_, fill="#ff5fd2")
                    lab.create_line(x_, y_ - 6, x_, y_ + 6, fill="#ff5fd2")
            if spk > 0:
                for fx, fy in blots:
                    x_, y_ = fx * sw_, fy * sh_
                    rr = max(4.0, spa * sw_)
                    lab.create_oval(x_ - rr, y_ - rr, x_ + rr, y_ + rr, outline="#7ad17a", width=1)
            if abs(wrp) >= 0.01 or scr > 0 or wnd > 0:
                for fx, fy in shapes:
                    x_, y_ = fx * sw_, fy * sh_
                    rr = max(4.0, wsz * sw_)
                    lab.create_oval(x_ - rr, y_ - rr, x_ + rr, y_ + rr, outline="#36a0ff", width=1)

    def rub(e: "tk.Event", drag: bool = False) -> None:
        """The eraser: a click rubs out one spot, a drag a stroke."""
        sw_, sh_ = shown()
        fx, fy = lab.canvasx(e.x) / sw_, lab.canvasy(e.y) / sh_
        rr = float(erase_size.get())
        if drag and erases:
            lx_, ly_, _r = erases[-1]
            if ((fx - lx_) ** 2 + ((fy - ly_) * sh_ / max(1, sw_)) ** 2) ** 0.5 < rr * 0.35:
                return                                       # not moved far enough to be worth a new dab
        else:
            remember()                                       # one undo step per stroke
        erases.append((fx, fy, rr))
        del erases[:-600]
        later()

    def mark(e: "tk.Event") -> None:
        """Click an eye: two marks, then a third starts over."""
        if erase_on.get():
            rub(e)
            return
        remember()
        here = {"color": spots, "shape": shapes, "paint": peyes}.get(mark_kind.get(), blots)
        if len(here) >= (2 if here is peyes else 8):     # eight is plenty; the ninth starts over (paint: two eyes)
            here.clear()
        sw_, sh_ = shown()
        here.append((min(1.0, max(0.0, lab.canvasx(e.x) / sw_)),
                     min(1.0, max(0.0, lab.canvasy(e.y) / sh_))))
        if mark_kind.get() == "paint" and paint_v.get() == "None" and len(peyes) == 2:
            paint_v.set(next((k for k in pstyles if k != "None"), "None"))   # both eyes marked: show something
        elif mark_kind.get() == "skin" and float(speckle.get()) < 0.01:
            speckle.set(0.6)                             # a patch marked with nothing to put in it
        elif mark_kind.get() == "color" and eyes_v.get() == "Leave" and glow_v.get() == "None":
            eyes_v.set(next((k for k in eyeset if k != "Leave"), "Leave"))
        elif (mark_kind.get() == "shape" and abs(float(warp.get())) < 0.01
              and abs(float(stretch.get())) < 0.01 and cut_v.get() == "None"):
            warp.set(0.35)
        draw()
    lab.bind("<Button-1>", mark)
    lab.bind("<B1-Motion>", lambda e: rub(e, True) if erase_on.get() else None)
    lab.bind("<Button-3>", lambda e: clear_eyes())

    def later(*_a: object) -> None:
        """Rough while the slider is moving, proper once it stops."""
        for k in ("job", "slow"):
            if keep.get(k):
                win.after_cancel(keep[k])                    # type: ignore[arg-type]
                keep.pop(k, None)
        keep["job"] = win.after(35, lambda: draw(True))
        keep["slow"] = win.after(260, draw)

    def slider(parent: tk.Misc, text: str, var: tk.DoubleVar, lo: float, hi: float) -> ttk.Label:
        row_ = ttk.Frame(parent)
        row_.pack(fill="x", pady=(2, 0))
        ttk.Label(row_, text=text, width=10).pack(side="left")   # "How many" needs the tenth
        out = ttk.Label(row_, width=5)
        sc_ = ttk.Scale(row_, from_=lo, to=hi, variable=var, command=later)
        sc_.pack(side="left", fill="x", expand=True, padx=6)
        sc_.bind("<ButtonPress-1>", lambda e: remember(), add="+")   # one step per drag, not per pixel
        out.pack(side="left")
        keep[f"sc_{id(out)}"] = sc_                      # so a slider can be grayed when it does nothing
        return out
    def group(parent: tk.Misc, text: str) -> ttk.Frame:
        """One thing you can do, with the settings that belong to it under it and nothing else."""
        ttk.Label(parent, text=text, font=("TkDefaultFont", 8, "bold"),
                  foreground="#555").pack(anchor="w", pady=(6, 0))
        box_ = ttk.Frame(parent)
        box_.pack(fill="x", padx=(10, 0))
        return box_

    nb = ttk.Notebook(side)                              # a tab each, rather than one long column
    nb.pack(fill="both", expand=True)
    t_skin, t_lips, t_spot, t_shape, t_pic, t_paint = (ttk.Frame(nb, padding=(6, 4)) for _ in range(6))
    for f_, nm_ in ((t_skin, "Skin"), (t_lips, "Lips"), (t_spot, "Spots"), (t_shape, "Shape"),
                    (t_pic, "Picture"), (t_paint, "Paint")):
        nb.add(f_, text=nm_)

    def tab_marks(_e: object = None, redraw: bool = True) -> None:
        """Clicks on the picture land where you are working: spots on Spots, shape points on Shape."""
        now = nb.index(nb.select())
        want = {0: "skin", 2: "color", 3: "shape", 5: "paint"}.get(now)
        if want and mark_kind.get() != want:
            mark_kind.set(want)
            if redraw:
                draw()
    nb.bind("<<NotebookTabChanged>>", tab_marks)
    if change_it is not None:                                        # most of what this did, by asking the model
        then_ = {"change": False}

        def change_now() -> None:                                    # the touch up so far (a mirror, a color)
            then_["change"] = True                                   # is saved first, and the change asked of
            apply_()                                                 # that picture, not the one it started from
        cbx_ = ttk.Button(t_skin, text="Change it...", command=change_now)
        cbx_.pack(anchor="w", pady=(2, 4))
        tooltip(cbx_, "Ask for any change in words (a darker skin, a scar, an eyepatch...) instead, "
                      "with a strength.")
    fl0_ = ttk.Checkbutton(t_skin, text="Mirror it", variable=flip_v, command=lambda: (remember(), draw()))
    fl0_.pack(anchor="w", pady=(0, 4))                               # the same tick as on the Picture tab
    tooltip(fl0_, "Flip the picture left to right.")
    t_color = group(t_skin, "Color it")
    sgrid = ttk.Frame(t_color)
    sgrid.pack(fill="x")
    for i, nm_ in enumerate(["Leave"] + list(skins)):
        ttk.Radiobutton(sgrid, text=nm_, value=nm_, variable=skin_v,
                        command=lambda: (remember(), draw())).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 8))
    pct = slider(t_color, "Strength", amount, 0.1, 1.0)
    gpct = slider(t_color, "Darker", light, -1.0, 1.0)
    cpct = slider(t_color, "Catch", catch, 0.1, 1.0)
    t_speck = group(t_skin, "Speckle it")
    spkpct = slider(t_speck, "How many", speckle, 0.0, 1.0)
    smpct = slider(t_speck, "Strength", spkamt, 0.1, 1.0)
    dppct = slider(t_speck, "Tone", spkdeep, -1.0, 1.0)
    arpct = slider(t_speck, "Area", spkarea, 0.04, 0.45)
    tooltip(smpct, "How strongly the speckles are laid on, and nothing else. How many sets the count, "
                   "Tone sets whether they are lighter or darker than the skin, Area how wide a patch "
                   "each mark covers.")
    tooltip(dppct, "Darker than the skin to the right, lighter to the left - pale freckles, old pocks. Measured against the "
            "face's own level, so darkening the skin keeps them. At 0 nothing shows.")
    blotlab = ttk.Label(t_speck, text="Click a cheek to put the speckles there.", foreground="#666")
    blotlab.pack(anchor="w")
    tooltip(arpct, "How wide a patch each mark speckles. With no marks the speckles go across the bridge "
                   "of the nose and both cheeks, where freckles are; click the picture to put them "
                   "somewhere of your own, and the green ring shows what each click covers.")
    tooltip(spkpct, "Freckles, a gith's markings, a pox-scarred face. Mixed from the skin's own color a few shades down, "
            "across the nose and cheeks, or around your marks. The engine will not paint these: at portrait size a "
            "freckle is two pixels.")
    tooltip(cpct, "How much of the face counts as skin, for Skin and for Lips. Low leaves patches behind; "
                  "high can start taking the background with it. Hair, cloth and metal are left alone "
                  "either way.")
    lgrid = ttk.Frame(t_lips)
    lgrid.pack(fill="x")
    for i, nm_ in enumerate(lipset):
        ttk.Radiobutton(lgrid, text=nm_, value=nm_, variable=lips_v,
                        command=lambda: (remember(), draw())).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 8))
    lpct = slider(t_lips, "Strength", lipamt, 0.1, 1.0)
    def header(parent: tk.Misc, text: str) -> None:
        """A section, named."""
        ttk.Label(parent, text=text, font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(6, 0))
    header(t_spot, "Spot color")
    eyelab = ttk.Label(t_spot, text="Click an eye, a gem, a buckle...", foreground="#666")
    eyelab.pack(anchor="w")
    tooltip(eyelab, "Click an eye, a gem, a buckle, then set the color. Only what matches the sampled spot moves, so the ring"
            " can be loose. Eight at a time; right-click to clear.")
    egrid = ttk.Frame(t_spot)
    egrid.pack(fill="x")
    for i, nm_ in enumerate(eyeset):
        ttk.Radiobutton(egrid, text=nm_, value=nm_, variable=eyes_v,
                        command=lambda: (remember(), draw())).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 8))
    epct = slider(t_spot, "Strength", eyeamt, 0.1, 1.0)
    slider(t_spot, "Size", eyesize, 0.015, 0.08)

    lrow = ttk.Frame(t_spot)                               # the same marks, lit instead of recolored
    lrow.pack(fill="x", pady=(6, 0))
    ttk.Label(lrow, text="Light it", font=("TkDefaultFont", 8, "bold"), foreground="#555").pack(side="left")
    for nm_ in ("None", "Glow", "Flame"):
        ttk.Radiobutton(lrow, text=nm_, value=nm_, variable=glow_v,
                        command=lambda: (remember(), draw())).pack(side="left", padx=(8, 0))
    lit_ = ttk.Frame(t_spot)
    lit_.pack(fill="x", padx=(10, 0))
    ggrid = ttk.Frame(lit_)
    ggrid.pack(fill="x")
    for i, nm_ in enumerate(glows):
        ttk.Radiobutton(ggrid, text=nm_, value=nm_, variable=glowclr,
                        command=lambda: (remember(), draw())).grid(row=i // 4, column=i % 4, sticky="w",
                                                                   padx=(0, 8))
    glpct = slider(lit_, "Strength", glowamt, 0.1, 1.0)
    gspct = slider(lit_, "Size", glowsize, 0.01, 0.09)
    grpct = slider(lit_, "Round", glowround, 0.15, 1.0)
    gppct = slider(lit_, "Spill", glowspill, 0.0, 1.0)
    tooltip(grpct, "The shape of the light. 1.00 is a circle; lower makes it an eye - a wide almond, "
                   "pointed at the corners - which is what a burning eye looks like and what a round "
                   "glow never does. Drop it further for a slit.")
    tooltip(lrow, "Light coming off the marks above - eyes, a gem, a brazier, the head of a staff. Glow "
                  "is a steady light, Flame is tongues of it rising and flickering. The engine will not "
                  "paint a lit eye however it is asked, so the light is put on here.")
    tooltip(ggrid, "The color of the light. Shadow is the other way round - the marks become pits of "
                   "dark instead, which is what an undead thing has where its eyes were.")
    tooltip(gspct, "How big the hot middle is. An eye is about six pixels across in a portrait this "
                   "size, so small is usually right; the light around it does the work.")
    tooltip(gppct, "How far the light falls on everything else - the brow, the cheekbone, the bridge of "
                   "the nose. This is what makes a glow look lit rather than stuck on. At 0 it lights "
                   "nothing but itself.")

    def clear_eyes() -> None:
        remember()
        erases.clear()
        spots.clear()
        shapes.clear()
        blots.clear()
        eyes_v.set("Leave")
        glow_v.set("None")
        warp.set(0.0)
        draw()
    erow_ = ttk.Frame(side)
    erow_.pack(fill="x", pady=(2, 0))
    clr_ = ttk.Button(erow_, text="Clear marks", width=11, command=clear_eyes, state="disabled")
    clr_.pack(side="left")
    tooltip(clr_, "Forget every mark and put what they did back to nothing, so the picture is as it was "
                  "painted. Right-clicking the picture does the same.")
    ttk.Label(erow_, text="Clicks land on the tab you are on.", foreground="#888").pack(side="right")
    mk_ = ttk.Checkbutton(erow_, text="Show marks", variable=marks_v, command=draw)
    mk_.pack(side="left", padx=(8, 0))
    erow2_ = ttk.Frame(side)
    erow2_.pack(fill="x", pady=(2, 0))
    er_ = ttk.Checkbutton(erow2_, text="Erase", variable=erase_on, command=draw)
    er_.pack(side="left")
    tooltip(er_, "Ticked: clicking or dragging on the picture rubs out what this pass did there - speckles "
                 "that strayed off the face, a bit of dirt in the hair. Untick to place marks again. Undo takes "
                 "back a stroke; Paint these in keeps the result.")
    ttk.Label(erow2_, text="Size", width=5).pack(side="left", padx=(8, 0))
    es_ = ttk.Scale(erow2_, from_=0.008, to=0.08, variable=erase_size)
    es_.pack(side="left", fill="x", expand=True, padx=(4, 0))
    tooltip(es_, "How big the eraser is.")
    tooltip(mk_, "The yellow rings are only there to show where you clicked. Turn them off to see the "
                 "picture as it will be - the color stays either way.")
    shapelab = ttk.Label(t_shape, text="Click a nose, a jaw, an eye - tick 'clicks mark these' first.",
                         foreground="#666")
    shapelab.pack(anchor="w")
    push = group(t_shape, "Push it out of shape")
    wpct = slider(push, "Swell", warp, -1.0, 1.0)
    stpct = slider(push, "Stretch", stretch, -1.0, 1.0)
    rch = slider(push, "Spread", warpsize, 0.05, 0.40)
    tooltip(stpct, "Pull a marked spot away from the middle, tapered: an ear drawn to an elven point, a horn pulled up. Left"
            " pulls it back in. Both ears go opposite ways from one setting.")
    tooltip(rch, "How wide an area Swell pushes. Small moves a nostril, large reshapes a whole jaw.")
    crow = ttk.Frame(t_shape)                               # the kind of cut, then the settings it takes
    crow.pack(fill="x", pady=(6, 0))
    ttk.Label(crow, text="Cut it", font=("TkDefaultFont", 8, "bold"), foreground="#555").pack(side="left")
    for nm_ in ("None", "Scar", "Wound"):
        ttk.Radiobutton(crow, text=nm_, value=nm_, variable=cut_v,
                        command=lambda: (remember(), draw())).pack(side="left", padx=(8, 0))
    cut_ = ttk.Frame(t_shape)
    cut_.pack(fill="x", padx=(10, 0))
    cpct3 = slider(cut_, "Strength", cutamt, 0.1, 1.0)
    lgpct = slider(cut_, "Length", scarlen, 0.05, 0.40)
    lnpct = slider(cut_, "Width", linew, 0.4, 2.5)
    agpct = slider(cut_, "Angle", angle_v, -90.0, 90.0)
    rdpct = slider(cut_, "Ridge", ridge, 0.0, 1.0)
    edpct = slider(cut_, "Edge", edge_v, 0.0, 1.0)
    tooltip(lgpct, "How far the cut runs across the face: a nick beside the eye at the low end, brow "
                   "to jaw at the high.")
    tooltip(agpct, "Which way the line runs: 0 is straight across the face, negative tilts the near end "
                   "down, and either edge of the slider stands it upright. Marks in one pass vary by a "
                   "few degrees so they are not stamped out; paint one in before setting the next.")
    tooltip(edpct, "A dark line drawn round the outside of a scar or a wound, the way the shipped "
                   "portraits draw round every feature. It is what makes a wound sit in the face "
                   "instead of on top of it.")
    tooltip(lnpct, "How thick the line of a scar or a wound is. Length sets how far it runs; this sets "
                   "how heavy it is - a razor cut at the low end, a sword's work at the high.")
    bgrid = ttk.Frame(cut_)
    bgrid.pack(fill="x", pady=(2, 0))
    bbtn: List[ttk.Radiobutton] = []                     # only a wound bleeds, so these gray out for a scar
    for i, nm_ in enumerate(bloods):
        rb_ = ttk.Radiobutton(bgrid, text=nm_, value=nm_, variable=blood_v,
                              command=lambda: (remember(), draw()))
        rb_.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 8))
        bbtn.append(rb_)
    tooltip(bgrid, "What runs out of a wound. Blood for anything that has it; ichor for the things that "
                   "do not - a golem, a spectre, a mind flayer. It sets the cut, its raw edge and the "
                   "bruise around it together.")
    tooltip(rdpct, "How much light the edge catches, so a scar reads as a raised ridge rather than a "
                   "smudge, and a wound looks wet. At 0 the scar is only a groove.")
    tooltip(crow, "What a marked spot becomes. A scar is mixed from the skin's own color: a groove, a ridge, the light it "
            "catches. A wound is raw and bleeding, the skin around it bruised.")
    tooltip(cpct3, "How deep the cut is. Low is an old nick, high is a wound that has only just closed.")
    tooltip(wpct, "Swell what is under the mark to the right, pinch it to the left: a broader nose, a heavier jaw, smaller "
            "eyes. The engine paints everybody good-looking whatever it is told, so the shape is changed here "
            "instead.")
    fs_ = ttk.Frame(t_pic)
    fs_.pack(fill="x")
    fpct = slider(fs_, "Ghostly", fade, 0.0, 1.5)
    cpct2 = slider(fs_, "Creases", creases, 0.0, 1.0)
    gpct2 = slider(fs_, "Grime", grime, 0.0, 1.0)
    t_dirt = group(t_pic, "Dirty it")
    sopct = slider(t_dirt, "Soot", soot_v, 0.0, 1.0)
    mupct = slider(t_dirt, "Mud", mud_v, 0.0, 1.0)
    sppct = slider(t_dirt, "Blood", spat_v, 0.0, 1.0)
    swpct = slider(t_dirt, "Sweat", sweat_v, 0.0, 1.0)
    dzpct = slider(t_dirt, "Size", dsize_v, 0.5, 2.0)
    tooltip(sopct, "Soot, wiped across the skin as if by a hand: a smith, a fire, a burnt village.")
    tooltip(mupct, "Caked mud in patches with broken edges.")
    tooltip(sppct, "Dried blood flecked over the face - someone else's, mostly.")
    tooltip(swpct, "Beads of sweat and a sheen where the light falls.")
    tooltip(dzpct, "How big the smudges and the spatter are. Applies to Soot, Mud and Blood.")
    t_pnt = group(t_paint, "Face paint")
    pgrid = ttk.Frame(t_pnt)
    pgrid.pack(fill="x")
    for i, nm_ in enumerate(pstyles):
        ttk.Radiobutton(pgrid, text=nm_, value=nm_, variable=paint_v,
                        command=lambda: (remember(), draw())).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 8))
    t_pcl = group(t_paint, "Color")
    cgrid_ = ttk.Frame(t_pcl)
    cgrid_.pack(fill="x")
    for i, nm_ in enumerate(paints):
        ttk.Radiobutton(cgrid_, text=nm_, value=nm_, variable=paintclr,
                        command=lambda: (remember(), draw())).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 8))
    papct = slider(t_paint, "Strength", paintamt, 0.1, 1.0)
    pzpct = slider(t_paint, "Size", paintsize, 0.5, 2.0)
    peyelab = ttk.Label(t_paint, text="", wraplength=260)
    peyelab.pack(anchor="w", pady=(6, 0))
    tooltip(papct, "How solid the paint is. Low lets the skin show through, like paint half worn off.")
    tooltip(pzpct, "How big the marks are: thicker stripes, a wider band, bigger dots.")
    fl_ = ttk.Checkbutton(fs_, text="Mirror the picture", variable=flip_v,
                          command=lambda: (remember(), draw()))
    fl_.pack(anchor="w", pady=(2, 0))
    tooltip(fpct, "Fade the picture into its background: solid at the brow, gone by the bottom, the eyes left burning. Past"
            " 100% the dissolve starts higher. The engine will not paint a see-through person, so it is done here.")
    tooltip(cpct2, "Lines where a face creases: the brow, the corners of the eyes, the folds beside the "
                   "nose and the jowls. Mark the two eyes as color spots and the crow's feet go where "
                   "they belong; otherwise they go where they usually are, since these portraits are all "
                   "framed alike.")
    tooltip(gpct2, "Dirt: soft smudges where sweat and soot gather, and a little grain over the whole "
                   "picture. The engine paints a peasant as clean as a prince whatever the description "
                   "says, so this is done to the picture afterwards.")
    tooltip(fl_, "Turn the picture left to right. The engine favors one three-quarter turn of the head, "
                 "so a roomful of its portraits all face the same way; mirroring some of them breaks "
                 "that up. Marks you place go where you see them.")

    def paint_in() -> None:
        """Settle this pass into the picture, so the next scar or wound can be set differently."""
        (hue, amt, sat, cat, lig, lh, lam, ls, eh, eam, es, esz, ev, fad, gri, wrp, wsz,
         cre, scr, wnd, flp, rdg, bld, lnw, agl, edg, sln, glw, gtn, gam, gsz, gsp, grd, stc, spk, spa, spd, spm) = values()
        if (not flp and amt <= 0 and lam <= 0 and eam <= 0 and fad <= 0 and gri <= 0 and cre <= 0
                and scr <= 0 and wnd <= 0 and gam <= 0 and spk <= 0 and abs(lig) < 0.01 and abs(wrp) < 0.01 and abs(stc) < 0.01
                and not extra_set()):
            say("Nothing set to paint in yet.")
            return
        try:
            cur = pg.flip_image(thumb) if flp else thumb     # type: ignore[attr-defined]
            cur = pg.stretch_image(cur, shapes, stc, wsz)    # type: ignore[attr-defined]
            cur = pg.warp_image(cur, shapes, wrp, wsz)       # type: ignore[attr-defined]
            if (scr > 0 or wnd > 0) and shapes:
                cur = pg.warp_image(cur, shapes, 0.20 * max(scr, wnd * 0.6),   # type: ignore[attr-defined]
                                    max(0.03, sln * 0.32))
            pre_ = cur
            cur = pg.touch_up(cur, hue, amt, sat, cat, lig, lh, lam, ls,   # type: ignore[attr-defined]
                              spots, eh, eam, es, esz, ev, gri, cre, scr, shapes, wnd, sln, rdg, bld, lnw, agl, edg, spk, blots, spa, spd, spm,
                                glw, gtn, gam, gsz, gsp, grd, **extra())
            cur = rub_out(pre_, cur)
            cur = pg.ghost_image(cur, fad)                   # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 - say so rather than losing the picture
            say(f"Could not paint that in ({e}).")
            return
        hist.append(dict(prev or snapshot()))                # one undo step, and it holds the picture too
        hist[-1]["base"] = thumb
        del hist[:-40]
        set_base(cur)
        keep["baked"] = True
        skin_v.set("Leave"), lips_v.set("Leave"), eyes_v.set("Leave")
        cut_v.set("None")
        glow_v.set("None")
        for v_, z_ in ((warp, 0.0), (stretch, 0.0), (fade, 0.0), (grime, 0.0), (creases, 0.0),
                       (light, 0.0), (soot_v, 0.0), (mud_v, 0.0), (spat_v, 0.0), (sweat_v, 0.0),
                       (speckle, 0.0)):                      # once in the picture, never laid on a second time
            v_.set(z_)
        erases.clear()
        paint_v.set("None")                                  # the eyes stay marked for the next paint
        flip_v.set(False)
        spots.clear()
        shapes.clear()
        blots.clear()
        prev.clear()
        draw()
        say("Painted in. Mark the next one - it can have its own width, color and angle.")

    def apply_() -> None:
        (hue, amt, sat, cat, lig, lh, lam, ls, eh, eam, es, esz, ev, fad, gri, wrp, wsz,
         cre, scr, wnd, flp, rdg, bld, lnw, agl, edg, sln, glw, gtn, gam, gsz, gsp, grd, stc, spk, spa, spd, spm) = values()
        if (not keep.get("baked") and not flp and amt <= 0 and lam <= 0 and eam <= 0 and fad <= 0
                and gri <= 0 and cre <= 0 and scr <= 0 and wnd <= 0 and gam <= 0 and spk <= 0 and abs(lig) < 0.01 and abs(wrp) < 0.01 and abs(stc) < 0.01
                and not extra_set()):
            win.destroy()
            return
        touch_memory[str(q)] = {"paint": paint_v.get(), "paintclr": paintclr.get(), "peyes": list(peyes),
                                **{k_: float(v_.get()) for v_, k_ in DIRT_VARS},
                                "skin": skin_v.get(), "lips": lips_v.get(), "eyes": eyes_v.get(),
                                "amount": float(amount.get()), "lipamt": float(lipamt.get()),
                                "eyeamt": float(eyeamt.get()), "eyesize": float(eyesize.get()),
                                "light": float(light.get()), "catch": float(catch.get()),
                                "fade": float(fade.get()), "grime": float(grime.get()),
                                "warp": float(warp.get()), "warpsize": float(warpsize.get()),
                                "creases": float(creases.get()), "speckles": float(speckle.get()),
                                "cut": cut_v.get(),
                                "cutamt": float(cutamt.get()), "flip": bool(flip_v.get()),
                                "ridge": float(ridge.get()), "line": float(linew.get()),
                                "angle": float(angle_v.get()), "edge": float(edge_v.get()),
                                "scarlen": float(scarlen.get()), "stretch": float(stretch.get()),
                                "glow": glow_v.get(),
                                "glowtint": glowclr.get(), "glowamt": float(glowamt.get()),
                                "glowsize": float(glowsize.get()), "glowspill": float(glowspill.get()),
                                "glowround": float(glowround.get()),
                                "blood": blood_v.get(),
                                "spots": list(spots), "shapes": list(shapes), "blots": list(blots),
                                "spkarea": float(spkarea.get()), "spkdeep": float(spkdeep.get()),
                                "spkamt": float(spkamt.get())}
        try:
            as_bmp = q.suffix.lower() == ".bmp"          # already the shape and size of a game portrait:
            new_ = str(q.with_name(q.stem + "_touch" + (".bmp" if as_bmp else ".png")))   # it stays one,
            start_bytes = thumb                          # so nothing is cut out of it a second time
            if flp:
                start_bytes = pg.flip_image(start_bytes)     # type: ignore[attr-defined]
            was_ = pg.stretch_image(start_bytes, shapes, stc, wsz)   # type: ignore[attr-defined]
            was_ = pg.warp_image(was_, shapes, wrp, wsz)      # type: ignore[attr-defined]
            if (scr > 0 or wnd > 0) and shapes:
                was_ = pg.warp_image(was_, shapes, 0.20 * max(scr, wnd * 0.6),   # type: ignore[attr-defined]
                                     max(0.03, sln * 0.32))
            out_ = pg.touch_up(was_, hue, amt, sat, cat, lig, lh, lam, ls,   # type: ignore[attr-defined]
                               spots, eh, eam, es, esz, ev, gri, cre, scr, shapes, wnd, sln, rdg, bld, lnw, agl, edg, spk, blots, spa, spd, spm,
                                glw, gtn, gam, gsz, gsp, grd, **extra())
            out_ = rub_out(was_, out_)
            done_ = pg.ghost_image(out_, fad)                # type: ignore[attr-defined]
            if as_bmp:                                       # back to a 24-bit BMP the game can read
                tmp_ = q.with_name(q.stem + "_touch_tmp.png")
                tmp_.write_bytes(done_)
                try:
                    to_portrait_bmp(str(tmp_), new_, src.width, src.height, None,
                                    (0, 0, src.width, src.height))
                finally:
                    try:
                        tmp_.unlink()
                    except OSError:
                        pass
            else:
                Path(new_).write_bytes(done_)
            gs["skin_catch"] = cat
            try:
                pg.save_patch(gs, {"skin_catch": cat})       # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the setting is not worth an error
                pass
        except OSError as e:
            say(f"Could not touch up {q.name} ({e}).")
            win.destroy()
            return
        win.destroy()
        # the plain picture stays: Plain has something to go back to, and the next touch up starts from
        # the plain one rather than piling onto this one
        on_done(path, new_, "")
        if change_it is not None and then_.get("change"):
            change_it(new_)
            return
        said = [f"{skin_v.get().lower()} skin" if amt > 0 else "",
                f"{lips_v.get().lower()} lips" if lam > 0 else "",
                f"{eyes_v.get().lower()} on {len(spots)} spot(s)" if eam > 0 else "",
                f"faded to {int(fad * 100)}%" if fad > 0 else "",
                f"{int(gri * 100)}% grime" if gri > 0 else "",
                ", ".join(f"{int(float(v_.get()) * 100)}% {k_}" for v_, k_ in DIRT_VARS[:4] if float(v_.get()) > 0),
                f"{paintclr.get().lower()} {paint_v.get().lower()}" if paint_v.get() != "None" and len(peyes) >= 2 else "",
                f"{int(cre * 100)}% creases" if cre > 0 else "",
                f"{len(shapes)} scar(s)" if scr > 0 and shapes else "",
                f"{len(shapes)} wound(s)" if wnd > 0 and shapes else "",
                "mirrored" if flp else "",
                ("swollen" if wrp > 0 else "pinched") if abs(wrp) >= 0.01 else "",
                ("lighter" if lig > 0 else "darker") if abs(lig) >= 0.01 else ""]
        say(", ".join(x for x in said if x).capitalize()
                  + ". Press Plain under it to put the picture back."
                  + (" It is already portrait-sized, so Propose under it makes it the one this creature "
                     "wears - nothing is cut out of it again." if as_bmp and not generic else ""))
    def reset_() -> None:
        """Every slider back where it started: nothing changed, the color the creature's own words ask for."""
        remember()
        skin_v.set("Leave")
        lips_v.set("Leave")
        eyes_v.set("Leave")
        spots.clear()
        amount.set(0.9)
        lipamt.set(0.85)
        eyeamt.set(0.95)
        eyesize.set(0.035)
        fade.set(0.0)
        grime.set(0.0)
        creases.set(0.0)
        speckle.set(0.0)
        spkarea.set(0.16)
        spkdeep.set(0.6)
        spkamt.set(0.75)
        blots.clear()
        cut_v.set("None")
        cutamt.set(0.85)
        flip_v.set(False)
        warp.set(0.0)
        warpsize.set(0.17)
        ridge.set(0.5)
        linew.set(1.0)
        angle_v.set(-55.0)
        edge_v.set(0.5)
        scarlen.set(0.17)
        stretch.set(0.0)
        glow_v.set("None")
        glowamt.set(0.85)
        glowsize.set(0.028)
        glowspill.set(0.6)
        glowround.set(0.40)
        blood_v.set(next(iter(bloods), "Blood"))
        for v_, z_ in zip((soot_v, mud_v, spat_v, sweat_v, dsize_v, paintamt, paintsize), (0, 0, 0, 0, 1.0, 0.8, 1.0)):
            v_.set(z_)
        paint_v.set("None")
        peyes.clear()
        erases.clear()
        shapes.clear()
        light.set(0.0)
        catch.set(float(gs.get("skin_catch", 0.6)))
        draw()
    brow_ = ttk.Frame(win)
    brow_.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(brow_, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(brow_, text="Use this", command=apply_).pack(side="right", padx=(0, 6))
    rb_ = ttk.Button(brow_, text="Reset", command=reset_)
    rb_.pack(side="left")
    undo_b = ttk.Button(brow_, text="Undo", command=undo, state="normal" if hist else "disabled")
    undo_b.pack(side="left", padx=(6, 0))
    bake_b = ttk.Button(brow_, text="Paint these in", command=lambda: paint_in())
    bake_b.pack(side="left", padx=(6, 0))
    tooltip(bake_b, "Settle what is set now into the picture and start again on top, marks cleared. One scar at a time: paint"
            " one in, then give the next its own width, color and angle. Undo takes a pass back off.")
    keep["undo"] = undo_b
    tooltip(undo_b, "Take back the last thing you did here - a slider moved, a color chosen, a mark "
                    "placed. Ctrl+Z does the same. Reset puts everything back at once.")
    win.bind("<Control-z>", lambda e: undo())
    scan.bind("<MouseWheel>", side_wheel)
    wheel_on(side)
    side_fits()
    tooltip(rb_, "Put every slider back where it started, without closing this window.")
    draw()
    win.bind("<Return>", lambda e: apply_())
    esc_closes(win)
    win.update_idletasks()
    try:
        x = dlg.winfo_rootx() + max(0, (dlg.winfo_width() - win.winfo_width()) // 2)
        y = dlg.winfo_rooty() + max(0, (dlg.winfo_height() - win.winfo_height()) // 4)
        win.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass
    win.grab_set()


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    st = load_settings()
    root = tk.Tk()
    root.title(f"Portrait Scout {VERSION}")
    root.geometry(f"1100x{min(820, max(600, root.winfo_screenheight() - 80))}")   # room for a row of candidates

    PANEL, CARD = "#d8dce1", "#f2f3f6"            # table / gallery background, and the gallery cards on it
    sty = ttk.Style(root)
    sty.configure("Treeview", background=PANEL, fieldbackground=PANEL, rowheight=22)
    sty.map("Treeview", background=[("selected", "#3a78b5")], foreground=[("selected", "white")])
    here = os.getcwd()
    # start from the current folder when it is a game folder (run it from the game folder), else the last one used
    game_var = tk.StringVar(value=here if (Path(here) / "chitin.key").exists() else (st.get("game") or ""))
    mod_var = tk.StringVar(value=st.get("mod", ""))
    modpic_var = tk.BooleanVar(value=bool(st.get("modpic_only", False)))
    status = tk.StringVar(value="Pick the game folder and the mod folder, then press Analyze. "
                          "(Leave the mod folder empty, or pick the game folder itself, to list every creature in the install.)")
    state: Dict[str, object] = {"res": None, "sort": ("lines", True), "imgs": {}, "page": 0}

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)                                  # game and mod folder share a row,
    top.columnconfigure(4, weight=1)                                  # each path box taking half the width

    def browse(var: tk.StringVar, title: str, start: str) -> None:
        d = filedialog.askdirectory(title=title, initialdir=start if start and os.path.isdir(start) else os.getcwd())
        if d:
            var.set(os.path.normpath(d))

    ttk.Label(top, text="Game folder:").grid(row=0, column=0, sticky="w")
    ttk.Entry(top, textvariable=game_var, width=34).grid(row=0, column=1, sticky="ew", padx=6)
    def path_box(var: tk.StringVar, title: str) -> None:
        """The whole path, in a box big enough to read it."""
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title(title)
        w.geometry("760x150")
        t = tk.Text(w, wrap="char", height=3, font=("TkDefaultFont", 10))
        t.pack(fill="both", expand=True, padx=8, pady=8)
        t.insert("1.0", var.get())
        t.focus_set()
        rw = ttk.Frame(w, padding=(8, 0, 8, 8))
        rw.pack(fill="x")

        def ok() -> None:
            var.set(" ".join(t.get("1.0", "end-1c").split()))
            w.destroy()
        ttk.Button(rw, text="OK", command=ok).pack(side="right")
        ttk.Button(rw, text="Cancel", command=w.destroy).pack(side="right", padx=6)
    gb_ = ttk.Frame(top)
    gb_.grid(row=0, column=2, sticky="w")
    ttk.Button(gb_, text="Browse...", command=lambda: browse(game_var, "Game folder (has chitin.key)",
                                                             game_var.get())).pack(side="left")
    ttk.Button(gb_, text="\u2922", width=3, command=lambda: path_box(game_var, "Game folder")).pack(side="left", padx=(3, 0))
    ttk.Label(top, text="Mod folder:").grid(row=0, column=3, sticky="w", padx=(14, 0))
    ttk.Entry(top, textvariable=mod_var, width=34).grid(row=0, column=4, sticky="ew", padx=6)
    mb_ = ttk.Frame(top)
    mb_.grid(row=0, column=5, sticky="w")
    ttk.Button(mb_, text="Browse...", command=lambda: browse(mod_var, "Mod folder (has the .tp2 / .tra / .d files)",
                                                             mod_var.get() or game_var.get())).pack(side="left")
    ttk.Button(mb_, text="\u2922", width=3, command=lambda: path_box(mod_var, "Mod folder")).pack(side="left", padx=(3, 0))

    def mod_bmp_index() -> Dict[str, str]:
        """NAME -> the mod folder's own .bmp, and CRE NAME -> the portraits the mod's own creature file asks for.
        That second map matters when another mod (HPS, say) has since rewritten the installed creature."""
        key = mod_var.get()
        if state.get("mod_bmp_dir") != key:
            idx: Dict[str, str] = {}
            cre_ports: Dict[str, Tuple[str, str]] = {}
            if key and os.path.isdir(key):
                for root_, dirs, names in os.walk(key):
                    dirs[:] = [d for d in dirs if d.lower() not in ("tutu", "bgt", "backup")]
                    for n_ in names:
                        low = n_.lower()
                        if low.endswith(".bmp"):
                            idx.setdefault(os.path.splitext(n_)[0].upper(), os.path.join(root_, n_))
                        elif low.endswith(".cre"):
                            try:
                                with open(os.path.join(root_, n_), "rb") as f:
                                    b = f.read(0x44)
                            except OSError:
                                continue
                            if len(b) >= 0x44:
                                cre_ports.setdefault(os.path.splitext(n_)[0].upper(), (_r8(b, 0x34), _r8(b, 0x3C)))
            state["mod_bmps"], state["mod_cre_ports"], state["mod_bmp_dir"] = idx, cre_ports, key
        return state["mod_bmps"]                                     # type: ignore[return-value]

    def bmp_file_dims(path: str) -> str:
        try:
            with open(path, "rb") as f:
                head = f.read(26)
            if head[:2] == b"BM" and len(head) >= 26:
                w, h = struct.unpack_from("<ii", head, 18)
                return f"{abs(w)}x{abs(h)}"
        except OSError:
            pass
        return ""

    def modpic_changed() -> None:
        st["modpic_only"] = modpic_var.get()
        save_settings(st)
        if state["res"]:
            refresh()
    pic_tip = ( "Gallery only, and only with a mod folder chosen.\n\nOff: each NPC as it looks in the game now, "
            "which includes pictures another mod or the installer replaced.\nOn: only the .bmp files the mod folder "
            "itself holds; an NPC the mod ships no picture for shows none.")
    btns = ttk.Frame(top)
    btns.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(8, 0))
    go_btn = tk.Button(btns, text="Analyze", bg="#2e9e4f", fg="white", activebackground="#25803f",
                       activeforeground="white", font=("TkDefaultFont", 10, "bold"), padx=14, pady=3, relief="raised")
    go_btn.pack(side="left")
    view_var = tk.StringVar(value="gallery")                     # the portraits are what this is for
    sty.configure("View.Toolbutton", padding=(12, 4))
    ttk.Radiobutton(btns, text="Gallery", value="gallery", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(16, 0))
    auto_mode_var = tk.BooleanVar(value=st.get("gallery_mode", "plain") != "plain")

    def auto_mode_changed() -> None:
        st["gallery_mode"] = "auto" if auto_mode_var.get() else "plain"
        save_settings(st)
        view_var.set("gallery")                                      # ticking it shows the gallery either way
        show_view()
    am_cb = ttk.Checkbutton(btns, text="Expanded", variable=auto_mode_var, command=auto_mode_changed)
    am_cb.pack(side="left", padx=(2, 6))
    tooltip(am_cb, "Expanded: each creature's card opened out, with its new pictures to pick from, the crop and "
                   "the batch tools (Generate for every creature listed). Off: the usual cards. Off at first; remembered.")
    ttk.Radiobutton(btns, text="Table", value="table", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(2, 0))
    gen_tab_btn = ttk.Radiobutton(btns, text="Propose", value="generate", variable=view_var, style="View.Toolbutton",
                                  command=lambda: show_view())
    gen_tab_btn.pack(side="left", padx=(2, 0))
    ttk.Radiobutton(btns, text="Pool", value="pool", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left")
    ttk.Radiobutton(btns, text="Proposals", value="export", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(2, 0))
    search_var = tk.StringVar(value="")
    ttk.Label(btns, text="Search:").pack(side="left", padx=(22, 4))
    search_entry = ttk.Entry(btns, textvariable=search_var, width=24)
    search_entry.pack(side="left")
    search_clear = ttk.Button(btns, text="x", width=2, command=lambda: search_var.set(""))
    search_clear.pack(side="left", padx=(2, 0))
    count_var = tk.StringVar(value="")
    ttk.Label(btns, textvariable=count_var, foreground="#555").pack(side="left", padx=8)
    loading_var = tk.StringVar(value="")
    ttk.Label(btns, textvariable=loading_var, foreground="#b45309",
              font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=14)
    setup_btn = ttk.Button(btns, text="Generator setup")                # the steps to get an image engine
    setup_btn.pack(side="right")

    fbar = ttk.Frame(root, padding=(8, 0, 8, 0))                     # what is listed: the same for Gallery, Table
    fbar.pack(fill="x")                                              # and Proposals, so it sits above all three
    content = ttk.Frame(root)
    content.pack(fill="both", expand=True, padx=8, pady=4)
    tab_table, tab_gal = ttk.Frame(content), ttk.Frame(content)
    tab_gen, tab_exp = ttk.Frame(content), ttk.Frame(content)
    tab_pool = ttk.Frame(content)                                    # pictures made and not used
    tab_auto = ttk.Frame(content)                                    # a batch for every creature listed

    cols = [k for k, _ in COLUMNS]
    tmid = ttk.Frame(tab_table)                                      # the table is wider than most windows, so it
    tree = ttk.Treeview(tmid, columns=cols + ["psize", "proposed"], show="headings", selectmode="browse")
    vs = ttk.Scrollbar(tmid, orient="vertical", command=tree.yview)
    hs = ttk.Scrollbar(tab_table, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)   # always there: hiding it on resize never came back
    hs.pack(side="bottom", fill="x")                                 # both scrollbars claim their strip before the
    tmid.pack(side="top", fill="both", expand=True)                  # table expands into what is left of the cavity
    vs.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    widths = {"name": 260, "lines": 70, "portrait": 220, "race": 110, "sex": 80, "anim": 220, "cre": 200, "area": 190, "proposed": 190, "psize": 150}
    for k, h in COLUMNS:
        tree.heading(k, text=h, command=lambda c=k: sort_by(c))
        # stretch=False: columns keep the width they are given. Left to themselves they share out the window and
        # squeeze back below it, so "Lines" ends up as "Li" and the scrollbar has nothing left to scroll.
        tree.column(k, width=widths[k], minwidth=widths[k], stretch=False, anchor="e" if k == "lines" else "w")
    tree.heading("psize", text="Portrait size (L / M)")
    tree.column("psize", width=widths["psize"], minwidth=widths["psize"], stretch=False, anchor="w")
    tree.heading("proposed", text="Proposed portrait")
    tree.column("proposed", width=widths["proposed"], minwidth=widths["proposed"], stretch=False, anchor="w")

    SORTS = [("Lines, most first", ("lines", True)), ("Lines, fewest first", ("lines", False)),
             ("Name, A to Z", ("name", False)), ("Name, Z to A", ("name", True)),
             ("Portrait", ("portrait", False)), ("Area", ("area", False)),
             ("Borrowed face", ("borrowed", False))]

    props: Dict[str, str] = {}                      # proposed portrait image per creature (key = its dialogs)
    skip: set = set()                               # creatures marked as not wanting a portrait, same key
    kept: Dict[str, bool] = {}                      # borrowed faces judged by hand: keep it, or it wants its own
    prop_label = tk.StringVar(value="Proposed portraits: 0")
    prop_where = tk.StringVar(value="")
    gbar = ttk.Frame(tab_gal, padding=(0, 0, 0, 4))
    gbar.pack(side="top", fill="x")
    ttk.Label(gbar, textvariable=prop_label, foreground="#555").pack(side="left", padx=(0, 10))
    pagebar = tk.Frame(gbar, bg=gbar.winfo_toplevel().cget("bg"), highlightthickness=0)
    pagebar.pack(side="left", padx=10)
    ttk.Label(gbar, text="Sort:").pack(side="left", padx=(10, 4))
    sort_var = tk.StringVar(value=SORTS[0][0])
    sort_box = ttk.Combobox(gbar, textvariable=sort_var, values=[lbl for lbl, _k in SORTS], state="readonly", width=18)
    sort_box.pack(side="left")
    tooltip(sort_box, "Borrowed face: creatures wearing a face copied from another creature come first, "
            "grouped by that face, most lines first.")
    show_skip_var = tk.BooleanVar(value=False)
    party_var = tk.BooleanVar(value=bool(st.get("party_npcs", True)))
    party_cb = ttk.Checkbutton(fbar, text="Party NPCs", variable=party_var)
    skip_cb = ttk.Checkbutton(fbar, text="Ignored", variable=show_skip_var)
    tooltip(skip_cb, "Creatures marked with 'Ignore this creature' on the right-click menu.\n\nOff: left out of the table, the"
            " gallery and the proposals.\nOn: listed with everybody else, marked (ignored), so you can let one back "
            "in.")
    tooltip(party_cb, "NPCs who can join the party, from pdialog.2da and interdia.2da.\n\nOn: listed like everybody else.\nOff:"
            " left out everywhere - they nearly always have a portrait already.\n\nA creature sharing a script name "
            "or dialog with one can be caught too.")

    pick_var = tk.BooleanVar(value=False)                            # gallery select mode: a box on every card
    gbar2 = ttk.Frame(tab_gal, padding=(0, 0, 0, 4))                 # its own row: the toolbar above is full
    pick_bar = ttk.Frame(gbar2)
    pick_cb = ttk.Checkbutton(gbar, text="Select", variable=pick_var)
    pick_cb.pack(side="left", padx=(12, 0))
    tooltip(pick_cb, "Put a tick box on every card, so a whole set of creatures can be dealt with at once.")
    pick_count = tk.StringVar(value="")

    # narrow the gallery to the creatures worth looking at next
    CHIPS: List[Tuple[str, Callable[[dict], bool]]] = [
        ("No portrait", lambda r: not (r.get("large") or r.get("small")) and rowkey(r) not in props),
        ("Proposed", lambda r: rowkey(r) in props or (bool(r.get("copy_port")) and face_kept(r))),
        #   a copy keeping the face it was copied with has its portrait settled too: it is listed with them
        ("Missing", lambda r: bool(props.get(rowkey(r))) and not os.path.isfile(str(props.get(rowkey(r))))),
        ("Borrowed", lambda r: bool(r.get("copy_port"))),
        ("Face kept", lambda r: face_kept(r)),
        ("10+ lines", lambda r: int(r.get("lines", 0) or 0) >= 10),
    ]
    chip_vars: Dict[str, tk.BooleanVar] = {}
    race_pick = tk.StringVar(value="All")
    keep_vars: List[tk.BooleanVar] = []             # the cards' keep-this-face ticks, while they are on screen
    ttk.Label(fbar, text="Also show:", foreground="#666").pack(side="left")
    party_cb.pack(side="left", padx=(6, 0))
    skip_cb.pack(side="left", padx=(10, 0))
    ttk.Label(fbar, text="Show only:", foreground="#666").pack(side="left", padx=(24, 0))
    for _lbl, _fn in CHIPS:
        chip_vars[_lbl] = tk.BooleanVar(value=False)
        _c = ttk.Checkbutton(fbar, text=_lbl, variable=chip_vars[_lbl], command=lambda: chip_changed())
        _c.pack(side="left", padx=(10, 0))
        tooltip(_c, "Show only creatures this is true of. Tick as many as you like; a creature has to match "
                    "all of them.")
    pic_chk = ttk.Checkbutton(fbar, text="Mod's portraits only", variable=modpic_var, command=modpic_changed)
    pic_chk.pack(side="left", padx=(24, 0))
    tooltip(pic_chk, pic_tip)
    ttk.Label(fbar, text="Race:").pack(side="left", padx=(24, 4))

    def race_list() -> None:
        seen_: Dict[str, int] = {}
        for o in (all_rows() if state.get("res") else []):
            k_ = str(o.get("race") or "").strip()
            if k_:
                seen_[k_] = seen_.get(k_, 0) + 1
        race_box.config(values=["All"] + [f for f in sorted(seen_, key=str.lower)])
    race_box = ttk.Combobox(fbar, textvariable=race_pick, width=14, state="readonly", postcommand=race_list)
    race_box.pack(side="left")
    race_box.bind("<<ComboboxSelected>>", lambda e: chip_changed())
    tooltip(race_box, "Show only creatures of one race (from their creature file), in the gallery and the "
                      "Expanded view.")

    def chip_changed(*_a: object) -> None:
        state["page"] = 0
        auto_ui["page"] = 0                                          # the Expanded view's pages too
        refresh()
        if view_var.get() == "export":
            fill_export()

    ebar = ttk.Frame(tab_exp, padding=(0, 0, 0, 6))                  # the Proposals tab: what you have chosen so far
    ebar.pack(side="top", fill="x")
    sty.configure("Export.TButton", font=("TkDefaultFont", 9, "bold"))
    export_btn = ttk.Button(ebar, text="Save to-do list...", style="Export.TButton")
    export_btn.pack(side="left")
    list_btn = ttk.Button(ebar, text="Save list...", state="disabled")
    list_btn.pack(side="left", padx=(6, 0))
    tooltip(list_btn, "Save the list of creatures as shown in Gallery and Table, as text or as a spreadsheet "
                      "(.csv) - pick the type in the save window. Analyze also saves it as text next to the program.")
    mod_btn = ttk.Button(ebar, text="Create mod...")
    mod_btn.pack(side="left", padx=(6, 0))
    prev_btn = ttk.Button(ebar, text="Preview page...")
    prev_btn.pack(side="left", padx=(6, 0))
    tooltip(prev_btn, "Save a web page of the proposed portraits - each with its name, race and gender - to look "
                      "the set over in a browser or share it. One file, the pictures inside it. Create mod... "
                      "puts one in the mod too (preview.html).")
    clear_btn = ttk.Button(ebar, text="Clear all proposals")
    clear_btn.pack(side="left", padx=6)
    ttk.Label(ebar, textvariable=prop_label, foreground="#555", font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=10)
    folder_btn = ttk.Button(ebar, text="Propose from folder...")
    folder_btn.pack(side="left", padx=(6, 0))
    hist_btn = ttk.Button(ebar, text="Saved sets...")
    hist_btn.pack(side="left", padx=(6, 0))
    share_btn = ttk.Button(ebar, text="Export work...")
    share_btn.pack(side="left", padx=(6, 0))
    takein_btn = ttk.Button(ebar, text="Import work...")
    takein_btn.pack(side="left", padx=(6, 0))
    lost_btn = tk.Button(ebar, text="", bg="#b5452f", fg="white", activebackground="#8f3524",
                         activeforeground="white", relief="raised", bd=1)   # shown only when something is lost
    ttk.Label(ebar, textvariable=prop_where, foreground="#888").pack(side="left", padx=10)
    ecols = ("creature", "cre", "picture", "size", "areas")
    etree = ttk.Treeview(tab_exp, columns=ecols, show="headings", selectmode="browse")
    for c_, h_, w_ in (("creature", "Creature", 220), ("cre", "CRE file(s)", 170), ("picture", "Proposed picture", 300),
                       ("size", "Size", 90), ("areas", "Area(s)", 260)):
        etree.heading(c_, text=h_)
        etree.column(c_, width=w_, anchor="w")
    evs = ttk.Scrollbar(tab_exp, orient="vertical", command=etree.yview)
    etree.configure(yscrollcommand=evs.set)
    etree.pack(side="left", fill="both", expand=True)
    evs.pack(side="right", fill="y")
    gcanvas = tk.Canvas(tab_gal, highlightthickness=0, bg=PANEL)
    gv = ttk.Scrollbar(tab_gal, orient="vertical", command=gcanvas.yview)
    gcanvas.configure(yscrollcommand=gv.set)
    gh = ttk.Scrollbar(tab_gal, orient="horizontal", command=gcanvas.xview)
    gcanvas.configure(xscrollcommand=gh.set)
    gh.pack(side="bottom", fill="x")
    gv.pack(side="right", fill="y")
    gcanvas.pack(side="left", fill="both", expand=True)
    ginner = tk.Frame(gcanvas, bg=PANEL)
    gwin = gcanvas.create_window((0, 0), window=ginner, anchor="nw")
    def fix_scrollregion(_e: object = None) -> None:
        # exact size of the cards, so you can't scroll into empty space below them
        cw = max(1, gcanvas.winfo_width())
        width = max(cw, MIN_PER_ROW * CARD_W)                     # never narrower than 4 cards; scroll sideways instead
        gcanvas.itemconfigure(gwin, width=width)
        gcanvas.configure(scrollregion=(0, 0, width, max(1, ginner.winfo_reqheight())))
        gcanvas.yview_moveto(min(gcanvas.yview()[0], max(0.0, 1.0 - (gcanvas.winfo_height() / max(1, ginner.winfo_reqheight())))))

    ginner.bind("<Configure>", fix_scrollregion)
    CARD_W = 185                                   # width reserved for one gallery card; the row fills the window
    CAND_W, CAND_H = 210, 330                      # every candidate is shown in this box unless "Full size" is on
    THUMB_W, THUMB_H = 155, 244                    # a proposal is shown at the size a 169x266 portrait gets here
    MIN_PER_ROW = 4                                # fewer than this per row is not supported (layout breaks)

    def per_row_now() -> int:
        w = gcanvas.winfo_width()
        return max(MIN_PER_ROW, (w if w > 100 else 1000) // CARD_W)

    def on_gal_resize(e: object) -> None:
        fix_scrollregion()
        if view_var.get() == "gallery" and tab_gal.winfo_ismapped() and state["res"] and per_row_now() != state.get("per_row"):
            if state.get("resize_job"):
                root.after_cancel(state["resize_job"])                   # type: ignore[arg-type]
            state["resize_job"] = root.after(150, lambda: build_gallery(sorted_rows()))

    gcanvas.bind("<Configure>", on_gal_resize)
    def gal_target() -> object:
        """What the wheel and the keys scroll in the gallery: the cards, or the Auto view when that is showing."""
        cv_ = auto_ui.get("canvas")
        return cv_ if cv_ is not None and tab_auto.winfo_ismapped() else gcanvas
    root.bind("<MouseWheel>", lambda e: gal_target().yview_scroll(int(-e.delta / 120), "units")   # type: ignore[attr-defined]
              if view_var.get() == "gallery" else None)

    def gal_key(what: str, n: int = 1) -> None:
        if view_var.get() == "gallery":
            cv_ = gal_target()
            if what == "page":
                cv_.yview_scroll(n, "pages")                         # type: ignore[attr-defined]
            elif what == "home":
                cv_.yview_moveto(0)                                  # type: ignore[attr-defined]
            else:
                cv_.yview_moveto(1)                                  # type: ignore[attr-defined]

    def gal_arrow(e: object, n: int) -> None:
        if view_var.get() == "gallery" and e.widget.winfo_class() not in ("TEntry", "Entry"):   # type: ignore[attr-defined]
            gal_target().yview_scroll(n, "units")                    # type: ignore[attr-defined]

    root.bind("<Up>", lambda e: gal_arrow(e, -3))
    root.bind("<Down>", lambda e: gal_arrow(e, 3))
    root.bind("<Prior>", lambda e: gal_key("page", -1))
    root.bind("<Next>", lambda e: gal_key("page", 1))
    root.bind("<Control-Home>", lambda e: gal_key("home"))
    root.bind("<Control-End>", lambda e: gal_key("end"))
    for key_, what_ in (("<Left>", "left"), ("<Right>", "right"), ("<Up>", "up"), ("<Down>", "down"),
                        ("<space>", "space"), ("<Home>", "home"), ("<End>", "end"), ("<Prior>", "prior"),
                        ("<Next>", "next"), ("<Delete>", "delete")):
        root.bind(key_, lambda e, w=what_: pool_key(e, w), add="+")  # the pool's keys (only on the Pool tab)

    def pool_refocus(e: object) -> None:
        """A click on a button leaves the keys on it, and Space would press it again: give them back to the
        pictures."""
        try:
            if view_var.get() == "pool" and e.widget.winfo_class() in (   # type: ignore[attr-defined]
                    "TButton", "TRadiobutton", "TCheckbutton", "Button", "Radiobutton", "Checkbutton"):
                root.after_idle(pool_canvas.focus_set)
        except (tk.TclError, AttributeError, NameError):
            pass
    root.bind("<ButtonRelease-1>", pool_refocus, add="+")

    # ------------------------------------------------------------------ the pool: pictures made, not used
    pool_bar = ttk.Frame(tab_pool, padding=(0, 0, 0, 4))
    pool_bar.pack(side="top", fill="x")
    pool_navtop = tk.Frame(tab_pool)                                 # < Previous  Page [n] of N  Next >
    pool_navtop.pack(side="top", anchor="w", pady=(0, 4))
    pool_sel: Set[str] = set()                                       # which of them are ticked
    pool_find = tk.StringVar()
    pool_race, pool_sex = tk.StringVar(value="any"), tk.StringVar(value="any")
    pool_count = tk.StringVar(value="")
    POOL_SORTS = ["Newest", "Oldest", "Creature", "Folder"]
    POOL_PER = 200                                                   # pictures to a page, as the gallery
    pool_sort = tk.StringVar(value=str(st.get("pool_sort") or "Newest") if str(st.get("pool_sort") or "") in POOL_SORTS
                             else "Newest")
    pool_hidewait = tk.BooleanVar(value=bool(st.get("pool_hidewait", True)))

    def pool_waiting() -> Set[str]:
        """Pictures still on an Auto card that has no pick applied yet: not spare until it does."""
        out_: Set[str] = set()
        for runs_ in (st.get("auto_runs") or {}).values():             # type: ignore[union-attr]
            for ent_ in (runs_ or {}).values():
                if not ent_.get("chosen"):
                    out_.update(os.path.normcase(str(p_)) for p_ in ent_.get("paths", []))
        return out_

    def pool_folder() -> str:
        """Where the generator puts what it makes."""
        try:
            pg_ = generator_module()
            out_ = str((pg_.load_settings() or {}).get("output_folder") or "generated") if pg_ else "generated"
        except Exception:  # noqa: BLE001 - the generator may not be there
            out_ = "generated"
        return str(Path(__file__).resolve().parent / out_)

    def pool_used() -> Set[str]:
        """Every picture a proposal rests on, the spares taken out of the pool for one included."""
        got = st.get("pool_from")
        return proposed_pictures(st.get("proposals") or {}, got if isinstance(got, dict) else None)

    def pool_who() -> Dict[str, Tuple[str, str]]:
        """Creature file -> (race, sex), from the analysis on screen. A picture made for a creature shows
        that creature, so this is what is in it."""
        if state.get("pool_who") is None:
            by: Dict[str, Tuple[str, str]] = {}
            for o in (all_rows() if state["res"] else []):
                for c_ in (o.get("cre_list") or []):
                    by[str(c_).upper()] = (str(o.get("race") or ""), str(o.get("sex") or ""))
            state["pool_who"] = by
        return state["pool_who"]                                     # type: ignore[return-value]

    def pool_list() -> List[Path]:
        """Everything spare, narrowed by the search box and the two pickers."""
        if state.get("pool_files") is None:
            state["pool_files"] = spare_pictures(pool_folder(), pool_used())
        out = list(state["pool_files"])                              # type: ignore[arg-type]
        look = pool_find.get().strip().casefold()
        if look:
            out = [q for q in out if look in q.name.casefold() or look in made_for(q.name).casefold()
                   or look in q.parent.name.casefold()]
        who = pool_who()
        for pick, at in ((pool_race.get(), 0), (pool_sex.get(), 1)):
            if pick and pick != "any":
                out = [q for q in out if who.get(made_from(q.name), ("", ""))[at] == pick]
        if pool_hidewait.get():
            wait_ = pool_waiting()
            out = [q for q in out if os.path.normcase(str(q)) not in wait_]
        how = pool_sort.get()
        if how in ("Newest", "Oldest"):
            mt_: Dict[str, float] = state.setdefault("pool_mtime", {})   # type: ignore[assignment]
            def age(q: Path) -> float:
                k_ = str(q)
                if k_ not in mt_:
                    try:
                        mt_[k_] = q.stat().st_mtime
                    except OSError:
                        mt_[k_] = 0.0
                return mt_[k_]
            out.sort(key=age, reverse=how == "Newest")
        elif how == "Creature":
            out.sort(key=lambda q: (made_for(q.name).lower(), q.name.lower()))
        return out

    def pool_choices() -> None:
        """Fill the two pickers with the races and sexes actually in the pool."""
        who = pool_who()
        got = [who.get(made_from(q.name), ("", "")) for q in (state.get("pool_files") or [])]
        for box, at in ((race_box, 0), (sex_box, 1)):
            vals = ["any"] + sorted({x[at] for x in got if x[at]})
            box.config(values=vals)
            if box.get() not in vals:
                box.set("any")

    def pool_again() -> None:
        """Read the folder again: something was proposed, deleted or generated."""
        state["pool_files"] = None
        state["pool_who"] = None
        for x_ in [x_ for x_ in pool_sel if not os.path.isfile(x_)]:   # ticks stay, for what is still there
            pool_sel.discard(x_)
        build_pool()

    def pool_cols() -> int:
        """As many cards per row as the window has room for, the same width a gallery card is given - 170
        fitted one card too many in, so the last one was cut off at the edge."""
        w = pool_canvas.winfo_width()
        return max(2, (w if w > 100 else 1000) // CARD_W)

    def wheel_over(w: object, cv: object) -> None:
        """The wheel scrolls a canvas wherever the pointer is over it. Tk sends the wheel to the one widget
        under the pointer, so every card and label on it has to pass it on."""
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            w.bind(ev, lambda e: cv.yview_scroll(                             # type: ignore[attr-defined]
                (-1 if getattr(e, "num", 0) == 4 else 1) if getattr(e, "num", 0) in (4, 5)
                else int(-e.delta / 120), "units"))
        for kid in w.winfo_children():                               # type: ignore[attr-defined]
            wheel_over(kid, cv)

    def pool_wheel(w: object) -> None:
        wheel_over(w, pool_canvas)

    def pool_resize(_e: object) -> None:
        """Lay the cards out again when the window is a different number of cards wide."""
        pool_canvas.configure(scrollregion=pool_canvas.bbox("all"))
        if view_var.get() == "pool" and pool_cols() != state.get("pool_row"):
            if state.get("pool_job"):
                root.after_cancel(state["pool_job"])                 # type: ignore[arg-type]
            state["pool_job"] = root.after(150, build_pool)

    def build_pool() -> None:
        for w_ in pool_grid.winfo_children():
            w_.destroy()
        pool_choices()
        files = pool_list()
        per = POOL_PER
        pages = max(1, -(-len(files) // per))
        page = max(0, min(int(state.get("pool_page") or 0), pages - 1))
        state["pool_page"] = page
        shown = files[page * per:(page + 1) * per]
        state["pool_page_files"] = shown
        state["pool_cur"] = max(0, min(int(state.get("pool_cur") or 0), len(shown) - 1))
        pool_count.set(f"{len(files):,} spare picture(s)" + (f", page {page + 1} of {pages}" if pages > 1 else "")
                       + (f"   -   {len(pool_sel):,} ticked" if pool_sel else ""))
        cols = pool_cols()
        state["pool_row"] = cols
        keep: List[object] = []
        cells_: Dict[str, Tuple[object, object]] = {}
        state["pool_cells"] = cells_
        for i, q in enumerate(shown):
            cell = tk.Frame(pool_grid, bg=CARD, padx=4, pady=4, highlightthickness=3,
                            highlightbackground=CARD if str(q) not in pool_sel else "#2e9e4f")
            cell.grid(row=i // cols, column=i % cols, padx=4, pady=4, sticky="n")
            img = load_thumb(str(q))
            lab = tk.Label(cell, image=img, bg=CARD, cursor="hand2") if img else \
                tk.Label(cell, text="(no preview)", bg=CARD, width=18, height=6)
            if img:
                keep.append(img)
            lab.pack()
            lab.bind("<Button-1>", lambda e, w=str(q): pool_tick(w))
            nm_ = tk.Label(cell, text=made_for(q.name), bg=CARD, font=("TkDefaultFont", 9, "bold"),
                           wraplength=150)
            nm_.pack()
            fo_ = tk.Label(cell, text=q.parent.name, bg=CARD, fg="#888", font=("TkDefaultFont", 8))
            fo_.pack()
            on_ = str(q) in pool_sel
            tk_ = tk.Label(cell, text="\u2611 Ticked" if on_ else "\u2610 Tick", bg=CARD,
                           fg="#2e9e4f" if on_ else "#555", cursor="hand2")
            tk_.pack()
            tk_.bind("<Button-1>", lambda e, w=str(q): pool_tick(w))
            cells_[str(q)] = (cell, tk_)
            for w_ in (cell, nm_, fo_):                               # a click beside the picture: just go there
                w_.bind("<Button-1>", lambda e, w=str(q): pool_goto(w))
            for w_ in (cell, lab, nm_, fo_, tk_):                     # right click: what to do with this one
                w_.bind("<Button-3>", lambda e, x=q: pool_menu(e, x))
            tooltip(lab, f"{q.name}\nClick to tick it, right-click for what to do with it.")
        state["pool_keep"] = keep                                    # Tk drops a picture nothing holds
        pool_nav(pool_navtop, page, pages, per, len(files))
        root.after_idle(pool_marks)                                  # the keyboard's place
        if pages > 1:                                                # and the same at the bottom
            nb_ = tk.Frame(pool_grid)
            nb_.grid(row=(len(shown) + cols - 1) // cols, column=0, columnspan=cols, sticky="w", padx=4, pady=(8, 12))
            pool_nav(nb_, page, pages, per, len(files))
        pool_wheel(pool_grid)
        pool_grid.update_idletasks()
        pool_canvas.configure(scrollregion=pool_canvas.bbox("all"))

    def pool_touch(q: Path) -> None:
        """Work on a spare picture. What comes out of it is a new spare beside it, so the one you had is kept."""
        pg_ = generator_module()
        if pg_ is None:
            return
        try:
            gs_ = pg_.load_settings()                                # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 - the generator may be half installed
            status.set(f"The generator settings could not be read ({e}).")
            return

        def took(_old: str, new_: str, _drop: str = "") -> None:
            pool_again()
            status.set(f"{Path(new_).name} is in the pool.")
        touch_up_window(root, str(q), pg=pg_, gs=gs_, r={"name": made_for(q.name)}, desc="",
                        say=status.set, on_done=took, touch_memory=TOUCH_MEMORY, generic=True)

    def pool_menu(event: object, q: Path) -> None:
        """The right-click menu of a spare picture."""
        menu = tk.Menu(root, tearoff=0)
        ticked = [x for x in pool_list() if str(x) in pool_sel]
        menu.add_command(label=f"Use {q.name} for...", command=lambda: pool_give([q]))
        menu.add_command(label=f"Open in Propose ({made_for(q.name)})", command=lambda: pool_propose(q))
        if len(ticked) > 1 and str(q) in pool_sel:
            menu.add_command(label=f"Use the {len(ticked):,} ticked for...", command=lambda: pool_give())
        menu.add_separator()
        menu.add_command(label="Touch up...", command=lambda: pool_touch(q))
        menu.add_command(label="Save a copy as...", command=lambda: save_image(root, q.name, None, str(q)))
        menu.add_command(label="Open the folder", command=lambda: open_path(str(q.parent)))
        menu.add_separator()
        menu.add_command(label="Untick" if str(q) in pool_sel else "Tick", command=lambda: pool_tick(str(q)))
        menu.add_command(label="Tick this page", command=pool_tick_page)
        if pool_sel:
            menu.add_command(label=f"Untick all ({len(pool_sel):,})",
                             command=lambda: (pool_sel.clear(), pool_marks()))
        menu.add_command(label="Delete this picture", command=lambda: pool_drop([q]))
        try:
            menu.tk_popup(event.x_root, event.y_root)                # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def pool_propose(q: Path) -> None:
        """Open the creature the picture was made for in Propose, with the picture among its candidates."""
        cre_ = made_from(q.name)
        if not state["res"]:                                         # nothing analyzed yet (just started): do it,
            status.set("Analyzing first...")                         # then open it
            try:
                analyze_clicked()
            except Exception:  # noqa: BLE001
                return
            tries_ = [0]

            def again() -> None:
                tries_[0] += 1
                if state["res"]:
                    pool_propose(q)
                elif tries_[0] < 600 and str(go_btn.cget("state")) != "normal" or tries_[0] < 4:
                    root.after(500, again)
            root.after(500, again)
            return
        has_ = lambda o: cre_ and cre_ in [str(c).upper() for c in (o.get("cre_list") or [])]   # noqa: E731
        r_ = next((o for o in all_rows() if has_(o)), None)
        if r_ is None:                                               # one of a merged row's creatures
            r_ = next((o for o in all_rows() if any(has_(p_) for p_ in (o.get("parts") or []))), None)
        if r_ is None:
            status.set(f"{made_for(q.name)} ({cre_ or 'no creature file in the name'}) is not in the mod analyzed, "
                       "so it cannot be opened in Propose. Use for... gives the picture to anyone.")
            return
        state["propose_take"] = str(q)                               # the Propose tab takes it in once it is built
        view_var.set("generate")
        state["gen_built"] = True
        show_view()
        generate_dialog(r_)

    def pool_turn(step: int, to: Optional[int] = None) -> None:
        state["pool_page"] = max(0, (to if to is not None else int(state.get("pool_page") or 0) + step))
        build_pool()
        pool_canvas.yview_moveto(0)

    def pool_nav(bar: object, page: int, pages: int, per: int, total: int) -> None:
        """< Previous, Page [n] of N, Next >: as the gallery has them. Type a page number and press Enter."""
        for w_ in bar.winfo_children():                              # type: ignore[attr-defined]
            w_.destroy()
        if pages <= 1:
            bar.config(highlightthickness=0)                         # type: ignore[attr-defined]
            return
        bar.config(highlightthickness=2, highlightbackground="#8a8f98", highlightcolor="#8a8f98", padx=4, pady=2)   # type: ignore[attr-defined]
        tk.Button(bar, text=f"< Previous {per}", command=lambda: pool_turn(-1),
                  state="normal" if page > 0 else "disabled").pack(side="left", padx=6)
        bg_ = bar.cget("bg")                                         # type: ignore[attr-defined]
        tk.Label(bar, text="Page", bg=bg_, fg="#555").pack(side="left", padx=(6, 2))
        pv_ = tk.StringVar(value=str(page + 1))
        pe_ = tk.Entry(bar, textvariable=pv_, width=4, justify="center")
        pe_.pack(side="left")

        def jump(_e: object = None) -> None:
            try:
                pool_turn(0, int(pv_.get().strip()) - 1)
            except ValueError:
                pass
        pe_.bind("<Return>", jump)
        pe_.bind("<KP_Enter>", jump)
        first = page * per
        tk.Label(bar, text=f"of {pages}  ({first + 1:,}-{min(first + per, total):,} of {total:,})", bg=bg_,
                 fg="#555").pack(side="left", padx=(2, 6))
        tk.Button(bar, text=f"Next {per} >", command=lambda: pool_turn(1),
                  state="normal" if page < pages - 1 else "disabled").pack(side="left", padx=6)

    def pool_tick_page() -> None:
        """Tick everything on this page - or untick it, when it is all ticked already."""
        here = [str(q) for q in state.get("pool_page_files") or []]
        if here and all(x in pool_sel for x in here):
            pool_sel.difference_update(here)
        else:
            pool_sel.update(here)
        pool_marks()

    def pool_tick_all() -> None:
        """Tick every picture listed (all pages, as narrowed above) - or untick them, when all are ticked."""
        every_ = [str(q) for q in pool_list()]
        if every_ and all(x in pool_sel for x in every_):
            pool_sel.difference_update(every_)
        else:
            pool_sel.update(every_)
        pool_marks()

    def pool_marks() -> None:
        """Show the ticks as they are now, on the pictures already drawn: no redraw of the page."""
        for path_, (cell_, lab_) in (state.get("pool_cells") or {}).items():   # type: ignore[union-attr]
            on_ = path_ in pool_sel
            try:
                cell_.config(highlightbackground="#2e9e4f" if on_ else CARD)   # type: ignore[attr-defined]
                lab_.config(text="\u2611 Ticked" if on_ else "\u2610 Tick", fg="#2e9e4f" if on_ else "#555")   # type: ignore[attr-defined]
            except tk.TclError:
                pass
        here_ = [str(q) for q in state.get("pool_page_files") or []]
        cur_ = int(state.get("pool_cur") or 0)
        for i_, path_ in enumerate(here_):                           # the keyboard's place: a blue card
            got_ = (state.get("pool_cells") or {}).get(path_)        # type: ignore[union-attr]
            if not got_:
                continue
            bg_ = "#cfe0f5" if i_ == cur_ and state.get("pool_keys") else CARD
            try:
                got_[0].config(bg=bg_)                               # type: ignore[attr-defined]
                for ch_ in got_[0].winfo_children():                 # type: ignore[attr-defined]
                    ch_.config(bg=bg_)
            except tk.TclError:
                pass
        txt_ = pool_count.get().split("   -   ")[0]
        pool_count.set(txt_ + (f"   -   {len(pool_sel):,} ticked" if pool_sel else ""))

    def pool_tick(path: str) -> None:
        pool_sel.discard(path) if path in pool_sel else pool_sel.add(path)
        here_ = [str(q) for q in state.get("pool_page_files") or []]
        if path in here_:                                            # a click moves the keyboard's place there too
            state["pool_cur"] = here_.index(path)
            state["pool_keys"] = True                                # and shows it, so the arrows go on from it
        try:
            pool_canvas.focus_set()                                  # the keys go to the pictures from here on
        except tk.TclError:
            pass
        pool_marks()

    def pool_goto(path: str) -> None:
        """Put the keyboard's place on this picture without ticking it."""
        here_ = [str(q) for q in state.get("pool_page_files") or []]
        if path in here_:
            state["pool_cur"] = here_.index(path)
            state["pool_keys"] = True
        try:
            pool_canvas.focus_set()
        except tk.TclError:
            pass
        pool_marks()

    def pool_see(i: int) -> None:
        """Scroll so picture i of the page is in view."""
        got_ = (state.get("pool_cells") or {}).get(                  # type: ignore[union-attr]
            str((state.get("pool_page_files") or [])[i]))            # type: ignore[index]
        if not got_:
            return
        try:
            cell_ = got_[0]
            total_ = max(1, pool_grid.winfo_height())
            top_, bot_ = cell_.winfo_y(), cell_.winfo_y() + cell_.winfo_height()   # type: ignore[attr-defined]
            v0, v1 = pool_canvas.yview()
            if top_ < v0 * total_:
                pool_canvas.yview_moveto(max(0.0, (top_ - 8) / total_))
            elif bot_ > v1 * total_:
                pool_canvas.yview_moveto(max(0.0, (bot_ + 8) / total_ - (v1 - v0)))
        except (tk.TclError, IndexError):
            pass

    def pool_key(e: object, what: str) -> Optional[str]:
        """Arrows move along the pictures, Space ticks, Page Up / Down turn the page, Delete deletes the ticked."""
        if view_var.get() != "pool":
            return None
        if e.widget.winfo_class() in ("TEntry", "Entry", "TCombobox", "TSpinbox", "Spinbox"):   # type: ignore[attr-defined]
            return None
        if e.widget is not pool_canvas:                              # a button had the keys: Space has pressed
            try:                                                     # it already, so take them back, and leave
                pool_canvas.focus_set()                              # this one press be
            except tk.TclError:
                pass
            if what == "space":
                return "break"
        here_ = state.get("pool_page_files") or []
        if not here_:
            return "break"
        cols_ = int(state.get("pool_row") or 1)
        cur_ = int(state.get("pool_cur") or 0)
        state["pool_keys"] = True
        if what == "space":
            pool_tick(str(here_[min(cur_, len(here_) - 1)]))          # type: ignore[index]
            return "break"
        if what in ("prior", "next"):
            pool_turn(-1 if what == "prior" else 1)
            state["pool_cur"] = 0
            pool_marks()
            return "break"
        if what == "delete":
            pool_drop()
            return "break"
        step_ = {"left": -1, "right": 1, "up": -cols_, "down": cols_}.get(what, 0)
        new_ = 0 if what == "home" else len(here_) - 1 if what == "end" else cur_ + step_   # type: ignore[arg-type]
        state["pool_cur"] = max(0, min(len(here_) - 1, new_))         # type: ignore[arg-type]
        pool_marks()
        pool_see(int(state["pool_cur"]))                             # type: ignore[arg-type]
        return "break"

    def pool_page(by: int) -> None:
        state["pool_page"] = max(0, int(state.get("pool_page") or 0) + by)
        build_pool()

    def pool_drop(only: "Optional[List[Path]]" = None) -> None:
        """Delete pictures that came out wrong: the ticked ones, or the one right-clicked. They go for good."""
        gone = list(only) if only else [q for q in pool_list() if str(q) in pool_sel]
        if not gone:
            status.set("Tick the pictures to delete first.")
            return
        if not messagebox.askyesno("Portrait Scout", f"Delete {len(gone):,} picture(s) from the disk?\n\n"
                                   + "\n".join(q.name for q in gone[:10])
                                   + (f"\n...and {len(gone) - 10:,} more" if len(gone) > 10 else "")
                                   + "\n\nThis cannot be undone.", parent=root):
            return
        went = bad = 0
        for q in gone:
            for f in {str(q)} | {str(x) for x in q.parent.glob(plain_of(str(q)).stem + "_*")
                                 if x.is_file() and x.suffix.lower() in POOL_EXT}:
                try:
                    os.remove(f)
                    went += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    bad += 1
        dead_ = {os.path.normcase(str(q)) for q in gone if not q.exists()}
        for runs_ in (st.get("auto_runs") or {}).values():             # type: ignore[union-attr]
            for ent_ in (runs_ or {}).values():                       # off the Auto cards too
                if any(os.path.normcase(str(p_)) in dead_ for p_ in ent_.get("paths", [])):
                    ent_["paths"] = [p_ for p_ in ent_.get("paths", []) if os.path.normcase(str(p_)) not in dead_]
                    if os.path.normcase(str(ent_.get("sel") or "")) in dead_:
                        ent_.pop("sel", None)
        pool_sel.difference_update(str(q) for q in gone)
        save_settings(st)
        pool_again()
        status.set(f"Deleted {went:,} file(s)." + (f" {bad:,} could not be deleted." if bad else ""))

    def pool_groups(every: List[dict]) -> "Dict[str, List[dict]]":
        """Creatures gathered into the groups worth filling in one go: everything, the ones with no face yet,
        and the ones a mod copied from the same creature, which are usually the same kind of person."""
        bare = [o for o in every if rowkey(o) not in props]
        out: Dict[str, List[dict]] = {}
        if bare:
            out[f"No face yet ({len(bare):,})"] = bare
        out[f"Everything ({len(every):,})"] = every
        by: Dict[str, List[dict]] = {}
        for o in bare:
            was = str(o.get("copy_of") or "")
            if was:
                by.setdefault(was, []).append(o)
        for was, lot in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(lot) > 1:
                out[f"Copied from {was} ({len(lot):,})"] = lot
        return out

    def pool_give(only: "Optional[List[Path]]" = None) -> None:
        """Hand pictures to creatures: the ticked ones, or the one right-clicked."""
        picks = list(only) if only else [q for q in pool_list() if str(q) in pool_sel]
        if not picks:
            status.set("Tick a picture first.")
            return
        every = [o for o in all_rows() if rowkey(o) not in skip]
        if not every:
            messagebox.showinfo("Portrait Scout", "Analyze a mod first.", parent=root)
            return
        groups = pool_groups(every)
        w = tk.Toplevel(root)
        w.title("Propose these pictures")
        w.transient(root)
        esc_closes(w)
        bx = ttk.Frame(w, padding=12)
        bx.pack(fill="both", expand=True)
        ttk.Label(bx, text=(f"{len(picks):,} picture(s) ticked. Pick a group to tick its creatures, or pick them "
                            "below. The pictures are dealt out in order."), wraplength=560,
                  justify="left").pack(anchor="w")
        gv, reuse_v = tk.StringVar(value=next(iter(groups))), tk.BooleanVar(value=False)
        top_ = ttk.Frame(bx)
        top_.pack(fill="x", pady=(8, 0))
        ttk.Label(top_, text="Group:").pack(side="left")
        gb_ = ttk.Combobox(top_, textvariable=gv, width=44, state="readonly", values=list(groups))
        gb_.pack(side="left", padx=(4, 10))
        tooltip(gb_, "Creatures that belong together: the ones with no face yet, or the ones a mod copied from "
                     "the same creature, which are usually the same kind of person.")
        rb_ = ttk.Checkbutton(top_, text="Reuse pictures", variable=reuse_v)
        rb_.pack(side="left")
        tooltip(rb_, "Off: each picture goes to one creature and the rest are left alone. On: the pictures go "
                     "round again, so a face can repeat.")
        lb = tk.Listbox(bx, selectmode="multiple", width=86, height=min(20, max(5, len(every))),
                        exportselection=False)
        lb.pack(fill="both", expand=True, pady=(8, 0))
        rows: List[dict] = []

        def fill(*_a: object) -> None:
            """Show the chosen group and tick all of it."""
            rows[:] = groups.get(gv.get()) or []
            lb.delete(0, "end")
            for o in rows:
                has = " [has one]" if rowkey(o) in props else ""
                lb.insert("end", f"{o['name']}  -  {o.get('race', '')} {o.get('sex', '')}"
                                 f"  -  {', '.join(o.get('cre_list') or [])}{has}")
            lb.select_set(0, "end")
        gb_.bind("<<ComboboxSelected>>", fill)
        fill()

        def give() -> None:
            picked = [rows[i] for i in lb.curselection()]
            if not picked:
                w.destroy()
                return
            if not reuse_v.get():
                picked = picked[:len(picks)]
            for i, o in enumerate(picked):
                props[rowkey(o)] = os.path.normpath(str(picks[i % len(picks)]))
                crop_store().pop(rowkey(o), None)
            persist_props()
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            w.destroy()
            pool_again()
            status.set(f"{len(picked):,} creature(s) proposed from the pool.")
        row_ = ttk.Frame(bx)
        row_.pack(anchor="e", pady=(10, 0))
        ttk.Button(row_, text="Use for these", command=give).pack(side="left")
        ttk.Button(row_, text="Pick all", command=lambda: lb.select_set(0, "end")).pack(side="left", padx=(6, 0))
        ttk.Button(row_, text="Pick none", command=lambda: lb.select_clear(0, "end")).pack(side="left", padx=6)
        ttk.Button(row_, text="Cancel", command=w.destroy).pack(side="left")
        w.grab_set()

    ttk.Label(pool_bar, textvariable=pool_count, foreground="#555").pack(side="left")
    ttk.Label(pool_bar, text="   Search:").pack(side="left")
    pf_ = ttk.Entry(pool_bar, textvariable=pool_find, width=22)
    pf_.pack(side="left", padx=(4, 0))
    pf_.bind("<KeyRelease>", lambda e: (state.__setitem__("pool_page", 0), build_pool()))
    ttk.Button(pool_bar, text="x", width=2,
               command=lambda: (pool_find.set(""), build_pool())).pack(side="left", padx=(2, 10))
    race_box = ttk.Combobox(pool_bar, textvariable=pool_race, width=13, state="readonly", values=["any"])
    race_box.pack(side="left")
    sex_box = ttk.Combobox(pool_bar, textvariable=pool_sex, width=9, state="readonly", values=["any"])
    sex_box.pack(side="left", padx=(4, 10))
    for _b in (race_box, sex_box):
        _b.bind("<<ComboboxSelected>>", lambda e: (state.__setitem__("pool_page", 0), build_pool()))
        tooltip(_b, "What the picture is of, taken from the creature it was made for. Pictures made for a mod "
                    "that is not the one analyzed are not narrowed by these.")
    pg_btn = ttk.Button(pool_bar, text="Use for...", command=pool_give)
    pg_btn.pack(side="left")
    tooltip(pg_btn, "Give the ticked pictures to creatures you pick. One picture goes to all of them; several "
                    "are dealt out in order, so twenty commoners can be handed twenty faces at once.")
    pd_btn = tk.Button(pool_bar, text="Delete", bg="#b5452f", fg="white", activebackground="#8f3524",
                       activeforeground="white", relief="raised", bd=1, command=pool_drop)
    pd_btn.pack(side="left", padx=(6, 0))
    tooltip(pd_btn, "Delete the ticked pictures from the disk, with anything made from them. They are the ones "
                    "that came out wrong, so this cannot be undone.")
    un_btn = ttk.Button(pool_bar, text="Untick all", width=10,
                        command=lambda: (pool_sel.clear(), pool_marks()))
    un_btn.pack(side="right")
    tooltip(un_btn, "Untick everything, on this page and the others.")
    ta_btn = ttk.Button(pool_bar, text="Tick all", width=9, command=lambda: pool_tick_all())
    ta_btn.pack(side="right", padx=(0, 4))
    tooltip(ta_btn, "Tick every picture listed, on every page (as narrowed by search and the pickers). "
                    "Again unticks them.")
    tk_btn = ttk.Button(pool_bar, text="Tick page", width=10, command=lambda: pool_tick_page())
    tk_btn.pack(side="right", padx=(0, 4))
    tooltip(tk_btn, "Tick every picture on this page. The ones on the other pages are left as they are.")
    hw_ = ttk.Checkbutton(pool_bar, text="Hide Auto's", variable=pool_hidewait,
                          command=lambda: (st.__setitem__("pool_hidewait", bool(pool_hidewait.get())),
                                           save_settings(st), state.__setitem__("pool_page", 0), build_pool()))
    hw_.pack(side="right", padx=(0, 10))
    tooltip(hw_, "Leave out pictures still on an Auto card with no pick applied, so a mass delete cannot take "
                 "them. Once a card's pick is applied, the others on it show here as spare.")
    so_ = ttk.Combobox(pool_bar, textvariable=pool_sort, values=POOL_SORTS, state="readonly", width=9)
    so_.pack(side="right", padx=(0, 4))
    so_.bind("<<ComboboxSelected>>", lambda e: (st.__setitem__("pool_sort", pool_sort.get()), save_settings(st),
                                                state.__setitem__("pool_page", 0), build_pool()))
    tooltip(so_, "Sort: newest or oldest first, by creature, or by folder.")
    ttk.Label(pool_bar, text="Sort:").pack(side="right", padx=(10, 2))
    pool_canvas = tk.Canvas(tab_pool, highlightthickness=0, bg=PANEL)
    pool_sb = ttk.Scrollbar(tab_pool, orient="vertical", command=pool_canvas.yview)
    pool_canvas.configure(yscrollcommand=pool_sb.set)
    pool_sb.pack(side="right", fill="y")
    pool_canvas.pack(side="left", fill="both", expand=True)
    pool_grid = tk.Frame(pool_canvas, bg=PANEL)
    pool_canvas.create_window((0, 0), window=pool_grid, anchor="nw")
    pool_grid.bind("<Configure>", lambda e: pool_canvas.configure(scrollregion=pool_canvas.bbox("all")))
    pool_canvas.bind("<Configure>", pool_resize)
    pool_wheel(pool_canvas)

    def show_view() -> None:
        for t in (tab_table, tab_gal, tab_gen, tab_exp, tab_pool, tab_auto):
            t.pack_forget()
        if view_var.get() in ("gallery", "table", "export", "auto"):        # the filters: what is listed in these three
            fbar.pack(fill="x", before=content)
        else:
            fbar.pack_forget()
        if view_var.get() == "auto":                                 # what the Auto tab was: the gallery's own view
            view_var.set("gallery")
            st["gallery_mode"] = "auto"
            auto_mode_var.set(True)
        if view_var.get() == "gallery" and st.get("gallery_mode", "plain") == "auto":
            tab_auto.pack(fill="both", expand=True)                  # the gallery opens on the batch: two pictures
            build_auto()                                             # a creature, pick one; Plain gallery for the cards
            return
        if view_var.get() == "pool":
            tab_pool.pack(fill="both", expand=True)
            state["pool_files"] = None                               # pictures may have been made since last time
            state["pool_who"] = None
            root.update_idletasks()                                  # so the canvas knows how wide it is
            build_pool()
            pool_canvas.focus_set()                                  # Space ticks, and does not press the
            return                                                   # Pool button again
        if view_var.get() == "gallery":
            tab_gal.pack(fill="both", expand=True)
            if state["res"]:                                         # at startup there is nothing to build yet, and
                root.update_idletasks()                              # build_gallery is not defined this early
                build_gallery(sorted_rows())
        elif view_var.get() == "export":
            tab_exp.pack(fill="both", expand=True)
            fill_export()
        elif view_var.get() == "generate":
            tab_gen.pack(fill="both", expand=True)
            if not state.get("gen_built"):                            # the creature selected in the table, or any portrait
                sel, rows = tree.selection(), sorted_rows()
                idx = tree.index(sel[0]) if sel else -1
                if generator_module() is None:
                    gen_hint("The generator folder is missing: generator/portrait_generator.py should sit "
                             "beside portrait_scout.py. See generator/README.md.")
                else:
                    generate_dialog(rows[idx] if 0 <= idx < len(rows) else None)
        else:
            tab_table.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ Auto: a portrait for every creature listed
    # One Generate for the whole list: each creature gets a couple of candidates made the way the Propose tab
    # makes them (its own description, colors, role and face, Vary faces on), and the Auto tab shows them two
    # creatures to a row. Clicking a picture proposes it. Nothing is proposed without being looked at: Klein still
    # paints the odd monkey-ogre, and only a person can tell.
    AUTO_TW, AUTO_TH = 160, 250
    AUTO_PAGE = 40                                                   # cards on one page of the Auto view                                      # a candidate as the Auto tab shows it
    auto_ui: Dict[str, object] = {"built": False, "busy": False, "queue": [], "imgs": {}, "cards": {}, "empty": None,
                                  "apply": None, "bulk": False}

    def auto_mod() -> str:
        res_ = state["res"]
        return str(res_["mod"]) if res_ else ""                      # type: ignore[index]

    def auto_store() -> Dict[str, dict]:
        """rowkey -> {"paths": [...candidates], "chosen": png, "bmp": its .bmp}, kept per mod between sessions."""
        return st.setdefault("auto_runs", {}).setdefault(auto_mod(), {})   # type: ignore[union-attr]

    def auto_folder(gs_: dict) -> Path:
        return (Path(__file__).resolve().parent / str(gs_.get("output_folder", "generated"))
                / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", auto_mod()))

    def auto_stem(r: dict) -> str:                                   # the Propose tab's own, so it finds them too
        return re.sub(r"[^A-Za-z0-9_.#!-]+", "_", f"{r['name']}_{(r.get('cre_list') or [''])[0]}").strip("_") or "portrait"

    def auto_style(pg_: object, gs_: dict, r: dict) -> str:
        """The style the Propose tab would open this creature with: the last one used for people, or for animals."""
        styles_ = pg_.style_names(gs_) if hasattr(pg_, "style_names") else []   # type: ignore[attr-defined]
        if not styles_:
            return ""
        fallback = gs_.get("style") if gs_.get("style") in styles_ else styles_[0]
        if is_animal(r):
            want = str(st.get("portrait_style_animal") or "")
            return want if want in styles_ else next((x for x in styles_ if animal_style(x)), str(fallback))
        for want in (str(st.get("portrait_style_people") or ""), str(st.get("portrait_style") or ""), str(fallback)):
            if want in styles_ and not animal_style(want):
                return want
        return next((x for x in styles_ if not animal_style(x)), str(fallback))

    def auto_desc(pg_: object, gs_: dict, r: dict) -> str:
        """What the Propose tab would start from: the description kept for it there, else its own file's."""
        key_ = rowkey(r)
        notes_ = ((st.get("portrait_notes") or {}).get(auto_mod()) or {})   # type: ignore[union-attr]
        saved_ = notes_.get(key_) or {}
        whose_ = ",".join(str(c).upper() for c in (r.get("cre_list") or [])) or str(r.get("name") or "")
        if not saved_ and r.get("cre_list"):                         # filed under the dialogs it had before
            saved_ = next((v_ for v_ in notes_.values() if str(v_.get("whose") or "") == whose_), {})
        if saved_.get("desc") and str(saved_.get("whose") or whose_) == whose_:
            return str(saved_["desc"])
        r["face_variant"] = ((st.get("face_variant") or {}).get(auto_mod()) or {}).get(key_, 0)   # type: ignore[union-attr]
        return str(pg_.describe(r, gs_)[1])                         # type: ignore[attr-defined]

    def auto_prompt(pg_: object, gs_: dict, style_: str, desc_: str, words_: str) -> Tuple[str, str]:
        tmpl, neg = pg_.style_text(gs_, style_)                      # type: ignore[attr-defined]
        if not st.get("use_lora", True):
            tmpl = re.sub(r"\s*<lora:[^>]*>", "", tmpl)
        elif str(st.get("lora_weight") or "Style's") != "Style's":
            w_ = str(st.get("lora_weight"))
            tmpl = re.sub(r"(<lora:[^:>]+):[0-9.]+>", lambda m_: f"{m_.group(1)}:{w_}>", tmpl)
        desc_ = pg_.womanly(desc_) if hasattr(pg_, "womanly") else desc_   # type: ignore[attr-defined]
        d_ = pg_.vary_merge(desc_, words_, gs_) if words_ else desc_    # type: ignore[attr-defined]
        subj_ = re.sub(r"^(adult|young)\s+", "", d_.split(",")[0].strip())
        bg_ = pg_.pick_palette(gs_, pg_.style_background(gs_, style_))   # type: ignore[attr-defined]  {random}: a palette
        pr_ = pg_.fill_prompt(tmpl, subj_, d_, "", bg_)              # type: ignore[attr-defined]
        if hasattr(pg_, "youthen"):
            pr_ = pg_.youthen(pr_, d_, gs_)                          # type: ignore[attr-defined]
        return pr_, neg

    def auto_rows() -> List[dict]:
        """The creatures a batch would make pictures for: those listed now (the filters above apply), less the
        ones already proposed and the ones a batch has already made pictures for."""
        rows_ = [r for r in sorted_rows() if rowkey(r) and (auto_redo.get() or rowkey(r) not in props)]
        if not auto_party.get():
            rows_ = [r for r in rows_ if not r.get("party")]
        if auto_skip_has.get():
            rows_ = [r for r in rows_ if not (r.get("large") or r.get("small"))]
        store_ = auto_store()
        return [r for r in rows_ if not any(os.path.isfile(p) for p in (store_.get(rowkey(r)) or {}).get("paths", []))
                and (store_.get(rowkey(r)) or {}).get("chosen") != "keep"]

    def auto_thumb(path: str) -> object:
        cache_: Dict[str, object] = auto_ui["imgs"]                  # type: ignore[assignment]
        if path in cache_:
            return cache_[path]
        img = None
        try:
            from PIL import Image, ImageTk
            im = Image.open(path)
            im.thumbnail((AUTO_TW, AUTO_TH))
            img = ImageTk.PhotoImage(im)
        except Exception:  # noqa: BLE001
            try:
                pi = tk.PhotoImage(file=path)
                k = max(1, -(-pi.width() // AUTO_TW), -(-pi.height() // AUTO_TH))
                img = pi.subsample(k, k) if k > 1 else pi
            except Exception:  # noqa: BLE001
                img = None
        cache_[path] = img
        return img

    def auto_status(text: str) -> None:
        if auto_ui.get("built"):
            auto_msg.set(text)                                       # type: ignore[union-attr]
        status.set("Auto: " + text)

    def auto_counts() -> None:
        if not auto_ui.get("built"):
            return
        store_ = auto_store()
        live = {k for k, v in store_.items() if v.get("chosen") == "keep" or any(os.path.isfile(p) for p in v.get("paths", []))}
        chosen = sum(1 for k in live if store_[k].get("chosen"))
        pend = sum(1 for k in live if store_[k].get("sel"))
        if auto_ui.get("apply") is not None:
            auto_ui["apply"].config(text=f"Apply picks ({pend})" if pend else "Apply picks",   # type: ignore[union-attr]
                                    state="normal" if pend else "disabled")
        left = len(auto_rows()) if state["res"] else 0
        auto_count_var.set(f"{len(live)} made, {chosen} proposed, {pend} picked to apply"   # type: ignore[union-attr]
                           + (f"  ·  {left} listed with none yet" if left else ""))

    def auto_wheel(e: object) -> str:
        auto_ui["canvas"].yview_scroll(int(-getattr(e, "delta", 0) / 120) or (1 if getattr(e, "num", 0) == 5 else -1), "units")
        return "break"

    def auto_catch(w: object) -> None:
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            w.bind(ev, auto_wheel)                                   # type: ignore[attr-defined]
        for kid in w.winfo_children():                               # type: ignore[attr-defined]
            auto_catch(kid)

    def auto_cut(r: dict, ent: Optional[dict] = None) -> Tuple[int, int, float, float, float]:
        """(width, height, zoom, dx, dy) a creature's portrait is cut with: its own crop when one was set here,
        else what the Propose tab uses (the style's zoom, your head zoom and nudge)."""
        pg_ = generator_module()
        gs_ = auto_ui.get("gs") or (pg_.load_settings() if pg_ else {})   # type: ignore[attr-defined]
        auto_ui["gs"] = gs_
        size_ = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 \
            else [(gs_.get("portrait_bmp") or {}).get("width", 169), (gs_.get("portrait_bmp") or {}).get("height", 266)]   # type: ignore[union-attr]
        bw, bh = int(size_[0] or 169), int(size_[1] or 266)
        own = (ent or {}).get("crop") or {}
        zm = own.get("z")
        if zm is None:
            zm = pg_.style_zoom(gs_, auto_style(pg_, gs_, r)) if pg_ and hasattr(pg_, "style_zoom") else None   # type: ignore[attr-defined]
            zm = zm or st.get("portrait_head_zoom") or (gs_.get("portrait_bmp") or {}).get("zoom") or 1.0   # type: ignore[union-attr]
        dx = own.get("dx", float(st.get("crop_dx") or 0))
        dy = own.get("dy", float(st.get("crop_dy") or 0))
        return bw, bh, max(1.0, min(2.5, float(zm))), float(dx), float(dy)

    def auto_box(r: dict, ent: dict, path: str) -> Optional[Tuple[int, int, int, int, int, int]]:
        """(left, top, width, height) of what the portrait keeps, in the picture's own pixels, and its size."""
        pg_ = generator_module()
        info_: Dict[str, object] = auto_ui.setdefault("info", {})   # type: ignore[assignment]
        if path not in info_:
            info_[path] = image_info(path)
        inf_ = info_[path]
        if not inf_ or pg_ is None or not hasattr(pg_, "crop_box"):
            return None
        bw, bh, zm, dx, dy = auto_cut(r, ent)
        sw, sh = int(inf_["w"]), int(inf_["h"])                      # type: ignore[index]
        top_ = float(((auto_ui.get("gs") or {}).get("portrait_bmp") or {}).get("top", 0) or 0)   # type: ignore[union-attr]
        lf, tp, cw, ch = pg_.crop_box(sw, sh, bw, bh, zm, top_)      # type: ignore[attr-defined]
        return int(max(0, min(sw - cw, lf + dx * sw))), int(max(0, min(sh - ch, tp + dy * sh))), cw, ch, sw, sh

    def auto_lock(r: dict, on: bool) -> None:
        ent = auto_store().setdefault(rowkey(r), {"paths": []})
        if on:
            ent["crop_locked"] = True
            if not ent.get("crop"):                                  # lock what is shown, even if never moved
                _bw, _bh, zm, dx, dy = auto_cut(r, ent)
                ent["crop"] = {"z": round(zm, 2), "dx": round(dx, 3), "dy": round(dy, 3)}
        else:
            ent.pop("crop_locked", None)
        save_settings(st)
        auto_place(r)

    def auto_thumb_cut(r: dict, ent: dict, path: str) -> object:
        """The picture as the portrait will be cut from it, at the card's size."""
        bx = auto_box(r, ent, path)
        if not bx:
            return auto_thumb(path)
        ck = f"cut:{path}:{bx[:4]}"
        cache_: Dict[str, object] = auto_ui["imgs"]                  # type: ignore[assignment]
        if ck not in cache_:
            try:
                from PIL import Image, ImageTk
                im = Image.open(path).convert("RGB").crop((bx[0], bx[1], bx[0] + bx[2], bx[1] + bx[3]))
                im.thumbnail((AUTO_TW, AUTO_TH))
                cache_[ck] = ImageTk.PhotoImage(im)
            except Exception:  # noqa: BLE001 - no Pillow: the whole picture with its box instead
                cache_[ck] = None
        return cache_[ck] or auto_thumb(path)

    def auto_src(bmp: str) -> str:
        """The picture a proposal was cut from ("X_1_169x266_z12_m0_1.bmp" is X_1.png beside it), or ""."""
        m_ = CUT_MARK.search(os.path.basename(bmp or ""))
        if not m_:
            return ""
        base_ = os.path.join(os.path.dirname(bmp), os.path.basename(bmp)[:m_.start()])
        for ext_ in (".png", ".jpg", ".jpeg", ".webp"):
            if os.path.isfile(base_ + ext_):
                return base_ + ext_
        return ""

    def auto_recut(r: dict, ent: dict) -> bool:
        """Cut the creature's current proposal again with the card's crop, when it is not one of the batch's
        pictures (made in Propose before, or since) but was cut from a picture that is still there."""
        pg_ = generator_module()
        key_ = rowkey(r)
        prop = str(props.get(key_) or "")
        src = auto_src(prop)
        if pg_ is None or not src:
            return False
        bw, bh, zm, dx, dy = auto_cut(r, ent)
        mark = f"_z{int(round(zm * 10))}" + (f"_m{int(dx * 100)}_{int(dy * 100)}" if dx or dy else "")
        bmp = os.path.splitext(src)[0] + f"_{bw}x{bh}{mark}.bmp"
        bx = auto_box(r, ent, src)
        try:
            to_portrait_bmp(src, bmp, bw, bh, getattr(pg_, "crop_box", None), bx[:4] if bx else None)
        except Exception as e:  # noqa: BLE001
            auto_status(f"Could not cut {Path(src).name} again: {e}")
            return False
        same_ = lambda a, b: os.path.normcase(str(a or "")) == os.path.normcase(str(b or ""))   # noqa: E731
        if same_(ent.get("was"), prop):
            ent["was"] = bmp                                         # Keep now keeps the new cut
        if same_(ent.get("bmp"), prop):
            ent["bmp"] = bmp
        props[key_] = os.path.normpath(bmp)
        persist_props()
        refresh_table_only()
        save_settings(st)
        auto_status(f"Cut again for {r['name']}: {Path(bmp).name}")
        return True

    def auto_crop(r: dict, zoom: float = 0.0, mx: float = 0.0, my: float = 0.0, reset: bool = False) -> None:
        """Move this creature's crop: the yellow box on its pictures. A picture already proposed is cut again."""
        ent = auto_store().setdefault(rowkey(r), {"paths": []})
        if reset:
            ent.pop("crop", None)
        else:
            bw, bh, zm, dx, dy = auto_cut(r, ent)
            ent["crop"] = {"z": round(max(1.0, min(2.5, zm + zoom)), 2),
                           "dx": round(max(-0.5, min(0.5, dx + mx)), 3), "dy": round(max(-0.5, min(0.5, dy + my)), 3)}
        save_settings(st)
        ch_ = str(ent.get("chosen") or "")
        if ch_ and ch_ not in ("keep", "elsewhere") and os.path.isfile(ch_) and auto_picked(r, ent) == ch_:
            auto_choose(r, ch_)                                      # proposed already: cut it again to match
        else:
            prop_ = str(props.get(rowkey(r)) or "")                  # one proposed in Propose: cut that again
            if prop_ and (os.path.normcase(prop_) == os.path.normcase(str(ent.get("was") or ""))
                          or os.path.normcase(prop_) == os.path.normcase(str(ent.get("bmp") or ""))):
                auto_recut(r, ent)
            auto_place(r)

    def auto_choose(r: dict, path: str) -> None:
        """Propose this candidate: cut to the game portrait size the Propose tab uses, exactly as "Use this"."""
        pg_ = generator_module()
        if pg_ is None:
            return
        ent0 = auto_store().get(rowkey(r)) or {}
        bw, bh, zm, dx, dy = auto_cut(r, ent0)
        mark = f"_z{int(round(zm * 10))}" + (f"_m{int(dx * 100)}_{int(dy * 100)}" if dx or dy else "")
        bmp = os.path.splitext(path)[0] + f"_{bw}x{bh}{mark}.bmp"
        bx = auto_box(r, ent0, path)
        box_ = bx[:4] if bx else None
        try:
            to_portrait_bmp(path, bmp, bw, bh, getattr(pg_, "crop_box", None), box_)
        except Exception as e:  # noqa: BLE001
            auto_status(f"Could not make the {bw}x{bh} .bmp for {r['name']}: {e}")
            return
        key_ = rowkey(r)
        ent = auto_store().setdefault(key_, {"paths": [path]})
        if "was" not in ent:                                         # what it had before: Keep puts it back
            ent["was"] = props.get(key_, "") if not ent.get("chosen") else ""
        old = str(ent.get("bmp") or "")
        if old and os.path.normcase(old) != os.path.normcase(bmp) and os.path.isfile(old) and auto_own_cut(ent, old):
            try:                                                     # the cut of the one chosen before
                os.remove(old)
            except OSError:
                pass
        ent["chosen"], ent["bmp"] = path, bmp
        props[key_] = os.path.normpath(bmp)
        persist_props()
        refresh_table_only()
        auto_status(f"Proposed for {r['name']}: {Path(bmp).name}")
        if auto_ui.get("bulk"):
            return
        if auto_hide.get():
            auto_fill()
        else:
            auto_place(r)
            auto_counts()

    def auto_discard(r: dict, only_unchosen: bool = False) -> None:
        """Delete a creature's batch pictures (all of them, or all but the chosen one) and take it off the list."""
        key_ = rowkey(r)
        ent = auto_store().get(key_) or {}
        keep = {os.path.normcase(str(ent.get("chosen") or ""))} if only_unchosen else set()
        for p in list(ent.get("paths", [])):
            if os.path.normcase(p) in keep:
                continue
            try:
                os.remove(p)
            except OSError:
                pass
            (auto_ui["imgs"]).pop(p, None)                           # type: ignore[union-attr]
        if only_unchosen:
            ent["paths"] = [p for p in ent.get("paths", []) if os.path.normcase(p) in keep]
        else:
            auto_store().pop(key_, None)
        save_settings(st)

    def auto_discard_unchosen() -> None:
        store_ = auto_store()
        done_ = [k for k, v in store_.items() if v.get("chosen") and
                 len(v.get("paths", [])) > (0 if v.get("chosen") == "keep" else 1)]
        if not done_:
            auto_status("Nothing to discard: no creature has a chosen picture with others beside it.")
            return
        if not messagebox.askyesno("Portrait Scout", f"Delete the pictures not chosen for {len(done_)} creature(s)? "
                                   "The chosen ones stay. This cannot be undone.", parent=root):
            return
        byk = {rowkey(o): o for o in all_rows()}
        for k in done_:
            auto_discard(byk.get(k) or {"dialogs": k.split(","), "name": k}, only_unchosen=True)
        auto_fill()

    def auto_now(r: dict, ent: dict) -> Tuple[object, str]:
        """What the creature has now, before the batch: the proposal it had (a file) or the portrait the game gives
        it. (picture, words under it); no picture when it has neither."""
        was = str(ent.get("was") or "")
        if was and os.path.isfile(was):
            return auto_thumb(was), "Earlier proposal"
        res_ = state["res"]
        nm = str(r.get("large_name") or r.get("small_name") or r.get("copy_port") or "")
        if not nm or not res_:
            return None, ""
        cache_: Dict[str, object] = auto_ui["imgs"]                  # type: ignore[assignment]
        ck = "game:" + nm.upper()
        if ck not in cache_:
            img = None
            try:
                data = res_["game"].get(nm, T_BMP)                   # type: ignore[index]
            except Exception:  # noqa: BLE001
                data = None
            if data:
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    im.thumbnail((AUTO_TW, AUTO_TH))
                    img = ImageTk.PhotoImage(im)
                except Exception:  # noqa: BLE001
                    img = thumb_from_bytes(data)
            cache_[ck] = img
        return cache_[ck], "Its portrait now"

    def auto_sync(r: dict, ent: dict) -> None:
        """Catch up with a proposal made or removed somewhere else (Propose, the gallery): a cut of one of these
        candidates marks that candidate; any other picture is shown on the card as proposed elsewhere."""
        key_ = rowkey(r)
        prop = str(props.get(key_) or "")
        ch = str(ent.get("chosen") or "")
        was_ = str(ent.get("was") or "")
        kept_ok = (os.path.normcase(prop) == os.path.normcase(was_)) if was_ else not prop
        if (ch == "keep" and kept_ok) or (prop and ent.get("bmp") and
                                         os.path.normcase(prop) == os.path.normcase(str(ent["bmp"]))):
            return                                                   # still as Auto left it
        if not prop:
            if ch:                                                   # taken away elsewhere
                ent.pop("chosen", None)
                ent.pop("bmp", None)
            return
        if ent.get("was") and os.path.normcase(prop) == os.path.normcase(str(ent["was"])):
            return                                                   # still what it had before the batch
        ent.pop("sel", None)                                         # chosen in Propose since: that is the pick
        #                                                              now, so Apply picks leaves it alone
        stem_ = Path(prop).stem.lower()
        for p_ in ent.get("paths", []):                              # "X_c1_..._1_169x266_z12_m0_1.bmp" is X_c1_..._1
            if stem_.startswith(Path(p_).stem.lower()):
                ent["chosen"], ent["bmp"] = p_, prop
                return
        ent["chosen"], ent["bmp"] = "elsewhere", prop

    def auto_picked(r: dict, ent: dict) -> str:
        """Which picture is the creature's now: a candidate's path, "keep", or "" (changed somewhere else)."""
        key_ = rowkey(r)
        ch = str(ent.get("chosen") or "")
        if ch == "keep":
            was = str(ent.get("was") or "")
            ok = (os.path.normcase(props.get(key_, "")) == os.path.normcase(was)) if was else key_ not in props
            return "keep" if ok else ""
        if ch and ent.get("bmp") and props.get(key_) and \
                os.path.normcase(props[key_]) == os.path.normcase(str(ent["bmp"])):
            return ch
        return ""

    def auto_own_cut(ent: dict, path: str) -> bool:
        """Whether a .bmp is Auto's own cut of one of this creature's candidates - the only kind it may delete.
        One proposed in Propose, or the creature's earlier proposal, is left alone."""
        if not path or not path.lower().endswith(".bmp"):
            return False
        st_ = os.path.normcase(os.path.splitext(path)[0])
        return any(st_.startswith(os.path.normcase(os.path.splitext(p)[0]) + "_") for p in ent.get("paths", [])) \
            and os.path.normcase(path) != os.path.normcase(str(ent.get("was") or ""))

    def auto_keep(r: dict) -> None:
        """Keep what it had: the earlier proposal back, or no proposal at all so the game's own portrait stays."""
        key_ = rowkey(r)
        ent = auto_store().setdefault(key_, {"paths": []})
        old = str(ent.get("bmp") or "")
        if old and os.path.isfile(old) and auto_own_cut(ent, old):
            try:
                os.remove(old)                                       # the cut of a candidate no longer wanted
            except OSError:
                pass
        was = str(ent.get("was") or "")
        if was and not os.path.isfile(was):                          # deleted since: nothing to put back
            was = ""
        if was:
            props[key_] = was
        else:
            props.pop(key_, None)
        ent["chosen"], ent["bmp"] = "keep", ""
        persist_props()
        refresh_table_only()
        auto_status(f"{r['name']} keeps " + ("the earlier proposal." if was else "its own portrait."))
        if auto_ui.get("bulk"):
            return
        if auto_hide.get():
            auto_fill()
        else:
            auto_place(r)
            auto_counts()

    def auto_mark(r: dict, what: str) -> None:
        """Pick a picture (or Keep) for this creature. Nothing is proposed until Apply picks; clicking the pick
        again takes it back."""
        ent = auto_store().setdefault(rowkey(r), {"paths": []})
        applied = auto_picked(r, ent)
        same = lambda a, b: os.path.normcase(str(a)) == os.path.normcase(str(b))   # noqa: E731
        if same(ent.get("sel") or "", what) or (not ent.get("sel") and applied and same(applied, what)):
            ent.pop("sel", None)                                     # clicked again: undecided
        else:
            ent["sel"] = what
        if applied and same(ent.get("sel") or "", applied):
            ent.pop("sel", None)                                     # already what is proposed
        save_settings(st)
        auto_place(r)
        auto_counts()

    def auto_pending() -> List[Tuple[dict, str]]:
        store_ = auto_store()
        byk = {rowkey(o): o for o in all_rows()}
        for k, v in store_.items():                                  # cards on other pages too: a proposal made
            if v.get("sel") and k in byk:                            # in Propose since beats the pick made here
                auto_sync(byk[k], v)
        return [(byk[k], str(v["sel"])) for k, v in store_.items() if v.get("sel") and k in byk]

    def auto_apply() -> None:
        """Propose every pick: the picture cut to the game size, or Keep."""
        todo = auto_pending()
        if not todo:
            auto_status("Nothing picked to apply. Click a picture on each card first.")
            return
        auto_ui["bulk"] = True                                       # one redraw at the end, not one per card
        try:
            for r, what in todo:
                if what == "keep":
                    auto_keep(r)
                elif os.path.isfile(what):
                    auto_choose(r, what)
                (auto_store().get(rowkey(r)) or {}).pop("sel", None)
        finally:
            auto_ui["bulk"] = False
        save_settings(st)
        if auto_hide.get():                                          # picked cards leave the page: lay it out again
            auto_fill()
        else:                                                        # otherwise only the cards that changed
            cards_ = auto_ui.get("cards") or {}
            for r, _what in todo:
                if rowkey(r) in cards_:                              # type: ignore[operator]
                    auto_place(r)
            auto_counts()
        auto_status(f"Applied {len(todo)} pick(s).")

    def auto_delete(r: dict, path: str) -> None:
        """Delete one candidate. A proposal already cut from it stays (that is its own .bmp)."""
        if not messagebox.askyesno("Portrait Scout", f"Delete this picture of {r['name']}?\n\n{Path(path).name}\n\n"
                                   "This cannot be undone.", parent=root):
            return
        ent = auto_store().get(rowkey(r)) or {}
        try:
            os.remove(path)
        except OSError as e:
            auto_status(f"Could not delete it: {e}")
            return
        (auto_ui["imgs"]).pop(path, None)                            # type: ignore[union-attr]
        ent["paths"] = [p for p in ent.get("paths", []) if os.path.normcase(p) != os.path.normcase(path)]
        if os.path.normcase(str(ent.get("sel") or "")) == os.path.normcase(path):
            ent.pop("sel", None)
        save_settings(st)
        auto_status(f"Deleted {Path(path).name}.")
        auto_place(r)
        auto_counts()

    def pool_chooser(parent: object, mine: Tuple[str, str], on_take: Callable[[Path], None],
                     say: Callable[[str], None]) -> None:
        """Take one of the spare pictures already made (Propose and the Auto view). The ones made for a creature of
        the same race and sex come first. on_take gets the picture chosen; say reports."""
        generic = not any(mine)
        spare = list(spare_pictures(pool_folder(), pool_used()))
        if not spare:
            say("There are no spare pictures yet. Generate some, and the ones you do not use land here.")
            return
        who = pool_who()
        fit = lambda q: who.get(made_from(q.name), ("", "")) == mine  # noqa: E731
        w = tk.Toplevel(parent)
        w.title("Pick from the pool")
        w.transient(parent)
        esc_closes(w)
        only = tk.BooleanVar(value=any(fit(q) for q in spare) and not generic)
        top_ = ttk.Frame(w, padding=(10, 8, 10, 0))
        top_.pack(fill="x")
        said = tk.StringVar()
        ttk.Label(top_, textvariable=said).pack(side="left")
        drop_v = tk.BooleanVar(value=bool(st.get("pool_drop_used")))
        db_ = ttk.Checkbutton(top_, text="Delete the spare", variable=drop_v,
                              command=lambda: (st.__setitem__("pool_drop_used", drop_v.get()),
                                               save_settings(st)))
        db_.pack(side="right", padx=(10, 0))
        tooltip(db_, "Delete the picture from the pool once it is used. It leaves the pool either way - "
                     "this throws the file away as well, so the proposal cannot be undone back to it.")
        cb_ = ttk.Checkbutton(top_, text=f"Only {' '.join(x for x in mine if x) or 'these'}", variable=only)
        if not generic:
            cb_.pack(side="right")
            tooltip(cb_, "Show only the pictures made for a creature of the same race and sex, which is "
                         "who is in them.")
        cv = tk.Canvas(w, bg=CARD, highlightthickness=0, width=5 * 170, height=3 * 230)
        sb = ttk.Scrollbar(w, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb.pack(side="right", fill="y", pady=10)
        inner = tk.Frame(cv, bg=CARD)
        cv.create_window((0, 0), window=inner, anchor="nw")
        hold: List[object] = []
        ticked: Dict[str, tk.BooleanVar] = {}
        cur = {"i": 0, "cells": []}                                  # the keyboard's place: (cell, picture)

        def spare_gone(q: Path) -> None:
            """Delete a spare from inside the chooser: the ones that came out wrong go for good."""
            spares_gone([q])

        def spares_gone(qs: List[Path]) -> None:
            if not qs or not messagebox.askyesno(
                    "Portrait Scout", (f"Delete {qs[0].name} from the disk?" if len(qs) == 1 else
                                       f"Delete the {len(qs):,} ticked pictures from the disk?") +
                    "\n\nThis cannot be undone.", parent=w):
                return
            went = bad = 0
            for q in qs:
                for f in {str(q)} | {str(x) for x in q.parent.glob(plain_of(str(q)).stem + "_*")
                                     if x.is_file() and x.suffix.lower() in POOL_EXT}:
                    try:
                        os.remove(f)
                        went += 1
                    except FileNotFoundError:
                        pass
                    except OSError:
                        bad += 1
                if q in spare:
                    spare.remove(q)
                ticked.pop(str(q), None)
            state["pool_files"] = None                           # the Pool tab reads the folder again
            fill()
            said.set(f"Deleted {went:,} file(s)." + (f" {bad:,} could not be deleted." if bad else ""))

        def shown() -> List[Path]:
            return ([q for q in spare if fit(q)] if only.get() else list(spare))[:300]

        def tick_all() -> None:
            on_ = not all(v.get() for v in ticked.values()) if ticked else True
            for v in ticked.values():
                v.set(on_)
            tick_b.config(text="Untick all" if on_ else "Tick all")

        tick_b = ttk.Button(top_, text="Tick all", command=lambda: tick_all())
        del_b = ttk.Button(top_, text="Delete ticked",
                           command=lambda: spares_gone([q for q in shown() if ticked.get(str(q)) and ticked[str(q)].get()]))
        del_b.pack(side="right", padx=(10, 0))
        tick_b.pack(side="right", padx=(10, 0))
        tooltip(del_b, "Delete the ticked pictures from the disk, with the copies made from them.")
        tooltip(tick_b, "Tick or untick every picture shown.")

        def spare_menu(event: object, q: Path) -> None:
            m = tk.Menu(w, tearoff=0)
            m.add_command(label=f"Use {q.name}", command=lambda: took(q))
            m.add_separator()
            m.add_command(label="Delete this picture", command=lambda: spare_gone(q))
            n_ = sum(1 for v in ticked.values() if v.get())
            if n_:
                m.add_command(label=f"Delete the {n_:,} ticked", command=lambda: spares_gone(
                    [y for y in shown() if ticked.get(str(y)) and ticked[str(y)].get()]))
            try:
                m.tk_popup(event.x_root, event.y_root)           # type: ignore[attr-defined]
            finally:
                m.grab_release()

        def took(q: Path) -> None:
            w.destroy()
            on_take(q)
            if drop_v.get():
                try:
                    os.remove(q)
                    say(f"{q.name} was used and deleted from the pool.")
                except OSError as e:
                    say(f"{q.name} was used, but it could not be deleted ({e}).")

        def fill() -> None:
            for x in inner.winfo_children():
                x.destroy()
            hold.clear()
            lot = [q for q in spare if fit(q)] if only.get() else list(spare)
            said.set(f"{len(lot):,} spare picture(s) - click one")
            keep_ = {k: v.get() for k, v in ticked.items()}
            ticked.clear()
            cur["cells"] = []
            for i, q in enumerate(lot[:300]):
                cell = tk.Frame(inner, bg=CARD, padx=4, pady=4, highlightthickness=3,
                                highlightbackground=CARD, highlightcolor=CARD)
                cur["cells"].append((cell, q))
                cell.grid(row=i // 5, column=i % 5, padx=4, pady=4, sticky="n")
                im = load_thumb(str(q))
                lb_ = tk.Label(cell, image=im, bg=CARD, cursor="hand2") if im else \
                    tk.Label(cell, text="(no preview)", bg=CARD, width=18, height=6)
                if im:
                    hold.append(im)
                lb_.pack()
                nm_ = tk.Label(cell, text=made_for(q.name), bg=CARD, wraplength=150,
                               font=("TkDefaultFont", 9, "bold"))
                nm_.pack()
                tv_ = tk.BooleanVar(value=keep_.get(str(q), False))
                ticked[str(q)] = tv_
                tk.Checkbutton(cell, text="Tick", variable=tv_, bg=CARD, activebackground=CARD,
                               takefocus=0).pack()
                for x in (cell, lb_, nm_):
                    x.bind("<Button-1>", lambda e, y=q: took(y))
                    x.bind("<Button-3>", lambda e, y=q: spare_menu(e, y))
                tooltip(lb_, f"{q.name}\nClick to use it, right-click to delete it, tick to delete several.")
            wheel_over(inner, cv)
            inner.update_idletasks()
            cv.configure(scrollregion=cv.bbox("all"))
            cur["i"] = min(cur["i"], max(0, len(cur["cells"]) - 1))
            mark()

        def mark(see: bool = False) -> None:
            for k_, (c_, _q) in enumerate(cur["cells"]):
                col_ = "#1a5fb4" if k_ == cur["i"] else CARD
                try:
                    c_.config(highlightbackground=col_, highlightcolor=col_)
                except tk.TclError:
                    pass
            if see and cur["cells"]:
                c_ = cur["cells"][cur["i"]][0]
                tot_ = max(1, inner.winfo_height())
                y0_, y1_ = c_.winfo_y() / tot_, (c_.winfo_y() + c_.winfo_height()) / tot_
                top_, bot_ = cv.yview()
                if y0_ < top_:
                    cv.yview_moveto(y0_)
                elif y1_ > bot_:
                    cv.yview_moveto(max(0.0, y1_ - (bot_ - top_)))

        def key(e: object) -> str:
            """Arrows move, Space ticks, Enter uses it, Delete deletes the ticked ones (or this one)."""
            n_ = len(cur["cells"])
            if not n_:
                return "break"
            ks = getattr(e, "keysym", "")
            step = {"Left": -1, "Right": 1, "Up": -5, "Down": 5, "Prior": -15, "Next": 15}.get(ks)
            if step is not None:
                cur["i"] = max(0, min(n_ - 1, cur["i"] + step))
            elif ks == "Home":
                cur["i"] = 0
            elif ks == "End":
                cur["i"] = n_ - 1
            elif ks == "space":
                v_ = ticked.get(str(cur["cells"][cur["i"]][1]))
                if v_ is not None:
                    v_.set(not v_.get())
                cur["i"] = min(n_ - 1, cur["i"] + 1)
            elif ks in ("Return", "KP_Enter"):
                took(cur["cells"][cur["i"]][1])
                return "break"
            elif ks == "Delete":
                many_ = [y for _c, y in cur["cells"] if ticked.get(str(y)) and ticked[str(y)].get()]
                spares_gone(many_ or [cur["cells"][cur["i"]][1]])
                return "break"
            else:
                return ""
            mark(see=True)
            return "break"
        for ks_ in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End", "space", "Return", "KP_Enter", "Delete"):
            w.bind(f"<{ks_}>", key)
        cb_.config(command=fill)
        wheel_over(cv, cv)
        wheel_over(w, cv)
        fill()
        state["pool_hold"] = hold                                # Tk drops a picture nothing holds
        w.grab_set()
        w.focus_set()

    def auto_from_pool(r: dict) -> None:
        """A spare from the pool as one more choice on a creature's card (a copy, as the Propose tab takes it)."""
        def take(q: Path) -> None:
            pg_ = generator_module()
            gs_ = pg_.load_settings() if pg_ else {}                 # type: ignore[attr-defined]
            fold = auto_folder(gs_)
            dst = fold / f"{auto_stem(r)}_p{int(time.time())}{q.suffix.lower()}"
            try:
                fold.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(q, dst)
            except OSError as e_:
                auto_status(f"Could not copy {q.name} ({e_}).")
                return
            st.setdefault("pool_from", {})[os.path.normcase(str(dst))] = os.path.normcase(os.path.normpath(str(q)))
            ent = auto_store().setdefault(rowkey(r), {"paths": []})
            auto_default(r, ent)
            if "was" not in ent:
                ent["was"] = str(props.get(rowkey(r)) or "")
            ent.setdefault("paths", []).append(str(dst))
            state["pool_files"] = None                               # the Pool tab reads the folder again
            save_settings(st)
            auto_place(r)
            auto_counts()
            auto_status(f"{q.name} added to {r['name']}'s card from the pool.")
        pool_chooser(root, (str(r.get("race") or ""), str(r.get("sex") or "")), take, auto_status)

    def auto_menu(e: object, r: dict, what: str) -> None:
        """Right-click on a picture in the Auto view."""
        ent = auto_store().get(rowkey(r)) or {}
        m = tk.Menu(root, tearoff=0)
        cand = what not in ("keep", "elsewhere") and os.path.isfile(what)
        file_ = what if cand else str(ent.get("was") or "") if what == "keep" else str(ent.get("bmp") or "")
        picked_ = os.path.normcase(str(ent.get("sel") or "")) == os.path.normcase(what)
        if what != "elsewhere":
            m.add_command(label="Take the pick back" if picked_ else ("Keep this" if what == "keep" else "Pick this one"),
                          command=lambda: auto_mark(r, what))
        if file_ and os.path.isfile(file_):
            m.add_command(label="Open full size", command=lambda: open_path(file_))
            m.add_command(label="Show in folder", command=lambda: (
                subprocess.Popen(["explorer", "/select,", os.path.normpath(file_)]) if os.name == "nt"
                else open_path(str(Path(file_).parent))))
        if cand:
            sd_ = image_seed(what)
            if sd_ is not None:
                m.add_command(label=f"Copy seed ({sd_})", command=lambda: (root.clipboard_clear(),
                                                                         root.clipboard_append(str(sd_)),
                                                                         auto_status(f"Copied seed {sd_}.")))
            m.add_separator()
            m.add_command(label="One more picture", command=lambda: auto_start([r], ask=False, count=1))
            def replace_() -> None:
                auto_delete(r, what)                                 # asks first
                if not os.path.isfile(what):                         # deleted: make one in its place
                    auto_start([r], ask=False, count=1)
            m.add_command(label="Replace with a new one...", command=replace_)
            m.add_command(label="Delete this picture...", command=lambda: auto_delete(r, what))
        m.add_separator()
        m.add_command(label="Add from the pool...", command=lambda: auto_from_pool(r))
        m.add_command(label="Open in Propose", command=lambda: auto_open(r))
        try:
            m.tk_popup(e.x_root, e.y_root)                           # type: ignore[attr-defined]
        finally:
            m.grab_release()

    def auto_card(parent: object, r: dict, ent: dict) -> object:
        card = tk.Frame(parent, bg=CARD, bd=1, relief="solid", padx=6, pady=6)   # type: ignore[arg-type]
        info = tk.Frame(card, bg=CARD, width=150, height=AUTO_TH + 30)
        info.pack(side="left", fill="y", padx=(0, 6))
        info.pack_propagate(False)
        nm_lab = tk.Label(info, text=str(r.get("name") or "?"), bg=CARD, font=("TkDefaultFont", 10, "bold"),
                          wraplength=145, justify="left", anchor="w", cursor="hand2")
        nm_lab.pack(fill="x")
        nm_lab.bind("<Button-1>", lambda e: copy_text(str(r.get("name") or "")))
        cre_ = ", ".join(r.get("cre_list") or [])
        if cre_:
            nm_lab.bind("<Button-3>", lambda e: copy_text(cre_))
        tooltip(nm_lab, "Click: copy the name. Right-click: copy the creature file name" + (f" ({cre_})." if cre_ else "."))
        sub = " ".join(x for x in (shown_sex(r.get("sex")).title(), str(r.get("race") or "").title()) if x)
        tk.Label(info, text=(sub + "\n" if sub else "") + str(r.get("anim") or ""), bg=CARD, fg="#555",
                 wraplength=145, justify="left", anchor="w").pack(fill="x")
        ln_ = tk.Label(info, text=f"{int(r.get('lines', 0) or 0)} lines", bg=CARD, fg="#1a5fb4", anchor="w",
                       cursor="hand2", font=("TkDefaultFont", 9, "underline"))
        ln_.pack(fill="x", pady=(2, 6))
        ln_.bind("<Button-1>", lambda e: show_lines(r))              # what they say, and play it: as in the gallery
        picked = auto_picked(r, ent)
        if False:
            tk.Label(info, text="Proposal changed elsewhere", bg=CARD, fg="#b45309", wraplength=145,
                     justify="left", anchor="w").pack(fill="x")
        bb = tk.Frame(info, bg=CARD)
        bb.pack(side="bottom", fill="x")
        crop_ = tk.Frame(bb, bg=CARD)
        crop_.pack(fill="x", pady=(0, 6))
        _bw, _bh, zm_, dx_, dy_ = auto_cut(r, ent)
        locked = bool(ent.get("crop_locked"))
        tk.Label(crop_, text=f"Crop  {zm_:.2f}x" + ("  \u2714 set" if locked else "  (own)" if ent.get("crop") else ""),
                 bg=CARD, fg="#1a7f37" if locked else "#8a6d00", anchor="w").pack(fill="x")
        if locked:
            eb_ = ttk.Button(crop_, text="Edit crop", command=lambda: auto_lock(r, False))
            eb_.pack(fill="x")
            tooltip(eb_, "Show the whole pictures with the yellow box again, to move it.")
        c1_ = tk.Frame(crop_, bg=CARD)
        c2_ = tk.Frame(crop_, bg=CARD)
        if not locked:                                               # locked: built, but not shown
            c1_.pack(fill="x")
            c2_.pack(fill="x", pady=(1, 0))
        for parent_, items in ((c1_, (("\u2212", -0.1, 0, 0, "Zoom out: keep more of the picture"),
                                      ("+", 0.1, 0, 0, "Zoom in: closer on the face"),
                                      ("Mid", 0, 0, 0, "Back to the crop Propose uses"))),
                               (c2_, (("\u25c0", 0, -0.02, 0, "Move the box left"), ("\u25b6", 0, 0.02, 0, "Move the box right"),
                                      ("\u25b2", 0, 0, -0.02, "Move the box up"), ("\u25bc", 0, 0, 0.02, "Move the box down")))):
            for txt_, z_, x_, y_, tip_ in items:
                b_ = ttk.Button(parent_, text=txt_, width=4 if txt_ == "Mid" else 2,
                                command=lambda z=z_, x=x_, y=y_, t=txt_: auto_crop(r, z, x, y, reset=(t == "Mid")))
                b_.pack(side="left", padx=(0, 1))
                tooltip(b_, tip_ + ". The yellow box shows what the portrait keeps; a proposed one is cut again.")
        if not locked:
            sb_ = ttk.Button(crop_, text="\u2714 Set crop", command=lambda: auto_lock(r, True))
            sb_.pack(fill="x", pady=(2, 0))
            tooltip(sb_, "Lock this crop in and show the pictures cut as the portrait will be. Edit crop undoes it.")
        rb = ttk.Button(bb, text="Reroll", command=lambda: auto_reroll(r))
        rb.pack(fill="x")
        tooltip(rb, "Throw these away and make new ones. The one picked now stays picked until you click another.")
        ob = ttk.Button(bb, text="Open in Propose", command=lambda: auto_open(r))
        ob.pack(fill="x", pady=2)
        tooltip(ob, "Work on this one by hand: its description, style, touch-ups. The pictures are there too.")
        db = ttk.Button(bb, text="Discard", command=lambda: (auto_discard(r), auto_fill()))
        db.pack(fill="x")
        tooltip(db, "Delete these pictures and take it off this list. Whatever is proposed for it stays.")

        sel = str(ent.get("sel") or "")
        same = lambda a, b: bool(a) and os.path.normcase(str(a)) == os.path.normcase(str(b))   # noqa: E731

        def tile(img: object, what: str, label: str, tip: str, right: object = None, name: str = "") -> None:
            if same(sel, what):
                bg_, fg_, txt = "#1a5fb4", "white", "\u25cf Picked " + label        # to apply
            elif same(picked, what) and not sel:
                bg_, fg_, txt = "#1a7f37", "white", "\u2714 Proposed " + label
            elif same(picked, what):
                bg_, fg_, txt = CARD, "#1a7f37", "Proposed now " + label       # Apply swaps it for the pick
            else:
                bg_, fg_, txt = CARD, "#555", "Pick " + label
            if what == "keep":                                       # Keep is not a letter
                txt = {"#1a5fb4": "\u25cf Keep", "#1a7f37": "\u2714 Kept"}.get(bg_, "Keep: " + now_txt.lower())
            if what == "elsewhere":
                txt = "\u2714 From Propose" if bg_ == "#1a7f37" else "From Propose (until Apply)"
            on = bg_ != CARD
            click = (lambda: None) if what == "elsewhere" else (lambda: auto_mark(r, what))   # noqa: E731
            box = tk.Frame(card, bg=bg_, padx=4, pady=4, cursor="hand2")
            box.pack(side="left", padx=2, anchor="n")
            src_ = what if what not in ("keep", "elsewhere") else \
                auto_src(str(ent.get("bmp") or "") if what == "elsewhere" else str(ent.get("was") or ""))
            if src_ and what in ("keep", "elsewhere") and os.path.isfile(src_):
                img = auto_thumb(src_)                               # the picture it was cut from, so the crop
            cut_ = locked and bool(src_) and os.path.isfile(src_)    # box works on it too
            if cut_:
                img = auto_thumb_cut(r, ent, src_)
            if img is not None:
                lab = tk.Canvas(box, width=img.width(), height=img.height(), highlightthickness=0, bd=0,   # type: ignore[attr-defined]
                                cursor="hand2")
                lab.create_image(0, 0, image=img, anchor="nw")
                bx = auto_box(r, ent, src_) if src_ and os.path.isfile(src_) and not cut_ else None
                if bx:
                    k = img.width() / float(bx[4] or 1)               # type: ignore[attr-defined]
                    lab.create_rectangle(bx[0] * k, bx[1] * k, (bx[0] + bx[2]) * k - 1, (bx[1] + bx[3]) * k - 1,
                                         outline="#ffd000", width=2)
            else:
                lab = tk.Label(box, text=name, bg=bg_, bd=0, wraplength=AUTO_TW, cursor="hand2")
            lab.image = img                                          # type: ignore[attr-defined]
            lab.pack()
            cap = tk.Label(box, text=txt, bg=bg_, fg=fg_, font=("TkDefaultFont", 10, "bold" if on else "normal"),
                           cursor="hand2")
            cap.pack(fill="x", pady=(3, 0))
            for w in (box, lab, cap):
                w.bind("<Button-1>", lambda e: click())             # type: ignore[operator]
                w.bind("<Button-3>", lambda e: auto_menu(e, r, what))   # no tooltip: the pictures are
                #                                                      redrawn under the mouse while a batch runs
        if picked == "elsewhere":
            ep_ = str(ent.get("bmp") or "")
            tile(auto_thumb(ep_) if os.path.isfile(ep_) else None, "elsewhere", "", "Proposed in the Propose tab "
                 "(or the gallery). Pick one of the others and Apply to replace it.", lambda: open_path(ep_),
                 Path(ep_).name)
        now_img, now_txt = auto_now(r, ent)
        if now_img is not None:                                      # nothing left to show (its earlier proposal
            #                                                          was deleted since): nothing to keep either
            tile(now_img, "keep", "(keep)", "Keep what it has now and use none of the new ones: "
                 + (now_txt.lower() or "its portrait") + ". Click again to take the pick back.")
        for i, p in enumerate([p for p in ent.get("paths", []) if os.path.isfile(p)]):
            letter = "ABCDEFGH"[i] if i < 8 else str(i + 1)
            tile(auto_thumb(p), p, letter, "Click: pick this one (Apply picks proposes it). Click again to take "
                 "it back. Right-click for more: full size, one more, replace, delete.", lambda q=p: open_path(q), Path(p).name)
        return card

    def auto_fill() -> None:
        """Lay the batch out again: two creatures to a row, in the order they are listed."""
        if not auto_ui.get("built"):
            return
        for w in auto_ui["grid"].winfo_children():
            w.destroy()
        auto_ui["cards"], auto_ui["empty"] = {}, None
        if not state["res"]:                                         # at startup: nothing analyzed, and the row
            auto_ui["empty"] = ttk.Label(auto_ui["grid"], text="Analyze a mod first.", foreground="#555")   # helpers
            auto_ui["empty"].grid(row=0, column=0, sticky="w", pady=8)   # type: ignore[union-attr]   are not built yet
            return
        store_ = auto_store()
        byk = {rowkey(o): o for o in all_rows()}
        order = {rowkey(o): i for i, o in enumerate(sorted_rows(False))}
        listed_ = {rowkey(o) for o in sorted_rows()}                 # the search box and the filters, as the
        #                                                              gallery has them ("1 of 489")
        if search_var.get().strip():                                 # a search shows whoever it finds, even
            for k_ in listed_:                                       # with nothing made yet: a card to Reroll
                if k_ and k_ in byk and k_ not in store_:            # from, with what it has now as Keep
                    store_[k_] = {"paths": []}
            searched_ = listed_
        else:
            searched_ = set()
        keys = [k for k, v in store_.items() if k in byk and k in listed_ and (k in searched_ or v.get("chosen") == "keep" or
                                                              k in props or      # its spares deleted, its proposal
                                                              any(os.path.isfile(p) for p in v.get("paths", [])))]   # stays
        if auto_hide.get():
            keys = [k for k in keys if not (auto_picked(byk[k], store_[k]) or store_[k].get("sel"))]
        keys.sort(key=lambda k: order.get(k, 1 << 30))
        per = AUTO_PAGE                                              # a page at a time: hundreds of cards were
        pages = max(1, -(-len(keys) // per))                         # thousands of widgets, slow to lay out and
        pg_n = max(0, min(int(auto_ui.get("page") or 0), pages - 1)) # flashing at the top left on every resize
        auto_ui["page"], auto_ui["pages"] = pg_n, pages
        total_ = len(keys)
        auto_page_var.set(f"Page {pg_n + 1} of {pages}  ({total_})" if pages > 1 else f"{total_} creature(s)")
        keys = keys[pg_n * per:(pg_n + 1) * per]
        if auto_ui.get("navtop") is not None:
            for w_ in auto_ui["navtop"].winfo_children():            # type: ignore[union-attr]
                w_.destroy()
            auto_nav(auto_ui["navtop"], pg_n, pages, per, total_)   # same as the gallery's: top and bottom
        if pages > 1 and keys:
            bot_ = tk.Frame(auto_ui["grid"], bg=PANEL)
            bot_.grid(row=(len(keys) + 1) // 2 + 1, column=0, columnspan=2, pady=8)
            auto_nav(bot_, pg_n, pages, per, total_)
        try:
            auto_ui["canvas"].yview_moveto(0) if auto_ui.get("paged") else None   # type: ignore[union-attr]
        except tk.TclError:
            pass
        auto_ui["paged"] = False
        if not keys:
            auto_ui["empty"] = ttk.Label(auto_ui["grid"], text=("Nothing made yet. Press Generate to make pictures for every creature listed "
                                   "(the filters above say which), or untick Expanded beside Gallery for the cards." if state["res"] else
                                   "Analyze a mod first."), foreground="#555")
            auto_ui["empty"].grid(row=0, column=0, sticky="w", pady=8)   # type: ignore[union-attr]
        gen_ = int(auto_ui.get("fill_gen") or 0) + 1                # a newer fill makes this one stop
        auto_ui["fill_gen"] = gen_

        def some(start: int) -> None:                                # a few cards at a time, so the window shows
            if auto_ui.get("fill_gen") != gen_:                      # at once and stays responsive while the
                return                                               # pictures load
            for i in range(start, min(start + 6, len(keys))):
                auto_place(byk[keys[i]], i)
            if start + 6 < len(keys):
                root.after(1, some, start + 6)
            else:
                auto_counts()
        some(0)
        auto_counts()

    def auto_fill_soon() -> None:
        """auto_fill once, however many times it is asked for in a row (an analysis asks several times)."""
        if auto_ui.get("fill_job"):
            return
        def run_() -> None:
            auto_ui["fill_job"] = None
            auto_fill()
        auto_ui["fill_job"] = root.after(30, run_)

    def auto_place(r: dict, slot: int = -1) -> None:
        """One creature's card, in its own slot (or the next free one): a finished creature or a new choice
        redraws that card alone, not the whole list."""
        cards: Dict[str, Tuple[object, int]] = auto_ui["cards"]     # type: ignore[assignment]
        key_ = rowkey(r)
        if key_ in cards:
            old, slot = cards[key_]
            old.destroy()                                            # type: ignore[attr-defined]
        elif slot < 0:                                               # new: only if this page has room for it
            if len(cards) >= AUTO_PAGE:
                return
            slot = max((s_ for _w, s_ in cards.values()), default=-1) + 1
        if auto_ui.get("empty") is not None:
            auto_ui["empty"].destroy()                               # type: ignore[union-attr]
            auto_ui["empty"] = None
        ent = auto_store().get(key_)
        if not ent:
            cards.pop(key_, None)
            return
        auto_sync(r, ent)
        auto_default(r, ent)                                         # cards made before this existed get it too
        w = auto_card(auto_ui["grid"], r, ent)
        w.grid(row=slot // 2, column=slot % 2, sticky="nsew", padx=4, pady=4)   # type: ignore[attr-defined]
        cards[key_] = (w, slot)
        auto_catch(w)

    def auto_open(r: dict) -> None:
        view_var.set("generate")
        state["gen_built"] = True
        show_view()
        generate_dialog(r)

    def auto_reroll(r: dict) -> None:
        ent = auto_store().get(rowkey(r)) or {}
        chosen_ = str(ent.get("chosen") or "")
        for p in [p for p in ent.get("paths", []) if os.path.normcase(p) != os.path.normcase(chosen_)]:
            try:
                os.remove(p)
            except OSError:
                pass
            (auto_ui["imgs"]).pop(p, None)                           # type: ignore[union-attr]
        ent["paths"] = [chosen_] if chosen_ and os.path.isfile(chosen_) else []
        if ent.get("sel") and ent["sel"] != "keep" and not os.path.isfile(str(ent["sel"])):
            ent.pop("sel", None)
        save_settings(st)
        auto_place(r)                                                # stays in its place, empty until the new ones come
        auto_status(f"Rerolling {r['name']}...")
        auto_start([r], ask=False)

    def auto_go() -> None:
        if auto_ui.get("busy"):
            auto_stop()
            return
        if not state["res"]:
            auto_status("Analyze a mod first.")
            return
        rows_ = auto_rows()
        if not rows_:
            auto_status("Every creature listed already has a proposal or pictures from a batch.")
            return
        auto_start(rows_, ask=True)

    def auto_start(rows_: List[dict], ask: bool, count: Optional[int] = None) -> None:
        pg_ = generator_module()
        if pg_ is None:
            auto_status("The generator folder is missing: see generator/README.md.")
            return
        try:
            n = max(1, min(8, int(auto_n.get())))
        except (tk.TclError, ValueError):
            n = 2
        st["auto_count"] = n
        save_settings(st)
        if ask and not messagebox.askyesno(
                "Portrait Scout", f"Make {n} picture(s) each for {len(rows_)} creature(s)? That is {n * len(rows_)} "
                f"pictures, about {max(1, round(n * len(rows_) * 3 / 60))} minute(s) on a fast engine.\n\n"
                "They are made the way the Propose tab makes them: each creature's own description, colors and "
                "face, with Vary faces on. Nothing is proposed until you click one.", parent=root):
            return
        queue: List[object] = auto_ui["queue"]                       # type: ignore[assignment]
        queue.extend([(r_, count) for r_ in rows_] if count else rows_)
        if auto_ui.get("busy"):
            auto_status(f"Added to the queue: {len(queue)} waiting.")
            return
        gs_ = pg_.load_settings()                                    # type: ignore[attr-defined]
        auto_ui["gs"] = gs_                                          # the crop reads the settings as they are now
        if state.get("engine") is None:
            state["engine"] = pg_.Engine(gs_)                         # type: ignore[attr-defined]
        eng = state["engine"]
        eng.log = lambda m: root.after(0, status.set, "Image engine: " + str(m))   # type: ignore[attr-defined]
        eng.progress = lambda frac, text: None                       # type: ignore[attr-defined]
        auto_ui["busy"] = True
        auto_ui["stop"] = threading.Event()
        auto_ui["btn"].config(text="Stop", bg="#b5452f", activebackground="#8f3524")
        vary_ = True                                                 # a batch is for new faces, always
        try:
            fade_ = max(0.05, min(1.5, float(gs_.get("ghost_fade", 0.55))))
        except (TypeError, ValueError):
            fade_ = 0.55
        total = [len(queue)]
        fails = [0]

        def work() -> None:
            err = ""
            done_n = 0
            try:
                while queue and not auto_ui["stop"].is_set():        # type: ignore[union-attr]
                    item_ = queue.pop(0)
                    r, k_ = item_ if isinstance(item_, tuple) else (item_, n)   # type: ignore[misc]
                    done_n += 1
                    total[0] = max(total[0], done_n + len(queue))
                    root.after(0, auto_status, f"{done_n} of {total[0]}: {r['name']}...")
                    desc_ = auto_desc(pg_, gs_, r)
                    style_ = auto_style(pg_, gs_, r)
                    ghost = fade_ if hasattr(pg_, "incorporeal") and pg_.incorporeal(desc_) else 0.0   # type: ignore[attr-defined]
                    rnd_ = random.Random()
                    for i in range(int(k_ or n)):
                        if auto_ui["stop"].is_set():                 # type: ignore[union-attr]
                            break
                        words_ = pg_.vary_words(rnd_, gs_, desc_) if vary_ and hasattr(pg_, "vary_words") else ""   # type: ignore[attr-defined]
                        pr_, ng_ = auto_prompt(pg_, gs_, style_, desc_, words_)
                        ini_, src_ = start_pick(pg_, gs_, str(r.get("race") or ""), desc_, pr_, r)
                        imgs_ = None
                        for try_ in range(2):                        # the engine now and then refuses one (500):
                            try:                                     # try once more, then move on
                                imgs_ = (eng.generate(pr_, ng_, 1, init=ini_,   # type: ignore[attr-defined]
                                                      strength=start_strength(pg_, gs_, desc_, str(r.get("race") or "")),
                                                      steps=start_steps(pg_, gs_, desc_, str(r.get("race") or "")),
                                                      **start_edit(pg_, gs_, desc_, str(r.get("race") or ""),
                                                                   str(r.get("anim") or ""), src_))
                                         if ini_ else eng.generate(pr_, ng_, 1))   # type: ignore[attr-defined]
                                fails[0] = 0
                                break
                            except Exception as e_:  # noqa: BLE001
                                if try_ or "(500)" not in str(e_):
                                    fails[0] += 1
                                    root.after(0, auto_status, f"{r['name']}: skipped ({str(e_)[:120]})")
                                    if fails[0] >= 3:                # three in a row: the engine is in trouble
                                        raise
                                time.sleep(2)
                        if not imgs_ or auto_ui["stop"].is_set():    # type: ignore[union-attr]
                            break                                    # Stop interrupts the picture: not kept half-made
                        pth = pg_.save_candidates(imgs_[:1], auto_folder(gs_), f"{auto_stem(r)}_c{i + 1}", ghost)[0]   # type: ignore[attr-defined]
                        root.after(0, auto_got, r, pth, words_, src_)
            except Exception as e:  # noqa: BLE001 - the engine's messages are meant for the user
                err = str(e) or e.__class__.__name__
            root.after(0, auto_finished, err)
        threading.Thread(target=work, daemon=True).start()

    def auto_default_keep(r: dict) -> bool:
        """Whether a creature should start on Keep: one the game already has, with lines added and a portrait of
        its own; or a copy of a named character that is the same person (Face kept). A copy made into someone new
        (Shandalar copied as "Bob the Wizard") gets a new portrait like any of the mod's own."""
        if r.get("copy_port"):
            return face_kept(r)
        return bool(r.get("patched")) and bool(r.get("large") or r.get("small"))

    def auto_default(r: dict, ent: dict) -> None:
        """Start a creature on Keep where auto_default_keep says so - once: taking the pick back sticks."""
        if ent.get("default_done"):
            return
        ent["default_done"] = True
        if not ent.get("chosen") and "sel" not in ent and auto_default_keep(r):
            ent["sel"] = "keep"                                      # picked for you; Apply picks keeps it

    def auto_got(r: dict, path: str, words_: str, src_: str = "") -> None:
        ent = auto_store().setdefault(rowkey(r), {"paths": []})
        auto_default(r, ent)
        if "was" not in ent:                                         # its proposal from before the batch: shown as
            ent["was"] = str(props.get(rowkey(r)) or "")             # Keep, and kept until a pick is applied
        ent.setdefault("paths", []).append(path)
        if words_:                                                   # so "Use this face" in Propose brings it back
            vw_ = st.setdefault("vary_words", {})
            vw_[Path(path).name] = words_                            # type: ignore[index]
        start_note(path, src_)
        save_settings(st)
        if auto_ui.get("built") and tab_auto.winfo_ismapped():
            n_ = len(ent["paths"])
            try:
                want = max(1, int(auto_n.get()))
            except (tk.TclError, ValueError):
                want = 2
            if n_ >= want or n_ == 1:                                # a card when it has its pictures, not per picture
                auto_place(r)
                auto_counts()

    def auto_stop() -> None:
        ev = auto_ui.get("stop")
        if ev is not None:
            ev.set()                                                 # type: ignore[union-attr]
        auto_ui["queue"].clear()                                     # type: ignore[union-attr]
        auto_ui["btn"].config(text="Stopping...", bg="#9bbfa6")
        eng = state.get("engine")
        if eng is not None and hasattr(eng, "cancel"):
            threading.Thread(target=eng.cancel, daemon=True).start()   # type: ignore[union-attr]
        auto_status("Stopping after the picture being painted...")

    def auto_finished(err: str) -> None:
        auto_ui["busy"] = False
        auto_ui["btn"].config(text="Generate", bg="#2e9e4f", activebackground="#25803f")
        stopped = bool(auto_ui.get("stop") and auto_ui["stop"].is_set())   # type: ignore[union-attr]
        auto_status(err if err else "Stopped. Generate carries on where it left off." if stopped else
                    "Done. Click the picture you want for each creature.")
        auto_counts()                                                # each card was drawn as its pictures came in:
        #                                                              no redraw of the whole page at the end

    def build_auto() -> None:
        auto_ui["gs"] = None                                         # settings may have been edited since
        if auto_ui.get("built"):
            auto_fill()
            return
        bar = ttk.Frame(tab_auto)
        bar.pack(fill="x", pady=(2, 4))
        btn = tk.Button(bar, text="Generate", bg="#2e9e4f", fg="white", activebackground="#25803f",
                        activeforeground="white", font=("TkDefaultFont", 10, "bold"), padx=12, relief="raised",
                        command=auto_go)
        btn.pack(side="left")
        tooltip(btn, "Make pictures for every creature listed (the filters above say which) that has no proposal "
                     "yet - or every one, with Redo proposed. Stop and Generate again carries on where it left off.")
        ap = tk.Button(bar, text="Apply picks", bg="#1a5fb4", fg="white", activebackground="#154a8c",
                       activeforeground="white", disabledforeground="#b7c9e2", font=("TkDefaultFont", 10, "bold"),
                       padx=10, relief="raised", state="disabled", command=lambda: auto_apply())
        ap.pack(side="left", padx=(8, 0))
        tooltip(ap, "Propose every picture picked (blue), cut to the game size. Picks are kept until then, "
                    "between sessions too.")
        auto_ui["apply"] = ap
        ttk.Label(bar, text="Each:").pack(side="left", padx=(14, 4))
        sp = ttk.Spinbox(bar, from_=1, to=8, width=3, textvariable=auto_n)
        sp.pack(side="left")
        tooltip(sp, "Pictures per creature.")
        start_controls(bar)
        c1 = ttk.Checkbutton(bar, text="Skip if has one", variable=auto_skip_has,
                             command=lambda: (st.__setitem__("auto_skip_has", bool(auto_skip_has.get())),
                                              save_settings(st), auto_counts()))
        c1.pack(side="left", padx=(14, 0))
        tooltip(c1, "Leave out creatures that already have a portrait. Off, they get new ones too, so the whole "
                    "mod comes out in one look.")
        c0 = ttk.Checkbutton(bar, text="Redo proposed", variable=auto_redo,
                             command=lambda: (st.__setitem__("auto_redo", bool(auto_redo.get())),
                                              save_settings(st), auto_counts()))
        c0.pack(side="left", padx=(8, 0))
        tooltip(c0, "Include creatures that already have a proposal. Picking one of the new pictures replaces it.")
        c2 = ttk.Checkbutton(bar, text="Party NPCs", variable=auto_party,
                             command=lambda: (st.__setitem__("auto_party", bool(auto_party.get())),
                                              save_settings(st), auto_counts()))
        c2.pack(side="left", padx=(8, 0))
        tooltip(c2, "Include joinable NPCs. Off by default: theirs are usually picked by hand.")
        c3 = ttk.Checkbutton(bar, text="Hide picked", variable=auto_hide,
                             command=lambda: (st.__setitem__("auto_hide", bool(auto_hide.get())),
                                              save_settings(st), auto_fill()))
        c3.pack(side="left", padx=(8, 0))
        tooltip(c3, "Show only the creatures with nothing picked.")
        du = ttk.Button(bar, text="Discard unchosen", command=auto_discard_unchosen)
        du.pack(side="left", padx=(14, 0))
        tooltip(du, "Delete the pictures not picked, for every creature that has one picked.")
        ttk.Label(bar, textvariable=auto_count_var, foreground="#555").pack(side="left", padx=(14, 0))
        hint_ = ttk.Frame(tab_auto)
        hint_.pack(fill="x")
        navtop_ = tk.Frame(hint_)
        navtop_.pack(side="left", padx=(0, 14))
        auto_ui["navtop"] = navtop_
        ttk.Label(hint_, textvariable=auto_msg, foreground="#1a5fb4").pack(side="left")
        ttk.Label(hint_, text="Click a picture to pick it, again to take it back \u00b7 right-click for more \u00b7 "
                              "click a name to copy it \u00b7 click the lines to read them", foreground="#777").pack(side="right")
        wrap = ttk.Frame(tab_auto)
        wrap.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical")
        sb.pack(side="right", fill="y")
        cv = tk.Canvas(wrap, highlightthickness=0, bg=PANEL, yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.config(command=cv.yview)
        inner = tk.Frame(cv, bg=PANEL)
        cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        auto_ui.update(built=True, btn=btn, canvas=cv, grid=inner)
        auto_catch(cv)
        auto_fill()

    auto_n = tk.IntVar(value=int(st.get("auto_count") or 2))
    auto_skip_has = tk.BooleanVar(value=bool(st.get("auto_skip_has", False)))
    auto_party = tk.BooleanVar(value=bool(st.get("auto_party", False)))
    auto_hide = tk.BooleanVar(value=bool(st.get("auto_hide", False)))
    auto_redo = tk.BooleanVar(value=bool(st.get("auto_redo", False)))
    auto_msg = tk.StringVar(value="")
    auto_page_var = tk.StringVar(value="")
    START_FROM = ["Nothing", "Face bank", "Its portrait"]
    start_v = tk.StringVar(value=str(st.get("start_from") or "Face bank")
                           if str(st.get("start_from") or "Face bank") in START_FROM else "Face bank")
    if st.get("start_strength") in (0.77, "0.77") and not st.get("start_v2"):
        st["start_strength"] = 0.68                                  # 0.77 was for 4 steps: the bank now uses 6
    st["start_v2"] = True
    start_k = tk.StringVar(value=str(st.get("start_strength") or "0.68"))

    def start_keep(*_a: object) -> None:
        st["start_from"] = start_v.get()
        try:
            st["start_strength"] = round(max(0.3, min(1.0, float(start_k.get()))), 2)
        except (tk.TclError, ValueError):
            return
        save_settings(st)
    start_v.trace_add("write", start_keep)
    start_k.trace_add("write", start_keep)

    def start_strength(pg_: object, gs_: dict, desc: str = "", race: str = "") -> float:
        mon_ = pg_.bank_monster(gs_, desc, race) if desc and hasattr(pg_, "bank_monster") else None   # type: ignore[attr-defined]
        if mon_:                                                     # a monster from the bank: further, always
            return float(mon_[0])
        try:
            return max(0.3, min(1.0, float(start_k.get())))
        except (tk.TclError, ValueError):
            return float(pg_.bank_strength(gs_)) if hasattr(pg_, "bank_strength") else 0.68   # type: ignore[attr-defined]

    def start_edit(pg_: object, gs_: dict, desc: str = "", race: str = "", anim: str = "",
                   src: str = "") -> dict:
        """A monster started from the face bank is painted twice: the face, then the monster in words."""
        if not desc or not hasattr(pg_, "monster_edit") or "faces" not in Path(str(src or "")).parts:
            return {}
        ed_ = pg_.monster_edit(gs_, desc, race, anim)                # type: ignore[attr-defined]
        return {"then_edit": ed_} if ed_ else {}

    def start_steps(pg_: object, gs_: dict, desc: str = "", race: str = "") -> Optional[int]:
        mon_ = pg_.bank_monster(gs_, desc, race) if desc and hasattr(pg_, "bank_monster") else None   # type: ignore[attr-defined]
        if mon_:
            return int(mon_[1])
        return int(pg_.bank_steps(gs_)) if hasattr(pg_, "bank_steps") else None   # type: ignore[attr-defined]

    def own_portrait_src(r: Optional[dict]) -> str:
        """Where the creature's own portrait is: "game:NAME" (the game has it) or the mod's own .bmp. "" if none."""
        if not r:
            return ""
        res_ = state.get("res")
        for f_ in ("large_name", "copy_port", "small_name"):
            nm = str(r.get(f_) or "")
            if not nm:
                continue
            try:
                if res_ and res_["game"].has(nm, T_BMP):             # type: ignore[index]
                    return "game:" + nm
            except Exception:  # noqa: BLE001
                pass
            p_ = mod_bmp_index().get(nm.upper(), "")
            if p_ and os.path.isfile(p_):
                return p_
        return ""

    def init_bytes(src: str) -> Optional[bytes]:
        """The start picture named by start_pick: a file, or "game:NAME" for a portrait the game has."""
        try:
            if src.startswith("game:"):
                res_ = state.get("res")
                return res_["game"].get(src[5:], T_BMP) if res_ else None   # type: ignore[index]
            return Path(src).read_bytes() if src and os.path.isfile(src) else None
        except Exception:  # noqa: BLE001
            return None

    def start_pick(pg_: object, gs_: dict, race: str, desc: str, prompt: str,
                   r: Optional[dict] = None) -> Tuple[Optional[bytes], str]:
        """What a picture starts from, as Start from says: (bytes, where from) or (None, "") for plain noise. The
        face bank leaves out beasts, monsters, the undead and ghosts; a child only takes a face from faces\\child."""
        mode = start_v.get()
        if mode == "Nothing" or not hasattr(pg_, "bank_pick"):
            return None, ""
        if mode == "Its portrait":
            src = own_portrait_src(r)
            data = init_bytes(src) if src else None
            if data:
                return data, src
        text = desc + (", nine years old" if "nine years old" in prompt.lower() else "")
        sx_ = str((r or {}).get("sex") or "").lower()                # a child's words say no gender: its file
        if sx_ in ("male", "female") and not re.search(r"\b(male|female|man|woman|boy|girl)\b", text.lower()):
            text += ", " + sx_                                       # does, for the right pile of faces
        p_ = pg_.bank_pick(gs_, race, text)                        # type: ignore[attr-defined]
        data = init_bytes(str(p_)) if p_ else None
        if data and hasattr(pg_, "bank_fill"):                       # its blurred padding painted over
            mon_ = pg_.bank_monster(gs_, text, race) if hasattr(pg_, "bank_monster") else None   # type: ignore[attr-defined]
            if mon_ and len(mon_) > 3 and mon_[3] and hasattr(pg_, "bank_hulk"):   # an ogre: a small head on a
                data = pg_.bank_hulk(data, text)                     # huge body   # type: ignore[attr-defined]
            elif mon_:                                               # a monster: only the face's shape and light
                data = pg_.bank_fill(data, pg_.bank_body(gs_, str(p_)), pg_.bank_scale(gs_),   # type: ignore[attr-defined]
                                     blur=float(mon_[2]))
            else:
                data = pg_.bank_fill(data, pg_.bank_body(gs_, str(p_)), pg_.bank_scale(gs_), pg_.bank_beard(text),   # type: ignore[attr-defined]
                                     pg_.bank_bald(text), pg_.bank_wide(text), pg_.bank_bare(text),
                                     pg_.bank_nose(text), pg_.bank_ears(text),
                                 chin="child" in Path(str(p_)).parts)   # type: ignore[attr-defined]
        return (data, str(p_)) if data else (None, "")

    def start_note(path: str, src: str) -> None:
        """Remember what a picture started from, so "Use this face" can start from it again."""
        if not src:
            return
        vi_ = st.setdefault("vary_init", {})
        vi_[Path(path).name] = src                                   # type: ignore[index]
        for old_ in list(vi_)[:-4000]:                               # type: ignore[arg-type]
            vi_.pop(old_, None)                                      # type: ignore[union-attr]

    def start_controls(bar: object, side: str = "left", sv: Optional[tk.StringVar] = None,
                       sk: Optional[tk.StringVar] = None) -> object:
        """Start from [list] [strength], for the Auto bar and the Propose window (they share one setting; the
        Propose window passes its own two, which follow the shared ones until a picture is restyled)."""
        lab = ttk.Label(bar, text="Start from:")
        box = ttk.Combobox(bar, textvariable=sv or start_v, values=START_FROM, state="readonly", width=11)
        spn = ttk.Spinbox(bar, from_=0.3, to=1.0, increment=0.01, width=5, textvariable=sk or start_k)
        tooltip(box, "Face bank: each picture starts from a new face from the faces folder, so faces differ. "
                     "Its portrait: from the creature's own portrait. Nothing: plain.")
        tooltip(spn, "Strength: how far a picture may move from its start face (about 0.6-0.72). Lower keeps more of it.")
        order = (lab, box, spn) if side == "left" else (spn, box, lab)
        for i_, w_ in enumerate(order):
            w_.pack(side=side, padx=((14 if side == "left" else 2), 4) if i_ == 0 else (0, 4))
        return box

    def auto_turn(step: int, to: Optional[int] = None) -> None:
        last = int(auto_ui.get("pages") or 1) - 1
        auto_ui["page"] = max(0, min(last, to if to is not None else int(auto_ui.get("page") or 0) + step))
        auto_ui["paged"] = True
        auto_fill()

    def auto_nav(bar: object, page: int, pages: int, per: int, total: int) -> None:
        """< Previous, Page [n] of N, Next >: as the gallery has them. Type a page number and press Enter."""
        if pages <= 1:
            tk.Label(bar, text=f"{total} creature(s)", fg="#555").pack(side="left")
            return
        bar.config(highlightthickness=2, highlightbackground="#8a8f98", highlightcolor="#8a8f98", padx=4, pady=2)   # type: ignore[attr-defined]
        tk.Button(bar, text=f"< Previous {per}", command=lambda: auto_turn(-1),
                  state="normal" if page > 0 else "disabled").pack(side="left", padx=6)
        bg_ = bar.cget("bg")                                         # type: ignore[attr-defined]
        tk.Label(bar, text="Page", bg=bg_, fg="#555").pack(side="left", padx=(6, 2))
        pv_ = tk.StringVar(value=str(page + 1))
        pe_ = tk.Entry(bar, textvariable=pv_, width=4, justify="center")
        pe_.pack(side="left")

        def jump(_e: object = None) -> None:
            try:
                auto_turn(0, int(pv_.get().strip()) - 1)
            except ValueError:
                pass
        pe_.bind("<Return>", jump)
        pe_.bind("<KP_Enter>", jump)
        first = page * per
        tk.Label(bar, text=f"of {pages}  ({first + 1}-{min(first + per, total)} of {total})", bg=bg_,
                 fg="#555").pack(side="left", padx=(2, 6))
        tk.Button(bar, text=f"Next {per} >", command=lambda: auto_turn(1),
                  state="normal" if page < pages - 1 else "disabled").pack(side="left", padx=6)
    auto_count_var = tk.StringVar(value="")

    def open_path(p: str) -> None:
        """Open a file or folder in whatever the system uses for it."""
        try:
            if os.name == "nt":
                os.startfile(p)                                      # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        except (OSError, AttributeError) as e:
            messagebox.showinfo("Portrait Scout", f"Could not open {p}:\n{e}")

    def fill_export() -> None:
        """The Proposals tab: every creature you have chosen a picture for."""
        etree.delete(*etree.get_children())
        rows = [r for r in sorted_rows(False) if rowkey(r) in props]
        state["exp_rows"] = rows
        for r in rows:
            pp = props[rowkey(r)]
            etree.insert("", "end", values=(r["name"], ", ".join(r.get("cre_list") or []), Path(pp).name,
                                            prop_dims(pp) or "", areas_text(r, 4)))
        if not rows:                                                 # a message has to fit the column it sits in
            etree.insert("", "end", values=("(nothing proposed yet)", "", "Propose one from a gallery card.",
                                            "", ""))

    def exp_row() -> Optional[dict]:
        sel = etree.selection()
        rows = state.get("exp_rows") or []
        idx = etree.index(sel[0]) if sel else -1
        return rows[idx] if 0 <= idx < len(rows) else None            # type: ignore[index,call-overload]

    def exp_open(_e: object = None) -> None:
        r = exp_row()
        if r:
            open_path(props[rowkey(r)])

    def exp_remove(_e: object = None) -> None:
        r = exp_row()
        if r:
            clear_prop(r)
            fill_export()
    etree.bind("<Double-Button-1>", exp_open)
    etree.bind("<Delete>", exp_remove)

    def exp_menu(e: object) -> None:
        r = exp_row()
        m = tk.Menu(root, tearoff=0)
        if r:
            m.add_command(label="Open the picture", command=exp_open)
            m.add_command(label="Open the folder it is in",
                          command=lambda: open_path(str(Path(props[rowkey(r)]).parent)))
            m.add_command(label="Work on this creature in the Propose tab", command=lambda: generate_dialog(r))
            m.add_separator()
            m.add_command(label="Remove this proposal", command=exp_remove)
        else:
            m.add_command(label="Nothing selected", state="disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)                            # type: ignore[attr-defined]
        finally:
            m.grab_release()
    etree.bind("<Button-3>", lambda e: (etree.selection_set(etree.identify_row(e.y)), exp_menu(e)))

    FORGE_DL = ("https://github.com/lllyasviel/stable-diffusion-webui-forge/releases/download/latest/"
                "webui_forge_cu121_torch231.7z")                      # the build this was written against
    FORGE_ALL = "https://github.com/lllyasviel/stable-diffusion-webui-forge/releases"
    FORGE_DOC = "https://github.com/lllyasviel/stable-diffusion-webui-forge"
    FLUX_DL = "https://huggingface.co/lllyasviel/flux1-dev-bnb-nf4"
    LORA_DL = "https://civitai.com/models/850634"                     # the one the default style asks for
    LORA_DL2 = "https://civitai.com/models/2071031/baldurs-gate-2-portrait-style-flux"
    NEO_DOC = "https://github.com/Haoming02/sd-webui-forge-classic/tree/neo"   # Forge Neo: paints FLUX.2 Klein
    NEO_GIT = "git clone https://github.com/Haoming02/sd-webui-forge-classic engine-neo --branch neo"
    NEO_VENV = "uv venv venv --python 3.13 --seed"
    GIT_DL = "https://git-scm.com/downloads"
    UV_DL = "https://github.com/astral-sh/uv#installation"
    KLEIN_DL = "https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8"
    KLEIN_BITS = "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b"   # encoder and VAE
    KLEIN_FILES = (                                                  # (folder under models, file, bytes, link)
        ("Stable-diffusion", "flux-2-klein-9b-fp8.safetensors", 9433061528,
         "https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/resolve/main/flux-2-klein-9b-fp8.safetensors?download=true"),
        ("text_encoder", "qwen_3_8b_fp8mixed.safetensors", 8664848742,
         KLEIN_BITS + "/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors?download=true"),
        ("VAE", "flux2-vae.safetensors", 336211292,
         KLEIN_BITS + "/resolve/main/split_files/vae/flux2-vae.safetensors?download=true"))

    def fresh_path() -> str:
        """PATH as Windows has it now, not as it was when Portrait Scout started: git or uv installed a minute
        ago are on it."""
        parts_ = [os.environ.get("PATH", "")]
        if os.name == "nt":
            try:
                import winreg
                for hive_, key_ in ((winreg.HKEY_LOCAL_MACHINE,
                                     r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                                    (winreg.HKEY_CURRENT_USER, "Environment")):
                    try:
                        with winreg.OpenKey(hive_, key_) as k_:
                            parts_.append(os.path.expandvars(str(winreg.QueryValueEx(k_, "Path")[0])))
                    except OSError:
                        pass
            except ImportError:
                pass
            parts_.append(os.path.expandvars(r"%USERPROFILE%\.local\bin"))   # where uv's installer puts it
        return os.pathsep.join(x for x in parts_ if x)

    def run_in_window(cmd: str, cwd: str, on_done: Optional[Callable[[], None]] = None) -> None:
        """Run a setup command in a command window of its own, which stays open until a key is pressed so its
        output can be read. on_done runs here once that window has closed."""
        if not os.path.isdir(cwd):
            messagebox.showinfo("Portrait Scout", f"{cwd} is not there yet - do the step before this one first.")
            return
        env_ = dict(os.environ, PATH=fresh_path())
        try:
            if os.name == "nt":                                      # run it, then wait for a key and close
                ps_ = (cmd + "; Write-Host ''; if ($?) { Write-Host 'Done.' -ForegroundColor Green } else "
                       "{ Write-Host 'That did not work - read the messages above.' -ForegroundColor Red }; "
                       "Write-Host 'Press any key to close this window.'; "
                       "$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')")
                proc_ = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_], cwd=cwd, env=env_,
                                 creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            else:
                proc_ = subprocess.Popen(["x-terminal-emulator", "-e", "bash", "-c",
                                  cmd + "; echo; read -n 1 -s -p 'Press any key to close this window.'"], cwd=cwd, env=env_)
        except OSError:
            try:
                proc_ = subprocess.Popen(["cmd", "/C", cmd + " & echo. & pause"], cwd=cwd, env=env_,
                                 creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError as e_:
                messagebox.showwarning("Portrait Scout", f"Could not open a command window:\n{e_}\n\n"
                                       f"Open one in {cwd} and type:\n{cmd}")
                return
        if on_done is not None:                                      # tick the step as soon as the window closes
            threading.Thread(target=lambda: (proc_.wait(), root.after(0, on_done)), daemon=True).start()

    FACES_URL = "https://github.com/smeagolheart/PortraitScout/releases/latest/download/PortraitScout-faces.zip"

    def faces_count() -> int:
        """How many adult faces the bank has (faces\\human\\male and female)."""
        d_ = Path(__file__).resolve().parent / "faces" / "human"
        n_ = 0
        for sx_ in ("male", "female"):
            try:
                n_ += sum(1 for f_ in (d_ / sx_).iterdir() if f_.suffix.lower() in (".jpg", ".jpeg", ".png"))
            except OSError:
                pass
        return n_

    def klein_found(sub: str, name: str) -> Optional[Path]:
        """Where a Klein file is: Forge Neo's own models folder, or the old Forge's (Neo reads both)."""
        here_ = Path(__file__).resolve().parent
        for d_ in (here_ / "engine-neo" / "models" / sub, here_ / "engine" / "webui" / "models" / sub):
            if (d_ / name).is_file():
                return d_ / name
        return None

    def downloads_dirs() -> List[Path]:
        """The Downloads folder - where the browser put the model files - wherever Windows keeps it."""
        out_ = [Path.home() / "Downloads"]
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k_:
                    v_ = os.path.expandvars(str(winreg.QueryValueEx(k_, "{374DE290-123F-4565-9164-39C4925E467B}")[0]))
                    out_.insert(0, Path(v_))
            except (ImportError, OSError):
                pass
        return [d_ for i_, d_ in enumerate(out_) if d_.is_dir() and d_ not in out_[:i_]]

    def klein_downloaded() -> List[Tuple[Path, Path]]:
        """Model files that are whole in Downloads but not in place yet: (where it is, where it goes)."""
        here_ = Path(__file__).resolve().parent
        out_: List[Tuple[Path, Path]] = []
        for sub_, nm_, size_, _u in KLEIN_FILES:
            if klein_found(sub_, nm_):
                continue
            stem_, suf_ = nm_.rsplit(".", 1)
            for d_ in downloads_dirs():                              # also "name (1).safetensors", a second try
                cands_ = [d_ / nm_] + sorted(d_.glob(f"{stem_} (*).{suf_}"))
                f_ = next((c_ for c_ in cands_ if c_.is_file() and c_.stat().st_size >= size_), None)
                if f_ is not None:
                    out_.append((f_, here_ / "engine-neo" / "models" / sub_ / nm_))
                    break
        return out_

    def neo_settings_patch() -> dict:
        """What generator/settings.json needs for Forge Neo with FLUX.2 Klein 9B."""
        mods_ = [str(klein_found(s_, n_) or Path(__file__).resolve().parent / "engine-neo" / "models" / s_ / n_)
                 for s_, n_, _b, _u in KLEIN_FILES[1:]]
        return {"engine": {"start": "generator\\start_engine_neo.bat {port}"},
                "style": "Klein painterly",
                "payload": {"steps": 4, "sampler_name": "Euler", "scheduler": "Simple", "cfg_scale": 1,
                            "override_settings": {"sd_model_checkpoint": KLEIN_FILES[0][1],
                                                  "forge_additional_modules": mods_}}}

    def neo_checks() -> List[Tuple[bool, str]]:
        """Each Forge Neo step: done or not, and what is missing."""
        here_ = Path(__file__).resolve().parent
        pth_ = fresh_path()
        out_: List[Tuple[bool, str]] = []
        miss_ = [x for x in ("git", "uv") if not shutil.which(x, path=pth_)]
        out_.append((not miss_, "" if not miss_ else " and ".join(miss_) + " not found"))
        neo_ = here_ / "engine-neo"
        out_.append(((neo_ / "webui.bat").is_file() or (neo_ / "webui.sh").is_file(),
                     "engine-neo has no webui.bat yet"))
        out_.append(((neo_ / "venv" / "Scripts" / "python.exe").is_file() or (neo_ / "venv" / "bin" / "python").is_file(),
                     "engine-neo\\venv has no Python yet"))
        bad_: List[str] = []
        for sub_, nm_, size_, _u in KLEIN_FILES:
            f_ = klein_found(sub_, nm_)
            if f_ is None:
                stray_ = next(iter(sorted((neo_ / "models").rglob(nm_))), None) if (neo_ / "models").is_dir() else None
                dl_ = next((d_ / nm_ for d_ in downloads_dirs() if (d_ / nm_).is_file()), None)
                bad_.append(f"{nm_} is in {stray_.parent.name}\\, not {sub_}\\" if stray_ else
                            f"{nm_} is in Downloads - press Move downloads" if dl_ and dl_.stat().st_size >= size_ else
                            f"{nm_} is still downloading" if dl_ else f"{nm_} missing")
            elif f_.stat().st_size < size_:
                bad_.append(f"{nm_} is only {f_.stat().st_size / 1e9:.1f} of {size_ / 1e9:.1f} GB (still downloading?)")
        out_.append((not bad_, "; ".join(bad_)))
        try:
            gs_ = generator_module().load_settings()                 # type: ignore[attr-defined]
            ov_ = (gs_.get("payload") or {}).get("override_settings") or {}
            ok_ = ("neo" in str((gs_.get("engine") or {}).get("start") or "").lower()
                   and ov_.get("sd_model_checkpoint") == KLEIN_FILES[0][1]
                   and all(os.path.isfile(str(m_)) for m_ in ov_.get("forge_additional_modules") or [None]))
        except Exception:  # noqa: BLE001 - no generator settings yet
            ok_ = False
        started_ = (neo_ / ".portrait_scout_started").is_file()      # its slow first start is behind it
        out_.append((ok_ and started_, "set, but not started once yet - press Use Forge Neo" if ok_ else
                     "not set to Forge Neo yet - press Use Forge Neo"))
        return out_

    def lora_files() -> List[str]:
        here_ = Path(__file__).resolve().parent
        found_: Set[str] = set()
        for d in (here_ / "engine" / "webui" / "models" / "Lora", here_ / "engine-neo" / "models" / "Lora"):
            try:                                                     # the old Forge's and Forge Neo's
                found_ |= {p.name for p in d.iterdir() if p.suffix.lower() in (".safetensors", ".pt", ".ckpt")}
            except OSError:
                pass
        return sorted(found_)

    def engine_ready() -> bool:
        """An engine is really there - the file a start_engine bat runs. Either Forge Neo in engine-neo\\ or the
        older Forge in engine\\webui\\. An empty models\\ tree, made while putting a model in place, must not
        count."""
        here_ = Path(__file__).resolve().parent
        return any((d / n).is_file() for d in (here_ / "engine-neo", here_ / "engine" / "webui")
                   for n in ("webui.bat", "webui.sh"))

    def open_make(path: str, *fallbacks: str) -> None:
        """Open a folder of the engine, making it if it is not there yet (the deeper ones appear as Forge unpacks)."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            path = next((p for p in fallbacks if os.path.isdir(p)), path)
        open_path(path)

    def engine_text() -> str:
        """The setup window's heading: which engine is here, and whether the settings use it."""
        here_ = Path(__file__).resolve().parent
        neo_ = (here_ / "engine-neo" / "webui.bat").is_file() or (here_ / "engine-neo" / "webui.sh").is_file()
        old_ = (here_ / "engine" / "webui" / "webui.bat").is_file() or (here_ / "engine" / "webui" / "webui.sh").is_file()
        try:
            eg_ = generator_module().load_settings().get("engine") or {}   # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            eg_ = {}
        if eg_ and not eg_.get("autostart", True):
            return f"Using your own generator at {eg_.get('url') or 'its address'}"
        uses_neo_ = "neo" in str(eg_.get("start") or "").lower()
        if uses_neo_ and neo_:
            return "Engine found: Forge Neo (FLUX.2 Klein)"
        if not uses_neo_ and old_:
            return "Engine found: Forge (FLUX.1)"
        if neo_:
            return "Engine found: Forge Neo - press Use Forge Neo (step 5) to use it"
        if old_:
            return "Engine found: Forge - the settings point at Forge Neo, which is not here"
        return "No image generator here yet"

    def setup_panel(parent: object, again: object = None) -> object:
        """What to download and where to put it, for someone who has just cloned the repository."""
        here = Path(__file__).resolve().parent
        BG = "#fff8e1"
        box = tk.Frame(parent, bg=BG, bd=1, relief="solid", padx=10, pady=8)   # type: ignore[arg-type]
        box.columnconfigure(0, weight=1)
        head = tk.Frame(box, bg=BG)
        head.grid(row=0, column=0, columnspan=2, sticky="w")
        head_l = tk.Label(head, text=engine_text(), bg=BG, font=("TkDefaultFont", 10, "bold"))
        head_l.pack(side="left")
        if callable(again):
            def again_() -> None:
                again()                                              # type: ignore[operator]
                if head_l.winfo_exists():                            # still open: say what was found
                    head_l.config(text=engine_text())
            ttk.Button(head, text="Check again", command=again_).pack(side="left", padx=10)
        tk.Label(box, bg=BG, justify="left", wraplength=980, fg="#5a4a00",
                 text="Portrait Scout builds the prompt, sends it, and turns the picture into a game portrait. The "
                      "generator itself and its models are many gigabytes and separately licensed, so they are not in "
                      "the repository. Set one up once:").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))

        def step(n: int, text: object, buttons: List[Tuple[str, object]], where: object = None) -> None:
            try:
                _step(n, text, buttons, where)
            except Exception as e:  # noqa: BLE001 - a step that will not draw must not hide the ones after it
                traceback.print_exc()
                tk.Label(where if where is not None else box, bg=BG, fg="#a03030", justify="left", wraplength=840,
                         text=f"(step {n} could not be drawn: {e.__class__.__name__}: {e})"
                         ).grid(row=n, column=0, columnspan=2, sticky="w")

        def _step(n: int, text: object, buttons: List[Tuple[str, object]], where: object = None) -> None:
            """text: a string, or pieces of one where (piece, True) is shown in bold."""
            into = where if where is not None else box
            pieces = [(text, False)] if isinstance(text, str) else list(text)   # type: ignore[arg-type]
            t = tk.Text(into, bg=BG, width=88, height=2, wrap="word", bd=0, highlightthickness=0,
                        font=("TkDefaultFont", 9), cursor="xterm", spacing3=2, exportselection=True)
            t.tag_configure("b", font=("TkDefaultFont", 9, "bold"))
            for piece, bold in pieces:
                t.insert("end", piece, ("b",) if bold else ())
            whole = "".join(p for p, _b in pieces)                   # as tall as the text needs, counted by hand:
            lines = whole.count("\n") + 1                            # asking Tk before it is drawn gives nonsense
            for para in whole.split("\n"):
                lines += len(para) // 84
            t.config(height=max(1, min(8, lines)), state="disabled")
            copyable(t)
            t.grid(row=n * 2, column=0, sticky="w", pady=2)
            if buttons:
                f = tk.Frame(into, bg=BG)
                f.grid(row=n * 2, column=1, sticky="w", padx=(10, 0), pady=(4, 4))
                for i_, (label, cmd) in enumerate(buttons):            # three to a row, so none is cut off
                    ttk.Button(f, text=label, command=cmd).grid(row=i_ // 3, column=i_ % 3, sticky="ew",
                                                                padx=(0, 6), pady=(0, 3))   # type: ignore[arg-type]

        def copy_all() -> None:
            """Every step's text and every note in the window, for pasting into a message."""
            out_: List[str] = []

            def walk(wd_: object) -> None:
                for c_ in wd_.winfo_children():                      # type: ignore[attr-defined]
                    try:
                        if not c_.winfo_ismapped():
                            continue
                        if isinstance(c_, tk.Text):
                            out_.append(c_.get("1.0", "end-1c").strip())
                        elif isinstance(c_, tk.Label) and str(c_.cget("text")).strip() not in ("", "\u2714", "\u2716"):
                            out_.append(str(c_.cget("text")).strip())   # the check note says what is not done
                    except tk.TclError:
                        pass
                    walk(c_)
            walk(box)
            root.clipboard_clear()
            root.clipboard_append("\n\n".join(x for x in out_ if x))
            status.set("Copied the setup steps and notes.")

        def copyable(t_: tk.Text) -> None:
            """Selectable read-only text: click focuses it, so Ctrl+C works; right-click to copy."""
            t_.bind("<Button-1>", lambda e_: t_.focus_set(), add="+")
            m_ = tk.Menu(t_, tearoff=0)
            m_.add_command(label="Copy", command=lambda: (root.clipboard_clear(), root.clipboard_append(
                t_.get("sel.first", "sel.last") if t_.tag_ranges("sel") else t_.get("1.0", "end-1c"))))
            m_.add_command(label="Copy all steps", command=copy_all)
            t_.bind("<Button-3>", lambda e_: m_.tk_popup(e_.x_root, e_.y_root))

        def status_line(where: object, row: int) -> Tuple[tk.Frame, Callable[[str, Optional[bool]], None]]:
            """A line under a step for what its button is doing, with a bar that moves while it works.
            say(text, busy): busy True starts the bar, False stops it, None leaves it."""
            f_ = tk.Frame(where, bg=BG)                              # type: ignore[arg-type]
            f_.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 4))
            bar_ = ttk.Progressbar(f_, mode="indeterminate", length=300)
            tx_ = tk.Text(f_, bg=BG, fg="#1f5fa8", width=88, height=1, wrap="word", bd=0, highlightthickness=0,
                          font=("TkDefaultFont", 9), cursor="xterm")
            tx_.pack(side="top", anchor="w", fill="x")
            copyable(tx_)
            f_.grid_remove()

            def say(text: str, busy: Optional[bool] = None) -> None:
                def ui() -> None:
                    if not f_.winfo_exists():
                        return
                    tx_.config(state="normal")
                    tx_.delete("1.0", "end")
                    tx_.insert("end", text)
                    tx_.config(height=max(1, min(4, 1 + len(text) // 100)), state="disabled")
                    if text:
                        f_.grid()
                    else:
                        f_.grid_remove()
                    if busy is True:
                        bar_.pack(side="top", anchor="w", pady=(2, 0))
                        bar_.start(15)
                    elif busy is False:
                        bar_.stop()
                        bar_.pack_forget()
                root.after(0, ui)
            return f_, say

        def copy_cmd(text: str) -> None:
            """Put a command on the clipboard: it is typed into a command window, not run from here."""
            root.clipboard_clear()
            root.clipboard_append(text)
            status.set("Copied: " + text)

        which = state.get("setup_engine") or "neo"                   # which of the two is being set up
        pick = tk.StringVar(value=str(which))
        picker = tk.Frame(box, bg=BG)
        picker.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        neo_f, old_f = tk.Frame(box, bg=BG), tk.Frame(box, bg=BG)
        neo_f.grid(row=3, column=0, columnspan=2, sticky="w")
        old_f.grid(row=3, column=0, columnspan=2, sticky="w")

        def picked() -> None:
            state["setup_engine"] = pick.get()
            (neo_f if pick.get() == "neo" else old_f).tkraise()
            (old_f if pick.get() == "neo" else neo_f).lower()
        for val, lab in (("neo", "Forge Neo (FLUX.2 Klein)"), ("old", "Forge (FLUX.1 dev, one file)")):
            tk.Radiobutton(picker, text=lab, value=val, variable=pick, bg=BG, activebackground=BG,
                           command=picked).pack(side="left", padx=(0, 12))

        # ---- Forge Neo: what the styles are written against now ----
        step(0, [("1.  Install ", 0), ("git", 1), (" and ", 0), ("uv", 1),
                 (". Both are free; the defaults of their installers will do.", 0)],
             [("git", lambda: webbrowser.open(GIT_DL)), ("uv", lambda: webbrowser.open(UV_DL))], neo_f)
        step(1, [("2.  Clone Forge Neo into ", 0), ("engine-neo", 1), (" (Run it opens a command window in "
                 + str(here) + " and types it):\n", 0), (NEO_GIT, 1)],
             [("Run it", lambda: run_in_window(NEO_GIT, str(here), lambda: check_neo())), ("Copy", lambda: copy_cmd(NEO_GIT)),
              ("About Neo", lambda: webbrowser.open(NEO_DOC))], neo_f)
        step(2, [("3.  Make its Python, inside engine-neo:\n", 0), (NEO_VENV, 1),
                 ("\n(Portrait Scout starts it with --uv.)", 0)],
             [("Run it", lambda: run_in_window(NEO_VENV, str(here / "engine-neo"), lambda: check_neo())),
              ("Copy", lambda: copy_cmd(NEO_VENV))], neo_f)
        step(3, [("4.  Three files, about 18.4 GB together, into ", 0), ("engine-neo\\models\\", 1), (":\n", 0),
                 ("flux-2-klein-9b-fp8.safetensors", 1), (" (9.4 GB) in Stable-diffusion\\, ", 0),
                 ("qwen_3_8b_fp8mixed.safetensors", 1), (" (8.7 GB) in text_encoder\\, ", 0),
                 ("flux2-vae.safetensors", 1), (" (336 MB) in VAE\\. The buttons download each one; Hugging Face "
                  "may ask you to sign in and accept the model's license first. Move downloads then puts "
                  "each one in its folder.", 0)],
             [("Model", lambda: webbrowser.open(KLEIN_FILES[0][3])),
              ("Encoder", lambda: webbrowser.open(KLEIN_FILES[1][3])),
              ("VAE", lambda: webbrowser.open(KLEIN_FILES[2][3])),
              ("Open folder", lambda: open_make(str(here / "engine-neo" / "models" / KLEIN_FILES[0][0]), str(here))),
              ("Open folder", lambda: open_make(str(here / "engine-neo" / "models" / KLEIN_FILES[1][0]), str(here))),
              ("Open folder", lambda: open_make(str(here / "engine-neo" / "models" / KLEIN_FILES[2][0]), str(here))),
              ("Move downloads", lambda: move_downloads())], neo_f)

        _f4, say4 = status_line(neo_f, 3 * 2 + 1)                     # under step 4: the moves

        def move_downloads() -> None:
            """Move the model files from Downloads into engine-neo\\models, each into its own folder - in the
            background: to another drive a move is a copy, and 18 GB takes minutes."""
            todo_ = klein_downloaded()
            if not todo_:
                left_ = [nm_ for sub_, nm_, _s, _u in KLEIN_FILES if not klein_found(sub_, nm_)]
                say4("Nothing to move: " + (("not in Downloads yet, or not finished downloading: " + ", ".join(left_))
                                            if left_ else "all three are in place already."), False)
                check_neo()
                return

            def work() -> None:
                moved_, err_ = [], []
                for k_, (src_, dst_) in enumerate(todo_):
                    say4(f"Moving {src_.name} ({k_ + 1} of {len(todo_)}, {src_.stat().st_size / 1e9:.1f} GB) "
                         f"into {dst_.parent.name}\\ - please wait, to another drive this is a copy.", True)
                    try:
                        dst_.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src_), str(dst_))
                        moved_.append(dst_.name)
                    except OSError as e_:
                        err_.append(f"{src_.name}: {e_}")
                left_ = [nm_ for sub_, nm_, _s, _u in KLEIN_FILES if not klein_found(sub_, nm_)]
                say4(("Moved " + ", ".join(moved_) + "." if moved_ else "Nothing moved.") +
                     (("  Could not move: " + "; ".join(err_) + ".") if err_ else "") +
                     (("  Not in Downloads yet (or not finished): " + ", ".join(left_) + ".") if left_ else ""), False)
                root.after(0, check_neo)
            threading.Thread(target=work, daemon=True).start()

        def use_neo() -> None:
            """Write the settings, then start the engine once: its first start installs Forge's packages, and
            that is best done here, not at the first Generate."""
            try:
                pg_ = generator_module()
                gs_ = pg_.load_settings()                            # type: ignore[attr-defined]
                pg_.save_patch(gs_, neo_settings_patch())            # type: ignore[attr-defined]
            except Exception as e_:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", f"Could not write generator/settings.json:\n{e_}")
                return
            st["portrait_style"] = st["portrait_style_people"] = "Klein painterly"
            save_settings(st)
            status.set("Generator settings: Forge Neo with FLUX.2 Klein 9B, 4 steps, CFG 1.")
            check_neo()
            if not all(ok_ for ok_, _w in neo_checks()[:4]):
                say5("Settings written. Finish the steps above, then press Use Forge Neo again to start it "
                     "for the first time.", False)
                return
            old_ = state.get("engine")
            if old_ is not None and getattr(old_, "proc", None) is not None:
                say5("Settings written. The engine is already running; it uses them from its next start.", False)
                return
            eng_ = pg_.Engine(pg_.load_settings())                   # type: ignore[attr-defined]
            state["engine"] = eng_                                   # stopped with Portrait Scout, like any other
            eng_.keep = bool(st.get("engine_keep"))                  # type: ignore[attr-defined]
            eng_.log = lambda m_: say5(m_)                           # type: ignore[attr-defined]
            eng_.progress = lambda f_, m_: say5(m_)                  # type: ignore[attr-defined]
            say5("Starting Forge Neo for the first time - please wait...", True)

            def work() -> None:
                try:
                    eng_.start()                                     # type: ignore[attr-defined]
                    say5("Forge Neo is set up and running. Press Check again at the top, then Generate.", False)
                except Exception as e_:  # noqa: BLE001
                    say5(f"The engine did not start: {e_}", False)
                root.after(0, check_neo)
            threading.Thread(target=work, daemon=True).start()
        step(4, [("5.  Set Portrait Scout to Forge Neo and start it once. ", 0), ("Use Forge Neo", 1),
                 (" writes the start script, the Klein model, the encoder and the VAE, 4 steps at CFG 1 and the "
                  "Klein painterly style into generator\\settings.json, then starts the engine for the first time. "
                  "That first start installs Forge's packages and takes a while, once; later starts are quick.", 0)],
             [("Use Forge Neo", use_neo),
              ("Setup notes", lambda: open_path(str(here / "generator" / "README.md")))], neo_f)
        _f5, say5 = status_line(neo_f, 4 * 2 + 1)                     # under step 5: the first start
        step(5, "6.  Press Check again, at the top: it names the engine it found. From now on Portrait Scout starts "
                "and stops it by itself.", [], neo_f)
        marks_ = [tk.Label(neo_f, text="", bg=BG, font=("TkDefaultFont", 12, "bold")) for _i in range(5)]
        for i_, m_ in enumerate(marks_):                             # level with the step's own buttons
            m_.grid(row=i_ * 2, column=2, sticky="w", padx=(2, 0))
        crow_ = tk.Frame(neo_f, bg=BG)
        crow_.grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))
        cnote_ = tk.Label(crow_, text="", bg=BG, fg="#5a4a00", justify="left", wraplength=700)

        def check_neo() -> None:
            """Tick each step that is done, and say what the first one that is not still needs."""
            if not cnote_.winfo_exists():                            # the window was closed meanwhile
                return
            try:
                res_ = neo_checks()
            except Exception as e_:  # noqa: BLE001
                cnote_.config(text=f"Could not check: {e_}")
                return
            for m_, (ok_, _w) in zip(marks_, res_):
                m_.config(text="\u2714" if ok_ else "\u2716", fg="#2e7d32" if ok_ else "#b23b3b")
            todo_ = [f"Step {i_ + 1}: {w_}" for i_, (ok_, w_) in enumerate(res_) if not ok_]
            cnote_.config(text=("All in place." + ("" if faces_count() else " Download the faces below, then close "
                                "this window.")) if not todo_ else "\n".join(todo_))
        chk_btn = ttk.Button(crow_, text="Check files", command=check_neo)
        chk_btn.pack(side="left", anchor="n")
        tooltip(chk_btn, "Look for git, uv, Forge Neo, its Python and the three model files (whole, in the right "
                         "folders), and whether the settings point at them.")
        cnote_.pack(side="left", padx=(10, 0))
        root.after(200, check_neo)

        # ---- the older Forge: one package, one model file, no git ----
        step(0, [("1.  Download Forge's Windows one-click package (webui_forge_cu121_torch231.7z, 1.7 GB) and ", 0),
                 ("unpack it into " + str(here / "engine"), 1),
                 (", so that you end up with engine\\webui\\ and engine\\system\\, with run.bat in engine\\.", 0)],
             [("Download Forge", lambda: webbrowser.open(FORGE_DL)),
              ("(releases)", lambda: webbrowser.open(FORGE_ALL)),
              ("Open folder", lambda: open_make(str(here / "engine"), str(here)))], old_f)
        step(1, [("2.  Run ", 0), ("engine\\update.bat", 1), (" once, then ", 0), ("engine\\run.bat", 1),
                 (" once and wait for the Forge page to open. That proves it works, then close it again.", 0)],
             [], old_f)
        step(2, [("3.  Put ", 0), ("flux1-dev-bnb-nf4-v2.safetensors", 1),
                 (" (12 GB) in engine\\webui\\models\\Stable-diffusion\\. One file: the text encoders and the VAE "
                  "are inside it.", 0)],
             [("Download model", lambda: webbrowser.open(FLUX_DL)),
              ("Open folder", lambda: open_make(str(here / "engine" / "webui" / "models" / "Stable-diffusion"),
                                                str(here / "engine"), str(here)))], old_f)
        step(3, [("4.  Optional: ", 0), ("chargen_oil_painting_v2.safetensors", 1),
                 (" (86 MB, from Painterly Fantasy Portrait) in engine\\webui\\models\\Lora\\. The style Portrait "
                  "Scout starts with asks for it by name - without it that style still runs, but the painted look is "
                  "mostly gone.", 0)],
             [("Download LoRA", lambda: webbrowser.open(LORA_DL)),
              ("(other LoRA)", lambda: webbrowser.open(LORA_DL2)),
              ("Open folder", lambda: open_make(str(here / "engine" / "webui" / "models" / "Lora"),
                                                str(here / "engine"), str(here)))], old_f)
        step(4, "5.  Press Check again, at the top. Portrait Scout starts and stops the generator by itself after "
                "that.", [], old_f)
        picked()
        # ---- the face bank: for either engine ----
        fbox = tk.Frame(box, bg=BG)
        fbox.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Label(fbox, bg=BG, justify="left", wraplength=720, anchor="w",
                 text="Face bank (about 550 MB): real-looking faces each portrait starts from, so no two look "
                      "alike. Synthetic faces from SFHQ, MIT License. Downloaded and unpacked into the faces "
                      "folder for you.").grid(row=0, column=0, sticky="w")
        fbtn = ttk.Button(fbox, text="Download faces")
        fbtn.grid(row=0, column=1, sticky="nw", padx=(10, 0))
        fmark = tk.Label(fbox, text="", bg=BG, font=("TkDefaultFont", 12, "bold"))
        fmark.grid(row=0, column=2, sticky="nw", padx=(6, 0))
        fnote = tk.Label(fbox, text="", bg=BG, fg="#5a4a00", justify="left", anchor="w")
        fnote.grid(row=1, column=0, columnspan=3, sticky="w")
        fprog = ttk.Progressbar(fbox, maximum=1000, length=420)

        def faces_state() -> None:
            if not fnote.winfo_exists():
                return
            n_ = faces_count()
            fmark.config(text="\u2714" if n_ else "\u2716", fg="#2e7d32" if n_ else "#b23b3b")
            fnote.config(text=f"{n_:,} faces in the faces folder." if n_ else "No faces yet: portraits start from "
                         "plain noise, and the same words paint the same face.")
            fbtn.config(text="Download again" if n_ else "Download faces")

        def faces_say(frac: Optional[float], text: str, done: bool = False) -> None:
            def ui() -> None:
                if not fnote.winfo_exists():
                    return
                if frac is None:
                    fprog.grid_remove()
                else:
                    fprog.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
                    fprog["value"] = int(frac * 1000)
                fnote.config(text=text)
                if done:
                    fbtn.state(["!disabled"])
                    fprog.grid_remove()
                    faces_state()
                    fnote.config(text=text)
            root.after(0, ui)

        def faces_get() -> None:
            here_ = Path(__file__).resolve().parent
            tmpd_ = here_ / "temp"
            part_ = tmpd_ / "faces-download.zip"
            try:
                tmpd_.mkdir(exist_ok=True)
                import urllib.request
                req_ = urllib.request.Request(FACES_URL, headers={"User-Agent": "PortraitScout/" + VERSION})
                with urllib.request.urlopen(req_, timeout=60) as r_, open(part_, "wb") as f_:
                    total_ = int(r_.headers.get("Content-Length") or 0)
                    got_ = 0
                    while True:
                        chunk_ = r_.read(1 << 20)
                        if not chunk_:
                            break
                        f_.write(chunk_)
                        got_ += len(chunk_)
                        faces_say(got_ / total_ if total_ else 0.0,
                                  f"Downloading: {got_ / 1e6:,.0f}" + (f" of {total_ / 1e6:,.0f} MB" if total_ else " MB"))
                made_ = 0
                with zipfile.ZipFile(part_) as z_:
                    names_ = [i_ for i_ in z_.infolist() if not i_.is_dir()]
                    for k_, i_ in enumerate(names_):
                        rel_ = i_.filename.replace("\\", "/")
                        if rel_.startswith("PortraitScout/"):         # packed to unzip beside the program's zip
                            rel_ = rel_[len("PortraitScout/"):]
                        if not rel_.startswith("faces/") or ".." in rel_.split("/"):
                            continue
                        dst_ = here_ / rel_
                        if dst_.is_file() and dst_.stat().st_size == i_.file_size:
                            continue                                 # already there
                        dst_.parent.mkdir(parents=True, exist_ok=True)
                        with z_.open(i_) as src_, open(dst_, "wb") as out_:
                            shutil.copyfileobj(src_, out_)
                        made_ += 1
                        if k_ % 50 == 0:
                            faces_say(k_ / max(1, len(names_)), f"Unpacking: {k_:,} of {len(names_):,} files")
                faces_say(None, f"Done: {made_:,} new file(s) in the faces folder.", done=True)
            except Exception as e_:  # noqa: BLE001 - no network, no release yet, a full disk...
                faces_say(None, f"Could not get the faces: {e_}. Download PortraitScout-faces.zip from the "
                                "release page and unzip it next to the program instead.", done=True)
            finally:
                try:
                    part_.unlink()
                except OSError:
                    pass

        def faces_start() -> None:
            fbtn.state(["disabled"])
            faces_say(0.0, "Starting the download...")
            threading.Thread(target=faces_get, daemon=True).start()
        fbtn.config(command=faces_start)
        tooltip(fbtn, "Download the face bank from the latest release and unpack it into the faces folder. "
                      "Faces already there are kept.")
        faces_state()
        # ---- defaults for new portraits ----
        dbox = tk.Frame(box, bg=BG)
        dbox.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Label(dbox, text="Portrait defaults:", bg=BG).pack(side="left", padx=(0, 8))
        try:
            pgd_ = generator_module()
            gsd_ = pgd_.load_settings()                              # type: ignore[attr-defined]
            for key_, lab_, terms_, tip_ in (
                    ("scar_default", "Scars", "scar_terms", "Scars a new portrait gets when you pick none: from None "
                     "to Wounded. Few, the default, adds a thin healed scar on one cheek."),
                    ("grime_default", "Grime", "grime_terms", "How dirty a new portrait's face is when you pick "
                     "nothing: from Clean to Filthy.")):
                names_ = [n_ for n_ in (gsd_.get(terms_) or {}) if not str(n_).startswith("_")]
                if not names_:
                    continue
                tk.Label(dbox, text=lab_ + ":", bg=BG).pack(side="left", padx=(8, 4))
                v_ = tk.StringVar(value=str(gsd_.get(key_) or names_[0]))
                cb_ = ttk.Combobox(dbox, textvariable=v_, values=names_, state="readonly", width=10)
                cb_.pack(side="left")
                tooltip(cb_, tip_)

                def set_default(_e: object = None, key_: str = key_, v_: tk.StringVar = v_, lab_: str = lab_) -> None:
                    try:
                        pgd_.save_patch(pgd_.load_settings(), {key_: v_.get()})   # type: ignore[attr-defined]
                    except Exception as e_:  # noqa: BLE001
                        messagebox.showwarning("Portrait Scout", str(e_), parent=box)
                        return
                    st["last_" + ("scars" if key_ == "scar_default" else "grime")] = v_.get()   # Propose's own pick
                    save_settings(st)
                    state.pop("gen", None)                           # read again at the next Propose
                    status.set(f"New portraits: {lab_} {v_.get()}.")
                cb_.bind("<<ComboboxSelected>>", set_default)
        except Exception:  # noqa: BLE001 - no generator here yet: nothing to set
            dbox.grid_remove()
        tk.Label(box, bg=BG, fg="#5a4a00", justify="left", wraplength=980,
                 text="Already run a generator of your own? None of the above is needed: open Generator settings on "
                      "the Propose tab, put its address in url, and turn \"start it for me\" off. Anything "
                      "with an Automatic1111 / Forge API will do, with any model.").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        more = tk.Frame(box, bg=BG)
        more.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(more, text="More information:", bg=BG, fg="#5a4a00").pack(side="left", padx=(0, 6))
        ttk.Button(more, text="About Forge",
                   command=lambda: webbrowser.open(FORGE_DOC)).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Other builds",
                   command=lambda: webbrowser.open(FORGE_ALL)).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Setup notes",
                   command=lambda: open_path(str(here / "generator" / "README.md"))).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Portrait Scout folder", command=lambda: open_path(str(here))).pack(side="left")
        return box

    def saved_sets() -> None:
        """Every set of proposals this copy of Portrait Scout has kept, with the chance to throw one away."""
        saved = st.get("proposals") or {}
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title("Saved proposals")
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, justify="left", wraplength=520,
                  text="Proposals are kept for each thing you analyze, under its name, and come back when you "
                       "analyze it again. Exporting does not clear them.").pack(anchor="w", pady=(0, 8))
        lb = tk.Listbox(f, width=60, height=min(12, max(3, len(saved) + 1)))
        lb.pack(fill="both", expand=True)
        names = sorted(saved)
        for n in names:
            lb.insert("end", f"{n}   -   {len(saved[n] or {})} portrait(s)")
        if not names:
            lb.insert("end", "(nothing saved yet)")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))

        def forget() -> None:
            sel = lb.curselection()
            if not names or not sel:
                return
            n = names[sel[0]]
            if not messagebox.askyesno("Portrait Scout", f"Forget the {len(saved[n] or {})} proposal(s) saved for "
                                       f"\"{n}\"? The pictures themselves stay where they are.", parent=w):
                return
            saved.pop(n, None)
            res = state["res"]
            if res and n == res["mod"]:                              # the one on screen
                props.clear()
                rebuild_keep_scroll()
                refresh_table_only()
            save_settings(st)
            update_prop_label()
            w.destroy()
        ttk.Button(row, text="Forget this set", command=forget).pack(side="left")
        ttk.Button(row, text="Close", command=w.destroy).pack(side="right")
    hist_btn.config(command=saved_sets)

    def setup_window() -> None:
        """The setup steps and their download links, on their own, for when the Propose tab is not where you are."""
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title("Setting up portrait generation")
        w.geometry("920x560")
        cv_ = tk.Canvas(w, highlightthickness=0)
        sb_ = ttk.Scrollbar(w, orient="vertical", command=cv_.yview)
        cv_.configure(yscrollcommand=sb_.set)
        cv_.pack(side="left", fill="both", expand=True)
        sb_.pack(side="right", fill="y")
        inner = ttk.Frame(cv_, padding=10)
        win_ = cv_.create_window(0, 0, window=inner, anchor="nw")
        cv_.bind("<Configure>", lambda e: cv_.itemconfigure(win_, width=e.width))
        inner.bind("<Configure>", lambda e: cv_.configure(scrollregion=cv_.bbox("all")))
        def rechecked() -> None:
            """Look again, then build the Propose tab afresh so the notice and the Generate button follow suit."""
            state.pop("gen", None)                                   # drop the cache so the module is read again
                                                                     # (setting it to None means "there is none")
            ready = engine_ready()                                   # the window stays: the faces may be next
            if state.get("gen_built"):
                r_ = state.get("gen_row")
                generate_dialog(r_ if isinstance(r_, dict) else None)
            status.set("Image engine: found in the engine folder." if ready else
                       "Image engine: still nothing in the engine folder - Forge's webui.bat is what is looked for.")

        try:
            setup_panel(inner, rechecked).pack(fill="x")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ttk.Label(inner, foreground="#a03030", wraplength=840, justify="left",
                      text=f"The steps could not be drawn: {e.__class__.__name__}: {e}").pack(anchor="w")
        det = ttk.Frame(inner, padding=(0, 10, 0, 0))
        det.pack(fill="x")

        def fit() -> None:
            """As big as the steps need, so nothing scrolls - unless the screen is smaller than that. Measured
            again once the file check has filled in its notes; it only ever grows."""
            if not w.winfo_exists():
                return
            w.update_idletasks()
            ww_ = min(max(w.winfo_width(), inner.winfo_reqwidth() + sb_.winfo_reqwidth() + 40), w.winfo_screenwidth() - 40)
            hh_ = min(max(w.winfo_height(), inner.winfo_reqheight() + 30), w.winfo_screenheight() - 90)
            w.geometry(f"{ww_}x{hh_}+{max(0, (w.winfo_screenwidth() - ww_) // 2)}+10")
        w.after(50, fit)
        w.after(700, fit)
        ttk.Label(det, foreground="#555", wraplength=840, justify="left",
                  text="The names, sizes and links above are what Portrait Scout was written against, as they stood "
                       "in November 2026. Addresses move: if a button opens nothing, search the file name - they are "
                       "all well known. Other models work too; name the checkpoint in payload.override_settings and "
                       "whatever LoRA you have in the style's prompt. SDXL and SD1.5 want a CFG around 5 to 7 and no "
                       "distilled CFG, where Flux wants CFG 1 and distilled CFG about 1.6."
                  ).pack(anchor="w")

    def gen_hint(text: str = "") -> None:
        """Empty the Propose tab (so it is built again next time it is shown), with a message if there is one."""
        state["gen_built"] = False
        for w in tab_gen.winfo_children():
            w.destroy()
        if not text:
            return
        box = ttk.Frame(tab_gen, padding=20)
        box.pack(anchor="w", fill="x")
        ttk.Label(box, foreground="#555", justify="left", wraplength=840, text=text).pack(anchor="w", pady=(0, 10))
        setup_panel(box, lambda: (state.pop("gen", None), gen_hint(), show_view())).pack(anchor="w", fill="x")

    show_view()

    def table_double(_e: object) -> None:
        sel = tree.selection()
        if sel:
            idx = tree.index(sel[0])
            rows = sorted_rows()
            if 0 <= idx < len(rows):
                show_lines(rows[idx])

    tree.bind("<Double-Button-1>", table_double)

    def table_menu(e: object) -> None:
        row = tree.identify_row(e.y)                                 # type: ignore[attr-defined]
        if not row:
            return
        tree.selection_set(row)
        rows = sorted_rows()
        idx = tree.index(row)
        if 0 <= idx < len(rows):
            row_menu(e, rows[idx], root)
    tree.bind("<Button-3>", table_menu)

    def copy_text(txt: str) -> None:
        root.clipboard_clear()
        root.clipboard_append(txt)
        status.set(f"Copied {txt} - paste it into NearInfinity's search")

    def table_click(e: object) -> None:
        if tree.identify("region", e.x, e.y) != "cell":                 # type: ignore[attr-defined]
            return
        col = tree.identify_column(e.x)                                 # type: ignore[attr-defined]
        row = tree.identify_row(e.y)                                    # type: ignore[attr-defined]
        if row and cols[int(col[1:]) - 1] == "cre":
            names = [n.strip().strip("[]") for n in str(tree.set(row, "cre")).split(",") if n.strip()]
            if names:
                copy_text(names[0] if len(names) == 1 else ", ".join(names))
        elif row and cols[int(col[1:]) - 1] == "name" and str(tree.set(row, "name")).strip():
            copy_text(str(tree.set(row, "name")).strip())

    tree.bind("<Button-1>", table_click, add="+")
    total_var = tk.StringVar(value="")                               # always visible: what is loaded
    foot = ttk.Frame(root)
    foot.pack(fill="x")
    ttk.Label(foot, textvariable=total_var, anchor="e", padding=(8, 2), font=("TkDefaultFont", 9, "bold")).pack(side="right")
    ttk.Label(foot, textvariable=status, anchor="w", padding=(8, 2)).pack(side="left", fill="x", expand=True)

    def row_text(r: dict) -> str:
        pp = props.get(rowkey(r))
        return " ".join([str(r.get("name", "")), " ".join(r.get("cre_list", [])), str(r.get("large", "")),
                         str(r.get("small", "")), str(r.get("race", "")), str(r.get("sex", "")), str(r.get("anim", "")),
                         " ".join(r.get("dialogs", [])), Path(pp).name if pp else "",
                         " ".join(f"{a} {nm}" for a, nm, _h in r.get("areas", []))]).lower()

    def sorted_rows(filtered: bool = True) -> List[dict]:
        res = state["res"]
        if not res:
            return []
        key, desc = state["sort"]                                    # type: ignore[misc]
        rows = all_rows()
        if not party_var.get():                                  # joinable NPCs: the game's own and the mods' own
            rows = [r for r in rows if not r.get("party")]
        if not show_skip_var.get() and skip:                     # the ones marked as not wanting a portrait
            rows = [r for r in rows if rowkey(r) not in skip]
        terms = search_var.get().lower().split() if filtered else []
        if terms:
            rows = [r for r in rows if all(t in row_text(r) for t in terms)]
        rc_ = race_pick.get() if filtered else "All"
        if rc_ and rc_ != "All":                                     # one race at a time: the goblins, the dwarves
            rows = [r for r in rows if str(r.get("race") or "").strip().lower() == rc_.lower()]
        on_ = [fn for lbl, fn in CHIPS if chip_vars[lbl].get()]     # the chips narrow the same view
        if on_:
            rows = [r for r in rows if all(fn(r) for fn in on_)]
        if key == "borrowed":                                    # borrowed faces first, those sharing one together
            rows.sort(key=lambda r: (-int(r.get("lines", 0) or 0), str(r.get("name", "")).lower()))
            rows.sort(key=lambda r: (not r.get("copy_port"), str(r.get("copy_port") or "").lower()), reverse=desc)
            return rows
        rows.sort(key=lambda r: (r[key] if key == "lines" else str(r[key]).lower()), reverse=desc)
        return rows

    def refresh() -> None:
        tree.delete(*tree.get_children())
        rows = sorted_rows()
        if not rows:
            blank = dict.fromkeys([k for k, _h in COLUMNS] + ["psize", "proposed"], "")
            blank["name"] = "(nothing analyzed yet)" if not state["res"] else "(nothing matches the search)"
            tree.insert("", "end", values=[blank[k] for k, _h in COLUMNS] + ["", ""])
        for r in rows:
            pp = props.get(rowkey(r))
            tree.insert("", "end", values=table_values(r))
        key, desc = state["sort"]                                    # type: ignore[misc]
        for k, h in COLUMNS:
            tree.heading(k, text=h + ((" ▼" if desc else " ▲") if k == key else ""))
        total = len(all_rows())
        count_var.set(f"{len(rows)} of {total}" if search_var.get().strip() and total else "")
        res_ = state["res"]
        if res_:
            unk = res_.get("stats", {}).get("dialogs not linked", 0) if res_.get("source") else 0       # type: ignore[union-attr]
            total_var.set(f"{res_['mod']}: "                                                         # type: ignore[index]
                          + (f"{len(rows)} of " if len(rows) != total else "") + f"{total} creature(s), "
                          + ("party NPCs hidden, " if not party_var.get() else "")
                          + (f"{len(skip)} ignored, " if skip and not show_skip_var.get() else "")

                          + f"{sum(r['lines'] for r in res_['rows']):,} line(s)"                     # type: ignore[index]
                          + ((" - from the mod's files" + (", EET" if res_.get("eet") else "")          # type: ignore[union-attr]
                              + (f", {unk} dialog(s) without a speaker" if unk else "")) if res_.get("source") else ""))   # type: ignore[union-attr]
        if view_var.get() == "gallery":
            build_gallery(rows)
        show_lost()                                                  # the same creatures the boxes above list

    def on_search(*_a: object) -> None:
        if state.get("search_job"):
            root.after_cancel(state["search_job"])                   # type: ignore[arg-type]

        def go() -> None:
            state["search_job"] = None
            state["page"] = 0
            auto_ui["page"] = 0                                      # the Auto view's pages too
            refresh()
        state["search_job"] = root.after(250, go)

    search_var.trace_add("write", on_search)
    root.bind("<Control-f>", lambda e: (search_entry.focus_set(), search_entry.select_range(0, "end")))


    def sort_by(col: str) -> None:
        key, desc = state["sort"]                                    # type: ignore[misc]
        state["sort"] = (col, (not desc) if col == key else (col == "lines"))
        sort_var.set(next((lbl for lbl, kd in SORTS if kd == state["sort"]), ""))
        refresh()

    def sort_picked(*_a: object) -> None:
        for lbl, kd in SORTS:
            if lbl == sort_var.get():
                state["sort"], state["page"] = kd, 0
                refresh()
                return
    sort_box.bind("<<ComboboxSelected>>", sort_picked)

    def party_changed(*_a: object) -> None:
        st["party_npcs"] = bool(party_var.get())
        save_settings(st)
        state["page"] = 0
        refresh()
    party_cb.config(command=party_changed)

    def skip_changed(*_a: object) -> None:
        state["page"] = 0
        refresh()
        if view_var.get() == "gallery":
            build_gallery(sorted_rows())
    skip_cb.config(command=skip_changed)

    def all_rows() -> List[dict]:
        """Every creature as it is listed now: one row per NPC, or one per creature when decloned."""
        res = state["res"]
        if not res:
            return []
        split = set((st.get("declone") or {}).get(str(res["mod"]), []))   # type: ignore[index]
        rows: List[dict] = []
        for r in res["rows"]:                                        # type: ignore[union-attr]
            rows += r["parts"] if (r.get("parts") and rowkey(r) in split) else [r]
        return rows

    def clones_of(r: dict) -> Tuple[Optional[dict], int]:
        """The row a creature was merged into, and how many creatures that row stands for."""
        par = r.get("_parent") or (r if r.get("parts") else None)
        return par, len(par["parts"]) if par else 0

    def declone_label(r: dict) -> Tuple[str, str]:
        """(button, what it does) for taking a name-only merge apart, or putting it back."""
        par, n = clones_of(r)
        if not par:
            return "", ""
        res = state["res"]
        split = set((st.get("declone") or {}).get(str(res["mod"]), [])) if res else set()
        if rowkey(par) in split:
            return "Merge", f"Put the {n} \"{par['name']}\" creatures back in one row."
        return "Declone", (f"List the {n} \"{par['name']}\" creatures one by one, so each can have a "
                           "portrait of its own.")

    def toggle_declone(r: dict) -> None:
        par, n = clones_of(r)
        res = state["res"]
        if not par or not res:
            return
        d = st.setdefault("declone", {})
        lst = [k for k in (d.get(str(res["mod"])) or []) if k]       # type: ignore[index]
        k = rowkey(par)
        if k in lst:
            lst.remove(k)
            status.set(f"{par['name']}: back in one row.")
        else:
            lst.append(k)
            status.set(f"{par['name']}: listed as {n} separate creatures.")
        d[str(res["mod"])] = lst                                     # type: ignore[index]
        save_settings(st)
        state["page"] = 0
        refresh()

    def mark_party(res: dict) -> None:
        """Flag the creatures the game (or a mod) treats as joinable NPCs: they are in the party dialog tables."""
        names: Set[str] = set()
        try:
            game = res["game"]
            for t in ("PDIALOG", "INTERDIA", "BDIALOG"):
                txt = (game.get(t, T_2DA) or b"").decode("latin-1", "replace")
                for ln in txt.splitlines()[3:]:                      # 2DA: signature, default, column names, then rows
                    f = ln.split()
                    if f:
                        names.add(f[0].upper())
                        names |= {x.upper() for x in f[1:] if x not in ("***", "*")}
            # portrait.2da holds the three portrait sizes of every joinable NPC, which is the only place some
            # of them are named: an EET game can carry BG1's NPCs without putting them in the dialog tables.
            # Only the name of the picture's owner is taken, never the picture, so a creature merely wearing
            # a party NPC's face is not mistaken for one.
            txt = (game.get("PORTRAIT", T_2DA) or b"").decode("latin-1", "replace")
            for ln in txt.splitlines()[3:]:
                f = ln.split()
                if f and len(f[0]) > 1 and f[0][-1].upper() in "LMS":
                    names.add(f[0][:-1].upper())
        except (OSError, AttributeError, ValueError):
            names = set()
        for r in [x for r_ in res["rows"] for x in ([r_] + (r_.get("parts") or []))]:
            r["party"] = bool(names and ({d.upper() for d in r["dialogs"]} & names
                                         or {c.upper() for c in (r.get("cre_list") or [])} & names))

    def rowkey(r: dict) -> str:
        return ",".join(sorted(set(r["dialogs"])))

    def remap_keys(keys: Iterable[str], targets: List[dict]) -> Dict[str, str]:
        """Old key -> the creature it stands for now, for keys that match nothing any more.

        Proposals, ignores and declones are all kept under the dialogs of the creature they were made for, so they are
        lost whenever those dialogs are grouped differently: reading the mod's own files finds dialogs the installed
        game does not, and a newer version may split, merge or re-attribute a dialog. Such a key is handed to the
        creature it shares the most dialogs with, preferring one that stands for about as many dialogs (so a single
        creature's key goes to that creature and not to the bunch it was merged into). One creature takes at most one
        old key."""
        live = {rowkey(r): r for r in targets}
        by_dlg: Dict[str, List[str]] = defaultdict(list)
        for k, r in live.items():
            if k:
                for d in set(r["dialogs"]):
                    by_dlg[d].append(k)
        keys = [k for k in keys if k]
        taken = {k for k in keys if k in live}
        out: Dict[str, str] = {}
        for key in sorted(keys, key=lambda k: (-len(k.split(",")), k)):
            if key in live:
                continue
            want = len(set(key.split(",")))
            votes: Counter = Counter()
            for d in set(key.split(",")):
                for cand in by_dlg.get(d, ()):
                    votes[cand] += 1
            best = [(n, -abs(len(set(live[c]["dialogs"])) - want), c) for c, n in votes.items() if c not in taken]
            if best:
                _n, _near, c = max(best)
                out[key], _ = c, taken.add(c)
        return out

    dims_cache: Dict[str, str] = {}

    def prop_dims(path: str) -> str:
        if path not in dims_cache:
            inf = image_info(path)
            dims_cache[path] = f"{inf['w']}x{inf['h']}" if inf else ""
        return dims_cache[path]

    def table_values(r: dict) -> List[str]:
        pp = props.get(rowkey(r))
        psize = " / ".join(x for x in (r.get("large_size", ""), r.get("small_size", "")) if x)
        prop_txt = ""
        if pp:
            d = prop_dims(pp)
            prop_txt = Path(pp).name + (f" ({d})" if d else "")
        out = [shown_sex(r[k]) if k == "sex" else str(r[k]) for k in cols] + [psize, prop_txt]
        if rowkey(r) in skip and "name" in cols:                     # listed only while Ignored is ticked
            out[cols.index("name")] += "   (ignored)"
        return out

    def save_image(parent: object, fname: str, data: Optional[bytes], src: Optional[str]) -> None:
        import shutil
        p = filedialog.asksaveasfilename(parent=parent, initialfile=fname, defaultextension=os.path.splitext(fname)[1],
                                         filetypes=[("Image", "*" + (os.path.splitext(fname)[1] or ".bmp")), ("All files", "*.*")])
        if p:
            try:
                if src:
                    shutil.copyfile(src, p)
                else:
                    Path(p).write_bytes(data or b"")
                status.set(f"Saved {p}")
            except OSError as e:
                messagebox.showerror("Portrait Scout", f"Could not save the image:\n{e}", parent=parent)

    def row_menu(event: object, r: dict, parent: object) -> None:
        """The right-click menu of a creature, wherever it is shown."""
        menu = tk.Menu(parent, tearoff=0)
        on = rowkey(r) in skip
        menu.add_command(label="Stop ignoring this creature" if on else "Ignore this creature",
                         command=lambda: set_skipped(r, not on))
        keep_item(menu, r)
        try:
            menu.tk_popup(event.x_root, event.y_root)                # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def touch_this(r: dict, pp: str, parent: object) -> None:
        """Work on the picture this creature has now. Whatever comes out of it becomes the proposal."""
        pg_ = generator_module()
        if pg_ is None:
            return
        try:
            gs_ = pg_.load_settings()                                # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 - the generator may be half installed
            status.set(f"The generator settings could not be read ({e}).")
            return
        try:
            desc_ = pg_.describe(r, gs_)[1]                          # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - only used to guess a starting color
            desc_ = ""

        def took(_old: str, new_: str, _drop: str = "") -> None:
            props[rowkey(r)] = os.path.normpath(new_)
            persist_props()
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            status.set(f"{Path(new_).name} is now the portrait proposed for {r['name']}.")
        touch_up_window(parent, pp, pg=pg_, gs=gs_, r=r, desc=desc_, say=status.set,
                        on_done=took, touch_memory=TOUCH_MEMORY)

    def keep_item(menu: object, r: dict) -> None:
        """The keep-this-borrowed-face line, on every menu a creature has."""
        if not r.get("copy_port"):
            return
        on = face_kept(r)
        menu.add_command(label=f"Give {r['name']} a face of its own" if on   # type: ignore[attr-defined]
                         else f"Keep {r['copy_port']} on {r['name']}",
                         command=lambda: set_kept(r, not on))

    def image_menu(event: object, r: dict, parent: object) -> None:
        res = state["res"]
        if not res:
            return
        game: Game = res["game"]                                     # type: ignore[index]
        menu = tk.Menu(parent, tearoff=0)
        on = rowkey(r) in skip
        menu.add_command(label="Stop ignoring this creature" if on else "Ignore this creature",
                         command=lambda: set_skipped(r, not on))
        keep_item(menu, r)
        menu.add_separator()
        pp = props.get(rowkey(r))
        n = 0
        if pp and os.path.isfile(pp):
            d = prop_dims(pp)
            menu.add_command(label=f"Save proposed image {Path(pp).name}{' (' + d + ')' if d else ''} as...",
                             command=lambda: save_image(parent, Path(pp).name, None, pp))
            n += 1
        seen = set()
        for field in ("small", "large"):
            nm = r.get(f"{field}_name", "")
            if nm and nm not in seen and game.has(nm, T_BMP):
                seen.add(nm)
                sz = r.get(f"{field}_size", "")
                menu.add_command(label=f"Save {nm}.bmp{' (' + sz + ')' if sz else ''} as...",
                                 command=lambda nm=nm: save_image(parent, nm + ".bmp", game.get(nm, T_BMP), None))
                n += 1
        if not n:
            menu.add_command(label="No image to save", state="disabled")
        pg_ = generator_module()
        if pp and os.path.isfile(pp) and pg_ is not None and hasattr(pg_, "touch_up"):
            menu.add_separator()
            menu.add_command(label=f"Touch up {Path(pp).name}...", command=lambda: touch_this(r, pp, parent))
        lbl, why = declone_label(r)
        if lbl:
            menu.add_separator()
            menu.add_command(label=f"{lbl}  -  {why}", command=lambda: toggle_declone(r))
        try:
            menu.tk_popup(event.x_root, event.y_root)                # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def listed_lost() -> Dict[str, str]:
        """Missing pictures of creatures that are listed now. A bunch taken apart one by one is not listed
        itself - its creatures are - so a picture once proposed for the whole bunch is not counted: it could
        not be shown, and the Missing box would find nothing to show for it."""
        res_ = state["res"]
        rows_ = all_rows()
        if res_ and not party_var.get():
            rows_ = [o for o in rows_ if not o.get("party")]
        if not show_skip_var.get():
            rows_ = [o for o in rows_ if rowkey(o) not in skip]
        keys_ = {rowkey(o) for o in rows_}
        return {k: v for k, v in lost_proposals(props).items() if k in keys_}

    def show_lost() -> None:
        """The button appears only when a proposed picture has gone missing, and says how many."""
        n = len(listed_lost())
        if n and not lost_btn.winfo_ismapped():
            lost_btn.pack(side="left", padx=(6, 0))
        elif not n and lost_btn.winfo_ismapped():
            lost_btn.pack_forget()
        if n:
            lost_btn.config(text=f"{n} missing picture(s)...")

    def fix_lost() -> None:
        """Point the proposals that lost their picture at another crop of the same one, where there is one."""
        lost = listed_lost()
        if not lost:
            return
        can = {k: v for k, v in lost.items() if v}
        name = {rowkey(o): o["name"] for o in all_rows()}
        lines = [f"  {name.get(k, k)}: {Path(props[k]).name}" + (f"\n      -> {Path(v).name}" if v else
                 "\n      -> nothing like it is left") for k, v in list(lost.items())[:12]]
        more = f"\n  ...and {len(lost) - 12} more" if len(lost) > 12 else ""
        if not can:
            messagebox.showinfo("Portrait Scout", f"{len(lost)} proposed picture(s) are no longer on the disk, "
                                "and there is no other crop of them to use instead:\n\n" + "\n".join(lines) + more
                                + "\n\nPropose something else for them, or put the files back.", parent=root)
            return
        if not messagebox.askyesno("Portrait Scout", f"{len(lost)} proposed picture(s) are no longer on the disk. "
                                   f"{len(can)} of them can point at another crop of the same picture:\n\n"
                                   + "\n".join(lines) + more + "\n\nPoint them at it?", parent=root):
            return
        for k, v in can.items():
            props[k] = v
        persist_props()
        if view_var.get() == "gallery":
            rebuild_keep_scroll()
        refresh_table_only()
        status.set(f"{len(can)} proposal(s) now point at another crop of the same picture"
                   + (f"; {len(lost) - len(can)} still have nothing to use." if len(can) < len(lost) else "."))

    def update_prop_label() -> None:
        res = state["res"]
        show_lost()
        prop_label.set(f"Proposed portraits: {len(props)}")
        prop_where.set(f"kept for \"{res['mod']}\", and remembered between sessions" if res else "")   # type: ignore[index]
        if view_var.get() == "export":
            fill_export()

    def persist_props() -> None:
        res = state["res"]
        if res:
            st.setdefault("proposals", {})[res["mod"]] = dict(props)          # type: ignore[index]
            save_settings(st)
        update_prop_label()

    def face_kept(r: dict) -> bool:
        """Whether this creature's borrowed face is the right one to keep. Judged by hand where it has been
        judged; otherwise Analyze's guess, which is weak - a template copied only once is called "the same
        creature", and most are (258 of CotSC's are), so Shandalar and a generic beggar look alike to it."""
        if not r.get("copy_port"):
            return False
        return bool(kept.get(rowkey(r), bool(r.get("copy_named"))))

    def set_kept(r: dict, on: bool) -> None:
        """Keep this creature's borrowed face, or hand it back for a portrait of its own."""
        res = state["res"]
        kept[rowkey(r)] = bool(on)
        if res:
            st.setdefault("kept_faces", {})[res["mod"]] = dict(kept)         # type: ignore[index]
            save_settings(st)
        refresh_table_only()
        if view_var.get() == "gallery":
            rebuild_keep_scroll()
        status.set(f"{r['name']} keeps {r.get('copy_port')}, and the mod writes it in." if on else
                   f"{r['name']} wants a face of its own; {r.get('copy_port')} is not written in.")

    def set_skipped(r: dict, on: bool) -> None:
        """Mark a creature as not wanting a portrait, or let it back in. Kept per mod, between sessions."""
        res = state["res"]
        k = rowkey(r)
        skip.add(k) if on else skip.discard(k)
        if res:
            st.setdefault("ignored", {})[res["mod"]] = sorted(skip)           # type: ignore[index]
            save_settings(st)
        state["page"] = 0
        refresh()
        if view_var.get() == "gallery":
            build_gallery(sorted_rows())
        status.set(f"{r['name']} is ignored." + ("" if show_skip_var.get() else " Tick Ignored to see it again.")
                   if on else f"{r['name']} is listed again.")

    def thumb_from_bytes(data: bytes) -> object:
        """A gallery-sized picture from the bytes of a .bmp, the same box a proposed picture gets."""
        try:
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((THUMB_W, THUMB_H))
            return ImageTk.PhotoImage(im)
        except Exception:  # noqa: BLE001
            try:
                w = abs(struct.unpack_from("<i", data, 18)[0])
                h = abs(struct.unpack_from("<i", data, 22)[0])
                ppm = bmp_to_ppm(data, max(1, -(-w // THUMB_W), -(-h // THUMB_H)))
                return tk.PhotoImage(data=ppm) if ppm else None
            except (struct.error, IndexError, tk.TclError):
                return None

    def load_thumb(path: str) -> object:
        cache: Dict[str, object] = state.setdefault("thumbs", {})             # type: ignore[assignment]
        if path in cache:
            return cache[path]
        img = None
        try:
            from PIL import Image, ImageTk                                      # optional: any image format
            im = Image.open(path)
            im.thumbnail((THUMB_W, THUMB_H))
            img = ImageTk.PhotoImage(im)
        except Exception:  # noqa: BLE001
            try:
                if path.lower().endswith(".bmp"):
                    data = open(path, "rb").read()
                    w = abs(struct.unpack_from("<i", data, 18)[0])
                    ppm = bmp_to_ppm(data, max(1, -(-w // THUMB_W)))
                    img = tk.PhotoImage(data=ppm) if ppm else None
                else:
                    pi = tk.PhotoImage(file=path)                               # png / gif
                    k = max(1, -(-pi.width() // THUMB_W), -(-pi.height() // THUMB_H))
                    img = pi.subsample(k, k) if k > 1 else pi
            except Exception:  # noqa: BLE001
                img = None
        cache[path] = img
        return img

    def rebuild_keep_scroll() -> None:
        pos = gcanvas.yview()[0]
        build_gallery(sorted_rows())
        root.update_idletasks()
        gcanvas.yview_moveto(pos)

    def propose(r: dict, after: object = None, parent: object = None) -> None:
        par = parent or root                          # dialogs belong to the window they were opened from
        p = filedialog.askopenfilename(
            parent=par, title=f"Proposed portrait for {r['name']}", initialdir=os.path.dirname(props.get(rowkey(r), "")) or None,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tga *.tif *.tiff"), ("All files", "*.*")])
        if p:
            crop_store().pop(rowkey(r), None)                        # a new picture, so any old framing is gone
            props[rowkey(r)] = os.path.normpath(p)
            persist_props()
            inf = image_info(p)
            size_ = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) else [169, 266]
            pw_, ph_ = int(size_[0]), int(size_[1])
            ready = bool(inf) and inf["fmt"] == "BMP" and inf["bpp"] in (8, 24) and (inf["w"], inf["h"]) == (pw_, ph_)
            if inf and not ready:                                    # any picture will do: frame it and cut it out
                frame_dialog(r, par, after)
                return
            if not inf:
                messagebox.showwarning("Portrait Scout", f"{Path(p).name} could not be read as an image. It is "
                                       "proposed anyway, but it may not work as a portrait.", parent=par)
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            if callable(after):
                after()                                                              # type: ignore[operator]
        if parent is not None:
            parent.lift()                                                            # type: ignore[attr-defined]
            parent.focus_force()                                                     # type: ignore[attr-defined]

    checked: set = set()                                             # rowkeys ticked in the gallery

    def upd_pick_count() -> None:
        pick_count.set(f"{len(checked):,} checked" if checked else "none checked")

    def pick_set(which: str) -> None:
        rows = sorted_rows()
        checked.clear()
        if which == "all":
            checked.update(rowkey(r) for r in rows)
        elif which == "proposals":
            checked.update(rowkey(r) for r in rows if rowkey(r) in props)
        upd_pick_count()
        build_gallery(rows)

    def harvest_current() -> None:
        """Give the ticked creatures the portrait they have now, so the built mod does not rely on whatever else
        happens to be installed. Pictures that come with the game are never taken - everyone has those - and nor,
        by default, are the ones this very mod ships, since installing the mod brings them anyway."""
        res = state["res"]
        if not res or not checked:
            messagebox.showinfo("Portrait Scout", "Tick some creatures in the gallery first.", parent=root)
            return
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title("Copy their portraits")
        w.transient(root)
        bx = ttk.Frame(w, padding=12)
        bx.pack(fill="both", expand=True)
        ttk.Label(bx, text=f"{len(checked):,} creature(s) ticked. Each gets, as a proposal, the portrait it is "
                           "wearing in your game now.", wraplength=460, justify="left").pack(anchor="w")
        skip_mod = tk.BooleanVar(value=True)
        keep_w = tk.BooleanVar(value=True)
        c1 = ttk.Checkbutton(bx, text="Skip mod portraits", variable=skip_mod)
        c1.pack(anchor="w", pady=(8, 0))
        tooltip(c1, "Installing the mod brings those pictures with it, so copying them into your portrait mod "
                    "only duplicates them.")
        c2 = ttk.Checkbutton(bx, text="Keep my choices", variable=keep_w)
        c2.pack(anchor="w")
        tooltip(c2, "On: a creature you have already proposed a picture for keeps it. Untick to let the portrait it "
                    "wears in the game replace your choice.")
        ttk.Label(bx, text="Portraits that come with the game are never copied - everyone has those already.",
                  foreground="#666", wraplength=460, justify="left").pack(anchor="w", pady=(8, 0))
        go2 = {"ok": False}
        rw = ttk.Frame(bx)
        rw.pack(anchor="e", pady=(12, 0))
        ttk.Button(rw, text="Copy them", command=lambda: (go2.__setitem__("ok", True), w.destroy())).pack(side="left")
        ttk.Button(rw, text="Cancel", command=w.destroy).pack(side="left", padx=(6, 0))
        w.grab_set()
        root.wait_window(w)
        if not go2["ok"]:
            return
        game: Game = res["game"]                                      # type: ignore[index]
        over = getattr(game, "over", {}) or {}
        own = mod_bmp_index() if skip_mod.get() else {}               # the pictures this mod itself ships
        out = (Path(__file__).resolve().parent / "generated"
               / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", str(res["mod"])) / "current")     # type: ignore[index]
        rows = [r for r in all_rows() if rowkey(r) in checked]
        took, biffed, none_, failed, mine, lent = 0, 0, 0, 0, 0, 0
        for r in rows:
            if rowkey(r) in props and keep_w.get():                  # a picture was already chosen for this one
                continue
            nm = next((x for x in (r.get("large_name"), r.get("small_name")) if x), "")
            borrowed = ""
            if not nm and r.get("copy_port"):                        # wears none of its own, so the face Analyze
                nm, borrowed = str(r["copy_port"]), str(r.get("copy_of") or "")   # found on the creature it was
            if not nm:                                               # copied from is the one to take
                none_ += 1
                continue
            if not borrowed and (nm.lower() + ".bmp") not in over:   # it comes with the game: everyone has it
                biffed += 1                                          # (a borrowed one is taken either way: the
                continue                                             # creature wearing it has none of its own)
            if nm.upper() in own:                                    # the mod ships it, so installing the mod brings it
                mine += 1
                continue
            data = game.get(nm, T_BMP)
            if not data:
                failed += 1
                continue
            try:
                out.mkdir(parents=True, exist_ok=True)
                dst = out / (nm.upper() + ".bmp")
                dst.write_bytes(data)
            except OSError:
                failed += 1
                continue
            crop_store().pop(rowkey(r), None)
            props[rowkey(r)] = os.path.normpath(str(dst))
            took += 1
            lent += 1 if borrowed else 0
        persist_props()
        if view_var.get() == "gallery":
            rebuild_keep_scroll()
        refresh_table_only()
        fill_export()
        msg = f"Proposed the portrait {took:,} creature(s) have now, copied from override into {out}."
        extra = []
        if biffed:
            extra.append(f"{biffed:,} already had a portrait that comes with the game, so nothing was copied")
        if mine:
            extra.append(f"{mine:,} wear a picture this mod ships itself, which installing it brings anyway")
        if lent:
            extra.append(f"{lent:,} wear nothing of their own and took the face of the creature they were copied "
                         "from - the same face is on somebody else too, so these are the ones worth a picture")
        if none_:
            extra.append(f"{none_:,} have no portrait at all")
        if failed:
            extra.append(f"{failed:,} could not be read or written")
        status.set(msg)
        messagebox.showinfo("Portrait Scout", msg + ("\n\n" + ".\n".join(extra) + "." if extra else ""), parent=root)

    def show_pick_bar() -> None:
        on = bool(pick_var.get())
        for w in pick_bar.winfo_children():
            w.destroy()
        if on:
            ttk.Label(pick_bar, text="Tick:").pack(side="left", padx=(0, 4))
            ab = ttk.Button(pick_bar, text="All", width=5, command=lambda: pick_set("all"))
            ab.pack(side="left")
            tooltip(ab, "Tick every creature the gallery is showing now - the search box and Party NPCs decide that.")
            pb = ttk.Button(pick_bar, text="Proposals", width=10, command=lambda: pick_set("proposals"))
            pb.pack(side="left", padx=2)
            tooltip(pb, "Tick only the creatures you have already chosen a picture for.")
            nb = ttk.Button(pick_bar, text="None", width=6, command=lambda: pick_set("none"))
            nb.pack(side="left")
            tooltip(nb, "Clear every tick.")
            ttk.Label(pick_bar, textvariable=pick_count, foreground="#555").pack(side="left", padx=8)
            hb = ttk.Button(pick_bar, text="Copy their portraits...", command=harvest_current)
            hb.pack(side="left", padx=(8, 0))
            tooltip(hb, "Give the ticked creatures, as a proposal, the portrait each is wearing in your game now, "
                        "so the mod you build carries it instead of relying on whatever else the player has "
                        "installed.\n\nIt asks what to leave out before it does anything.")
            pick_bar.pack(side="left")
            gbar2.pack(side="top", fill="x", after=gbar)
        else:
            pick_bar.pack_forget()
            gbar2.pack_forget()
        upd_pick_count()
        if state["res"]:
            build_gallery(sorted_rows())
    pick_cb.config(command=show_pick_bar)

    def propose_folder() -> None:
        """A whole set of pictures at once: match the files in a folder to creatures by name.

        Portrait projects name a picture after the creature file it belongs to (HPS's ACHEN.bmp is for ACHEN.CRE),
        which is also what Create mod writes, so one pass can propose thousands."""
        if not state["res"]:
            messagebox.showinfo("Portrait Scout", "Analyze a mod or a game first.", parent=root)
            return
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title("Propose from a folder")
        w.transient(root)
        box = ttk.Frame(w, padding=12)
        box.pack(fill="both", expand=True)
        dir_var = tk.StringVar(value=str(st.get("propose_folder") or ""))
        ttk.Label(box, text="Folder of pictures:").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=dir_var, width=62).grid(row=0, column=1, sticky="ew", padx=6)

        def pick() -> None:
            d = filedialog.askdirectory(parent=w, title="Folder of portraits", initialdir=dir_var.get() or None)
            if d:
                dir_var.set(d)
                count()
        ttk.Button(box, text="Browse...", command=pick).grid(row=0, column=2)
        sub_var = tk.BooleanVar(value=True)
        by_cre = tk.BooleanVar(value=True)
        by_port = tk.BooleanVar(value=True)
        over_var = tk.BooleanVar(value=False)
        gap_var = tk.BooleanVar(value=False)
        opts = ttk.Frame(box)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        for txt, var, tip in (
                ("Look in subfolders", sub_var, "HPS keeps its pictures in portraits/core, portraits/minor_npcs and "
                                                "so on, so this is usually wanted."),
                ("Match the creature file name", by_cre, "ACHEN.bmp is proposed for ACHEN.CRE. This is how HPS and "
                                                         "Create mod name their pictures."),
                ("Match the portrait name", by_port, "HALFG3.bmp is proposed for every creature whose portrait is "
                                                     "HALFG3 now - a stock picture shared by many creatures."),
                ("Replace proposals already made", over_var, "Off: creatures you have already chosen a picture for "
                                                             "are left alone."),
                ("Only creatures with no portrait now", gap_var, "Fill the gaps and nothing else.")):
            cb = ttk.Checkbutton(opts, text=txt, variable=var, command=lambda: count())
            cb.pack(anchor="w")
            tooltip(cb, tip)
        note = tk.StringVar(value="")
        ttk.Label(box, textvariable=note, foreground="#1a5fb4", wraplength=520, justify="left"
                  ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        def index() -> Dict[str, str]:
            """picture name (no extension, upper case) -> the file, .bmp winning over the rest."""
            d = dir_var.get().strip()
            found: Dict[str, str] = {}
            if not os.path.isdir(d):
                return found
            for root_, dirs, names in (os.walk(d) if sub_var.get() else [(d, [], os.listdir(d))]):
                dirs[:] = [x for x in dirs if x.lower() not in ("backup", "tutu", "bgt")]
                for n in names:
                    stem_, ext = os.path.splitext(n)
                    if ext.lower() not in (".bmp", ".png", ".jpg", ".jpeg", ".tga", ".webp", ".gif"):
                        continue
                    k = stem_.upper()
                    if k not in found or (ext.lower() == ".bmp" and not found[k].lower().endswith(".bmp")):
                        found[k] = os.path.join(root_, n)
            return found

        def matches() -> Tuple[List[Tuple[dict, str]], int]:
            pics = index()
            out, seen = [], set()
            for r in all_rows():
                k = rowkey(r)
                if k in seen or k in skip:                           # an ignored creature wants no portrait,
                    continue                                         # least of all one proposed in bulk
                if k in props and not over_var.get():
                    continue
                if gap_var.get() and (r.get("large") or r.get("small")):
                    continue
                hit = ""
                if by_cre.get():
                    hit = next((pics[c.upper()] for c in (r.get("cre_list") or []) if c.upper() in pics), "")
                if not hit and by_port.get():
                    hit = next((pics[n.upper()] for n in (r.get("large_name"), r.get("small_name")) if n and n.upper() in pics), "")
                if hit:
                    out.append((r, hit))
                    seen.add(k)
            return out, len(pics)

        def count() -> None:
            npics = 0
            if not os.path.isdir(dir_var.get().strip()):
                note.set("Choose a folder.")
                go.config(state="disabled")
                return
            w.config(cursor="watch")
            w.update_idletasks()
            try:
                got, npics = matches()
            finally:
                w.config(cursor="")
            listed = len(all_rows())
            if not listed:
                note.set(f"{npics:,} picture(s) in that folder. No creatures or dialog detected, so there is "
                         "nothing to match them to.")
            else:
                note.set(f"{npics:,} picture(s) in that folder, matching {len(got):,} creature(s) of the "
                         f"{listed:,} listed." + ("" if got else "  Nothing would change."))
            go.config(state="normal" if got else "disabled")

        def apply_() -> None:
            got, _n = matches()
            if not got:
                return
            for r, pth in got:
                crop_store().pop(rowkey(r), None)                    # a new picture: any old framing is gone
                props[rowkey(r)] = os.path.normpath(pth)
            st["propose_folder"] = dir_var.get().strip()
            persist_props()
            save_settings(st)
            w.destroy()
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            fill_export()
            status.set(f"Proposed {len(got):,} portrait(s) from {dir_var.get().strip()}.")
        brow = ttk.Frame(box)
        brow.grid(row=3, column=0, columnspan=3, sticky="e", pady=(12, 0))
        go = ttk.Button(brow, text="Propose these", command=apply_, state="disabled")
        go.pack(side="left")
        ttk.Button(brow, text="Cancel", command=w.destroy).pack(side="left", padx=(6, 0))
        box.columnconfigure(1, weight=1)
        dir_var.trace_add("write", lambda *_a: count())
        count()

    folder_btn.config(command=propose_folder)

    def open_propose(r: dict, after: object = None, parent: object = None) -> None:
        """Choosing a portrait, from a file or made on the spot: one window either way. Without the generator
        folder there is nothing to open, so the file picker stands in."""
        if generator_module() is None:
            propose(r, after, parent)
        else:
            generate_dialog(r, after, parent)

    def generator_module() -> object:
        """The optional portrait generator (generator/portrait_generator.py next to this file), or None."""
        if "gen" not in state:
            mod = None
            gp = Path(__file__).resolve().parent / "generator" / "portrait_generator.py"
            if gp.exists():
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("portrait_generator", gp)
                    mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
                    spec.loader.exec_module(mod)                     # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    mod = None
            state["gen"] = mod
        return state["gen"]

    def generate_dialog(r: Optional[dict] = None, after: object = None, parent: object = None) -> None:
        """The Propose tab, with the problem shown in it if anything goes wrong while building it."""
        if view_var.get() in ("gallery", "table"):                   # where "Back" should return to
            state["gen_from"] = view_var.get()
        try:
            build_generate(r, after, parent)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            gen_hint(f"The Propose tab could not be built: {e.__class__.__name__}: {e}")

    def build_generate(r: Optional[dict] = None, after: object = None, parent: object = None) -> None:
        """The Propose tab: describe the creature, pick a picture or make one, and crop it to a game portrait.
        Without a creature (r = None) it is a plain picture maker: type what you want and press Generate."""
        pg = generator_module()
        res = state["res"]
        if pg is None:
            gen_hint("The generator folder is missing: generator/portrait_generator.py should sit beside "
                     "portrait_scout.py. See generator/README.md.")
            return
        generic = r is None
        if r is None:
            r = {"name": "Any portrait", "race": "", "sex": "", "anim": "", "cre_list": [], "dialogs": {}}
        try:
            gs = pg.load_settings()                                  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("Portrait Scout", str(e), parent=parent or root)
            return
        key = "" if generic else rowkey(r)
        mod_name = "_anything" if generic or not res else str(res["mod"])   # type: ignore[index]
        notes = st.setdefault("portrait_notes", {}).setdefault(mod_name, {})
        saved = notes.get(key, {})
        if not saved and not generic:                                # filed under the dialogs it had before (a
            whose0 = ",".join(str(c).upper() for c in (r.get("cre_list") or []))   # newer analysis groups them
            hit_ = next((k_ for k_, v_ in notes.items() if whose0 and str(v_.get("whose") or "") == whose0), "")
            if hit_:                                                 # differently): same creature file, same note
                notes[key] = notes.pop(hit_)
                saved = notes[key]
        # a kept description belongs to one creature: if the note was written for another (two rows can share a
        # dialog key, and old notes outlive an analysis), start from the game data again rather than its words
        whose = ",".join(str(c).upper() for c in (r.get("cre_list") or [])) or str(r.get("name") or "")
        if saved and str(saved.get("whose") or whose) != whose:
            saved = {}
        elif saved and not saved.get("whose"):                       # written before notes were stamped: claim it
            saved["whose"] = whose                                   # now, so it cannot drift to another creature
            save_settings(st)
        fvars = st.setdefault("face_variant", {}).setdefault(mod_name, {})
        if not generic:
            r["face_variant"] = fvars.get(key, 0)                    # which draw of the face this creature is on
        character = "" if generic else pg.describe(r, gs)[1]         # type: ignore[attr-defined]
        folder = Path(__file__).resolve().parent / str(gs.get("output_folder", "generated")) / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", mod_name)
        stem = re.sub(r"[^A-Za-z0-9_.#!-]+", "_", f"{r['name']}_{(r.get('cre_list') or [''])[0]}").strip("_") or "portrait"

        for w in tab_gen.winfo_children():                           # the Propose tab works on one creature at a time
            w.destroy()
        state["gen_row"] = r
        state["gen_built"] = True                                    # before the view switch below: it must not rebuild
        dlg = ttk.Frame(tab_gen)                                     # everything below lives in the tab
        dlg.pack(fill="both", expand=True)
        if view_var.get() != "generate":
            view_var.set("generate")
            show_view()
        root.lift()
        head_ = ttk.Frame(dlg, padding=(10, 6, 10, 0))
        head_.pack(fill="x")
        if not generic:                                              # with no creature there is nothing to go back to
            back_to = str(state.get("gen_from") or "gallery")
            back_b = ttk.Button(head_, text="\u25c0 " + ("Gallery" if back_to == "gallery" else "Table"), width=10,
                                command=lambda: (view_var.set(back_to), show_view()))
            back_b.pack(side="left", padx=(0, 10))
            tooltip(back_b, "Back to the creature you came from, with whatever you proposed on its card.")
        nm_lab = ttk.Label(head_, text=r["name"], font=("TkDefaultFont", 11, "bold"),
                           cursor="" if generic else "hand2")
        nm_lab.pack(side="left")
        if not generic:                                              # click a name or a CRE to copy it, as elsewhere
            nm_lab.bind("<Button-1>", lambda e, n=r["name"]: copy_text(n))
            tooltip(nm_lab, "Click to copy the name.")
            ttk.Button(head_, text="Unselect", width=9, command=lambda: generate_dialog()).pack(side="left", padx=(8, 0))
        ttk.Label(head_, foreground="#666",
                  text=("no creature: type what you want below - nothing is proposed for a creature" if generic else
                        "  ".join(x for x in (r["race"], shown_sex(r["sex"]), r["anim"]) if x) or "(no creature details)")
                  ).pack(side="left", padx=10)
        for cn_ in (r.get("cre_list") or [])[:6]:
            cl_ = tk.Label(head_, text=cn_, fg="#1a5fb4", cursor="hand2",
                           font=("TkDefaultFont", 9, "bold underline" if cn_ in (r.get("cre_known") or [])
                                 else "underline"))
            cl_.pack(side="left", padx=(0, 6))
            cl_.bind("<Button-1>", lambda e, n=cn_: copy_text(n))
            tooltip(cl_, "Click to copy, for NearInfinity's search.")
        if not generic:                                            # v2: context strip - what it has now vs what is proposed
            ctx = ttk.Frame(head_)                                     # on the name's row: one line less
            ctx.pack(side="left", padx=(14, 0))
            in_port = str(r["small"] or r["large"] or "").split("/")[0]
            ttk.Label(ctx, text="In-game:", foreground="#666").pack(side="left")
            ttk.Label(ctx, text=in_port or "(none)", foreground="#1a5fb4" if in_port else "#999").pack(side="left")
            ttk.Label(ctx, text="   Proposed:", foreground="#666").pack(side="left")
            prop_lab = ttk.Label(ctx, text="(none)", foreground="#999")   # names only: the pictures themselves
            prop_lab.pack(side="left")                               # are among the candidates below (in-game
            #                                                          "its portrait now", proposed in green)

            seen_prop = {"p": str(props.get(key) or "")}

            def prop_name() -> None:
                """Keep the proposed file's name current as proposals are made and cleared - and the green
                frame on the candidates with it."""
                if not prop_lab.winfo_exists():
                    return
                p_ = str(props.get(key) or "")
                if p_ != seen_prop["p"]:
                    seen_prop["p"] = p_
                    try:
                        for c_ in cells:
                            mark_now(c_)
                    except NameError:
                        pass
                if not p_:
                    prop_lab.config(text="(none)", foreground="#999")
                else:
                    ok_ = os.path.isfile(p_)
                    prop_lab.config(text=Path(p_).name + ("" if ok_ else " (missing)"),
                                    foreground="#2e9e4f" if ok_ else "#b3261e")
                prop_lab.after(800, prop_name)
            prop_name()
            ttk.Label(ctx, text="   Description: " + ("your saved edit" if saved.get("desc") else "animation + creature file"),
                      foreground="#666").pack(side="left", padx=(0, 0))
            tooltip(ctx, "In-game: the portrait the game shows now.  Proposed: the picture this tab is working on. "
                         "Description: where the words at the top come from.")

        pickrow = ttk.Frame(dlg, padding=(10, 4, 10, 2))             # race, gender, class, age, look: what the
        pickrow.pack(fill="x")                                       # description is built from, under the name
        ttk.Label(pickrow, text="Built from:", foreground="#666").pack(side="left", padx=(0, 6))
        eng_s = gs.get("engine") or {}
        if not engine_ready() and eng_s.get("autostart", True) and "start_engine.bat" in str(eng_s.get("start", "")):
            nb_ = tk.Frame(dlg, bg="#fdf6d8", highlightthickness=1, highlightbackground="#d9c97a")
            nb_.pack(fill="x", padx=10, pady=(6, 0))
            tk.Label(nb_, bg="#fdf6d8", fg="#5a4a00", justify="left", wraplength=620,
                     text="No image generator is set up yet, so nothing can be generated. Choosing a picture from a "
                          "file works as it is.").pack(side="left", padx=8, pady=6)
            ttk.Button(nb_, text="Set it up...", command=setup_window).pack(side="right", padx=8, pady=6)
            ttk.Button(nb_, text="Hide", width=6, command=lambda: fold_gen(True)).pack(side="right", pady=6)
            state["gen_banner"] = nb_
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)
        # Everything above the candidates on the left; on the right, the creature's sprite and the spares from
        # the pool, at a size you can see (they used to sit small on the status line).
        topwrap = ttk.Frame(dlg)
        topwrap.pack(fill="x", before=head_)
        side_ = ttk.Frame(topwrap, padding=(8, 6, 10, 0))
        side_.pack(side="right", anchor="n")
        left_ = ttk.Frame(topwrap)
        left_.pack(side="left", fill="x", expand=True, anchor="n")
        for w_ in [x for x in dlg.pack_slaves() if x is not topwrap]:
            info_ = {k_: v_ for k_, v_ in w_.pack_info().items() if k_ != "in"}
            w_.pack_forget()
            w_.pack(in_=left_, **info_)
            w_.lift()                                                # above the frames made after it, or they
            #                                                          hide it (Tk draws a later sibling on top)
        dlab = ttk.Label(frm, text="Description:")
        dlab.grid(row=1, column=0, sticky="w")
        desc0 = str(saved.get("desc") or character)
        if saved.get("extra") and str(saved["extra"]).strip() not in desc0:   # from when extra details had their own box
            desc0 = ", ".join(x for x in (desc0, str(saved["extra"]).strip()) if x)
        desc_var = tk.StringVar(value=desc0)
        dent = ttk.Entry(frm, textvariable=desc_var)
        dent.grid(row=1, column=1, sticky="ew", padx=6)
        dbtn = ttk.Frame(frm)
        dbtn.grid(row=1, column=2)
        reset_btn = ttk.Button(pickrow, text="\u21bb Reset", width=8, command=lambda: pick_reset())
        tooltip(reset_btn, "Reset: put the description back to what the animation and the creature file say, "
                           "losing anything typed here and any new face." if not generic else
                "Reset: back to Human, Male, Fighter, Adult.")

        def big_edit(var: tk.StringVar, title: str, on_ok: Optional[Callable[[], None]] = None) -> None:
            """A roomy box for text too long to read in one line."""
            w = tk.Toplevel(root)
            esc_closes(w)
            w.title(title)
            w.geometry("700x240")
            row_ = ttk.Frame(w, padding=(8, 0, 8, 8))                # packed first, at the bottom: a Text asks for
            row_.pack(side="bottom", fill="x")                       # 24 lines and would push the buttons off
            t = tk.Text(w, wrap="word", font=("TkDefaultFont", 10), height=6)
            t.pack(fill="both", expand=True, padx=8, pady=8)
            t.insert("1.0", var.get())
            t.focus_set()

            def ok(_e: object = None) -> str:
                var.set(" ".join(t.get("1.0", "end-1c").split()))
                w.destroy()
                if on_ok:
                    on_ok()                                          # kept right away, not only on Generate
                return "break"
            ok_ = ttk.Button(row_, text="OK", command=ok)
            ok_.pack(side="right")
            tooltip(ok_, "Keep the changes (Ctrl+Enter).")
            ttk.Button(row_, text="Cancel", command=w.destroy).pack(side="right", padx=6)
            t.bind("<Control-Return>", ok)
        def keep_desc() -> None:
            """The description as edited, saved now: it used to be kept only when Generate was pressed."""
            notes.setdefault(key, {})["desc"] = desc_var.get().strip()
            save_settings(st)
            gstat.set("Description saved.")
        ttk.Button(dbtn, text="\u2922", width=3,
                   command=lambda: big_edit(desc_var, "Description", keep_desc)).pack(side="left")
        if not generic:

            def clauses(t: str) -> List[str]:
                return [c.strip() for c in t.split(",") if c.strip()]

            def described(n: int) -> List[str]:
                """The description this creature would get on face n."""
                r["face_variant"] = n
                return clauses(pg.describe(r, gs, age_v.get(), look_v.get(), cloth_v.get(), mood_v.get(), scar_v.get(), grime_v.get(), beard_v.get())[1])   # type: ignore[attr-defined]

            def new_face() -> None:
                """Another face for this creature. Only the drawn face is swapped out - anything typed by hand
                stays where it is. Reset is the one that starts over."""
                now, nxt = int(fvars.get(key, 0)), int(fvars.get(key, 0)) + 1
                before, after = described(now), described(nxt)
                gone = [c for c in before if c not in after]         # what this creature's face used to be
                fresh = [c for c in after if c not in before]        # and what it is now
                parts, at = clauses(desc_var.get()), -1
                for c in gone:                                       # swap them where they stand, in place
                    if c in parts:
                        at = parts.index(c)
                        parts[at] = fresh.pop(0) if fresh else ""
                fvars[key] = nxt                                     # the draw moves on either way: it is also
                r["face_variant"] = nxt                              # what the picture is painted from
                save_settings(st)
                if at < 0:
                    # Nothing in the description comes from the face draw - a sirine or a troll has no face drawn
                    # for it, and a description written over by hand has none left.
                    gstat.set(f"Face {nxt + 1} for {r['name']}: nothing in the description comes from the face "
                              "draw, so it is unchanged. Generate paints somebody new either way.")
                    return
                parts[at + 1:at + 1] = fresh                         # anything left over goes in beside it
                desc_var.set(", ".join(c for c in parts if c))
                gstat.set(f"Face {nxt + 1} for {r['name']}: new face words. Press again for another; Reset "
                          "starts over.")
        prow0 = ttk.Frame(pickrow)                                   # what the description is built from
        prow0.pack(side="left")
        reset_btn.pack(side="left", padx=(6, 0))
        seed_var = tk.StringVar(value=str(saved.get("seed") or ""))
        ttk.Label(pickrow, text="Seed:").pack(side="left", padx=(18, 4))
        seed_ent = ttk.Entry(pickrow, textvariable=seed_var, width=12)
        seed_ent.pack(side="left")
        seed_x = ttk.Button(pickrow, text="x", width=2, command=lambda: (seed_var.set(""), keep_seed()))
        seed_x.pack(side="left", padx=(2, 0))
        for w_ in (seed_ent, seed_x):
            tooltip(w_, "Empty: every Generate paints somebody new. A number: the first picture starts from that "
                        "seed, so the same face comes back - change the description and it keeps the face "
                        "with what you changed. Right-click a picture to take its seed. x clears it.")

        def keep_seed(*_a: object) -> None:
            v_ = seed_var.get().strip()
            if v_ and not v_.isdigit():
                return
            notes.setdefault(key, {})["seed"] = v_
            if not v_:
                notes[key]["init"] = ""                             # no seed: new start faces again
            save_settings(st)
        seed_ent.bind("<FocusOut>", keep_seed)
        seed_ent.bind("<Return>", keep_seed)
        RACES = ["Human", "Elf", "Half-Elf", "Drow", "Dwarf", "Duergar", "Halfling", "Gnome", "Half-Orc", "Tiefling",
                 "Githyanki", "Orc", "Goblin", "Hobgoblin", "Ogre"]             # each has its words in race_terms
        CLASSES = list(pg.roles(gs)) or ["Fighter"]                 # type: ignore[attr-defined]
        #                                                            classes, then stations: role_terms in settings
        last = st.get("generic_pick") if isinstance(st.get("generic_pick"), list) else []
        last = (list(last) + ["Human", "Male", "Fighter"])[:3]
        race_vals, class_vals = list(RACES), list(CLASSES)
        if not generic:                                              # start from what the creature file says
            aw_ = str(r.get("anim") or "").split()
            raw_race = str(r.get("race") or "").strip()
            cre_race = next((x for x in RACES if x.lower() == raw_race.lower()), "")
            anim_race = next((x for x in RACES if x.lower().replace("-", "_") in
                              [w.lower().replace("-", "_") for w in aw_]), "")
            if anim_race and not cre_race:                           # a "golem" with a gnome's sprite: the sprite
                cre_race = anim_race                                 # is what the portrait stands beside
            if raw_race and not cre_race:                            # an orc, a sahuagin: not a playable race, but
                cre_race = raw_race                                  # it is what this creature is, so offer it
                race_vals = [raw_race] + race_vals
            cre_sex = str(r.get("sex") or "").strip().title() if str(r.get("sex") or "").strip().lower() in ("male", "female") else ""
            anim_sex = next((w.title() for w in aw_ if w.lower() in ("male", "female")), "")
            if anim_sex:                                             # the sprite's gender beats the file's
                cre_sex = anim_sex
            cre_cls = pg.find_role(aw_, gs)                           # type: ignore[attr-defined]
            if not cre_cls and aw_:                                  # "Orog", "Sahuagin": the animation is the role
                cre_cls = aw_[0]
                class_vals = [cre_cls] + class_vals
            last = [cre_race or last[0], cre_sex or last[1], cre_cls or last[2]]
        race_v = tk.StringVar(value=str(saved.get("race") or last[0]))
        sex_v = tk.StringVar(value=str(saved.get("sex") or last[1]))
        class_v = tk.StringVar(value=str(saved.get("class") or last[2]))
        ages = pg.term_names(gs, "age_terms") or ["Adult"]           # type: ignore[attr-defined]
        looks = pg.term_names(gs, "look_terms") or ["Ordinary"]      # type: ignore[attr-defined]
        age_v = tk.StringVar(value=str(saved.get("age") or st.get("last_age") or ("Adult" if "Adult" in ages else ages[0])))
        def kept(v: object, vals: List[str]) -> str:                 # a choice since taken out of settings
            return str(v) if str(v or "") in vals else vals[0]       # ("Noble" was a look once) starts over
        look_v = tk.StringVar(value=kept(saved.get("look") or st.get("last_look"), looks))
        moods = pg.term_names(gs, "mood_terms") or ["Neutral"]      # type: ignore[attr-defined]
        mood_v = tk.StringVar(value=kept(saved.get("mood") or st.get("last_mood"), moods))
        scars = pg.term_names(gs, "scar_terms") or ["None"]         # type: ignore[attr-defined]
        scar0 = str(gs.get("scar_default") or "") if str(gs.get("scar_default") or "") in scars else scars[0]
        scar_v = tk.StringVar(value=str(saved.get("scars")) if saved.get("scars") in scars else
                              kept(st.get("last_scars") or scar0, scars))
        beards = pg.term_names(gs, "beard_terms") or ["By race"]   # type: ignore[attr-defined]
        beard_v = tk.StringVar(value=kept(saved.get("beard"), beards))
        grimes = pg.term_names(gs, "grime_terms") or ["Clean"]      # type: ignore[attr-defined]
        grime0 = str(gs.get("grime_default") or "") if str(gs.get("grime_default") or "") in grimes else grimes[0]
        grime_v = tk.StringVar(value=kept(saved.get("grime") or st.get("last_grime") or grime0, grimes))
        cloths = pg.term_names(gs, "outfit_terms") or ["By role"]   # type: ignore[attr-defined]
        cloth_v = tk.StringVar(value=kept(saved.get("outfit"), cloths))

        def pick_changed(*_a: object) -> None:
            race, sex, cls = race_v.get() or "Human", sex_v.get() or "Male", class_v.get() or "Fighter"
            row_ = {"race": race, "sex": sex, "anim": f"{cls} {sex} {race}"}
            if generic:
                st["generic_pick"] = [race, sex, cls]                 # remembered until Reset
            else:                                                     # keep the creature's own face and files
                same_ = (cls == last[2] and race.lower() == str(last[0]).lower() and sex.lower() == str(last[1]).lower())
                row_ = dict(r, race=race, sex=sex, anim=str(r.get("anim") or "") if same_ else f"{cls} {sex} {race}")
                #   race, gender and role as the creature has them: its own animation, so the sprite's look
                #   (a beggar's bald head and bare chest) stays when only the scars or the age are changed
                if race.lower() != str(r.get("race") or "").lower():  # a race picked by hand beats what the
                    row_["colors"] = {k_: v_ for k_, v_ in (r.get("colors") or {}).items() if k_ != "kind"}   # colors hint
            st["last_age"], st["last_look"], st["last_mood"] = age_v.get(), look_v.get(), mood_v.get()
            st["last_scars"], st["last_grime"] = scar_v.get(), grime_v.get()
            save_settings(st)
            desc_var.set(pg.describe(row_, gs, age_v.get(), look_v.get(), cloth_v.get(), mood_v.get(), scar_v.get(), grime_v.get(), beard_v.get())[1])   # type: ignore[attr-defined]

        def pick_reset() -> None:
            st.pop("generic_pick", None)
            race_v.set(last[0] if not generic else "Human")
            sex_v.set(last[1] if not generic else "Male")
            class_v.set(last[2] if not generic else "Fighter")
            age_v.set("Adult" if "Adult" in ages else ages[0])
            look_v.set(looks[0])
            mood_v.set(moods[0])
            scar_v.set(scar0)
            beard_v.set(beards[0])
            grime_v.set(grimes[0])
            cloth_v.set(cloths[0])
            if generic:
                pick_changed()
            else:                                                    # the creature as its own file describes it
                row_ = r                                             # (race and gender as the sprite shows them)
                if race_v.get().lower() != str(r.get("race") or "").lower() or \
                        sex_v.get().lower() != str(r.get("sex") or "").lower():
                    row_ = dict(r, race=race_v.get(), sex=sex_v.get())
                desc_var.set(pg.describe(row_, gs, age_v.get(), look_v.get(), cloth_v.get(), mood_v.get(), scar_v.get(), grime_v.get(), beard_v.get())[1])   # type: ignore[attr-defined]
        picks = [(race_v, race_vals, 11), (sex_v, ["Male", "Female"], 7), (class_v, class_vals, 10),
                 (age_v, ages, 10), (look_v, looks, 11), (beard_v, beards, 8), (scar_v, scars, 8), (grime_v, grimes, 8), (mood_v, moods, 8),
                 (cloth_v, cloths, 11)]
        pick_tips = {id(race_v): "Race", id(sex_v): "Gender",
                     id(class_v): "Role: who they are. Classes first, then everyday folk. Sets the outfit too, "
                                  "unless Outfit says otherwise.",
                     id(age_v): "Age",
                     id(look_v): "Look: the skin - weathered, freckled, sickly, well-kept.",
                     id(beard_v): "Beard, for men. By race: a dwarf, gnome or duergar gets his race's own, "
                                  "different for each one; anyone else gets none said.",
                     id(scar_v): "Scars: none, a few, scarred, heavily scarred, or fresh wounds.",
                     id(grime_v): "Grime: how dirty the face is.",
                     id(mood_v): "Mood: the expression. Any lets Vary faces pick one per picture, mostly calm.",
                     id(cloth_v): "Outfit: what they wear. By role takes the role's own; pick one to dress a "
                                  "fighter in mail or a farmer in rags. The creature's colors go into it."}
        typed = {id(race_v), id(class_v)} if generic else set()      # a cat is not in the list: type it in
        for var_, vals_, w_ in picks:
            free = id(var_) in typed
            cb_ = ttk.Combobox(prow0, textvariable=var_, values=vals_, width=w_,
                               state="normal" if free else "readonly")
            cb_.pack(side="left", padx=(0, 4))
            cb_.bind("<<ComboboxSelected>>", pick_changed)
            if id(var_) in pick_tips:
                tooltip(cb_, pick_tips[id(var_)])
            if free:
                cb_.bind("<Return>", pick_changed)
                cb_.bind("<FocusOut>", pick_changed)
        tooltip(prow0, ("Race and role can be typed, not only picked: \"cat\", \"owl\", \"wolf\". For an "
                        "animal take the Animal style as well, or it comes out as a person in "
                        "clothes. " if generic else "")
                       + "Race, gender, role, age, face, mood and outfit. They "
                       + ("start from the creature's own file and nothing happens until you change one, which "
                          "rewrites the description. " if not generic else "")
                       + "Flux paints clean, young skin unless it is told otherwise, so \"Old\" and \"Weathered\" "
                         "are worth a try.")
        if generic and not desc_var.get().strip():
            pick_changed()
        brow = ttk.Frame(frm)                                        # background, and what to make from it
        brow.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        gen_only = [brow]                                            # filled in below: the generation-only widgets
        bg_lab = ttk.Label(brow, text="Background:")
        bg_lab.pack(side="left")
        bg_var = tk.StringVar()
        tooltip(bg_lab, "The prompts put this in where they say {background}, so one change covers every look that "
                        "uses it. Empty it and the sentence about the background drops out of the prompt.")
        def fade_depth() -> float:
            """How far the Ghostly button fades a picture."""
            try:
                return max(0.05, min(1.5, float(gs.get("ghost_fade", 0.55))))
            except (TypeError, ValueError):
                return 0.55

        def ghost_depth() -> float:
            """Pictures of something with no body are faded as they arrive, so a spectre comes out a spectre."""
            if not hasattr(pg, "incorporeal") or not pg.incorporeal(desc_var.get()):   # type: ignore[attr-defined]
                return 0.0
            return fade_depth()
        have_lora = bool(lora_files()) or not engine_ready()       # an engine of your own: no way to tell, so show it
        lora_var = tk.BooleanVar(value=bool(st.get("use_lora", True)) and have_lora)

        def lora_changed() -> None:
            st["use_lora"] = bool(lora_var.get())
            save_settings(st)
        LORA_WS = ["Style's", "1.0", "0.85", "0.65", "0.5", "0.35", "0.2"]
        lora_w = tk.StringVar(value=str(st.get("lora_weight") or "Style's"))
        if lora_w.get() not in LORA_WS:
            lora_w.set("Style's")
        if have_lora:                                                # packed right to left: LoRA ends up last
            lw_cb = ttk.Combobox(brow, textvariable=lora_w, values=LORA_WS, width=7, state="readonly")
            lw_cb.pack(side="right", padx=(2, 0))
            lw_cb.bind("<<ComboboxSelected>>", lambda e: (st.__setitem__("lora_weight", lora_w.get()), save_settings(st)))
            tooltip(lw_cb, "How hard the LoRA pulls. Style's: what the style says (BG2 framing: 0.35). The "
                           "portrait LoRA was trained on armored adventurers, so it paints armor and a leather "
                           "coat over whatever clothes are asked for: at 0.35 rags come out as rags. Lower also "
                           "lets a creature keep its own features; higher looks more like the game's portraits.")
            lora_cb = ttk.Checkbutton(brow, text="LoRA", variable=lora_var, command=lora_changed)
            lora_cb.pack(side="right", padx=(6, 0))
            tooltip(lora_cb, "Off: the <lora:...> part is taken out of the prompt, so only the model itself paints "
                             "the picture. A LoRA pushes hard towards what it was trained on, which can cost a "
                             "creature its own features (horns, green skin).")
        styles = pg.style_names(gs) if hasattr(pg, "style_names") else []   # type: ignore[attr-defined]
        # An animal style asks for "a real animal and nothing else", so it must not carry over to the next person
        # opened (an ogre came out a gorilla). Each kind keeps the style last used for it.
        fallback_ = gs.get("style") if gs.get("style") in styles else (styles[0] if styles else "")
        last_ = str(st.get("portrait_style") or "")
        if r is not None and styles:
            want_animal = is_animal(r)
            if want_animal and not animal_style(last_):
                last_ = next((x for x in (str(st.get("portrait_style_animal") or ""),) if x in styles), "") or \
                    next((x for x in styles if animal_style(x)), last_)
            elif not want_animal and animal_style(last_):
                pp_ = str(st.get("portrait_style_people") or "")
                last_ = pp_ if pp_ in styles and not animal_style(pp_) else (
                    fallback_ if not animal_style(fallback_) else
                    ("BG2 framing" if "BG2 framing" in styles else fallback_))
        style_var = tk.StringVar(value=last_ if last_ in styles else fallback_)
        style_box = ttk.Combobox(brow, textvariable=style_var, values=styles, state="readonly", width=22)
        style_box.pack(side="right", padx=(4, 0))
        ttk.Label(brow, text="Style:").pack(side="right", padx=(12, 0))
        count_var = tk.IntVar(value=int(saved.get("count") or gs.get("candidates", 2) or 2))
        try:
            max_c = max(1, int(gs.get("max_candidates") or MAX_CANDIDATES))
        except (TypeError, ValueError):
            max_c = MAX_CANDIDATES
        # Every picture gets a face of its own in words (Klein paints nearly one face per description whatever
        # the seed). A seed in the Seed box means "this face": then no new words, so it comes back the same.
        cnt_ = ttk.Spinbox(brow, from_=1, to=max_c, width=3, textvariable=count_var)
        cnt_.pack(side="right", padx=(4, 0))
        tooltip(cnt_, f"How many to paint each time Generate is pressed, up to {max_c} (max_candidates in "
                      "generator/settings.json). They arrive one "
                      "by one and Stop keeps the ones already made.")
        ttk.Label(brow, text="Candidates:").pack(side="right", padx=(14, 0))
        p_sv = tk.StringVar(value=start_v.get())                     # this window's: "This picture" while a
        p_sk = tk.StringVar(value=start_k.get())                     # picture is restyled, the shared ones else
        p_box = start_controls(brow, "right", p_sv, p_sk)
        p_guard = {"on": False}

        def p_sv_changed(*_a: object) -> None:
            if p_guard["on"]:
                return
            v_ = p_sv.get()
            if v_ != "This picture":
                if restyle["path"]:
                    restyle_from("")
                start_v.set(v_)

        def p_sk_changed(*_a: object) -> None:
            if p_guard["on"]:
                return
            try:
                k_ = max(0.3, min(1.0, float(p_sk.get())))
            except (tk.TclError, ValueError):
                return
            if restyle["path"]:
                restyle["k"] = k_                                    # fine-tuning the restyle, not Auto's
            else:
                start_k.set(p_sk.get())
        p_sv.trace_add("write", p_sv_changed)
        p_sk.trace_add("write", p_sk_changed)
        def keep_bg() -> None:
            """The background as edited, saved straight away (the prompt words are left as they are saved)."""
            name = style_var.get()
            try:
                pg.save_background(gs, name, bg_var.get().strip())    # type: ignore[attr-defined]
                gstat.set("Background saved.")
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", str(e), parent=dlg)
        ttk.Button(brow, text="\u2922", width=3,
                   command=lambda: big_edit(bg_var, "Background", keep_bg)).pack(side="right", padx=(4, 0))
        ttk.Entry(brow, textvariable=bg_var).pack(side="left", fill="x", expand=True, padx=(6, 0))
        fold_lbl = tk.StringVar()

        def fold_gen(hide: object = None) -> None:
            """Put the description, the background and the no-generator notice away, for working from files only."""
            if hide is not None:
                st["gen_folded"] = bool(hide)
                save_settings(st)
            off = bool(st.get("gen_folded"))
            fold_lbl.set(("\u25b6 Show generator settings" if off else "\u25bc Hide generator settings"))
            for w_ in gen_only:
                try:
                    w_.grid_remove() if off else w_.grid()
                except (tk.TclError, AttributeError):
                    pass
            try:                                                     # the pickers are a row of their own
                pickrow.pack_forget() if off else pickrow.pack(fill="x", after=head_)
            except (tk.TclError, AttributeError):
                pass
            nb = state.get("gen_banner")
            if nb is not None:
                try:
                    nb.pack_forget() if off else nb.pack(fill="x", padx=10, pady=(6, 0), before=frm)
                except (tk.TclError, AttributeError):
                    pass
        erow = ttk.Frame(frm)                                        # Generate, then the engine it talks to
        erow.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        erow2 = ttk.Frame(frm)                                       # the engine, and the prompt it is sent
        erow2.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        bar = erow                                                   # the same row: Generate is packed first
        zoom_var = tk.DoubleVar(value=float(st.get("portrait_head_zoom") or (gs.get("portrait_bmp") or {}).get("zoom") or 1.0))
        size0 = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 \
            else [(gs.get("portrait_bmp") or {}).get("width", 169), (gs.get("portrait_bmp") or {}).get("height", 266)]
        w_var = tk.IntVar(value=int(size0[0] or 169))
        h_var = tk.IntVar(value=int(size0[1] or 266))
        gen_btn = tk.Button(bar, text="Generate", bg="#2e9e4f", fg="white", activebackground="#25803f",
                            activeforeground="white", font=("TkDefaultFont", 9, "bold"), padx=12, relief="raised",
                            disabledforeground="#e8e8e8")
        gen_btn.pack(side="left")
        no_engine = (not engine_ready() and (gs.get("engine") or {}).get("autostart", True)
                     and "start_engine.bat" in str((gs.get("engine") or {}).get("start", "")))
        if no_engine:                                                # nothing to send the prompt to
            gen_btn.config(state="disabled", bg="#9bbfa6")
            tooltip(gen_btn, "There is no image generator set up yet, so there is nothing to send the prompt to. "
                             "Use \"Select image...\" for a picture you already have, or set one up from the "
                             "Engine menu.")

        def pick_image() -> None:
            """A picture from a file, put in with the candidates so it can be framed like a generated one."""
            q = filedialog.askopenfilename(
                parent=dlg, title=f"Picture for {r['name']}",
                initialdir=os.path.dirname(props.get(key, "")) or str(folder) if folder.is_dir() else None,
                filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tga *.tif *.tiff"), ("All files", "*.*")])
            if q:
                take_image(q)

        def take_image(q: str, came_from: str = "") -> None:
            """Put a picture in with the candidates, so it can be framed like a generated one. A spare taken
            out of the pool is remembered, so that using it here takes it out of the pool."""
            try:                                                     # a copy: Discard deletes candidates, and the
                folder.mkdir(parents=True, exist_ok=True)            # picture you picked is not ours to delete
                dst = folder / f"{stem}_f{int(time.time())}{Path(q).suffix.lower()}"
                shutil.copyfile(q, dst)
                if came_from:
                    st.setdefault("pool_from", {})[os.path.normcase(str(dst))] = \
                        os.path.normcase(os.path.normpath(came_from))
                    save_settings(st)
                q = str(dst)
            except OSError as e:
                messagebox.showwarning("Portrait Scout", f"Could not copy {Path(q).name} into {folder} ({e}).\n\n"
                                       "It is used where it lies instead - take care with Discard.", parent=dlg)
            gd = ghost_depth()
            if gd > 0 and hasattr(pg, "ghost_image"):                # fade what was brought in, too
                try:
                    faded = pg.ghost_image(Path(q).read_bytes(), gd)  # type: ignore[attr-defined]
                    gq = str(Path(q).with_suffix("")) + "_ghost.png"
                    Path(gq).write_bytes(faded)
                    q = gq
                except OSError as e:
                    gstat.set(f"Could not fade {Path(q).name} ({e}); it is added as it is.")
            same_q = lambda x: os.path.normcase(os.path.normpath(x))  # noqa: E731
            if not any(same_q(c["path"]) == same_q(q) for c in cells):
                add_candidate(os.path.normpath(q))
            gstat.set(f"{Path(q).name} added. Frame it with the crop box, then press "
                      + ("Make portrait." if generic else "Use this."))
        def pick_spare() -> None:
            """Take one of the spare pictures already made. The ones made for this kind of creature come first."""
            pool_chooser(dlg, ("" if generic else str(r.get("race") or ""), "" if generic else str(r.get("sex") or "")),
                         lambda q: take_image(str(q), came_from=str(q)), gstat.set)

        def sel_menu() -> None:
            """Two ways in: a file of your own, or one of the spares already made."""
            m = tk.Menu(dlg, tearoff=0)
            m.add_command(label="From a file...", command=pick_image)
            m.add_command(label="From the pool...", command=pick_spare)
            try:
                m.tk_popup(sel_btn.winfo_rootx(), sel_btn.winfo_rooty() + sel_btn.winfo_height())
            finally:
                m.grab_release()
        ttk.Label(bar, text="or").pack(side="left", padx=(8, 8))
        sel_btn = tk.Button(bar, text="Select image...", bg="#2f6fb5", fg="white", activebackground="#27598f",
                            activeforeground="white", font=("TkDefaultFont", 9, "bold"), padx=12, relief="raised",
                            disabledforeground="#e8e8e8", command=sel_menu)
        sel_btn.pack(side="left")
        tooltip(sel_btn, "A picture of your own, or one of the spare ones already made.")
        gen_only.extend([dlab, dent, dbtn])
        fb_ = ttk.Button(bar, textvariable=fold_lbl, command=lambda: fold_gen(not st.get("gen_folded")))
        fb_.pack(side="left", padx=(12, 0))
        tooltip(fb_, "Fold away the description, the background and the generator notice, for when you are only "
                     "choosing pictures from files.")
        fold_gen()
        tooltip(sel_btn, "Use a picture you already have instead of generating one. It joins the candidates below, "
                         "so the crop box works on it the same way.")
        gstat = tk.StringVar(value="Type what the portrait should show, then press Generate." if generic else
                             "Description comes from the creature's gender, race and animation type - edit it freely.")

        def prompt_now(more: str = "") -> Tuple[str, str]:
            base_ = pg.womanly(desc_var.get().strip()) if hasattr(pg, "womanly") else desc_var.get().strip()   # type: ignore[attr-defined]
            d = pg.vary_merge(base_, more, gs) if hasattr(pg, "vary_merge") else \
                ", ".join(x for x in (desc_var.get().strip(), more.strip()) if x)     # type: ignore[attr-defined]
            subj = re.sub(r"^(adult|young)\s+", "", d.split(",")[0].strip())
            tmpl = ptext.get("1.0", "end-1c")
            if not lora_var.get():
                tmpl = re.sub(r"\s*<lora:[^>]*>", "", tmpl)          # paint without the LoRA
            elif lora_w.get() != "Style's":                          # the strength picked beside the tick box
                tmpl = re.sub(r"(<lora:[^:>]+):[0-9.]+>", lambda m_: f"{m_.group(1)}:{lora_w.get()}>", tmpl)
            bg_ = pg.pick_palette(gs, bg_var.get()) if hasattr(pg, "pick_palette") else bg_var.get()   # type: ignore[attr-defined]
            pr_ = pg.fill_prompt(tmpl, subj, d, "", bg_)              # type: ignore[attr-defined]  {random}: a palette
            if hasattr(pg, "youthen"):
                pr_ = pg.youthen(pr_, d, gs)                         # type: ignore[attr-defined]
            return pr_, ntext.get("1.0", "end-1c")

        def copy_prompt() -> None:
            pr, ng = prompt_now()
            dlg.clipboard_clear()
            dlg.clipboard_append(pr + "\nNegative prompt: " + ng)
            gstat.set("Prompt copied - paste it into your image generator.")


        prow = ttk.Frame(frm)                                        # the status line, and the progress beside it
        prow.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        gbar_ = ttk.Progressbar(prow, maximum=1000)                  # shown while generating, beside the status
        busy = {"spin": False}

        def show_progress(frac: object, text: str) -> None:
            """Progress of the running job: here and in the main window's status bar."""
            status.set(f"Portrait for {r['name']}: {text}")
            if not dlg.winfo_exists():
                return
            gstat.set(text)
            try:                                                     # the engine line follows what is happening
                if text.startswith(("Starting the image engine", "First start")):
                    eng_var.set("starting...")
                elif text.startswith("Candidate"):
                    eng_var.set("running")
                    web_btn.config(state="normal")
            except (NameError, tk.TclError):
                pass
            if not gbar_.winfo_ismapped():
                gbar_.pack(side="right", fill="x", expand=True, padx=(14, 0))
            if frac is None:
                if not busy["spin"]:
                    gbar_.config(mode="indeterminate")
                    gbar_.start(60)
                    busy["spin"] = True
            else:
                if busy["spin"]:
                    gbar_.stop()
                    busy["spin"] = False
                gbar_.config(mode="determinate", value=int(float(frac) * 1000))   # type: ignore[arg-type]
        body_ = ttk.Frame(dlg)
        body_.pack(fill="both", expand=True)
        result_box = ttk.Frame(body_, padding=10)                    # the cropped game portrait, after "Use this"
        result_box.pack(side="right", anchor="n")
        cwrap = ttk.Frame(body_)
        cwrap.pack(side="left", fill="both", expand=True)
        crow = ttk.Frame(erow)                                       # the crop controls join the row above
        crow.pack(side="left", padx=(16, 0))
        ttk.Label(crow, text="Portrait:").pack(side="left")
        w_box = ttk.Spinbox(crow, from_=16, to=1024, width=5, textvariable=w_var)
        w_box.pack(side="left", padx=(4, 2))
        ttk.Label(crow, text="x").pack(side="left")
        h_box = ttk.Spinbox(crow, from_=16, to=1536, width=5, textvariable=h_var)
        h_box.pack(side="left", padx=(2, 8))

        tooltip(w_box, "The size of the .bmp made from a candidate. The pictures themselves are generated at the size "
                       "set under Generator settings.")

        def set_size(w_: int, h_: int) -> None:
            w_var.set(w_)
            h_var.set(h_)
            st["portrait_size"] = [w_, h_]
            save_settings(st)
        SIZES = {"speaker 169x266": (169, 266), "large 210x330": (210, 330)}
        size_var = tk.StringVar(value="")

        def size_picked(*_a: object) -> None:
            wh = SIZES.get(size_var.get())
            if wh:
                set_size(*wh)
        size_box = ttk.Combobox(crow, textvariable=size_var, values=list(SIZES), state="readonly", width=15)
        size_box.pack(side="left", padx=(0, 12))
        size_box.bind("<<ComboboxSelected>>", size_picked)

        def size_shown(*_a: object) -> None:                         # keep the box in step with the two numbers
            try:
                now = (int(w_var.get()), int(h_var.get()))
            except (tk.TclError, ValueError):
                return
            size_var.set(next((k for k, v in SIZES.items() if v == now), ""))
        for _v in (w_var, h_var):
            _v.trace_add("write", size_shown)
        size_shown()
        tooltip(size_box, "The two sizes the game uses: 169x266 for the small portrait a speaker shows, 210x330 for "
                          "the large one. Type your own in the boxes to the left for anything else.")
        ttk.Label(crow, text="Zoom:").pack(side="left")
        z_box = ttk.Spinbox(crow, from_=1.0, to=2.5, increment=0.1, width=4, format="%.1f", textvariable=zoom_var)
        z_box.pack(side="left", padx=(4, 8))
        tooltip(z_box, "How much closer the .bmp is than the picture (1 = the whole picture, which is already the "
                       "shape of a portrait).\n\nAbove 1 a yellow box is drawn on each candidate: that box is "
                       "exactly what the game portrait keeps.")
        nudge = {"dx": float(st.get("crop_dx") or 0), "dy": float(st.get("crop_dy") or 0)}

        def box_for(sw: int, sh: int) -> Tuple[int, int, int, int]:
            """What the game portrait keeps from a picture this size: the head zoom, moved where you put it."""
            try:
                zm_ = max(1.0, min(2.5, float(zoom_var.get())))
                bw_, bh_ = max(16, int(w_var.get())), max(16, int(h_var.get()))
            except (tk.TclError, ValueError):
                zm_, bw_, bh_ = 1.25, 169, 266
            top_ = float((gs.get("portrait_bmp") or {}).get("top", 0) or 0)
            lf, tp, cw, ch = pg.crop_box(sw, sh, bw_, bh_, zm_, top_)   # type: ignore[attr-defined]
            lf = int(max(0, min(sw - cw, lf + nudge["dx"] * sw)))
            tp = int(max(0, min(sh - ch, tp + nudge["dy"] * sh)))
            return lf, tp, cw, ch

        def shift(dx: float, dy: float) -> None:
            nudge["dx"] = 0.0 if dx == 0 and dy == 0 else max(-0.5, min(0.5, nudge["dx"] + dx))
            nudge["dy"] = 0.0 if dx == 0 and dy == 0 else max(-0.5, min(0.5, nudge["dy"] + dy))
            st["crop_dx"], st["crop_dy"] = nudge["dx"], nudge["dy"]
            save_settings(st)
            crop_preview()

        move_ws: List[object] = []
        for txt, dx, dy in (("\u25c0", -0.02, 0), ("\u25b6", 0.02, 0), ("\u25b2", 0, -0.02), ("\u25bc", 0, 0.02)):
            mv_ = ttk.Button(crow, text=txt, width=2, command=lambda a=dx, b=dy: shift(a, b))
            mv_._ps_pack = {"side": "left", "padx": 1}               # type: ignore[attr-defined]
            mv_.pack(**mv_._ps_pack)                                 # type: ignore[attr-defined]
            tooltip(mv_, "Move the crop box on every candidate at once. \"Middle\" puts it back.")
            move_ws.append(mv_)
        mid_btn = ttk.Button(crow, text="Mid", width=4, command=lambda: shift(0, 0))
        mid_btn._ps_pack = {"side": "left", "padx": (4, 8)}          # type: ignore[attr-defined]
        mid_btn.pack(**mid_btn._ps_pack)                             # type: ignore[attr-defined]
        move_ws.append(mid_btn)
        tooltip(mid_btn, "Put the box back in the middle. The arrows move it on every candidate at once; the picture "
                         "that \"Use this\" makes is exactly what the box shows.")
        z1_btn = ttk.Button(crow, text="Zoom 1", width=7, command=lambda: zoom_var.set(1.0))
        z1_btn._ps_pack = {"side": "left", "padx": (0, 8)}           # type: ignore[attr-defined]
        z1_btn.pack(**z1_btn._ps_pack)                               # type: ignore[attr-defined]
        move_ws.append(z1_btn)
        tooltip(z1_btn, "Put Head zoom back to 1.0 - the box as wide as the picture allows, nothing cropped closer.")
        full_var = tk.BooleanVar(value=bool(st.get("cand_full_size", False)))

        def full_toggled() -> None:
            st["cand_full_size"] = bool(full_var.get())
            save_settings(st)
            relist()
        full_cb = ttk.Checkbutton(crow, text="Full size", variable=full_var, command=full_toggled)
        full_cb.pack(side="left", padx=(0, 8))
        tooltip(full_cb, f"Off: every candidate is shown the same size ({CAND_W}x{CAND_H}), so they can be compared "
                         "side by side. On: each is shown at its own size, so a finished game portrait looks small "
                         "next to the pictures it was cut from.")
        cropnote = tk.StringVar(value="")                            # kept: the note itself is now the z_box tooltip
        cbody = ttk.Frame(cwrap)                                     # the candidates scroll: there can be many
        cbody.pack(fill="both", expand=True)
        ccv = tk.Canvas(cbody, highlightthickness=0)
        csb = ttk.Scrollbar(cbody, orient="vertical", command=ccv.yview)
        ccv.configure(yscrollcommand=csb.set)
        ccv.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        cands = ttk.Frame(ccv, padding=10)
        cwin = ccv.create_window((0, 0), window=cands, anchor="nw")
        cands.bind("<Configure>", lambda e: ccv.configure(scrollregion=ccv.bbox("all")))

        def wheel(e: object) -> str:
            ccv.yview_scroll(int(-getattr(e, "delta", 0) / 120) or (1 if getattr(e, "num", 0) == 5 else -1), "units")
            return "break"

        def catch_wheel(w: object) -> None:
            """The wheel scrolls the candidates wherever the pointer is over them (Tk sends it to one widget only)."""
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                w.bind(ev, wheel)                                    # type: ignore[attr-defined]
            for kid in w.winfo_children():                           # type: ignore[attr-defined]
                catch_wheel(kid)
        for w_ in (ccv, cands):
            catch_wheel(w_)
        cells: List[dict] = []                                       # one per candidate shown: path, cell, image

        cut_out = lambda q: (str(q).lower().endswith(".bmp") and bool(   # noqa: E731
            re.search(r"_\d+x\d+(_z\d+)?(_m-?\d+_-?\d+)?$", Path(q).stem)))

        def use(path: str) -> None:
            # the proposal is a game-ready .bmp (cropped and shrunk); the full-size picture stays next to it
            size = gs.get("portrait_bmp", {}) or {}
            try:
                bw, bh = max(16, min(1024, int(w_var.get()))), max(16, min(1536, int(h_var.get())))
            except (tk.TclError, ValueError):
                bw, bh = int(size.get("width", 169) or 169), int(size.get("height", 266) or 266)
            st["portrait_size"] = [bw, bh]
            import importlib.util
            if importlib.util.find_spec("PIL") is None and not st.get("pillow_asked"):
                st["pillow_asked"] = True                            # ask once only
                save_settings(st)
                if messagebox.askyesno("Portrait Scout", "Shrinking pictures looks a little sharper with the free Pillow "
                                       "library, which is not installed.\n\nInstall it now? (runs: pip install pillow; "
                                       "needs the internet)", parent=dlg):
                    try:
                        pr_ = subprocess.run([sys.executable, "-m", "pip", "install", "pillow"], capture_output=True, text=True,
                                             timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        importlib.invalidate_caches()
                        ok_ = pr_.returncode == 0
                        messagebox.showinfo("Portrait Scout", "Pillow is installed." if ok_ else
                                            "Pillow could not be installed:\n\n" + (pr_.stderr or pr_.stdout)[-600:]
                                            + "\n\nPortrait Scout carries on with its own method.", parent=dlg)
                    except (OSError, subprocess.SubprocessError) as e:
                        messagebox.showinfo("Portrait Scout", f"Pillow could not be installed ({e}). Portrait Scout "
                                            "carries on with its own method.", parent=dlg)
            bmp = os.path.splitext(path)[0] + f"_{bw}x{bh}.bmp"
            try:
                dlg.config(cursor="watch")
                dlg.update_idletasks()
                try:
                    zm = max(1.0, min(2.5, float(zoom_var.get())))
                except (tk.TclError, ValueError):
                    zm = 1.25
                st["portrait_head_zoom"] = zm
                save_settings(st)
                inf_ = image_info(path)
                mark = f"_z{int(round(zm * 10))}" + (f"_m{int(nudge['dx'] * 100)}_{int(nudge['dy'] * 100)}"
                                                     if nudge["dx"] or nudge["dy"] else "")
                bmp = os.path.splitext(path)[0] + f"_{bw}x{bh}{mark}.bmp"
                to_portrait_bmp(path, bmp, bw, bh, getattr(pg, "crop_box", None),   # exactly what the yellow box shows
                                box_for(inf_["w"], inf_["h"]) if inf_ else None)
                path = bmp
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", f"Could not make the {bw}x{bh} .bmp ({e}).\n"
                                       "The full-size picture is used as the proposal instead.", parent=dlg)
            finally:
                dlg.config(cursor="")
            same_ = lambda q: os.path.normcase(os.path.normpath(q))  # noqa: E731
            if path.lower().endswith(".bmp"):                        # only one cut-out portrait at a time: the old one
                for c in [c for c in cells if cut_out(c["path"])     # goes. The portrait it wears in the game, and
                          and same_(c["path"]) != same_(path)]:      # anything touched up, are not ours to delete
                    try:
                        os.remove(c["path"])
                    except OSError:
                        pass
                    c["cell"].destroy()
                    cells.remove(c)
                relayout()
            if path.lower().endswith(".bmp") and not any(same_(c["path"]) == same_(path) for c in cells):
                add_candidate(path)                                  # the cropped portrait joins the candidates
            if generic:                                              # no creature to propose it for
                gstat.set(f"Made the game portrait {Path(path).name} (it is in {folder}).")
                if path.lower().endswith(".bmp"):
                    show_result(path)
                return
            props[key] = os.path.normpath(path)
            persist_props()
            if not share_btn.winfo_ismapped():
                share_btn.pack(side="left", padx=(8, 0), before=crow)
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            if callable(after):
                after()                                              # type: ignore[operator]
            gstat.set(f"Proposed portrait: {Path(path).name}")
            if path.lower().endswith(".bmp"):                        # show what the game portrait looks like
                show_result(path)

        def propose(path: str) -> None:
            """An already-cropped game portrait: just make it the proposal again."""
            if generic:
                show_result(path)
                return
            props[key] = os.path.normpath(path)
            persist_props()
            if not share_btn.winfo_ismapped():
                share_btn.pack(side="left", padx=(8, 0), before=crow)
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            if callable(after):
                after()                                              # type: ignore[operator]
            gstat.set(f"Proposed portrait: {Path(path).name}")
            show_result(path)

        def show_result(path: str) -> None:
            try:
                data = open(path, "rb").read()
                ppm = bmp_to_ppm(data, 1)
                if not ppm:
                    return
                pi = tk.PhotoImage(data=ppm)
                for w_ in result_box.winfo_children():
                    w_.destroy()
                ttk.Label(result_box, text="Game portrait:", foreground="#666").pack(anchor="w")
                lab = tk.Label(result_box, image=pi)
                lab.image = pi                                       # type: ignore[attr-defined]
                lab.pack()
                ttk.Label(result_box, text=f"{pi.width()}x{pi.height()} - change Head zoom and press Use this again "
                          "to re-crop", foreground="#888", wraplength=200, justify="left").pack(anchor="w")
            except (OSError, tk.TclError):
                pass

        def view_full(path: str) -> None:
            """The picture on its own, as large as the screen allows - a 169x266 game portrait opens several
            times its own size, since at 100% there is nothing to see."""
            v = tk.Toplevel(dlg)
            esc_closes(v)
            v.title(Path(path).name)
            keep_: Dict[str, object] = {}
            try:
                im0 = raw0 = None
                try:
                    from PIL import Image
                    im0 = Image.open(path).convert("RGB")
                    w0, h0 = im0.width, im0.height
                except Exception:  # noqa: BLE001 - no Pillow, or a format it will not open
                    raw0 = open(path, "rb").read()
                    if raw0[:2] != b"BM":
                        raise tk.TclError("could not read the picture")
                    w0 = abs(struct.unpack_from("<i", raw0, 18)[0])
                    h0 = abs(struct.unpack_from("<i", raw0, 22)[0])
                if not w0 or not h0:
                    raise tk.TclError("could not read the picture")
                lab = tk.Label(v, cursor="hand2")
                lab.pack()
                cap = tk.StringVar()
                bar_ = ttk.Frame(v)
                bar_.pack(pady=(2, 6))
                fit = max(1.0, min((v.winfo_screenheight() - 220) / h0, (v.winfo_screenwidth() * 0.7) / w0))
                zoom = {"k": round(min(fit, 6.0), 2)}

                def render() -> None:
                    k = zoom["k"]
                    if im0 is not None:
                        from PIL import Image, ImageTk
                        sm = im0.resize((max(1, int(w0 * k)), max(1, int(h0 * k))),
                                        Image.NEAREST if k >= 3 else Image.LANCZOS)
                        keep_["img"] = ImageTk.PhotoImage(sm)
                    else:
                        ppm = bmp_to_ppm(raw0, 1)                    # type: ignore[arg-type]
                        base = tk.PhotoImage(data=ppm) if ppm else None
                        if base is None:
                            return
                        keep_["img"] = base.zoom(int(max(1, round(k))), int(max(1, round(k))))
                    lab.config(image=keep_["img"])                   # type: ignore[arg-type]
                    cap.set(f"{w0}x{h0}, shown at {int(k * 100)}%   -   click the picture to close, "
                            "right-click to save a copy")

                def step(by: float) -> None:
                    zoom["k"] = max(0.25, min(8.0, round(zoom["k"] * by, 2)))
                    render()
                ttk.Button(bar_, text="-", width=3, command=lambda: step(1 / 1.25)).pack(side="left")
                ttk.Button(bar_, text="+", width=3, command=lambda: step(1.25)).pack(side="left", padx=(4, 0))
                ttk.Button(bar_, text="1:1", width=4,
                           command=lambda: (zoom.update(k=1.0), render())).pack(side="left", padx=(4, 0))
                ttk.Button(bar_, text="Fit", width=4,
                           command=lambda: (zoom.update(k=round(fit, 2)), render())).pack(side="left", padx=(4, 10))
                ttk.Label(bar_, textvariable=cap, foreground="#666").pack(side="left")
                tu_ = ttk.Button(bar_, text="Touch up", width=9,
                                 command=lambda: (v.destroy(), skinned(path)))
                tu_.pack(side="right")
                tooltip(tu_, "Work on this picture: skin, lips, speckles, a spot of color, scars and wounds, "
                             "light, and the shape of what you click. It opens on the picture you are looking "
                             "at, and this window closes behind it.")
                render()
                lab.bind("<Button-1>", lambda e: v.destroy())
                lab.bind("<Button-3>", lambda e: save_image(v, Path(path).name, None, path))
                esc_closes(v)
                v.bind("<plus>", lambda e: step(1.25))
                v.bind("<minus>", lambda e: step(1 / 1.25))
                v.bind("<MouseWheel>", lambda e: step(1.25 if e.delta > 0 else 1 / 1.25))
            except (tk.TclError, OSError, ValueError, struct.error) as e:
                ttk.Label(v, text=f"Could not show {path}: {e}", padding=20).pack()

        def cand_cols() -> int:
            """How many candidates fit side by side right now."""
            wide = max((c.get("tw", 0) for c in cells), default=160) + 24
            return max(1, int(ccv.winfo_width() or dlg.winfo_width() or 900) // max(80, wide))

        def relayout(*_a: object) -> None:
            cols = cand_cols()
            if cols == state.get("cand_cols") and _a:
                return
            state["cand_cols"] = cols
            for i, c in enumerate(cells):
                c["cell"].grid(row=i // cols, column=i % cols, sticky="n")
        ccv.bind("<Configure>", lambda e: (ccv.itemconfigure(cwin, width=e.width), relayout(e)), add="+")

        def crop_preview(*_a: object) -> None:
            """Draw on each candidate what the game portrait would keep."""
            try:
                zm = max(1.0, min(2.5, float(zoom_var.get())))
                bw, bh = max(16, int(w_var.get())), max(16, int(h_var.get()))
            except (tk.TclError, ValueError):
                return
            show_ = zm > 1.005 or nudge["dx"] or nudge["dy"]       # at zoom 1, centered, the box is the whole picture
            cropnote.set("the yellow box is what the game portrait keeps" if show_ else "")
            for mw in move_ws:                                       # nothing to nudge when the box is the picture
                try:
                    mw.pack(**mw._ps_pack) if show_ else mw.pack_forget()   # type: ignore[attr-defined]
                except (tk.TclError, AttributeError):
                    pass
            for c in cells:
                cvs = c.get("canvas")
                if not cvs or "rect" not in c:                        # a cropped portrait: nothing to outline
                    continue
                try:
                    lf, tp, cw, ch = box_for(c["sw"], c["sh"])
                    sx, sy = c["tw"] / c["sw"], c["th"] / c["sh"]
                    box = (lf * sx, tp * sy, (lf + cw) * sx, (tp + ch) * sy)
                    for item, grow in ((c["rect"], 0), (c["shade"], 1)):
                        cvs.coords(item, box[0] - grow, box[1] - grow, box[2] + grow, box[3] + grow)
                        cvs.itemconfigure(item, state="normal" if show_ else "hidden")
                except (tk.TclError, KeyError, ZeroDivisionError, AttributeError):
                    pass
            if not generic:                                          # the one proposed from here follows the box
                try:
                    if recut_job.get("id"):
                        dlg.after_cancel(recut_job["id"])            # type: ignore[arg-type]
                    recut_job["id"] = dlg.after(700, recut_proposal)
                except (tk.TclError, NameError):                    # (while the window is still being built)
                    pass

        recut_job: Dict[str, object] = {}
        dlg.after(2000, lambda: recut_job.__setitem__("armed", True))   # changes made by you, not by the opening

        def recut_proposal() -> None:
            """Cut the proposed portrait again when the crop changes, if it was cut from one of the pictures
            here - so zooming or nudging after "Use this" updates the proposal instead of needing it again."""
            recut_job["id"] = None
            if not recut_job.get("armed"):
                return
            prop_ = str(props.get(key) or "")
            m_ = CUT_MARK.search(os.path.basename(prop_))
            if not m_:
                return
            base_ = os.path.normcase(os.path.join(os.path.dirname(prop_), os.path.basename(prop_)[:m_.start()]))
            src_ = next((c["path"] for c in cells if os.path.normcase(os.path.splitext(c["path"])[0]) == base_
                         and not str(c["path"]).lower().endswith(".bmp")), "")
            if not src_:
                return
            try:
                zm_ = max(1.0, min(2.5, float(zoom_var.get())))
            except (tk.TclError, ValueError):
                return
            want_ = f"_z{int(round(zm_ * 10))}" + (f"_m{int(nudge['dx'] * 100)}_{int(nudge['dy'] * 100)}"
                                                    if nudge["dx"] or nudge["dy"] else "")
            if os.path.basename(prop_)[m_.start():].lower().endswith(f"{want_}.bmp".lower()):
                return                                               # already cut this way
            use(src_)
            gstat.set(f"The proposal follows the crop: cut again from {Path(src_).name}.")

        restyle = {"path": "", "k": 0.75}                             # Generate starts from this picture

        def restyle_from(path: str, k: float = 0.0) -> None:
            restyle.update(path=path, k=k)
            p_guard["on"] = True                                     # Start from shows it, and its number is the
            try:                                                     # restyle's strength to fine-tune
                p_box.config(values=START_FROM + (["This picture"] if path else []))   # type: ignore[attr-defined]
                p_sv.set("This picture" if path else start_v.get())
                p_sk.set(f"{k:.2f}" if path else start_k.get())
            finally:
                p_guard["on"] = False
            gstat.set(f"Generate now starts from {Path(path).name} at strength {k:.2f} (fine-tune it beside Start "
                      "from): the description and style repaint it, keeping its face and pose. Pick another Start "
                      "from, or right-click it again, to stop." if path else
                      "Generate paints new pictures again (Start from).")

        def edit_from(path: str, expression: str = "", change: str = "", amount: float = 1.0) -> None:
            """Repaint a candidate from itself in edit mode: the same person, with only what is asked changed.
            As many new candidates as Candidates says, each a little different."""
            if not hasattr(pg, "edit_prompt"):
                return
            try:
                n_ = max(1, min(max_c, int(count_var.get())))
            except (tk.TclError, ValueError):
                n_ = 1
            try:
                data_ = Path(path).read_bytes()
            except OSError as e_:
                gstat.set(f"Could not read {Path(path).name}: {e_}")
                return
            prompt_ = pg.edit_prompt(gs, expression, change)          # type: ignore[attr-defined]
            eng = get_engine()
            gstat.set(f"{Path(path).name}: {expression.lower() if expression else change}...")
            gen_btn.config(state="disabled", bg="#7fae8d")

            def work() -> None:
                try:
                    def got(i: int, d: bytes) -> None:
                        pth = pg.save_candidates([d], folder, f"{stem}_e{i + 1}")[0]   # type: ignore[attr-defined]
                        #                                    (no fading: a faded picture comes back faded already)
                        root.after(0, add_candidate, pth)
                    # Full strength makes the change in full; less blends it with the picture as it was (8 steps,
                    # so that there is room between) - "a little darker" that came out near black, made milder.
                    eng.generate(prompt_, "", n_, got, None, init=data_,   # type: ignore[attr-defined]
                                 strength=amount, steps=None if amount >= 0.99 else 8)
                    root.after(0, done, "")
                except Exception as e_:  # noqa: BLE001 - the engine's messages are meant for the user
                    root.after(0, done, str(e_) or e_.__class__.__name__)
            threading.Thread(target=work, daemon=True).start()

        def edit_ask(path: str) -> None:
            """What to change, in words, and how strongly."""
            w_ = tk.Toplevel(dlg)
            w_.title("Change it")
            w_.transient(dlg)
            esc_closes(w_)
            f_ = ttk.Frame(w_, padding=12)
            f_.pack(fill="both", expand=True)
            ttk.Label(f_, text="What to change? Everything else is kept.\n(for example: make the hair gray / add "
                               "a deep hood / give him a small black eye patch)").pack(anchor="w")
            tv_ = tk.StringVar(value=str(st.get("edit_last") or ""))
            en_ = ttk.Entry(f_, textvariable=tv_, width=70)
            en_.pack(fill="x", pady=(6, 8))
            w_.after(60, lambda: (en_.focus_set(), en_.select_range(0, "end")))   # after Esc's own focus grab
            row_ = ttk.Frame(f_)
            row_.pack(fill="x")
            ttk.Label(row_, text="Strength:").pack(side="left")
            kv_ = tk.DoubleVar(value=float(st.get("edit_strength") or 1.0))
            kl_ = ttk.Label(row_, text="", width=5)
            sc_ = ttk.Scale(row_, from_=0.5, to=1.0, variable=kv_, length=260,
                            command=lambda _v: kl_.config(text=f"{kv_.get():.2f}"))
            sc_.pack(side="left", padx=6)
            kl_.pack(side="left")
            kl_.config(text=f"{kv_.get():.2f}")

            def at_(e: object) -> str:
                """A click (or a drag) anywhere on the line puts the strength there, in steps of 0.05 - not a
                jump to one end, which is what a plain ttk scale does."""
                w__ = max(1, sc_.winfo_width() - 16)
                f_ = max(0.0, min(1.0, (int(e.x) - 8) / w__))       # type: ignore[attr-defined]
                kv_.set(round((0.5 + 0.5 * f_) * 20) / 20)
                kl_.config(text=f"{kv_.get():.2f}")
                return "break"
            sc_.bind("<Button-1>", at_)
            sc_.bind("<B1-Motion>", at_)
            tooltip(sc_, "1.00 makes the change in full. Lower blends it with the picture as it is: 0.7 for "
                         "a little, 0.55 for a touch.")

            def go_(_e: object = None) -> None:
                t_ = tv_.get().strip()
                if not t_:
                    return
                k_ = round(max(0.5, min(1.0, float(kv_.get()))), 2)
                st["edit_last"], st["edit_strength"] = t_, k_
                save_settings(st)
                w_.destroy()
                edit_from(path, change=t_, amount=k_)
            en_.bind("<Return>", go_)
            br_ = ttk.Frame(f_)
            br_.pack(fill="x", pady=(10, 0))
            ttk.Button(br_, text="Change", command=go_).pack(side="right")
            ttk.Button(br_, text="Cancel", command=w_.destroy).pack(side="right", padx=6)

        def cand_menu(event: object, path: str) -> None:
            menu = tk.Menu(dlg, tearoff=0)
            exs_ = list(((gs.get("edit_terms") or {}).get("expressions") or {}))   # type: ignore[union-attr]
            if exs_ and hasattr(pg, "edit_prompt"):
                sube_ = tk.Menu(menu, tearoff=0)
                for x_ in exs_:
                    sube_.add_command(label=x_, command=lambda x=x_: edit_from(path, expression=x))
                sube_.add_separator()

                def other_() -> None:
                    w_ = simpledialog.askstring("Portrait Scout", "Which expression?\n(for example: a nervous grin, "
                                                "a haunted stare, mild disgust)", parent=dlg)
                    if w_ and w_.strip():
                        edit_from(path, expression=w_.strip())
                sube_.add_command(label="Other...", command=other_)
                menu.add_cascade(label="Change expression", menu=sube_)
                menu.add_command(label="Change it...", command=lambda: edit_ask(path))
                menu.add_separator()
            if hasattr(pg, "touch_up"):                              # by hand: glowing eyes, an exact recolor
                menu.add_command(label="Touch up...", command=lambda: skinned(path))
            same_ = bool(restyle["path"]) and os.path.normcase(restyle["path"]) == os.path.normcase(path)
            if same_:
                menu.add_command(label="Stop restyling from this", command=lambda: restyle_from(""))
            else:
                sub_ = tk.Menu(menu, tearoff=0)
                for k_ in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85):
                    lab_ = f"{k_:.2f}" + ("  (keeps the most)" if k_ == 0.5 else "  (mostly new)" if k_ == 0.85 else "")
                    sub_.add_command(label=lab_, command=lambda k=k_: restyle_from(path, k))
                menu.add_cascade(label="Restyle from this", menu=sub_)
            menu.add_separator()
            menu.add_command(label=f"Save {Path(path).name} as...", command=lambda: save_image(dlg, Path(path).name, None, path))
            menu.add_command(label="Open full size", command=lambda: view_full(path))
            sd_ = image_seed(path)
            menu.add_separator()
            if sd_ is None:
                menu.add_command(label="No seed in this picture", state="disabled")
            else:
                def take_seed(v: int = sd_) -> None:
                    seed_var.set(str(v))
                    keep_seed()
                    notes.setdefault(key, {})["init"] = str((st.get("vary_init") or {}).get(plain_of(path).name, ""))   # type: ignore[union-attr]
                    save_settings(st)
                    w_ = str((st.get("vary_words") or {}).get(plain_of(path).name, ""))   # type: ignore[union-attr]
                    if w_ and w_.split("||")[0].strip() not in desc_var.get():   # the face was made in words
                        desc_var.set(pg.vary_merge(desc_var.get().strip(), w_, gs))   # type: ignore[attr-defined]
                    gstat.set(f"Seed {v}: Generate makes this face now"
                              + (", its face words added to the description" if w_ else "")
                              + ". Clear the Seed box (x) for new faces again.")
                menu.add_command(label=f"Use this face (seed {sd_})", command=take_seed)
                menu.add_command(label="Copy seed", command=lambda v=sd_: copy_text(str(v)))
            menu.add_separator()
            menu.add_command(label="Discard", command=lambda: discard([path]))
            try:
                menu.tk_popup(event.x_root, event.y_root)            # type: ignore[attr-defined]
            finally:
                menu.grab_release()

        def fit_card(pi: object, path: str) -> Tuple[object, float]:
            """One candidate shrunk (or grown) to the card box. Returns the picture and real size / shown size,
            which is what the yellow-box maths uses to get back to the full-size picture."""
            try:
                from PIL import Image, ImageTk                        # exact scaling when Pillow is here
                im = Image.open(path).convert("RGB")
                sc = min(CAND_W / max(1, im.width), CAND_H / max(1, im.height))
                sm = im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))))
                return ImageTk.PhotoImage(sm), (1.0 / sc if sc else 1.0)
            except Exception:  # noqa: BLE001 - no Pillow, or a format it will not open
                pass
            w0, h0 = pi.width(), pi.height()                          # type: ignore[attr-defined]
            want = min(CAND_W / max(1, w0), CAND_H / max(1, h0))
            fr = Fraction(want).limit_denominator(8)                  # tkinter can only zoom/shrink by whole numbers
            num, den = max(1, fr.numerator), max(1, fr.denominator)
            out = pi
            if num > 1:
                out = out.zoom(num, num)                              # type: ignore[attr-defined]
            if den > 1:
                out = out.subsample(den, den)                         # type: ignore[attr-defined]
            shown = max(1, out.height())                              # type: ignore[attr-defined]
            return out, h0 / shown

        ticked: Set[str] = set()                                     # candidates ticked for "Discard ticked"
        tick_ui: Dict[str, object] = {}

        def tick_count() -> None:
            """Forget ticks on cards that are gone, and show how many are ticked on the button."""
            ticked.intersection_update({os.path.normcase(str(c["path"])) for c in cells})
            b_ = tick_ui.get("btn")
            if b_ is not None and b_.winfo_exists():                 # type: ignore[attr-defined]
                n_ = len(ticked)
                b_.config(text=f"Discard ticked ({n_})" if n_ else "Discard ticked",   # type: ignore[attr-defined]
                          state="normal" if n_ else "disabled")

        def set_tick(path: str, on: bool) -> None:
            (ticked.add if on else ticked.discard)(os.path.normcase(path))
            tick_count()

        def relist() -> None:
            """Draw the candidate strip again, at whatever size the Full size box now says."""
            paths = [str(c["path"]) for c in cells]
            for c in list(cells):
                c["cell"].destroy()
            cells.clear()
            state["cand_cols"] = None
            for q in paths:
                add_candidate(q)

        def prop_source() -> str:
            """The picture the proposal now was cut from ("X_1_169x266_z12_m0_1.bmp" is X_1.png), if it is
            still there: it leads the candidates, marked, so it can be cropped and worked on like the rest."""
            if generic:
                return ""
            prop_ = str(props.get(key) or "")
            m_ = re.search(r"_\d+x\d+(_z\d+)?(_m-?\d+_-?\d+)?\.bmp$", os.path.basename(prop_), re.I)
            if not m_:                                               # (older cuts carry no zoom mark)
                return ""
            base_ = os.path.join(os.path.dirname(prop_), os.path.basename(prop_)[:m_.start()])
            for ext_ in (".png", ".jpg", ".jpeg", ".webp"):
                if os.path.isfile(base_ + ext_):
                    return base_ + ext_
            return ""

        def prop_shown() -> str:
            """What stands for the proposal among the candidates: the picture it was cut from, or the cut itself
            when that picture is gone (one brought in long ago, say)."""
            src_ = prop_source()
            if src_:
                return src_
            p_ = "" if generic else str(props.get(key) or "")
            return p_ if p_ and os.path.isfile(p_) else ""

        def mark_now(c_: dict) -> None:
            """The proposed one: a green frame and "proposed now", as the gallery and Auto have it - kept up to
            date when another is proposed from this window."""
            try:
                now_ = bool(prop_shown()) and os.path.normcase(prop_shown()) == os.path.normcase(str(c_["path"]))
                c_["canvas"].config(highlightthickness=5 if now_ else 0,
                                    highlightbackground="#2e9e4f", highlightcolor="#2e9e4f")
                rw_, rh_, ig_, db_ = c_["rw"], c_["rh"], c_["in_game"], c_["done_bmp"]
                c_["szl"].config(text=(f"its portrait now {rw_}x{rh_}" if ig_ else
                                       f"\u2714 proposed now {rw_}x{rh_}" if now_ else
                                       f"game portrait {rw_}x{rh_}" if db_ else f"{rw_}x{rh_}"),
                                 foreground="#1a5fb4" if ig_ else "#1a7f37" if (db_ or now_) else "#888",
                                 font=("TkDefaultFont", 9, "bold") if (db_ or now_) else "TkDefaultFont")
            except (KeyError, tk.TclError):
                pass

        def add_candidate(path: str) -> None:
            if not dlg.winfo_exists():
                return
            cell = ttk.Frame(cands, padding=4)
            cols_ = state.get("cand_cols") or cand_cols()
            cell.grid(row=len(cells) // cols_, column=len(cells) % cols_, sticky="n")   # type: ignore[operator]
            info = {"path": path, "cell": cell, "image": None}
            done_bmp = path.lower().endswith(".bmp")                 # a game portrait made from one of the candidates
            try:
                pil = None
                try:                                                 # jpg, webp, tga... Tk reads none of them
                    from PIL import Image, ImageTk
                    pil = Image.open(path).convert("RGB")
                except Exception:  # noqa: BLE001 - no Pillow, or a format it will not open
                    pil = None
                if pil is not None:
                    box_ = (CAND_W, CAND_H) if not full_var.get() else (10_000, 300)
                    sc = min(box_[0] / max(1, pil.width), box_[1] / max(1, pil.height), 1.0)
                    sm = pil.resize((max(1, round(pil.width * sc)), max(1, round(pil.height * sc))))
                    pi, k = ImageTk.PhotoImage(sm), (1.0 / sc if sc else 1.0)
                else:
                    if done_bmp:
                        ppm = bmp_to_ppm(open(path, "rb").read(), 1)
                        if not ppm:
                            raise tk.TclError("not a picture")
                        pi = tk.PhotoImage(data=ppm)
                    else:
                        pi = tk.PhotoImage(file=path)
                    if full_var.get():                               # each at its own size
                        k = max(1, -(-pi.height() // 300))
                        pi = pi.subsample(k, k) if k > 1 else pi
                    else:                                            # all the same, so they compare side by side
                        pi, k = fit_card(pi, path)
                cvs = tk.Canvas(cell, width=pi.width(), height=pi.height(), highlightthickness=0, cursor="hand2")
                cvs.create_image(0, 0, image=pi, anchor="nw")
                if not done_bmp:                                     # what "Use this" would keep at the settings above
                    info["rect"] = cvs.create_rectangle(0, 0, 0, 0, outline="#ffd000", width=2)
                    info["shade"] = cvs.create_rectangle(0, 0, 0, 0, outline="#303030", width=1)
                cvs.pack()
                cvs.bind("<Button-1>", lambda e: view_full(path))     # click: full size
                cvs.bind("<Button-3>", lambda e: cand_menu(e, path))  # right click: save a copy
                rw, rh = int(round(pi.width() * k)), int(round(pi.height() * k))
                in_game = "_gnow_" in Path(path).stem             # the picture it wears in the game right now
                srow = ttk.Frame(cell)
                srow.pack()
                tv = tk.BooleanVar(value=os.path.normcase(path) in ticked)
                tcb = ttk.Checkbutton(srow, variable=tv, command=lambda: set_tick(path, tv.get()))
                tcb.pack(side="left")
                tooltip(tcb, "Tick to delete with Discard ticked. Ctrl+click the picture does the same.")
                cvs.bind("<Control-Button-1>", lambda e: (tv.set(not tv.get()), set_tick(path, tv.get()), "break")[-1])
                szl_ = ttk.Label(srow)
                szl_.pack(side="left")
                info.update(szl=szl_, rw=rw, rh=rh, in_game=in_game, done_bmp=done_bmp, canvas=cvs)
                mark_now(info)
                info.update(image=pi, canvas=cvs, sw=rw, sh=rh,
                            tw=pi.width(), th=pi.height(), tick=tv)
            except tk.TclError:
                ttk.Label(cell, text=Path(path).name, wraplength=160).pack()
            cells.append(info)
            relayout()
            crop_preview()
            btns = ttk.Frame(cell)
            btns.pack(pady=2)
            ttk.Button(btns, text=("Show" if generic else "Propose") if done_bmp else
                       ("Make portrait" if generic else "Use this"),
                       command=lambda: (propose(path) if done_bmp else use(path))).pack(side="left")
            ttk.Button(btns, text="Discard", width=8, command=lambda: discard([path])).pack(side="left", padx=(4, 0))
            worked = Path(path).stem.endswith(("_ghost", "_skin", "_touch"))
            #   Touch up is on the right-click menu now: Change it... does most of what it did, better
            if worked:
                gb = ttk.Button(btns, text="Plain", width=6, command=lambda: plain_again(path))
                gb.pack(side="left", padx=(4, 0))
                tooltip(gb, "Put the picture back as it came out of the engine.")
            catch_wheel(cell)

        def swap_candidate(path: str, new_: str, drop: str = "") -> None:
            """Put another file in this candidate's place, keeping them all in the order they were in."""
            paths = [c["path"] for c in cells]
            for c in cells:
                c["cell"].destroy()
            cells.clear()
            for x in paths:
                add_candidate(new_ if os.path.normcase(x) == os.path.normcase(path) else x)
            if drop:
                try:
                    os.remove(drop)
                except OSError:
                    pass

        def plain_again(path: str) -> None:
            """Back to the picture as it came out of the engine. The worked-on copy is deleted."""
            q = Path(path)
            cut = next(len(x) for x in ("_ghost", "_touch", "_skin") if q.stem.endswith(x))
            back = next((str(x) for x in (q.with_name(q.stem[:-cut] + e) for e in
                                          (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga", ".gif", ".tif"))
                         if x.is_file()), "")
            if not back:
                gstat.set("The plain picture is no longer there, so this one cannot go back.")
                return
            swap_candidate(path, back, str(q))
            gstat.set(f"{Path(back).name} — the plain picture.")



        def skinned(path: str) -> None:
            """Touch up this candidate. The window itself lives at module level, so the creature's own
            picture can be worked on from the lines window too."""
            touch_up_window(dlg, path, pg=pg, gs=gs, r=r, desc=desc_var.get(), say=gstat.set,
                            on_done=swap_candidate, touch_memory=TOUCH_MEMORY, generic=generic,
                            change_it=edit_ask)

        def files_of(path: str) -> List[str]:
            """A generated picture and the game-size .bmp copies made from it."""
            base = Path(path).stem + "_"
            out = [path]
            try:
                out += [str(q) for q in Path(path).parent.iterdir()
                        if q.name.startswith(base) and (q.suffix.lower() == ".bmp"
                                                        or q.stem in (Path(path).stem + "_ghost",
                                                                      Path(path).stem + "_touch",
                                                                      Path(path).stem + "_skin"))]
            except OSError:
                pass
            return out

        def discard(paths: List[str], confirm: str = "") -> None:
            """Delete generated pictures from the disk and take them off the window."""
            # from the plain picture, so discarding a touched-up card takes its original with it
            files = [f for q in paths for f in files_of(str(plain_of(q)))]
            same = lambda q: os.path.normcase(os.path.normpath(q))  # noqa: E731
            cur = props.get(key)
            used = bool(cur) and same(cur) in {same(f) for f in files}
            if used:
                confirm = (confirm + "\n\n" if confirm else "") + ("The proposed portrait for this creature is among them, "
                           "so the proposal is cleared too." if len(paths) > 1 else
                           "This picture is the proposed portrait. Discard it and clear the proposal?")
            if confirm and not messagebox.askyesno("Portrait Scout", confirm, parent=dlg):
                return
            failed = 0
            for f in files:
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass
                except OSError:
                    failed += 1
            gone = {same(q) for q in paths if not os.path.exists(q)}
            for c in [c for c in cells if same(c["path"]) in gone]:
                c["cell"].destroy()
                cells.remove(c)
            relayout()
            if used and not os.path.exists(cur):
                props.pop(key, None)
                persist_props()
                for w_ in result_box.winfo_children():
                    w_.destroy()
                if view_var.get() == "gallery":
                    rebuild_keep_scroll()
                refresh_table_only()
                if callable(after):
                    after()                                          # type: ignore[operator]
            gstat.set(f"Discarded {len(gone)} picture(s)." + (f" {failed} file(s) could not be deleted." if failed else ""))
            tick_count()

        def discard_ticked() -> None:
            paths = [str(c["path"]) for c in cells if os.path.normcase(str(c["path"])) in ticked]
            if not paths:
                return
            discard(paths, f"Delete the {len(paths)} ticked picture(s) from the disk? This cannot be undone.")

        def discard_all() -> None:
            try:
                every = [str(q) for q in folder.glob(f"{stem}_c*.png")] + [str(q) for q in folder.glob(f"{stem}_c*.bmp")] \
                    + [str(q) for q in folder.glob(f"{stem}_f*") if q.is_file()] \
                    + [str(q) for q in folder.glob(f"{stem}_e*") if q.is_file()]   # brought in, and edits
            except OSError:
                every = []
            src_ = prop_source()                                     # the proposed one stays (right-click it >
            if src_ or props.get(key):                               # Discard to delete it on purpose)
                keep_ = {os.path.normcase(x) for x in (src_, str(props.get(key) or "")) if x}
                stem_ = os.path.normcase(os.path.splitext(src_)[0]) if src_ else ""
                every = [x for x in every if os.path.normcase(x) not in keep_
                         and not (stem_ and os.path.normcase(x).startswith(stem_ + "_"))]
            if not every:
                gstat.set("Nothing to discard" + (" but the proposed one (right-click it > Discard to delete it)."
                                                  if src_ else "."))
                return
            discard(every, f"Delete all {len(every)} generated picture(s) for {r['name']}"
                    + (", all but the proposed one" if src_ else "") + "? This cannot be undone.")

        def done(err: str) -> None:
            status.set(f"Portrait for {r['name']}: " + (err if err else "candidates ready"))
            if not dlg.winfo_exists():
                return
            if busy["spin"]:
                gbar_.stop()
                busy["spin"] = False
            gbar_.pack_forget()
            gen_btn.config(state="normal", text="Generate", bg="#2e9e4f", activebackground="#25803f", command=run)
            stopped = bool(getattr(get_engine(), "canceled", None) and get_engine().canceled.is_set())   # type: ignore[union-attr]
            gstat.set(err if err else "Stopped. The ones already made are here; press Generate for more."
                      if stopped else "Done. Press \"Use this\" under the one you want, or Generate for more.")
            engine_state()

        def run() -> None:
            try:
                n = max(1, min(max_c, int(count_var.get())))
            except (tk.TclError, ValueError):
                n = 2
            seed_txt = seed_var.get().strip()
            seed_ = int(seed_txt) if seed_txt.isdigit() else None
            init_was = str((notes.get(key) or {}).get("init") or "") if seed_ is not None else ""
            notes[key] = {"desc": desc_var.get().strip(), "count": n, "age": age_v.get(), "look": look_v.get(),
                          "mood": mood_v.get(), "outfit": cloth_v.get(),
                          "scars": scar_v.get(), "grime": grime_v.get(), "beard": beard_v.get(),
                          "whose": whose, "seed": seed_txt if seed_ is not None else "", "init": init_was}
            if generic:
                notes[key].update(race=race_v.get(), sex=sex_v.get(), **{"class": class_v.get()})
            st["portrait_style"] = style_var.get()
            st["portrait_style_animal" if animal_style(style_var.get()) else "portrait_style_people"] = style_var.get()
            save_settings(st)
            pr, ng = prompt_now()
            vary_on = seed_ is None                                  # a seed asks for one face: no new face words
            eng = get_engine()

            def stop_now() -> None:
                """Stop after the picture being painted, and leave the engine running."""
                gen_btn.config(state="disabled", text="Stopping...", bg="#9bbfa6")
                gstat.set("Stopping after this one...")
                threading.Thread(target=getattr(eng, "cancel", lambda: None), daemon=True).start()
            if hasattr(eng, "cancel"):                               # the green button becomes the way to stop
                gen_btn.config(text="Stop", bg="#b5452f", activebackground="#8f3524", command=stop_now)
            else:
                gen_btn.config(state="disabled", bg="#7fae8d")

            def work() -> None:
                try:
                    def on_image(i: int, data: bytes, words: str = "", src: str = "") -> None:
                        pth = pg.save_candidates([data], folder, f"{stem}_c{i + 1}",   # type: ignore[attr-defined]
                                                 ghost_depth())[0]
                        start_note(pth, src)
                        if words:                                    # so "Use this face" can bring it back
                            vw_ = st.setdefault("vary_words", {})
                            vw_[Path(pth).name] = words              # type: ignore[index]
                            for old_ in list(vw_)[:-800]:            # type: ignore[arg-type]
                                vw_.pop(old_, None)                  # type: ignore[union-attr]
                        root.after(0, add_candidate, pth)
                    rs_ = restyle["path"]
                    if rs_ and os.path.isfile(rs_):
                        # Restyle: each picture starts from the one picked, repainted in the description and
                        # style. No new face words - the face is the picture's.
                        ini_ = Path(rs_).read_bytes()
                        for i_ in range(n):
                            if i_ and getattr(eng, "canceled", None) is not None and eng.canceled.is_set():   # type: ignore[attr-defined]
                                break
                            eng.generate(pr, ng, 1,                  # type: ignore[attr-defined]
                                         lambda _k, data, k=i_: on_image(k, data, "", rs_),
                                         None, init=ini_, strength=float(restyle["k"]))
                        save_settings(st)
                        root.after(0, done, "")
                        return
                    if vary_on:
                        # Klein paints nearly one face for one description whatever the seed: each picture
                        # gets a face of its own in words instead, one request each.
                        rnd_ = random.Random()
                        for i_ in range(n):
                            if i_ and getattr(eng, "canceled", None) is not None and eng.canceled.is_set():   # type: ignore[attr-defined]
                                break
                            words_ = pg.vary_words(rnd_, gs, desc_var.get())   # type: ignore[attr-defined]
                            pr_i, ng_i = prompt_now(words_)
                            ini_, src_ = start_pick(pg, gs, race_v.get(), desc_var.get(), pr_i, r)
                            eng.generate(pr_i, ng_i, 1,          # type: ignore[attr-defined]
                                         lambda _k, data, k=i_, w=words_, s=src_: on_image(k, data, w, s),
                                         (seed_ + i_) if seed_ is not None else None,
                                         **({"init": ini_, "strength": start_strength(pg, gs, desc_var.get(), race_v.get()),
                                             "steps": start_steps(pg, gs, desc_var.get(), race_v.get()),
                                             **start_edit(pg, gs, desc_var.get(), race_v.get(), str(r.get("anim") or ""), src_)}
                                            if ini_ else {}))
                        save_settings(st)
                        root.after(0, done, "")
                        return
                    # No seed: the engine rolls its own, so Generate never hands back a face it has painted
                    # before. A creature's seed used to be worked out from its creature file, which meant the
                    # same four faces every time the tab was opened again - and spares already in the pool
                    # coming back as "new" candidates.
                    ini_ = init_bytes(init_was) if init_was else None   # "Use this face": its start face too
                    if ini_ and Path(init_was).suffix.lower() == ".jpg" and hasattr(pg, "bank_fill") \
                            and "faces" in Path(init_was).parts:
                        mon_ = pg.bank_monster(gs, desc_var.get(), race_v.get())   # type: ignore[attr-defined]
                        ini_ = pg.bank_hulk(ini_, desc_var.get()) if mon_ and len(mon_) > 3 and mon_[3] else \
                            pg.bank_fill(ini_, pg.bank_body(gs, init_was), pg.bank_scale(gs), blur=float(mon_[2])) if mon_ else \
                            pg.bank_fill(ini_, pg.bank_body(gs, init_was), pg.bank_scale(gs), pg.bank_beard(desc_var.get()),
                                           pg.bank_bald(desc_var.get()), pg.bank_wide(desc_var.get()),
                                           pg.bank_bare(desc_var.get()), pg.bank_nose(desc_var.get()),
                                           pg.bank_ears(desc_var.get()), chin="child" in Path(init_was).parts)   # type: ignore[attr-defined]
                    eng.generate(pr, ng, n,                           # type: ignore[attr-defined]
                                 lambda k, d, s=(init_was if ini_ else ""): on_image(k, d, "", s), seed_,
                                 **({"init": ini_, "strength": start_strength(pg, gs, desc_var.get(), race_v.get()),
                                     "steps": start_steps(pg, gs, desc_var.get(), race_v.get()),
                                     **start_edit(pg, gs, desc_var.get(), race_v.get(), str(r.get("anim") or ""), init_was)}
                                    if ini_ else {}))
                    root.after(0, done, "")
                except Exception as e:  # noqa: BLE001 - GeneratorError texts are meant for the user
                    root.after(0, done, str(e) or e.__class__.__name__)
            threading.Thread(target=work, daemon=True).start()
        gen_btn.config(command=run)
        share_btn = ttk.Button(bar, text="Use for others...",
                               command=lambda: share_portrait(r, dlg, after if callable(after) else None))
        if not generic and key in props:
            share_btn.pack(side="left", padx=(8, 0))

        # the image engine: start / stop by hand, and whether it outlives Portrait Scout
        eurl = str((gs.get("engine") or {}).get("url", ""))
        elab = ttk.Label(erow2, text="Image engine:")
        elab.pack(side="left")
        tooltip(elab, f"Portrait Scout talks to a generator at {eurl} (Automatic1111 / Forge API). Nothing is bundled: "
                      "it starts your own copy in the 'engine' folder, or you can point generator/settings.json at a "
                      "generator you already run.")
        eng_var = tk.StringVar(value="checking...")
        ttk.Label(erow2, textvariable=eng_var, width=12, foreground="#666").pack(side="left", padx=(4, 6))
        start_btn = ttk.Button(erow2, text="Start")
        start_btn.pack(side="left")
        stop_btn = ttk.Button(erow2, text="Stop")
        stop_btn.pack(side="left", padx=6)
        web_btn = ttk.Button(erow2, text="Web UI", width=7, command=lambda: webbrowser.open(eurl))
        web_btn.pack(side="left", padx=(0, 6))
        tooltip(web_btn, f"Open the generator's own page ({eurl}) in your browser, to watch it work or to use "
                         "it by hand. It has to be running.")
        keep_var = tk.BooleanVar(value=bool(st.get("engine_keep")))

        for _v in (zoom_var, w_var, h_var):
            _v.trace_add("write", crop_preview)

        def get_engine() -> object:
            if state.get("engine") is None:
                state["engine"] = pg.Engine(gs)                       # type: ignore[attr-defined]
            eng = state["engine"]
            eng.keep = bool(keep_var.get())                           # type: ignore[attr-defined]
            eng.log = lambda m: root.after(0, show_progress, None, m)                 # type: ignore[attr-defined]
            eng.progress = lambda frac, text: root.after(0, show_progress, frac, text)  # type: ignore[attr-defined]
            return eng

        def keep_changed() -> None:
            st["engine_keep"] = bool(keep_var.get())
            save_settings(st)
            get_engine()
        here_ = Path(__file__).resolve().parent
        show_var = tk.BooleanVar(value=bool((gs.get("engine") or {}).get("show_window")))

        def show_changed() -> None:
            try:
                pg.save_patch(gs, {"engine": {"show_window": bool(show_var.get())}})   # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", str(e), parent=dlg)
                return
            gstat.set("The generator gets its own window the next time it starts." if show_var.get() else
                      "The generator runs out of sight; what it prints goes to generator/engine.log.")
        def open_log() -> None:
            f = here_ / "generator" / "engine.log"
            if f.is_file():
                open_path(str(f))
            else:
                messagebox.showinfo("Portrait Scout", "There is no engine.log yet. It is written while the generator "
                                    "runs without a window of its own.", parent=dlg)
        emenu = tk.Menu(erow2, tearoff=0)                             # the settings that are set once and left alone
        emenu.add_checkbutton(label="Keep open after Portrait Scout closes", variable=keep_var, command=keep_changed)
        emenu.add_checkbutton(label="Give the generator its own window", variable=show_var, command=show_changed)
        emenu.add_separator()
        emenu.add_command(label="Show the log...", command=open_log)
        emenu.add_command(label="Open the generator's own page...", command=lambda: webbrowser.open(eurl))
        emenu.add_command(label="Set up the generator...", command=setup_window)
        emenu.add_command(label="Edit settings.json...",
                          command=lambda: open_path(str(here_ / "generator" / "settings.json")))
        ebtn = ttk.Menubutton(erow2, text="Engine \u25be", menu=emenu, width=9)
        ebtn.pack(side="left", padx=(6, 0))
        tooltip(ebtn, "Keep open: leave the generator running when Portrait Scout closes, so the model need not load again. It "
                "holds graphics memory until you press Stop.\n\nOwn window: watch what it is doing, from its next start. "
                "Off, the same text goes to generator/engine.log.")

        def engine_state() -> None:
            eng = get_engine()

            def show(ok: bool) -> None:
                if dlg.winfo_exists():
                    eng_var.set("running" if ok else "not running")
                    web_btn.config(state="normal" if ok else "disabled")
            threading.Thread(target=lambda: root.after(0, show, eng.alive()), daemon=True).start()   # type: ignore[attr-defined]

        def engine_job(stop: bool) -> None:
            eng = get_engine()
            start_btn.config(state="disabled")
            stop_btn.config(state="disabled")
            show_progress(None, "Stopping the image engine..." if stop else "Starting the image engine...")

            def fin(err: str) -> None:
                status.set("Image engine: " + (err or ("stopped" if stop else "ready")))
                if not dlg.winfo_exists():
                    return
                if busy["spin"]:
                    gbar_.stop()
                    busy["spin"] = False
                gbar_.pack_forget()
                start_btn.config(state="normal")
                stop_btn.config(state="normal")
                gstat.set(err or ("The image engine is stopped." if stop else "The image engine is ready."))
                engine_state()

            def work() -> None:
                try:
                    eng.stop(True) if stop else eng.start()           # type: ignore[attr-defined]
                    root.after(0, fin, "")
                except Exception as e:  # noqa: BLE001
                    root.after(0, fin, str(e) or e.__class__.__name__)
            threading.Thread(target=work, daemon=True).start()
        # the prompt itself: edit it, keep it, or put the style back the way it came
        pbody = ttk.Frame(frm)
        pbody.grid(row=7, column=0, columnspan=3, sticky="ew")
        frm.rowconfigure(7, weight=1)
        ptxt = tk.StringVar()
        pmsg = tk.StringVar(value="{character} is replaced by the description above plus your extra details; "
                                  "{subject} by the short form.")
        ttk.Label(pbody, textvariable=pmsg, foreground="#888", wraplength=700, justify="left").grid(row=0, column=0,
                                                                                                   columnspan=4, sticky="w")
        ptext = tk.Text(pbody, height=7, wrap="word", font=("TkDefaultFont", 9))
        ptext.grid(row=1, column=0, columnspan=3, sticky="ew")
        pscroll = ttk.Scrollbar(pbody, orient="vertical", command=ptext.yview)
        pscroll.grid(row=1, column=3, sticky="ns")
        ptext.config(yscrollcommand=pscroll.set)
        ttk.Label(pbody, text="Negative prompt (ignored at CFG 1):", foreground="#666").grid(row=3, column=0,
                                                                                            columnspan=4, sticky="w")
        ntext = tk.Text(pbody, height=3, wrap="word", font=("TkDefaultFont", 9))
        ntext.grid(row=4, column=0, columnspan=3, sticky="ew")
        nscroll = ttk.Scrollbar(pbody, orient="vertical", command=ntext.yview)
        nscroll.grid(row=4, column=3, sticky="ns")
        ntext.config(yscrollcommand=nscroll.set)
        pbody.columnconfigure(0, weight=1)
        pbtns = ttk.Frame(pbody)
        pbtns.grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        def load_prompt(*_a: object) -> None:
            """Put the chosen style's words in the boxes."""
            try:
                tm, ng = pg.style_text(gs, style_var.get())           # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                tm, ng = "", ""
            for box, val in ((ptext, tm), (ntext, ng)):
                box.delete("1.0", "end")
                box.insert("1.0", val)
            bg_var.set(pg.style_background(gs, style_var.get()))      # type: ignore[attr-defined]
            zm = pg.style_zoom(gs, style_var.get())                   # type: ignore[attr-defined]
            if zm:
                zoom_var.set(zm)
            base = pg.baseline_text(style_var.get())                  # type: ignore[attr-defined]
            rev_btn.config(state="normal" if base else "disabled")
            pmsg.set(f"Style \"{style_var.get()}\": edit the words and press Save to keep them. "
                     "{character} is replaced by the description at the top, {background} by the background box, "
                     "{subject} by the short form of the description."
                     + ("" if base else "  (your own style: Revert has nothing to go back to)"))

        def save_prompt(as_new: str = "") -> None:
            name = as_new or style_var.get()
            try:
                try:
                    zm = max(1.0, min(2.5, float(zoom_var.get())))
                except (tk.TclError, ValueError):
                    zm = 0
                pg.save_style(gs, name, ptext.get("1.0", "end-1c"), ntext.get("1.0", "end-1c"), zm,   # type: ignore[attr-defined]
                              bg_var.get().strip())
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", str(e), parent=dlg)
                return
            if as_new:
                names = pg.style_names(gs)                            # type: ignore[attr-defined]
                style_box.config(values=names)
                style_var.set(name)
                st["portrait_style"] = name
                st["portrait_style_animal" if animal_style(name) else "portrait_style_people"] = name
                save_settings(st)
            gstat.set(f"Saved the prompt and head zoom of \"{name}\" in generator/settings.json.")
            load_prompt()

        def save_as() -> None:
            name = simpledialog.askstring("Portrait Scout", "Name for this style:", parent=dlg,
                                          initialvalue=style_var.get() + " (mine)")
            if name and name.strip() and not name.startswith("_"):
                save_prompt(name.strip())

        def revert_prompt() -> None:
            base = pg.baseline_text(style_var.get())                  # type: ignore[attr-defined]
            if not base:
                return
            if not messagebox.askyesno("Portrait Scout", f"Put the prompt of \"{style_var.get()}\" back the way it "
                                       "came with Portrait Scout? Your changes to it are lost.", parent=dlg):
                return
            for box, val in ((ptext, base[0]), (ntext, base[1])):
                box.delete("1.0", "end")
                box.insert("1.0", val)
            bg_var.set(pg.baseline_background())                      # type: ignore[attr-defined]
            save_prompt()
        ttk.Button(pbtns, text="Save", command=lambda: save_prompt()).pack(side="left")
        ttk.Button(pbtns, text="Save as style...", command=save_as).pack(side="left", padx=6)
        rev_btn = ttk.Button(pbtns, text="Revert to baseline", command=revert_prompt)
        rev_btn.pack(side="left")
        ttk.Label(pbtns, text="unsaved changes are still used when you press Generate",
                  foreground="#888").pack(side="left", padx=8)
        pshow = tk.StringVar()

        def toggle_prompt() -> None:
            st["prompt_open"] = not st.get("prompt_open", False)
            save_settings(st)
            show_prompt()

        def show_prompt() -> None:
            opened = bool(st.get("prompt_open", False))
            pshow.set(("\u25bc" if opened else "\u25b6") + " Prompt")
            if opened:
                pbody.grid()
            else:
                pbody.grid_remove()
        pbtn_ = ttk.Button(erow2, textvariable=pshow, command=toggle_prompt, width=9)
        pbtn_.pack(side="left", padx=(16, 0))
        tooltip(pbtn_, "The words sent to the generator, for the style chosen above.")
        cp_btn = ttk.Button(erow2, text="Copy prompt", command=copy_prompt)
        cp_btn.pack(side="left", padx=(4, 0))
        tooltip(cp_btn, "Put the prompt and the negative prompt on the clipboard, to paste into a generator's own "
                        "page and try things there.")
        ttk.Label(prow, textvariable=gstat, foreground="#1a5fb4", wraplength=640, justify="left"
                  ).pack(side="left")
        spare_bar = ttk.Frame(side_)                                 # a few spares like this one, before
        spare_bar.pack(side="right", anchor="n")                     # generating more
        if not generic:                                              # and the creature as it walks about
            spr_p = tk.Frame(side_, width=160, height=238)           # its own size, whichever way it faces
            spr_p.pack(side="right", anchor="n", padx=(0, 14))
            spr_p.pack_propagate(False)
            sprite_show(r, spr_p, size=(150, 200))
        spare_hold: List[object] = []

        def fill_spares() -> None:
            """Up to six spares made for a creature of the same race and sex, a new six each time."""
            if not spare_bar.winfo_exists():
                return
            for x in spare_bar.winfo_children():
                x.destroy()
            spare_hold.clear()
            if generic:
                return
            mine = (str(r.get("race") or ""), str(r.get("sex") or ""))
            if state.get("pool_files") is None:
                state["pool_files"] = spare_pictures(pool_folder(), pool_used())
            taken_ = set((st.get("pool_from") or {}).values())       # type: ignore[union-attr]
            who = pool_who()
            lot = [q for q in (state.get("pool_files") or []) if who.get(made_from(q.name), ("", "")) == mine   # type: ignore[union-attr]
                   and os.path.normcase(os.path.normpath(str(q))) not in taken_]
            if not lot:
                return
            lab_ = ttk.Label(spare_bar, text=f"Pool ({len(lot)}) - another six", foreground="#555", cursor="hand2")
            lab_.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
            lab_.bind("<Button-1>", lambda e: fill_spares())
            tooltip(lab_, f"Spare pictures made for a {' '.join(x for x in mine if x)}. Click one to add it to "
                          "the candidates. Click here for another six.")
            try:
                from PIL import Image, ImageTk
            except ImportError:
                return
            for n_, q in enumerate(random.sample(lot, min(6, len(lot)))):
                try:
                    im_ = Image.open(q)
                    im_.thumbnail((66, 104))
                    ph_ = ImageTk.PhotoImage(im_)
                except Exception:  # noqa: BLE001
                    continue
                spare_hold.append(ph_)
                b_ = tk.Label(spare_bar, image=ph_, cursor="hand2", bd=1, relief="solid")
                b_.grid(row=1 + n_ // 3, column=n_ % 3, padx=1, pady=1)
                b_.bind("<Button-1>", lambda e, y=q: (take_image(str(y), came_from=str(y)), fill_spares()))
                tooltip(b_, f"{made_for(q.name)}: click to add it to the candidates.")
            mb_ = ttk.Button(spare_bar, text="More...", width=7, command=pick_spare)
            mb_.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(3, 0))
            tooltip(mb_, "All the spares, to pick from.")
        dlg.after_idle(fill_spares)
        take_ = str(state.pop("propose_take", "") or "")            # opened from the Pool with a picture
        if take_ and os.path.isfile(take_) and not generic:
            dlg.after_idle(lambda: take_image(take_, came_from=take_))
        style_box.bind("<<ComboboxSelected>>", load_prompt)
        load_prompt()
        show_prompt()

        # what is sent to the generator, and where it lives: the rest is in generator/settings.json
        sbody = ttk.Frame(frm, padding=(0, 4, 0, 0))
        sbody.grid(row=8, column=0, columnspan=3, sticky="ew")
        pay = {k: v for k, v in (gs.get("payload") or {}).items() if not k.startswith("_")}
        eng_s = gs.get("engine") or {}
        sv = {
            "url": tk.StringVar(value=str(eng_s.get("url", ""))),
            "start": tk.StringVar(value=str(eng_s.get("start", ""))),
            "model": tk.StringVar(value=str((pay.get("override_settings") or {}).get("sd_model_checkpoint", ""))),
            "sampler": tk.StringVar(value=str(pay.get("sampler_name", ""))),
            "scheduler": tk.StringVar(value=str(pay.get("scheduler", ""))),
            "width": tk.StringVar(value=str(pay.get("width", 652))),
            "height": tk.StringVar(value=str(pay.get("height", 1024))),
            "steps": tk.StringVar(value=str(pay.get("steps", 25))),
            "cfg": tk.StringVar(value=str(pay.get("cfg_scale", 1))),
            "dcfg": tk.StringVar(value=str(pay.get("distilled_cfg_scale", 2))),
        }
        auto_var = tk.BooleanVar(value=bool(eng_s.get("autostart", True)))

        def field(row: int, col: int, label: str, key: str, width: int, tip: str = "") -> None:
            ttk.Label(sbody, text=label).grid(row=row, column=col, sticky="e", padx=(0, 4), pady=1)
            e = ttk.Entry(sbody, textvariable=sv[key], width=width)
            e.grid(row=row, column=col + 1, sticky="w" if width < 40 else "ew", pady=1)
            if tip:
                tooltip(e, tip)
        sbody.columnconfigure(1, weight=1)
        field(0, 0, "Generator address:", "url", 46, "Where the generator answers (Automatic1111 / Forge API). "
              "Point this at one you already run to use it instead.")
        ttk.Checkbutton(sbody, text="Start it", variable=auto_var).grid(row=0, column=2, sticky="w", padx=8)
        field(1, 0, "Start command:", "start", 46, "Run from the Portrait Scout folder when nothing answers at the "
              "address above. {port} is taken from the address. Leave empty if you start the generator yourself.")
        field(2, 0, "Model (checkpoint):", "model", 46, "Sent as override_settings.sd_model_checkpoint - the name as "
              "your generator lists it.")
        srow2 = ttk.Frame(sbody)
        srow2.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        def quick_steps(n: int) -> None:
            """A step count that takes effect at once, without pressing Save."""
            sv["steps"].set(str(n))
            try:
                pg.save_patch(gs, {"payload": {"steps": n}})          # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", str(e), parent=dlg)
                return
            gstat.set(f"{n} steps from now on. More steps means more detail and a slower picture.")
        for lbl, fkey, w in (("Picture size:", "width", 6), ("x", "height", 6), ("   Steps:", "steps", 4),
                             ("   Sampler:", "sampler", 14), ("   Scheduler:", "scheduler", 14),
                             ("   CFG:", "cfg", 4), ("   Distilled CFG:", "dcfg", 4)):
            ttk.Label(srow2, text=lbl).pack(side="left")
            ttk.Entry(srow2, textvariable=sv[fkey], width=w).pack(side="left", padx=(4, 0))
            if fkey == "steps":                                      # the two worth switching between
                STEPS = {"25 (quicker)": 25, "35 (more detail)": 35}
                step_var = tk.StringVar(value=next((k for k, v in STEPS.items()
                                                    if str(v) == str(sv["steps"].get())), ""))

                def step_picked(*_a: object) -> None:
                    n_ = STEPS.get(step_var.get())
                    if n_:
                        quick_steps(n_)
                step_box = ttk.Combobox(srow2, textvariable=step_var, values=list(STEPS),
                                        state="readonly", width=16)
                step_box.pack(side="left", padx=(4, 0))
                step_box.bind("<<ComboboxSelected>>", step_picked)
                sv["steps"].trace_add("write", lambda *_a: step_var.set(
                    next((k for k, v in STEPS.items() if str(v) == str(sv["steps"].get())), "")))
                tooltip(step_box, "25 is the usual setting and quicker. 35 puts more detail into skin and hair and "
                                  "takes about half again as long. Type any other number in the box to the left.")
        sbtn = ttk.Frame(sbody)
        sbtn.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def save_engine() -> None:
            def num(key: str, cast: object, dflt: object) -> object:
                try:
                    return cast(sv[key].get().strip())               # type: ignore[operator]
                except (ValueError, tk.TclError):
                    return dflt
            patch = {"engine": {"url": sv["url"].get().strip(), "start": sv["start"].get().strip(),
                                "autostart": bool(auto_var.get())},
                     "payload": {"width": num("width", int, 652), "height": num("height", int, 1024),
                                 "steps": num("steps", int, 25), "sampler_name": sv["sampler"].get().strip(),
                                 "scheduler": sv["scheduler"].get().strip(), "cfg_scale": num("cfg", float, 1),
                                 "distilled_cfg_scale": num("dcfg", float, 2),
                                 "override_settings": {"sd_model_checkpoint": sv["model"].get().strip()}}}
            was = str((gs.get("engine") or {}).get("url", ""))
            try:
                pg.save_patch(gs, patch)                              # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", str(e), parent=dlg)
                return
            if was != patch["engine"]["url"]:
                state["engine"] = None                                # talk to the new address from now on
            gstat.set("Saved in generator/settings.json." + (" Press Stop first if the old generator is still running "
                      "and you want it closed." if was != patch["engine"]["url"] else ""))
            engine_state()
        ttk.Button(sbtn, text="Save", command=save_engine).pack(side="left")
        ttk.Button(sbtn, text="Open settings.json", command=lambda: open_path(
            str(Path(__file__).resolve().parent / "generator" / "settings.json"))).pack(side="left", padx=6)
        ttk.Label(sbtn, foreground="#888", wraplength=600, justify="left",
                  text="Anything else the generator takes goes in settings.json under \"payload\"; it is sent as it is."
                  ).pack(side="left", padx=8)
        sshow = tk.StringVar()

        def toggle_settings() -> None:
            st["engine_settings_open"] = not st.get("engine_settings_open", False)
            save_settings(st)
            show_settings()

        def show_settings() -> None:
            opened = bool(st.get("engine_settings_open", False))
            sshow.set(("\u25bc" if opened else "\u25b6") + " Generator settings")
            if opened:
                sbody.grid()
            else:
                sbody.grid_remove()
        sbtn_ = ttk.Button(erow2, textvariable=sshow, command=toggle_settings, width=21)
        sbtn_.pack(side="left", padx=(4, 0))
        tooltip(sbtn_, "Where the generator is, how to start it, which model, and the size the pictures are generated "
                       "at. Nothing here comes with Portrait Scout.")
        da_btn = tk.Button(erow2, text="Discard all", command=discard_all, bg="#b5452f", fg="white",
                           activebackground="#8f3524", activeforeground="white", relief="raised", bd=1)
        da_btn.pack(side="left", padx=(10, 0))
        dt_btn = tk.Button(erow2, text="Discard ticked", command=discard_ticked, bg="#b5452f", fg="white",
                           activebackground="#8f3524", activeforeground="white", disabledforeground="#e8c4bc",
                           relief="raised", bd=1, state="disabled")
        dt_btn.pack(side="left", padx=(6, 0))
        tick_ui["btn"] = dt_btn
        tooltip(dt_btn, "Delete the pictures whose box is ticked, from the disk. Asks first.")
        tooltip(da_btn, "Delete every picture generated or brought in for this creature, from the disk. This "
                        "cannot be undone, so it asks first.")
        show_settings()
        start_btn.config(command=lambda: engine_job(False))
        stop_btn.config(command=lambda: engine_job(True))
        engine_state()

        def current_shot() -> None:
            """The portrait this creature wears in the game now, put in with the candidates so it can be touched up
            or re-cut like any other picture. It is copied out of the game's files once, then kept."""
            def area(tag: object) -> int:                        # "210x330" -> 69300, so the bigger of the two
                m = re.match(r"(\d+)x(\d+)$", str(tag or ""))      # pictures is the one worked on
                return int(m.group(1)) * int(m.group(2)) if m else 0
            both = [(str(r.get("large_name") or ""), area(r.get("large_size"))),
                    (str(r.get("small_name") or ""), area(r.get("small_size")))]
            nm = max((x for x in both if x[0]), key=lambda x: x[1], default=("", 0))[0]
            if not nm or not res:
                return
            dst = folder / f"{stem}_gnow_{nm.upper()}.bmp"
            same_ = lambda q: os.path.normcase(os.path.normpath(str(q)))   # noqa: E731
            if any(same_(c["path"]) == same_(dst) for c in cells):
                return
            if not dst.is_file():
                try:
                    data = res["game"].get(nm, T_BMP)                # type: ignore[index]
                except Exception:  # noqa: BLE001 - a half-read install reads no pictures
                    data = None
                if not data:
                    return
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                except OSError:
                    return
            add_candidate(str(dst))

        try:                                                         # candidates made earlier for this creature,
            pics = ".png .bmp .jpg .jpeg .webp .tga .gif .tif .tiff"  # generated (_c), brought in (_f), or the
            found = {q for pat in (f"{stem}_c*", f"{stem}_f*", f"{stem}_gnow_*",   # game's own (_gnow_),
                                   f"{stem}_e*", f"{stem}_p*")       # edited (_e) and from the pool (_p)
                     for q in folder.glob(pat) if q.is_file() and q.suffix.lower() in pics}
            old = sorted(found, key=lambda q: q.stat().st_mtime)[-12:]
            src0 = prop_shown()                                      # what is proposed now, first of all
            if src0:
                old = [Path(src0)] + [q for q in old if os.path.normcase(str(q)) != os.path.normcase(src0)]
        except OSError:
            old = []
        for q in old:
            add_candidate(str(q))
        if not generic:
            current_shot()

    def share_portrait(r: dict, parent: object = None, after: object = None) -> None:
        """Give the picture proposed for this creature to other creatures as well - the rest of a decloned bunch,
        or everyone who shares its portrait now."""
        par = parent or root
        path = props.get(rowkey(r))
        if not path:
            messagebox.showinfo("Portrait Scout", "Propose a portrait for this creature first.", parent=par)
            return
        mine = {x.upper() for f in ("large", "small") for x in str(r.get(f) or "").split("/") if x}

        def ports(o: dict) -> Set[str]:
            return {x.upper() for f in ("large", "small") for x in str(o.get(f) or "").split("/") if x}
        here = rowkey(r)
        cands = [o for o in all_rows() if rowkey(o) != here
                 and (o["name"] == r["name"] or (mine and mine & ports(o)))]
        cands.sort(key=lambda o: (o["name"].lower(), (o.get("cre_list") or [""])[0]))
        if not cands:
            messagebox.showinfo("Portrait Scout", "No other creature shares this one's name or portrait.\n\n"
                                "(With Declone off, the creatures of one name are already a single row.)", parent=par)
            return
        w = tk.Toplevel(par)                                         # type: ignore[arg-type]
        esc_closes(w)
        w.title(f"Use {Path(path).name} for others")
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, justify="left", wraplength=620,
                  text=f"Creatures that share {r['name']}'s name or portrait. Click the ones that should get "
                       f"{Path(path).name} as well - click again to drop one.").pack(anchor="w", pady=(0, 6))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        lb = tk.Listbox(box, selectmode="multiple", width=86, height=min(18, max(4, len(cands))),
                        activestyle="none", exportselection=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
        lb.config(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for o in cands:
            cre = ", ".join(o.get("cre_list") or [])[:28]
            now = Path(props[rowkey(o)]).name if rowkey(o) in props else (o["large"] or o["small"] or "no portrait")
            lb.insert("end", f"{o['name'][:26]:28} {cre:30} {str(o.get('area') or '')[:22]:24} now: {now}")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))

        def use_them() -> None:
            picked = [cands[i] for i in lb.curselection()]
            for o in picked:
                props[rowkey(o)] = path
            if picked:
                persist_props()
                if view_var.get() == "gallery":
                    rebuild_keep_scroll()
                refresh_table_only()
                if callable(after):
                    after()                                          # type: ignore[operator]
                status.set(f"{Path(path).name} is now proposed for {len(picked) + 1} creature(s).")
            w.destroy()
        ttk.Button(row, text="Use for these", command=use_them).pack(side="left")
        ttk.Button(row, text="Pick all", command=lambda: lb.select_set(0, "end")).pack(side="left", padx=6)
        ttk.Button(row, text="Pick none", command=lambda: lb.select_clear(0, "end")).pack(side="left")
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right")

    def crop_store() -> dict:
        res = state["res"]
        return st.setdefault("crops", {}).setdefault(str(res["mod"]), {}) if res else {}   # type: ignore[index]

    def crop_for(r: dict) -> Optional[dict]:
        """The frame someone set by hand, if it still belongs to the picture proposed now. 'src' is the picture it
        was cut from (a photo, a .jpg...), 'out' the game portrait that was cut out of it."""
        c = crop_store().get(rowkey(r))
        pp = props.get(rowkey(r))
        if isinstance(c, dict) and pp and os.path.normcase(pp) in (os.path.normcase(c.get("src", "")),
                                                                   os.path.normcase(c.get("out", ""))):
            return c
        return None

    def full_image(path: str, fit: int = 520) -> Tuple[object, int, int, float]:
        """The picture as big as `fit` allows: (PhotoImage, its width, its height, shown size / real size)."""
        try:
            from PIL import Image, ImageTk
            im = Image.open(path).convert("RGB")
            k = min(1.0, fit / max(1, im.height), (fit * 4 // 3) / max(1, im.width))
            sm = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
            return ImageTk.PhotoImage(sm), im.width, im.height, k
        except Exception:  # noqa: BLE001
            pass
        if path.lower().endswith(".bmp"):
            data = open(path, "rb").read()
            w, h = abs(struct.unpack_from("<i", data, 18)[0]), abs(struct.unpack_from("<i", data, 22)[0])
            k = max(1, -(-h // fit))
            ppm = bmp_to_ppm(data, k)
            return (tk.PhotoImage(data=ppm) if ppm else None), w, h, 1.0 / k
        pi = tk.PhotoImage(file=path)                                # png / gif
        k = max(1, -(-pi.height() // fit))
        return (pi.subsample(k, k) if k > 1 else pi), pi.width(), pi.height(), 1.0 / k

    def frame_dialog(r: dict, parent: object = None, after: object = None) -> None:
        """Choose which part of the proposed picture becomes the portrait. Drag the box, or resize it."""
        par = parent or root
        was = crop_for(r)
        path = (was or {}).get("src") or props.get(rowkey(r))        # frame the original, not a portrait cut from it
        if not path or not os.path.isfile(path):
            path = props.get(rowkey(r))
        if not path or not os.path.isfile(path):
            messagebox.showinfo("Portrait Scout", "Propose a picture for this creature first.", parent=par)
            return
        sz = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 \
            else [169, 266]
        pw, ph = int(sz[0]), int(sz[1])
        try:
            img, sw, sh, k = full_image(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("Portrait Scout", f"That picture cannot be shown here ({e}).", parent=par)
            return
        if img is None:
            messagebox.showwarning("Portrait Scout", "That picture cannot be shown here (install Pillow for more "
                                                     "formats).", parent=par)
            return
        w = tk.Toplevel(par)                                         # type: ignore[arg-type]
        esc_closes(w)
        w.title(f"Frame the portrait - {r['name']}")
        top_ = ttk.Frame(w, padding=10)
        top_.pack(fill="x")
        ttk.Label(top_, justify="left", wraplength=640,
                  text=f"{Path(path).name}  -  {sw} x {sh}.  Drag the box where you want it; the part inside becomes "
                       f"the {pw} x {ph} portrait. The wheel, or the slider, makes it bigger or smaller.").pack(anchor="w")
        cv = tk.Canvas(w, width=img.width(), height=img.height(), highlightthickness=0)   # type: ignore[attr-defined]
        cv.pack(padx=10)
        cv.create_image(0, 0, image=img, anchor="nw")
        cv.image = img                                               # type: ignore[attr-defined]
        shown_w, shown_h = img.width(), img.height()                 # type: ignore[attr-defined]
        start = was
        if start:
            box = [start["box"][0] * k, start["box"][1] * k, start["box"][2] * k, start["box"][3] * k]
        else:
            ch_ = min(shown_h, shown_w * ph / pw)
            cw_ = ch_ * pw / ph
            box = [(shown_w - cw_) / 2, (shown_h - ch_) / 2, cw_, ch_]
        rect = cv.create_rectangle(0, 0, 0, 0, outline="#ffd000", width=2)
        dark = cv.create_rectangle(0, 0, 0, 0, outline="#303030", width=1)

        def draw() -> None:
            box[2] = max(24.0, min(box[2], shown_w))
            box[3] = box[2] * ph / pw
            if box[3] > shown_h:
                box[3] = float(shown_h)
                box[2] = box[3] * pw / ph
            box[0] = max(0.0, min(box[0], shown_w - box[2]))
            box[1] = max(0.0, min(box[1], shown_h - box[3]))
            cv.coords(rect, box[0], box[1], box[0] + box[2], box[1] + box[3])
            cv.coords(dark, box[0] - 1, box[1] - 1, box[0] + box[2] + 1, box[1] + box[3] + 1)
        drag = {"x": 0.0, "y": 0.0}

        def grab(e: object) -> None:
            drag["x"], drag["y"] = float(e.x), float(e.y)            # type: ignore[attr-defined]

        def move(e: object) -> None:
            box[0] += float(e.x) - drag["x"]                         # type: ignore[attr-defined]
            box[1] += float(e.y) - drag["y"]                         # type: ignore[attr-defined]
            grab(e)
            draw()

        def wheel(e: object) -> str:
            step = 1.1 if getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4 else 1 / 1.1
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            box[2] *= step
            box[3] = box[2] * ph / pw
            box[0], box[1] = cx - box[2] / 2, cy - box[3] / 2
            draw()
            return "break"
        cv.bind("<Button-1>", grab)
        cv.bind("<B1-Motion>", move)
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            cv.bind(ev, wheel)
        row = ttk.Frame(w, padding=10)
        row.pack(fill="x")

        def center() -> None:
            ch2 = min(shown_h, shown_w * ph / pw)
            box[0], box[1], box[2], box[3] = (shown_w - ch2 * pw / ph) / 2, (shown_h - ch2) / 2, ch2 * pw / ph, ch2
            draw()

        def keep() -> None:
            px = [int(box[0] / k), int(box[1] / k), int(box[2] / k), int(box[3] / k)]
            res_ = state["res"]
            out = Path(__file__).resolve().parent / "generated" / re.sub(r"[^A-Za-z0-9_.#!-]+", "_",
                                                                        str(res_["mod"]) if res_ else "proposed")
            try:                                                     # cut it out now, so the card shows the portrait
                out.mkdir(parents=True, exist_ok=True)
                stem_ = re.sub(r"[^A-Za-z0-9_.#!-]+", "_", f"{r['name']}_{Path(path).stem}")[:60].strip("_") or "portrait"
                bmp = str(out / f"{stem_}_{pw}x{ph}.bmp")
                to_portrait_bmp(path, bmp, pw, ph, None, tuple(px))   # type: ignore[arg-type]
            except Exception as e:  # noqa: BLE001
                messagebox.showwarning("Portrait Scout", f"The picture could not be cut out ({e}).\n\nThe framing is "
                                       "kept, and the mod will use it when it is made.", parent=w)
                bmp = ""
            crop_store()[rowkey(r)] = {"src": path, "box": px, "size": [pw, ph], "out": bmp}
            if bmp:
                props[rowkey(r)] = os.path.normpath(bmp)
                persist_props()
                state.setdefault("thumbs", {}).pop(bmp, None)        # type: ignore[union-attr]
                if view_var.get() == "gallery":
                    rebuild_keep_scroll()
                refresh_table_only()
            save_settings(st)
            status.set(f"{r['name']}: framed, and cut to {pw}x{ph}.")
            if callable(after):
                after()                                              # type: ignore[operator]
            w.destroy()

        def forget() -> None:
            crop_store().pop(rowkey(r), None)
            save_settings(st)
            status.set(f"{r['name']}: back to the middle of the picture.")
            if callable(after):
                after()                                              # type: ignore[operator]
            w.destroy()
        ttk.Button(row, text="Use this framing", command=keep).pack(side="left")
        ttk.Button(row, text="Whole picture", command=lambda: (box.__setitem__(2, float(shown_w)), draw())).pack(side="left", padx=6)
        ttk.Button(row, text="Middle", command=center).pack(side="left")
        ttk.Button(row, text="Forget my framing", command=forget).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right")
        draw()

    def clear_prop(r: dict) -> None:
        if props.pop(rowkey(r), None) is not None:
            persist_props()
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()

    def refresh_table_only() -> None:
        sel = tree.selection()
        refresh_rows = sorted_rows()
        tree.delete(*tree.get_children())
        for r in refresh_rows:
            pp = props.get(rowkey(r))
            tree.insert("", "end", values=table_values(r))

    def clear_all() -> None:
        if props and messagebox.askyesno("Portrait Scout", f"Remove all {len(props)} proposed portraits?"):
            props.clear()
            persist_props()
            rebuild_keep_scroll()
            refresh_table_only()

    def export_props() -> None:
        res = state["res"]
        rows = [r for r in sorted_rows(False) if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet. Use 'Propose portrait...' on a gallery card.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=f"{res['mod']}-portrait-todo.txt",   # type: ignore[index]
                                         filetypes=[("Text", "*.txt")])
        if not p:
            return
        sz_ = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 \
            else [169, 266]
        lines = [f"# Portrait changes for {res['mod']}  ({len(rows)} creature(s))", "#",                             # type: ignore[index]
                 "# Doing it by hand, for each line below:",
                 f"#   1. make a {int(sz_[0])}x{int(sz_[1])} 24-bit .bmp of the picture,",
                 "#      named as the second column says (a resref is at most 8 characters),",
                 "#   2. put it in the game's override folder,",
                 "#   3. open the creature in NearInfinity and write that name into both portrait",
                 "#      fields - Portrait (small) at 0x34 and Portrait (large) at 0x3c.",
                 "# 'Create mod...' in Portrait Scout does all of that for you, as an installable WeiDU mod.",
                 "#",
                 "# creature file | name to give the picture | creature | picture proposed | portrait it has now"]
        for r in rows:
            src_ = props[rowkey(r)]
            now = r["large"] or r["small"] or "none"
            warn = check_image(src_)
            framed = " (framed by hand)" if crop_for(r) else ""
            for cre in (r["cre_list"] or [""]):
                lines.append(f"{(cre + '.CRE') if cre else '(no creature file)'} | "
                             f"{(cre + '.bmp') if cre else '(name it yourself)'} | {r['name']} | "
                             f"{Path(src_).name}{framed} | {now}"
                             + (f"   [areas: {areas_text(r, 8)}]" if r.get("areas") else "")
                             + (f"   !! {'; '.join(warn)}" if warn else ""))
        Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        status.set(f"Exported {len(rows)} proposed change(s) to {p}")

    WORK_IN = "portrait-scout-work.json"                            # what a shared bundle is known by

    def work_text(mod_name: str, told: List[Tuple[dict, str, dict]], style: str) -> str:
        """The readable half of a bundle: what each picture was asked for, so the next person can match it."""
        out = [f"Portrait Scout {VERSION} - work on {mod_name}, {time.strftime('%Y-%m-%d')}",
               f"{len(told)} picture(s) proposed." + (f"  Style: {style}" if style else ""), "",
               "Open the .zip with 'Open shared work...' on the Proposals tab to carry on from it.",
               "This file is for reading; nothing is read back out of it.", ""]
        for r, nm, note in told:
            out.append(f"{r['name']}" + (f"   [{', '.join(r.get('cre_list') or [])}]" if r.get("cre_list") else ""))
            out.append(f"    picture : {nm}")
            if r.get("areas"):
                out.append(f"    areas   : {areas_text(r, 8)}")
            if note:
                if note.get("desc"):
                    out.append(f"    asked   : {note['desc']}")
                bits = [f"{k}={note[k]}" for k in ("age", "look", "race", "sex", "class", "count")
                        if note.get(k) not in (None, "", 0)]
                if bits:
                    out.append(f"    settings: {', '.join(bits)}")
            out.append("")
        return "\n".join(out)

    def share_work() -> None:
        """Everything another person needs to carry on: the pictures chosen, and what each one answers."""
        res = state["res"]
        rows = [r for r in all_rows() if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet, so there is nothing to hand over.")
            return
        mod_name = str(res["mod"])                                   # type: ignore[index]
        p = filedialog.asksaveasfilename(defaultextension=".zip", initialfile=f"{mod_name}-portrait-work.zip",
                                         filetypes=[("Zip", "*.zip")])
        if not p:
            return
        notes = (st.get("portrait_notes") or {}).get(mod_name, {})   # type: ignore[union-attr]
        fvars = (st.get("face_variant") or {}).get(mod_name, {})     # type: ignore[union-attr]
        crops = (st.get("crops") or {}).get(mod_name, {})            # type: ignore[union-attr]
        man: Dict[str, object] = {"tool": "Portrait Scout", "version": VERSION, "mod": mod_name,
                                  "made": time.strftime("%Y-%m-%d %H:%M"), "style": st.get("portrait_style", ""),
                                  "size": st.get("portrait_size") or [169, 266], "proposals": {}, "names": {},
                                  "cres": {}, "notes": {}, "variants": {}, "crops": {},
                                  "ignored": sorted(skip),
                                  "declone": [k for k in ((st.get("declone") or {}).get(mod_name) or []) if k]}
        told: List[Tuple[dict, str, dict]] = []
        gone: List[str] = []
        try:
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
                used: Set[str] = set()
                for r in rows:
                    k = rowkey(r)
                    src = Path(props[k])
                    if not src.is_file():
                        gone.append(r["name"])
                        continue
                    nm = re.sub(r"[^A-Za-z0-9_.#!-]+", "_", src.name)
                    while nm.lower() in used:
                        nm = "_" + nm
                    used.add(nm.lower())
                    z.write(str(src), "pictures/" + nm)
                    man["proposals"][k] = "pictures/" + nm            # type: ignore[index]
                    man["names"][k] = r["name"]                       # type: ignore[index]
                    man["cres"][k] = r.get("cre_list") or []          # type: ignore[index]
                    if notes.get(k):
                        man["notes"][k] = notes[k]                    # type: ignore[index]
                    if fvars.get(k):
                        man["variants"][k] = fvars[k]                 # type: ignore[index]
                    if isinstance(crops.get(k), dict):
                        man["crops"][k] = crops[k]                    # type: ignore[index]
                    told.append((r, nm, notes.get(k) or {}))
                z.writestr(WORK_IN, json.dumps(man, indent=1))
                z.writestr("how-these-were-made.txt", work_text(mod_name, told, str(st.get("portrait_style") or "")))
                root.config(cursor="watch")                          # a page to look the work over at a glance
                root.update_idletasks()
                try:
                    shown_ = [r for r in sorted_rows(False) if rowkey(r) in props]
                    z.writestr("preview.html", preview_html(mod_name, preview_items(shown_)))
                finally:
                    root.config(cursor="")
        except OSError as e:
            messagebox.showerror("Portrait Scout", f"Could not write {p}:\n{e}")
            return
        status.set(f"Shared {len(told)} picture(s) in {p}, with preview.html to look them over"
                   + (f" - {len(gone)} picture(s) are no longer on disk and were left out." if gone else ""))

    def open_work() -> None:
        """Take in somebody else's bundle: their pictures, and the creatures they chose them for."""
        res = state["res"]
        if not res:
            messagebox.showinfo("Portrait Scout", "Analyze a mod first, then open the work to add to it.")
            return
        p = filedialog.askopenfilename(filetypes=[("Portrait Scout work", "*.zip"), ("All files", "*.*")])
        if not p:
            return
        mod_name = str(res["mod"])                                   # type: ignore[index]
        try:
            with zipfile.ZipFile(p) as z:
                if WORK_IN not in z.namelist():
                    messagebox.showwarning("Portrait Scout", "That .zip is not Portrait Scout work - it has no "
                                           f"{WORK_IN} in it.")
                    return
                man = json.loads(z.read(WORK_IN).decode("utf-8"))
                theirs = {k: v for k, v in (man.get("proposals") or {}).items() if isinstance(v, str)}
                if not theirs:
                    messagebox.showinfo("Portrait Scout", "There are no proposed pictures in that bundle.")
                    return
                if str(man.get("mod") or "") != mod_name and not messagebox.askyesno(
                        "Portrait Scout", f"That work is for \"{man.get('mod')}\", and you are looking at "
                        f"\"{mod_name}\".\n\nTake it in anyway?"):
                    return
                where = remap_keys(list(theirs), all_rows())      # their grouping need not be ours
                mine = {k for k in theirs if where.get(k, k) in props}
                over = not messagebox.askyesno(                   # Yes keeps mine, so No is the one that replaces
                    "Portrait Scout", f"{len(mine)} creature(s) already have a picture you chose.\n\n"
                    "Yes: keep yours.   No: take theirs.") if mine else True
                try:                                             # "pg" was never in scope here, so this
                    pg_ = generator_module()                     # raised NameError every time and shared
                    made = str((pg_.load_settings() or {}).get(   # type: ignore[union-attr]
                        "output_folder") or "generated")         # work always landed in generated/
                except Exception:  # noqa: BLE001 - the generator may not be there at all
                    made = "generated"
                into = Path(__file__).resolve().parent / made / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", mod_name) / \
                    ("shared-" + re.sub(r"[^A-Za-z0-9_.#!-]+", "_", Path(p).stem))[:64]
                into.mkdir(parents=True, exist_ok=True)
                moved: Dict[str, str] = {}
                for k, rel in theirs.items():
                    if not rel.startswith("pictures/") or ".." in rel:
                        continue
                    if where.get(k, k) in props and not over:        # kept mine, so their picture is never
                        continue                                     # used: copying it only fills the disk
                    dst = into / Path(rel).name
                    with z.open(rel) as f_, open(dst, "wb") as g_:
                        shutil.copyfileobj(f_, g_)
                    moved[k] = os.path.normpath(str(dst))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            messagebox.showerror("Portrait Scout", f"Could not read that work:\n{e}")
            return
        added = kept = 0
        for k, path in moved.items():
            k2 = where.get(k, k)
            if k2 in props and not over:
                kept += 1
                continue
            props[k2] = path
            added += 1
        for k in (man.get("ignored") or []):
            if isinstance(k, str):
                skip.add(where.get(k, k))
        was = [x for x in ((st.get("declone") or {}).get(mod_name) or []) if x]   # type: ignore[union-attr]
        more = [where.get(k, k) for k in (man.get("declone") or []) if isinstance(k, str)]
        st.setdefault("declone", {})[mod_name] = list(dict.fromkeys(was + more))  # type: ignore[index]
        notes = st.setdefault("portrait_notes", {}).setdefault(mod_name, {})      # type: ignore[union-attr]
        for k, note in (man.get("notes") or {}).items():
            if isinstance(note, dict):
                notes.setdefault(where.get(k, k), note)
        fvars = st.setdefault("face_variant", {}).setdefault(mod_name, {})        # type: ignore[union-attr]
        for k, v in (man.get("variants") or {}).items():
            if isinstance(v, int):
                fvars.setdefault(where.get(k, k), v)
        crops = st.setdefault("crops", {}).setdefault(mod_name, {})               # type: ignore[union-attr]
        for k, c in (man.get("crops") or {}).items():                             # a frame set by hand knows which
            if not isinstance(c, dict) or k not in moved:                         # picture it was cut for, so the
                continue                                                          # path has to follow the copy here
            c = dict(c)
            for f_ in ("src", "out"):
                if isinstance(c.get(f_), str) and os.path.basename(c[f_].replace("\\", "/")) == Path(moved[k]).name:
                    c[f_] = moved[k]
            crops.setdefault(where.get(k, k), c)
        persist_props()
        st.setdefault("ignored", {})[mod_name] = sorted(skip)        # type: ignore[index]
        save_settings(st)
        refresh()
        status.set(f"Took in {added} picture(s) from {Path(p).name}"
                   + (f"; kept {kept} of your own." if kept else ".")
                   + f"  They are in {into}.")

    def ask_mod_folder(mod_name: str) -> Tuple[str, bool, bool]:
        """Which mod to write, and where. The default is a folder of its own under the project's 'mod' folder."""
        here = Path(__file__).resolve().parent
        out: Dict[str, str] = {}
        w = tk.Toplevel(root)
        esc_closes(w)
        w.title("Export as mod")
        w.transient(root)
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Mod name (this becomes the folder and the installer's name):").grid(row=0, column=0,
                                                                                               columnspan=3, sticky="w")
        name_v = tk.StringVar(value=str(st.get("mod_export_name") or
                                        (re.sub(r"[^A-Za-z0-9_#!-]+", "-", f"PS-{mod_name}").strip("-") or "PS-portraits")))
        ent = ttk.Entry(f, textvariable=name_v, width=42)
        ent.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        ent.focus_set()
        where_v = tk.StringVar()
        folder = {"dir": str(st.get("mod_export_parent") or (here / "mod"))}
        ttk.Label(f, textvariable=where_v, foreground="#555", wraplength=460, justify="left").grid(row=2, column=0,
                                                                                                  columnspan=3,
                                                                                                  sticky="w", pady=(6, 0))

        def tidy(nm: str) -> str:
            return re.sub(r"[^A-Za-z0-9_#!-]+", "-", nm).strip("-")  # the name is kept as typed, capitals and all

        def shown(*_a: object) -> None:
            where_v.set("Goes in:  " + os.path.join(folder["dir"], tidy(name_v.get())))
        name_v.trace_add("write", shown)
        shown()

        def elsewhere() -> None:
            d = filedialog.askdirectory(title="Folder to put the mod folder in", initialdir=folder["dir"], parent=w)
            if d:
                folder["dir"] = d
                shown()
        lent = [r_ for r_ in all_rows() if r_.get("copy_port") and rowkey(r_) not in props]
        lent_n, stock_n = sum(1 for r_ in lent if face_kept(r_)), sum(1 for r_ in lent if not face_kept(r_))
        see_v = tk.BooleanVar(value=bool(st.get("mod_export_as_seen", True)))
        ship_v = tk.BooleanVar(value=bool(st.get("mod_export_inherited_files", True)))
        gen_v = tk.BooleanVar(value=bool(st.get("mod_export_stock_faces", True)))
        if lent_n:
            ttk.Label(f, foreground="#1a5fb4", wraplength=460, justify="left",
                      text=f"{lent_n:,} creature(s) here are a copy of one of the game's own and wear its face"
                           + (" - the ones ticked \"Keep face\" in the gallery" if any(rowkey(r_) in kept
                                                                                        for r_ in lent) else "")
                           + ". They are written in too, so the copy keeps looking like it whatever order the "
                             "mods went in.").grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
            cb1_ = ttk.Checkbutton(f, variable=see_v, text="Everything I see", command=lambda: see_changed())
            cb1_.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
            tooltip(cb1_, "The mod puts these creatures back the way they look on your game, pictures and all - "
                          "it means both of the boxes below at once. Untick to set those two yourself: which "
                          "creatures are included, and whether their pictures go in the mod.")
            kids = ttk.Frame(f)
            kids.grid(row=7, column=0, columnspan=3, sticky="w", padx=(18, 0))
            if stock_n:
                judged = any(rowkey(r_) in kept for r_ in lent)      # nothing ticked by hand: as it always read
                cb3_ = ttk.Checkbutton(kids, variable=gen_v,
                                       text=f"The rest too ({stock_n:,})" if judged
                                       else f"Stock faces too ({stock_n:,})")
                cb3_.pack(anchor="w")
                tooltip(cb3_, f"Which creatures go in.\n\nAlways: the {lent_n:,} ticked \"Keep face\" - a "
                              "second Bassilus, who is Bassilus on anybody's game.\n\nThis box: the "
                              f"{stock_n:,} left unticked, which mostly copy a stock farmer, commoner or "
                              "merchant. That picture is whatever each install puts there, and need not even "
                              "be the same sex.")
            cb2_ = ttk.Checkbutton(kids, variable=ship_v, text="Carry the pictures")
            cb2_.pack(anchor="w")
            tooltip(cb2_, "What goes in the mod, for the creatures above.\n\nOff: the face is named only. It has "
                          "to be in that game already, and a creature whose face is missing there is left "
                          "alone.\n\nOn: the picture is copied in, so it shows without the mod it came from. "
                          "Credit that mod if you pass yours on.")

            def see_changed() -> None:
                """Ticked, it means both of the two below; unticked, they are there to set one by one."""
                if see_v.get():
                    gen_v.set(True)
                    ship_v.set(True)
                    kids.grid_remove()
                else:
                    kids.grid()
            see_changed()
            tooltip(cb2_, "Off: the face is named, not copied - it is already in the game, and a creature whose face is missing "
                    "there is left alone.\n\nOn: copied in, so the portraits show without the mod they came from. Credit it "
                    "if you pass yours on.")
        row = ttk.Frame(f)
        row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        def ok() -> None:
            nm = tidy(name_v.get())
            if not nm:
                messagebox.showwarning("Portrait Scout", "The mod needs a name.", parent=w)
                return
            out["dir"] = os.path.join(folder["dir"], nm)
            st["mod_export_name"], st["mod_export_parent"] = nm, folder["dir"]
            st["mod_export_as_seen"] = bool(see_v.get())
            st["mod_export_inherited_files"] = bool(ship_v.get())
            st["mod_export_stock_faces"] = bool(gen_v.get())
            save_settings(st)
            w.destroy()
        ttk.Button(row, text="Export", command=ok).pack(side="left")
        ttk.Button(row, text="Somewhere else...", command=elsewhere).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="left")
        ttk.Label(f, foreground="#888", wraplength=460, justify="left", padding=(0, 10, 0, 0),
                  text="Exporting again with the same name updates that mod; a different name starts another one. "
                       "Proposals stay where they are either way.").grid(row=4, column=0, columnspan=3, sticky="w")
        w.grab_set()
        root.wait_window(w)
        return out.get("dir", ""), bool(ship_v.get()), bool(gen_v.get())

    def preview_items(rows_: List[dict]) -> List[dict]:
        """The chosen portraits at game size as JPEG, with names and what each is, for preview_html."""
        pg_ = generator_module()
        sz_ = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 else [169, 266]
        out_: List[dict] = []
        tmp_ = Path(tempfile.mkdtemp(prefix="ps_preview_"))
        try:
            from PIL import Image as _I
            for r_ in rows_:
                src_ = str(props.get(rowkey(r_)) or "")
                if not src_ or not os.path.isfile(src_):
                    continue
                dst_ = tmp_ / "p.bmp"
                try:
                    inf_ = image_info(src_)
                    box_ = (crop_for(r_) or {}).get("box")
                    if not box_ and inf_ and (inf_["w"], inf_["h"]) == (int(sz_[0]), int(sz_[1])):
                        shutil.copyfile(src_, dst_)
                    else:
                        to_portrait_bmp(src_, str(dst_), int(sz_[0]), int(sz_[1]), getattr(pg_, "crop_box", None),
                                        tuple(box_) if box_ else None)   # type: ignore[arg-type]
                    buf_ = io.BytesIO()
                    _I.open(dst_).convert("RGB").save(buf_, "JPEG", quality=90)
                except Exception:  # noqa: BLE001 - one unreadable picture is left out
                    continue
                kind_ = " ".join(x for x in (str(r_.get("race") or ""), shown_sex(r_.get("sex"))) if x)
                out_.append({"name": r_["name"], "kind": kind_, "jpeg": buf_.getvalue(),
                             "cre": ", ".join(r_.get("cre_list") or [])})
        finally:
            shutil.rmtree(tmp_, ignore_errors=True)
        return out_

    def save_preview() -> None:
        res = state["res"]
        rows = [r for r in sorted_rows(False) if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet.")
            return
        p_ = filedialog.asksaveasfilename(parent=root, title="Save the preview page",
                                          initialfile=f"{_safe_name(str(res['mod']))}-portraits.html",   # type: ignore[index]
                                          defaultextension=".html", filetypes=[("Web page", "*.html")])
        if not p_:
            return
        root.config(cursor="watch")
        root.update_idletasks()
        try:
            items = preview_items(rows)
            Path(p_).write_text(preview_html(str(res["mod"]), items), encoding="utf-8")   # type: ignore[index]
        except OSError as e_:
            messagebox.showwarning("Portrait Scout", f"Could not save the page:\n\n{e_}")
            return
        finally:
            root.config(cursor="")
        status.set(f"Saved a preview page of {len(items)} portrait(s): {p_}")
        try:
            os.startfile(p_)                                         # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    def export_as_mod() -> None:
        res = state["res"]
        rows = [r for r in sorted_rows(False) if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet. Use 'Propose portrait...' on a gallery card first.")
            return
        d, ship_lent, take_stock = ask_mod_folder(str(res["mod"]))   # type: ignore[index]
        if not d:
            return
        pg = generator_module()
        sz = st.get("portrait_size") if isinstance(st.get("portrait_size"), list) and len(st.get("portrait_size")) == 2 else [169, 266]

        def convert(src: str, dst: str, box: Optional[List[int]] = None) -> None:
            inf = image_info(src)
            if not box and inf and inf["fmt"] == "BMP" and inf["bpp"] in (8, 24) \
                    and (inf["w"], inf["h"]) == (int(sz[0]), int(sz[1])):
                shutil.copyfile(src, dst)                            # already a game portrait of the right size
                return
            to_portrait_bmp(src, dst, int(sz[0]), int(sz[1]), getattr(pg, "crop_box", None),
                            tuple(box) if box else None)             # type: ignore[arg-type]
        try:
            os.makedirs(d, exist_ok=True)
            entries = [{"name": r["name"], "cres": r["cre_list"], "image": props[rowkey(r)],
                        "areas": areas_text(r), "box": (crop_for(r) or {}).get("box")} for r in rows]
            lent_rows = [r for r in sorted_rows(False)               # a copy of a creature wears its face
                         if rowkey(r) not in props and r.get("copy_port") and r.get("cre_list")
                         and (face_kept(r) or take_stock)]            # kept by hand, or the whole lot if asked
            stage = (Path(__file__).resolve().parent / "generated"
                     / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", str(res["mod"])) / "inherited")   # type: ignore[index]
            kept_face: Dict[str, str] = {}

            def face_file(face: str) -> str:
                """The inherited picture, pulled out of the game once, when it is to be carried along."""
                if face not in kept_face:
                    dst = stage / (face.upper() + ".bmp")
                    if not dst.is_file():
                        data = res["game"].get(face, T_BMP)           # type: ignore[index]
                        if not data:
                            kept_face[face] = ""
                            return ""
                        stage.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(data)
                    kept_face[face] = str(dst)
                return kept_face[face]
            for r in lent_rows:
                face = str(r["copy_port"])
                e_ = {"name": r["name"], "cres": r["cre_list"], "areas": areas_text(r), "box": None, "image": ""}
                if ship_lent:
                    e_["image"] = face_file(face)
                    if not e_["image"]:
                        continue                                      # the picture is not readable: leave it out
                else:
                    e_["point_at"] = face                             # already in the game: just point at it
                entries.append(e_)
            info = export_mod(d, str(res["mod"]), entries, convert, game_var.get())
            try:                                                     # a page to look the set over, in the mod
                Path(d, "preview.html").write_text(preview_html(str(res["mod"]), preview_items(rows)), encoding="utf-8")   # type: ignore[index]
            except OSError:
                pass
        except (OSError, ValueError) as e:
            messagebox.showwarning("Portrait Scout", f"Could not export the mod:\n\n{e}")
            return
        name = Path(d).name
        carried = info.get("carried", info["portraits"])
        pointed = info["portraits"] - carried
        msg = (f"Exported {info['portraits']} portrait(s) for {info['creatures']} creature file(s) of {res['mod']}"
               + (f", {pointed} of them already in the game and only pointed at" if pointed > 0 else "") + ".\n\n"   # type: ignore[index]
               f"{d}\n  setup-{name}.tp2   the installer\n  readme.txt, portraits.txt   what is in it\n"
               + ("  preview.html   the portraits, to look over in a browser\n" if Path(d, "preview.html").is_file() else "") +
               f"  portraits\\...   the pictures\n\n"
               f"The mod now has {info['sets']} set(s). Each is offered twice in the installer: replace the portraits "
               "creatures already have, or only fill in missing ones.\n\nTo install: copy the folder and "
               + (f"setup-{name}.exe (copied next to it) " if info["exe"] else
                  f"a WeiDU program renamed setup-{name}.exe ")
               + "into the game folder, and run it.")
        if info["skipped"]:
            msg += f"\n\nLeft out ({len(info['skipped'])}):\n" + "\n".join(info["skipped"][:12]) \
                + ("\n..." if len(info["skipped"]) > 12 else "")
        messagebox.showinfo("Portrait Scout", msg)
        status.set(f"Exported the mod to {d}")

    export_btn.config(command=export_props)
    mod_btn.config(command=export_as_mod)
    prev_btn.config(command=save_preview)
    clear_btn.config(command=clear_all)

    def build_gallery(rows: List[dict]) -> None:
        if st.get("gallery_mode", "plain") == "auto" and view_var.get() == "gallery":
            auto_fill_soon()                                         # the Auto view is what shows: it is the one to
            return                                                   # draw (Plain gallery draws the cards when chosen)
        keep_vars.clear()                                            # the cards are about to be thrown away
        loading_var.set("Loading...")
        root.update_idletasks()
        try:
            _build_gallery(rows)
        except Exception as e:  # noqa: BLE001 - a half-drawn gallery looks like a wrong analysis; say what happened
            traceback.print_exc()
            messagebox.showwarning("Portrait Scout", f"The gallery could not be drawn in full:\n\n"
                                   f"{e.__class__.__name__}: {e}\n\nWhat you see may be only the first few "
                                   "creatures. The table tab is unaffected.")
        finally:
            loading_var.set("")
            root.update_idletasks()
            fix_scrollregion()

    def _build_gallery(rows: List[dict]) -> None:
        state["resize_job"] = None
        for w in ginner.winfo_children():
            w.destroy()
        res = state["res"]
        if not res:
            return
        game: Game = res["game"]                                     # type: ignore[index]
        imgs: Dict[str, object] = state["imgs"]                      # type: ignore[assignment]
        per_row = per_row_now()
        state["per_row"] = per_row
        for c in range(per_row + 1):
            ginner.columnconfigure(c, weight=1 if c < per_row else 0, uniform="gal" if c < per_row else "")
        cards: List[Tuple[object, dict]] = []
        PAGE = 200          # keeps the canvas well under the 32,767 px limit of Windows widgets
        npages = max(1, (len(rows) + PAGE - 1) // PAGE)
        page = min(int(state["page"]), npages - 1)                   # type: ignore[call-overload]
        state["page"] = page
        first = page * PAGE

        for w in pagebar.winfo_children():
            w.destroy()

        def nav_bar(grid_row: Optional[int]) -> None:
            if npages <= 1:
                return
            if grid_row is None:
                bar = pagebar
            else:
                bar = tk.Frame(ginner, bg=PANEL)
                bar.grid(row=grid_row, column=0, columnspan=per_row, pady=6)

            bar.config(highlightthickness=2, highlightbackground="#8a8f98", highlightcolor="#8a8f98", padx=4, pady=2)

            def go(d: int) -> None:
                state["page"] = page + d
                build_gallery(sorted_rows())
                gcanvas.yview_moveto(0)
            tk.Button(bar, text=f"< Previous {PAGE}", command=lambda: go(-1),
                      state="normal" if page > 0 else "disabled").pack(side="left", padx=6)
            def jump(_e: object = None, v: Optional[tk.StringVar] = None) -> None:
                try:
                    n = int((v.get() if v else "").strip()) - 1          # type: ignore[union-attr]
                except ValueError:
                    return
                n = max(0, min(npages - 1, n))
                if n != page:
                    state["page"] = n
                    build_gallery(sorted_rows())
                    gcanvas.yview_moveto(0)
            bg = bar.cget("bg")
            tk.Label(bar, text="Page", bg=bg, fg="#555").pack(side="left", padx=(6, 2))
            pv = tk.StringVar(value=str(page + 1))
            pe = tk.Entry(bar, textvariable=pv, width=4, justify="center")
            pe.pack(side="left")
            pe.bind("<Return>", lambda e, v=pv: jump(e, v))
            pe.bind("<KP_Enter>", lambda e, v=pv: jump(e, v))
            tk.Label(bar, text=f"of {npages}  ({first + 1}-{min(first + PAGE, len(rows))} of {len(rows)})",
                     bg=bg, fg="#555").pack(side="left", padx=(2, 6))
            tk.Button(bar, text=f"Next {PAGE} >", command=lambda: go(1),
                      state="normal" if page < npages - 1 else "disabled").pack(side="left", padx=6)

        nav_bar(None)
        shown_rows = rows[first:first + PAGE]
        for i, r in enumerate(shown_rows):
            prop = props.get(rowkey(r))
            card = tk.Frame(ginner, bg=CARD, bd=1, relief="solid", padx=4, pady=4, highlightthickness=4,
                            highlightbackground="#2e9e4f" if prop else CARD, highlightcolor="#2e9e4f" if prop else CARD)
            card.grid(row=i // per_row, column=i % per_row, padx=4, pady=4, sticky="nsew")   # a row is as tall as its tallest card
            cards.append((card, r))
            if pick_var.get():                                       # select mode: a box in the corner of the card
                kx = rowkey(r)
                cv_ = tk.BooleanVar(value=kx in checked)
                cb_ = tk.Checkbutton(card, variable=cv_, bg=CARD, activebackground=CARD, highlightthickness=0,
                                     command=lambda k=kx, v=cv_: (checked.add(k) if v.get() else checked.discard(k),
                                                                  upd_pick_count()))
                cb_._ps_var = cv_            # keep it alive: a collected tk variable takes the card down with it
                cb_.pack(anchor="w")
            img = load_thumb(prop) if prop else None
            shown_field = "prop" if img else ""
            src_mode = hasattr(game, "get_mod")                       # the mod's own files were read
            mod_idx = mod_bmp_index() if modpic_var.get() and not src_mode else {}
            mod_only = bool(modpic_var.get()) and (src_mode or bool(mod_idx))
            mod_file = ""
            for field in ("small", "large", "copy"):
                if img:
                    break
                if field == "copy":                                  # wears none of its own: show the face of the
                    nm = "" if mod_only else str(r.get("copy_port") or "")   # creature it was copied from
                elif mod_only and src_mode:
                    nm = r.get("mod_ports", {}).get(field, "")
                else:
                    nm = r[field].split("/")[0] if r[field] else ""
                    if mod_only:                                     # installed mode: only pictures the mod ships
                        ports = state.get("mod_cre_ports") or {}
                        own = [(ports.get(c.upper()) or ("", ""))[0 if field == "small" else 1]
                               for c in (r.get("cre_list") or [])]   # what the mod's own creature file asks for
                        nm = next((x for x in own + [nm] if x and x.upper() in mod_idx), "")
                if not nm:
                    continue
                ikey = ("mod:" if mod_only else "") + nm
                if ikey not in imgs:
                    if mod_only and not src_mode:
                        try:
                            data = open(mod_idx[nm.upper()], "rb").read()
                        except OSError:
                            data = None
                    else:
                        data = game.get_mod(nm, T_BMP) if mod_only else game.get(nm, T_BMP)   # type: ignore[attr-defined]
                    imgs[ikey] = thumb_from_bytes(data) if data else None
                img = imgs[ikey]
                if img:
                    shown_field = field
                    mod_file = mod_idx.get(nm.upper(), "") if mod_only and not src_mode else ""
            if prop and not load_thumb(prop):                    # say which it is: a picture that has been
                lost_ = not os.path.isfile(prop)                  # deleted since reads nothing like one Tk cannot show
                tk.Label(card, text=(f"{Path(prop).name}\n\nthis picture is no longer\non the disk" if lost_ else
                                     f"{Path(prop).name}\n(preview needs PNG/GIF/BMP,\nor install Pillow)"),
                         bg="#f0d6d6" if lost_ else "#cfe8d6", width=18, height=6).pack()
            elif img:
                il = tk.Label(card, image=img, bg=CARD)
                il.pack()
                il.bind("<Button-3>", lambda e, rr=r: image_menu(e, rr, root))
            elif mod_only and r.get("copy_port") and face_kept(r) and \
                    (inh_ := inherited_thumb(game, imgs, str(r.get("copy_port")), thumb_from_bytes)) is not None:
                il = tk.Label(card, image=inh_, bg=CARD)             # a copy keeping the face it was copied with:
                il.pack()                                            # that face is the one it wears, mod or not
                il.bind("<Button-3>", lambda e, rr=r: image_menu(e, rr, root))
                tk.Label(card, text=f"inherits {r.get('copy_port')}", bg=CARD, fg="#1a7f37",
                         font=("TkDefaultFont", 8, "bold")).pack()
            elif mod_only and r.get("patched") and (r.get("large_name") or r.get("small_name")) and \
                    (own_ := inherited_thumb(game, imgs, str(r.get("large_name") or r.get("small_name")),
                                             thumb_from_bytes)) is not None:
                il = tk.Label(card, image=own_, bg=CARD)             # one of the game's own creatures the mod only
                il.pack()                                            # adds lines to: it keeps the game's portrait
                il.bind("<Button-3>", lambda e, rr=r: image_menu(e, rr, root))
                tk.Label(card, text=f"game's own ({r.get('large_name') or r.get('small_name')})", bg=CARD,
                         fg="#1a7f37", font=("TkDefaultFont", 8, "bold")).pack()
            else:
                tk.Label(card, text="no portrait\nfrom this mod" if mod_only else
                         "no portrait" if not (r["large"] or r["small"]) else "(file not found)",
                         width=14, height=6, bg="#d4d7dc").pack()
            sz = (prop_dims(prop) if shown_field == "prop" else r.get(f"{shown_field}_size", "")) if shown_field else ""
            if mod_only and shown_field in ("small", "large"):
                sz = bmp_file_dims(mod_file) if mod_file else r.get("mod_sizes", {}).get(shown_field, "")
            if shown_field == "copy" or (r.get("copy_worn") and shown_field in ("small", "large")):
                tk.Label(card, text=f"{r.get('copy_port')} - borrowed", bg=CARD, fg="#1a5fb4",
                         font=("TkDefaultFont", 8, "bold")).pack()   # not its face: the one it was copied from
            if r.get("copy_port") and not prop:                      # is this borrowed face the right one?
                kv_ = tk.BooleanVar(value=face_kept(r))
                kc_ = tk.Checkbutton(card, text="Keep face", variable=kv_, bg=CARD, activebackground=CARD,
                                     font=("TkDefaultFont", 8), command=lambda rr=r, v=kv_: set_kept(rr, v.get()))
                kc_.pack()
                keep_vars.append(kv_)                                # Tk drops a variable nothing holds
                tooltip(kc_, f"Ticked: {r['name']} keeps {r.get('copy_port')}, and Create mod writes that face "
                             f"into it, so it survives whatever order the mods go in. Unticked: it wants a face "
                             "of its own and nothing is written in.\n\nThe tick starts on Analyze's guess, which "
                             "only knows whether the name matched or the creature was copied once.")
            elif sz:
                tk.Label(card, text=sz, bg=CARD, fg="#888", font=("TkDefaultFont", 8)).pack()
            nl = tk.Label(card, text=r["name"], bg=CARD, wraplength=CARD_W - 30, justify="center", cursor="hand2",
                          font=("TkDefaultFont", 9, "bold"))
            nl.pack()
            nl.bind("<Button-1>", lambda e, n=r["name"]: copy_text(n))                    # click the name: copy it
            nl.bind("<Button-3>", lambda e, rr=r: row_menu(e, rr, root))                  # right click: ignore it
            if rowkey(r) in skip:
                nl.config(fg="#999")
            tk.Label(card, text=f"{r['lines']} lines", bg=CARD, fg="#666").pack()
            if prop:                                                 # the green border says it is a proposal already
                tooltip(card, f"Proposed: {Path(prop).name}" + (f"  ({prop_dims(prop)})" if prop_dims(prop) else "")
                        + "\n\nDouble-click the card to see it, frame it, or clear it.")
            _st: List[str] = []                                  # what this card is, in a line
            if prop:                                                 # the borrowed face and "no portrait" are
                _st.append("missing file" if not os.path.isfile(prop) else "proposed")   # already said above
            if rowkey(r) in skip:
                _st.append("ignored")
            sub = " ".join(x for x in (r["race"], shown_sex(r["sex"])) if x)
            if _st:
                tk.Label(card, text=" · ".join(_st), bg=CARD, fg="#a35a00",
                         font=("TkDefaultFont", 8, "bold")).pack()
            if sub:
                tk.Label(card, text=sub, bg=CARD, fg="#666", wraplength=CARD_W - 30, justify="center").pack()
            for cn in r["cre_list"][:6]:
                cl = tk.Label(card, text=cn, bg=CARD, fg="#1a5fb4", cursor="hand2",
                              font=("TkDefaultFont", 8, "bold underline" if cn in r["cre_known"] else "underline"))
                cl.pack()
                cl._copy_label = True                                                      # type: ignore[attr-defined]
                cl.bind("<Button-1>", lambda e, n=cn: copy_text(n))
                cl.bind("<Double-Button-1>", lambda e: "break")
            tk.Label(card, text="double-click: see lines", bg=CARD, fg="#aaa").pack(side="bottom")
            brow = tk.Frame(card, bg=CARD)
            brow.pack(side="bottom", pady=2)
            b1 = tk.Button(brow, text="Change proposal..." if prop else "Propose portrait...", font=("TkDefaultFont", 8),
                           command=lambda rr=r: open_propose(rr))
            b1.pack(side="left")
            b1._copy_label = True                                                          # type: ignore[attr-defined]
            tooltip(b1, "Make a picture, take one of your own or one from the pool, frame it, and propose it.")
            dl_g, dl_gw = declone_label(r)
            if dl_g:
                db_ = tk.Button(brow, text=dl_g, font=("TkDefaultFont", 8), command=lambda rr=r: toggle_declone(rr))
                db_.pack(side="left", padx=(2, 0))
                db_._copy_label = True                                                     # type: ignore[attr-defined]
                tooltip(db_, dl_gw)
            if prop:
                b2 = tk.Button(brow, text="x", font=("TkDefaultFont", 8), command=lambda rr=r: clear_prop(rr))
                b2.pack(side="left", padx=2)
                b2._copy_label = True                                                      # type: ignore[attr-defined]

        def bind_all_children(w: object, row: dict) -> None:
            if getattr(w, "_copy_label", False):
                return                                                                     # CRE name labels only copy
            w.bind("<Double-Button-1>", lambda e, rr=row: show_lines(rr))                # type: ignore[attr-defined]
            for ch in w.winfo_children():                                                  # type: ignore[attr-defined]
                bind_all_children(ch, row)

        for card, row in cards:
            bind_all_children(card, row)
        nav_bar((len(shown_rows) - 1) // per_row + 1)

    SPRITE_DIR = Path(__file__).resolve().parent / "generated" / "_sprites"

    def find_java() -> str:
        """Java, as Near Infinity uses it: on the PATH, or in one of the usual install folders."""
        for nm_ in ("java", "javaw"):
            got_ = shutil.which(nm_)
            if got_:
                return got_
        import glob as _gl
        for base_ in (os.environ.get("JAVA_HOME", ""), r"C:\Program Files\Java\*", r"C:\Program Files\Eclipse Adoptium\*",
                      r"C:\Program Files\Microsoft\jdk*", r"C:\Program Files\Zulu\*", r"C:\Program Files\BellSoft\*"):
            for hit_ in sorted(_gl.glob(os.path.join(base_, "bin", "java.exe")) if base_ else [], reverse=True):
                return hit_
        return ""

    def sprite_tools() -> Tuple[str, str, str]:
        """(java, Near Infinity's jar, our helper jar), "" for whatever is missing."""
        here_ = Path(__file__).resolve().parent
        ni_ = str(st.get("ni_jar") or here_ / "NearInfinity.jar")
        helper_ = here_ / "generator" / "spriteshot.jar"
        return (find_java(), ni_ if os.path.isfile(ni_) else "", str(helper_) if helper_.is_file() else "")

    def sprite_make(cres: List[str], done_: Callable[[Set[str], str], None]) -> None:
        """Draw the creatures' in-game sprites (Near Infinity's own decoder, standing, first direction) into
        generated/_sprites, in the background. done_(drawn, problem) is called on the main thread."""
        java_, ni_, helper_ = sprite_tools()
        game_ = game_var.get().strip()
        if not (java_ and ni_ and helper_ and game_):
            done_(set(), "needs " + ", ".join(x for x, ok in (("Java", java_), ("NearInfinity.jar in the Portrait Scout "
                                                                                 "folder", ni_), ("generator/spriteshot.jar",
                                                                                                  helper_), ("the game folder", game_)) if not ok))
            return

        def work() -> None:
            got_: Set[str] = set()
            why_ = ""
            try:
                SPRITE_DIR.mkdir(parents=True, exist_ok=True)
                pr_ = subprocess.run([java_, "-cp", os.pathsep.join((ni_, helper_)), "SpriteShot", game_,
                                      str(SPRITE_DIR), "-1"] + [c.upper() for c in cres],   # every direction
                                     capture_output=True, text=True, timeout=300,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for ln_ in (pr_.stdout or "").splitlines():
                    if ln_.startswith("OK "):
                        got_.add(ln_[3:].split()[0].strip())
                    elif ln_.startswith("FAIL"):
                        why_ = ln_[5:].strip()
                if not got_ and not why_:
                    why_ = ((pr_.stderr or "").strip().splitlines() or ["Near Infinity did not answer"])[-1][:200]
            except Exception as e_:  # noqa: BLE001
                why_ = str(e_)[:200]
            root.after(0, done_, got_, why_)
        threading.Thread(target=work, daemon=True).start()

    def sprite_files(cre_: str) -> List[Path]:
        """The creature's sprite, one picture per direction (NAME_c0.png ...), in order."""
        got_ = []
        try:                                                         # drawn by an older helper (random colors
            new_ = (Path(__file__).resolve().parent / "generator" / "spriteshot.jar").stat().st_mtime   # rolled
        except OSError:                                              # by Near Infinity, not as described): again
            new_ = 0.0
        for q_ in SPRITE_DIR.glob(f"{cre_}_c*.png"):
            m_ = re.match(r".*_c(\d+)$", q_.stem)
            try:
                if q_.stat().st_mtime < new_:
                    continue
            except OSError:
                continue
            if m_:
                got_.append((int(m_.group(1)), q_))
        return [q_ for _n, q_ in sorted(got_)]

    def sprite_big(pth: Path, parent: object) -> None:
        """A sprite at a size you can see the pixels of - twice over for a small one, as it is for a dragon - with
        scroll bars when it is bigger than the screen."""
        try:
            from PIL import Image, ImageTk
            im_ = Image.open(pth).convert("RGBA")
        except Exception:  # noqa: BLE001
            return
        k_ = 3 if max(im_.size) < 150 else 2 if max(im_.size) < 300 else 1
        im_ = im_.resize((im_.width * k_, im_.height * k_), Image.NEAREST)
        v_ = tk.Toplevel(parent)                                     # type: ignore[arg-type]
        v_.title(f"{pth.stem.split('_c')[0]} - sprite")
        esc_closes(v_)
        sw_, sh_ = min(im_.width, root.winfo_screenwidth() - 120), min(im_.height, root.winfo_screenheight() - 160)
        cv_ = tk.Canvas(v_, width=sw_, height=sh_, bg="#bfc3c9", highlightthickness=0,
                        scrollregion=(0, 0, im_.width, im_.height))
        if im_.width > sw_:
            hs_ = ttk.Scrollbar(v_, orient="horizontal", command=cv_.xview)
            hs_.pack(side="bottom", fill="x")
            cv_.configure(xscrollcommand=hs_.set)
        if im_.height > sh_:
            vs_ = ttk.Scrollbar(v_, orient="vertical", command=cv_.yview)
            vs_.pack(side="right", fill="y")
            cv_.configure(yscrollcommand=vs_.set)
        cv_.pack(fill="both", expand=True)
        ph_ = ImageTk.PhotoImage(im_)
        cv_.create_image(0, 0, image=ph_, anchor="nw")
        cv_.image = ph_                                              # type: ignore[attr-defined]
        cv_.bind("<Button-1>", lambda e: v_.destroy())

    def sprite_show(r: dict, box: tk.Frame, again: bool = False, size: Tuple[int, int] = (200, 230),
                    side: bool = False) -> None:
        """The creature as it walks about in the game, beside its portrait: drawn once (every direction), then
        kept. The arrows turn it; a click shows it bigger. Kept to the box's size, so a dragon fits too."""
        cre_ = str((r.get("cre_list") or [""])[0]).upper()
        if not cre_:
            return
        for w_ in box.winfo_children():
            w_.destroy()
        nav_ = ttk.Frame(box)                                        # the arrows stay put at the foot (or the
        nav_.pack(side="right" if side else "bottom", padx=(4, 0) if side else 0, pady=0 if side else (2, 0))
        rnd_ = str((r.get("colors") or {}).get("_random") or "")
        BOXW, BOXH = size
        if rnd_:                                                     # rolled by the game when it makes the creature:
            slots_ = [s_.split(" (")[0] for s_ in rnd_.split(", ")]  # listed, not hidden in a tooltip
            rl_ = tk.Label(box, text="Random in game: " + ", ".join(slots_), fg="#b5651d",
                           font=("TkDefaultFont", 8, "bold"), wraplength=max(80, BOXW - 10), justify="center")
            rl_.pack(side="bottom", fill="x", pady=(2, 0))
            tooltip(rl_, f"The game rolls these each time it makes the creature: {rnd_}. Shown in one of them, "
                         "the same one the portrait is described in.")
            BOXH -= 30                                           # room for the list
        lab = tk.Label(box, bd=0, fg="#888", justify="center", cursor="hand2")   # side) whatever its size
        lab.pack(side="left" if side else "top", expand=True)
        at_ = {"i": 0}

        def put() -> None:
            files_ = sprite_files(cre_)
            if not files_:
                return
            at_["i"] %= len(files_)
            pth_ = files_[at_["i"]]
            try:
                from PIL import Image, ImageTk
                im_ = Image.open(pth_).convert("RGBA")
                k_ = min(3.0, BOXW / max(1, im_.width), BOXH / max(1, im_.height))
                if k_ >= 1:                                          # pixel art: whole steps, no blur
                    k_ = int(k_)
                    im_ = im_.resize((im_.width * k_, im_.height * k_), Image.NEAREST)
                else:                                                # too big for the box (a dragon): shrunk
                    im_ = im_.resize((max(1, int(im_.width * k_)), max(1, int(im_.height * k_))), Image.LANCZOS)
                ph_ = ImageTk.PhotoImage(im_)
                lab.config(image=ph_, text="")
                lab.image = ph_                                      # type: ignore[attr-defined]
                cyc_.set(f"{at_['i'] + 1}/{len(files_)}")
                lab.bind("<Button-1>", lambda e, q=pth_: sprite_big(q, box.winfo_toplevel()))
            except Exception:  # noqa: BLE001
                lab.config(text="(sprite\nunreadable)")

        def turn(by: int) -> None:
            at_["i"] += by
            put()
        cyc_ = tk.StringVar(value="")
        ttk.Button(nav_, text="\u25c0", width=3, command=lambda: turn(-1)).pack(side="top" if side else "left")
        ttk.Label(nav_, textvariable=cyc_, width=6, anchor="center").pack(side="top" if side else "left")
        ttk.Button(nav_, text="\u25b6", width=3, command=lambda: turn(1)).pack(side="top" if side else "left")
        lab.bind("<Button-3>", lambda e: sprite_show(r, box, again=True, size=size, side=side))
        tooltip(lab, "Its sprite in the game, in its own colors (drawn by Near Infinity). The arrows turn it, "
                     "a click shows it bigger, right-click draws it again.")
        if sprite_files(cre_) and not again:
            put()
            return
        lab.config(image="", text="drawing\nthe sprite...")

        def done(got_: Set[str], why_: str) -> None:
            try:
                if not lab.winfo_exists():
                    return
            except tk.TclError:
                return
            if cre_ in got_ and sprite_files(cre_):
                put()
                if not state.get("sprites_all"):                     # then the rest of the mod's, in one go in
                    state["sprites_all"] = True                      # the background: Near Infinity takes a while
                    rest_ = sorted({str((o.get("cre_list") or [""])[0]).upper() for o in all_rows()} - {""})
                    rest_ = [c for c in rest_ if not sprite_files(c)]
                    if rest_:                                        # to open the game, so once, not per creature
                        sprite_make(rest_, lambda g_, w_: status.set(f"Sprites drawn for {len(g_)} creature(s)."))
            else:
                lab.config(text="no sprite")
                tooltip(lab, f"Could not draw {cre_}'s sprite: {why_ or 'unknown'}.")
        sprite_make([cre_], done)

    def show_lines(r: dict) -> None:
        res = state["res"]
        win = tk.Toplevel(root)
        esc_closes(win)
        win.title(f"{r['name']} - {r['lines']} line(s)")
        win.geometry("900x640")
        win.minsize(680, 320)
        head = ttk.Frame(win, padding=8)
        head.pack(fill="x")
        img = None
        if res:
            game: Game = res["game"]                                 # type: ignore[index]
            for field in ("small", "large", "copy"):                 # ...and the face it was copied from,
                nm = (str(r.get("copy_port") or "") if field == "copy" else   # which is what the gallery shows
                      (r[field].split("/")[0] if r[field] else ""))
                data = game.get(nm, T_BMP) if nm else None
                img = thumb_from_bytes(data) if data else None       # the same box as a proposed picture
                if img:
                    break
        lb = tk.Label(head, bd=0, highlightthickness=4, cursor="hand2")   # the proposal (green border) or the
        lb.pack(side="left", padx=(0, 10))                           # portrait the creature wears now
        lb.bind("<Button-3>", lambda e: image_menu(e, r, win))
        spr_ = tk.Frame(head, width=210, height=268)                 # and the creature as it walks about - a box
        spr_.pack(side="left", padx=(0, 12), anchor="n")             # of its own size, so turning it does not
        spr_.pack_propagate(False)                                   # move the words beside it
        sprite_show(r, spr_)

        def big_view(_e: object = None) -> None:
            """Click the picture: it opens as large as the screen allows, which for a 169x266 game portrait
            means several times its own size. + and - change that."""
            pp = props.get(rowkey(r))
            res_ = state["res"]
            nm_ = next((r[f].split("/")[0] for f in ("small", "large") if r.get(f)), "")
            data = None
            if not (pp and os.path.isfile(pp)):
                data = res_["game"].get(nm_, T_BMP) if (res_ and nm_) else None   # type: ignore[index]
                if not data:
                    status.set(f"{r['name']} has no picture to show.")
                    return
            v = tk.Toplevel(win)
            esc_closes(v)
            v.title(Path(pp).name if pp and os.path.isfile(pp) else nm_ + ".bmp")
            keep_: Dict[str, object] = {}
            lab_ = tk.Label(v, cursor="hand2")
            lab_.pack()
            cap = tk.StringVar()
            bar_ = ttk.Frame(v)
            bar_.pack(pady=(2, 6))

            def source():
                """(Pillow image or None, raw bytes or None, width, height)."""
                try:
                    from PIL import Image
                    im_ = Image.open(pp).convert("RGB") if (pp and os.path.isfile(pp)) else \
                        Image.open(io.BytesIO(data)).convert("RGB")                     # type: ignore[arg-type]
                    return im_, None, im_.width, im_.height
                except Exception:  # noqa: BLE001 - no Pillow, or a format it will not open
                    raw = data if data else open(pp, "rb").read()                       # type: ignore[arg-type]
                    w_ = abs(struct.unpack_from("<i", raw, 18)[0]) if raw[:2] == b"BM" else 0
                    h_ = abs(struct.unpack_from("<i", raw, 22)[0]) if raw[:2] == b"BM" else 0
                    return None, raw, w_, h_
            im0, raw0, w0, h0 = source()
            if not w0 or not h0:
                v.destroy()
                status.set("That picture could not be read.")
                return
            fit = max(1.0, min((v.winfo_screenheight() - 220) / h0, (v.winfo_screenwidth() * 0.7) / w0))
            zoom = {"k": max(1.0, round(min(fit, 6.0), 2))}

            def render() -> None:
                k = zoom["k"]
                if im0 is not None:
                    from PIL import Image, ImageTk
                    nw, nh = max(1, int(w0 * k)), max(1, int(h0 * k))
                    small_ = im0.resize((nw, nh), Image.LANCZOS if k < 1 else Image.NEAREST if k >= 3 else Image.LANCZOS)
                    keep_["img"] = ImageTk.PhotoImage(small_)
                else:
                    ppm = bmp_to_ppm(raw0, 1)                                           # type: ignore[arg-type]
                    img_ = tk.PhotoImage(data=ppm) if ppm else None
                    if img_ is None:
                        return
                    keep_["img"] = img_.zoom(int(max(1, round(k))), int(max(1, round(k))))
                lab_.config(image=keep_["img"])                                         # type: ignore[arg-type]
                cap.set(f"{w0}x{h0}, shown at {int(k * 100)}%   -   click the picture to close, "
                        "right-click to save a copy")

            def step(by: float) -> None:
                zoom["k"] = max(0.25, min(8.0, round(zoom["k"] * by, 2)))
                render()
            ttk.Button(bar_, text="-", width=3, command=lambda: step(1 / 1.25)).pack(side="left")
            ttk.Button(bar_, text="+", width=3, command=lambda: step(1.25)).pack(side="left", padx=(4, 0))
            ttk.Button(bar_, text="1:1", width=4,
                       command=lambda: (zoom.update(k=1.0), render())).pack(side="left", padx=(4, 0))
            ttk.Button(bar_, text="Fit", width=4,
                       command=lambda: (zoom.update(k=round(fit, 2)), render())).pack(side="left", padx=(4, 10))
            ttk.Label(bar_, textvariable=cap, foreground="#666").pack(side="left")
            render()
            lab_.bind("<Button-1>", lambda e: v.destroy())
            lab_.bind("<Button-3>", lambda e: image_menu(e, r, v))
            esc_closes(v)
            v.bind("<plus>", lambda e: step(1.25))
            v.bind("<minus>", lambda e: step(1 / 1.25))
            v.bind("<MouseWheel>", lambda e: step(1.25 if e.delta > 0 else 1 / 1.25))
            v.transient(win)
        lb.bind("<Button-1>", big_view)

        def show_portrait() -> None:
            pp = props.get(rowkey(r))
            pim = load_thumb(pp) if pp else None
            shown = pim or img
            if shown:
                lb.config(image=shown, text="", width=0, height=0)
                lb.image = shown                                     # keep a reference
            else:
                lb.config(image="", text="no portrait" if not pp else "(no preview)", width=14, height=6, bg="#d4d7dc")
                lb.image = None
            color = "#2e9e4f" if pp else win.cget("bg")
            lb.config(highlightbackground=color, highlightcolor=color)

        info = ttk.Frame(head)
        info.pack(side="left", fill="x", expand=True)
        nml = ttk.Label(info, text=r["name"], font=("TkDefaultFont", 13, "bold"), cursor="hand2")
        nml.pack(anchor="w")
        nml.bind("<Button-1>", lambda e: copy_text(r["name"]))
        ttk.Label(info, text="  ".join(x for x in (r["race"], shown_sex(r["sex"]), r["anim"]) if x), foreground="#666").pack(anchor="w")
        sizes = ", ".join(x for x in ((f"large {r['large_size']}" if r.get("large_size") else ""),
                                      (f"medium {r['small_size']}" if r.get("small_size") else "")) if x)
        ttk.Label(info, text=f"Portrait: {r['portrait']}" + (f"  ({sizes})" if sizes else "")
                  + "     (click the picture to see it full size, right-click to save it)",
                  foreground="#666", wraplength=420, justify="left").pack(anchor="w")
        if r.get("copy_of"):                                         # built by copying one of the game's own
            ttk.Label(info, foreground="#1a5fb4", wraplength=640, justify="left",
                      text=(f"The mod builds this by copying {r['copy_of']}.CRE, so the face it shows is "
                            f"{r['copy_port']} - that creature's, not its own."
                            if r.get("copy_worn") else
                            f"Wears nothing of its own. The mod builds it by copying {r['copy_of']}.CRE, "
                            f"which wears {r['copy_port']} - the face it would have had.")).pack(anchor="w")
            kv2 = tk.BooleanVar(value=face_kept(r))
            kc2 = ttk.Checkbutton(info, text="Keep this face", variable=kv2,
                                  command=lambda: set_kept(r, kv2.get()))
            kc2.pack(anchor="w")
            keep_vars.append(kv2)
            tooltip(kc2, f"Ticked: Create mod writes {r['copy_port']} into {r['name']}, so it keeps this face "
                         "whatever order the mods go in. Unticked: it wants a face of its own.")
        mine = {x.upper() for f in ("large", "small") for x in str(r.get(f) or "").split("/") if x}
        others = sorted({o["name"] for o in all_rows() if o is not r and o["name"] != r["name"]
                         and mine & {x.upper() for f in ("large", "small") for x in str(o.get(f) or "").split("/") if x}})
        if others:
            ttk.Label(info, foreground="#8a6d0b", wraplength=640, justify="left",
                      text="Same portrait as: " + ", ".join(others[:12])
                           + (f" and {len(others) - 12} more" if len(others) > 12 else "")).pack(anchor="w")
        pf = ttk.Frame(info)
        pf.pack(anchor="w", pady=(4, 0))
        pl = tk.StringVar()

        def upd_prop() -> None:
            pp = props.get(rowkey(r))
            pl.set((f"Proposed: {Path(pp).name}" + (f" ({prop_dims(pp)})" if prop_dims(pp) else "")) if pp
                   else "No portrait proposed yet")
            clr.config(state="normal" if pp else "disabled")
            show_portrait()

        def clear_here() -> None:
            clear_prop(r)
            upd_prop()

        # the lines window has done its job once you are choosing a picture: it closes rather than lurking behind
        prop_btn = ttk.Button(pf, text="Propose...", command=lambda: (win.destroy(), open_propose(r, None, root)))
        prop_btn.pack(side="left")
        tooltip(prop_btn, "Make a picture, take one of your own or one from the pool, frame it, and propose it.")
        clr = ttk.Button(pf, text="Clear", command=clear_here)
        clr.pack(side="left", padx=4)
        tooltip(clr, "Forget the portrait proposed for this creature.")
        frame_btn = ttk.Button(pf, text="Frame...", command=lambda: frame_dialog(r, win, upd_prop))
        frame_btn.pack(side="left", padx=(0, 4))
        tooltip(frame_btn, "Choose which part of the proposed picture becomes the portrait. Without it the middle is "
                           "used, cut to the portrait's shape.")
        same_btn = ttk.Button(pf, text="Share...", command=lambda: share_portrait(r, win, upd_prop))
        same_btn.pack(side="left")
        dl_lbl, dl_why = declone_label(r)
        if dl_lbl:
            dl_btn = ttk.Button(pf, text=dl_lbl, command=lambda: (toggle_declone(r), win.destroy()))
            dl_btn.pack(side="left", padx=(8, 0))
            tooltip(dl_btn, dl_why + "\n\nThey were put in one row only because they share a name and look the same. "
                            "Creatures that share a creature file are never split.")
        tooltip(same_btn, "Give the proposed picture to other creatures of the same name, or to the ones that share "
                          "this creature's portrait now. Useful after decloning.")
        ttk.Label(info, textvariable=pl, foreground="#1f7a3a", wraplength=380, justify="left").pack(anchor="w")
        upd_prop()
        ttk.Label(info, text="(Clear or export proposals from the Proposals tab.)", foreground="#888").pack(anchor="w")
        chip_head = ttk.Frame(info)                                  # a well-used creature has dozens of these:
        chip_head.pack(anchor="w", pady=(2, 0))                      # a row would run off the window
        chips = ttk.Frame(info, padding=(14, 0, 0, 2))
        groups = [("Creature files", r["cre_list"]), ("Dialog files", sorted(set(r["dialogs"])))]
        if any(names for _lbl, names in groups):
            per = 8                                                  # chips per line, wrapped instead of one long row
            for lbl, names in groups:
                if not names:
                    continue
                for i in range(0, len(names), per):
                    line = ttk.Frame(chips)
                    line.pack(anchor="w")
                    ttk.Label(line, text=(f"{lbl}:" if i == 0 else " " * len(lbl)), foreground="#888",
                              width=max(len(x) for x, _n in groups) + 1).pack(side="left")
                    for nm in names[i:i + per]:                      # a creature and its dialog share a name, so
                        ch = tk.Label(line, text=nm, fg="#1a5fb4", cursor="hand2",   # say which list each is in
                                      font=("TkDefaultFont", 9,
                                            "bold underline" if (lbl.startswith("Creature") and nm in r["cre_known"])
                                            else "underline"))
                        ch.pack(side="left", padx=4)
                        ch.bind("<Button-1>", lambda e, n=nm: copy_text(n))
            ctxt = tk.StringVar()
            n_cre, n_dlg = len(r["cre_list"]), len(set(r["dialogs"]))

            def show_chips(opened: bool) -> None:
                ctxt.set(("\u25bc" if opened else "\u25b6")
                         + f" Creature and dialog files ({n_cre} CRE, {n_dlg} DLG) - click one to copy")
                if opened:
                    chips.pack(anchor="w", after=chip_head)
                else:
                    chips.pack_forget()

            def toggle_chips() -> None:
                st["chips_open"] = not st.get("chips_open", False)
                save_settings(st)
                show_chips(bool(st["chips_open"]))
            ttk.Button(chip_head, textvariable=ctxt, command=toggle_chips).pack(side="left")
            show_chips(bool(st.get("chips_open", False)))

        def copy_all() -> None:
            win.clipboard_clear()
            win.clipboard_append("\n".join(t for _sr, t in r["texts"]))

        ttk.Button(head, text="Copy all lines", command=copy_all).pack(side="right")
        if r["cre_known"]:
            ttk.Label(win, text="Bold creature file(s) are the ones this mod itself ships or names; the others only share the "
                      "same dialog.", foreground="#666", wraplength=720, padding=(8, 0)).pack(anchor="w")
        if r["origin"]:
            # collapsed by default so the lines get the room; the choice is remembered
            n_notes = len(r["origin"])
            oh = ttk.Frame(win, padding=(8, 2))
            oh.pack(fill="x")
            ob = ttk.Frame(win, padding=(14, 0, 8, 4))
            for note in r["origin"]:
                ttk.Label(ob, text=note, wraplength=700, justify="left").pack(anchor="w", pady=1)
            otxt = tk.StringVar()

            def show_origin(opened: bool) -> None:
                otxt.set(("\u25bc" if opened else "\u25b6") + f" Where this comes from ({n_notes} note{'s' if n_notes != 1 else ''})")
                if opened:
                    ob.pack(fill="x", after=oh)
                else:
                    ob.pack_forget()

            # a few notes: open, as that is usually all there is to say. A long list (a well-traveled NPC) starts
            # closed so it does not push the lines off the window. Each kind remembers what you last chose.
            okey = "origin_open" if n_notes <= 4 else "origin_open_long"

            def toggle_origin() -> None:
                st[okey] = not st.get(okey, n_notes <= 4)
                save_settings(st)
                show_origin(bool(st[okey]))
            ttk.Button(oh, textvariable=otxt, command=toggle_origin).pack(side="left")
            show_origin(bool(st.get(okey, n_notes <= 4)))
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True)
        cv = tk.Canvas(body, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        inner = ttk.Frame(cv)
        wid = cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(wid, width=e.width))
        win.bind("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        sounds: Dict[int, str] = r.get("sounds", {})
        play: Dict[str, object] = {}                                 # what this window is playing: sr, button, timer

        def reset_play() -> None:
            job = play.pop("job", None)
            if job:
                try:
                    win.after_cancel(job)                            # type: ignore[arg-type]
                except tk.TclError:
                    pass
            btn = play.pop("btn", None)
            play.pop("sr", None)
            try:
                if btn is not None:
                    btn.config(text="\u25b6 Play")                  # type: ignore[attr-defined]
            except tk.TclError:
                pass

        def toggle_sound(sr: int, btn: object) -> None:
            if play.get("sr") == sr:                                 # same line again = stop
                stop_sound()
                reset_play()
                return
            stop_sound()
            reset_play()
            snd = sounds[sr]
            game: Game = res["game"]                                 # type: ignore[index]
            data = game.get(snd, T_WAV)
            if not data:
                status.set(f"Sound {snd} could not be read")
                return
            wav, err = sound_to_wav(data)
            if not wav:
                status.set(f"Sound {snd}: {err}")
                return
            d = Path(tempfile.gettempdir()) / "portrait_scout_sounds"
            try:
                d.mkdir(exist_ok=True)
                f = d / f"{snd}.wav"
                f.write_bytes(wav)
            except OSError as e:
                status.set(f"Sound {snd}: could not write a temp file ({e})")
                return
            if not play_file(str(f)):
                status.set(f"Sound {snd}: no way to play sounds on this system")
                return
            secs = wav_seconds(wav)
            play.update(sr=sr, btn=btn)
            btn.config(text="\u25a0 Stop")                          # type: ignore[attr-defined]
            if secs:
                play["job"] = win.after(int(secs * 1000) + 300, reset_play)
            status.set(f"Playing {snd} ({secs:.1f} s)" if secs else f"Playing {snd}")

        def on_close(e: object) -> None:
            if getattr(e, "widget", None) is win and play.get("sr") is not None:
                stop_sound()
        win.bind("<Destroy>", on_close)

        bar = ttk.Frame(win, padding=(8, 2))
        bar.pack(fill="x", before=body)
        ttk.Label(bar, text="Search:").pack(side="left")
        line_q = tk.StringVar(value="")
        qe = ttk.Entry(bar, textvariable=line_q, width=28)
        qe.pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="x", width=2, command=lambda: line_q.set("")).pack(side="left", padx=(2, 0))
        line_count = tk.StringVar(value="")
        ttk.Label(bar, textvariable=line_count, foreground="#1a5fb4").pack(side="left", padx=(6, 0))
        if sounds:
            ttk.Label(bar, text=f"{len(sounds)} of {len(r['texts'])} line(s) have a sound - press \u25b6 to hear it.",
                      foreground="#666").pack(side="left", padx=(12, 0))
        win.bind("<Control-f>", lambda e: (qe.focus_set(), qe.select_range(0, "end")))
        PAGE = 200                                                   # thousands of cards make Tk hang, so page them
        nav = ttk.Frame(win, padding=(8, 0))
        prev_b = ttk.Button(nav, text="< Previous", command=lambda: turn(-1))
        prev_b.pack(side="left")
        ttk.Label(nav, text="Page").pack(side="left", padx=(6, 2))
        page_var = tk.StringVar(value="1")
        page_ent = ttk.Entry(nav, textvariable=page_var, width=5, justify="center")
        page_ent.pack(side="left")
        page_lbl = ttk.Label(nav, text="")
        page_lbl.pack(side="left", padx=(2, 6))
        next_b = ttk.Button(nav, text="Next >", command=lambda: turn(1))
        next_b.pack(side="left")
        view: Dict[str, object] = {"match": list(r["texts"]), "page": 0, "cards": [], "job": None}

        def make_card(sr: int, t: str) -> object:
            card = ttk.Frame(inner, padding=6, relief="groove")
            card.pack(fill="x", padx=8, pady=3)
            top = ttk.Frame(card)
            top.pack(fill="x")
            from_dlg = (r.get("ref_of") or {}).get(sr, "")           # which dialog this line came out of, when
            many = len(set(r["dialogs"])) > 1                        # the creature has more than one
            ttk.Label(top, text=(r["line_src"].get(sr, "") if r.get("line_src") else f"#{sr}")
                      + (f"   {from_dlg}" if many and from_dlg else "")
                      + (f"   {sounds[sr]}" if sr in sounds else ""), foreground="#888").pack(side="left")
            ttk.Button(top, text="Copy", width=6,
                       command=lambda tt=t, s=sr: (win.clipboard_clear(), win.clipboard_append(tt),
                                                   status.set(f"Copied line #{s}"))).pack(side="right")
            if sr in sounds:
                b = ttk.Button(top, text="\u25b6 Play", width=8)
                b.config(command=lambda s=sr, bb=b: toggle_sound(s, bb))
                b.pack(side="right", padx=(0, 4))
            ttk.Label(card, text=t or "(empty)", wraplength=680, justify="left").pack(anchor="w")
            return card

        def render() -> None:
            if play.get("sr") is not None:                           # its button is about to go away
                stop_sound()
                reset_play()
            for c in view["cards"]:                                  # type: ignore[union-attr]
                c.destroy()                                          # type: ignore[attr-defined]
            match: List[Tuple[int, str]] = view["match"]             # type: ignore[assignment]
            pages = max(1, (len(match) + PAGE - 1) // PAGE)
            pg = min(max(0, int(view["page"])), pages - 1)           # type: ignore[call-overload]
            view["page"] = pg
            view["cards"] = [make_card(sr, t) for sr, t in match[pg * PAGE:(pg + 1) * PAGE]]
            if not match:
                view["cards"] = [ttk.Label(inner, text="No line matches.", foreground="#888", padding=8)]
                view["cards"][0].pack(anchor="w")                    # type: ignore[index]
            page_var.set(str(pg + 1))
            page_lbl.config(text=f"of {pages}  (lines {pg * PAGE + 1 if match else 0}-{min(len(match), (pg + 1) * PAGE)})")
            for w, ok in ((prev_b, pg > 0), (next_b, pg < pages - 1)):
                w.config(state="normal" if ok else "disabled")
            if pages > 1:
                nav.pack(anchor="e", before=body)
            else:
                nav.pack_forget()
            cv.yview_moveto(0)

        def jump_page(_e: object = None) -> None:
            try:
                view["page"] = int(page_var.get().strip()) - 1
            except ValueError:
                pass
            render()
        page_ent.bind("<Return>", jump_page)

        def turn(d: int) -> None:
            view["page"] = int(view["page"]) + d                     # type: ignore[call-overload]
            render()

        def filter_lines() -> None:
            view["job"] = None
            if not win.winfo_exists():
                return
            terms = line_q.get().lower().split()
            view["match"] = [(sr_, t_) for sr_, t_ in r["texts"]
                             if all(w.lstrip("#") in f"{sr_} {sounds.get(sr_, '')} {t_}".lower() for w in terms)]
            view["page"] = 0
            line_count.set(f"{len(view['match'])} of {len(r['texts'])}" if terms else "")   # type: ignore[arg-type]
            render()

        def on_line_q(*_a: object) -> None:
            if view["job"]:
                win.after_cancel(view["job"])                        # type: ignore[arg-type]
            view["job"] = win.after(250, filter_lines)
        line_q.trace_add("write", on_line_q)
        render()

    def finish(res: Optional[dict], err: Optional[str]) -> None:
        go_btn.config(state="normal")
        if err:
            status.set("Error: " + err)
            messagebox.showerror("Portrait Scout", err)
            return
        state["res"] = res
        state["page"] = 0
        mark_party(res)
        for r_ in res["rows"]:                                       # a split creature knows the row it came from
            for p_ in r_.get("parts") or []:
                p_["_parent"] = r_
        state["gen_row"] = None                                      # the Propose tab belonged to the last analysis
        gen_hint()
        props.clear()
        props.update({k: v for k, v in (st.get("proposals", {}).get(res["mod"], {}) or {}).items()          # type: ignore[index]
                      if isinstance(v, str)})
        skip.clear()
        skip.update(x for x in (st.get("ignored", {}).get(res["mod"], []) or []) if isinstance(x, str))   # type: ignore[index]
        kept.clear()
        kept.update({k: bool(v) for k, v in ((st.get("kept_faces") or {}).get(res["mod"], {}) or {}).items()
                     if isinstance(k, str)})
        # creatures can be grouped differently than when these were saved (above all: the mod's own files hold dialogs
        # the installed game does not), which would leave the pictures attached to nothing. Hand them over instead.
        bunches = [r for r in res["rows"] if r.get("parts")]         # type: ignore[union-attr]
        was_split = [k for k in ((st.get("declone") or {}).get(str(res["mod"])) or []) if k]   # type: ignore[index]
        d_moved = remap_keys(was_split, bunches)
        if d_moved:                                                  # the bunches taken apart one by one, re-found
            now_split = list(dict.fromkeys(d_moved.get(k, k) for k in was_split))
            st.setdefault("declone", {})[res["mod"]] = now_split     # type: ignore[index]
        every = [x for r_ in res["rows"] for x in ([r_] + (r_.get("parts") or []))]            # type: ignore[union-attr]
        moved = remap_keys(list(props), every)
        for old_, new_ in moved.items():
            props[new_] = props.pop(old_)
        s_moved = remap_keys(sorted(skip), every)
        for old_, new_ in s_moved.items():
            skip.discard(old_)
            skip.add(new_)
        k_moved = remap_keys(sorted(kept), every)
        for old_, new_ in k_moved.items():
            kept[new_] = kept.pop(old_)
        now_split = [k for k in ((st.get("declone") or {}).get(str(res["mod"])) or []) if k]   # type: ignore[index]
        woke = []                                                    # a picture chosen for one creature of a bunch is
        for b in bunches:                                            # only on show while the bunch is listed one by one
            bk = rowkey(b)
            if bk not in now_split and bk not in props and any(rowkey(p_) in props for p_ in b["parts"]):
                woke.append(bk)
        woke_set = set(woke)
        if woke:
            st.setdefault("declone", {})[res["mod"]] = now_split + woke   # type: ignore[index]
        if moved or s_moved or k_moved or d_moved or woke:
            st.setdefault("proposals", {})[res["mod"]] = dict(props)     # type: ignore[index]
            st.setdefault("ignored", {})[res["mod"]] = sorted(skip)      # type: ignore[index]
            st.setdefault("kept_faces", {})[res["mod"]] = dict(kept)     # type: ignore[index]
            save_settings(st)
        update_prop_label()
        refresh()
        list_btn.config(state="normal")
        extra = ""
        if res.get("source"):                                                        # type: ignore[union-attr]
            unk = res["stats"].get("dialogs not linked", 0)                          # type: ignore[index]
            extra = (" from the mod's files" + (" (EET install)" if res.get("eet") else "")       # type: ignore[union-attr]
                     + (f", {unk} dialog(s) without a speaker" if unk else "")
                     + ("" if res.get("has_game") else " - no game folder, so the game's own NPCs are not named"))   # type: ignore[union-attr]
        back = len(moved) + sum(1 for b in bunches if rowkey(b) in woke_set for p_ in b["parts"] if rowkey(p_) in props)
        if back:                                                     # these are grouped differently than when chosen
            extra += f", {back} picture(s) given back to their creature"
        out = Path(__file__).resolve().parent / f"{res['mod']}-creatures.txt"       # type: ignore[index]
        try:
            out.write_text(as_text(res, sorted_rows(False)), encoding="utf-8")            # type: ignore[arg-type]
            status.set(f"{len(res['rows'])} creature(s){extra}. Saved {out}")        # type: ignore[index]
        except OSError:
            status.set(f"{len(res['rows'])} creature(s){extra}.")                    # type: ignore[index]

    def analyze_clicked() -> None:
        g, m = game_var.get().strip(), mod_var.get().strip()
        if not os.path.isdir(g) or (m and not os.path.isdir(m)):
            messagebox.showwarning("Portrait Scout", "Pick an existing game folder (and a mod folder, or leave it empty "
                                   "for the whole install).")
            return
        st.update({"game": g, "mod": m})
        save_settings(st)
        go_btn.config(state="disabled")
        state["imgs"] = {}
        box: Dict[str, object] = {}

        def work() -> None:
            say = lambda s: root.after(0, status.set, s)            # noqa: E731
            try:
                try:
                    box["res"] = analyze(g, m, False, say)
                except NotInstalled:                                 # not installed in this game: read the mod's
                    say("The mod is not installed here - reading its own files...")   # own files instead
                    box["res"] = analyze_source(g, m, False, say)
            except Exception as e:  # noqa: BLE001
                box["err"] = str(e)
            res_ = box.get("res")
            close_ = getattr(res_.get("game"), "close", None) if isinstance(res_, dict) else None
            if close_:
                close_()                                             # don't keep the BIFs open afterwards
            root.after(0, lambda: finish(box.get("res"), box.get("err")))   # type: ignore[arg-type]

        threading.Thread(target=work, daemon=True).start()

    def save_list() -> None:
        res = state["res"]
        if not res:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=f"{res['mod']}-creatures",    # type: ignore[index]
                                         filetypes=[("Text", "*.txt"), ("Spreadsheet (CSV)", "*.csv")])
        if not p:
            return
        if p.lower().endswith(".csv"):
            write_csv(res, p, sorted_rows(False))                                             # type: ignore[arg-type]
        else:
            Path(p).write_text(as_text(res, sorted_rows(False)), encoding="utf-8")            # type: ignore[arg-type]
        status.set(f"Saved {p}")

    share_btn.config(command=share_work)
    takein_btn.config(command=open_work)
    lost_btn.config(command=fix_lost)
    tooltip(lost_btn, "Some creatures are proposed a picture that is no longer on the disk - usually one that "
                      "was cropped again, which deletes the crop it replaces. This points them at the crop "
                      "that is there, and lists any it cannot place.")
    tooltip(share_btn, "Put everything another person needs to carry on into one .zip: the pictures you have "
                       "chosen, which creature each one is for, the creatures you have set aside, and what each "
                       "picture was asked for. Their copy of Portrait Scout reads it with 'Import work...'.")
    tooltip(takein_btn, "Take in work shared from another copy of Portrait Scout. The pictures are copied into "
                        "this one's generated folder and matched to the creatures here - which works even when "
                        "their mod is grouped differently from yours. You are asked before anything you have "
                        "already chosen is replaced.")
    go_btn.config(command=analyze_clicked)
    list_btn.config(command=save_list)
    setup_btn.config(command=setup_window)
    tooltip(setup_btn, "How to set up the image engine, with download links. Shown by itself the first "
                       "time Portrait Scout opens without one.")
    refresh()                                                        # the table says what to do before the first run
    if (not engine_ready() or not faces_count()) and not st.get("setup_shown"):             # first run with no engine: show the steps once
        st["setup_shown"] = True
        save_settings(st)
        root.after(600, setup_window)
    root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Creatures / speakers of an installed mod: name, lines, portrait, race, gender, animation.")
    ap.add_argument("--cli", nargs=2, metavar=("GAMEDIR", "MODDIR"), help="no window: analyze and print")
    ap.add_argument("--out", default="", help="also write this text file")
    ap.add_argument("--csv", default="", help="also write this CSV file")
    ap.add_argument("--merge-names", action="store_true")
    ap.add_argument("--source", metavar="MODDIR", default="", help="no window: read the mod's own files "
                    "(works before the mod is installed); add --game GAMEDIR to name NPCs of the game")
    ap.add_argument("--game", default="", help="with --source: the game folder (optional)")
    a = ap.parse_args()
    if a.source:
        res = analyze_source(a.game, a.source, a.merge_names, lambda s: print(s, file=sys.stderr))
        txt = as_text(res)
        print(txt)
        if a.out:
            Path(a.out).write_text(txt, encoding="utf-8")
        if a.csv:
            write_csv(res, a.csv)
        return 0
    if not a.cli:
        run_gui()
        return 0
    res = analyze(a.cli[0], a.cli[1], a.merge_names, lambda s: print(s, file=sys.stderr))
    txt = as_text(res)
    print(txt)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8")
    if a.csv:
        write_csv(res, a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
