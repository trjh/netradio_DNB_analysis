"""DP-22 §6.9-a: the whole-song L/R review web page (`/align/review`).

Three families, in the repo's frontend-test idiom:

- **route tests** — the page is served by `align_server` behind the same `_gate`
  as every align route (read-only hides it, Basic Auth challenges it), and the
  two pages cross-link;
- **math mirrors** — the page's §6.2 least-squares fit (slope, intercept,
  per-anchor deviations) and §6.3 mapping are re-implemented in Python here and
  pinned AGAINST `review_notes`' server-side recomputation: the server rejects a
  note whose rate/orig_t its own session rows could not reproduce, so the JS must
  agree with `review_notes._lsq_slope` / `expected_orig_t` bit-for-bit at the
  emitted precision — these tests fail if either side's formula drifts;
- **page pins** — source greps for the §6.1/§6.2/§6.3/§6.4/§6.6 behaviours the design
  makes load-bearing (hard pans, varispeed, frozen-while-playing selection,
  hold+latch solo, the drift-corrector threshold, the note POST payload shape,
  marker hooks, and the no-foreign-fetch guardrail).
"""

import json
import math
import os
import re
import threading
import unittest
from http import client as http_client
from unittest import mock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import align_server
import review_notes

HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "public", "review.html")
ALIGN_HTML = os.path.join(os.path.dirname(HTML), "align.html")


def _page_src():
    with open(HTML, "r", encoding="utf-8") as handle:
        return handle.read()


class TestReviewRoute(unittest.TestCase):
    """The /align/review page rides the align server's gate like every route."""

    def _server(self):
        httpd = align_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), align_server.AlignHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _get(self, port, path, headers=None):
        conn = http_client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    OPEN_ENV = {"NETRADIO_READ_ONLY": "", "NETRADIO_AUTH_USER": "",
                "NETRADIO_AUTH_PASS": ""}

    def test_review_page_served(self):
        with mock.patch.dict(os.environ, self.OPEN_ENV):
            port = self._server()
            for path in ("/align/review", "/align/review/"):
                resp, data = self._get(port, path)
                self.assertEqual(resp.status, 200, path)
                self.assertIn("text/html", resp.getheader("Content-Type", ""))
                self.assertIn(b"whole-song L/R review", data)
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_read_only_hides_the_page(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}):
            resp, _ = self._get(self._server(), "/align/review")
            self.assertEqual(resp.status, 404)

    def test_auth_required_when_configured(self):
        with mock.patch.dict(os.environ, {"NETRADIO_AUTH_USER": "u",
                                          "NETRADIO_AUTH_PASS": "p",
                                          "NETRADIO_READ_ONLY": ""}):
            port = self._server()
            resp, _ = self._get(port, "/align/review")
            self.assertEqual(resp.status, 401)
            resp, _ = self._get(port, "/align/review",
                                {"Authorization": "Basic dTpw"})   # u:p
            self.assertEqual(resp.status, 200)

    def test_pages_cross_link(self):
        src = _page_src()
        self.assertIn('href="/align"', src)
        with open(ALIGN_HTML, "r", encoding="utf-8") as handle:
            self.assertIn('href="/align/review"', handle.read())


def _js_lsq(pairs):
    """The page's lsqSlope + intercept + deviations, mirrored in Python."""
    n = len(pairs)
    sm = sum(p["stream_s"] for p in pairs) / n
    om = sum(p["orig_s"] for p in pairs) / n
    den = sum((p["stream_s"] - sm) ** 2 for p in pairs)
    if den <= 1e-9:
        return None, None, None
    slope = sum((p["stream_s"] - sm) * (p["orig_s"] - om) for p in pairs) / den
    r = float("%.6f" % slope)          # the canonical 6 dp representation
    a = sum(p["orig_s"] - r * p["stream_s"] for p in pairs) / n
    devs = [(p["orig_s"] - (a + r * p["stream_s"])) * 1000.0 for p in pairs]
    return r, a, devs


PAIRS = [{"k": "1", "stream_s": 69.472, "orig_s": 99.134},
         {"k": "2", "stream_s": 194.315, "orig_s": 226.699},
         {"k": "3", "stream_s": 401.220, "orig_s": 438.183}]


class TestRateFitMirror(unittest.TestCase):
    """§6.2 math: the page's fit must be exactly what review_notes re-derives —
    the server rejects a fitted-mode rate that is not the 6 dp LSQ slope, and an
    orig_t off the mapping by more than MAPPING_TOLERANCE_S."""

    def test_slope_matches_the_servers_canonical_fit(self):
        r, _, _ = _js_lsq(PAIRS)
        server = float("%.6f" % review_notes._lsq_slope(PAIRS))
        self.assertEqual(r, server)

    def test_two_point_fit_is_the_exact_line(self):
        two = PAIRS[:2]
        r, a, devs = _js_lsq(two)
        exact = (two[1]["orig_s"] - two[0]["orig_s"]) / \
                (two[1]["stream_s"] - two[0]["stream_s"])
        self.assertAlmostEqual(r, float("%.6f" % exact))
        # deviations vanish on the exact two-point line (up to 6 dp rate rounding
        # over the anchor span — sub-ms by construction)
        for d in devs:
            self.assertLess(abs(d), 1.0)

    def test_constant_rate_mapping_matches_expected_orig_t(self):
        r, a, _ = _js_lsq(PAIRS)
        for t in (0.0, 69.472, 150.0, 401.22, 500.0):
            js = min(max(a + r * t, 0.0), 24 * 3600.0)
            self.assertAlmostEqual(
                js, review_notes.expected_orig_t("constant-rate", r, PAIRS, t),
                places=9)

    def test_re_anchor_mapping_matches_expected_orig_t(self):
        r, _, _ = _js_lsq(PAIRS)
        for t in (0.0, 69.472, 100.0, 194.315, 300.0, 401.22, 900.0):
            # the page: governed by the last selected anchor at or before t
            gov = PAIRS[0]
            for p in PAIRS:
                if p["stream_s"] <= t:
                    gov = p
            js = min(max(gov["orig_s"] + r * (t - gov["stream_s"]), 0.0),
                     24 * 3600.0)
            self.assertAlmostEqual(
                js, review_notes.expected_orig_t("re-anchor", r, PAIRS, t),
                places=9)

    def test_mapped_note_validates_end_to_end(self):
        # a note whose orig_t the page computed through the mirror must pass the
        # server's reproducibility check (the POST-shaped dict, validate()'s
        # mapping stage) — mode cardinality and the canonical rate included
        r, a, _ = _js_lsq(PAIRS)
        t = 250.0
        clean = {"rate": r, "pairs": PAIRS, "mode": "constant-rate",
                 "stream_t": t, "orig_t": a + r * t}
        exp = review_notes.expected_orig_t(clean["mode"], clean["rate"],
                                           clean["pairs"], clean["stream_t"])
        self.assertLessEqual(abs(clean["orig_t"] - exp),
                             review_notes.MAPPING_TOLERANCE_S)

    def test_degenerate_span_is_refused_not_fit(self):
        same = [{"k": "1", "stream_s": 5.0, "orig_s": 1.0},
                {"k": "2", "stream_s": 5.0, "orig_s": 2.0}]
        self.assertEqual(_js_lsq(same), (None, None, None))
        self.assertIsNone(review_notes._lsq_slope(same))

    def test_js_formula_templates_present(self):
        # the JS the mirror above mirrors (drift here = drift in the mirror too)
        src = _page_src()
        for needle in (
                "num += (p.stream_s - sm) * (p.orig_s - om);",
                "if (den <= 1e-9) return null;",
                "r = Number(slope.toFixed(6));",
                "const a = pts.reduce((s, p) => s + (p.orig_s - r * p.stream_s), 0) / pts.length;",
                "return gov.orig_s + fit.r * (t - gov.stream_s);",
                "if (p.stream_s <= t) gov = p;",
        ):
            self.assertIn(needle, src)


class TestReviewPagePins(unittest.TestCase):
    """Source pins for the design's load-bearing page behaviours."""

    def setUp(self):
        self.src = _page_src()

    # ---- §6.1: presentation ----
    def test_hard_pans_and_swap_flip_both(self):
        self.assertIn("createStereoPanner()", self.src)
        self.assertIn("panS.pan.value = swap ? 1 : -1;", self.src)
        self.assertIn("panO.pan.value = swap ? -1 : 1;", self.src)

    def test_varispeed_all_vendor_spellings(self):
        # §6.9: pitch-true correction is the WANTED behaviour
        self.assertIn("aO.preservesPitch = false;", self.src)
        self.assertIn("aO.mozPreservesPitch = false;", self.src)
        self.assertIn("aO.webkitPreservesPitch = false;", self.src)
        self.assertIn("aO.playbackRate = fit.r;", self.src)

    def test_solo_hold_and_latch_are_gain_only(self):
        # both §6.1 forms; solo/swap are graph changes — the solo path must never
        # pause an element (playback never stops across the switches). DP-36:
        # solo zeroes the original WITHOUT touching its stored volume
        self.assertIn('holdBtn.addEventListener("pointerdown"', self.src)
        self.assertIn('holdBtn.addEventListener("pointerup", holdOff);', self.src)
        self.assertIn("latchSolo = !latchSolo;", self.src)
        self.assertIn("gainO.gain.value = (holdSolo || latchSolo) ? 0 : volO;",
                      self.src)
        solo_fns = self.src.split("function applySolo()")[1].split("}")[0] + \
            self.src.split("const holdOff = ")[1].split(";")[0]
        self.assertNotIn(".pause()", solo_fns)

    # ---- §6.2: selection + fit ----
    def test_selection_frozen_while_playing_and_reset_on_change(self):
        self.assertIn("if (playing) { cb.checked = sel.has(p.id); return; }",
                      self.src)
        # the run start freezes the list; a change resets to the window start
        self.assertIn("setEditEnabled(false);", self.src)
        toggle = self.src.split("function toggleAnchor(")[1].split(
            "\n// ----------")[0]
        self.assertIn("resetPlayback();", toggle)
        self.assertIn("aS.currentTime = win.t0;", self.src)

    def test_last_anchor_cannot_be_deselected(self):
        self.assertIn("const lastSelected = sel.size === 1;", self.src)
        self.assertIn('cb.dataset.locked = (!usableP || (lastSelected && '
                      'cb.checked)) ? "1" : "0";', self.src)

    def test_assumed_rate_mode_flagged_with_pass1_fallback(self):
        # §6.2 single-anchor policy: the pair's Pass-1 rate when known, else 1.000,
        # and the readout says "assumed"
        self.assertIn("r = (cur.rate != null) ? Number(cur.rate.toFixed(6)) : 1.0;",
                      self.src)
        self.assertIn('return (fit && fit.assumed) ? "assumed-rate" : mode;',
                      self.src)
        self.assertIn("(assumed)", self.src)

    def test_zero_pair_gate_with_guidance(self):
        self.assertIn("seat a point in /align (or run match-hints) first",
                      self.src)
        self.assertIn("if (!e.usable.length) {", self.src)

    def test_duplicate_stream_instant_guard_mirrors_the_server(self):
        # review_notes rejects duplicate anchor stream_s at 6 dp — the page must
        # refuse the selection instead of failing on POST
        self.assertIn('selectedPoints().some(q => q.stream_s.toFixed(6) === key6)',
                      self.src)

    def test_deviation_column_is_live_ms(self):
        self.assertIn("(p.orig_s - (a + r * p.stream_s)) * 1000", self.src)
        self.assertIn('fit.devs[p.id].toFixed(1) + " ms"', self.src)
        self.assertIn("multi-rate suspected", self.src)   # the §6.2 verdict line

    # ---- §6.3/§6.9: playback mapping + corrector ----
    def test_drift_corrector_threshold_and_logging(self):
        self.assertIn("const DRIFT_NUDGE_MS = 15;", self.src)
        self.assertIn("const DRIFT_CHECK_MS = 2000;", self.src)
        self.assertIn("Math.abs(driftMs) > DRIFT_NUDGE_MS", self.src)
        self.assertIn('console.log("drift corrector: "', self.src)

    def test_drift_readout_tooltip(self):
        # §6.3 (requirement of 2026-08-11): the readout carries an info
        # affordance saying what the number measures, that 0 ms is on-target,
        # and pointing at the plan section the explanation lives in
        self.assertIn('id="driftInfo"', self.src)
        self.assertIn("0 ms = perfectly on target", self.src)
        self.assertIn("PLAN_align_tool.md</code> §6.3", self.src)
        self.assertIn('$("driftInfo").onclick', self.src)

    def test_wv_only_refusal_message_names_the_dp23_sync(self):
        # the transcode fallback is gone (§6.8) — the page must surface the
        # route's refusal (or its own fallback copy naming the DP-23 sync),
        # never retry toward a transcode tier that no longer exists
        self.assertIn("run the DP-23 flac sync to make its FLAC sibling", self.src)
        self.assertNotIn("transcode?", self.src)
        self.assertNotIn("origRetries", self.src)

    def test_default_selection_dedups_duplicate_instants(self):
        # hints + placed routinely carry the same anchor instant; the server
        # refuses duplicate stream_s at 6 dp — the default select-all must
        # apply the same rule, not fail every note POST (independent-review
        # finding, 2026-08-18)
        self.assertIn("function dedupInstants(ids, usable)", self.src)
        self.assertIn("sel = new Set(dedupInstants(restoredSel || e.usable.map(p => p.id), e.usable));",
                      self.src)

    def test_prefs_restore_never_clobbers_the_saved_state(self):
        # setVol saves prefs; during restorePrefs that write used to rewrite
        # localStorage with pair:null + default mode BEFORE the pair/mode were
        # restored — losing them whenever the saved pair wasn't restorable
        # (an independent review's finding; the Swift side already guarded)
        self.assertIn("restoringPrefs = true;", self.src)
        self.assertIn("if (!restoringPrefs) savePrefs();", self.src)

    def test_fine_strip_status_has_its_own_line(self):
        # an independent review's finding: the fine-strip failure message must not
        # clobber the window readout (#waveInfo) — it gets its own element,
        # cleared on every explicit (re-)engage
        self.assertIn('id="fineInfo"', self.src)
        self.assertIn('$("fineInfo").textContent = "fine strips unavailable"', self.src)
        self.assertNotIn('$("waveInfo").textContent = "fine strips unavailable"', self.src)

    def test_rate_note_reaches_the_picker(self):
        # the placed op's rate_note (the rate rules of #130) is shown on the
        # pair row — carried through the merge, rendered via textContent
        self.assertIn("if (!e.rateNote && set.rate_note) e.rateNote = set.rate_note;",
                      self.src)
        self.assertIn('n.textContent = "rate: " + e.rateNote;', self.src)

    def test_re_anchor_crossing_reseats_and_marks_used(self):
        self.assertIn("usedAnchors.add(p.id);", self.src)
        self.assertIn("correctDrift(true);", self.src)
        self.assertIn('console.log("re-anchor at "', self.src)
        # crossing detection counts PASSED anchors, not the governing index — the
        # governing index is already 0 before the first anchor, so tracking it
        # could never mark the first anchor used (found in the live self-test)
        self.assertIn("const cc = crossCount(t);", self.src)
        self.assertIn("if (cc > lastCross) {", self.src)

    def test_scrub_never_touches_the_model(self):
        # §6.2: scrubbing moves position only — no fit recompute in the seek path
        seek = self.src.split("function seekTo(")[1].split("function ")[0]
        self.assertNotIn("recomputeFit", seek)
        self.assertIn("lastCross = crossCount(t);", seek)
        self.assertIn("syncOrig(true);", seek)

    def test_margins_are_20s_clamped_and_stream_solo(self):
        self.assertIn("const MARGIN_S = 20;", self.src)
        self.assertIn("win.t0 = Math.max(0, start - MARGIN_S);", self.src)
        self.assertIn("stream solo (margin)", self.src)
        self.assertIn("clamped, the overhang is unavailable", self.src)

    def test_coverage_entry_is_per_frame_not_timer_paced(self):
        # review iteration 1 P1: with an unclamped pre-roll the original's entry
        # into coverage must happen in the playback loop (rAF), not on the 2 s
        # corrector timer — the timer alone started the original up to 2 s late
        # and permanently skipped its opening audio
        step = self.src.split("function tick()")[1].split("function seekTo(")[0]
        self.assertIn("if (playing) syncOrig(false);", step)
        # and syncOrig is the edge handler: pause outside coverage, seat + play
        # the moment coverage begins
        sync = self.src.split("function syncOrig(")[1].split("function ")[0]
        self.assertIn("if (!inRange) { if (!aO.paused) aO.pause(); return; }",
                      sync)
        self.assertIn("if (force || aO.paused) aO.currentTime = target;", sync)
        self.assertIn("if (playing && aO.paused) aO.play()", sync)

    def test_preroll_boundary_math_for_a_late_starting_original(self):
        # review iteration 1 P1 regression (mirror): a pair whose original
        # begins AFTER stream zero has an unclamped 20 s pre-roll; the §6.4
        # boundary is exactly where the mapping crosses the original's zero,
        # and the mapping is continuous there (no content is skippable by
        # re-seating at the boundary)
        pairs = [{"k": "1", "stream_s": 100.0, "orig_s": 30.0},
                 {"k": "2", "stream_s": 300.0, "orig_s": 234.4}]
        r, a, _ = _js_lsq(pairs)
        entry = (0.0 - a) / r                  # spanL: stream time of orig zero
        self.assertGreater(entry, 20.0, "the pre-roll must be unclamped here")
        # window start = entry - MARGIN_S, entirely stream-solo up to `entry`
        self.assertAlmostEqual(max(0.0, entry - 20.0), entry - 20.0)
        # at the boundary the mapped original position is its zero (continuity)
        self.assertAlmostEqual(a + r * entry, 0.0, places=9)
        # one 60 fps frame late costs < 20 ms of the original — the per-frame
        # entry bounds the loss to a frame, vs ~2 s on the corrector timer
        self.assertLess(r * (1 / 60), 0.020)

    def test_ios_screen_lock_limitation_disclosed_up_front(self):
        # review iteration 1 P2: §6.9 states the limitation must be stated up
        # front on the phone-facing page
        intro = self.src.split("<section", 1)[0]
        self.assertIn("Known iOS limitation", intro)
        self.assertIn("SUSPENDS", intro)

    # ---- §6.6: notation ----
    def test_note_placement_only_while_paused(self):
        self.assertIn("if (playing || !fit) return;", self.src)

    def test_note_post_payload_shape(self):
        # exactly the review_notes.validate() contract
        self.assertIn('fetch("/api/align/review-note"', self.src)
        for needle in ("stem: cur.stem", "orig: cur.orig",
                       "stream_t: Number(notePos.toFixed(6))",
                       "orig_t: Number(mapOrigClamped(notePos).toFixed(6))",
                       "rate: fit.r", "mode: effMode()",
                       "pairs: fit.anchors.map(p => ({ k: p.k, "
                       "stream_s: p.stream_s, orig_s: p.orig_s }))"):
            self.assertIn(needle, self.src)

    def test_note_errors_surface_honestly(self):
        self.assertIn("HTTP ' + r.status", self.src)
        self.assertIn('escapeHtml(j.error || "save failed")', self.src)

    def test_marker_hooks_render_notes_and_used_anchors(self):
        self.assertIn("function drawNoteMarkers(", self.src)
        self.assertIn("function drawAnchorFlags(", self.src)
        self.assertIn("markerBoxes.push({", self.src)
        self.assertIn("notes.push({ stream_t: body.stream_t", self.src)

    # ---- §6.5: the picker ----
    def test_picker_interleaves_both_sources_stream_ordered(self):
        self.assertIn('for (const h of sources.hints || []) ingest(h, "hints");',
                      self.src)
        self.assertIn('for (const pl of sources.placed || []) '
                      'ingest(pl, "placed");', self.src)
        self.assertIn("e.pointsAll.sort((a, b) => "
                      "(a.stream_s || 0) - (b.stream_s || 0));", self.src)
        self.assertIn("NOT-FOUND", self.src)

    def test_anchor_key_regex_mirrors_the_server(self):
        js = re.search(r"const ANCHOR_K_RE = /(.+)/;", _page_src()).group(1)
        self.assertEqual("^" + review_notes._ANCHOR_K_RE.pattern.lstrip("^"),
                         js)

    # ---- guardrails ----
    def test_every_fetch_targets_the_align_api(self):
        targets = re.findall(r'fetch\(\s*"([^"]+)"', self.src)
        self.assertTrue(targets, "the page must fetch its data somewhere")
        for t in targets:
            self.assertTrue(t.startswith("/api/align/"), t)
        # and never a dynamic/foreign URL scheme
        self.assertNotIn("http://", self.src.split("</style>")[1])
        self.assertNotIn("https://", self.src.split("</style>")[1])

    def test_prefs_idiom(self):
        self.assertIn('const PREFS_KEY = "netradio.review.prefs";', self.src)
        self.assertIn("localStorage.setItem(PREFS_KEY", self.src)
        self.assertIn("localStorage.getItem(PREFS_KEY", self.src)

    def test_envelopes_fetched_once_per_pair_with_honest_degrade(self):
        self.assertIn('fetch("/api/align/overview"', self.src)
        self.assertIn("ovCache.set(key, j);", self.src)
        self.assertIn("position-bar mode", self.src)
        self.assertIn("if (r.status === 429) { setTimeout(fetchOverview, 1200); "
                      "return; }", self.src)

    def test_time_cap_mirrors_the_server(self):
        self.assertIn("const MAX_T_S = 24 * 3600;", self.src)


def _js_vol(pct):
    """The page's setVol percent → gain conversion, mirrored (clamp then /100)."""
    return min(max(pct, 0), 200) / 100


class TestVolumeAndDownmixPins(unittest.TestCase):
    """DP-36: unmodified default levels, 0–200 % per-source sliders, and the
    stereo hard-pan overload fix (downmix-before-pan).

    Root cause (measured): StereoPannerNode with a STEREO input at pan = ±1
    sums L+R into the one output channel per the Web Audio spec's stereo-pan
    equations — up to 2× full scale (the gate pair measured +5.4 dBFS stream /
    +5.9 dBFS original: the reported clipping). The fix forces each source's
    gain node to MONO so the spec's standard down-mix — 0.5·(L+R) — happens
    BEFORE the pan; the hard pan is then a unity router and the ear's peak can
    never exceed the source's own."""

    def setUp(self):
        self.src = _page_src()

    def test_downmix_happens_before_the_pan(self):
        # the explicit-mono constraint sits on the GAIN nodes, upstream of the
        # panners in the graph (source → gain → pan → destination)
        for needle in ("g.channelCount = 1;",
                       'g.channelCountMode = "explicit";',
                       'g.channelInterpretation = "speakers";'):
            self.assertIn(needle, self.src)
        graph = self.src.split("function ensureGraph()")[1].split("function ")[0]
        self.assertLess(graph.index('g.channelCountMode = "explicit";'),
                        graph.index("srcS.connect(gainS).connect(panS)"))
        self.assertIn("srcO.connect(gainO).connect(panO)", graph)

    def test_unmodified_default_is_documented_in_the_graph(self):
        # "unmodified" for a stereo source = 0.5·(L+R) in the assigned ear (the
        # standard mono down-mix); a mono source passes at unity — stated where
        # the graph is built, so the definition can't drift from the code
        graph = self.src.split("function ensureGraph()")[1].split("function ")[0]
        self.assertIn("0.5·(L+R)", graph)
        self.assertIn("mono source passes at unity", graph)

    def test_downmix_peak_never_exceeds_the_sources_own(self):
        # the level math, mirrored: |0.5·(l+r)| ≤ max(|l|, |r|) for every sample
        # pair — the pre-fix sum could reach 2.0, the post-fix ear cannot clip
        # unless the source itself does
        cases = [(1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (0.3, 0.9), (0.0, 1.0)]
        for l, r in cases:
            self.assertLessEqual(abs(0.5 * (l + r)), max(abs(l), abs(r)))
        self.assertEqual(abs(1.0 + 1.0), 2.0)   # the pre-fix hazard, for the record

    def test_sliders_present_with_range_and_unity_default(self):
        self.assertIn("let volS = 1, volO = 1;", self.src)
        for sid in ("volS", "volO"):
            self.assertRegex(self.src, r'id="%s" class="vol" min="0" max="200" '
                                       r'value="100"' % sid)

    def test_percent_to_gain_conversion_mirrors(self):
        self.assertIn("const v = Math.min(Math.max(pct, 0), 200) / 100;",
                      self.src)
        for pct, gain in ((-10, 0.0), (0, 0.0), (50, 0.5), (100, 1.0),
                          (200, 2.0), (250, 2.0)):
            self.assertEqual(_js_vol(pct), gain)

    def test_sliders_are_live_gain_changes_never_pausing(self):
        self.assertIn('$("volS").addEventListener("input", () => '
                      'setVol("S", +$("volS").value));', self.src)
        self.assertIn('$("volO").addEventListener("input", () => '
                      'setVol("O", +$("volO").value));', self.src)
        set_vol = self.src.split("function setVol(")[1].split("function ")[0]
        self.assertIn("applySolo();", set_vol)     # the one place levels land
        self.assertNotIn(".pause()", set_vol)

    def test_double_tap_resets_to_unity(self):
        for needle in ('dblReset($("volS"), () => setVol("S", 100));',
                       'dblReset($("volO"), () => setVol("O", 100));',
                       'dblReset($("volSOut"), () => setVol("S", 100));',
                       'dblReset($("volOOut"), () => setVol("O", 100));'):
            self.assertIn(needle, self.src)

    def test_above_unity_shows_the_amber_cue_and_no_limiter(self):
        self.assertIn('$("volSOut").classList.toggle("hot", volS > 1);', self.src)
        self.assertIn('$("volOOut").classList.toggle("hot", volO > 1);', self.src)
        self.assertIn(".volOut.hot { color:var(--warn);", self.src)
        # honesty over safety: no limiter/compressor node anywhere on the page
        self.assertNotIn("createDynamicsCompressor", self.src)

    def test_volumes_ride_the_prefs(self):
        self.assertIn("volS: volPct(volS),", self.src)
        self.assertIn("volO: volPct(volO),", self.src)
        # restore goes through the same clamp as the sliders
        self.assertIn('if (Number.isFinite(p.volS)) setVol("S", p.volS);', self.src)
        self.assertIn('if (Number.isFinite(p.volO)) setVol("O", p.volO);', self.src)


def _js_round(x):
    """JS Math.round — half always rounds UP (Python's round() half-evens)."""
    return math.floor(x + 0.5)


def _js_view_span(z):
    """The page's viewSpanS(): the viewport's seconds at zoom factor z."""
    return 30 / z


def _js_x_of(t, cursor, z, cw):
    """The page's x0(): stream time -> CSS px in the playhead-centred view."""
    span = _js_view_span(z)
    return (t - (cursor - span / 2)) / span * cw


def _js_time_at(x, cursor, z, cw):
    """The page's tap mapping: CSS px -> stream time (the pointer handlers)."""
    span = _js_view_span(z)
    return cursor - span / 2 + x / cw * span


def _js_fine_covered(tiles, a, b):
    """The page's fineCoveredRange merge-walk, mirrored (tiles = (t0, t1))."""
    at = a
    for t0, t1 in sorted(tiles):
        if t0 > at + 1e-3:
            break
        if t1 > at:
            at = t1
        if at >= b - 1e-3:
            return True
    return at >= b - 1e-3


def _js_max_zoom(cps, dev_w):
    """The page's maxZoom(): the MAX-honesty formula, mirrored.

    zMax = round(MAX_PX_PER_COL * VIEW_S * cps / devW) clamped to [2, 16] —
    the zoom at which one envelope column spans ~2-3 DEVICE pixels; past it
    there is no more data, only wider blocks."""
    if not cps:
        return 4
    return min(max(_js_round(2.5 * 30 * cps / dev_w), 2), 16)


class _WebFineSim:
    """The page's fine-fetch ownership + parking algorithm, mirrored as a
    state machine (review iterations 2/3): one global lock; ensureFineStrips
    (an EXPLICIT engage) clears the park but is turned away while the lock is
    held; every automatic trigger goes through rearmFineStrips, which
    preserves the current pair's failed park; the finally block re-arms a
    switched-to pair through that same guard; the staleness guard precedes
    every failure mark."""

    def __init__(self):
        self.cache = {}
        self.fine = None
        self.cur = None
        self.fetching = False
        self._inflight = None

    def inflight_key(self):
        return self._inflight[0] if self._inflight else None

    def open_pair(self, key):
        self.cur = key
        self.fine = self.cache.get(key)
        self.ensure()          # openPair's explicit zoomed call

    def set_zoom(self):
        self.ensure()          # the zoom control — the explicit re-engage

    def ensure(self):
        if self.cur is None or self.fetching:
            return             # turned away at the lock
        key = self.cur
        f = self.cache.get(key) or {"tiles": [], "failed": False}
        f["failed"] = False    # an explicit (re-)engage clears the park
        self._publish(key, f)
        self.fetching = True
        self._inflight = (key, f)

    def rearm(self):
        # rearmFineStrips: the guard every automatic trigger routes through
        f = self.cache.get(self.cur)
        if f is not None and f["failed"]:
            return             # parked until an explicit re-engage
        self.ensure()

    def _publish(self, key, f):
        self.cache[key] = f
        if self.cur == key:
            self.fine = f

    def _finish(self, key):
        # the finally block: release the lock, guarded re-arm on pair change
        self.fetching = False
        if self.cur != key:
            self.rearm()

    def respond(self, ok=True):
        key, f = self._inflight
        self._inflight = None
        if self.cur != key:    # staleness guard before any failure mark
            self._finish(key)
            return
        if not ok:
            f["failed"] = True
            self._publish(key, f)
            self._finish(key)
            return
        f["tiles"].append("tile")
        self._publish(key, f)
        self._finish(key)


class TestZoomAndHeightPins(unittest.TestCase):
    """DP-37: doubled waveform lanes + the x1/x2/max time-axis zoom.

    Height: the dual-lane canvas doubles to 380 px BY DEFAULT with the
    pre-DP-37 190 px behind a toggle (small phones keep the transport above
    the fold). Zoom: three states — x1, x2, and MAX, where max is what the
    envelope DATA honestly resolves to on the device (measured column rate,
    never interpolation); the overview op's ~6 cols/s is below x2 on any real
    screen, so zoomed views fetch the SS5 context op's 50 cols/s fine strips
    through the existing DSP boundary — no new op."""

    def setUp(self):
        self.src = _page_src()

    # ---- lane height ----
    def test_lane_height_doubled_default_with_compact_available(self):
        # 380 is the default canvas rule; .compact restores the old 190
        self.assertRegex(self.src,
                         r"canvas#wave \{[^}]*height:380px")
        self.assertIn("canvas#wave.compact { height:190px; }", self.src)
        self.assertIn('id="btnWaveH"', self.src)
        self.assertIn('$("wave").classList.toggle("compact", !waveTall);',
                      self.src)
        self.assertIn("let waveTall = true;", self.src)   # doubled is the default
        # the redraw path re-sizes the backing store when HEIGHT changes too
        self.assertIn("if (c.width !== cw * dpr || c.height !== chh * dpr)",
                      self.src)

    # ---- zoom states ----
    def test_zoom_control_three_states(self):
        for needle in ('id="zoom1"', 'id="zoom2"', 'id="zoomMax"',
                       'const ZOOM_LEVELS = ["1", "2", "max"];',
                       "if (!ZOOM_LEVELS.includes(z)) return;",
                       'if (zoom === "2") return 2;',
                       "return VIEW_S / zoomFactor();"):
            self.assertIn(needle, self.src)
        # the truthful label: what max actually resolves to on this data+screen
        self.assertIn('$("zoomMax").textContent = "max (×" + maxZoom() + ")";',
                      self.src)

    def test_max_zoom_formula_pinned(self):
        # the honesty formula, verbatim, with its constant — and the measured
        # (never assumed) column rate feeding it
        self.assertIn("const MAX_PX_PER_COL = 2.5;", self.src)
        self.assertIn("return Math.min(Math.max(Math.round("
                      "MAX_PX_PER_COL * VIEW_S * cps / devW), 2), 16);",
                      self.src)
        self.assertIn("f.cps = j.n_cols / (2 * j.context_s);", self.src)
        self.assertIn("if (ov) return ov.n_cols / ov.stream_len_s;", self.src)

    def test_max_zoom_mirror_values(self):
        # fine strips (50 cols/s): a mini-class screen (~740 CSS px @2x) -> x3,
        # a phone (~338 CSS px @3x) -> x4; the overview alone (~6 cols/s) is
        # BELOW x2 everywhere -> the low clamp (the "data limit" case); tiny
        # widths hit the high clamp; no data at all -> the marker-only default
        self.assertEqual(_js_max_zoom(50, 1480), 3)
        self.assertEqual(_js_max_zoom(50, 1014), 4)
        self.assertEqual(_js_max_zoom(6, 1480), 2)
        self.assertEqual(_js_max_zoom(6, 1014), 2)
        self.assertEqual(_js_max_zoom(50, 100), 16)
        self.assertEqual(_js_max_zoom(None, 1480), 4)

    # ---- geometry stays position-true at every zoom ----
    def test_time_pixel_mapping_at_every_zoom(self):
        cw = 712.0
        cursor = 200.0
        for z in (1, 2, 4):
            # the playhead is ALWAYS the centre column
            self.assertAlmostEqual(_js_x_of(cursor, cursor, z, cw), cw / 2)
            # a marker Dt ahead of the playhead sits Dt/span of the width right
            # of centre — magnification scales the offset linearly
            for dt in (-5.0, 1.0, 3.0):
                x = _js_x_of(cursor + dt, cursor, z, cw)
                self.assertAlmostEqual(x - cw / 2, dt / (30 / z) * cw)
                # and the tap mapping is the exact inverse (crosshair placement)
                self.assertAlmostEqual(
                    _js_time_at(x, cursor, z, cw), cursor + dt, places=9)
        # zoom doubles -> on-screen offsets double (markers, shading, anchors
        # all route through the same x0)
        off1 = _js_x_of(cursor + 3, cursor, 1, cw) - cw / 2
        off2 = _js_x_of(cursor + 3, cursor, 2, cw) - cw / 2
        self.assertAlmostEqual(off2, 2 * off1)

    def test_all_geometry_routes_through_the_one_span(self):
        # drawWave, both pointer handlers, and the lane renderer all read the
        # SAME zoomed span — markers, crosshair, used-anchor flags and the
        # coverage shading cannot drift from each other at any zoom
        self.assertIn("const span = viewSpanS();", self.src)
        self.assertIn("const tL = t - span / 2;", self.src)
        self.assertIn("const x0 = tt => (tt - tL) / span * cw;", self.src)
        self.assertIn("aS.currentTime - span / 2 + ex / rect.width * span", self.src)
        self.assertIn("aS.currentTime - span / 2 + "
                      "(e.clientX - rect.left) / rect.width * span", self.src)
        draw = self.src.split("function drawWave()")[1].split(
            "function drawLane(")[0]
        self.assertNotIn("VIEW_S / 2", draw)   # no stray unzoomed geometry

    # ---- fine strips (the SS5 context op, no new DSP op) ----
    def test_fine_strips_via_the_context_op(self):
        self.assertIn('fetch("/api/align/context"', self.src)
        self.assertIn("const FINE_CTX_S = 60;", self.src)
        self.assertIn("context: FINE_CTX_S,", self.src)
        self.assertIn("const MAX_FINE_TILES = 10;", self.src)
        # lazy (zoom leaves x1), cached per pair, 429 waits for the DSP lock
        self.assertIn('if (zoom !== "1") ensureFineStrips();', self.src)
        self.assertIn("fineCache.set(key, f);", self.src)
        self.assertIn("if (r && r.status === 429 && tries < 5) {", self.src)

    def test_margin_tiles_keep_stream_fine_and_orig_falls_back(self):
        # a clamped-centre tile's orig lane is not mapping-true: the guard
        self.assertIn("Math.abs(body.orig_t - want) < 1e-5", self.src)
        self.assertIn("if (!tl.origOk) continue;", self.src)
        # and the fine orig lane is clipped to the original's coverage
        self.assertIn("clip0: Math.max(tl.t0, spanL),", self.src)

    def test_lane_sources_fine_first_overview_beneath(self):
        draw = self.src.split("function drawWave()")[1].split(
            "function drawLane(")[0]
        self.assertLess(draw.index("if (fine) {"), draw.index("if (ov) {"))
        self.assertIn("srcs.find(q => tm >= q.clip0 && tm <= q.clip1)", self.src)
        # amplitude stays on ONE scale across tile boundaries
        self.assertIn("scale: (ov && tl.rmsGain > 1e-9) "
                      "? ov.rms_gain / tl.rmsGain : 1 });", self.src)

    # ---- review iteration 1 P1: the label/max are VIEWPORT-truthful ----
    def test_label_and_max_claim_fine_only_where_fine_covers(self):
        # the honest ceiling is computed from what the viewport actually draws:
        # outside tile coverage drawLane falls back to the overview, so the cps
        # feeding maxZoom and the "fine strips" label must fall back too
        self.assertIn("if (viewportFine()) return fine.cps;", self.src)
        max_fn = self.src.split("function maxZoom()")[1].split("function ")[0]
        self.assertIn("return maxZoomFor(viewportCps());", max_fn)
        render = self.src.split("function renderZoom()")[1].split("function ")[0]
        self.assertIn("const fineNow = viewportFine();", render)
        self.assertIn('(fineNow ? "fine strips" : "overview")', render)
        # the recursion break: viewSpanS→zoomFactor→maxZoom→viewportCps→
        # viewportFine must NOT loop back through viewSpanS — coverage is
        # judged against the pure-formula candidate span (live-verify found
        # the original cycle as a stack overflow at max zoom)
        self.assertIn('const z = zoom === "2" ? 2 : zoom === "max" '
                      "? maxZoomFor(fine.cps) : 1;", self.src)
        vf = self.src.split("function viewportFine()")[1].split("function ")[0]
        self.assertNotIn("viewSpanS()", vf)
        self.assertIn("function maxZoomFor(cps)", self.src)

    def test_coverage_merge_mirror(self):
        # the page's fineCoveredRange merge-walk, mirrored — incl. out-of-order
        # tile arrival (viewport-priority fetching delivers tiles unsorted)
        contiguous = [(0.0, 120.0), (120.0, 240.0)]
        self.assertTrue(_js_fine_covered(contiguous, 10.0, 200.0))
        gap = [(0.0, 120.0), (240.0, 360.0)]
        self.assertFalse(_js_fine_covered(gap, 100.0, 250.0))
        unordered = [(240.0, 360.0), (0.0, 120.0), (120.0, 240.0)]
        self.assertTrue(_js_fine_covered(unordered, 50.0, 300.0))
        self.assertTrue(_js_fine_covered([(0.0, 120.0)], 0.0, 120.0005))  # eps
        self.assertFalse(_js_fine_covered([], 0.0, 1.0))
        # and the JS lines the mirror mirrors
        for needle in ("if (tl.t0 > at + 1e-3) break;",
                       "if (tl.t1 > at) at = tl.t1;",
                       "return at >= b - 1e-3;"):
            self.assertIn(needle, self.src)

    def test_uncovered_region_rearms_and_failure_parks(self):
        # seek/playback into an uncovered region re-arms the fetcher from the
        # per-frame draw THROUGH the guard; a failed fetch parks auto-retry
        # (no per-frame hammering) until the zoom control is re-engaged
        draw = self.src.split("function drawWave()")[1].split(
            "function drawLane(")[0]
        self.assertIn("if (mz !== lastMaxZoom) { lastMaxZoom = mz; "
                      "renderZoom(); }", draw)
        self.assertIn("rearmFineStrips();", draw)
        rearm = self.src.split("function rearmFineStrips()")[1].split(
            "async function ")[0]
        self.assertIn("if (fine && fine.failed) return;", rearm)
        self.assertIn("if (viewportFine()) return;", rearm)
        self.assertIn("f.failed = true;", self.src)
        self.assertIn("f.failed = false;    "
                      "// an explicit (re-)engage clears the auto-retry park",
                      self.src)
        # review iteration 2 P2 parity: if the pair changed while this run
        # owned the (global) fetch lock, the finally block re-arms the new
        # pair — through the guard, so an already-parked pair stays parked
        # (review iteration 3 P2)
        ens = self.src.split("async function ensureFineStrips()")[1].split(
            "\n// ----------")[0]
        self.assertIn('if (cur && cur.stem + "|" + cur.orig !== key) '
                      "rearmFineStrips();", ens)
        # every AUTOMATIC trigger goes through the guard: the only direct
        # ensureFineStrips() calls are the explicit user actions (the zoom
        # control, opening a pair) plus the guard's own dispatch
        calls = [ln.strip() for ln in self.src.splitlines()
                 if "ensureFineStrips();" in ln]
        self.assertEqual(len(calls), 3, calls)
        self.assertEqual(sum('zoom !== "1"' in ln for ln in calls), 2, calls)

    def test_parked_pair_survives_stale_completion(self):
        # review iteration 3 P2, behaviorally: B in flight -> switch to an
        # already-failed A -> B completes; NO request may occur for A until
        # the zoom control is explicitly re-engaged
        sim = _WebFineSim()
        sim.open_pair("A")
        sim.respond(ok=False)                    # A fails while current: parked
        self.assertTrue(sim.cache["A"]["failed"])
        sim.open_pair("B")                       # B's fetch takes the lock
        self.assertEqual(sim.inflight_key(), "B")
        sim.open_pair("A")                       # turned away by the lock
        self.assertTrue(sim.cache["A"]["failed"])
        sim.respond(ok=True)                     # B completes STALE
        self.assertIsNone(sim.inflight_key())    # parked A NOT retried
        self.assertTrue(sim.cache["A"]["failed"])
        sim.set_zoom()                           # explicit re-engage
        self.assertEqual(sim.inflight_key(), "A")
        self.assertFalse(sim.cache["A"]["failed"])

    def test_viewport_priority_live_window_and_eviction(self):
        # the slot grid re-reads `win` on every call (a window growing
        # mid-fetch extends the running loop), viewport slots come first, and
        # the KEPT-tiles cap evicts the tile farthest from the viewport
        centres = self.src.split("function fineCentres()")[1].split(
            "function ")[0]
        self.assertIn("for (let tc = win.t0 + FINE_CTX_S; "
                      "tc - FINE_CTX_S < win.t1; tc += step)", centres)
        self.assertIn("return list.sort((x, y) => "
                      "Math.abs(x - vc) - Math.abs(y - vc));", centres)
        self.assertIn("if (f.tiles.length >= MAX_FINE_TILES) {", self.src)
        self.assertIn("f.tiles.splice(far, 1);", self.src)
        # the priority order, mirrored: window (0, 500), viewport at 430
        step = 120.0
        grid = []
        tc = 0.0 + 60.0
        while tc - 60.0 < 500.0:
            grid.append(tc)
            tc += step
        self.assertEqual(grid, [60.0, 180.0, 300.0, 420.0, 540.0])
        self.assertEqual(sorted(grid, key=lambda x: abs(x - 430.0)),
                         [420.0, 540.0, 300.0, 180.0, 60.0])

    # ---- persistence ----
    def test_zoom_and_height_ride_the_prefs(self):
        self.assertIn("zoom: zoom,", self.src)
        self.assertIn('waveH: waveTall ? "tall" : "compact",', self.src)
        self.assertIn("if (ZOOM_LEVELS.includes(p.zoom)) "
                      "{ zoom = p.zoom; renderZoom(); }", self.src)
        self.assertIn('if (p.waveH === "tall" || p.waveH === "compact") '
                      'setWaveH(p.waveH === "tall");', self.src)


if __name__ == "__main__":
    unittest.main()
