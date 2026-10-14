"""DP-22 §6.6/§6.7: the review-note grammar writer + its route.

All offline. The load-bearing pins:
  * §6.6 grammar emission BYTE-EXACT against fixtures (session row, anchor rows, note
    row — including the "1 pair" singular and the 6 dp timestamps);
  * durable per-file session keys: allocated under the append lock, reused while the
    mapping context (mode + rate + selection) is unchanged, bumped when it differs —
    and the adversarial audit case: two sessions with the same rate but different
    selections, rows re-sorted by timestamp, every note still resolving to exactly
    one complete mapping via its key;
  * sanitization (tabs/newlines/control chars, length cap) so a note can never break
    the TSV shape, and REVIEW rows never carrying either `verified` token form;
  * §3.3-style validation (identifiers against the server's own listings, capped
    numbers, bounded anchor lists) and the fsync-before-ack write;
  * the route: dark 404 without a checkout, read-only 404, Basic Auth 401,
    and a full HTTP round trip that lands the row and nudges review_sync.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # sibling test helpers

import align_server
import review_notes
import review_sync

from test_align import _fake_checkout, _fake_mp3_dir, _root


class _Env(unittest.TestCase):
    """A configured review-note environment: a checkout + mp3 dir in a tempdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="review-notes-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = _fake_checkout(self.tmp.name)
        self.mp3 = _fake_mp3_dir(self.tmp.name)
        root = _root(self.repo)
        root.start()
        self.addCleanup(root.stop)
        patcher = mock.patch.dict(os.environ, {
            "NETRADIO_SOURCES_DIR": "",
            "NETRADIO_MP3_DIR": self.mp3,
            "NETRADIO_READ_ONLY": "",
            "NETRADIO_AUTH_USER": "",
            "NETRADIO_AUTH_PASS": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        date = mock.patch.object(review_notes, "_today", lambda: "2026-08-06")
        date.start()
        self.addCleanup(date.stop)

    def _body(self, **kw):
        # canonical by construction: rate = the pairs' LSQ slope at 6 dp
        # (127.565/124.843 → 1.021803), orig_t = re-anchor from the second
        # anchor: 226.699 + 1.021803·(254.315 − 194.315) = 288.007180
        body = {"stem": "d376-395", "orig": 72, "stream_t": 254.315,
                "orig_t": 288.00718,
                "text": "mix-out — bass swap under the incoming record",
                "mode": "re-anchor", "rate": 1.021803,
                "pairs": [{"k": "1", "stream_s": 69.472, "orig_s": 99.134},
                          {"k": "2", "stream_s": 194.315, "orig_s": 226.699}]}
        body.update(kw)
        return body

    def _file(self):
        path = os.path.join(self.repo, "labels", "review", "d376-395.orig072.review.tsv")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()


class TestGrammar(_Env):

    def test_first_note_emits_session_anchors_note_byte_exact(self):
        payload, status = review_notes.handle_review_note(self._body(), self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], "s1")
        self.assertEqual(self._file(), (
            "0.000000\t0.000000\tnote REVIEW session s1: re-anchor rate 1.021803 "
            "from 2 pairs 2026-08-06\n"
            "69.472000\t69.472000\tnote REVIEW anchor s1: 1 orig072 99.134000\n"
            "194.315000\t194.315000\tnote REVIEW anchor s1: 2 orig072 226.699000\n"
            "254.315000\t254.315000\tnote REVIEW s1: orig072 288.007180 | "
            "mix-out — bass swap under the incoming record\n"))

    def test_same_context_reuses_the_session_key(self):
        review_notes.handle_review_note(self._body(), self.mp3)
        payload, status = review_notes.handle_review_note(
            self._body(stream_t=194.315, orig_t=226.699, text="ID over the break"),
            self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], "s1")
        self.assertEqual(payload["rows"], 1)      # note row only, no new context rows
        self.assertTrue(self._file().endswith(
            "194.315000\t194.315000\tnote REVIEW s1: orig072 226.699000 | "
            "ID over the break\n"))

    def test_changed_context_allocates_the_next_key_with_new_context_rows(self):
        review_notes.handle_review_note(self._body(), self.mp3)
        payload, _ = review_notes.handle_review_note(
            self._body(mode="constant-rate", text="drift audible here"), self.mp3)
        self.assertEqual(payload["session"], "s2")
        content = self._file()
        self.assertIn("note REVIEW session s2: constant-rate rate 1.021803 "
                      "from 2 pairs 2026-08-06", content)
        self.assertIn("note REVIEW anchor s2: 1 orig072 99.134000", content)
        self.assertIn("note REVIEW s2: orig072 288.007180 | drift audible here",
                      content)

    def test_single_pair_session_says_pair_singular(self):
        body = self._body(mode="assumed-rate", rate=1.0,
                          stream_t=69.472, orig_t=99.134,
                          pairs=[{"k": "1", "stream_s": 69.472, "orig_s": 99.134}])
        review_notes.handle_review_note(body, self.mp3)
        self.assertIn("note REVIEW session s1: assumed-rate rate 1.000000 "
                      "from 1 pair 2026-08-06", self._file())

    def test_selection_change_at_same_rate_is_a_new_session(self):
        # the audit-claim precondition: same rate, different selection => new key
        # (two DIFFERENT pair sets whose LSQ slope is exactly 1.01)
        a = [{"k": "1", "stream_s": 0.0, "orig_s": 10.0},
             {"k": "2", "stream_s": 100.0, "orig_s": 111.0}]
        b = [{"k": "1", "stream_s": 0.0, "orig_s": 5.0},
             {"k": "3", "stream_s": 200.0, "orig_s": 207.0}]
        payload, status = review_notes.handle_review_note(
            self._body(rate=1.01, pairs=a, stream_t=100.0, orig_t=111.0), self.mp3)
        self.assertEqual(status, 200, payload)
        payload, status = review_notes.handle_review_note(
            self._body(rate=1.01, pairs=b, stream_t=200.0, orig_t=207.0), self.mp3)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["session"], "s2")

    def test_resorted_rows_still_resolve_every_note_to_one_mapping(self):
        # two sessions, same rate, different selections; then sort_tsv-style re-sort
        review_notes.handle_review_note(self._body(text="first mapping note"),
                                        self.mp3)
        review_notes.handle_review_note(
            self._body(mode="assumed-rate", rate=1.0,
                       pairs=[{"k": "1", "stream_s": 69.472, "orig_s": 99.134}],
                       stream_t=10.0, orig_t=39.662,
                       text="second mapping note"), self.mp3)
        path = os.path.join(self.repo, "labels", "review", "d376-395.orig072.review.tsv")
        with open(path, "r", encoding="utf-8") as handle:
            rows = handle.read().splitlines()
        rows.sort(key=lambda r: float(r.split("\t", 1)[0]))     # time order
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(r + "\n" for r in rows))
        # keyed association survives the sort: each note names a key whose session
        # row + anchor rows fully reconstruct its mapping
        sessions, anchors, notes = {}, {}, {}
        for r in rows:
            m = review_notes._SESSION_ROW_RE.match(r)
            if m:
                sessions[int(m.group(1))] = (m.group(2), int(m.group(4)))
                continue
            m = review_notes._ANCHOR_ROW_RE.match(r)
            if m:
                anchors.setdefault(int(m.group(2)), []).append(m.group(3))
                continue
            if "\tnote REVIEW s" in r:
                key = int(r.split("note REVIEW s", 1)[1].split(":", 1)[0])
                notes[r.rsplit("| ", 1)[1]] = key
        self.assertEqual(notes["first mapping note"], 1)
        self.assertEqual(notes["second mapping note"], 2)
        for text, key in notes.items():
            mode, n = sessions[key]
            self.assertEqual(len(anchors[key]), n, (text, key))
        # and a fresh append still allocates ABOVE the max key after the re-sort
        payload, _ = review_notes.handle_review_note(
            self._body(mode="constant-rate"), self.mp3)
        self.assertEqual(payload["session"], "s3")

    def test_review_rows_never_carry_a_verified_token(self):
        review_notes.handle_review_note(self._body(), self.mp3)
        for row in self._file().splitlines():
            text = row.split("\t", 2)[2]
            structural = text.split("|", 1)[0]
            self.assertNotIn("verified", structural, row)
            self.assertTrue(text.startswith("note REVIEW"), row)   # prose note family

    def test_sessions_are_independent_per_pair_file(self):
        review_notes.handle_review_note(self._body(), self.mp3)
        # a second original => its own file, keys restart at s1
        open(os.path.join(self.repo, "sources_local", "073-Other.wv"), "w").close()
        payload, status = review_notes.handle_review_note(
            self._body(orig=73), self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], "s1")
        self.assertEqual(payload["file"], "d376-395.orig073.review.tsv")


class TestSanitization(_Env):

    def test_tabs_newlines_and_control_chars_become_spaces(self):
        body = self._body(text="a\tb\nc\rd\x00e")
        review_notes.handle_review_note(body, self.mp3)
        last = self._file().splitlines()[-1]
        self.assertEqual(len(last.split("\t")), 3, "the TSV shape must survive")
        self.assertTrue(last.endswith("| a b c d e"))

    def test_text_is_length_capped(self):
        review_notes.handle_review_note(self._body(text="x" * 2000), self.mp3)
        last = self._file().splitlines()[-1]
        self.assertEqual(last.rsplit("| ", 1)[1], "x" * review_notes.MAX_TEXT_LEN)

    def test_sanitize_is_the_single_choke_point(self):
        self.assertEqual(review_notes.sanitize_text("  ok\ttext \n"), "ok text")
        self.assertEqual(review_notes.sanitize_text("\t\n\x1f"), "")


class TestValidation(_Env):

    def _expect_400(self, fragment, **kw):
        payload, status = review_notes.handle_review_note(self._body(**kw), self.mp3)
        self.assertEqual(status, 400, payload)
        self.assertIn(fragment, payload["error"])

    def test_non_object_body(self):
        payload, status = review_notes.handle_review_note([1, 2], self.mp3)
        self.assertEqual(status, 400)

    def test_identifiers_checked_against_the_servers_own_listings(self):
        self._expect_400("unknown stem", stem="not-a-stream")
        self._expect_400("bad stem", stem="../../etc/passwd")
        self._expect_400("unknown orig", orig=999)
        self._expect_400("bad orig", orig="nope")

    def test_numbers_hard_capped(self):
        self._expect_400("stream_t out of range", stream_t=-1)
        self._expect_400("orig_t out of range", orig_t=1e9)
        self._expect_400("rate out of range", rate=9.0)
        self._expect_400("bad rate", rate=None)

    def test_mode_and_text_required(self):
        self._expect_400("bad mode", mode="freestyle")
        self._expect_400("empty text", text="\t\n")

    def test_anchor_list_bounded_and_shaped(self):
        self._expect_400("bad pairs", pairs=[])
        self._expect_400("bad pairs", pairs="nope")
        too_many = [{"k": "k%d" % i, "stream_s": float(i), "orig_s": float(i)}
                    for i in range(review_notes.MAX_ANCHORS + 1)]
        self._expect_400("bad pairs", pairs=too_many)
        self._expect_400("bad pair key",
                         pairs=[{"k": "bad key!", "stream_s": 1.0, "orig_s": 1.0}])
        self._expect_400("pair stream_s out of range",
                         pairs=[{"k": "1", "stream_s": -5, "orig_s": 1.0}])

    def test_dark_without_a_checkout(self):
        with _root(""):
            payload, status = review_notes.handle_review_note(self._body(), self.mp3)
            self.assertEqual(status, 404)
            self.assertIn("not configured", payload["error"])


class TestMappingConsistency(_Env):
    """§6.6's audit claim, enforced at the door: a note whose orig_t its own
    session rows (mode + rate + anchors) cannot reproduce is REJECTED, never
    durably acked — in every mode."""

    def _file_bytes(self):
        path = os.path.join(self.repo, "labels", "review", "d376-395.orig072.review.tsv")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def _expect_400(self, **kw):
        before = self._file_bytes()
        payload, status = review_notes.handle_review_note(self._body(**kw), self.mp3)
        self.assertEqual(status, 400, payload)
        self.assertIn("orig_t inconsistent", payload["error"])
        self.assertEqual(self._file_bytes(), before,
                         "nothing may be written for a rejected note")

    def test_re_anchor_rejects_an_irreproducible_orig_t(self):
        self._expect_400(orig_t=312.48)                     # ~24 s off the mapping

    def test_constant_rate_rejects_an_irreproducible_orig_t(self):
        self._expect_400(mode="constant-rate", orig_t=100.0)

    def test_assumed_rate_rejects_an_irreproducible_orig_t(self):
        self._expect_400(mode="assumed-rate", rate=1.0, stream_t=69.472,
                         orig_t=999.0,
                         pairs=[{"k": "1", "stream_s": 69.472, "orig_s": 99.134}])

    def test_re_anchor_is_governed_by_the_last_anchor_at_or_before_t(self):
        # three anchors whose LSQ slope is exactly 1.01 but whose per-pair
        # deviations differ by seconds: between anchors 1 and 2, the FIRST
        # anchor's segment maps t (§6.3) — the second anchor's segment value is
        # 4 s away and must be rejected there
        pairs = [{"k": "1", "stream_s": 0.0, "orig_s": 10.0},
                 {"k": "2", "stream_s": 100.0, "orig_s": 115.0},
                 {"k": "3", "stream_s": 200.0, "orig_s": 212.0}]
        # gov anchor 1 at t=50: 10 + 1.01·50 = 60.5
        payload, status = review_notes.handle_review_note(
            self._body(pairs=pairs, rate=1.01, stream_t=50.0, orig_t=60.5),
            self.mp3)
        self.assertEqual(status, 200, payload)
        self._expect_400(pairs=pairs, rate=1.01, stream_t=50.0,
                         orig_t=115.0 + 1.01 * (50.0 - 100.0))     # anchor 2's segment

    def test_margin_notes_clamp_at_the_originals_clock_zero(self):
        # §6.4: a note in the stream-solo pre-roll maps before the original's 0;
        # the client clamps to 0.0 and the server accepts exactly that
        body = self._body(mode="assumed-rate", rate=1.0, stream_t=50.0,
                          orig_t=0.0,
                          pairs=[{"k": "1", "stream_s": 100.0, "orig_s": 5.0}])
        payload, status = review_notes.handle_review_note(body, self.mp3)
        self.assertEqual(status, 200, payload)
        payload, status = review_notes.handle_review_note(
            dict(body, orig_t=45.0, text="not clamped"), self.mp3)
        self.assertEqual(status, 400)

    def test_expected_orig_t_constant_rate_is_the_fixed_slope_lsq_line(self):
        pairs = [{"k": "1", "stream_s": 0.0, "orig_s": 10.0},
                 {"k": "2", "stream_s": 100.0, "orig_s": 112.0}]
        # residuals vs slope 1.0: +10 and +12 → intercept 11
        self.assertAlmostEqual(
            review_notes.expected_orig_t("constant-rate", 1.0, pairs, 50.0),
            61.0, places=9)


class TestCanonicalModel(_Env):
    """Review iteration 2: the mapping MODEL must be canonical, not merely the
    value — ambiguous or contract-invalid anchor sets are rejected at the door."""

    def _expect_400(self, fragment, **kw):
        payload, status = review_notes.handle_review_note(self._body(**kw), self.mp3)
        self.assertEqual(status, 400, payload)
        self.assertIn(fragment, payload["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.repo, "labels", "review")),
                         "nothing may be written for a rejected note")

    def test_duplicate_anchor_instants_rejected_in_both_orders(self):
        # the reviewer's ambiguity case: equal-time anchors persist as
        # indistinguishable rows, so the re-anchor schedule would depend on
        # request order — which a sort_tsv re-sort destroys
        dup = [{"k": "1", "stream_s": 10.0, "orig_s": 20.0},
               {"k": "2", "stream_s": 10.0, "orig_s": 30.0}]
        self._expect_400("duplicate anchor", rate=1.0, stream_t=11.0, orig_t=31.0,
                         pairs=dup)
        self._expect_400("duplicate anchor", rate=1.0, stream_t=11.0, orig_t=21.0,
                         pairs=list(reversed(dup)))

    def test_mode_cardinality_enforced(self):
        one = [{"k": "1", "stream_s": 69.472, "orig_s": 99.134}]
        self._expect_400("needs at least 2 pairs", pairs=one,
                         stream_t=69.472, orig_t=99.134)                # re-anchor
        self._expect_400("needs at least 2 pairs", mode="constant-rate", pairs=one,
                         stream_t=69.472, orig_t=99.134)
        self._expect_400("exactly 1 pair", mode="assumed-rate", rate=1.0,
                         stream_t=194.315, orig_t=226.699)             # 2 pairs

    def test_fitted_modes_verify_the_rate_against_the_lsq_slope(self):
        pairs = [{"k": "1", "stream_s": 0.0, "orig_s": 0.0},
                 {"k": "2", "stream_s": 100.0, "orig_s": 120.0}]
        # §6.2: the rate IS the fit (1.2 here); a caller-supplied 1.0 is a lie
        self._expect_400("rate must be the least-squares fit", mode="constant-rate",
                         rate=1.0, pairs=pairs, stream_t=50.0, orig_t=50.0)
        # ...and the true fitted rate with a consistent orig_t is accepted
        payload, status = review_notes.handle_review_note(
            self._body(mode="constant-rate", rate=1.2, pairs=pairs,
                       stream_t=50.0, orig_t=60.0), self.mp3)
        self.assertEqual(status, 200, payload)
        self.assertIn("rate 1.200000 from 2 pairs", self._file())

    def test_assumed_rate_stays_free_but_bounded(self):
        # one anchor gives nothing to fit: §6.2's assumed-rate takes the summary
        # rate (or 1.000) on faith — bounded by RATE_RANGE, not fitted
        payload, status = review_notes.handle_review_note(
            self._body(mode="assumed-rate", rate=1.5, stream_t=71.472,
                       orig_t=102.134,
                       pairs=[{"k": "1", "stream_s": 69.472, "orig_s": 99.134}]),
            self.mp3)
        self.assertEqual(status, 200, payload)

    def test_degenerate_anchor_span_rejected(self):
        pairs = [{"k": "1", "stream_s": 10.0, "orig_s": 20.0},
                 {"k": "2", "stream_s": 10.000001, "orig_s": 20.5}]
        self._expect_400("degenerate anchor span", rate=1.0,
                         stream_t=10.0, orig_t=20.0, pairs=pairs)

    def test_malformed_session_row_fails_closed_not_open(self):
        # a session-shaped row the grammar can't parse must 500, never be counted
        # as "no sessions" (which would restart keys at s1 and merge two mappings)
        review_notes.handle_review_note(self._body(), self.mp3)
        path = os.path.join(self.repo, "labels", "review", "d376-395.orig072.review.tsv")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("0.000000\t0.000000\tnote REVIEW session s9: mangled-by-hand\n")
        payload, status = review_notes.handle_review_note(self._body(), self.mp3)
        self.assertEqual(status, 500, payload)
        self.assertIn("append failed", payload["error"])

    def test_free_text_mentioning_a_session_row_is_not_session_shaped(self):
        # the fail-closed detector keys on the row's third TSV column; free text
        # sits after "| " (tabs sanitized out), so it can never look like one
        review_notes.handle_review_note(
            self._body(text="see note REVIEW session s1: above"), self.mp3)
        payload, status = review_notes.handle_review_note(
            self._body(stream_t=194.315, orig_t=226.699, text="still fine"),
            self.mp3)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["session"], "s1")

    def test_near_fit_rate_is_rejected_not_tolerated(self):
        # review iteration 3's boundary case: a rate only 2e-4 off the fit walks
        # ~8 s away from the fitted mapping by the far end of a long capture —
        # the canonical rate is the fit's OWN 6 dp value, nothing else
        pairs = [{"k": "1", "stream_s": 0.0, "orig_s": 0.0},
                 {"k": "2", "stream_s": 100.0, "orig_s": 100.0}]
        self._expect_400("rate must be the least-squares fit",
                         rate=1.000199, pairs=pairs,
                         stream_t=40000.0, orig_t=40007.9401)
        # the canonical rate at the same far instant is accepted
        payload, status = review_notes.handle_review_note(
            self._body(rate=1.0, pairs=pairs, stream_t=40000.0, orig_t=40000.0),
            self.mp3)
        self.assertEqual(status, 200, payload)
        self.assertIn("rate 1.000000 from 2 pairs", self._file())


class TestDurability(_Env):

    def test_fsync_happens_before_the_ack(self):
        calls = []
        real_fsync = os.fsync
        with mock.patch("os.fsync",
                        side_effect=lambda fd: (calls.append("fsync"),
                                                real_fsync(fd))[1]):
            payload, status = review_notes.handle_review_note(self._body(), self.mp3)
        self.assertEqual(status, 200)
        # first note on a fresh checkout: the FILE, then the new labels/review/ dir, then
        # its parent (the directory entries must reach the disk too); all before
        # the 200
        self.assertEqual(calls, ["fsync"] * 3, calls)
        with mock.patch("os.fsync",
                        side_effect=lambda fd: (calls.append("fsync"),
                                                real_fsync(fd))[1]):
            review_notes.handle_review_note(
                self._body(stream_t=194.315, orig_t=226.699), self.mp3)
        self.assertEqual(calls, ["fsync"] * 4, "existing file: one fsync only")

    def test_concurrent_appends_keep_the_file_coherent(self):
        # many threads, two alternating mapping contexts: keys must stay monotonic,
        # every note must reference an emitted session, no row may interleave
        bodies = [self._body(text="note %d" % i,
                             mode=("re-anchor" if i % 2 else "constant-rate"))
                  for i in range(12)]
        errors = []

        def _go(b):
            payload, status = review_notes.handle_review_note(b, self.mp3)
            if status != 200:
                errors.append(payload)
        threads = [threading.Thread(target=_go, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = self._file().splitlines()
        session_keys, note_keys = set(), set()
        for r in rows:
            self.assertEqual(len(r.split("\t")), 3, r)
            m = review_notes._SESSION_ROW_RE.match(r)
            if m:
                session_keys.add(int(m.group(1)))
            elif "\tnote REVIEW s" in r:
                note_keys.add(int(r.split("note REVIEW s", 1)[1].split(":", 1)[0]))
        self.assertEqual(session_keys, set(range(1, max(session_keys) + 1)),
                         "keys allocate monotonically with no gaps")
        self.assertTrue(note_keys <= session_keys,
                        "every note resolves to an emitted session")
        self.assertEqual(sum(1 for r in rows if "| note " in r), 12)


class TestRoute(unittest.TestCase):
    """The HTTP surface: gate behaviour and one full round trip."""

    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), align_server.AlignHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _post(self, port, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request("POST", "/api/align/review-note", body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_dark_404_without_a_checkout(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}), _root(""):
            status, data = self._post(self._server(), json.dumps({"stem": "x"}))
            self.assertEqual(status, 404)
            self.assertIn("not configured", json.loads(data)["error"])

    def test_read_only_hides_the_route(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}):
            status, _ = self._post(self._server(), json.dumps({}))
            self.assertEqual(status, 404)

    def test_auth_required_when_configured(self):
        with mock.patch.dict(os.environ, {"NETRADIO_AUTH_USER": "u",
                                          "NETRADIO_AUTH_PASS": "p",
                                          "NETRADIO_READ_ONLY": ""}):
            status, _ = self._post(self._server(), json.dumps({}))
            self.assertEqual(status, 401)

    def test_bad_json_body_is_a_400(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}):
            status, _ = self._post(self._server(), "{nope")
            self.assertEqual(status, 400)

    def test_round_trip_lands_the_row_and_nudges_review_sync(self):
        tmp = tempfile.TemporaryDirectory(prefix="review-route-")
        self.addCleanup(tmp.cleanup)
        repo = _fake_checkout(tmp.name)
        mp3 = _fake_mp3_dir(tmp.name)
        with mock.patch.dict(os.environ, {"NETRADIO_SOURCES_DIR": "",
                                          "NETRADIO_MP3_DIR": mp3,
                                          "NETRADIO_READ_ONLY": "",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}), _root(repo):
            review_sync.NUDGE.clear()
            body = json.dumps({"stem": "d376-395", "orig": 72, "stream_t": 1.0,
                               "orig_t": 2.0, "text": "hello", "mode": "assumed-rate",
                               "rate": 1.0,
                               "pairs": [{"k": "1", "stream_s": 1.0, "orig_s": 2.0}]})
            status, data = self._post(self._server(), body)
            self.assertEqual(status, 200, data)
            payload = json.loads(data)
            self.assertEqual(payload["session"], "s1")
            path = os.path.join(repo, "labels", "review", "d376-395.orig072.review.tsv")
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertIn("note REVIEW s1: orig072 2.000000 | hello",
                              handle.read())
            self.assertTrue(review_sync.NUDGE.is_set(),
                            "a durable append must nudge the committer")


if __name__ == "__main__":
    unittest.main()
