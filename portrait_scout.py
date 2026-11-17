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
import csv
import io
import json
import os
import re
import shutil
from fractions import Fraction

import struct
import subprocess
import sys
import time
import tempfile
import threading
import wave
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

VERSION = "1.2"
T_BMP, T_WAV, T_BCS, T_IDS, T_CRE, T_DLG, T_2DA = 0x0001, 0x0004, 0x03EF, 0x03F0, 0x03F1, 0x03F3, 0x03F4
T_ARE, T_WMP = 0x03F2, 0x03F7
EXT = {T_BMP: ".bmp", T_WAV: ".wav", T_BCS: ".bcs", T_IDS: ".ids", T_CRE: ".cre", T_DLG: ".dlg", T_2DA: ".2da",
       T_ARE: ".are", T_WMP: ".wmp"}
SOURCE_EXTS = {".tra", ".d", ".baf", ".tph", ".tpa", ".tpp", ".tp2", ".tp3", ".tpl"}
NO_PORTRAIT = {"", "NONE", "NO_PORT", "NOPORT"}
SEX_NAMES = {1: "Male", 2: "Female", 3: "Other", 4: "Neither"}
RACE_FALLBACK = {1: "Human", 2: "Elf", 3: "Half-Elf", 4: "Dwarf", 5: "Halfling", 6: "Gnome", 7: "Half-Orc", 183: "Tiefling"}
Progress = Callable[[str], None]


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
        self._load_key()

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
        p = self._path_ci(self.bifs[idx]) if idx < len(self.bifs) else None
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
        return ents

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
        p = self._path_ci(self.bifs[loc[0]])
        with open(p, "rb") as f:                                    # type: ignore[arg-type]
            f.seek(o)
            return f.read(min(s, limit) if limit > 0 else s)

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
            raise ValueError("None of this mod's text was found in dialog.tlk - is the mod installed in this game folder?")
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
    cres = game.names(T_CRE)
    for k, cn in enumerate(cres):
        if k % 1000 == 0:
            progress(f"Reading creatures... {k}/{len(cres)}")
        b = game.get(cn, T_CRE, 0x2D4)
        if not b or len(b) < 0x2D4 or b[:4] != b"CRE " or b[4:8] not in (b"V1.0", b"V1  "):
            continue
        info = {"cre": cn, "long": struct.unpack_from("<i", b, 8)[0], "short": struct.unpack_from("<i", b, 12)[0],
                "anim": struct.unpack_from("<I", b, 0x28)[0], "small": _r8(b, 0x34), "large": _r8(b, 0x3C),
                "race": b[0x272], "cls": b[0x273], "sex": b[0x275], "dialog": _r8(b, 0x2CC).upper()}
        cre_by_name[cn] = info
        if info["dialog"]:
            cre_by_dlg.setdefault(info["dialog"], []).append(info)
    cre_names_sorted = sorted(cre_by_name)

    def resolve(dlg: str) -> Tuple[List[dict], str, str]:
        """(creature files, how): 'dialog' = the creature's own dialog field points here, 'name' = same file name,
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
            pre = [cn for cn in cre_names_sorted if len(cn) >= 4 and base.startswith(cn)]
            if pre:
                return [cre_by_name[max(pre, key=len)]], "guess", ""
            if len(base) >= 4:
                for cn in cre_names_sorted:
                    if cn.startswith(base):
                        return [cre_by_name[cn]], "guess", ""
        return [], "", ""

    race_ids, anim_ids = game.read_ids("RACE"), game.read_ids("ANIMATE")

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
            if same_npc or not any(look) or not any(glook) or look == glook:
                if not same_npc:                                     # merged on the name alone: they may be clones
                    g.setdefault("parts", [_snapshot(g)]).append(_snapshot(r))
                g["lines"] += r["lines"]
                g["refs"] |= r["refs"]
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
    # where do the unknown / guessed speakers come from?
    need = {dn for r in out for dn, how in r["how"] if how in ("", "guess", "related")}
    progress("Looking up where the unknown speakers come from...")
    mentions = find_mentions(mod_dir, need) if (need and not whole) else {}
    for r in out:
        notes: List[str] = []
        for dn, how in r["how"]:
            if how not in ("", "guess", "related"):
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
            b = f.read(0x2D4)
    except OSError:
        return None
    if len(b) < 0x2D4 or b[:4] != b"CRE " or b[4:8] not in (b"V1.0", b"V1  "):
        return None
    return {"cre": os.path.splitext(os.path.basename(path))[0].upper(), "name": "", "anim": struct.unpack_from("<I", b, 0x28)[0],
            "small": _r8(b, 0x34), "large": _r8(b, 0x3C), "race": b[0x272], "sex": b[0x275],
            "scripts": {_r8(b, o).upper() for o in (0x248, 0x250, 0x258, 0x260, 0x268)} - {"", "NONE"},
            "dv": b[0x280:0x2A0].split(b"\0", 1)[0].decode("latin1").strip().upper(), "dialog": _r8(b, 0x2CC).upper(),
            "long": struct.unpack_from("<i", b, 8)[0], "file": path}


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
            body = t[m.end():nx.start() if nx else len(t)]
            for dst_name, src_name in targets:
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
                    txt = tra_text(int(val[1:]), p, True)[0] if val.startswith("@") else subst(_unq(val))
                    if s.group(1).upper() in ("NAME1", "0X8", "0X08") or not c["name"]:
                        c["name"] = _COLOR.sub(r"\1", txt).strip()
                for w in _WA_RE.finditer(body):
                    off, val = int(w.group(1), 0), subst(_unq(w.group(2))).strip()
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
                                  "kind": src_kind, "voiced": 0, "infos": [],
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
    for k, sr in enumerate(src["rows"]):
        infos: List[dict] = sr["infos"]
        key = keys[k]
        r = rows.setdefault(key, {"name": sr["name"], "lines": 0, "large": "", "small": "", "race": "", "sex": "", "anim": "",
                                  "dialogs": [], "cres": [], "found": False, "refs": set(), "how": [], "via": {},
                                  "_lines": [], "origin": [], "_infos": []})
        r["lines"] += sr["lines"]
        r["_lines"] += sr["texts"]
        r["_infos"] += infos
        r["found"] = r["found"] or bool(infos) or sr["kind"] == "game"
        r["cres"] += [c for c in sr["cres"] if c not in r["cres"]]
        for c in infos:
            for f in ("large", "small"):
                v = c[f]
                if v and v.upper() not in NO_PORTRAIT and v.upper() not in (x.upper() for x in r[f].split("/")):
                    r[f] = f"{r[f]}/{v}" if r[f] else v
        if not infos and not r["large"] and not r["small"]:
            r["large"], r["small"] = sr.get("g_large", ""), sr.get("g_small", "")
        if infos and not r["race"]:
            c = infos[0]
            r["race"] = nice(race_ids.get(c["race"], "")) or RACE_FALLBACK.get(c["race"], f"race {c['race']}" if c["race"] else "")
            r["sex"] = SEX_NAMES.get(c["sex"], "")
            an = (anim_ids.get(c["anim"]) or anim_ids.get(c["anim"] & 0xFF00) or "") if c["anim"] else ""
            r["anim"] = nice(an) or (f"0x{c['anim']:04X}" if c["anim"] else "")
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
    out = sorted(rows.values(), key=lambda r: (-r["lines"], r["name"].lower()))

    def bmp_dims(nm: str) -> str:
        b = files.get(nm, T_BMP, 64) if nm else None
        if b and b[:2] == b"BM" and len(b) >= 26:
            w, h = struct.unpack_from("<ii", b, 18)
            return f"{abs(w)}x{abs(h)}"
        return ""

    for r in out:
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
    add_areas(out, game, text_, progress, mod_dir)
    st_ = src["stats"]
    progress(f"Done: {len(out)} creature(s), {sum(r['lines'] for r in out):,} line(s) from the mod's files.")
    return {"game": files, "rows": out, "whole": False, "strings": st_.get("lines", 0), "mod": src["mod"], "source": True,
            "stats": st_, "eet": src["eet"], "has_game": game is not None}


def source_text(res: dict) -> str:
    st = res["stats"]
    out = [f"Speakers of {res['mod']} read from the mod's own files", ""]
    out += [f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}" for k, v in st.items()] + [""]
    w = max([len(r["name"]) for r in res["rows"]] + [8])
    out.append(f"{'Creature'.ljust(w)}  Lines  Voiced  {'Portrait'.ljust(10)}  CRE file(s) / dialogs")
    for r in res["rows"]:
        dl = ", ".join(f"{d} ({n}{'; ' + how if how else ''})" for d, (n, how) in sorted(r["dialogs"].items()))
        out.append(f"{r['name'].ljust(w)}  {r['lines']:5}  {r['voiced']:6}  {(r['portrait'] or '-').ljust(10)}  "
                   f"{'/'.join(r['cres']) or '-'} | {dl}")
    return "\n".join(out) + "\n"


COLUMNS = [("name", "Creature"), ("lines", "Lines"), ("portrait", "Portrait"), ("race", "Race"), ("sex", "Gender"),
           ("anim", "Animation"), ("cre", "CRE file(s)"), ("area", "Area(s)")]


def as_text(res: dict, rows: Optional[List[dict]] = None) -> str:
    rows = res["rows"] if rows is None else rows
    w = [max(len(h), *(len(str(r[k])) for r in rows)) if rows else len(h) for k, h in COLUMNS]
    lines = [f"Creatures of {res['mod']}  ({len(rows)} creature(s), {sum(r['lines'] for r in rows):,} line(s))", ""]
    lines.append("  ".join(h.ljust(w[i]) if i != 1 else h.rjust(w[i]) for i, (k, h) in enumerate(COLUMNS)))
    lines.append("  ".join("-" * x for x in w))
    for r in rows:
        lines.append("  ".join(str(r[k]).ljust(w[i]) if i != 1 else str(r[k]).rjust(w[i]) for i, (k, h) in enumerate(COLUMNS)))
    users: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        for p in filter(None, r["large"].split("/")):
            users[p.upper()].append(r["name"])
    dups = {p: sorted(set(n)) for p, n in users.items() if len(set(n)) > 1}
    if dups:
        lines += ["", "Portraits used by several creatures:"] + [f"  {p}: {', '.join(n)}" for p, n in sorted(dups.items())]
    nop = sum(1 for r in rows if not (r["large"] or r["small"]))
    lines += ["", f"{nop} of {len(rows)} creature(s) have no portrait."]
    return "\n".join(lines) + "\n"


def write_csv(res: dict, path: str, rows: Optional[List[dict]] = None) -> None:
    rows = res["rows"] if rows is None else rows
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["creature", "lines", "portrait", "small_portrait", "large_size", "small_size", "race", "gender",
                    "animation", "dialog", "cre", "areas", "notes"])
        for r in rows:
            w.writerow([r["name"], r["lines"], r["large"] or r["small"], r["small"], r.get("large_size", ""),
                        r.get("small_size", ""), r["race"], r["sex"], r["anim"],
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


def areas_text(r: dict, limit: int = 0) -> str:
    al = r.get("areas", [])
    out = "; ".join(f"{a}{' ' + nm if nm else ''} ({how})" for a, nm, how in (al[:limit] if limit else al))
    return out + (f"; +{len(al) - limit} more" if limit and len(al) > limit else "")


# --------------------------------------------------------------------------------------------- export as a WeiDU mod
MOD_MARK = "// Made by Portrait Scout"
MOD_VERSION = "1.0"
_TP2_FUNC = """ALWAYS
  // Gives one creature file a portrait. replace = 0: only if the creature has no portrait picture now.
  DEFINE_ACTION_FUNCTION ps_portrait INT_VAR replace = 1 STR_VAR cre = ~~ bmp = ~~ set = ~~ BEGIN
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
        COPY ~%MOD_FOLDER%/portraits/%set%/%bmp%.bmp~ ~override~
        COPY_EXISTING ~%cre%.cre~ ~override~
          WRITE_ASCIIE 0x34 ~%bmp%~ #8
          WRITE_ASCIIE 0x3c ~%bmp%~ #8
        BUT_ONLY
      END
    END
  END
END
"""


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_#!-]+", "_", s).strip("_") or "set"


def _read_set(listp: Path) -> Tuple[str, int, List[List[str]]]:
    title, comp, rows = listp.parent.name, -1, []
    for ln in listp.read_text(encoding="utf-8", errors="replace").splitlines():
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
                rows.append([x.strip() for x in f] + [""] * (4 - len(f)))
    return title, comp, rows


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
            for cre, bmp, name, _areas in rows:
                out.append(f"  LAF ps_portrait INT_VAR replace = {replace} STR_VAR cre = ~{cre}~ bmp = ~{bmp}~ "
                           f"set = ~{sdir}~ END" + (f"   // {q(name)}" if name else ""))
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
                rows.append([x.strip() for x in f] + [""] * (4 - len(f)))
    return title, comp, rows


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
            rows.append([c, bmp, e["name"], str(e.get("areas", ""))])
    for old in sdir.glob("*.bmp"):                                   # pictures of an earlier export, no longer proposed
        if old.stem.upper() not in files:
            try:
                old.unlink()
            except OSError:
                pass
    listp.write_text("\n".join([f"# set: {set_name}", f"# component: {comp}",
                                "# creature file<TAB>picture<TAB>creature<TAB>areas it appears in"]
                               + ["\t".join(r) for r in rows]) + "\n", encoding="utf-8")
    nsets = write_mod_tp2(str(root))
    exe = root.parent / f"setup-{root.name}.exe"
    src_exe = _find_weidu(game_dir, str(Path(__file__).resolve().parent))
    if src_exe and not (exe.is_file() and _is_weidu(str(exe))):      # replace a wrong one from an older export
        try:
            shutil.copyfile(src_exe, exe)
        except OSError:
            src_exe = ""
    return {"portraits": len(files), "creatures": len(rows), "skipped": skipped, "sets": nsets,
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


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    st = load_settings()
    root = tk.Tk()
    root.title(f"Portrait Scout {VERSION}")
    root.geometry("1100x720")

    PANEL, CARD = "#d8dce1", "#f2f3f6"            # table / gallery background, and the gallery cards on it
    sty = ttk.Style(root)
    sty.configure("Treeview", background=PANEL, fieldbackground=PANEL, rowheight=22)
    sty.map("Treeview", background=[("selected", "#3a78b5")], foreground=[("selected", "white")])
    here = os.getcwd()
    # start from the current folder when it is a game folder (run it from the game folder), else the last one used
    game_var = tk.StringVar(value=here if (Path(here) / "chitin.key").exists() else (st.get("game") or ""))
    mod_var = tk.StringVar(value=st.get("mod", ""))
    merge_var = tk.BooleanVar(value=False)
    source_var = tk.BooleanVar(value=bool(st.get("source", False)))
    modpic_var = tk.BooleanVar(value=bool(st.get("modpic_only", False)))
    status = tk.StringVar(value="Pick the game folder and the mod folder, then press Analyze. "
                          "(Leave the mod folder empty, or pick the game folder itself, to list every creature in the install.)")
    state: Dict[str, object] = {"res": None, "sort": ("lines", True), "imgs": {}, "page": 0}

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)
    top.columnconfigure(3, minsize=420)                              # room for the two checkboxes beside the folders

    def browse(var: tk.StringVar, title: str, start: str) -> None:
        d = filedialog.askdirectory(title=title, initialdir=start if start and os.path.isdir(start) else os.getcwd())
        if d:
            var.set(os.path.normpath(d))

    ttk.Label(top, text="Game folder:").grid(row=0, column=0, sticky="w")
    ttk.Entry(top, textvariable=game_var, width=34).grid(row=0, column=1, sticky="ew", padx=6)
    def path_box(var: tk.StringVar, title: str) -> None:
        """The whole path, in a box big enough to read it."""
        w = tk.Toplevel(root)
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
    chk1 = ttk.Frame(top)                                            # both are about the mod: they sit on its row
    chk1.grid(row=1, column=3, sticky="w", padx=(12, 0), pady=(6, 0))
    ttk.Label(top, text="Mod folder:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(top, textvariable=mod_var, width=34).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
    mb_ = ttk.Frame(top)
    mb_.grid(row=1, column=2, sticky="w", pady=(6, 0))
    ttk.Button(mb_, text="Browse...", command=lambda: browse(mod_var, "Mod folder (has the .tp2 / .tra / .d files)",
                                                             mod_var.get() or game_var.get())).pack(side="left")
    ttk.Button(mb_, text="\u2922", width=3, command=lambda: path_box(mod_var, "Mod folder")).pack(side="left", padx=(3, 0))
    def tooltip(widget: object, text: str) -> None:
        """Show `text` in a small box while the mouse rests on the widget."""
        tip: Dict[str, object] = {"win": None, "job": None}

        def show() -> None:
            tip["job"] = None
            w = tk.Toplevel(root)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{widget.winfo_rootx() + 12}+{widget.winfo_rooty() + widget.winfo_height() + 4}")   # type: ignore[attr-defined]
            tk.Label(w, text=text, justify="left", wraplength=420, bg="#ffffe0", relief="solid", bd=1,
                     padx=6, pady=4).pack()
            tip["win"] = w

        def enter(_e: object) -> None:
            leave(_e)
            tip["job"] = root.after(450, show)

        def leave(_e: object) -> None:
            if tip["job"]:
                root.after_cancel(tip["job"])                        # type: ignore[arg-type]
                tip["job"] = None
            if tip["win"]:
                tip["win"].destroy()                                 # type: ignore[attr-defined]
                tip["win"] = None
        widget.bind("<Enter>", enter, add="+")                       # type: ignore[attr-defined]
        widget.bind("<Leave>", leave, add="+")                       # type: ignore[attr-defined]
        widget.bind("<ButtonPress>", leave, add="+")                 # type: ignore[attr-defined]

    src_chk = ttk.Checkbutton(chk1, text="Read the mod's own files", variable=source_var,
                              command=lambda: merge_chk.config(state="disabled" if source_var.get() else "normal"))
    src_chk.pack(side="left")
    src_note = ttk.Label(chk1, text="(install not needed)", foreground="#777")
    src_note.pack(side="left", padx=(4, 0))
    for w_ in (src_chk, src_note):
        tooltip(w_, "The mod does not have to be installed: the speakers are worked out from its own files (.d, .tra, "
                ".tp2, .cre, .are) instead of by matching its text against the installed game.\n\nEvery line comes with "
                "its speaker, so short lines like \"Yes.\" are not credited to the wrong creature, and components that "
                "are not installed are counted too.\n\nExperimental. Needs both the game folder and the mod folder.")

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
    pic_chk = ttk.Checkbutton(chk1, text="Show mod's own portraits only", variable=modpic_var, command=modpic_changed)
    pic_chk.pack(side="left", padx=(14, 0))
    tooltip(pic_chk, "Gallery only, and only with a mod folder chosen.\n\nOff: each NPC as it looks in the game now, "
            "which includes pictures another mod or the installer replaced.\nOn: only the .bmp files the mod folder "
            "itself holds; an NPC the mod ships no picture for shows none.")
    btns = ttk.Frame(top)
    btns.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
    go_btn = tk.Button(btns, text="Analyze", bg="#2e9e4f", fg="white", activebackground="#25803f",
                       activeforeground="white", font=("TkDefaultFont", 10, "bold"), padx=14, pady=3, relief="raised")
    go_btn.pack(side="left")
    save_btn = ttk.Button(btns, text="Save as text file...", state="disabled")
    save_btn.pack(side="right")
    csv_btn = ttk.Button(btns, text="Save as CSV...", state="disabled")
    csv_btn.pack(side="right", padx=6)
    merge_chk = ttk.Checkbutton(btns, text="Merge same names", variable=merge_var,
                                state="disabled" if source_var.get() else "normal")
    merge_chk.pack(side="right", padx=12)
    tooltip(merge_chk, "Treat every dialog whose creature has the same name as one NPC.\n\nOff (usual): they are "
            "merged only when they also share a creature file or portrait, or do not differ in race, gender and "
            "animation - so two different \"Guard\" creatures stay apart.")
    view_var = tk.StringVar(value="table")
    sty.configure("View.Toolbutton", padding=(12, 4))
    ttk.Radiobutton(btns, text="Table", value="table", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(16, 0))
    ttk.Radiobutton(btns, text="Gallery", value="gallery", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(2, 0))
    gen_tab_btn = ttk.Radiobutton(btns, text="Generate", value="generate", variable=view_var, style="View.Toolbutton",
                                  command=lambda: show_view())
    gen_tab_btn.pack(side="left", padx=(2, 0))
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

    content = ttk.Frame(root)
    content.pack(fill="both", expand=True, padx=8, pady=4)
    tab_table, tab_gal = ttk.Frame(content), ttk.Frame(content)
    tab_gen, tab_exp = ttk.Frame(content), ttk.Frame(content)

    cols = [k for k, _ in COLUMNS]
    tree = ttk.Treeview(tab_table, columns=cols + ["psize", "proposed"], show="headings", selectmode="browse")
    vs = ttk.Scrollbar(tab_table, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.pack(side="left", fill="both", expand=True)
    vs.pack(side="right", fill="y")
    widths = {"name": 260, "lines": 70, "portrait": 220, "race": 110, "sex": 80, "anim": 220, "cre": 200, "area": 190, "proposed": 190, "psize": 150}
    for k, h in COLUMNS:
        tree.heading(k, text=h, command=lambda c=k: sort_by(c))
        tree.column(k, width=widths[k], anchor="e" if k == "lines" else "w")
    tree.heading("psize", text="Portrait size (L / M)")
    tree.column("psize", width=widths["psize"], anchor="w")
    tree.heading("proposed", text="Proposed portrait")
    tree.column("proposed", width=widths["proposed"], anchor="w")

    SORTS = [("Lines, most first", ("lines", True)), ("Lines, fewest first", ("lines", False)),
             ("Name, A to Z", ("name", False)), ("Name, Z to A", ("name", True)),
             ("Portrait", ("portrait", False)), ("Area", ("area", False))]

    props: Dict[str, str] = {}                      # proposed portrait image per creature (key = its dialogs)
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
    party_var = tk.BooleanVar(value=bool(st.get("party_npcs", True)))
    party_cb = ttk.Checkbutton(gbar, text="Party NPCs", variable=party_var)
    party_cb.pack(side="left", padx=(12, 0))
    tooltip(party_cb, "NPCs who can join the party: the game's own and any a mod adds, found in the party dialog "
                      "tables (pdialog.2da, interdia.2da).\n\nOn: they are listed like everybody else.\nOff: they are "
                      "left out of the table, the gallery, the reports and the exports - they nearly always have a "
                      "portrait already.\n\nA creature that shares a script name or dialog with one of them can be "
                      "caught by this too.")

    ebar = ttk.Frame(tab_exp, padding=(0, 0, 0, 6))                  # the Proposals tab: what you have chosen so far
    ebar.pack(side="top", fill="x")
    sty.configure("Export.TButton", font=("TkDefaultFont", 9, "bold"))
    export_btn = ttk.Button(ebar, text="Export proposed changes...", style="Export.TButton")
    export_btn.pack(side="left")
    mod_btn = ttk.Button(ebar, text="Create mod...")
    mod_btn.pack(side="left", padx=(6, 0))
    clear_btn = ttk.Button(ebar, text="Clear all proposals")
    clear_btn.pack(side="left", padx=6)
    ttk.Label(ebar, textvariable=prop_label, foreground="#555", font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=10)
    hist_btn = ttk.Button(ebar, text="Saved sets...")
    hist_btn.pack(side="left", padx=(6, 0))
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
        if view_var.get() == "gallery" and state["res"] and per_row_now() != state.get("per_row"):
            if state.get("resize_job"):
                root.after_cancel(state["resize_job"])                   # type: ignore[arg-type]
            state["resize_job"] = root.after(150, lambda: build_gallery(sorted_rows()))

    gcanvas.bind("<Configure>", on_gal_resize)
    root.bind("<MouseWheel>", lambda e: gcanvas.yview_scroll(int(-e.delta / 120), "units") if view_var.get() == "gallery" else None)

    def gal_key(what: str, n: int = 1) -> None:
        if view_var.get() == "gallery":
            if what == "page":
                gcanvas.yview_scroll(n, "pages")
            elif what == "home":
                gcanvas.yview_moveto(0)
            else:
                gcanvas.yview_moveto(1)

    def gal_arrow(e: object, n: int) -> None:
        if view_var.get() == "gallery" and e.widget.winfo_class() not in ("TEntry", "Entry"):   # type: ignore[attr-defined]
            gcanvas.yview_scroll(n, "units")

    root.bind("<Up>", lambda e: gal_arrow(e, -3))
    root.bind("<Down>", lambda e: gal_arrow(e, 3))
    root.bind("<Prior>", lambda e: gal_key("page", -1))
    root.bind("<Next>", lambda e: gal_key("page", 1))
    root.bind("<Control-Home>", lambda e: gal_key("home"))
    root.bind("<Control-End>", lambda e: gal_key("end"))

    def show_view() -> None:
        for t in (tab_table, tab_gal, tab_gen, tab_exp):
            t.pack_forget()
        if view_var.get() == "gallery":
            tab_gal.pack(fill="both", expand=True)
            root.update_idletasks()
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
                    gen_hint("Making pictures needs an image generator, which is not part of the Portrait Scout "
                             "download. See generator/README.md for the setup.")
                else:
                    generate_dialog(rows[idx] if 0 <= idx < len(rows) else None)
        else:
            tab_table.pack(fill="both", expand=True)

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
        if not rows:
            etree.insert("", "end", values=("(nothing proposed yet - use \"Propose portrait...\" or the Generate tab)",
                                            "", "", "", ""))

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
            m.add_command(label="Work on this creature in the Generate tab", command=lambda: generate_dialog(r))
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
    LORA_DL = "https://civitai.com/models/2071031/baldurs-gate-2-portrait-style-flux"

    def lora_files() -> List[str]:
        d = Path(__file__).resolve().parent / "engine" / "webui" / "models" / "Lora"
        try:
            return sorted(p.name for p in d.iterdir() if p.suffix.lower() in (".safetensors", ".pt", ".ckpt"))
        except OSError:
            return []

    def engine_ready() -> bool:
        return (Path(__file__).resolve().parent / "engine" / "webui").is_dir()

    def open_make(path: str, *fallbacks: str) -> None:
        """Open a folder of the engine, making it if it is not there yet (the deeper ones appear as Forge unpacks)."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            path = next((p for p in fallbacks if os.path.isdir(p)), path)
        open_path(path)

    def setup_panel(parent: object, again: object = None) -> object:
        """What to download and where to put it, for someone who has just cloned the repository."""
        here = Path(__file__).resolve().parent
        BG = "#fff8e1"
        box = tk.Frame(parent, bg=BG, bd=1, relief="solid", padx=10, pady=8)   # type: ignore[arg-type]
        box.columnconfigure(0, weight=1)
        head = tk.Frame(box, bg=BG)
        head.grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(head, text="No image generator here yet", bg=BG, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        if callable(again):
            ttk.Button(head, text="Check again", command=again).pack(side="left", padx=10)   # type: ignore[arg-type]
        tk.Label(box, bg=BG, justify="left", wraplength=860, fg="#5a4a00",
                 text="Portrait Scout builds the prompt, sends it, and turns the picture into a game portrait. The "
                      "generator itself and its models are many gigabytes and separately licensed, so they are not in "
                      "the repository. Set one up once:").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))

        def step(n: int, text: object, buttons: List[Tuple[str, object]]) -> None:
            """text: a string, or pieces of one where (piece, True) is shown in bold."""
            pieces = [(text, False)] if isinstance(text, str) else list(text)   # type: ignore[arg-type]
            t = tk.Text(box, bg=BG, width=78, height=2, wrap="word", bd=0, highlightthickness=0,
                        font=("TkDefaultFont", 9), cursor="arrow", spacing3=2)
            t.tag_configure("b", font=("TkDefaultFont", 9, "bold"))
            for piece, bold in pieces:
                t.insert("end", piece, ("b",) if bold else ())
            t.update_idletasks()
            try:                                                     # as tall as the text actually needs
                lines = t.count("1.0", "end", "displaylines")[0]      # type: ignore[index]
            except (tk.TclError, TypeError, IndexError):
                lines = max(1, len("".join(p for p, _b in pieces)) // 76 + 1)
            t.config(height=max(1, int(lines)), state="disabled")
            t.grid(row=n + 1, column=0, sticky="w", pady=2)
            if buttons:
                f = tk.Frame(box, bg=BG)
                f.grid(row=n + 1, column=1, sticky="w", padx=(10, 0))
                for label, cmd in buttons:
                    ttk.Button(f, text=label, command=cmd).pack(side="left", padx=(0, 6))   # type: ignore[arg-type]
        step(1, [("1.  Download Forge's Windows one-click package (webui_forge_cu121_torch231.7z, 1.7 GB) and ", 0),
                 ("unpack it into " + str(here / "engine"), 1),
                 (", so that you end up with engine\\webui\\ and engine\\system\\, with run.bat in engine\\.", 0)],
             [("Download Forge", lambda: webbrowser.open(FORGE_DL)),
              ("(releases)", lambda: webbrowser.open(FORGE_ALL)),
              ("Open folder", lambda: open_make(str(here / "engine"), str(here)))])
        step(2, [("2.  Run ", 0), ("engine\\update.bat", 1), (" once, then ", 0), ("engine\\run.bat", 1),
                 (" once and wait for the Forge page to open. That proves it works, then close it again.", 0)], [])
        step(3, [("3.  Put ", 0), ("flux1-dev-bnb-nf4-v2.safetensors", 1), (" (11.5 GB) in "
                  "engine\\webui\\models\\Stable-diffusion\\. A LoRA is optional but recommended: ", 0),
                 ("BG_Portrait.safetensors", 1), (" (38 MB) in engine\\webui\\models\\Lora\\.", 0)],
             [("Download model", lambda: webbrowser.open(FLUX_DL)),
              ("Download LoRA", lambda: webbrowser.open(LORA_DL)),
              ("Open folder", lambda: open_make(str(here / "engine" / "webui" / "models")
                                                if (here / "engine" / "webui").is_dir() else str(here / "engine"),
                                                str(here / "engine"), str(here)))])
        step(4, "4.  Press Check again, at the top. Portrait Scout starts and stops the generator by itself after that.", [])
        tk.Label(box, bg=BG, fg="#5a4a00", justify="left", wraplength=860,
                 text="Already run a generator of your own? None of the above is needed: open Generator settings "
                      "below, put its address in, and turn \"start it for me\" off. Anything with an Automatic1111 / "
                      "Forge API will do, with any model.").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        more = tk.Frame(box, bg=BG)
        more.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(more, text="More information:", bg=BG, fg="#5a4a00").pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Forge: what it is, and its install notes",
                   command=lambda: webbrowser.open(FORGE_DOC)).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Other Forge builds (CUDA / Torch)",
                   command=lambda: webbrowser.open(FORGE_ALL)).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="Portrait Scout's setup notes",
                   command=lambda: open_path(str(here / "generator" / "README.md"))).pack(side="left", padx=(0, 6))
        ttk.Button(more, text="The Portrait Scout folder", command=lambda: open_path(str(here))).pack(side="left")
        return box

    def saved_sets() -> None:
        """Every set of proposals this copy of Portrait Scout has kept, with the chance to throw one away."""
        saved = st.get("proposals") or {}
        w = tk.Toplevel(root)
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
        ttk.Button(row, text="Forget the selected set", command=forget).pack(side="left")
        ttk.Button(row, text="Close", command=w.destroy).pack(side="right")
    hist_btn.config(command=saved_sets)

    def gen_hint(text: str = "") -> None:
        """Empty the Generate tab (so it is built again next time it is shown), with a message if there is one."""
        state["gen_built"] = False
        for w in tab_gen.winfo_children():
            w.destroy()
        if not text:
            return
        box = ttk.Frame(tab_gen, padding=20)
        box.pack(anchor="w", fill="x")
        ttk.Label(box, foreground="#555", justify="left", wraplength=840, text=text).pack(anchor="w", pady=(0, 10))
        setup_panel(box, lambda: (state.__setitem__("gen", None), gen_hint(), show_view())).pack(anchor="w", fill="x")

    show_view()

    def table_double(_e: object) -> None:
        sel = tree.selection()
        if sel:
            idx = tree.index(sel[0])
            rows = sorted_rows()
            if 0 <= idx < len(rows):
                show_lines(rows[idx])

    tree.bind("<Double-Button-1>", table_double)

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
        terms = search_var.get().lower().split() if filtered else []
        if terms:
            rows = [r for r in rows if all(t in row_text(r) for t in terms)]
        rows.sort(key=lambda r: (r[key] if key == "lines" else str(r[key]).lower()), reverse=desc)
        return rows

    def refresh() -> None:
        tree.delete(*tree.get_children())
        rows = sorted_rows()
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

                          + f"{sum(r['lines'] for r in res_['rows']):,} line(s)"                     # type: ignore[index]
                          + ((" - from the mod's files" + (", EET" if res_.get("eet") else "")          # type: ignore[union-attr]
                              + (f", {unk} dialog(s) without a speaker" if unk else "")) if res_.get("source") else ""))   # type: ignore[union-attr]
        if view_var.get() == "gallery":
            build_gallery(rows)

    def on_search(*_a: object) -> None:
        if state.get("search_job"):
            root.after_cancel(state["search_job"])                   # type: ignore[arg-type]

        def go() -> None:
            state["search_job"] = None
            state["page"] = 0
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
            return f"Merge {n} creatures", f"Put the {n} \"{par['name']}\" creatures back in one row."
        return f"Declone {n} creatures", (f"List the {n} \"{par['name']}\" creatures one by one, so each can have a "
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
        except (OSError, AttributeError, ValueError):
            names = set()
        for r in [x for r_ in res["rows"] for x in ([r_] + (r_.get("parts") or []))]:
            r["party"] = bool(names and ({d.upper() for d in r["dialogs"]} & names
                                         or {c.upper() for c in (r.get("cre_list") or [])} & names))

    def rowkey(r: dict) -> str:
        return ",".join(sorted(set(r["dialogs"])))

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
        return [str(r[k]) for k in cols] + [psize, prop_txt]

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

    def image_menu(event: object, r: dict, parent: object) -> None:
        res = state["res"]
        if not res:
            return
        game: Game = res["game"]                                     # type: ignore[index]
        menu = tk.Menu(parent, tearoff=0)
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
        lbl, why = declone_label(r)
        if lbl:
            menu.add_separator()
            menu.add_command(label=f"{lbl}  -  {why}", command=lambda: toggle_declone(r))
        try:
            menu.tk_popup(event.x_root, event.y_root)                # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def update_prop_label() -> None:
        res = state["res"]
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
        """The Generate tab, with the problem shown in it if anything goes wrong while building it."""
        try:
            build_generate(r, after, parent)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            gen_hint(f"The Generate tab could not be built: {e.__class__.__name__}: {e}")

    def build_generate(r: Optional[dict] = None, after: object = None, parent: object = None) -> None:
        """Describe the creature from the game data, let the user add details, and ask the generator for candidates.
        Without a creature (r = None) it is a plain picture maker: type what you want and press Generate."""
        pg = generator_module()
        res = state["res"]
        if pg is None:
            gen_hint("Making pictures needs an image generator, which is not part of the Portrait Scout download. "
                     "See generator/README.md.")
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
        character = "" if generic else pg.describe(r, gs)[1]         # type: ignore[attr-defined]
        folder = Path(__file__).resolve().parent / str(gs.get("output_folder", "generated")) / re.sub(r"[^A-Za-z0-9_.#!-]+", "_", mod_name)
        stem = re.sub(r"[^A-Za-z0-9_.#!-]+", "_", f"{r['name']}_{(r.get('cre_list') or [''])[0]}").strip("_") or "portrait"

        for w in tab_gen.winfo_children():                           # the Generate tab works on one creature at a time
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
        ttk.Label(head_, text=r["name"], font=("TkDefaultFont", 11, "bold")).pack(side="left")
        if not generic:
            ttk.Button(head_, text="Unselect", width=9, command=lambda: generate_dialog()).pack(side="left", padx=(8, 0))
        ttk.Label(head_, foreground="#666",
                  text=("no creature: type what you want below - nothing is proposed for a creature" if generic else
                        "  ".join(x for x in (r["race"], r["sex"], r["anim"]) if x) or "(no creature details)")
                  ).pack(side="left", padx=10)
        ttk.Label(head_, text=", ".join(r.get("cre_list") or []) or "", foreground="#888").pack(side="left")

        eng_s = gs.get("engine") or {}
        if not engine_ready() and eng_s.get("autostart", True) and "start_engine.bat" in str(eng_s.get("start", "")):
            setup_panel(dlg, lambda: generate_dialog(None if generic else r, after, parent)).pack(fill="x", padx=10, pady=(6, 0))
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Description:").grid(row=1, column=0, sticky="w")
        desc0 = str(saved.get("desc") or character)
        if saved.get("extra") and str(saved["extra"]).strip() not in desc0:   # from when extra details had their own box
            desc0 = ", ".join(x for x in (desc0, str(saved["extra"]).strip()) if x)
        desc_var = tk.StringVar(value=desc0)
        ttk.Entry(frm, textvariable=desc_var).grid(row=1, column=1, sticky="ew", padx=6)
        dbtn = ttk.Frame(frm)
        dbtn.grid(row=1, column=2)
        reset_btn = ttk.Button(dbtn, text="Reset", width=6,
                               command=(lambda: pick_reset()) if generic else (lambda: desc_var.set(character)))
        tooltip(reset_btn, "Put the description back to what the game data says." if not generic else
                "Back to Human, Male, Fighter, Adult.")

        def big_edit(var: tk.StringVar, title: str) -> None:
            """A roomy box for text too long to read in one line."""
            w = tk.Toplevel(root)
            w.title(title)
            w.geometry("700x240")
            t = tk.Text(w, wrap="word", font=("TkDefaultFont", 10))
            t.pack(fill="both", expand=True, padx=8, pady=8)
            t.insert("1.0", var.get())
            t.focus_set()
            row_ = ttk.Frame(w, padding=(8, 0, 8, 8))
            row_.pack(fill="x")

            def ok() -> None:
                var.set(" ".join(t.get("1.0", "end-1c").split()))
                w.destroy()
            ttk.Button(row_, text="OK", command=ok).pack(side="right")
            ttk.Button(row_, text="Cancel", command=w.destroy).pack(side="right", padx=6)
        ttk.Button(dbtn, text="\u2922", width=3, command=lambda: big_edit(desc_var, "Description")).pack(side="left")
        reset_btn.pack(side="left", padx=(3, 0))
        prow0 = ttk.Frame(frm)                                       # what the description is built from
        prow0.grid(row=1, column=3, sticky="w", padx=(8, 0))
        RACES = ["Human", "Elf", "Half-Elf", "Dwarf", "Halfling", "Gnome", "Half-Orc"]
        CLASSES = ["Fighter", "Paladin", "Ranger", "Barbarian", "Monk", "Cleric", "Druid", "Shaman",
                   "Mage", "Sorcerer", "Thief", "Bard"]
        last = st.get("generic_pick") if isinstance(st.get("generic_pick"), list) else []
        last = (list(last) + ["Human", "Male", "Fighter"])[:3]
        race_v = tk.StringVar(value=str(saved.get("race") or last[0]))
        sex_v = tk.StringVar(value=str(saved.get("sex") or last[1]))
        class_v = tk.StringVar(value=str(saved.get("class") or last[2]))
        ages = pg.term_names(gs, "age_terms") or ["Adult"]           # type: ignore[attr-defined]
        looks = pg.term_names(gs, "look_terms") or ["Ordinary"]      # type: ignore[attr-defined]
        age_v = tk.StringVar(value=str(saved.get("age") or st.get("last_age") or ("Adult" if "Adult" in ages else ages[0])))
        look_v = tk.StringVar(value=str(saved.get("look") or st.get("last_look") or looks[0]))

        def pick_changed(*_a: object) -> None:
            if generic:
                race, sex, cls = race_v.get() or "Human", sex_v.get() or "Male", class_v.get() or "Fighter"
                row_ = {"race": race, "sex": sex, "anim": f"{cls} {sex} {race}"}
                st["generic_pick"] = [race, sex, cls]                 # remembered until Reset
            else:
                row_ = r
            st["last_age"], st["last_look"] = age_v.get(), look_v.get()
            save_settings(st)
            desc_var.set(pg.describe(row_, gs, age_v.get(), look_v.get())[1])   # type: ignore[attr-defined]

        def pick_reset() -> None:
            st.pop("generic_pick", None)
            race_v.set("Human")
            sex_v.set("Male")
            class_v.set("Fighter")
            age_v.set("Adult" if "Adult" in ages else ages[0])
            look_v.set(looks[0])
            pick_changed()
        picks = ([(race_v, RACES, 10), (sex_v, ["Male", "Female"], 8), (class_v, CLASSES, 10)] if generic else []) \
            + [(age_v, ages, 11), (look_v, looks, 11)]
        for var_, vals_, w_ in picks:
            cb_ = ttk.Combobox(prow0, textvariable=var_, values=vals_, state="readonly", width=w_)
            cb_.pack(side="left", padx=(0, 4))
            cb_.bind("<<ComboboxSelected>>", pick_changed)
        tooltip(prow0, ("Race, gender and class, then how old the character looks and anything about the face - "
                        if generic else "How old the character looks, and anything about the face - ")
                       + "they are written into the description. Flux paints clean, young skin unless it is told "
                         "otherwise, so \"Old\" and \"Weathered\" are worth a try.")
        if generic and not desc_var.get().strip():
            pick_changed()
        brow = ttk.Frame(frm)                                        # background, and what to make from it
        brow.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        bg_lab = ttk.Label(brow, text="Background:")
        bg_lab.pack(side="left")
        bg_var = tk.StringVar()
        tooltip(bg_lab, "The prompts put this in where they say {background}, so one change covers every look that "
                        "uses it. Empty it and the sentence about the background drops out of the prompt.")
        have_lora = bool(lora_files()) or not engine_ready()       # an engine of your own: no way to tell, so show it
        lora_var = tk.BooleanVar(value=bool(st.get("use_lora", True)) and have_lora)

        def lora_changed() -> None:
            st["use_lora"] = bool(lora_var.get())
            save_settings(st)
        if have_lora:                                                # packed right to left: LoRA ends up last
            lora_cb = ttk.Checkbutton(brow, text="LoRA", variable=lora_var, command=lora_changed)
            lora_cb.pack(side="right", padx=(6, 0))
            tooltip(lora_cb, "Off: the <lora:...> part is taken out of the prompt, so only the model itself paints "
                             "the picture. A LoRA pushes hard towards what it was trained on, which can cost a "
                             "creature its own features (horns, green skin).")
        styles = pg.style_names(gs) if hasattr(pg, "style_names") else []   # type: ignore[attr-defined]
        style_var = tk.StringVar(value=st.get("portrait_style") if st.get("portrait_style") in styles
                                 else (gs.get("style") if gs.get("style") in styles else (styles[0] if styles else "")))
        style_box = ttk.Combobox(brow, textvariable=style_var, values=styles, state="readonly", width=22)
        style_box.pack(side="right", padx=(4, 0))
        ttk.Label(brow, text="Style:").pack(side="right", padx=(12, 0))
        count_var = tk.IntVar(value=int(saved.get("count") or gs.get("candidates", 2) or 2))
        ttk.Spinbox(brow, from_=1, to=8, width=3, textvariable=count_var).pack(side="right", padx=(4, 0))
        ttk.Label(brow, text="Candidates:").pack(side="right", padx=(14, 0))
        ttk.Button(brow, text="\u2922", width=3,
                   command=lambda: big_edit(bg_var, "Background")).pack(side="right", padx=(4, 0))
        ttk.Entry(brow, textvariable=bg_var).pack(side="left", fill="x", expand=True, padx=(6, 0))
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
        gstat = tk.StringVar(value="Type what the portrait should show, then press Generate." if generic else
                             "Description comes from the creature's gender, race and animation type - edit it freely.")

        def prompt_now() -> Tuple[str, str]:
            d = desc_var.get().strip()
            subj = re.sub(r"^(adult|young)\s+", "", d.split(",")[0].strip())
            tmpl = ptext.get("1.0", "end-1c")
            if not lora_var.get():
                tmpl = re.sub(r"\s*<lora:[^>]*>", "", tmpl)          # paint without the LoRA
            return (pg.fill_prompt(tmpl, subj, d, "", bg_var.get()),   # type: ignore[attr-defined]
                    ntext.get("1.0", "end-1c"))

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
        ttk.Label(crow, text="Head zoom:").pack(side="left")
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

        for txt, dx, dy in (("\u25c0", -0.02, 0), ("\u25b6", 0.02, 0), ("\u25b2", 0, -0.02), ("\u25bc", 0, 0.02)):
            mv_ = ttk.Button(crow, text=txt, width=2, command=lambda a=dx, b=dy: shift(a, b))
            mv_.pack(side="left", padx=1)
            tooltip(mv_, "Move the crop box on every candidate at once. \"Middle\" puts it back.")
        mid_btn = ttk.Button(crow, text="Middle", width=7, command=lambda: shift(0, 0))
        mid_btn.pack(side="left", padx=(4, 8))
        tooltip(mid_btn, "Put the box back in the middle. The arrows move it on every candidate at once; the picture "
                         "that \"Use this\" makes is exactly what the box shows.")
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
            if path.lower().endswith(".bmp"):                        # only one game portrait at a time: the old one goes
                for c in [c for c in cells if str(c["path"]).lower().endswith(".bmp")
                          and same_(c["path"]) != same_(path)]:
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
            v = tk.Toplevel(dlg)
            v.title(Path(path).name)
            try:
                shown, fw, fh, k = full_image(path, max(300, v.winfo_screenheight() - 140))
                if shown is None:                                    # a .bmp Tk cannot read and Pillow is not here
                    raise tk.TclError("could not read the picture")
                lab = tk.Label(v, image=shown, cursor="hand2")
                lab.image = shown                                    # type: ignore[attr-defined]
                lab.pack()
                lab.bind("<Button-1>", lambda e: v.destroy())
                lab.bind("<Button-3>", lambda e: save_image(v, Path(path).name, None, path))
                ttk.Label(v, text=f"{fw}x{fh}" + (f", shown at {int(round(k * 100))}%" if k < 0.999 else "")
                          + "   -   click to close, right-click to save a copy", foreground="#666").pack(pady=2)
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
            show_ = zm > 1.005 or nudge["dx"] or nudge["dy"]       # at zoom 1, centred, the box is the whole picture
            cropnote.set("the yellow box is what the game portrait keeps" if show_ else "")
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

        def cand_menu(event: object, path: str) -> None:
            menu = tk.Menu(dlg, tearoff=0)
            menu.add_command(label=f"Save {Path(path).name} as...", command=lambda: save_image(dlg, Path(path).name, None, path))
            menu.add_command(label="Open full size", command=lambda: view_full(path))
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

        def relist() -> None:
            """Draw the candidate strip again, at whatever size the Full size box now says."""
            paths = [str(c["path"]) for c in cells]
            for c in list(cells):
                c["cell"].destroy()
            cells.clear()
            state["cand_cols"] = None
            for q in paths:
                add_candidate(q)

        def add_candidate(path: str) -> None:
            if not dlg.winfo_exists():
                return
            cell = ttk.Frame(cands, padding=4)
            cols_ = state.get("cand_cols") or cand_cols()
            cell.grid(row=len(cells) // cols_, column=len(cells) % cols_, sticky="n")   # type: ignore[operator]
            info = {"path": path, "cell": cell, "image": None}
            done_bmp = path.lower().endswith(".bmp")                 # a game portrait made from one of the candidates
            try:
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
                else:                                                # all the same, so they compare side by side
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
                ttk.Label(cell, text=(f"game portrait {rw}x{rh}" if done_bmp else f"{rw}x{rh}"),
                          foreground="#1a7f37" if done_bmp else "#888",
                          font=("TkDefaultFont", 9, "bold") if done_bmp else "TkDefaultFont").pack()
                info.update(image=pi, canvas=cvs, sw=rw, sh=rh,
                            tw=pi.width(), th=pi.height())
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
            catch_wheel(cell)

        def files_of(path: str) -> List[str]:
            """A generated picture and the game-size .bmp copies made from it."""
            base = Path(path).stem + "_"
            out = [path]
            try:
                out += [str(q) for q in Path(path).parent.iterdir()
                        if q.name.startswith(base) and q.suffix.lower() == ".bmp"]
            except OSError:
                pass
            return out

        def discard(paths: List[str], confirm: str = "") -> None:
            """Delete generated pictures from the disk and take them off the window."""
            files = [f for q in paths for f in files_of(q)]
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

        def discard_all() -> None:
            try:
                every = [str(q) for q in folder.glob(f"{stem}_c*.png")] + [str(q) for q in folder.glob(f"{stem}_c*.bmp")]
            except OSError:
                every = []
            if not every:
                gstat.set("Nothing to discard.")
                return
            discard(every, f"Delete all {len(every)} generated picture(s) for {r['name']}? This cannot be undone.")
        ttk.Button(crow, text="Discard all", command=discard_all).pack(side="left", padx=(10, 0))

        def done(err: str) -> None:
            status.set(f"Portrait for {r['name']}: " + (err if err else "candidates ready"))
            if not dlg.winfo_exists():
                return
            if busy["spin"]:
                gbar_.stop()
                busy["spin"] = False
            gbar_.pack_forget()
            gen_btn.config(state="normal", bg="#2e9e4f")
            gstat.set(err if err else "Done. Press \"Use this\" under the one you want, or Generate for more.")
            engine_state()

        def run() -> None:
            try:
                n = max(1, min(8, int(count_var.get())))
            except (tk.TclError, ValueError):
                n = 2
            notes[key] = {"desc": desc_var.get().strip(), "count": n, "age": age_v.get(), "look": look_v.get()}
            if generic:
                notes[key].update(race=race_v.get(), sex=sex_v.get(), **{"class": class_v.get()})
            st["portrait_style"] = style_var.get()
            save_settings(st)
            pr, ng = prompt_now()
            eng = get_engine()
            gen_btn.config(state="disabled", bg="#7fae8d")

            def work() -> None:
                try:
                    def on_image(i: int, data: bytes) -> None:
                        pth = pg.save_candidates([data], folder, f"{stem}_c{i + 1}")[0]   # type: ignore[attr-defined]
                        root.after(0, add_candidate, pth)
                    sd = None if generic else pg.creature_seed(pg.face_key(r))   # type: ignore[attr-defined]
                    eng.generate(pr, ng, n, on_image, sd)             # type: ignore[attr-defined]
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
        emenu.add_command(label="Edit settings.json...",
                          command=lambda: open_path(str(here_ / "generator" / "settings.json")))
        ebtn = ttk.Menubutton(erow2, text="Engine \u25be", menu=emenu, width=9)
        ebtn.pack(side="left", padx=(6, 0))
        tooltip(ebtn, "Keep open: leave the generator running when Portrait Scout closes, so the next session does "
                      "not wait for the model to load again - it holds graphics memory until you press Stop.\n\n"
                      "Own window: watch what the generator is doing; takes effect the next time it starts. With it "
                      "off the same text goes to generator/engine.log, which 'Show the log' opens.")

        def engine_state() -> None:
            eng = get_engine()

            def show(ok: bool) -> None:
                if dlg.winfo_exists():
                    eng_var.set("running" if ok else "not running")
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
        ttk.Button(pbtns, text="Save as new style...", command=save_as).pack(side="left", padx=6)
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
        ttk.Checkbutton(sbody, text="start it for me", variable=auto_var).grid(row=0, column=2, sticky="w", padx=8)
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
        show_settings()
        start_btn.config(command=lambda: engine_job(False))
        stop_btn.config(command=lambda: engine_job(True))
        engine_state()

        try:                                                         # candidates made earlier for this creature
            old = sorted(list(folder.glob(f"{stem}_c*.png")) + list(folder.glob(f"{stem}_c*.bmp")),
                         key=lambda q: q.stat().st_mtime)[-12:]
        except OSError:
            old = []
        for q in old:
            add_candidate(str(q))

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
        ttk.Button(row, text="Use it for the picked ones", command=use_them).pack(side="left")
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

        def centre() -> None:
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
        ttk.Button(row, text="Middle", command=centre).pack(side="left")
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

    def ask_mod_folder(mod_name: str) -> str:
        """Which mod to write, and where. The default is a folder of its own under the project's 'mod' folder."""
        here = Path(__file__).resolve().parent
        out: Dict[str, str] = {}
        w = tk.Toplevel(root)
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
        row = ttk.Frame(f)
        row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        def ok() -> None:
            nm = tidy(name_v.get())
            if not nm:
                messagebox.showwarning("Portrait Scout", "The mod needs a name.", parent=w)
                return
            out["dir"] = os.path.join(folder["dir"], nm)
            st["mod_export_name"], st["mod_export_parent"] = nm, folder["dir"]
            save_settings(st)
            w.destroy()
        ttk.Button(row, text="Export", command=ok).pack(side="left")
        ttk.Button(row, text="Put it somewhere else...", command=elsewhere).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="left")
        ttk.Label(f, foreground="#888", wraplength=460, justify="left", padding=(0, 10, 0, 0),
                  text="Exporting again with the same name updates that mod; a different name starts another one. "
                       "Proposals stay where they are either way.").grid(row=4, column=0, columnspan=3, sticky="w")
        w.grab_set()
        root.wait_window(w)
        return out.get("dir", "")

    def export_as_mod() -> None:
        res = state["res"]
        rows = [r for r in sorted_rows(False) if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet. Use 'Propose portrait...' or 'Generate...' first.")
            return
        d = ask_mod_folder(str(res["mod"]))                          # type: ignore[index]
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
            info = export_mod(d, str(res["mod"]),
                               [{"name": r["name"], "cres": r["cre_list"], "image": props[rowkey(r)],
                                 "areas": areas_text(r), "box": (crop_for(r) or {}).get("box")} for r in rows],
                               convert, game_var.get())
        except (OSError, ValueError) as e:
            messagebox.showwarning("Portrait Scout", f"Could not export the mod:\n\n{e}")
            return
        name = Path(d).name
        msg = (f"Exported {info['portraits']} portrait(s) for {info['creatures']} creature file(s) of {res['mod']}.\n\n"   # type: ignore[index]
               f"{d}\n  setup-{name}.tp2   the installer\n  readme.txt, portraits.txt   what is in it\n"
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
    clear_btn.config(command=clear_all)

    def build_gallery(rows: List[dict]) -> None:
        loading_var.set("Loading...")
        root.update_idletasks()
        try:
            _build_gallery(rows)
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
            img = load_thumb(prop) if prop else None
            shown_field = "prop" if img else ""
            src_mode = hasattr(game, "get_mod")                       # the mod's own files were read
            mod_idx = mod_bmp_index() if modpic_var.get() and not src_mode else {}
            mod_only = bool(modpic_var.get()) and (src_mode or bool(mod_idx))
            mod_file = ""
            for field in ("small", "large"):
                if img:
                    break
                if mod_only and src_mode:
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
            if prop and not load_thumb(prop):
                tk.Label(card, text=f"{Path(prop).name}\n(preview needs PNG/GIF/BMP,\nor install Pillow)", bg="#cfe8d6",
                         width=18, height=6).pack()
            elif img:
                il = tk.Label(card, image=img, bg=CARD)
                il.pack()
                il.bind("<Button-3>", lambda e, rr=r: image_menu(e, rr, root))
            else:
                tk.Label(card, text="no portrait\nfrom this mod" if mod_only else
                         "no portrait" if not (r["large"] or r["small"]) else "(file not found)",
                         width=14, height=6, bg="#d4d7dc").pack()
            sz = (prop_dims(prop) if shown_field == "prop" else r.get(f"{shown_field}_size", "")) if shown_field else ""
            if mod_only and shown_field in ("small", "large"):
                sz = bmp_file_dims(mod_file) if mod_file else r.get("mod_sizes", {}).get(shown_field, "")
            if sz:
                tk.Label(card, text=sz, bg=CARD, fg="#888", font=("TkDefaultFont", 8)).pack()
            nl = tk.Label(card, text=r["name"], bg=CARD, wraplength=CARD_W - 30, justify="center", cursor="hand2",
                          font=("TkDefaultFont", 9, "bold"))
            nl.pack()
            nl.bind("<Button-1>", lambda e, n=r["name"]: copy_text(n))                    # click the name: copy it
            tk.Label(card, text=f"{r['lines']} lines", bg=CARD, fg="#666").pack()
            if prop:                                                 # the green border says it is a proposal already
                tooltip(card, f"Proposed: {Path(prop).name}" + (f"  ({prop_dims(prop)})" if prop_dims(prop) else "")
                        + "\n\nDouble-click the card to see it, frame it, or clear it.")
            sub = " ".join(x for x in (r["race"], r["sex"]) if x)
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
                           command=lambda rr=r: propose(rr))
            b1.pack(side="left")
            b1._copy_label = True                                                          # type: ignore[attr-defined]
            if generator_module() is not None:
                bg_ = tk.Button(brow, text="Generate...", font=("TkDefaultFont", 8), command=lambda rr=r: generate_dialog(rr))
                bg_.pack(side="left", padx=(2, 0))
                bg_._copy_label = True                                                     # type: ignore[attr-defined]
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

    def show_lines(r: dict) -> None:
        res = state["res"]
        win = tk.Toplevel(root)
        win.title(f"{r['name']} - {r['lines']} line(s)")
        win.geometry("760x640")
        head = ttk.Frame(win, padding=8)
        head.pack(fill="x")
        img = None
        if res:
            game: Game = res["game"]                                 # type: ignore[index]
            for field in ("small", "large"):
                nm = r[field].split("/")[0] if r[field] else ""
                data = game.get(nm, T_BMP) if nm else None
                img = thumb_from_bytes(data) if data else None       # the same box as a proposed picture
                if img:
                    break
        lb = tk.Label(head, bd=0, highlightthickness=4)              # shows the proposal (green border) or the current portrait
        lb.pack(side="left", padx=(0, 10))
        lb.bind("<Button-3>", lambda e: image_menu(e, r, win))

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
            colour = "#2e9e4f" if pp else win.cget("bg")
            lb.config(highlightbackground=colour, highlightcolor=colour)

        info = ttk.Frame(head)
        info.pack(side="left", fill="x", expand=True)
        nml = ttk.Label(info, text=r["name"], font=("TkDefaultFont", 13, "bold"), cursor="hand2")
        nml.pack(anchor="w")
        nml.bind("<Button-1>", lambda e: copy_text(r["name"]))
        ttk.Label(info, text="  ".join(x for x in (r["race"], r["sex"], r["anim"]) if x), foreground="#666").pack(anchor="w")
        sizes = ", ".join(x for x in ((f"large {r['large_size']}" if r.get("large_size") else ""),
                                      (f"medium {r['small_size']}" if r.get("small_size") else "")) if x)
        ttk.Label(info, text=f"Portrait: {r['portrait']}" + (f"  ({sizes})" if sizes else "")
                  + "     (right-click the picture to save it)", foreground="#666").pack(anchor="w")
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

        ttk.Button(pf, text="Propose portrait...", command=lambda: propose(r, upd_prop, win)).pack(side="left")
        if generator_module() is not None:
            ttk.Button(pf, text="Generate portrait...",
                       command=lambda: (generate_dialog(r, upd_prop, win), win.lower())).pack(side="left", padx=(4, 0))
        clr = ttk.Button(pf, text="Clear proposal", command=clear_here)
        clr.pack(side="left", padx=4)
        frame_btn = ttk.Button(pf, text="Frame...", command=lambda: frame_dialog(r, win, upd_prop))
        frame_btn.pack(side="left", padx=(0, 4))
        tooltip(frame_btn, "Choose which part of the proposed picture becomes the portrait. Without it the middle is "
                           "used, cut to the portrait's shape.")
        same_btn = ttk.Button(pf, text="Use for others...", command=lambda: share_portrait(r, win, upd_prop))
        same_btn.pack(side="left")
        dl_lbl, dl_why = declone_label(r)
        if dl_lbl:
            dl_btn = ttk.Button(pf, text=dl_lbl, command=lambda: (toggle_declone(r), win.destroy()))
            dl_btn.pack(side="left", padx=(8, 0))
            tooltip(dl_btn, dl_why + "\n\nThey were put in one row only because they share a name and look the same. "
                            "Creatures that share a creature file or a portrait are never split.")
        tooltip(same_btn, "Give the proposed picture to other creatures of the same name, or to the ones that share "
                          "this creature's portrait now. Useful after decloning.")
        ttk.Label(pf, textvariable=pl, foreground="#1f7a3a").pack(side="left", padx=8)
        upd_prop()
        ttk.Label(info, text="(Clear or export proposals from the Gallery tab.)", foreground="#888").pack(anchor="w")
        chips = ttk.Frame(info)
        chips.pack(anchor="w", pady=(2, 0))
        ttk.Label(chips, text="Click to copy:", foreground="#888").pack(side="left", padx=(0, 4))
        for kind, names in (("CRE", r["cre_list"]), ("DLG", sorted(set(r["dialogs"])))):
            for nm in names[:8]:
                ch = tk.Label(chips, text=nm, fg="#1a5fb4", cursor="hand2",
                              font=("TkDefaultFont", 9, "bold underline" if (kind == "CRE" and nm in r["cre_known"]) else "underline"))
                ch.pack(side="left", padx=4)
                ch.bind("<Button-1>", lambda e, n=nm: copy_text(n))

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

            # a few notes: open, as that is usually all there is to say. A long list (a well-travelled NPC) starts
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
            ttk.Label(top, text=(r["line_src"].get(sr, "") if r.get("line_src") else f"#{sr}")
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
        state["gen_row"] = None                                      # the Generate tab belonged to the last analysis
        gen_hint()
        props.clear()
        props.update({k: v for k, v in (st.get("proposals", {}).get(res["mod"], {}) or {}).items()          # type: ignore[index]
                      if isinstance(v, str)})
        update_prop_label()
        refresh()
        for b in (save_btn, csv_btn):
            b.config(state="normal")
        extra = ""
        if res.get("source"):                                                        # type: ignore[union-attr]
            unk = res["stats"].get("dialogs not linked", 0)                          # type: ignore[index]
            extra = (" from the mod's files" + (" (EET install)" if res.get("eet") else "")       # type: ignore[union-attr]
                     + (f", {unk} dialog(s) without a speaker" if unk else "")
                     + ("" if res.get("has_game") else " - no game folder, so the game's own NPCs are not named"))   # type: ignore[union-attr]
        out = Path(__file__).resolve().parent / f"{res['mod']}-creatures.txt"       # type: ignore[index]
        try:
            out.write_text(as_text(res, sorted_rows(False)), encoding="utf-8")            # type: ignore[arg-type]
            status.set(f"{len(res['rows'])} creature(s){extra}. Saved {out}")        # type: ignore[index]
        except OSError:
            status.set(f"{len(res['rows'])} creature(s){extra}.")                    # type: ignore[index]

    def analyze_clicked() -> None:
        g, m = game_var.get().strip(), mod_var.get().strip()
        from_source = source_var.get()
        if from_source:
            same = os.path.isdir(m) and os.path.isdir(g) and os.path.normcase(os.path.abspath(m)) == os.path.normcase(os.path.abspath(g))
            if not os.path.isdir(m) or same or not os.path.isdir(g):
                messagebox.showwarning("Portrait Scout", "Reading the mod's own files needs the game folder and the mod's "
                                       "folder (not the whole install). The mod does not have to be installed.")
                return
        elif not os.path.isdir(g) or (m and not os.path.isdir(m)):
            messagebox.showwarning("Portrait Scout", "Pick an existing game folder (and a mod folder, or leave it empty "
                                   "for the whole install).")
            return
        st.update({"game": g, "mod": m, "source": from_source})
        save_settings(st)
        go_btn.config(state="disabled")
        state["imgs"] = {}
        box: Dict[str, object] = {}

        def work() -> None:
            try:
                box["res"] = (analyze_source(g, m, False, lambda s: root.after(0, status.set, s)) if from_source else
                              analyze(g, m, merge_var.get(), lambda s: root.after(0, status.set, s)))
            except Exception as e:  # noqa: BLE001
                box["err"] = str(e)
            root.after(0, lambda: finish(box.get("res"), box.get("err")))   # type: ignore[arg-type]

        threading.Thread(target=work, daemon=True).start()

    def save_text() -> None:
        res = state["res"]
        if not res:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=f"{res['mod']}-creatures.txt",   # type: ignore[index]
                                         filetypes=[("Text", "*.txt")])
        if p:
            Path(p).write_text(as_text(res, sorted_rows(False)), encoding="utf-8")            # type: ignore[arg-type]
            status.set(f"Saved {p}")

    def save_csv() -> None:
        res = state["res"]
        if not res:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{res['mod']}-creatures.csv",    # type: ignore[index]
                                         filetypes=[("CSV", "*.csv")])
        if p:
            write_csv(res, p, sorted_rows(False))                                             # type: ignore[arg-type]
            status.set(f"Saved {p}")

    go_btn.config(command=analyze_clicked)
    save_btn.config(command=save_text)
    csv_btn.config(command=save_csv)
    root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Creatures / speakers of an installed mod: name, lines, portrait, race, gender, animation.")
    ap.add_argument("--cli", nargs=2, metavar=("GAMEDIR", "MODDIR"), help="no window: analyze and print")
    ap.add_argument("--out", default="", help="with --cli: also write this text file")
    ap.add_argument("--csv", default="", help="with --cli: also write this CSV file")
    ap.add_argument("--merge-names", action="store_true")
    ap.add_argument("--source", metavar="MODDIR", default="", help="experimental, no window: read the mod's own files "
                    "(works before the mod is installed); add --game GAMEDIR to name NPCs of the game")
    ap.add_argument("--game", default="", help="with --source: the game folder (optional)")
    a = ap.parse_args()
    if a.source:
        txt = source_text(scan_source(a.source, a.game, lambda s: print(s, file=sys.stderr)))
        print(txt)
        if a.out:
            Path(a.out).write_text(txt, encoding="utf-8")
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
