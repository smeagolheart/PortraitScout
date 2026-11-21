#!/usr/bin/env python3
"""
test_portrait_scout.py
Checks the parts of Portrait Scout that run without a game folder, a window or an image engine.
Run it from anywhere:  python test_portrait_scout.py

Covered: the .CRE struct offsets and the two readers that use them, the WeiDU .d
parser, text normalizing, the color words a creature's palette turns into, the
mod-export helpers, and cutting any picture down to a game portrait.
"""
import os, struct, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import portrait_scout as ps

# ---------------------------------------------------------------- CRE constants
class TestCreConstants(unittest.TestCase):
    def test_values_match_old_literals(self):
        self.assertEqual(ps.CRE_SIZE,           0x2D4)
        self.assertEqual(ps.CRE_NAME_LONG,       8)
        self.assertEqual(ps.CRE_NAME_SHORT,     12)
        self.assertEqual(ps.CRE_ANIM,           0x28)
        self.assertEqual(ps.CRE_COLOR_RGB,      0x2C)
        self.assertEqual(ps.CRE_PORTRAIT_SMALL, 0x34)
        self.assertEqual(ps.CRE_PORTRAIT_LARGE, 0x3C)
        self.assertEqual(ps.CRE_RACE,           0x272)
        self.assertEqual(ps.CRE_CLASS,          0x273)
        self.assertEqual(ps.CRE_SEX,            0x275)
        self.assertEqual(ps.CRE_DIALOG,         0x2CC)
        self.assertEqual(ps.CRE_DV,             0x280)
        self.assertEqual(ps.CRE_SCRIPTS,        (0x248, 0x250, 0x258, 0x260, 0x268))

def _make_cre(long_name=1, short_name=2, anim=3,
              small_port="SML1", large_port="LGE1",
              race=1, cls=2, sex=1, dialog="MYDLG") -> bytes:
    b = bytearray(ps.CRE_SIZE)
    b[:8] = b"CRE " + b"V1.0"
    struct.pack_into("<i", b, ps.CRE_NAME_LONG,   long_name)
    struct.pack_into("<i", b, ps.CRE_NAME_SHORT,  short_name)
    struct.pack_into("<I", b, ps.CRE_ANIM,        anim)
    b[ps.CRE_COLOR_RGB: ps.CRE_COLOR_RGB+7] = bytes([1,2,3,4,5,6,7])
    b[ps.CRE_PORTRAIT_SMALL: ps.CRE_PORTRAIT_SMALL+4] = small_port.encode()
    b[ps.CRE_PORTRAIT_LARGE: ps.CRE_PORTRAIT_LARGE+4] = large_port.encode()
    b[ps.CRE_SCRIPTS[0]: ps.CRE_SCRIPTS[0]+4] = b"DEFT"
    b[ps.CRE_RACE]   = race
    b[ps.CRE_CLASS]  = cls
    b[ps.CRE_SEX]    = sex
    b[ps.CRE_DIALOG: ps.CRE_DIALOG+4] = dialog.encode()
    return bytes(b)

class TestReadModCre(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".cre", delete=False) as f:
            f.write(_make_cre()); path = f.name
        try:
            r = ps.read_mod_cre(path)
            self.assertIsNotNone(r)
            self.assertEqual(r["long"],   1)
            self.assertEqual(r["anim"],   3)
            self.assertEqual(r["race"],   1)
            self.assertEqual(r["sex"],    1)
            self.assertEqual(r["small"],  "SML1")
            self.assertEqual(r["large"],  "LGE1")
            self.assertEqual(r["dialog"], "MYDLG")
            self.assertIn("DEFT", r["scripts"])
        finally:
            os.unlink(path)

    def test_bad_header_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".cre", delete=False) as f:
            f.write(b"NOT A CRE FILE"); path = f.name
        try:
            self.assertIsNone(ps.read_mod_cre(path))
        finally:
            os.unlink(path)


# ---------------------------------------------------------------- export helpers
class TestExportHelpers(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(ps._safe_name("My Set"),  "My_Set")
        self.assertEqual(ps._safe_name("a/b\\c"),  "a_b_c")
        self.assertEqual(ps._safe_name("ok!name"), "ok!name")
        self.assertEqual(ps._safe_name(""),        "set")

    def test_read_set(self):
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            p = d / "portraits.txt"
            p.write_text(
                "# set: My Set\n"
                "# component: 3\n"
                "SKIEL\tSKIEL\t\t\n"
                "\n"
                "# comment\n",
                encoding="utf-8")
            title, comp, rows = ps._read_set(p)
            self.assertEqual(title, "My Set")
            self.assertEqual(comp, 3)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][:2], ["SKIEL", "SKIEL"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- normalize
class TestNormalize(unittest.TestCase):
    def test_collapses_whitespace_and_casefolds(self):
        self.assertEqual(ps.normalize("  Hello   WORLD  "), "hello world")
        self.assertEqual(ps.normalize("a\n\tb"), "a b")

    def test_nonbreaking_space(self):
        self.assertEqual(ps.normalize("a\u00a0b"), "a b")


# ---------------------------------------------------------------- _rgb_word
class TestRgbWord(unittest.TestCase):
    def test_black(self):
        self.assertEqual(ps._rgb_word((0, 0, 0)), "black")

    def test_white(self):
        self.assertEqual(ps._rgb_word((255, 255, 255)), "white")

    def test_pure_red(self):
        self.assertEqual(ps._rgb_word((255, 0, 0)), "red")

    def test_skin_fair(self):
        self.assertEqual(ps._rgb_word((255, 255, 255), "skin"), "fair skin")

    def test_skin_charcoal(self):
        self.assertIn("charcoal-gray skin", ps._rgb_word((10, 10, 10), "skin"))

    def test_hair_black_and_white(self):
        self.assertEqual(ps._rgb_word((0,   0,   0),   "hair"), "black hair")
        self.assertEqual(ps._rgb_word((255, 255, 255), "hair"), "white hair")

    def test_hair_dark_brown(self):
        self.assertEqual(ps._rgb_word((139, 90, 43), "hair"), "dark brown hair")


# ---------------------------------------------------------------- parse_d
class TestParseD(unittest.TestCase):
    # WeiDU .d: SAY text [SND] = ; SAY @num =
    D = """BEGIN ~MYDLG~
SAY
~Hello~
[SND1]
=
SAY
@100
=
END
"""

    def test_npc_lines(self):
        lines, _ = ps.parse_d(self.D)
        # first line: text "Hello", sound "SND1"
        self.assertIn(("MYDLG", "text", "Hello", "SND1"), lines)
        # second line: @-reference 100, no sound
        self.assertIn(("MYDLG", "@",    "100",  ""),      lines)
        self.assertEqual(len(lines), 2)

    def test_no_setdlg(self):
        _, setdlg = ps.parse_d(self.D)
        self.assertEqual(setdlg, [])


# ---------------------------------------------------------------- to_portrait_bmp
class TestToPortraitBmp(unittest.TestCase):
    def test_roundtrip(self):
        w, h = 120, 200
        pad  = (4 - (w * 3) % 4) % 4
        row  = w * 3 + pad
        data = bytearray(row * h)
        for y in range(h):
            for x in range(w):
                o = y * row + x * 3
                data[o]     = (x * 2) % 256
                data[o + 1] = (y) % 256
                data[o + 2] = 128
        bmp = (b"BM"
               + struct.pack("<IHHI", 54 + len(data), 0, 0, 54)
               + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
               + bytes(data))
        src = Path(tempfile.gettempdir()) / f"_ps_t_src_{os.getpid()}.bmp"
        dst = Path(tempfile.gettempdir()) / f"_ps_t_dst_{os.getpid()}.bmp"
        try:
            src.write_bytes(bmp)
            out = ps.to_portrait_bmp(str(src), str(dst), 210, 330)
            self.assertEqual(out, str(dst))
            ob = dst.read_bytes()
            self.assertEqual(ob[:2], b"BM")
            ow, oh = struct.unpack_from("<ii", ob, 18)
            self.assertEqual(abs(ow), 210)
            self.assertEqual(abs(oh), 330)
            self.assertEqual(struct.unpack_from("<H", ob, 28)[0], 24)
        finally:
            src.unlink(missing_ok=True)
            dst.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
