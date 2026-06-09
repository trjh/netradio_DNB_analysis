"""Tests for the Google Sheet backup helpers (offline — no network)."""
import io
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
        self.assertIn("secrets", b.SKIP_TABS)
        self.assertIn("baseurl", b.SKIP_TABS)

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
