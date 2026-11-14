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
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import struct
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

T_BMP, T_IDS, T_CRE, T_DLG = 0x0001, 0x03F0, 0x03F1, 0x03F3
EXT = {T_BMP: ".bmp", T_IDS: ".ids", T_CRE: ".cre", T_DLG: ".dlg"}
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
    out = sorted(rows.values(), key=lambda r: (-r["lines"], r["name"].lower()))

    def bmp_dims(nm: str) -> str:
        b = game.get(nm, T_BMP, 64) if nm else None
        if b and b[:2] == b"BM" and len(b) >= 26:
            w, h = struct.unpack_from("<ii", b, 18)
            return f"{abs(w)}x{abs(h)}"
        return ""

    for r in out:
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
    progress(f"Done: {len(out)} creature(s), {sum(r['lines'] for r in out):,} line(s).")
    return {"game": game, "rows": out, "whole": whole, "strings": len(texts),
            "mod": "whole-install" if whole else (os.path.basename(mod_dir.rstrip("/\\")) or "mod")}


COLUMNS = [("name", "Creature"), ("lines", "Lines"), ("portrait", "Portrait"), ("race", "Race"), ("sex", "Gender"),
           ("anim", "Animation"), ("cre", "CRE file(s)")]


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
                    "animation", "dialog", "cre", "notes"])
        for r in rows:
            w.writerow([r["name"], r["lines"], r["large"] or r["small"], r["small"], r.get("large_size", ""),
                        r.get("small_size", ""), r["race"], r["sex"], r["anim"],
                        ",".join(sorted(set(r["dialogs"]))), ",".join(sorted(set(r["cres"]))),
                        " | ".join(r["origin"])])


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


_RESREF_OK = re.compile(r"^[A-Za-z0-9_!#$%@&\-]+$")


def check_image(path: str) -> List[str]:
    """Things that may stop a proposed portrait from working in the game (empty list = looks fine)."""
    issues: List[str] = []
    stem = os.path.splitext(os.path.basename(path))[0]
    if len(stem) > 8:
        issues.append(f"the name '{stem}' is {len(stem)} characters; a resref can be at most 8")
    if not _RESREF_OK.match(stem):
        issues.append("the name has spaces or characters a resref can't contain")
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
    from tkinter import filedialog, messagebox, ttk

    st = load_settings()
    root = tk.Tk()
    root.title("Portrait Scout")
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
    status = tk.StringVar(value="Pick the game folder and the mod folder, then press Analyze. "
                          "(Leave the mod folder empty, or pick the game folder itself, to list every creature in the install.)")
    state: Dict[str, object] = {"res": None, "sort": ("lines", True), "imgs": {}}

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    top.columnconfigure(1, weight=1)

    def browse(var: tk.StringVar, title: str, start: str) -> None:
        d = filedialog.askdirectory(title=title, initialdir=start if start and os.path.isdir(start) else os.getcwd())
        if d:
            var.set(os.path.normpath(d))

    ttk.Label(top, text="Game folder:").grid(row=0, column=0, sticky="w")
    ttk.Entry(top, textvariable=game_var).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(top, text="Browse...", command=lambda: browse(game_var, "Game folder (has chitin.key)", game_var.get())).grid(row=0, column=2)
    ttk.Label(top, text="Mod folder:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(top, textvariable=mod_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
    ttk.Button(top, text="Browse...", command=lambda: browse(mod_var, "Mod folder (has the .tp2 / .tra / .d files)",
                                                             mod_var.get() or game_var.get())).grid(row=1, column=2, pady=(6, 0))
    btns = ttk.Frame(top)
    btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    go_btn = tk.Button(btns, text="Analyze mod", bg="#2e9e4f", fg="white", activebackground="#25803f",
                       activeforeground="white", font=("TkDefaultFont", 10, "bold"), padx=14, pady=3, relief="raised")
    go_btn.pack(side="left")
    save_btn = ttk.Button(btns, text="Save as text file...", state="disabled")
    save_btn.pack(side="right")
    csv_btn = ttk.Button(btns, text="Save as CSV...", state="disabled")
    csv_btn.pack(side="right", padx=6)
    ttk.Checkbutton(btns, text="Merge dialogs with the same creature name", variable=merge_var).pack(side="right", padx=12)
    view_var = tk.StringVar(value="table")
    sty.configure("View.Toolbutton", padding=(12, 4))
    ttk.Radiobutton(btns, text="Table", value="table", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(16, 0))
    ttk.Radiobutton(btns, text="Gallery", value="gallery", variable=view_var, style="View.Toolbutton",
                    command=lambda: show_view()).pack(side="left", padx=(2, 0))

    content = ttk.Frame(root)
    content.pack(fill="both", expand=True, padx=8, pady=4)
    tab_table, tab_gal = ttk.Frame(content), ttk.Frame(content)

    cols = [k for k, _ in COLUMNS]
    tree = ttk.Treeview(tab_table, columns=cols + ["psize", "proposed"], show="headings", selectmode="browse")
    vs = ttk.Scrollbar(tab_table, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.pack(side="left", fill="both", expand=True)
    vs.pack(side="right", fill="y")
    widths = {"name": 260, "lines": 70, "portrait": 220, "race": 110, "sex": 80, "anim": 220, "cre": 200, "proposed": 190, "psize": 150}
    for k, h in COLUMNS:
        tree.heading(k, text=h, command=lambda c=k: sort_by(c))
        tree.column(k, width=widths[k], anchor="e" if k == "lines" else "w")
    tree.heading("psize", text="Portrait size (L / M)")
    tree.column("psize", width=widths["psize"], anchor="w")
    tree.heading("proposed", text="Proposed portrait")
    tree.column("proposed", width=widths["proposed"], anchor="w")

    props: Dict[str, str] = {}                      # proposed portrait image per creature (key = its dialogs)
    prop_label = tk.StringVar(value="Proposed portraits: 0")
    gbar = ttk.Frame(tab_gal, padding=(0, 0, 0, 4))
    gbar.pack(side="top", fill="x")
    sty.configure("Export.TButton", font=("TkDefaultFont", 9, "bold"))
    export_btn = ttk.Button(gbar, text="Export proposed changes...", style="Export.TButton")
    export_btn.pack(side="left")
    clear_btn = ttk.Button(gbar, text="Clear all proposals")
    clear_btn.pack(side="left", padx=6)
    ttk.Label(gbar, textvariable=prop_label, foreground="#555").pack(side="left", padx=10)
    ttk.Label(gbar, text="Use 'Propose portrait...' on a card to pick an image for that creature.",
              foreground="#888").pack(side="right")
    gcanvas = tk.Canvas(tab_gal, highlightthickness=0, bg=PANEL)
    gv = ttk.Scrollbar(tab_gal, orient="vertical", command=gcanvas.yview)
    gcanvas.configure(yscrollcommand=gv.set)
    gcanvas.pack(side="left", fill="both", expand=True)
    gv.pack(side="right", fill="y")
    ginner = tk.Frame(gcanvas, bg=PANEL)
    gwin = gcanvas.create_window((0, 0), window=ginner, anchor="nw")
    ginner.bind("<Configure>", lambda e: gcanvas.configure(scrollregion=gcanvas.bbox("all")))
    CARD_W = 185                                   # width reserved for one gallery card; the row fills the window

    def per_row_now() -> int:
        w = gcanvas.winfo_width()
        return max(1, (w if w > 100 else 1000) // CARD_W)

    def on_gal_resize(e: object) -> None:
        gcanvas.itemconfigure(gwin, width=e.width)                       # type: ignore[attr-defined]
        if view_var.get() == "gallery" and state["res"] and per_row_now() != state.get("per_row"):
            if state.get("resize_job"):
                root.after_cancel(state["resize_job"])                   # type: ignore[arg-type]
            state["resize_job"] = root.after(150, lambda: build_gallery(sorted_rows()))

    gcanvas.bind("<Configure>", on_gal_resize)
    root.bind("<MouseWheel>", lambda e: gcanvas.yview_scroll(int(-e.delta / 120), "units") if view_var.get() == "gallery" else None)

    def show_view() -> None:
        tab_table.pack_forget()
        tab_gal.pack_forget()
        if view_var.get() == "gallery":
            tab_gal.pack(fill="both", expand=True)
            root.update_idletasks()
            build_gallery(sorted_rows())
        else:
            tab_table.pack(fill="both", expand=True)

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

    tree.bind("<Button-1>", table_click, add="+")
    ttk.Label(root, textvariable=status, anchor="w", padding=(8, 2)).pack(fill="x")

    def sorted_rows() -> List[dict]:
        res = state["res"]
        if not res:
            return []
        key, desc = state["sort"]                                    # type: ignore[misc]
        rows = list(res["rows"])                                     # type: ignore[index]
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
        if view_var.get() == "gallery":
            build_gallery(rows)

    def sort_by(col: str) -> None:
        key, desc = state["sort"]                                    # type: ignore[misc]
        state["sort"] = (col, (not desc) if col == key else (col == "lines"))
        refresh()

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
        try:
            menu.tk_popup(event.x_root, event.y_root)                # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def update_prop_label() -> None:
        prop_label.set(f"Proposed portraits: {len(props)}")

    def persist_props() -> None:
        res = state["res"]
        if res:
            st.setdefault("proposals", {})[res["mod"]] = dict(props)          # type: ignore[index]
            save_settings(st)
        update_prop_label()

    def load_thumb(path: str) -> object:
        cache: Dict[str, object] = state.setdefault("thumbs", {})             # type: ignore[assignment]
        if path in cache:
            return cache[path]
        img = None
        try:
            from PIL import Image, ImageTk                                      # optional: any image format
            im = Image.open(path)
            im.thumbnail((110, 170))
            img = ImageTk.PhotoImage(im)
        except Exception:  # noqa: BLE001
            try:
                if path.lower().endswith(".bmp"):
                    data = open(path, "rb").read()
                    w = abs(struct.unpack_from("<i", data, 18)[0])
                    ppm = bmp_to_ppm(data, max(1, -(-w // 110)))
                    img = tk.PhotoImage(data=ppm) if ppm else None
                else:
                    pi = tk.PhotoImage(file=path)                               # png / gif
                    k = max(1, -(-pi.width() // 110), -(-pi.height() // 170))
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
            issues = check_image(p)
            if issues and not messagebox.askyesno(
                    "Check this image", "This image may not work as a portrait:\n\n- " + "\n- ".join(issues)
                    + "\n\nUse it as the proposal anyway?", parent=par):
                par.lift()
                return
            props[rowkey(r)] = os.path.normpath(p)
            persist_props()
            if view_var.get() == "gallery":
                rebuild_keep_scroll()
            refresh_table_only()
            if callable(after):
                after()                                                              # type: ignore[operator]
        if parent is not None:
            parent.lift()                                                            # type: ignore[attr-defined]
            parent.focus_force()                                                     # type: ignore[attr-defined]

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
        rows = [r for r in sorted_rows() if rowkey(r) in props]
        if not res or not rows:
            messagebox.showinfo("Portrait Scout", "No portraits proposed yet. Use 'Propose portrait...' on a gallery card.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=f"{res['mod']}-portrait-todo.txt",   # type: ignore[index]
                                         filetypes=[("Text", "*.txt")])
        if not p:
            return
        lines = [f"# Portrait changes for {res['mod']}  ({len(rows)} creature(s))",                                  # type: ignore[index]
                 "# creature file  name -> proposed portrait file   [portrait it has now]   (!! = problems found with the image)"]
        for r in rows:
            fname = Path(props[rowkey(r)]).name
            now = r["large"] or r["small"] or "none"
            warn = check_image(props[rowkey(r)])
            for cre in (r["cre_list"] or [""]):
                lines.append(f"{cre + '.CRE' if cre else '(no creature file)'}  {r['name']} -> {fname}   [now: {now}]"
                             + (f"   !! check: {'; '.join(warn)}" if warn else ""))
        Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        status.set(f"Exported {len(rows)} proposed change(s) to {p}")

    export_btn.config(command=export_props)
    clear_btn.config(command=clear_all)

    def build_gallery(rows: List[dict]) -> None:
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
        for i, r in enumerate(rows[:400]):
            prop = props.get(rowkey(r))
            card = tk.Frame(ginner, bg=CARD, bd=1, relief="solid", padx=4, pady=4, highlightthickness=4,
                            highlightbackground="#2e9e4f" if prop else CARD, highlightcolor="#2e9e4f" if prop else CARD)
            card.grid(row=i // per_row, column=i % per_row, padx=4, pady=4, sticky="nsew")   # a row is as tall as its tallest card
            cards.append((card, r))
            img = load_thumb(prop) if prop else None
            shown_field = "prop" if img else ""
            for field, shrink in (("small", 1), ("large", 2)):
                if img:
                    break
                nm = (r[field].split("/")[0] if r[field] else "")
                if not nm:
                    continue
                if nm not in imgs:
                    data = game.get(nm, T_BMP)
                    ppm = bmp_to_ppm(data, shrink) if data else None
                    imgs[nm] = tk.PhotoImage(data=ppm) if ppm else None
                img = imgs[nm]
                if img:
                    shown_field = field
            if prop and not load_thumb(prop):
                tk.Label(card, text=f"{Path(prop).name}\n(preview needs PNG/GIF/BMP,\nor install Pillow)", bg="#cfe8d6",
                         width=18, height=6).pack()
            elif img:
                il = tk.Label(card, image=img, bg=CARD)
                il.pack()
                il.bind("<Button-3>", lambda e, rr=r: image_menu(e, rr, root))
            else:
                tk.Label(card, text="no portrait" if not (r["large"] or r["small"]) else "(file not found)",
                         width=14, height=6, bg="#d4d7dc").pack()
            sz = (prop_dims(prop) if shown_field == "prop" else r.get(f"{shown_field}_size", "")) if shown_field else ""
            if sz:
                tk.Label(card, text=sz, bg=CARD, fg="#888", font=("TkDefaultFont", 8)).pack()
            tk.Label(card, text=r["name"], bg=CARD, wraplength=CARD_W - 30, justify="center",
                     font=("TkDefaultFont", 9, "bold")).pack()
            tk.Label(card, text=f"{r['lines']} lines", bg=CARD, fg="#666").pack()
            if prop:
                tk.Label(card, text=f"Proposed: {Path(prop).name}" + (f" ({prop_dims(prop)})" if prop_dims(prop) else ""),
                         bg=CARD, fg="#1f7a3a", wraplength=CARD_W - 30,
                         font=("TkDefaultFont", 9, "bold")).pack()
                pi = check_image(prop)
                if pi:
                    wl = tk.Label(card, text="! check image (hover)", bg=CARD, fg="#b45309", cursor="question_arrow")
                    wl.pack()
                    wl.bind("<Button-1>", lambda e, m="\n- ".join(pi): messagebox.showwarning("Check this image", "- " + m))
                    wl._copy_label = True                                                  # type: ignore[attr-defined]
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
        if len(rows) > 400:
            tk.Label(ginner, text=f"... and {len(rows) - 400} more - see the Table", bg=PANEL, fg="#555").grid(
                row=(400 // per_row) + 1, column=0, columnspan=per_row, pady=8)

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
            for field, shrink in (("small", 1), ("large", 2)):
                nm = r[field].split("/")[0] if r[field] else ""
                data = game.get(nm, T_BMP) if nm else None
                ppm = bmp_to_ppm(data, shrink) if data else None
                if ppm:
                    img = tk.PhotoImage(data=ppm)
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
        ttk.Label(info, text=r["name"], font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(info, text="  ".join(x for x in (r["race"], r["sex"], r["anim"]) if x), foreground="#666").pack(anchor="w")
        sizes = ", ".join(x for x in ((f"large {r['large_size']}" if r.get("large_size") else ""),
                                      (f"medium {r['small_size']}" if r.get("small_size") else "")) if x)
        ttk.Label(info, text=f"Portrait: {r['portrait']}" + (f"  ({sizes})" if sizes else "")
                  + "     (right-click the picture to save it)", foreground="#666").pack(anchor="w")
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
        clr = ttk.Button(pf, text="Clear proposal", command=clear_here)
        clr.pack(side="left", padx=4)
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
            ob = ttk.LabelFrame(win, text="Where this comes from", padding=6)
            ob.pack(fill="x", padx=8, pady=4)
            for note in r["origin"]:
                ttk.Label(ob, text=note, wraplength=700, justify="left").pack(anchor="w", pady=1)
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
        for sr, t in r["texts"]:
            card = ttk.Frame(inner, padding=6, relief="groove")
            card.pack(fill="x", padx=8, pady=3)
            ttk.Label(card, text=f"#{sr}", foreground="#888").pack(anchor="w")
            ttk.Label(card, text=t or "(empty)", wraplength=680, justify="left").pack(anchor="w")

    def finish(res: Optional[dict], err: Optional[str]) -> None:
        go_btn.config(state="normal")
        if err:
            status.set("Error: " + err)
            messagebox.showerror("Portrait Scout", err)
            return
        state["res"] = res
        props.clear()
        props.update({k: v for k, v in (st.get("proposals", {}).get(res["mod"], {}) or {}).items()          # type: ignore[index]
                      if isinstance(v, str)})
        update_prop_label()
        refresh()
        for b in (save_btn, csv_btn):
            b.config(state="normal")
        out = Path(__file__).resolve().parent / f"{res['mod']}-creatures.txt"       # type: ignore[index]
        try:
            out.write_text(as_text(res, sorted_rows()), encoding="utf-8")            # type: ignore[arg-type]
            status.set(f"{len(res['rows'])} creature(s). Saved {out}")               # type: ignore[index]
        except OSError:
            status.set(f"{len(res['rows'])} creature(s).")                           # type: ignore[index]

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
            try:
                box["res"] = analyze(g, m, merge_var.get(), lambda s: root.after(0, status.set, s))
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
            Path(p).write_text(as_text(res, sorted_rows()), encoding="utf-8")            # type: ignore[arg-type]
            status.set(f"Saved {p}")

    def save_csv() -> None:
        res = state["res"]
        if not res:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{res['mod']}-creatures.csv",    # type: ignore[index]
                                         filetypes=[("CSV", "*.csv")])
        if p:
            write_csv(res, p, sorted_rows())                                             # type: ignore[arg-type]
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
    a = ap.parse_args()
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
