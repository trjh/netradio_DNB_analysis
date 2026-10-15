"""DP-22 §6.9-b: the Mac-native review app (`native/align_review_app.swift`).

CI has no Swift toolchain, so the app is held to the server's math a different
way (the repo's page-pin idiom applied to Swift):

- **fixture authority** — `native/review_math_fixture.json` carries the §6.2/§6.3
  reference vectors. This file asserts the fixture equals `review_notes`' OWN
  recomputation (slope, canonical 6 dp rate, intercept, deviations,
  `expected_orig_t` in every mode), so the fixture can never drift from the
  server. `align_review_app --selftest` (run by `make review-app` after every
  build) asserts the Swift port reproduces the same fixture — the two sides pin
  each other through one shared artifact.
- **source pins** — greps for the load-bearing Swift lines: the LSQ formulas,
  the §6.2 cardinality branches (assumed-rate, refuse-last-deselect,
  frozen-while-playing + reset-on-change), the §6.3 mapping + crossing detector,
  varispeed rate application, the shared-render-clock `AVAudioTime` sampleTime
  scheduling, hard pans, gain-only solo, §6.4 margins, the note POST payload
  shape, and the thin-client guardrail (every request under `/api/align/`,
  and NO drift corrector — sample-locked by the engine, §6.10).
"""

import json
import math
import os
import re
import subprocess
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import align_inspect
import review_notes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT = os.path.join(ROOT, "native", "align_review_app.swift")
FIXTURE = os.path.join(ROOT, "native", "review_math_fixture.json")


def _swift_src():
    with open(SWIFT, "r", encoding="utf-8") as handle:
        return handle.read()


def _fixture():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


class TestFixtureMatchesServerMath(unittest.TestCase):
    """(a) the shared fixture IS review_notes' math — regenerating it from the
    server's own functions must reproduce every stored value exactly (floats
    round-trip JSON losslessly, so equality is exact, not approximate)."""

    def setUp(self):
        self.fx = _fixture()
        self.pairs = self.fx["pairs"]

    def test_rate_is_the_canonical_6dp_lsq_slope(self):
        slope = review_notes._lsq_slope(self.pairs)
        self.assertEqual(self.fx["rate_6dp"], float("%.6f" % slope))

    def test_intercept_and_deviations(self):
        r = self.fx["rate_6dp"]
        a = sum(p["orig_s"] - r * p["stream_s"] for p in self.pairs) / len(self.pairs)
        self.assertEqual(self.fx["intercept"], a)
        devs = [(p["orig_s"] - (a + r * p["stream_s"])) * 1000.0 for p in self.pairs]
        self.assertEqual(self.fx["devs_ms"], devs)

    def test_constant_rate_mapping_rows(self):
        r = self.fx["rate_6dp"]
        for row in self.fx["constant_rate"]:
            self.assertEqual(
                row["orig_t"],
                review_notes.expected_orig_t("constant-rate", r, self.pairs,
                                             row["t"]))

    def test_re_anchor_mapping_rows(self):
        r = self.fx["rate_6dp"]
        for row in self.fx["re_anchor"]:
            self.assertEqual(
                row["orig_t"],
                review_notes.expected_orig_t("re-anchor", r, self.pairs,
                                             row["t"]))

    def test_assumed_rate_rows_single_pair(self):
        ar = self.fx["assumed_rate"]
        for row in ar["points"]:
            self.assertEqual(
                row["orig_t"],
                review_notes.expected_orig_t("assumed-rate", ar["rate"],
                                             [ar["pair"]], row["t"]))

    def test_constants_match_the_server(self):
        self.assertEqual(self.fx["max_t_s"], align_inspect.MAX_T_S)
        self.assertEqual(self.fx["margin_s"], 20.0)   # §6.4, the web page's MARGIN_S

    def test_segment_plan_case(self):
        # the up-front original-lane planner (the P1 surface): the fixture case
        # carries anchors 50 ms and 10 ms apart plus §6.4 pre-roll/tail clips —
        # this mirror recomputes the plan from the same §6.3 governing-anchor
        # formula review_notes.expected_orig_t uses (unclamped, as the client
        # maps), and the Swift selftest holds planOrigSegments to the same rows
        sp = self.fx["segment_plan"]
        pairs = sp["pairs"]
        r = float("%.6f" % review_notes._lsq_slope(pairs))
        self.assertEqual(sp["rate_6dp"], r)

        def map_unclamped(t):
            gov = pairs[0]
            for p in pairs:
                if p["stream_s"] <= t:
                    gov = p
            return gov["orig_s"] + r * (t - gov["stream_s"])

        t0, win_t1, orig_len = sp["t0"], sp["win_t1"], sp["orig_len"]
        bounds = [t0] + [p["stream_s"] for p in pairs
                         if t0 < p["stream_s"] < win_t1]
        segs = []
        for i, b in enumerate(bounds):
            b_end = bounds[i + 1] if i + 1 < len(bounds) else win_t1
            if b_end <= b:
                continue
            o0 = map_unclamped(b)
            o1 = o0 + r * (b_end - b)
            if o1 <= 0 or o0 >= orig_len:
                continue
            s_t, s_o = b, o0
            if o0 < 0:
                s_t, s_o = b + (0 - o0) / r, 0.0
            e_t, e_o = b_end, o1
            if o1 > orig_len:
                e_t, e_o = b + (orig_len - o0) / r, orig_len
            segs.append({"start_stream_t": s_t, "end_stream_t": e_t,
                         "start_file_s": s_o, "end_file_s": e_o})
        self.assertEqual(sp["segments"], segs)
        # the case must actually contain closely spaced anchors (sub-100 ms) —
        # the spacing the old deferred-arming design could miss
        gaps = [q["stream_s"] - p["stream_s"]
                for p, q in zip(pairs, pairs[1:])]
        self.assertTrue(any(g <= 0.1 for g in gaps), gaps)
        # and both §6.4 clips: a pre-roll entry after t0, a tail clip at len
        self.assertGreater(segs[0]["start_stream_t"], t0)
        self.assertEqual(segs[0]["start_file_s"], 0.0)
        self.assertEqual(segs[-1]["end_file_s"], orig_len)
        self.assertLess(segs[-1]["end_stream_t"], win_t1)

    def test_fixture_pairs_validate_as_a_note_mapping(self):
        # the fixture's rate/pairs form a canonical fitted-mode session by the
        # server's own rules (validate()'s mapping stage would accept them)
        r = self.fx["rate_6dp"]
        t = 250.0
        exp = review_notes.expected_orig_t("constant-rate", r, self.pairs, t)
        row = next(x for x in self.fx["constant_rate"] if x["t"] == t)
        self.assertLessEqual(abs(row["orig_t"] - exp),
                             review_notes.MAPPING_TOLERANCE_S)


class TestSwiftMathPins(unittest.TestCase):
    """(b) the Swift lines the fixture parity rests on — if a formula is edited,
    the pin fails here and the selftest fails at build time."""

    def setUp(self):
        self.src = _swift_src()

    # ---- §6.2: the fit ----
    def test_lsq_slope_formula(self):
        for needle in (
                "num += (p.streamS - sm) * (p.origS - om)",
                "den += (p.streamS - sm) * (p.streamS - sm)",
                "if den <= 1e-9 { return nil }",
        ):
            self.assertIn(needle, self.src)

    def test_canonical_6dp_rounding(self):
        self.assertIn(
            "func round6(_ x: Double) -> Double { (x * 1e6).rounded() / 1e6 }",
            self.src)
        self.assertIn("r = round6(slope)", self.src)

    def test_intercept_and_deviation_formulas(self):
        self.assertIn("let a = pts.reduce(0.0) { $0 + ($1.origS - r * $1.streamS) }"
                      " / Double(pts.count)", self.src)
        self.assertIn("devs[p.id] = (p.origS - (a + r * p.streamS)) * 1000.0",
                      self.src)

    def test_cardinality_assumed_rate_branch(self):
        # §6.2: exactly one pair = flagged assumed-rate (Pass-1 rate else 1.000)
        self.assertIn("if pts.count == 1 {", self.src)
        self.assertIn("r = pairRate != nil ? round6(pairRate!) : 1.0", self.src)
        self.assertIn('(fit?.assumed ?? false) ? "assumed-rate" : mode', self.src)
        self.assertIn("(assumed)", self.src)

    def test_cardinality_last_pair_cannot_be_deselected(self):
        self.assertIn("if sel.count == 1 { return }", self.src)

    def test_selection_frozen_while_playing_and_reset_on_change(self):
        toggle = self.src.split("func toggleAnchor(")[1].split("func setMode(")[0]
        self.assertIn("if playing { return }", toggle)
        self.assertIn("resetPlayback()", toggle)
        set_mode = self.src.split("func setMode(")[1].split("var verdict")[0]
        self.assertIn("if playing { return }", set_mode)
        self.assertIn("resetPlayback()", set_mode)
        # the reset returns to the §6.4 window start
        reset = self.src.split("func resetPlayback()")[1].split("func ")[0]
        self.assertIn("cursor = winT0", reset)

    def test_duplicate_stream_instant_guard_mirrors_the_server(self):
        self.assertIn('String(format: "%.6f", $0.streamS) == s6', self.src)

    def test_verdict_line_present(self):
        self.assertIn("multi-rate suspected", self.src)

    def test_anchor_key_regex_mirrors_the_server(self):
        m = re.search(r'NSRegularExpression\(pattern: "(.+?)"\)', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), review_notes._ANCHOR_K_RE.pattern)

    # ---- §6.3: the mapping + crossings ----
    def test_mapping_formulas(self):
        for needle in (
                'if effMode == "constant-rate" { return fit.a + fit.r * t }',
                "for p in fit.anchors where p.streamS <= t { gov = p }",
                "return gov.origS + fit.r * (t - gov.streamS)",
                "min(max(mapOrig(t, fit: fit, effMode: effMode), 0.0), K.maxTS)",
        ):
            self.assertIn(needle, self.src)

    def test_crossing_detector_counts_passed_anchors(self):
        self.assertIn("filter { $0.streamS <= t }.count", self.src)
        self.assertIn("if cc > lastCross {", self.src)

    def test_seek_reseats_from_the_nearest_prior_anchor(self):
        seek = self.src.split("func seek(to t: Double)")[1].split("func ")[0]
        self.assertIn("lastCross = crossCount(clamped, fit: fit)", seek)
        # a seek never touches the model — no fit recompute in the seek path
        self.assertNotIn("recomputeFit", seek)

    # ---- the engine: sample-locked §6.9-b playback ----
    def test_varispeed_rate_application(self):
        # §6.9-b varispeed by SAMPLE-RATE DECLARATION: the original's PCM is
        # scheduled declared at fsO * r (same audible pitch+tempo varispeed as
        # AVAudioUnitVarispeed, exact double precision). The unit itself must
        # stay OUT of the graph: it races the upstream node's timeline at r×
        # wall speed and skews play(at:) by horizon × (r − 1) — measured
        # +166 ms on an 8 s horizon in the live self-test.
        self.assertIn("let want = fsO * r", self.src)
        self.assertIn("ensureOrigRate(fit.r)", self.src)
        self.assertIn("AVAudioFormat(standardFormatWithSampleRate: want", self.src)
        self.assertNotIn("AVAudioUnitVarispeed()", self.src)   # never re-attach it
        self.assertIn("Don't reintroduce the unit", self.src)

    def test_shared_render_instant_scheduling(self):
        # both branches schedule against the SAME engine render-clock instant,
        # carried on the HOST-CLOCK leg: AVAudioTime sampleTime legs are
        # NODE-LOCAL timelines, and mixing them across a 16 kHz stream node and
        # a 44.1 kHz original node measured as a flat +514 ms skew in the live
        # self-test. The comment pin keeps that lesson from being reintroduced.
        for needle in (
                "guard let rt = out.lastRenderTime, rt.isSampleTimeValid, "
                "rt.isHostTimeValid",
                "anchorHost = rt.hostTime + "
                "AVAudioTime.hostTime(forSeconds: K.scheduleLeadS)",
                "AVAudioTime(hostTime: anchorHost + "
                "AVAudioTime.hostTime(forSeconds: d))",
                "sampleTime legs are NODE-LOCAL",
        ):
            self.assertIn(needle, self.src)

    def test_stale_completion_cannot_kill_a_new_run(self):
        # live self-test find: node.stop() fires the completion callback of the
        # OLD run after the new one started — the generation guard is load-bearing
        self.assertIn("runGen += 1", self.src)
        self.assertIn("if gen == self.runGen, self.running { self.onStreamEnd?() }",
                      self.src)

    def test_no_drift_corrector_exists(self):
        # §6.10 native bar: sample-locked by the engine, not by nudging — the
        # readout is telemetry only. The web page's corrector names must be gone.
        self.assertIn("no drift corrector exists in this file", self.src)
        self.assertNotIn("DRIFT_NUDGE", self.src)
        self.assertNotIn("correctDrift", self.src)
        self.assertIn("corrections: none (sample-locked; no corrector exists)",
                      self.src)

    def test_hard_pans_and_swap_flip_both(self):
        self.assertIn("streamMix.pan = swap ? 1 : -1", self.src)
        self.assertIn("origMix.pan = swap ? -1 : 1", self.src)

    def test_solo_hold_and_latch_are_gain_only(self):
        # DP-36: solo and the per-source volumes land in ONE place (applyLevels)
        # as mixer-gain changes — never a stop/pause; solo zeroes the original
        # WITHOUT touching its stored volume
        self.assertIn("origMix.outputVolume = (holdSolo || latchSolo)", self.src)
        self.assertIn("? 0 : Float(comp(origFile) * volOrig)", self.src)
        solo = self.src.split("func applyLevels(volStream:")[1].split(
            "\n    }")[0]
        self.assertNotIn(".stop()", solo)
        self.assertNotIn(".pause()", solo)

    def test_whole_lane_scheduled_up_front(self):
        # review iteration 1 P1: NOTHING is armed after playback begins — the
        # entire original lane (audio slices back-to-back, silence for the §6.4
        # margins) is scheduled before play, so no timer latency can miss a
        # boundary at any anchor spacing (the fixture's segment_plan pins the
        # closely-spaced case)
        self.assertIn("segments = planOrigSegments(from: t0", self.src)
        self.assertIn("scheduleLane(from: t0)", self.src)
        self.assertIn("scheduled up front in its ENTIRETY", self.src)
        self.assertIn("fileStartS: nil", self.src)          # silence pieces
        self.assertNotIn("func arm(", self.src)             # no deferred arming
        # cumulative wall positions, so per-piece rounding never accumulates
        self.assertIn("((streamT - t0) * declaredRate).rounded()", self.src)

    def test_start_instant_chosen_after_all_scheduling(self):
        # review iteration 2 P1: the shared start instant must be read off the
        # render clock only AFTER both lanes are fully queued — however long
        # slicing/copying takes, the deadline cannot expire mid-preparation,
        # because the only work between choosing it and playback is the two
        # play(at:) calls. Enforced as a source-order pin over startRun's body.
        body = self.src.split("func startRun(")[1].split(
            "\n    private func scheduleLane(")[0]
        order = [body.index("streamPlayer.scheduleSegment("),
                 body.index("segments = planOrigSegments(from: t0"),
                 body.index("scheduleLane(from: t0)"),
                 body.index("chosen\n        // only AFTER every buffer"),
                 body.index("anchorHost = rt.hostTime"),
                 body.index("streamPlayer.play(at:"),
                 body.index("origPlayer.play(at:")]
        self.assertEqual(order, sorted(order), "startRun is out of order")
        # and queueing the lane never starts the node early
        lane_fn = self.src.split("private func scheduleLane(")[1].split(
            "\n    private func silence(")[0]
        self.assertNotIn(".play(", lane_fn)

    def test_margins_are_20s_clamped_and_stream_solo(self):
        self.assertIn("static let marginS = 20.0", self.src)
        self.assertIn("winT0 = max(0, start - K.marginS)", self.src)
        self.assertIn("stream solo (margin)", self.src)
        self.assertIn("clamped, the overhang is unavailable", self.src)

    def test_time_cap_mirrors_the_server(self):
        self.assertIn("static let maxTS = 24.0 * 3600.0", self.src)
        self.assertIn("static let maxTextLen = 500", self.src)

    # ---- §6.6: the note POST ----
    def test_note_post_payload_shape(self):
        # exactly the review_notes.validate() contract, like the web page's pin
        self.assertIn('postJSON("/api/align/review-note"', self.src)
        for needle in ('"stem": cur.stem',
                       '"orig": cur.orig',
                       '"stream_t": round6(pos)',
                       '"orig_t": round6(mapOrigClamped(pos, fit: fit, '
                       'effMode: effMode))',
                       '"rate": fit.r',
                       '"mode": effMode',
                       '"pairs": fit.anchors.map { ["k": $0.k, '
                       '"stream_s": $0.streamS, "orig_s": $0.origS] }'):
            self.assertIn(needle, self.src)

    def test_note_placement_only_while_paused(self):
        place = self.src.split("func placeNote(at t: Double)")[1].split("func ")[0]
        self.assertIn("guard !playing", place)

    def test_note_errors_surface_honestly(self):
        self.assertIn('noteStatus = "HTTP \\(status): '
                      '\\((j["error"] as? String) ?? "save failed")"', self.src)

    # ---- §6.5/§6.2: the picker + zero-pair gate ----
    def test_picker_ingests_both_sources_and_gates_zero_pairs(self):
        self.assertIn('{ ingest(h, "hints") }', self.src)
        self.assertIn('{ ingest(pl, "placed") }', self.src)
        self.assertIn("seat a point in /align (or run match-hints) first",
                      self.src)
        self.assertIn("NOT-FOUND", self.src)

    # ---- the #297 restack sweep (2026-08-18) ----
    def test_drift_readout_carries_the_info_affordance(self):
        # §6.3 (owner requirement 2026-08-11): what the number measures, 0 ms =
        # on target, pointer to the plan section — hover .help + ⓘ popover
        self.assertIn("0 ms = perfectly on target", self.src)
        self.assertIn("docs/PLAN_align_tool.md §6.3", self.src)
        self.assertIn(".popover(isPresented: $showDriftInfo)", self.src)

    def test_wv_refusal_is_final_and_named_never_retried(self):
        # #294 removed the transcode tier: the route's JSON refusal (naming the
        # DP-23 sync) is surfaced verbatim and marked final — no retry loop
        # toward a tier that no longer exists
        self.assertIn('(error as NSError).userInfo["alignFinal"] == nil', self.src)
        # BOTH stages mark refusals final — the probe hits the 503 first (the
        # independent-review finding: the download-stage code alone was
        # unreachable for the wv case)
        probe = self.src.split("func probeIdentity(")[1].split("\n    private func ")[0]
        self.assertIn('info["alignFinal"] = true', probe)
        fetch = self.src.split("func fetchAudio(")[1].split("\n    private func ")[0]
        self.assertIn('info["alignFinal"] = true', fetch)
        self.assertIn("run the DP-23 sync (scripts/sync_sources.sh)", self.src)
        self.assertNotIn("transcode?", self.src)

    def test_default_selection_dedups_duplicate_instants(self):
        # the server refuses duplicate anchor stream_s at 6 dp and hints+placed
        # carry the same instants — select-all must not doom every note POST
        # (independent-review finding on #296; same bug here)
        open_pair = self.src.split("func openPair(")[1].split("func ")[0]
        self.assertIn('String(format: "%.6f", $0)', open_pair)
        self.assertIn("!seen.contains(s6)", open_pair)

    def test_duplicate_instant_tiebreak_is_deterministic(self):
        # Swift's sort is not stable: equal instants tie-break by source
        # (hints first), matching the web page's stable insertion order
        self.assertIn("a != b ? a < b : $0.source < $1.source", self.src)

    def test_rate_note_reaches_the_picker(self):
        # analysis #130's owner rate rules ride rate_note into the pair row
        self.assertIn('set["rate_note"] as? String', self.src)
        self.assertIn('Text("rate: " + note)', self.src)

    # ---- guardrails: a THIN client of the align API ----
    def test_every_request_path_is_under_api_align(self):
        paths = re.findall(r'"(/api/[^"]*)"', self.src)
        self.assertTrue(paths, "the app must call the align API somewhere")
        for p in paths:
            self.assertTrue(p.startswith("/api/align/"), p)

    def test_no_foreign_hosts_and_the_default_base(self):
        self.assertIn('static let defaultBaseURL = "http://127.0.0.1:8766"',
                      self.src)
        self.assertIn('env["NETRADIO_ALIGN_URL"]', self.src)
        # the only URL literal in the file is the loopback default
        urls = re.findall(r'https?://[^"\s]+', self.src)
        self.assertEqual({u.rstrip(";,)") for u in urls},
                         {"http://127.0.0.1:8766"})

    def test_no_dsp_reimplementation(self):
        # envelopes come from the overview op; the app never computes analysis.
        # ("streamalign" may appear in operator GUIDANCE text — "run streamalign
        # match-hints" — but never as a Process/launch invocation.)
        for banned in ("fft", "FFT", "correlate", "Process(", "launchPath",
                       "NSTask"):
            self.assertNotIn(banned, self.src)

    def test_cache_revalidates_against_the_resolved_source(self):
        # review iteration 1 P2: the audio cache must never silently replay
        # stale bytes — a 1-byte Range probe reads the server-resolved identity
        # (X-Review-Source + the Content-Range total) and the cache key embeds
        # name+size, so a re-rip or a different resolved tier MISSES
        self.assertIn('req.setValue("bytes=0-0", forHTTPHeaderField: "Range")',
                      self.src)
        self.assertIn('http.value(forHTTPHeaderField: "Content-Range")',
                      self.src)
        self.assertIn('http.value(forHTTPHeaderField: "X-Review-Source")',
                      self.src)
        # review iteration 2 P2: the server's revision token (mtime_ns-size,
        # X-Review-Source-Rev) joins the key, so an in-place re-rip at an
        # UNCHANGED size still misses; the server side is pinned by
        # tests/test_review_audio.py::TestSourceRevisionHeader
        self.assertIn('http.value(forHTTPHeaderField: "X-Review-Source-Rev")',
                      self.src)
        self.assertIn('"\\(prefix)-\\(safe)-\\(ident.size)-\\(rev).\\(ext)"',
                      self.src)
        self.assertIn("probeIdentity(path: path)", self.src)

    def test_selftest_entry_point_consumes_the_shared_fixture(self):
        self.assertIn('args[1] == "--selftest"', self.src)
        self.assertIn("review_math_fixture.json", self.src)
        self.assertIn("func runSelftest(fixturePath: String) -> Int", self.src)
        # incl. the P1 planner surface
        self.assertIn("planOrigSegments(from: t0, fit: spFit", self.src)

    # ---- DP-36: unmodified levels, per-source volumes, down-mix compensation ----
    def test_stereo_downmix_compensation_constant(self):
        # AVAudioMixerNode's stereo→mono down-mix is (L+R)/√2 (measured: +3.01
        # dBFS on full-scale in-phase stereo — clipping); the ×(1/√2) comp
        # makes the ear signal exactly 0.5·(L+R) at unity. The math, mirrored:
        import math
        comp = 0.5 * math.sqrt(2)
        self.assertAlmostEqual(comp, 1 / math.sqrt(2), places=15)
        # (L+R)/√2 × comp = 0.5·(L+R) — the standard mono down-mix
        self.assertAlmostEqual((1 / math.sqrt(2)) * comp, 0.5, places=15)
        self.assertIn(
            "static let stereoDownmixComp = 0.5 * 2.0.squareRoot()", self.src)

    def test_comp_is_per_branch_and_mono_passes_at_unity(self):
        self.assertIn("((file?.processingFormat.channelCount ?? 2) > 1) "
                      "? K.stereoDownmixComp : 1.0", self.src)
        self.assertIn(
            "streamMix.outputVolume = Float(comp(streamFile) * volStream)",
            self.src)

    def test_volume_range_and_unity_defaults(self):
        self.assertIn("static let volMin = 0.0", self.src)
        self.assertIn("static let volMax = 2.0", self.src)
        self.assertIn("@Published var volStream = 1.0", self.src)
        self.assertIn("@Published var volOrig = 1.0", self.src)
        self.assertIn("Slider(value: value, in: K.volMin...K.volMax)", self.src)

    def test_volume_reset_and_amber_cue(self):
        # double-click the label or % readout → back to unity; above 100 % the
        # readout goes amber (clipping by user choice — no limiter, on purpose)
        self.assertIn(".onTapGesture(count: 2) { value.wrappedValue = 1.0 }",
                      self.src)
        self.assertIn("pct > 100 ? Pal.warn : Pal.ink", self.src)
        self.assertNotIn("AVAudioUnitEffect", self.src)   # no limiter node

    def test_volumes_live_during_playback_and_persisted(self):
        # didSet → applyLevels (a running-graph gain change), and the volumes
        # ride UserDefaults through the same clamp as the sliders
        self.assertIn("{ didSet { applyLevels(); saveVolPrefs() } }", self.src)
        self.assertIn('"netradio.review.volStream"', self.src)
        self.assertIn('"netradio.review.volOrig"', self.src)
        self.assertIn("min(max(d.double(forKey: Self.volStreamKey), K.volMin), "
                      "K.volMax)", self.src)

    def test_levels_and_pans_applied_after_engine_start(self):
        # the DP-36 offline harness measured AVAudioMixing values set on a
        # not-yet-started engine being dropped when the units are instantiated —
        # startRun must start the engine FIRST, then set pans + levels
        body = self.src.split("func startRun(")[1].split(
            "\n    private func scheduleLane(")[0]
        order = [body.index("try engine.start()"),
                 body.index("setPans(swap: swap)"),
                 body.index("applyLevels(volStream: volStream")]
        self.assertEqual(order, sorted(order), "startRun levels out of order")
        # and the run carries the model's volumes
        self.assertIn("volStream: volStream, volOrig: volOrig", self.src)

    def test_ios_path_and_build_pattern_documented(self):
        # §6.9-b/§6.11: the header states the iOS port path + the swiftc-only build
        head = self.src.split("import SwiftUI")[0]
        self.assertIn("full Xcode + signing", head)
        self.assertIn("expires\n// every 7 days", head)
        self.assertIn("Command Line Tools ONLY", head)


def _mirror_max_zoom(cps, dev_w):
    """The shared DP-37 MAX-honesty formula (both surfaces): round(2.5 * 30 *
    cps / devW) clamped to [2, 16] — JS Math.round / Swift .rounded() both
    round half UP, so the mirror must too (Python round() half-evens)."""
    if not cps:
        return 4
    return min(max(math.floor(2.5 * 30 * cps / dev_w + 0.5), 2), 16)


class _FineOwnerSim:
    """ensureFineStrips' pair-switch ownership algorithm, mirrored as a state
    machine (review iteration 2 P2). One global lock; a run's state (`f`) is
    scoped to the pair it was started for; publish() stores under the run's
    own key and updates the live `fine` only while that pair is open; the
    defer re-arms the current pair when the lock was held for another; the
    staleness guard precedes every failure mark."""

    def __init__(self):
        self.cache = {}
        self.fine = None
        self.cur = None
        self.fetching = False
        self.rearmed = []
        self._inflight = None      # (key, working-copy f)

    def inflight_key(self):
        return self._inflight[0] if self._inflight else None

    def open_pair(self, key):
        # openPair: adopt the pair's cached strips, then the zoomed re-arm
        self.cur = key
        self.fine = self.cache.get(key)
        self.ensure()

    def ensure(self):
        if self.cur is None or self.fetching:
            return                           # turned away at the guard
        key = self.cur
        f = self.cache.get(key) or {"tiles": [], "failed": False}
        f["failed"] = False                  # (re-)engage clears the park
        self._publish(key, f)
        self.fetching = True
        self._inflight = (key, f)

    def _publish(self, key, f):
        self.cache[key] = f
        if self.cur == key:
            self.fine = f

    def _finish(self, key):
        # the defer: release the lock, then re-arm a switched-to pair THROUGH
        # the guard (rearmFineStrips) — a parked pair stays parked (iter 3)
        self.fetching = False
        if self.cur != key:
            self.rearm()

    def rearm(self):
        f = self.cache.get(self.cur)
        if f is not None and f["failed"]:
            return                       # parked until an explicit re-engage
        self.rearmed.append(self.cur)
        self.ensure()

    def respond(self, ok=True):
        key, f = self._inflight
        self._inflight = None
        if self.cur != key:                  # guard cur == key else return
            self._finish(key)
            return
        if not ok:                           # failure while CURRENT: park key only
            f["failed"] = True
            self._publish(key, f)
            self._finish(key)
            return
        f["tiles"].append("tile")
        self._publish(key, f)
        self._finish(key)


class TestZoomAndHeightPins(unittest.TestCase):
    """DP-37 (Mac surface): doubled lanes + the x1/x2/max time-axis zoom,
    mirroring the web page — same states, same MAX-honesty formula (max is
    what the envelope DATA resolves to on the screen, measured column rate,
    never interpolation), same fine strips through the SS5 context op."""

    def setUp(self):
        self.src = _swift_src()
        with open(os.path.join(ROOT, "public", "review.html"),
                  encoding="utf-8") as handle:
            self.web = handle.read()

    def test_lane_block_doubled(self):
        # 2 x the POC's 190, allocated via the one constant
        self.assertIn("static let waveH: CGFloat = 380", self.src)
        self.assertIn(".frame(height: K.waveH)", self.src)

    def test_zoom_three_states_with_truthful_max_label(self):
        self.assertIn('static let zoomLevels = ["1", "2", "max"]', self.src)
        self.assertIn('if zoom == "2" { return 2 }', self.src)
        self.assertIn('if zoom == "max" { return Double(maxZoom) }', self.src)
        self.assertIn("var viewSpan: Double { K.viewS / zoomFactor }", self.src)
        # the segmented control's max tag carries the resolved xN, not a guess
        self.assertIn('Text("max (×\\(model.maxZoom))").tag("max")',
                      self.src)

    def test_max_zoom_formula_pinned_and_mirrors_the_web(self):
        self.assertIn("static let maxPxPerCol = 2.5", self.src)
        self.assertIn("(K.maxPxPerCol * K.viewS * cps / max(waveDevW, 1))"
                      ".rounded()", self.src)
        self.assertIn("min(max(Int(z), 2), 16)", self.src)
        # measured column rates, never assumed — and VIEWPORT-truthful: fine
        # counts only while fine tiles cover the visible span (review
        # iteration 1 P1)
        self.assertIn("if viewportFine, let c = fine?.cps { return c }", self.src)
        self.assertIn("if let ov { return ov.nCols / ov.streamLenS }", self.src)
        self.assertIn("maxZoomFor(viewportCps)", self.src)
        # the recursion break, mirrored from the web (viewSpan→zoomFactor→
        # maxZoom→viewportCps→viewportFine must not loop back through viewSpan)
        self.assertIn('let z = zoom == "2" ? 2.0 : zoom == "max" '
                      "? Double(maxZoomFor(fcps)) : 1.0", self.src)
        self.assertIn("func maxZoomFor(_ cps: Double?) -> Int", self.src)
        # the same constants on both surfaces (one formula, two ports)
        self.assertIn("const MAX_PX_PER_COL = 2.5;", self.web)
        self.assertIn("const FINE_CTX_S = 60;", self.web)
        self.assertIn("static let fineCtxS = 60.0", self.src)
        self.assertIn("static let maxFineTiles = 10", self.src)
        self.assertIn("const MAX_FINE_TILES = 10;", self.web)
        # and the mirror's numbers (see test_review_page for the web's own)
        self.assertEqual(_mirror_max_zoom(50, 1480), 3)
        self.assertEqual(_mirror_max_zoom(6, 2800), 2)
        self.assertEqual(_mirror_max_zoom(None, 1480), 4)

    def test_geometry_routes_through_the_one_span(self):
        # draw, click mapping and marker placement all read model.viewSpan —
        # markers/crosshair/shading stay position-true at every zoom
        self.assertIn("let span = model.viewSpan", self.src)
        self.assertIn("let tL = model.cursor - span / 2", self.src)
        self.assertIn("func x0(_ t: Double) -> CGFloat "
                      "{ CGFloat((t - tL) / span) * cw }", self.src)
        draw = self.src.split("private func draw(")[1].split(
            "private func drawLane(")[0]
        self.assertNotIn("K.viewS", draw)   # no stray unzoomed geometry
        # the device-pixel width the formula needs is MEASURED off the canvas
        self.assertIn("model.waveDevW = Double(size.width) * scale", self.src)

    def test_fine_strips_via_the_context_op(self):
        self.assertIn('api.postJSON("/api/align/context", body: body)', self.src)
        self.assertIn("f.cps = nCols / (2 * ctxS)", self.src)   # measured
        self.assertIn("if transportError == nil, status == 429, attempt < 5 {",
                      self.src)
        self.assertIn("fineCache[key] = f", self.src)
        # margin tiles: fine stream lane stays, the orig lane falls back
        self.assertIn("abs(reqOrig - want) < 1e-5", self.src)
        self.assertIn("if !tl.origOk { continue }", self.src)
        # fine first, overview beneath, one amplitude scale
        self.assertIn("srcs.first(where: { tm >= $0.clip0 && tm <= $0.clip1 })",
                      self.src)
        self.assertIn("model.ov!.rmsGain / tl.rmsGain", self.src)

    def test_viewport_truth_rearm_and_eviction(self):
        # review iteration 1 P1, the Mac port: coverage merge-walk, the
        # "fine strips" label gated on viewport coverage, seek/playback
        # re-arming the fetcher, failure parking, viewport-priority slots
        # (re-reading the live window), and the farthest-tile eviction
        for needle in ("func fineCoveredRange(",
                       "if tl.t0 > at + 1e-3 { break }",
                       "if tl.t1 > at { at = tl.t1 }",
                       "return at >= b - 1e-3",
                       'let src = viewportFine ? "fine strips" : "overview"',
                       "func rearmFineStrips()",
                       "rearmFineStrips()   // playback advancing",
                       "f.failed = true",
                       "f.failed = false    "
                       "// an explicit (re-)engage clears the auto-retry park",
                       "return list.sorted { abs($0 - vc) < abs($1 - vc) }",
                       "if f.tiles.count >= K.maxFineTiles {",
                       "f.tiles.remove(at: far)"):
            self.assertIn(needle, self.src)
        seek = self.src.split("func seek(to t: Double)")[1].split("func ")[0]
        self.assertIn("rearmFineStrips()", seek)

    # ---- review iteration 2 P2: pair-switch ownership of the fine fetch ----
    def test_fine_fetch_state_is_key_scoped_with_defer_rearm(self):
        # every mutation goes through the run-local working copy + publish()
        # (stores under the run's OWN key; updates the live `fine` only while
        # that pair is open); the defer re-arms the CURRENT pair whenever the
        # lock was held for a different one; centre picking reads the run's
        # copy, not the re-paired live state
        ens = self.src.split("func ensureFineStrips()")[1].split(
            "\n    // MARK")[0]
        for needle in ("var f = fineCache[key] ?? FineStrips()",
                       "func publish() {",
                       "fineCache[key] = f",
                       "if cur?.id == key { fine = f }",
                       "if cur?.id != key { rearmFineStrips() }",
                       "guard let tc = nextFineCentre(in: f) else { break }"):
            self.assertIn(needle, ens)
        # the staleness guard comes BEFORE any failure marking, so a stale
        # run can never park (or store under) another pair
        self.assertLess(ens.index("guard cur?.id == key else { return }"),
                        ens.index("f.failed = true"))
        # the old wrong-key patterns are gone
        self.assertNotIn("fine?.failed = true", self.src)
        self.assertNotIn("if let f = fine { fineCache[key] = f }", self.src)

    def test_pair_switch_ownership_simulation(self):
        # the reviewer's scenario, run as a state machine mirroring
        # ensureFineStrips' control flow: cached-overview A -> in-flight B ->
        # back to A, success AND failed-request branches
        sim = _FineOwnerSim()
        # B's fetch is in flight when the operator switches to A
        sim.open_pair("B")
        self.assertEqual(sim.inflight_key(), "B")
        sim.open_pair("A")                       # ensure() turned away (lock held)
        self.assertEqual(sim.inflight_key(), "B")
        # B's stale response lands: dropped, and the defer re-arms A
        sim.respond(ok=True)
        self.assertEqual(sim.rearmed, ["A"])
        self.assertEqual(sim.inflight_key(), "A")
        self.assertEqual(sim.cache["B"]["tiles"], [])   # stale tile dropped
        # A's own response fills A only
        sim.respond(ok=True)
        self.assertEqual(sim.cache["A"]["tiles"], ["tile"])
        self.assertIs(sim.fine, sim.cache["A"])
        # failed branch: B in flight, switch to A, the failure is STALE — it
        # must park nothing; A's re-armed fetch then fails while current and
        # parks A alone
        sim2 = _FineOwnerSim()
        sim2.open_pair("B")
        sim2.open_pair("A")
        sim2.respond(ok=False)                   # stale failure: no park anywhere
        self.assertFalse(sim2.cache["B"]["failed"])
        self.assertFalse(sim2.cache["A"]["failed"])
        self.assertEqual(sim2.inflight_key(), "A")   # re-armed
        sim2.respond(ok=False)                   # current failure: parks A only
        self.assertTrue(sim2.cache["A"]["failed"])
        self.assertFalse(sim2.cache["B"]["failed"])
        # re-engaging zoom clears the park (ensure() resets failed)
        sim2.open_pair("A")
        self.assertFalse(sim2.cache["A"]["failed"])

    def test_parked_pair_survives_stale_completion(self):
        # review iteration 3: a deliberately parked pair must NOT be retried
        # just because another pair's stale request completed — the deferred
        # re-arm goes through the guard; only an explicit engage clears it
        sim = _FineOwnerSim()
        sim.open_pair("A")
        sim.respond(ok=False)                    # A fails while current: parked
        self.assertTrue(sim.cache["A"]["failed"])
        sim.open_pair("B")                       # B's fetch takes the lock
        self.assertEqual(sim.inflight_key(), "B")
        sim.open_pair("A")                       # turned away by the lock; park intact
        self.assertTrue(sim.cache["A"]["failed"])
        sim.respond(ok=True)                     # B completes STALE
        self.assertIsNone(sim.inflight_key())    # no request for parked A
        self.assertTrue(sim.cache["A"]["failed"])
        sim.ensure()                             # the explicit zoom re-engage
        self.assertEqual(sim.inflight_key(), "A")
        self.assertFalse(sim.cache["A"]["failed"])

    def test_zoom_rides_the_prefs(self):
        self.assertIn('static let zoomKey = "netradio.review.zoom"', self.src)
        self.assertIn("UserDefaults.standard.set(zoom, forKey: Self.zoomKey)",
                      self.src)
        self.assertIn("if let z = d.string(forKey: Self.zoomKey), "
                      "K.zoomLevels.contains(z) {", self.src)


def _recipe_shell(target):
    """The target's recipe as one bash script: tabs and a leading `@` stripped, `$$` -> `$`."""
    with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as fh:
        mk = fh.read()
    body = mk.split("\n%s:" % target, 1)[1].split("\n", 1)[1]
    lines = []
    for line in body.split("\n"):
        if not line.startswith("\t"):
            break
        lines.append(line[1:].lstrip("@").replace("$$", "$"))
    return "\n".join(lines)


class TheMakeTarget(unittest.TestCase):
    """`make review-app-run` finds a LAN-bound align server without a hand-typed URL."""

    def test_review_app_run_derives_the_url_from_the_bind_host(self):
        recipe = _recipe_shell("review-app-run")
        self.assertIn('if [ -z "$NETRADIO_ALIGN_URL" ]', recipe)
        self.assertIn("NETRADIO_ALIGN_HOST:-127.0.0.1", recipe)
        self.assertIn("NETRADIO_ALIGN_PORT:-8766", recipe)
        self.assertIn('"0.0.0.0" ] && h=127.0.0.1', recipe)     # a wildcard bind is loopback-reachable
        self.assertIn("./native/align_review_app", recipe)

    def test_review_app_builds_then_runs_the_math_selftest(self):
        recipe = _recipe_shell("review-app")
        self.assertIn("swiftc -parse-as-library -O -o native/align_review_app "
                      "native/align_review_app.swift", recipe)
        self.assertIn("./native/align_review_app --selftest native/review_math_fixture.json",
                      recipe)

    def test_the_env_file_is_read_never_executed(self):
        recipe = _recipe_shell("review-app-run")
        self.assertIn(". scripts/lib/env.sh; env_load .env;", recipe)
        self.assertNotIn(". .env", recipe)
        self.assertNotIn("set -a", recipe)


class TheMakeTargetBehaves(unittest.TestCase):
    """Run the recipe's own shell (the binary swapped for `echo`) under each bind case, with
    the .env the recipe loads swapped for a scratch one: a reordering that only looks right
    fails here."""

    def _run(self, dotenv="", **env):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(dotenv)
            sh = _recipe_shell("review-app-run").replace("./native/align_review_app", "echo RUN")
            sh = sh.replace("env_load .env", "env_load %s" % path)
            full = {k: v for k, v in os.environ.items() if not k.startswith("NETRADIO_")}
            full.update(env)
            out = subprocess.run(["bash", "-c", sh], cwd=ROOT, env=full,
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_explicit_url_wins(self):
        out = self._run(NETRADIO_ALIGN_URL="http://x:1", NETRADIO_ALIGN_HOST="10.0.0.9")
        self.assertIn("review app -> http://x:1\n", out)

    def test_lan_bind_is_derived(self):
        out = self._run(NETRADIO_ALIGN_HOST="10.0.0.9", NETRADIO_ALIGN_PORT="8766")
        self.assertIn("review app -> http://10.0.0.9:8766\n", out)

    def test_wildcard_and_unset_binds_are_loopback(self):
        self.assertIn("http://127.0.0.1:8766", self._run(NETRADIO_ALIGN_HOST="0.0.0.0"))
        self.assertIn("http://127.0.0.1:9001", self._run(NETRADIO_ALIGN_PORT="9001"))

    def test_the_env_file_supplies_the_bind(self):
        out = self._run("NETRADIO_ALIGN_HOST=10.0.0.9\nNETRADIO_ALIGN_PORT=9002\n")
        self.assertIn("review app -> http://10.0.0.9:9002\n", out)

    def test_the_environment_wins_over_the_env_file(self):
        out = self._run("NETRADIO_ALIGN_HOST=10.0.0.9\n", NETRADIO_ALIGN_HOST="10.0.0.7")
        self.assertIn("review app -> http://10.0.0.7:8766\n", out)

    def test_the_env_file_is_not_executed(self):
        out = self._run("NETRADIO_ALIGN_URL=$(echo http://ran:1)\n")
        self.assertIn("review app -> $(echo http://ran:1)\n", out)


if __name__ == "__main__":
    unittest.main()
