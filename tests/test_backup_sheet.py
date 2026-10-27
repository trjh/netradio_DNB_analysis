"""Tests for the Google Sheet backup helpers (offline — no network)."""
import contextlib
import io
import os
import sys
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import backup_sheet as b  # noqa: E402

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class HelperTests(unittest.TestCase):
    def test_col_index(self):
        self.assertEqual(b.col_index("A1"), 0)
        self.assertEqual(b.col_index("B2"), 1)
        self.assertEqual(b.col_index("Z9"), 25)
        self.assertEqual(b.col_index("AA1"), 26)
        self.assertEqual(b.col_index("AB10"), 27)

    def test_safe_filename(self):
        self.assertEqual(b.safe_filename("Track Sources"), "Track Sources")
        self.assertEqual(b.safe_filename("a/b:c*?"), "a-b-c-")
        self.assertEqual(b.safe_filename(""), "sheet")

    def test_skip_tabs_excludes_secrets_by_default(self):
        # The hard default must always exclude the credential tabs.
        self.assertIn("secrets", b.skip_tabs())
        self.assertIn("baseurl", b.skip_tabs())
        # no import-time skip list: the set is built only after .env loads
        self.assertFalse(hasattr(b, "SKIP_TABS"))

    def test_skip_tabs_credential_exclusion_is_mandatory(self):
        # Empty / unset env must NOT re-enable the SECRETS export.
        for env in ("", None, "   ", ",,"):
            with self.subTest(env=env):
                s = b.skip_tabs(env)
                self.assertIn("secrets", s)
                self.assertIn("baseurl", s)
        # Env only ADDS tabs; it can never remove the mandatory ones.
        s = b.skip_tabs("Drafts, Scratch")
        self.assertEqual(s, {"secrets", "baseurl", "drafts", "scratch"})
        # Even an env that tries to "replace" with something else keeps secrets.
        s = b.skip_tabs("Tracklist")
        self.assertIn("secrets", s)
        self.assertIn("baseurl", s)
        self.assertIn("tracklist", s)
        # Case/whitespace normalisation.
        self.assertIn("secrets", b.skip_tabs("  SECRETS  "))


class SheetIdTests(unittest.TestCase):
    def setUp(self):
        saved = os.environ.pop("NETRADIO_SHEET_ID", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "NETRADIO_SHEET_ID", saved)
        else:
            self.addCleanup(os.environ.pop, "NETRADIO_SHEET_ID", None)

    def test_no_id_committed_in_the_script(self):
        # the ID lives in .env only: the module carries no default sheet
        self.assertFalse(hasattr(b, "DEFAULT_SHEET"))
        src = (REPO / "scripts" / "backup_sheet.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"[A-Za-z0-9_-]{40,}")

    def test_refuses_with_no_id(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("NETRADIO_SHEET_ID", None)
                else:
                    os.environ["NETRADIO_SHEET_ID"] = value
                with self.assertRaises(SystemExit) as ctx:
                    b.sheet_id(None)
                self.assertIn("NETRADIO_SHEET_ID", str(ctx.exception.code))

    def test_flag_wins_then_the_environment(self):
        os.environ["NETRADIO_SHEET_ID"] = "from-env"
        self.assertEqual(b.sheet_id("from-flag"), "from-flag")
        self.assertEqual(b.sheet_id(None), "from-env")

    def test_main_refuses_before_any_download(self):
        # main() loads the checkout's .env first; point that at a missing file so the
        # test does not depend on this machine's .env, and make any download fail loudly
        import align_server
        real_load, real_dl = align_server.load_env, b.download_xlsx
        self.addCleanup(setattr, align_server, "load_env", real_load)
        self.addCleanup(setattr, b, "download_xlsx", real_dl)
        align_server.load_env = lambda path=None: None
        b.download_xlsx = lambda sheet: self.fail("downloaded with no sheet ID")
        with self.assertRaises(SystemExit) as ctx:
            b.main([])
        self.assertIn("NETRADIO_SHEET_ID", str(ctx.exception.code))

    def test_main_reads_the_id_and_extra_skips_from_dotenv(self):
        # a tab named only in .env's NETRADIO_SHEET_SKIP_TABS is skipped, like SECRETS
        import tempfile
        import align_server
        tmp = tempfile.TemporaryDirectory(prefix="backup-sheet-")
        self.addCleanup(tmp.cleanup)
        saved = os.environ.pop("NETRADIO_SHEET_SKIP_TABS", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "NETRADIO_SHEET_SKIP_TABS", saved)
        else:
            self.addCleanup(os.environ.pop, "NETRADIO_SHEET_SKIP_TABS", None)
        env = Path(tmp.name) / ".env"
        env.write_text("NETRADIO_SHEET_ID=from-dotenv\nNETRADIO_SHEET_SKIP_TABS=Private\n",
                       encoding="utf-8")
        real_load, real_dl, real_out = align_server.load_env, b.download_xlsx, b.OUT_DIR
        self.addCleanup(setattr, align_server, "load_env", real_load)
        self.addCleanup(setattr, b, "download_xlsx", real_dl)
        self.addCleanup(setattr, b, "OUT_DIR", real_out)
        align_server.load_env = lambda path=None: real_load(str(env))
        b.OUT_DIR = Path(tmp.name) / "out"
        asked = []

        def fake_download(sheet):
            asked.append(sheet)
            rels = ('<Relationships>'
                    + "".join('<Relationship Id="r%d" Target="worksheets/sheet%d.xml"/>'
                              % (i, i) for i in (1, 2, 3))
                    + '</Relationships>')
            wb = ('<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                  '<sheet name="Tracks" r:id="r1"/><sheet name="Private" r:id="r2"/>'
                  '<sheet name="SECRETS" r:id="r3"/></sheets></workbook>'
                  % (NS, b.RNS[1:-1]))
            sheet = ('<worksheet xmlns="%s"><sheetData><row r="1">'
                     '<c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'
                     '</sheetData></worksheet>' % NS)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("xl/_rels/workbook.xml.rels", rels)
                zf.writestr("xl/workbook.xml", wb)
                for i in (1, 2, 3):
                    zf.writestr("xl/worksheets/sheet%d.xml" % i, sheet)
            buf.seek(0)
            return zipfile.ZipFile(buf)

        b.download_xlsx = fake_download
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.main([]), 0)
        self.assertEqual(asked, ["from-dotenv"])
        self.assertEqual(sorted(p.name for p in b.OUT_DIR.iterdir()), ["Tracks.csv"])


class SharedStringsAndRowsTests(unittest.TestCase):
    def _zip(self, parts):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in parts.items():
                zf.writestr(name, data)
        buf.seek(0)
        return zipfile.ZipFile(buf)

    def test_shared_strings(self):
        xml = ('<sst xmlns="%s"><si><t>Hello</t></si>'
               '<si><r><t>Wor</t></r><r><t>ld</t></r></si></sst>' % NS)
        zf = self._zip({"xl/sharedStrings.xml": xml})
        self.assertEqual(b.shared_strings(zf), ["Hello", "World"])

    def test_sheet_to_rows_shared_inline_and_trailing_trim(self):
        strings = ["Alpha", "Beta"]
        sheet = (
            '<worksheet xmlns="%s"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Inline</t></is></c>'
            '<c r="B2"><v>42</v></c></row>'
            '<row r="3"><c r="A3"></c></row>'   # trailing empty row -> trimmed
            '</sheetData></worksheet>' % NS
        )
        zf = self._zip({"xl/worksheets/sheet1.xml": sheet})
        rows = b.sheet_to_rows(zf, "xl/worksheets/sheet1.xml", strings)
        self.assertEqual(rows, [["Alpha", "", "Beta"], ["Inline", "42"]])


if __name__ == "__main__":
    unittest.main()
