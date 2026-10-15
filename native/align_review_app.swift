// align_review_app.swift — DP-22 §6.9-b: the Mac-native whole-song L/R review POC.
//
// A THIN CLIENT of the align server's HTTP API (docs/PLAN_align_tool.md §6.9-b):
// listings (GET /api/align/sources), whole-file audio (GET /api/align/audio/*),
// waveform envelopes (POST /api/align/overview), and the review-note write
// (POST /api/align/review-note). NO analysis logic is reimplemented here — the
// §6.2/§6.3 math below MIRRORS review_notes.py exactly (the server recomputes the
// mapping and rejects an irreproducible note), pinned two ways by
// tests/test_review_mac.py: the shared fixture native/review_math_fixture.json is
// asserted against review_notes' own recomputation in Python, and
// `align_review_app --selftest <fixture>` (run by `make review-app` after every
// build) asserts this file's Swift port reproduces the same numbers.
//
// Why native (§6.9-b): AVAudioEngine renders both branches off ONE render clock,
// so simultaneous stream-left / original-right playback is sample-locked BY
// CONSTRUCTION — no drift corrector exists in this file; the drift readout is
// telemetry, not control. Both branches are scheduled on the host-clock leg of one
// render instant. The original plays at the §6.2 fitted rate by sample-rate
// declaration (its PCM scheduled as fsO·r), NOT through AVAudioUnitVarispeed (see
// ensureOrigRate). Pitch shifts with the rate on purpose: the DJ's pitch fader
// shifted pitch too, so the pitch-true correction is what makes the two ears comparable.
//
// Build: `make review-app` — swiftc + Command Line Tools ONLY (the DP-21 build
// pattern; no Xcode), never in CI: the compile and the fixture selftest run locally.
// Run: `make review-app-run` (base URL from NETRADIO_ALIGN_URL; when that is unset,
// `make` derives it from NETRADIO_ALIGN_HOST:NETRADIO_ALIGN_PORT, default
// http://127.0.0.1:8766, because a server bound to a LAN address is not on loopback;
// Basic Auth from NETRADIO_AUTH_USER/NETRADIO_AUTH_PASS when set).
//
// The iOS path (§6.9-b/§6.11): this file's audio graph + API client are the port —
// an iOS build needs full Xcode + signing (free-Apple-ID provisioning expires
// every 7 days vs. the paid program's year), a later, separate decision.

import SwiftUI
import AVFoundation

// MARK: - constants (mirrored by tests/test_review_mac.py)

enum K {
    static let defaultBaseURL = "http://127.0.0.1:8766"
    static let marginS = 20.0            // §6.4: stream-solo pre/post-roll past the original's span
    static let viewS = 30.0              // scrolling viewport width at ×1 (seconds of stream time)
    // DP-37: doubled lanes + ×1/×2/max time-axis zoom (mirrors the web page)
    static let waveH: CGFloat = 380      // the doubled lane block (2 × the POC's 190)
    static let maxPxPerCol = 2.5         // MAX honesty: device px one envelope column may span
    static let fineCtxS = 60.0           // fine strips: the context op's ± half-width (its cap)
    static let maxFineTiles = 10         // hard cap on fine strips fetched per pair
    static let zoomLevels = ["1", "2", "max"]
    static let maxTS = 24.0 * 3600.0     // align_inspect.MAX_T_S (review_notes mirrors it)
    static let maxTextLen = 500          // review_notes.MAX_TEXT_LEN
    static let scheduleLeadS = 0.25      // shared sample-locked start lead on the render clock
    // DP-36 per-source volume sliders: 0–200 %, 100 % = unity (unmodified)
    static let volMin = 0.0
    static let volMax = 2.0
    // DP-36 overload fix: AVAudioMixerNode's stereo→mono down-mix is (L+R)/√2
    // (equal-power — MEASURED, offline render: full-scale in-phase stereo
    // peaked 1.414 = +3.01 dBFS, i.e. clipping). This compensation on each
    // STEREO branch makes the ear signal exactly 0.5·(L+R) at unity volume —
    // the standard mono down-mix, whose peak can never exceed the source's own.
    // Mono branches need no compensation (pan at ±1 measured unity).
    static let stereoDownmixComp = 0.5 * 2.0.squareRoot()   // = 1/√2
}

// review_notes._ANCHOR_K_RE
let anchorKRegex = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")

func anchorKeyOK(_ k: String) -> Bool {
    let r = NSRange(k.startIndex..., in: k)
    return anchorKRegex.firstMatch(in: k, range: r) != nil
}

// MARK: - §6.2/§6.3 math (MIRRORS review_notes.py — pinned by the shared fixture)

struct FitAnchor: Equatable, Hashable {
    let id: String       // "source:k:i" — the selection identity
    let k: String
    let streamS: Double
    let origS: Double
}

struct Fit {
    let r: Double                 // the effective rate, canonical 6 dp (§6.2)
    let a: Double                 // LSQ intercept given slope r
    let anchors: [FitAnchor]      // the selected pairs, stream-ordered
    let assumed: Bool             // §6.2 single-anchor assumed-rate mode
    let devs: [String: Double]    // id → deviation from the fitted line, ms
}

// the server's canonical 6 dp representation (review_notes: float("%.6f" % slope))
func round6(_ x: Double) -> Double { (x * 1e6).rounded() / 1e6 }

// MIRRORS review_notes._lsq_slope (the server recomputes this and rejects a
// fitted-mode rate that is not the 6 dp least-squares slope)
func lsqSlope(_ pts: [FitAnchor]) -> Double? {
    let n = Double(pts.count)
    let sm = pts.reduce(0.0) { $0 + $1.streamS } / n
    let om = pts.reduce(0.0) { $0 + $1.origS } / n
    var den = 0.0, num = 0.0
    for p in pts {
        den += (p.streamS - sm) * (p.streamS - sm)
        num += (p.streamS - sm) * (p.origS - om)
    }
    if den <= 1e-9 { return nil }
    return num / den
}

// §6.2: 1 pair = the flagged assumed-rate mode (Pass-1 rate when known, else
// 1.000); 2+ pairs = the least-squares fit; nil = a degenerate span (equal-time
// anchors — refuse, never invent)
func computeFit(selected: [FitAnchor], pairRate: Double?) -> Fit? {
    let pts = selected.sorted { $0.streamS < $1.streamS }
    guard !pts.isEmpty else { return nil }
    var assumed = false
    var r: Double
    if pts.count == 1 {
        assumed = true
        r = pairRate != nil ? round6(pairRate!) : 1.0
    } else {
        guard let slope = lsqSlope(pts) else { return nil }
        r = round6(slope)
    }
    // LSQ intercept given slope r — review_notes.expected_orig_t's constant-rate line
    let a = pts.reduce(0.0) { $0 + ($1.origS - r * $1.streamS) } / Double(pts.count)
    var devs: [String: Double] = [:]
    if !assumed {
        for p in pts { devs[p.id] = (p.origS - (a + r * p.streamS)) * 1000.0 }
    }
    return Fit(r: r, a: a, anchors: pts, assumed: assumed, devs: devs)
}

// MIRRORS review_notes.expected_orig_t (unclamped): constant-rate = the fitted
// line; re-anchor/assumed = governed by the last selected anchor at or before t
func mapOrig(_ t: Double, fit: Fit, effMode: String) -> Double {
    if effMode == "constant-rate" { return fit.a + fit.r * t }
    var gov = fit.anchors[0]
    for p in fit.anchors where p.streamS <= t { gov = p }
    return gov.origS + fit.r * (t - gov.streamS)
}

// what the note POST carries — the server clamps expected_orig_t to [0, MAX_T_S]
func mapOrigClamped(_ t: Double, fit: Fit, effMode: String) -> Double {
    return min(max(mapOrig(t, fit: fit, effMode: effMode), 0.0), K.maxTS)
}

// how many selected anchors playback has passed — the §6.3 crossing detector
// (counts PASSED anchors, not the governing index: the first anchor stays
// UNcrossed until playback reaches it, exactly as the web page counts)
func crossCount(_ t: Double, fit: Fit) -> Int {
    return fit.anchors.filter { $0.streamS <= t }.count
}

func fmtT(_ t: Double) -> String {
    let neg = t < 0
    let a = abs(t)
    let m = Int(a / 60)
    let s = a - Double(m) * 60
    return String(format: "%@%d:%04.1f", neg ? "−" : "", m, s)
}

// one straight §6.3 line per segment; re-anchor boundaries are the anchor
// crossings. A free function (not engine state) so --selftest can pin it
// against the shared fixture's segment_plan case — including anchors a few
// MILLISECONDS apart, because the whole lane is scheduled up front (below)
// and must stay correct for any §6.2-legal anchor spacing.
struct OrigSeg: Equatable {
    let startStreamT: Double
    let endStreamT: Double
    let startFileS: Double
    let endFileS: Double
}

func planOrigSegments(from t0: Double, fit: Fit, effMode: String,
                      winT1: Double, origLen: Double) -> [OrigSeg] {
    var bounds: [Double] = [t0]
    if effMode == "re-anchor" {
        for p in fit.anchors where p.streamS > t0 && p.streamS < winT1 {
            bounds.append(p.streamS)
        }
    }
    var segs: [OrigSeg] = []
    for (i, b) in bounds.enumerated() {
        let bEnd = i + 1 < bounds.count ? bounds[i + 1] : winT1
        if bEnd <= b { continue }
        // the §6.3 mapping over [b, bEnd) is one straight line at slope r
        let o0 = mapOrig(b, fit: fit, effMode: effMode)
        let o1 = o0 + fit.r * (bEnd - b)
        // §6.4 margins/overhang: clip to the original's own coverage [0, len] —
        // outside it the right ear is simply silent (stream-solo)
        if o1 <= 0 || o0 >= origLen { continue }
        var sT = b
        var sO = o0
        if o0 < 0 {
            sT = b + (0 - o0) / fit.r
            sO = 0
        }
        var eT = bEnd
        var eO = o1
        if o1 > origLen {
            eT = b + (origLen - o0) / fit.r
            eO = origLen
        }
        segs.append(OrigSeg(startStreamT: sT, endStreamT: eT,
                            startFileS: sO, endFileS: eO))
    }
    return segs
}

// MARK: - --selftest (the fixture parity gate `make review-app` runs)

func runSelftest(fixturePath: String) -> Int {
    func fail(_ msg: String) -> Int { FileHandle.standardError.write(Data(("selftest FAIL: " + msg + "\n").utf8)); return 1 }
    guard let data = FileManager.default.contents(atPath: fixturePath),
          let fx = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    else { return fail("cannot read fixture \(fixturePath)") }
    func dbl(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }
    guard let rawPairs = fx["pairs"] as? [[String: Any]] else { return fail("no pairs") }
    let pairs = rawPairs.enumerated().compactMap { (i, p) -> FitAnchor? in
        guard let k = p["k"] as? String, let s = dbl(p["stream_s"]), let o = dbl(p["orig_s"])
        else { return nil }
        return FitAnchor(id: "fx:\(k):\(i)", k: k, streamS: s, origS: o)
    }
    guard pairs.count == rawPairs.count, pairs.count >= 2 else { return fail("bad pairs") }
    guard let fit = computeFit(selected: pairs, pairRate: nil) else { return fail("no fit") }
    guard let wantR = dbl(fx["rate_6dp"]), fit.r == wantR else {
        return fail("rate \(fit.r) != fixture \(dbl(fx["rate_6dp"]) ?? .nan)")
    }
    guard let wantA = dbl(fx["intercept"]), abs(fit.a - wantA) < 1e-9 else {
        return fail("intercept \(fit.a)")
    }
    if let wantDevs = fx["devs_ms"] as? [Any] {
        for (p, wd) in zip(fit.anchors, wantDevs) {
            guard let w = dbl(wd), abs((fit.devs[p.id] ?? .nan) - w) < 1e-6 else {
                return fail("dev for \(p.k)")
            }
        }
    }
    for (key, mode) in [("constant_rate", "constant-rate"), ("re_anchor", "re-anchor")] {
        guard let rows = fx[key] as? [[String: Any]] else { return fail("no \(key)") }
        for row in rows {
            guard let t = dbl(row["t"]), let want = dbl(row["orig_t"]) else { return fail("bad \(key) row") }
            let got = mapOrigClamped(t, fit: fit, effMode: mode)
            guard abs(got - want) < 1e-9 else {
                return fail("\(mode) t=\(t): \(got) != \(want)")
            }
        }
    }
    if let ar = fx["assumed_rate"] as? [String: Any],
       let rp = ar["pair"] as? [String: Any], let rate = dbl(ar["rate"]),
       let k = rp["k"] as? String, let s = dbl(rp["stream_s"]), let o = dbl(rp["orig_s"]),
       let rows = ar["points"] as? [[String: Any]] {
        let one = [FitAnchor(id: "fx:\(k):0", k: k, streamS: s, origS: o)]
        guard let afit = computeFit(selected: one, pairRate: rate), afit.assumed,
              afit.r == round6(rate) else { return fail("assumed-rate fit") }
        for row in rows {
            guard let t = dbl(row["t"]), let want = dbl(row["orig_t"]) else { return fail("bad assumed row") }
            let got = mapOrigClamped(t, fit: afit, effMode: "assumed-rate")
            guard abs(got - want) < 1e-9 else { return fail("assumed t=\(t): \(got) != \(want)") }
        }
    }
    guard dbl(fx["max_t_s"]) == K.maxTS, dbl(fx["margin_s"]) == K.marginS else {
        return fail("constant mismatch (MAX_T_S / MARGIN_S)")
    }
    // the up-front lane planner, incl. the closely-spaced-anchor case (anchors
    // 50 ms / 10 ms apart) and the §6.4 pre-roll/tail clips
    if let sp = fx["segment_plan"] as? [String: Any],
       let spPairs = sp["pairs"] as? [[String: Any]],
       let spSegs = sp["segments"] as? [[String: Any]],
       let mode = sp["mode"] as? String,
       let t0 = dbl(sp["t0"]), let winT1 = dbl(sp["win_t1"]),
       let oLen = dbl(sp["orig_len"]) {
        let anchors = spPairs.enumerated().compactMap { (i, p) -> FitAnchor? in
            guard let k = p["k"] as? String, let s = dbl(p["stream_s"]),
                  let o = dbl(p["orig_s"]) else { return nil }
            return FitAnchor(id: "sp:\(k):\(i)", k: k, streamS: s, origS: o)
        }
        guard anchors.count == spPairs.count,
              let spFit = computeFit(selected: anchors, pairRate: nil),
              spFit.r == dbl(sp["rate_6dp"]) else { return fail("segment_plan fit") }
        let got = planOrigSegments(from: t0, fit: spFit, effMode: mode,
                                   winT1: winT1, origLen: oLen)
        guard got.count == spSegs.count else {
            return fail("segment_plan count \(got.count) != \(spSegs.count)")
        }
        for (g, w) in zip(got, spSegs) {
            for (gv, key) in [(g.startStreamT, "start_stream_t"),
                              (g.endStreamT, "end_stream_t"),
                              (g.startFileS, "start_file_s"),
                              (g.endFileS, "end_file_s")] {
                guard let wv = dbl(w[key]), abs(gv - wv) < 1e-9 else {
                    return fail("segment_plan \(key): \(gv)")
                }
            }
        }
    } else {
        return fail("no segment_plan case in the fixture")
    }
    print("selftest PASS: §6.2 LSQ fit + §6.3 mappings + lane plan match review_math_fixture.json")
    return 0
}

// MARK: - sources model (the §6.5 merged pair picker, mirroring the web page)

struct AnchorPoint: Identifiable {
    let id: String          // pointId(source, k, i) — "source:k:i", the web page's identity
    let source: String      // "hints" | "placed"
    let k: String
    let streamS: Double?
    let origS: Double?
    let confidence: Double?
    let grade: String?
    let why: String?
    var usable: Bool { streamS != nil && origS != nil && anchorKeyOK(k) }
    var isNotFound: Bool { grade?.range(of: "not.?found", options: [.regularExpression, .caseInsensitive]) != nil }
}

struct PairEntry: Identifiable {
    let stem: String
    let orig: Int
    var origName: String
    var rate: Double?          // Pass-1 summary rate (assumed-rate fallback)
    var rateNote: String?      // the placed op's owner rate rules (AB-first, dup letters)
    var inverted: Bool
    var sources: Set<String>
    var pointsAll: [AnchorPoint]
    var usable: [AnchorPoint] { pointsAll.filter { $0.usable } }
    var nf: Int { pointsAll.filter { $0.isNotFound }.count }
    var id: String { stem + "|" + String(orig) }
    var origLabel: String { String(format: "orig%03d", orig) }
}

func dblOf(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }

// one picker entry per (stem, orig); §6.2's ONE MIXED LIST interleaves, in stream
// order, every candidate point from both §6.5 sources (hints + placed)
func buildEntries(sources: [String: Any]) -> [PairEntry] {
    var map: [String: PairEntry] = [:]
    var order: [String] = []
    var origNames: [Int: String] = [:]
    for o in sources["originals"] as? [[String: Any]] ?? [] {
        if let n = (o["num"] as? NSNumber)?.intValue { origNames[n] = o["name"] as? String ?? "" }
    }
    func ingest(_ set: [String: Any], _ source: String) {
        guard let stem = set["stem"] as? String,
              let orig = (set["orig"] as? NSNumber)?.intValue else { return }
        let key = stem + "|" + String(orig)
        if map[key] == nil {
            map[key] = PairEntry(stem: stem, orig: orig, origName: origNames[orig] ?? "",
                                 rate: nil, rateNote: nil, inverted: false,
                                 sources: [], pointsAll: [])
            order.append(key)
        }
        map[key]!.sources.insert(source)
        if map[key]!.rate == nil, let r = dblOf(set["rate"]) { map[key]!.rate = r }
        if map[key]!.rateNote == nil, let n = set["rate_note"] as? String, !n.isEmpty {
            map[key]!.rateNote = n
        }
        if source == "hints", (set["inverted"] as? NSNumber)?.boolValue == true {
            map[key]!.inverted = true
        }
        for (i, p) in (set["points"] as? [[String: Any]] ?? []).enumerated() {
            let k = (p["k"] as? String) ?? String(describing: p["k"] ?? "")
            map[key]!.pointsAll.append(AnchorPoint(
                id: source + ":" + k + ":" + String(i), source: source, k: k,
                streamS: dblOf(p["stream_s"]), origS: dblOf(p["orig_s"]),
                confidence: dblOf(p["confidence"]) ?? dblOf(p["seat_conf"]),
                grade: p["grade"] as? String, why: p["why"] as? String))
        }
    }
    for h in sources["hints"] as? [[String: Any]] ?? [] { ingest(h, "hints") }
    for pl in sources["placed"] as? [[String: Any]] ?? [] { ingest(pl, "placed") }
    var entries = order.compactMap { map[$0] }
    for i in entries.indices {
        // tie-break equal instants by source (hints first) — Swift's sort is
        // not stable, and the dedup winner must match the web page's
        entries[i].pointsAll.sort {
            let (a, b) = ($0.streamS ?? 0, $1.streamS ?? 0)
            return a != b ? a < b : $0.source < $1.source
        }
    }
    func firstS(_ e: PairEntry) -> Double { e.usable.first?.streamS ?? 1e12 }
    entries.sort { $0.stem == $1.stem ? firstS($0) < firstS($1) : $0.stem < $1.stem }
    return entries
}

// MARK: - API client (thin: four routes, nothing else)

struct Api {
    let base: URL
    let auth: String?

    init() {
        let env = ProcessInfo.processInfo.environment
        var raw = env["NETRADIO_ALIGN_URL"] ?? K.defaultBaseURL
        while raw.hasSuffix("/") { raw.removeLast() }
        base = URL(string: raw) ?? URL(string: K.defaultBaseURL)!
        if let u = env["NETRADIO_AUTH_USER"], let p = env["NETRADIO_AUTH_PASS"],
           !u.isEmpty, !p.isEmpty {
            auth = "Basic " + Data((u + ":" + p).utf8).base64EncodedString()
        } else {
            auth = nil
        }
    }

    func request(_ path: String) -> URLRequest {
        var req = URLRequest(url: base.appendingPathComponent(path))
        if let auth { req.setValue(auth, forHTTPHeaderField: "Authorization") }
        return req
    }

    func getJSON(_ path: String) async throws -> [String: Any] {
        let (data, _) = try await URLSession.shared.data(for: request(path))
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "align", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "non-object JSON from \(path)"])
        }
        return obj
    }

    func postJSON(_ path: String, body: [String: Any]) async throws -> (Int, [String: Any]) {
        var req = request(path)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return (status, obj)
    }
}

// one instance per fetch: a download with honest progress to the temp cache dir
final class Downloader: NSObject, URLSessionDownloadDelegate {
    private var progressCB: ((Double) -> Void)?
    private var doneCB: ((Result<(URL, HTTPURLResponse), Error>) -> Void)?
    private var session: URLSession?

    func fetch(_ req: URLRequest, progress: @escaping (Double) -> Void,
               done: @escaping (Result<(URL, HTTPURLResponse), Error>) -> Void) {
        progressCB = progress
        doneCB = done
        let s = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        session = s
        s.downloadTask(with: req).resume()
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        let frac = totalBytesExpectedToWrite > 0
            ? Double(totalBytesWritten) / Double(totalBytesExpectedToWrite) : 0
        DispatchQueue.main.async { self.progressCB?(frac) }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {
        // move the download out of URLSession's temp before this callback returns
        let hold = FileManager.default.temporaryDirectory
            .appendingPathComponent("align-review-" + UUID().uuidString)
        do {
            try FileManager.default.moveItem(at: location, to: hold)
        } catch {
            DispatchQueue.main.async { self.doneCB?(.failure(error)); self.doneCB = nil }
            return
        }
        let resp = downloadTask.response as? HTTPURLResponse ??
            HTTPURLResponse(url: downloadTask.originalRequest!.url!, statusCode: 0,
                            httpVersion: nil, headerFields: nil)!
        DispatchQueue.main.async { self.doneCB?(.success((hold, resp))); self.doneCB = nil }
        session.finishTasksAndInvalidate()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        if let error {
            DispatchQueue.main.async { self.doneCB?(.failure(error)); self.doneCB = nil }
        }
        session.finishTasksAndInvalidate()
    }
}

// MARK: - overview payload (DP-10 envelopes, fetched once per pair like the web)

struct Envelope {
    let mins: [Double]
    let maxs: [Double]
}

struct OvSegment {
    let a0: Double
    let a1: Double
    let env: Envelope
}

struct Overview {
    let stream: Envelope
    let segments: [OvSegment]
    let nCols: Double
    let streamLenS: Double
    let origLenS: Double?
    let rmsGain: Double          // whole-file RMS match — the shared amplitude scale (DP-37)
}

// DP-37 fine strips: one §5 context-op tile (±fineCtxS at 50 cols/s, both lanes
// on the SAME stream-time axis because the tile is centred ON the mapping)
struct FineTile {
    let tc: Double               // requested centre (stream s)
    let t0: Double
    let t1: Double
    let stream: Envelope
    let orig: Envelope
    let rmsGain: Double          // the tile's own ±60 s RMS match
    let origOk: Bool             // centre orig_t == the mapping's own (not clamped)
}

struct FineStrips {
    var cps: Double?             // MEASURED from the payload — never assumed
    var tiles: [FineTile] = []
    var failed = false           // parks auto-retry until zoom is re-engaged
}

// DP-37: one drawable lane source — fine tiles first, the overview beneath
struct LaneSrc {
    let env: Envelope
    let t0: Double
    let t1: Double
    let clip0: Double
    let clip1: Double
    let scale: Double
}

func parseEnvelope(_ obj: [String: Any]?) -> Envelope? {
    guard let obj,
          let mins = (obj["min"] as? [Any])?.compactMap(dblOf),
          let maxs = (obj["max"] as? [Any])?.compactMap(dblOf) else { return nil }
    return Envelope(mins: mins, maxs: maxs)
}

func parseOverview(_ obj: [String: Any]) -> Overview? {
    guard let stream = parseEnvelope(obj["stream"] as? [String: Any]),
          let nCols = dblOf(obj["n_cols"]), let sLen = dblOf(obj["stream_len_s"]),
          nCols > 0, sLen > 0 else { return nil }
    let segs = (obj["segments"] as? [[String: Any]] ?? []).compactMap { s -> OvSegment? in
        guard let a0 = dblOf(s["a0"]), let a1 = dblOf(s["a1"]),
              let env = parseEnvelope(s) else { return nil }
        return OvSegment(a0: a0, a1: a1, env: env)
    }
    return Overview(stream: stream, segments: segs, nCols: nCols,
                    streamLenS: sLen, origLenS: dblOf(obj["orig_len_s"]),
                    rmsGain: dblOf(obj["rms_gain"]) ?? 1.0)
}

// MARK: - the sample-locked engine (§6.9-b: the reason this app exists)

final class LockedEngine {
    let engine = AVAudioEngine()
    private let streamPlayer = AVAudioPlayerNode()
    private let streamMix = AVAudioMixerNode()
    private let origPlayer = AVAudioPlayerNode()
    private let origMix = AVAudioMixerNode()
    private var attached = false

    private(set) var streamFile: AVAudioFile?
    private(set) var origFile: AVAudioFile?
    // the whole original decoded once at its NATIVE rate; §6.9-b varispeed is
    // applied by DECLARING slices of this data at fsO·r (see ensureOrigRate) —
    // resident for the pair's lifetime (~130 MB for a 6-min stereo original, a
    // POC tradeoff; an iOS port would stream slices instead)
    private var rawOrig: AVAudioPCMBuffer?
    private var declaredFmt: AVAudioFormat?
    private var declaredRate = 0.0
    var streamLen: Double? { streamFile.map { Double($0.length) / $0.processingFormat.sampleRate } }
    var origLen: Double? { origFile.map { Double($0.length) / $0.processingFormat.sampleRate } }

    // the original lane's schedule, in the orig node's own sample positions
    // (declared-rate samples == original file frames; 1:1 with wall time):
    // audio pieces name their file start, silence pieces (margins / clipped
    // spans) carry nil — origPos() walks this table off playerTime
    private struct LanePiece {
        let startPT: AVAudioFramePosition
        let frames: AVAudioFramePosition
        let fileStartS: Double?
    }
    private var lane: [LanePiece] = []
    private(set) var segments: [OrigSeg] = []
    private var runT0 = 0.0
    private var streamBaseT = 0.0
    private var anchorHost: UInt64 = 0
    private(set) var running = false
    private var runGen = 0        // stale completion handlers must not kill a NEW run
    var onStreamEnd: (() -> Void)?

    func load(streamURL: URL, origURL: URL) throws {
        stopRun()
        streamFile = try AVAudioFile(forReading: streamURL)
        let oFile = try AVAudioFile(forReading: origURL)
        origFile = oFile
        guard let buf = AVAudioPCMBuffer(pcmFormat: oFile.processingFormat,
                                         frameCapacity: AVAudioFrameCount(oFile.length)) else {
            throw NSError(domain: "align", code: 6,
                          userInfo: [NSLocalizedDescriptionKey: "original too large to buffer"])
        }
        try oFile.read(into: buf)
        rawOrig = buf
        declaredRate = 0   // force ensureOrigRate to rewire for the next run
        declaredFmt = nil
        wire()
    }

    private func wire() {
        guard let sFile = streamFile, let oFile = origFile else { return }
        if !attached {
            engine.attach(streamPlayer)
            engine.attach(streamMix)
            engine.attach(origMix)
            engine.attach(origPlayer)
            attached = true
        }
        engine.stop()
        let sFmt = sFile.processingFormat
        let oFmt = oFile.processingFormat
        // §6.1 hard pans want the WHOLE signal in one ear: each branch is downmixed
        // to MONO before its ±1 pan (a stereo pan is only a balance control).
        // The mixer's own down-mix is (L+R)/√2, +3 dB over the standard
        // 0.5·(L+R) — applyLevels' K.stereoDownmixComp cancels that (DP-36)
        let monoS = AVAudioFormat(standardFormatWithSampleRate: sFmt.sampleRate, channels: 1)!
        let monoO = AVAudioFormat(standardFormatWithSampleRate: oFmt.sampleRate, channels: 1)!
        engine.connect(streamPlayer, to: streamMix, format: sFmt)
        engine.connect(streamMix, to: engine.mainMixerNode, format: monoS)
        engine.connect(origMix, to: engine.mainMixerNode, format: monoO)
    }

    // §6.9-b varispeed, applied by SAMPLE-RATE DECLARATION: the original's PCM is
    // scheduled in a format declaring sampleRate = fsO * fit.r, so the mixer's
    // SRC consumes r original-seconds per wall second — the same audible result
    // as AVAudioUnitVarispeed (pitch shifts too, on purpose: the DJ's pitch
    // fader did), at exact double precision. AVAudioUnitVarispeed itself is
    // deliberately NOT in the graph: it makes the upstream `AVAudioPlayerNode`'s
    // timeline race at r× wall speed, which skews play(at:) host→sample
    // conversion by horizon × (r − 1) — the live self-test measured +166 ms on
    // an 8 s horizon at r = 1.0204. Don't reintroduce the unit.
    private func ensureOrigRate(_ r: Double) {
        guard let oFmt = origFile?.processingFormat else { return }
        let fsO = oFmt.sampleRate
        let want = fsO * r
        if declaredFmt != nil, abs(want - declaredRate) < 1e-9 { return }
        engine.stop()
        declaredRate = want
        declaredFmt = AVAudioFormat(standardFormatWithSampleRate: want,
                                    channels: oFmt.channelCount)!
        engine.connect(origPlayer, to: origMix, format: declaredFmt!)
    }

    // raw original frames [f0, f1) copied into a buffer carrying the DECLARED
    // format (a plain copy on purpose: a zero-copy AudioBufferList view was
    // tried and aborted deep in CoreAudio during the branch handoff)
    private func slice(f0: AVAudioFramePosition, f1: AVAudioFramePosition) -> AVAudioPCMBuffer? {
        guard let raw = rawOrig, let fmt = declaredFmt,
              let src = raw.floatChannelData, f1 > f0 else { return nil }
        let frames = AVAudioFrameCount(f1 - f0)
        guard let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames),
              let dst = buf.floatChannelData else { return nil }
        for c in 0..<Int(fmt.channelCount) {
            dst[c].update(from: src[c] + Int(f0), count: Int(frames))
        }
        buf.frameLength = frames
        return buf
    }

    // §6.1: hard pans at ±1; one-tap swap flips BOTH (a running-graph change)
    func setPans(swap: Bool) {
        streamMix.pan = swap ? 1 : -1
        origMix.pan = swap ? -1 : 1
    }

    // one stereo-branch down-mix compensation per file (DP-36): mono files pass
    // at unity, stereo files get ×(1/√2) so the ear hears 0.5·(L+R) at 100 %
    private func comp(_ file: AVAudioFile?) -> Double {
        ((file?.processingFormat.channelCount ?? 2) > 1) ? K.stereoDownmixComp : 1.0
    }

    // §6.1 solo (hold or latch) + the DP-36 per-source volumes, one place: gain
    // changes on the running graph — playback never stops, returning to stereo
    // is one action. outputVolume is measured UNclamped above 1.0, so 200 %
    // rides it directly; solo zeroes the original WITHOUT touching its volume.
    func applyLevels(volStream: Double, volOrig: Double,
                     holdSolo: Bool, latchSolo: Bool) {
        streamMix.outputVolume = Float(comp(streamFile) * volStream)
        origMix.outputVolume = (holdSolo || latchSolo)
            ? 0 : Float(comp(origFile) * volOrig)
    }

    // level telemetry for the autotest (the applied mixer gains, comp included)
    var streamLevel: Float { streamMix.outputVolume }
    var origLevel: Float { origMix.outputVolume }

    // both branches schedule against the SAME engine render instant: the shared
    // anchor (read off the output node's render clock) plus the stream-time
    // delta — sample-locked by construction. The instant is carried on the
    // HOST-CLOCK leg on purpose: an AVAudioTime's sampleTime legs are NODE-LOCAL
    // timelines (a 16 kHz stream node and a 44.1 kHz original node read the same
    // sampleTime as different instants — the live self-test measured that as a
    // flat +514 ms skew), while play(at:) converts a host-time instant to each
    // node's own sample position sample-accurately. Don't reintroduce the
    // sampleTime leg across nodes with different rates.
    private func atTime(forStreamT t: Double) -> AVAudioTime {
        let d = t - runT0
        return AVAudioTime(hostTime: anchorHost + AVAudioTime.hostTime(forSeconds: d))
    }

    func startRun(from t0: Double, fit: Fit, effMode: String, winT1: Double,
                  swap: Bool, volStream: Double, volOrig: Double,
                  holdSolo: Bool, latchSolo: Bool) throws {
        stopRun()
        guard let sFile = streamFile, origFile != nil else {
            throw NSError(domain: "align", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "audio not loaded"])
        }
        // §6.9-b varispeed: the original branch runs at the §6.2 fitted rate
        // (declared-rate SRC — see ensureOrigRate)
        ensureOrigRate(fit.r)
        if !engine.isRunning {
            engine.prepare()
            try engine.start()
        }
        // pans + levels AFTER the engine is running: AVAudioMixing values set on
        // a not-yet-started engine can be dropped when the underlying units are
        // (re)instantiated — measured in the DP-36 offline harness
        setPans(swap: swap)
        applyLevels(volStream: volStream, volOrig: volOrig,
                    holdSolo: holdSolo, latchSolo: latchSolo)
        runT0 = t0
        runGen += 1

        // stream branch: ONE segment for the whole run window, QUEUED (not yet
        // playing — the shared start instant is chosen only after ALL
        // scheduling work below is done)
        let fsS = sFile.processingFormat.sampleRate
        let f0 = min(AVAudioFramePosition((max(0, t0) * fsS).rounded()), sFile.length)
        let fN = min(AVAudioFramePosition((winT1 * fsS).rounded()), sFile.length)
        guard fN > f0 else {
            throw NSError(domain: "align", code: 4,
                          userInfo: [NSLocalizedDescriptionKey: "empty stream window"])
        }
        streamBaseT = Double(f0) / fsS
        let gen = runGen
        streamPlayer.scheduleSegment(sFile, startingFrame: f0, frameCount: AVAudioFrameCount(fN - f0),
                                     at: nil, completionCallbackType: .dataPlayedBack) { _ in
            DispatchQueue.main.async {
                // node.stop() also fires this callback — a STALE run's completion
                // must never end the run that replaced it (live self-test find)
                if gen == self.runGen, self.running { self.onStreamEnd?() }
            }
        }

        // the ORIGINAL LANE, scheduled up front in its ENTIRETY before playback
        // begins: one buffer slice per §6.3 straight-line segment, back-to-back
        // (an anchor jump is just the next slice starting at a different file
        // frame — zero wall gap), with explicit silence buffers filling the §6.4
        // margins / clipped spans. Nothing is armed later, so no timer latency
        // can ever miss a boundary — correct for ANY anchor spacing (the
        // planner's closely-spaced-anchor case is pinned in the fixture).
        segments = planOrigSegments(from: t0, fit: fit, effMode: effMode,
                                    winT1: winT1, origLen: origLen ?? 0)
        scheduleLane(from: t0)

        // THE shared start instant on the engine render clock (§6.9-b), chosen
        // only AFTER every buffer of both lanes is queued — however long the
        // slicing/copying above took, the deadline cannot have expired, because
        // the only work left before it is the two play(at:) calls themselves
        let out = engine.outputNode
        guard let rt = out.lastRenderTime, rt.isSampleTimeValid, rt.isHostTimeValid else {
            throw NSError(domain: "align", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "no render clock"])
        }
        anchorHost = rt.hostTime + AVAudioTime.hostTime(forSeconds: K.scheduleLeadS)
        running = true
        streamPlayer.play(at: atTime(forStreamT: t0))
        if !lane.isEmpty {
            origPlayer.play(at: atTime(forStreamT: t0))
        }
    }

    private func scheduleLane(from t0: Double) {
        lane = []
        origPlayer.stop()
        guard !segments.isEmpty, let fsO = origFile?.processingFormat.sampleRate,
              let raw = rawOrig else { return }
        // cumulative node-sample positions come from the WALL offsets directly
        // (declared samples are 1:1 with wall time), so per-piece rounding
        // never accumulates across a run
        func pt(_ streamT: Double) -> AVAudioFramePosition {
            AVAudioFramePosition(((streamT - t0) * declaredRate).rounded())
        }
        var cursorPT: AVAudioFramePosition = 0
        for seg in segments {
            let segStart = pt(seg.startStreamT)
            if segStart > cursorPT {
                // §6.4: the right ear is SILENT outside the original's coverage
                if let gap = silence(frames: segStart - cursorPT) {
                    origPlayer.scheduleBuffer(gap)
                    lane.append(LanePiece(startPT: cursorPT, frames: segStart - cursorPT,
                                          fileStartS: nil))
                    cursorPT = segStart
                }
            }
            let f0 = min(AVAudioFramePosition((seg.startFileS * fsO).rounded()),
                         AVAudioFramePosition(raw.frameLength))
            let frames = min(pt(seg.endStreamT) - cursorPT,
                             AVAudioFramePosition(raw.frameLength) - f0)
            guard frames > 0, let buf = slice(f0: f0, f1: f0 + frames) else { continue }
            origPlayer.scheduleBuffer(buf)
            lane.append(LanePiece(startPT: cursorPT, frames: frames,
                                  fileStartS: Double(f0) / fsO))
            cursorPT += frames
        }
        // the play(at:) call happens in startRun, AFTER the shared start
        // instant is chosen — queueing here must never start the node
    }

    private func silence(frames: AVAudioFramePosition) -> AVAudioPCMBuffer? {
        guard frames > 0, let fmt = declaredFmt,
              let buf = AVAudioPCMBuffer(pcmFormat: fmt,
                                         frameCapacity: AVAudioFrameCount(frames)),
              let dst = buf.floatChannelData else { return nil }
        for c in 0..<Int(fmt.channelCount) {
            dst[c].update(repeating: 0, count: Int(frames))
        }
        buf.frameLength = AVAudioFrameCount(frames)
        return buf
    }

    func streamPos(fallback: Double) -> Double {
        guard running, let nt = streamPlayer.lastRenderTime, nt.isSampleTimeValid,
              let pt = streamPlayer.playerTime(forNodeTime: nt) else { return fallback }
        return streamBaseT + max(0, Double(pt.sampleTime) / pt.sampleRate)
    }

    // the measured original position off the same render clock — telemetry for
    // the drift readout (there is NO corrector consuming it). Each declared-rate
    // sample rendered is exactly one ORIGINAL frame, so the lane table maps
    // playerTime to a file position (silence pieces and the run's end map to
    // nil: §6.4 margin, stream solo).
    func origPos() -> Double? {
        guard running, !lane.isEmpty,
              let fsO = origFile?.processingFormat.sampleRate else { return nil }
        let node = origPlayer
        guard node.isPlaying, let nt = node.lastRenderTime, nt.isSampleTimeValid,
              let pt = node.playerTime(forNodeTime: nt), pt.sampleTime >= 0 else { return nil }
        let pos = pt.sampleTime
        for piece in lane where pos >= piece.startPT && pos < piece.startPT + piece.frames {
            guard let f = piece.fileStartS else { return nil }
            return f + Double(pos - piece.startPT) / fsO
        }
        return nil
    }

    func stopRun() {
        running = false
        streamPlayer.stop()
        origPlayer.stop()
        segments = []
        lane = []
    }
}

// MARK: - the model

struct SavedNote: Identifiable {
    let id = UUID()
    let streamT: Double
    let origT: Double
    let text: String
}

@MainActor
final class ReviewModel: ObservableObject {
    let api = Api()
    let engine = LockedEngine()

    // picker
    @Published var entries: [PairEntry] = []
    @Published var sourcesEnabled = false
    @Published var pickerStatus = "loading sources…"
    @Published var cur: PairEntry?
    @Published var pickerOpen = true

    // §6.2 selection + fit
    @Published var sel: Set<String> = []
    @Published var mode = "re-anchor"
    @Published var fit: Fit?
    @Published var fitError: String?
    @Published var selInfo = ""

    // playback + window
    @Published var playing = false
    @Published var cursor = 0.0
    @Published var winT0 = 0.0
    @Published var winT1 = 0.0
    @Published var windowInfo = ""
    private var lastCross = 0

    // §6.1 toggles
    @Published var swap = false { didSet { engine.setPans(swap: swap) } }
    @Published var holdSolo = false { didSet { applyLevels() } }
    @Published var latchSolo = false { didSet { applyLevels() } }

    // DP-36 per-source volumes: 1.0 = unity (unmodified level), range 0–2;
    // live during playback, persisted in the app's prefs (UserDefaults)
    @Published var volStream = 1.0 { didSet { applyLevels(); saveVolPrefs() } }
    @Published var volOrig = 1.0 { didSet { applyLevels(); saveVolPrefs() } }

    // telemetry (readout only — no corrector exists to consume it)
    @Published var driftMs: Double?
    @Published var maxAbsDriftMs: Double?
    @Published var origPosNow: Double?
    @Published var targetNow: Double?

    // §6.6 notes
    @Published var notePos: Double?
    @Published var noteText = ""
    @Published var noteStatus = ""
    @Published var notes: [SavedNote] = []
    @Published var usedAnchors: Set<String> = []
    @Published var markerInfo = ""

    // audio + overview
    @Published var audioStatus = ""
    @Published var streamProgress = 0.0
    @Published var origProgress = 0.0
    @Published var audioReady = false
    @Published var ov: Overview?
    @Published var waveInfo = ""
    private var ovCache: [String: Overview] = [:]
    private var loadGeneration = 0

    // DP-37: time-axis zoom ("1" | "2" | "max") — persisted; fine strips ride it
    @Published var zoom = "1" {
        didSet {
            saveZoomPref()
            if zoom != "1" { Task { await self.ensureFineStrips() } }
        }
    }
    @Published var waveDevW = 1480.0     // measured device-pixel width of the waveform canvas
    @Published var fine: FineStrips?
    private var fineCache: [String: FineStrips] = [:]
    private var fineFetching = false

    private var timer: Timer?

    var effMode: String { (fit?.assumed ?? false) ? "assumed-rate" : mode }

    var cacheDir: URL {
        let d = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("netradio-review-app")
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    private func applyLevels() {
        engine.applyLevels(volStream: volStream, volOrig: volOrig,
                           holdSolo: holdSolo, latchSolo: latchSolo)
    }

    // ---------- DP-36 volume prefs (the web page's prefs idiom, UserDefaults) ----------

    static let volStreamKey = "netradio.review.volStream"
    static let volOrigKey = "netradio.review.volOrig"
    static let zoomKey = "netradio.review.zoom"      // DP-37: zoom rides the prefs
    private var restoringVols = false

    private func saveVolPrefs() {
        if restoringVols { return }
        let d = UserDefaults.standard
        d.set(volStream, forKey: Self.volStreamKey)
        d.set(volOrig, forKey: Self.volOrigKey)
    }

    private func saveZoomPref() {
        if restoringVols { return }
        UserDefaults.standard.set(zoom, forKey: Self.zoomKey)
    }

    func restoreVolPrefs() {
        let d = UserDefaults.standard
        restoringVols = true
        if d.object(forKey: Self.volStreamKey) != nil {
            volStream = min(max(d.double(forKey: Self.volStreamKey), K.volMin), K.volMax)
        }
        if d.object(forKey: Self.volOrigKey) != nil {
            volOrig = min(max(d.double(forKey: Self.volOrigKey), K.volMin), K.volMax)
        }
        // DP-37: restore zoom through the same whitelist the control offers
        if let z = d.string(forKey: Self.zoomKey), K.zoomLevels.contains(z) {
            zoom = z
        }
        restoringVols = false
    }

    // ---------- DP-37: time-axis zoom (×1 / ×2 / max) ----------

    // geometric coverage: does the tile union cover [a, b]? Tiles can arrive
    // in any order (viewport-priority fetching below), so merge-walk sorted.
    func fineCoveredRange(_ a: Double, _ b: Double) -> Bool {
        guard let fine, !fine.tiles.isEmpty else { return false }
        var at = a
        for tl in fine.tiles.sorted(by: { $0.t0 < $1.t0 }) {
            if tl.t0 > at + 1e-3 { break }
            if tl.t1 > at { at = tl.t1 }
            if at >= b - 1e-3 { return true }
        }
        return at >= b - 1e-3
    }

    // is the VISIBLE window portion drawn from fine strips end-to-end?
    // Recursion break (viewSpan → zoomFactor → maxZoom → here): judge coverage
    // against the span the CURRENT zoom would show IF the fine claim held —
    // the narrowest candidate. Covered → the real span IS that span, so the
    // claim matches the drawing exactly; uncovered → the wider overview-span
    // viewport (a superset around the same centre) is uncovered too, so the
    // fallback is consistent — and conservative: the claim never overstates.
    var viewportFine: Bool {
        guard let fine, let fcps = fine.cps, fit != nil else { return false }
        let z = zoom == "2" ? 2.0 : zoom == "max" ? Double(maxZoomFor(fcps)) : 1.0
        let span = K.viewS / z
        let a = max(cursor - span / 2, winT0)
        let b = min(cursor + span / 2, winT1)
        if b <= a { return false }
        return fineCoveredRange(a, b)
    }

    var viewportCps: Double? {
        // the column rate of what the CURRENT viewport actually draws,
        // MEASURED from the payloads — never assumed. The label and max may
        // claim fine resolution ONLY while fine tiles really cover the visible
        // span; outside coverage the draw falls back to the overview, so the
        // claim must too (review iteration 1 P1: a seek past the fetched
        // tiles must not keep the 50 cols/s max).
        if viewportFine, let c = fine?.cps { return c }
        if let ov { return ov.nCols / ov.streamLenS }
        return nil
    }

    var maxZoom: Int {
        // DP-37 MAX honesty (mirrors the web page): magnification can only
        // reveal detail the envelope data actually carries — nothing is ever
        // interpolated. With a column rate of `cps` (measured from the payload)
        // and a viewport of `devW` device pixels spanning viewS/z seconds, one
        // column occupies devW·z/(viewS·cps) device pixels. MAX is the zoom at
        // which a column reaches maxPxPerCol (~2–3) device pixels — past it
        // there is no more data, only wider blocks:
        //     zMax = round(maxPxPerCol · viewS · cps / devW), clamped to [2, 16]
        // The DP-10 overview's ~6 cols/s puts zMax below ×2 on any real screen
        // (the low clamp is the "data limit" case); the §5 context op's
        // 50 cols/s fine strips (ensureFineStrips) lift it to ×3–×4.
        maxZoomFor(viewportCps)
    }

    func maxZoomFor(_ cps: Double?) -> Int {
        // the pure formula (no viewport dependence — viewportFine's recursion
        // break relies on that)
        guard let cps else { return 4 }   // DSP dark: markers only
        let z = (K.maxPxPerCol * K.viewS * cps / max(waveDevW, 1)).rounded()
        return min(max(Int(z), 2), 16)
    }

    var zoomFactor: Double {
        if zoom == "2" { return 2 }
        if zoom == "max" { return Double(maxZoom) }
        return 1
    }

    var viewSpan: Double { K.viewS / zoomFactor }

    var zoomInfo: String {
        guard let cps = viewportCps else { return "" }
        let src = viewportFine ? "fine strips" : "overview"
        return String(format: cps < 10 ? "%.1f cols/s %@ · view %.0f s"
                                       : "%.0f cols/s %@ · view %.0f s",
                      cps, src, viewSpan)
    }

    // DP-37 fine strips: the DP-10 overview op is FIXED at ~6 cols/s, so zoomed
    // views fetch the inspector's §5 context strips instead — ±fineCtxS
    // decimated slices at 50 cols/s, tiled to cover the §6.4 window, through the
    // SAME DSP boundary (no new op). Tiles are fetched lazily when zoom leaves
    // ×1, sequentially (the DSP lock is one-at-a-time — 429 waits its turn),
    // cached per pair, and never refetched on selection changes (the mapping
    // moves by ms — invisible at 20 ms columns).
    private func fineCentres() -> [Double] {
        // the slot grid: 120 s slots anchored at winT0 — re-read from the
        // window on EVERY call, so a window that grows mid-fetch (the overview
        // arriving with the real orig_len) extends the very loop already
        // running. Viewport slots first: a seek or playback into an uncovered
        // region gets its fine detail before the far ends fill in.
        let step = 2 * K.fineCtxS
        var list: [Double] = []
        var tc = winT0 + K.fineCtxS
        while tc - K.fineCtxS < winT1 {
            list.append(tc)
            tc += step
        }
        let vc = cursor
        return list.sorted { abs($0 - vc) < abs($1 - vc) }
    }

    private func nextFineCentre(in f: FineStrips) -> Double? {
        // reads the RUN's working copy, not the (possibly re-paired) live state
        for tc in fineCentres()
        where !f.tiles.contains(where: { abs($0.tc - tc) < 0.5 }) {
            return tc
        }
        return nil
    }

    func ensureFineStrips() async {
        guard let e = cur, let fit, sourcesEnabled, !fineFetching else { return }
        let key = e.id
        // review iteration 2 P2: the request lock is GLOBAL but every piece of
        // state this run touches is scoped to its own pair — `f` is the run's
        // working copy, `publish()` stores it under the run's OWN key and
        // updates the live `fine` only while that pair is still open. Stale
        // and error paths can therefore never mark another pair failed or
        // store under the wrong cache key.
        var f = fineCache[key] ?? FineStrips()
        f.failed = false    // an explicit (re-)engage clears the auto-retry park
        func publish() {
            fineCache[key] = f
            if cur?.id == key { fine = f }
        }
        publish()
        fineFetching = true
        defer {
            fineFetching = false
            // and if the pair changed while this run owned the lock, the new
            // pair's own fetch was turned away at the guard — re-arm it now
            if cur?.id != key { rearmFineStrips() }
        }
        for _ in 0..<K.maxFineTiles {
            guard let tc = nextFineCentre(in: f) else { break }
            let want = mapOrig(tc, fit: fit, effMode: effMode)
            let reqOrig = round6(mapOrigClamped(tc, fit: fit, effMode: effMode))
            let body: [String: Any] = [
                "stem": e.stem, "orig": e.orig,
                "stream_t": round6(tc), "orig_t": reqOrig,
                "rate": fit.r, "invert": e.inverted,
                "context": K.fineCtxS,
            ]
            var j: [String: Any] = [:]
            var status = 0
            var transportError: String?
            for attempt in 0..<6 {
                do {
                    (status, j) = try await api.postJSON("/api/align/context", body: body)
                    transportError = nil
                } catch {
                    transportError = error.localizedDescription
                }
                if transportError == nil, status == 429, attempt < 5 {
                    // the one-at-a-time DSP lock
                    try? await Task.sleep(nanoseconds: 1_200_000_000)
                    continue
                }
                break
            }
            guard cur?.id == key else { return }   // pair changed mid-fetch (defer re-arms it)
            if let terr = transportError {
                waveInfo = "fine strips unavailable (\(terr)) — overview resolution"
                // park auto-retry (the tick re-arm would hammer the server
                // otherwise); re-engaging the zoom control tries again
                f.failed = true
                publish()
                return
            }
            if let err = j["error"] as? String {
                waveInfo = "fine strips unavailable: \(err) — overview resolution"
                f.failed = true
                publish()
                return
            }
            guard let nCols = dblOf(j["n_cols"]), let ctxS = dblOf(j["context_s"]),
                  let startS = dblOf(j["start_s"]), nCols > 0, ctxS > 0,
                  let sEnv = parseEnvelope(j["stream"] as? [String: Any]),
                  let oEnv = parseEnvelope(j["orig"] as? [String: Any]) else {
                waveInfo = "fine strips unavailable (bad payload) — overview resolution"
                f.failed = true
                publish()
                return
            }
            // the column rate is MEASURED from the payload — never assumed
            f.cps = nCols / (2 * ctxS)
            if f.tiles.count >= K.maxFineTiles {
                // the explicit cache policy: maxFineTiles is the KEPT-tiles
                // cap per pair; on a window longer than the cap covers, evict
                // the tile FARTHEST from the viewport so fine detail follows
                // the operator
                let vc = cursor
                if let far = f.tiles.indices.max(by: {
                    abs(f.tiles[$0].tc - vc) < abs(f.tiles[$1].tc - vc) }) {
                    f.tiles.remove(at: far)
                }
            }
            // a tile's orig lane is only mapping-true when the requested centre
            // orig_t IS the mapping's own (an out-of-coverage centre was
            // clamped): margin tiles keep their fine STREAM lane and the orig
            // lane falls back to the overview
            let oLen = engine.origLen ?? ov?.origLenS
            let origOk = want >= 0 && (oLen == nil || want <= oLen!) &&
                abs(reqOrig - want) < 1e-5
            f.tiles.append(FineTile(tc: tc, t0: startS, t1: startS + 2 * ctxS,
                                    stream: sEnv, orig: oEnv,
                                    rmsGain: dblOf(j["rms_gain"]) ?? 1.0,
                                    origOk: origOk))
            publish()
        }
    }

    // ---------- boot ----------

    func boot() async {
        restoreVolPrefs()
        engine.onStreamEnd = { [weak self] in
            guard let self, self.playing else { return }
            self.stopPlayback()
            self.cursor = self.winT1
        }
        do {
            let j = try await api.getJSON("/api/align/sources")
            sourcesEnabled = (j["enabled"] as? NSNumber)?.boolValue ?? false
            entries = buildEntries(sources: j)
            pickerStatus = entries.isEmpty
                ? "no pairs found — run streamalign match-hints, or check the align server"
                : "\(entries.count) pairs (hints + placed, grouped by capture)" +
                  (sourcesEnabled ? "" : " — DSP dark: no waveform envelopes")
        } catch {
            pickerStatus = "sources unavailable: \(error.localizedDescription) — is the align server up at \(api.base)?"
        }
    }

    // ---------- §6.2 selection ----------

    func selectedAnchors() -> [FitAnchor] {
        guard let cur else { return [] }
        return cur.usable.filter { sel.contains($0.id) }
            .compactMap { p in
                guard let s = p.streamS, let o = p.origS else { return nil }
                return FitAnchor(id: p.id, k: p.k, streamS: s, origS: o)
            }
            .sorted { $0.streamS < $1.streamS }
    }

    func recomputeFit() {
        fitError = nil
        let pts = selectedAnchors()
        fit = pts.isEmpty ? nil : computeFit(selected: pts, pairRate: cur?.rate)
        if fit == nil, !pts.isEmpty {
            fitError = "degenerate anchor span — deselect one of the equal-time anchors"
        }
        recomputeWindow()
        updateSelInfo()
    }

    func updateSelInfo() {
        guard let cur else { selInfo = ""; return }
        selInfo = "\(sel.count) of \(cur.usable.count) anchors selected — deviations are from the fitted single-rate line"
    }

    func toggleAnchor(_ p: AnchorPoint) {
        // §6.2 (A4): selection edits only while playback is STOPPED
        if playing { return }
        guard let s6 = p.streamS.map({ String(format: "%.6f", $0) }) else { return }
        if sel.contains(p.id) {
            // §6.2: the list refuses to deselect the LAST remaining pair
            if sel.count == 1 { return }
            sel.remove(p.id)
        } else {
            // mirror the server's duplicate-stream-instant refusal (equal-time
            // anchors would make the re-anchor order undefined after sort_tsv)
            if selectedAnchors().contains(where: { String(format: "%.6f", $0.streamS) == s6 }) {
                selInfo = "anchor \(p.k) shares its stream instant with a selected anchor — pick one of the two"
                return
            }
            sel.insert(p.id)
        }
        recomputeFit()
        resetPlayback()   // §6.2 (A4): any selection change RESETS playback
    }

    func setMode(_ m: String) {
        if playing { return }   // frozen during a playback run
        mode = m
        recomputeFit()
        resetPlayback()
    }

    var verdict: String {
        guard let fit, !fit.assumed else { return "" }
        let ds = fit.anchors.map { fit.devs[$0.id] ?? 0 }
        let maxAbs = ds.map { abs($0) }.max() ?? 0
        // curvature, not trend (LSQ residuals are trend-free by construction)
        let curved = ds.count >= 3 && ds[0] * ds[ds.count - 1] > 0 &&
            ds.dropFirst().dropLast().contains { $0 * ds[0] < 0 && abs($0) > 10 }
        return (curved && maxAbs > 25)
            ? "systematic drift \(Int(maxAbs.rounded())) ms across the span — multi-rate suspected"
            : String(format: "scattered ±%.1f ms — the single rate looks honest", maxAbs)
    }

    var assumedBanner: String? {
        guard let fit, fit.assumed else { return nil }
        let src = cur?.rate != nil ? "the pair's Pass-1 rate" : "1.000"
        return "assumed-rate mode: only one anchor is selected, so there is nothing to fit — " +
            "the rate readout is \(src), taken on faith. A second anchor seated in the " +
            "/align inspector buys a measured rate."
    }

    // ---------- §6.4 window ----------

    func recomputeWindow() {
        guard let fit else { return }
        let sLen = engine.streamLen ?? ov?.streamLenS
        let oLen = engine.origLen ?? ov?.origLenS
        let start = (0 - fit.a) / fit.r
        let end = oLen != nil ? (oLen! - fit.a) / fit.r : (sLen ?? start + 300)
        winT0 = max(0, start - K.marginS)
        winT1 = sLen != nil ? min(sLen!, end + K.marginS) : end + K.marginS
        let clampL = start < 0
        let clampR = sLen != nil && end > sLen!
        var info = "window \(fmtT(winT0)) – \(fmtT(winT1)) (original's span + \(Int(K.marginS)) s margins"
        if clampL || clampR {
            let side = clampL && clampR ? "at both edges" : clampL ? "at the start" : "at the end"
            info += "; the original overhangs the capture \(side) — clamped, the overhang is unavailable"
        }
        windowInfo = info + ")"
        cursor = min(max(cursor, winT0), winT1)
    }

    // ---------- playback ----------

    func resetPlayback() {
        stopPlayback()
        usedAnchors.removeAll()
        maxAbsDriftMs = nil
        cursor = winT0
        if let fit { lastCross = crossCount(winT0, fit: fit) }
    }

    func stopPlayback() {
        if playing { cursor = engine.streamPos(fallback: cursor) }
        playing = false
        engine.stopRun()
        driftMs = nil
        origPosNow = nil
        targetNow = nil
    }

    func startPlayback() {
        guard let fit, audioReady else { return }
        do {
            try engine.startRun(from: cursor, fit: fit, effMode: effMode, winT1: winT1,
                                swap: swap, volStream: volStream, volOrig: volOrig,
                                holdSolo: holdSolo, latchSolo: latchSolo)
            playing = true   // §6.2: the anchor set is frozen for the playback run
            lastCross = crossCount(cursor, fit: fit)
            audioStatus = ""
        } catch {
            audioStatus = "playback failed: \(error.localizedDescription)"
        }
    }

    func playPause() {
        if playing { stopPlayback() } else { startPlayback() }
    }

    // §6.2: scrubbing is ALWAYS allowed, during playback too — it moves the
    // position, never the model (re-anchor re-seats from the nearest prior
    // anchor because the fresh schedule is governed by it)
    func seek(to t: Double) {
        let clamped = min(max(t, winT0), winT1)
        cursor = clamped
        if let fit { lastCross = crossCount(clamped, fit: fit) }
        if playing {
            engine.stopRun()
            startPlayback()
        }
        rearmFineStrips()
    }

    // DP-37 (review iteration 1 P1): entering an uncovered region while zoomed
    // re-arms the fetcher (viewport slots first) so the fine detail — and with
    // it the truthful label/max — follows the operator; a failed fetch parks
    // the auto-retry until the zoom control is re-engaged
    func rearmFineStrips() {
        guard zoom != "1", sourcesEnabled, !(fine?.failed ?? false),
              !viewportFine else { return }
        Task { await self.ensureFineStrips() }
    }

    func startTimer() {
        stopTimer()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tickUI() }
        }
    }

    func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    private func tickUI() {
        guard playing, let fit else { return }
        let pos = engine.streamPos(fallback: cursor)
        cursor = pos
        // §6.3: mark anchors as playback crosses them (re-anchor mode's usage record)
        if effMode == "re-anchor" {
            let cc = crossCount(pos, fit: fit)
            if cc > lastCross {
                for p in fit.anchors.prefix(cc).suffix(cc - lastCross) {
                    usedAnchors.insert(p.id)
                }
            }
            if cc != lastCross { lastCross = cc }
        }
        // telemetry: measured original position vs the §6.3 target — readout only
        let target = mapOrig(pos, fit: fit, effMode: effMode)
        targetNow = target
        origPosNow = engine.origPos()
        if let op = origPosNow, target >= 0, target <= (engine.origLen ?? .infinity) {
            let d = (op - target) * 1000.0
            driftMs = d
            if abs(d) > (maxAbsDriftMs ?? 0) { maxAbsDriftMs = abs(d) }
        } else {
            driftMs = nil   // §6.4 margin/overhang: stream solo, nothing to compare
        }
        if pos >= winT1 - 0.02 {
            stopPlayback()
            cursor = winT1
        }
        rearmFineStrips()   // playback advancing into an uncovered region
    }

    // ---------- pair open + audio fetch ----------

    func openPair(_ e: PairEntry) {
        guard !e.usable.isEmpty else { return }
        stopPlayback()
        cur = e
        notes = []
        usedAnchors.removeAll()
        notePos = nil
        noteStatus = ""
        markerInfo = ""
        // the server refuses duplicate anchor stream_s at 6 dp, and hints +
        // placed routinely carry the SAME instant — default select-all keeps the
        // first point per instant (stream order), mirroring the toggle guard
        var seen = Set<String>()
        sel = Set(e.usable.compactMap { p -> String? in
            guard let s6 = p.streamS.map({ String(format: "%.6f", $0) }),
                  !seen.contains(s6) else { return nil }
            seen.insert(s6)
            return p.id
        })
        ov = nil
        fine = fineCache[e.id]     // DP-37: fine strips ride their own cache
        audioReady = false
        pickerOpen = false
        recomputeFit()
        resetPlayback()
        loadGeneration += 1
        let gen = loadGeneration
        Task { await self.loadAudio(for: e, generation: gen) }
        Task { await self.fetchOverview(for: e, generation: gen) }
        if zoom != "1" { Task { await self.ensureFineStrips() } }  // a restored zoom wants strips
    }

    // the server-resolved identity of one audio route's answer: X-Review-Source
    // (the file rangeserve actually served), its total byte size from the
    // Content-Range of a 1-byte probe, and X-Review-Source-Rev (the server's
    // mtime_ns-size revision token — the repo's name:size:mtime cache-key
    // convention; rangeserve deliberately carries no ETag/Last-Modified). The
    // cache key embeds all three, so a re-rip — even an in-place one at an
    // UNCHANGED size — or a different resolved tier changes the key and the
    // cache misses instead of silently replaying stale bytes.
    private struct SourceIdentity {
        let source: String
        let size: Int64
        let rev: String
        let ext: String
    }

    private func probeIdentity(path: String) async throws -> SourceIdentity {
        var req = api.request(path)
        req.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        let (body, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw NSError(domain: "align", code: 7,
                          userInfo: [NSLocalizedDescriptionKey: "no HTTP response from \(path)"])
        }
        guard http.statusCode == 206 || http.statusCode == 200 else {
            // a JSON body is the route's own refusal (e.g. the wv-only 503
            // naming the DP-23 sync) — deterministic: surface it, never retry
            var info: [String: Any] = [NSLocalizedDescriptionKey: "HTTP \(http.statusCode) from \(path)"]
            if let j = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
               let msg = j["error"] as? String {
                info[NSLocalizedDescriptionKey] = msg
                info["alignFinal"] = true
            }
            if let ra = http.value(forHTTPHeaderField: "Retry-After") { info["retryAfter"] = ra }
            throw NSError(domain: "align", code: http.statusCode, userInfo: info)
        }
        let src = (http.value(forHTTPHeaderField: "X-Review-Source") ?? "")
            .removingPercentEncoding ?? ""
        var size: Int64 = -1
        if let cr = http.value(forHTTPHeaderField: "Content-Range"),
           let total = cr.split(separator: "/").last, let n = Int64(total) {
            size = n
        } else if http.statusCode == 200 {
            size = http.expectedContentLength
        }
        let rev = String((http.value(forHTTPHeaderField: "X-Review-Source-Rev") ?? "")
            .unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) || $0 == "-" }
            .map(Character.init)).prefix(40)
        return SourceIdentity(source: src, size: size, rev: String(rev),
                              ext: (src as NSString).pathExtension)
    }

    private func cacheURL(prefix: String, ident: SourceIdentity, fallbackExt: String) -> URL {
        let stem = ((ident.source as NSString).lastPathComponent as NSString)
            .deletingPathExtension
        let safe = String(stem.unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0) || $0 == "." || $0 == "_" || $0 == "-"
        }.map(Character.init)).prefix(80)
        let ext = ident.ext.isEmpty ? fallbackExt : ident.ext
        let rev = ident.rev.isEmpty ? "norev" : ident.rev
        return cacheDir.appendingPathComponent(
            "\(prefix)-\(safe)-\(ident.size)-\(rev).\(ext)")
    }

    private func fetchAudio(path: String, cachePrefix: String, fallbackExt: String,
                            progress: @escaping (Double) -> Void) async throws -> URL {
        var lastErr: Error = NSError(domain: "align", code: 5,
                                     userInfo: [NSLocalizedDescriptionKey: "download failed"])
        for attempt in 0..<3 {
            do {
                let ident = try await probeIdentity(path: path)
                let dest = cacheURL(prefix: cachePrefix, ident: ident, fallbackExt: fallbackExt)
                if let onDisk = try? dest.resourceValues(forKeys: [.fileSizeKey]).fileSize,
                   onDisk > 0, ident.size <= 0 || Int64(onDisk) == ident.size {
                    progress(1.0)
                    return dest
                }
                let (tmp, resp) = try await withCheckedThrowingContinuation { cont in
                    Downloader().fetch(api.request(path), progress: progress) { cont.resume(with: $0) }
                }
                if resp.statusCode == 200 {
                    try? FileManager.default.removeItem(at: dest)
                    try FileManager.default.moveItem(at: tmp, to: dest)
                    return dest
                }
                // a non-200 with a JSON body is the route's own refusal (e.g. the
                // wv-only 503 naming the DP-23 sync) — deterministic, never retried
                var info: [String: Any] = [NSLocalizedDescriptionKey: "HTTP \(resp.statusCode) from \(path)"]
                if let body = try? Data(contentsOf: tmp),
                   let j = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
                   let msg = j["error"] as? String {
                    info[NSLocalizedDescriptionKey] = msg
                    info["alignFinal"] = true
                }
                try? FileManager.default.removeItem(at: tmp)
                throw NSError(domain: "align", code: resp.statusCode, userInfo: info)
            } catch where attempt < 2 && (error as NSError).userInfo["alignFinal"] == nil {
                lastErr = error
                // transient (network hiccup / plain 5xx): wait and retry
                audioStatus = "audio fetch failed — retry \(attempt + 1)/3"
                try? await Task.sleep(nanoseconds: 2_500_000_000)
            }
        }
        throw lastErr
    }

    private func loadAudio(for e: PairEntry, generation: Int) async {
        audioStatus = "loading audio…"
        streamProgress = 0
        origProgress = 0
        do {
            async let sTask = fetchAudio(path: "/api/align/audio/stream/\(e.stem)",
                                         cachePrefix: "stream-\(e.stem)", fallbackExt: "mp3",
                                         progress: { [weak self] f in
                                             Task { @MainActor in self?.streamProgress = f }
                                         })
            async let oTask = fetchAudio(path: "/api/align/audio/orig/\(e.orig)",
                                         cachePrefix: String(format: "orig-%03d", e.orig),
                                         fallbackExt: "flac",
                                         progress: { [weak self] f in
                                             Task { @MainActor in self?.origProgress = f }
                                         })
            let (sURL, oURL) = try await (sTask, oTask)
            guard generation == loadGeneration else { return }
            try engine.load(streamURL: sURL, origURL: oURL)
            audioReady = true
            audioStatus = "audio ready — stream \(fmtT(engine.streamLen ?? 0)), original \(fmtT(engine.origLen ?? 0)) native"
            recomputeWindow()
            cursor = winT0
            if let fit { lastCross = crossCount(winT0, fit: fit) }
        } catch {
            guard generation == loadGeneration else { return }
            // the route's refusal text (when present) already names the fix
            audioStatus = "audio failed to load: \(error.localizedDescription)" +
                ((error as NSError).userInfo["alignFinal"] != nil ? "" :
                 " — if this original is wv-only, run the DP-23 sync (scripts/sync_sources.sh)")
        }
    }

    // envelope strips: ONE overview fetch per pair through the DSP boundary,
    // exactly the web page's request shape; dark DSP degrades to a position bar
    private func fetchOverview(for e: PairEntry, generation: Int) async {
        guard sourcesEnabled else {
            waveInfo = "DSP dark — position-bar mode (markers still live)"
            return
        }
        if let hit = ovCache[e.id] {
            ov = hit
            recomputeWindow()
            return
        }
        let bySrc = Dictionary(grouping: e.usable, by: { $0.source })
        let hintsPts = bySrc["hints"] ?? []
        let placedPts = bySrc["placed"] ?? []
        let knots = (hintsPts.count >= placedPts.count ? hintsPts : placedPts)
            .sorted { ($0.streamS ?? 0) < ($1.streamS ?? 0) }
        guard let first = knots.first, let ft = first.streamS, let fo = first.origS else { return }
        let body: [String: Any] = [
            "stem": e.stem, "orig": e.orig,
            "stream_t": ft, "orig_t": fo,
            "rate": fit?.r ?? 1.0,
            "invert": e.inverted,
            "points": knots.compactMap { p -> [Double]? in
                guard let s = p.streamS, let o = p.origS else { return nil }
                return [s, o]
            },
        ]
        waveInfo = "computing waveform envelopes…"
        for _ in 0..<4 {
            do {
                let (status, j) = try await api.postJSON("/api/align/overview", body: body)
                guard generation == loadGeneration else { return }
                if status == 429 {
                    try await Task.sleep(nanoseconds: 1_200_000_000)
                    continue
                }
                if let err = j["error"] as? String {
                    waveInfo = "envelopes unavailable: \(err) — position-bar mode"
                    return
                }
                guard let parsed = parseOverview(j) else {
                    waveInfo = "envelopes unavailable (bad payload) — position-bar mode"
                    return
                }
                ov = parsed
                ovCache[e.id] = parsed
                waveInfo = ""
                recomputeWindow()
                if !playing, cursor < winT0 { cursor = winT0 }
                // DP-37: the final window can grow the tile set — through the
                // guard, so a parked pair stays parked (review iteration 3)
                rearmFineStrips()
                return
            } catch {
                waveInfo = "envelopes unavailable (\(error.localizedDescription)) — position-bar mode"
                return
            }
        }
        waveInfo = "envelopes unavailable (busy) — position-bar mode"
    }

    // ---------- §6.6 notes ----------

    func placeNote(at t: Double) {
        // §6.6: placement happens while PAUSED, by clicking the waveform
        guard !playing, fit != nil else { return }
        notePos = min(max(t, winT0), winT1)
        noteStatus = ""
    }

    func nudgeNote(_ d: Double) {
        guard let p = notePos else { return }
        notePos = min(max(p + d, winT0), winT1)
    }

    func cancelNote() {
        notePos = nil
        noteText = ""
        noteStatus = ""
    }

    func saveNote() async {
        guard let pos = notePos, let fit, let cur else { return }
        let text = String(noteText.prefix(K.maxTextLen)).trimmingCharacters(in: .whitespaces)
        if text.isEmpty {
            noteStatus = "type the note first"
            return
        }
        // §6.6/§6.7: POST with the FULL session context — mode, the canonical
        // fitted rate, and the selected anchor set — exactly what the writer's
        // session + anchor rows persist (the server re-derives orig_t and
        // rejects a mismatch)
        let body: [String: Any] = [
            "stem": cur.stem,
            "orig": cur.orig,
            "stream_t": round6(pos),
            "orig_t": round6(mapOrigClamped(pos, fit: fit, effMode: effMode)),
            "text": text,
            "rate": fit.r,
            "mode": effMode,
            "pairs": fit.anchors.map { ["k": $0.k, "stream_s": $0.streamS, "orig_s": $0.origS] },
        ]
        noteStatus = "saving…"
        do {
            let (status, j) = try await api.postJSON("/api/align/review-note", body: body)
            if status != 200 {
                // errors surface honestly — incl. the 404 when review notes are dark
                noteStatus = "HTTP \(status): \((j["error"] as? String) ?? "save failed")"
                return
            }
            notes.append(SavedNote(streamT: round6(pos),
                                   origT: round6(mapOrigClamped(pos, fit: fit, effMode: effMode)),
                                   text: text))
            markerInfo = "saved — session \((j["session"] as? String) ?? "?") → \((j["file"] as? String) ?? "?")"
            noteText = ""
            cancelNote()
        } catch {
            noteStatus = "save failed: \(error.localizedDescription)"
        }
    }
}

// MARK: - autotest (the §6.10 live self-test, driving the REAL model + engine)
//
// NETRADIO_REVIEW_AUTOTEST=1 makes the launched app drive itself: open the pair
// named by NETRADIO_REVIEW_AUTOPAIR ("stem|orig", default: the first usable),
// play ~30 s in re-anchor then constant-rate logging the sample-lock telemetry
// once per second, snapshot its own window (permission-free NSView render, no
// screen-recording entitlement) to native/screenshots/, place one test note
// through the real POST path, and exit. This is the harness the PR's live-test
// evidence comes from — it exercises the exact production code paths.

extension ReviewModel {
    func autotest() async {
        func log(_ s: String) { print("AUTOTEST " + s) }
        let env = ProcessInfo.processInfo.environment
        let playS = Double(env["NETRADIO_REVIEW_AUTOTEST_S"] ?? "") ?? 30
        var target: PairEntry?
        if let spec = env["NETRADIO_REVIEW_AUTOPAIR"] {
            let bits = spec.split(separator: "|")
            if bits.count == 2, let o = Int(bits[1]) {
                target = entries.first { $0.stem == String(bits[0]) && $0.orig == o }
            }
        }
        guard let e = target ?? entries.first(where: { !$0.usable.isEmpty }) else {
            log("FAIL: no usable pair"); exit(2)
        }
        log("pair \(e.stem) <-> \(e.origLabel) (\(e.usable.count) usable points)")
        openPair(e)
        for _ in 0..<600 {   // wait for audio (large originals take a while)
            if audioReady { break }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        guard audioReady else { log("FAIL: audio never became ready — \(audioStatus)"); exit(2) }
        log("audio ready: \(audioStatus)")
        // DP-36: the applied per-branch mixer gains (down-mix comp included) at
        // default, at the slider extremes, and after reset — the sliders' proof
        func lv() -> String {
            String(format: "streamMix=%.4f origMix=%.4f",
                   engine.streamLevel, engine.origLevel)
        }
        volStream = 1.0; volOrig = 1.0
        log("DP-36 levels 100%/100% (unmodified: 0.5*(L+R) per stereo ear): " + lv())
        volStream = 2.0; volOrig = 0.5
        log("DP-36 levels 200%/50%: " + lv())
        volStream = 1.0; volOrig = 1.0
        log("DP-36 levels reset 100%/100%: " + lv())
        log("window \(String(format: "%.3f", winT0)) – \(String(format: "%.3f", winT1)); " +
            "fit r=\(fit.map { String(format: "%.6f", $0.r) } ?? "—") mode=\(effMode)")
        for m in ["re-anchor", "constant-rate"] {
            setMode(m)
            // start just before an anchor so a crossing (and, in re-anchor mode,
            // the A/B branch handoff) actually happens inside the observed span
            if let f = fit, let a = f.anchors.first(where: { $0.streamS > winT0 + 5 }) {
                seek(to: max(winT0, a.streamS - 8))
                log("\(m): seeked to \(String(format: "%.3f", cursor)) " +
                    "(anchor \(a.k) at \(String(format: "%.3f", a.streamS)))")
            }
            startPlayback()
            guard playing else { log("FAIL: playback did not start (\(audioStatus))"); exit(2) }
            let n = Int(playS)
            for i in 0..<n {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                let d = driftMs.map { String(format: "%+.2f", $0) } ?? "—"
                let op = origPosNow.map { String(format: "%.3f", $0) } ?? "—"
                let tg = targetNow.map { String(format: "%.3f", $0) } ?? "—"
                log("\(m) t+\(i + 1)s stream=\(String(format: "%.3f", cursor)) " +
                    "orig=\(op) target=\(tg) drift=\(d)ms used=\(usedAnchors.count)")
                if i == n / 2 { snapshotWindow(name: "autotest-\(m)") }
            }
            log("\(m) done: max |drift| " +
                (maxAbsDriftMs.map { String(format: "%.2f ms", $0) } ?? "—") +
                ", used anchors \(usedAnchors.count), corrections none (no corrector exists)")
            stopPlayback()
        }
        // §6.6 round trip through the real POST path
        placeNote(at: winT0 + min(25, (winT1 - winT0) / 2))
        noteText = env["NETRADIO_REVIEW_NOTE_TEXT"] ?? "DP-22 mac POC autotest note"
        await saveNote()
        log("note result: \(markerInfo.isEmpty ? noteStatus : markerInfo)")
        snapshotWindow(name: "autotest-final")
        log("DONE")
        exit(markerInfo.hasPrefix("saved") ? 0 : 3)
    }

    func snapshotWindow(name: String) {
        guard let win = NSApplication.shared.windows.first,
              let view = win.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return }
        let dir = URL(fileURLWithPath: "native/screenshots", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(name + ".png")
        try? png.write(to: dest)
        print("AUTOTEST screenshot -> \(dest.path)")
    }
}

// MARK: - views

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xff) / 255.0,
                  green: Double((hex >> 8) & 0xff) / 255.0,
                  blue: Double(hex & 0xff) / 255.0)
    }
}

enum Pal {
    static let bg = Color(hex: 0x14161a)
    static let panel = Color(hex: 0x1d2127)
    static let ink = Color(hex: 0xd8dee6)
    static let dim = Color(hex: 0x8a93a0)
    static let accent = Color(hex: 0x5ec2ff)
    static let stream = Color(hex: 0x6fd08c)
    static let orig = Color(hex: 0xe0b452)
    static let note = Color(hex: 0xc98cf0)
    static let warn = Color(hex: 0xe6b352)
    static let line = Color(hex: 0x2c323b)
    static let err = Color(hex: 0xee6666)
}

struct WaveformView: View {
    @ObservedObject var model: ReviewModel
    @State private var dragNote = false

    var body: some View {
        GeometryReader { geo in
            Canvas { g, size in
                draw(&g, size: size)
            }
            .background(Color(hex: 0x171a1f))
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { v in handleDrag(v, size: geo.size) }
                .onEnded { _ in dragNote = false })
            .onAppear { updateDevW(geo.size) }
            .onChange(of: geo.size) { _, size in updateDevW(size) }
        }
        .frame(height: K.waveH)          // DP-37: the doubled lane block
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(Pal.line))
    }

    // DP-37: the MAX-zoom honesty formula wants the canvas width in DEVICE px
    private func updateDevW(_ size: CGSize) {
        let scale = Double((NSApp.keyWindow?.screen ?? NSScreen.main)?.backingScaleFactor ?? 2)
        model.waveDevW = Double(size.width) * scale
    }

    private func timeAt(x: CGFloat, width: CGFloat) -> Double {
        // DP-37: clicks map through the zoomed span, same as every marker
        let span = model.viewSpan
        return model.cursor - span / 2 + Double(x / max(width, 1)) * span
    }

    private func xOf(_ t: Double, width: CGFloat) -> CGFloat {
        let span = model.viewSpan
        return CGFloat((t - (model.cursor - span / 2)) / span) * width
    }

    private func handleDrag(_ v: DragGesture.Value, size: CGSize) {
        let began = !dragNote
        if began, let hit = hitMarker(at: v.location, size: size) {
            model.markerInfo = hit
            return
        }
        if model.playing || model.fit == nil { return }
        dragNote = true
        model.placeNote(at: timeAt(x: v.location.x, width: size.width))
    }

    private func hitMarker(at pt: CGPoint, size: CGSize) -> String? {
        guard let cur = model.cur else { return nil }
        // note markers live in the bottom band; anchor flags in the top band
        if pt.y > size.height - 24 {
            for nt in model.notes {
                let x = xOf(nt.streamT, width: size.width)
                if abs(pt.x - x) < 10 {
                    return "note @ \(String(format: "%.3f", nt.streamT)) s: \(nt.text)"
                }
            }
        }
        if pt.y < 18 {
            for p in cur.usable {
                guard let s = p.streamS else { continue }
                let x = xOf(s, width: size.width)
                if pt.x >= x - 2 && pt.x <= x + 26 {
                    let used = model.usedAnchors.contains(p.id) ? " · used this run" : ""
                    return "anchor \(p.k) (\(p.source)) — stream \(String(format: "%.3f", s)) s" +
                        (p.origS.map { " · orig \(String(format: "%.3f", $0)) s" } ?? "") + used
                }
            }
        }
        return nil
    }

    private func draw(_ g: inout GraphicsContext, size: CGSize) {
        guard let cur = model.cur, let fit = model.fit else { return }
        let cw = size.width
        let chh = size.height
        let span = model.viewSpan                    // DP-37: viewS / zoom factor
        let tL = model.cursor - span / 2
        func x0(_ t: Double) -> CGFloat { CGFloat((t - tL) / span) * cw }
        let laneH = (chh - 24) / 2
        let midS = 4 + laneH / 2
        let midO = 20 + laneH + laneH / 2

        // §6.4 margins (right ear silent) + window bounds
        let spanL = (0 - fit.a) / fit.r
        let spanR = (model.engine.origLen ?? model.ov?.origLenS).map { ($0 - fit.a) / fit.r }
        var m = Path()
        if spanL > tL {
            m.addRect(CGRect(x: 0, y: 20 + laneH, width: min(cw, x0(spanL)), height: laneH + 4))
        }
        if let sr = spanR, sr < tL + span {
            m.addRect(CGRect(x: max(0, x0(sr)), y: 20 + laneH, width: cw - max(0, x0(sr)), height: laneH + 4))
        }
        g.fill(m, with: .color(Pal.dim.opacity(0.10)))
        var shade = Path()
        if model.winT0 > tL {
            shade.addRect(CGRect(x: 0, y: 0, width: min(cw, x0(model.winT0)), height: chh))
        }
        if model.winT1 < tL + span {
            shade.addRect(CGRect(x: max(0, x0(model.winT1)), y: 0, width: cw - max(0, x0(model.winT1)), height: chh))
        }
        g.fill(shade, with: .color(.black.opacity(0.35)))

        // DP-37: each lane draws from an ORDERED source list — the 50 cols/s
        // fine strips first where loaded, the ~6 cols/s overview beneath; per
        // pixel the FIRST source covering the pixel's time wins, so zoomed
        // views show real fine data wherever it exists and the coarse envelope
        // elsewhere — never an interpolation of either.
        var streamSrcs: [LaneSrc] = []
        var origSrcs: [LaneSrc] = []
        if let fine = model.fine {
            for tl in fine.tiles {
                streamSrcs.append(LaneSrc(env: tl.stream, t0: tl.t0, t1: tl.t1,
                                          clip0: tl.t0, clip1: tl.t1, scale: 1))
                if !tl.origOk { continue }   // margin tile: overview fallback for the orig lane
                origSrcs.append(LaneSrc(
                    env: tl.orig, t0: tl.t0, t1: tl.t1,
                    clip0: max(tl.t0, spanL),
                    clip1: spanR.map { min(tl.t1, $0) } ?? tl.t1,
                    // one amplitude scale: the overview is whole-file
                    // RMS-matched, each tile only over its own ±60 s
                    scale: (model.ov != nil && tl.rmsGain > 1e-9)
                        ? model.ov!.rmsGain / tl.rmsGain : 1))
            }
        }
        if let ov = model.ov {
            // stream lane from the whole-capture overview envelope (~6 cols/s)
            streamSrcs.append(LaneSrc(env: ov.stream, t0: 0, t1: ov.streamLenS,
                                      clip0: 0, clip1: ov.streamLenS, scale: 1))
            // original lane: piecewise-stretched segments, already in stream time
            for seg in ov.segments where !seg.env.mins.isEmpty {
                origSrcs.append(LaneSrc(env: seg.env, t0: seg.a0, t1: seg.a1,
                                        clip0: seg.a0, clip1: seg.a1, scale: 1))
            }
        }
        if streamSrcs.isEmpty && origSrcs.isEmpty {
            // DSP dark: degrade honestly to a position bar — markers still live
            var p = Path()
            p.move(to: CGPoint(x: 0, y: midS)); p.addLine(to: CGPoint(x: cw, y: midS))
            p.move(to: CGPoint(x: 0, y: midO)); p.addLine(to: CGPoint(x: cw, y: midO))
            g.stroke(p, with: .color(Pal.line))
        } else {
            drawLane(&g, srcs: streamSrcs, cw: cw, tL: tL, span: span,
                     mid: midS, amp: laneH * 0.46, color: Pal.stream)
            drawLane(&g, srcs: origSrcs, cw: cw, tL: tL, span: span,
                     mid: midO, amp: laneH * 0.46, color: Pal.orig)
        }

        g.draw(Text("stream L").font(.system(size: 10, design: .monospaced)).foregroundColor(Pal.dim),
               at: CGPoint(x: 30, y: 10))
        g.draw(Text("\(cur.origLabel) R (rate \(String(format: "%.4f", fit.r)))")
            .font(.system(size: 10, design: .monospaced)).foregroundColor(Pal.dim),
               at: CGPoint(x: 70, y: 20 + laneH + 10))

        // anchor flags: dim unselected, bright selected, accent + ✓ once USED (§6.3)
        for p in cur.usable {
            guard let s = p.streamS else { continue }
            let x = x0(s)
            if x < -30 || x > cw + 30 { continue }
            let selOn = model.sel.contains(p.id)
            let used = model.usedAnchors.contains(p.id)
            var line = Path()
            line.move(to: CGPoint(x: x, y: 0)); line.addLine(to: CGPoint(x: x, y: chh))
            g.stroke(line, with: .color(used ? Pal.accent.opacity(0.9)
                                        : selOn ? Pal.ink.opacity(0.7) : Pal.dim.opacity(0.25)))
            let flag = CGRect(x: x + 1.5, y: 2, width: 22, height: 14)
            g.fill(Path(flag), with: .color(used ? Pal.accent : selOn ? Color(hex: 0x3a414c) : Color(hex: 0x232830)))
            g.draw(Text(String(p.k.prefix(2)) + (used ? "✓" : ""))
                .font(.system(size: 10, design: .monospaced))
                .foregroundColor(used ? Color(hex: 0x08222f) : selOn ? Pal.ink : Pal.dim),
                   at: CGPoint(x: flag.midX, y: flag.midY))
        }

        // §6.6: saved notes render as markers immediately
        for nt in model.notes {
            let x = x0(nt.streamT)
            if x < -20 || x > cw + 20 { continue }
            var line = Path()
            line.move(to: CGPoint(x: x, y: 0)); line.addLine(to: CGPoint(x: x, y: chh))
            g.stroke(line, with: .color(Pal.note.opacity(0.7)))
            var dia = Path()
            dia.move(to: CGPoint(x: x, y: chh - 18)); dia.addLine(to: CGPoint(x: x + 6, y: chh - 11))
            dia.addLine(to: CGPoint(x: x, y: chh - 4)); dia.addLine(to: CGPoint(x: x - 6, y: chh - 11))
            dia.closeSubpath()
            g.fill(dia, with: .color(Pal.note))
        }

        // playhead, centre
        var ph = Path()
        ph.move(to: CGPoint(x: cw / 2, y: 0)); ph.addLine(to: CGPoint(x: cw / 2, y: chh))
        g.stroke(ph, with: .color(.white.opacity(0.9)))

        // pending note crosshair (§6.6 placement: paused click; draggable)
        if let np = model.notePos {
            let x = x0(np)
            var ch = Path()
            ch.move(to: CGPoint(x: x, y: 0)); ch.addLine(to: CGPoint(x: x, y: chh))
            g.stroke(ch, with: .color(Pal.note.opacity(0.95)), style: StrokeStyle(dash: [4, 3]))
            g.draw(Text(String(format: "%.3f s", np))
                .font(.system(size: 10, design: .monospaced)).foregroundColor(Pal.note),
                   at: CGPoint(x: min(x + 30, cw - 35), y: chh - 8))
        }
    }

    private func drawLane(_ g: inout GraphicsContext, srcs: [LaneSrc], cw: CGFloat,
                          tL: Double, span: Double, mid: CGFloat, amp: CGFloat,
                          color: Color) {
        // min/max envelope over the visible window: per pixel, min/max over the
        // columns that pixel's time span covers (the web page's drawLane), from
        // the FIRST source whose clip window contains the pixel centre — fine
        // strips are listed before the overview (DP-37), so real detail wins
        // wherever it exists and nothing is ever interpolated
        if srcs.isEmpty { return }
        var p = Path()
        for x in stride(from: 0, to: Int(cw), by: 1) {
            let ta = tL + Double(x) / Double(cw) * span
            let tb = tL + Double(x + 1) / Double(cw) * span
            let tm = (ta + tb) / 2
            guard let s = srcs.first(where: { tm >= $0.clip0 && tm <= $0.clip1 })
            else { continue }
            let n = s.env.mins.count
            if n == 0 || s.t1 <= s.t0 { continue }
            func colOfTime(_ t: Double) -> Double { (t - s.t0) / (s.t1 - s.t0) * Double(n) }
            var i0 = Int(colOfTime(ta).rounded(.down))
            var i1 = Int(colOfTime(tb).rounded(.up))
            if i1 <= 0 || i0 >= n { continue }
            i0 = max(0, i0)
            i1 = min(n, max(i1, i0 + 1))
            var lo = 1e9, hi = -1e9
            for i in i0..<i1 {
                lo = min(lo, s.env.mins[i])
                hi = max(hi, s.env.maxs[i])
            }
            if lo > hi { continue }
            lo *= s.scale
            hi *= s.scale
            let yh = mid - CGFloat(min(1, max(-1, hi))) * amp
            let yl = mid - CGFloat(min(1, max(-1, lo))) * amp
            p.move(to: CGPoint(x: CGFloat(x) + 0.5, y: yh))
            p.addLine(to: CGPoint(x: CGFloat(x) + 0.5, y: yl + 0.001))
        }
        g.stroke(p, with: .color(color))
    }
}

struct PickerPane: View {
    @ObservedObject var model: ReviewModel

    var body: some View {
        List {
            ForEach(groupedStems(), id: \.self) { stem in
                Section(stem) {
                    ForEach(model.entries.filter { $0.stem == stem }) { e in
                        row(e)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .overlay(alignment: .bottom) {
            Text(model.pickerStatus)
                .font(.caption).foregroundColor(Pal.dim)
                .padding(6).frame(maxWidth: .infinity).background(.thinMaterial)
        }
    }

    private func groupedStems() -> [String] {
        var seen: [String] = []
        for e in model.entries where !seen.contains(e.stem) { seen.append(e.stem) }
        return seen
    }

    @ViewBuilder
    private func row(_ e: PairEntry) -> some View {
        let usable = !e.usable.isEmpty
        Button {
            model.openPair(e)
        } label: {
            HStack(spacing: 6) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(e.origLabel + (e.origName.isEmpty ? "" : " · " + e.origName))
                        .lineLimit(1)
                    if !usable {
                        // §6.2's 0-pair gate: no rate is ever invented from nothing
                        Text("seat a point in /align (or run match-hints) first")
                            .font(.caption2).foregroundColor(Pal.dim)
                    }
                    if let note = e.rateNote {
                        // the placed op's owner rate rules (analysis #130):
                        // surfaced, never silently applied
                        Text("rate: " + note)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(Pal.warn).lineLimit(2)
                    }
                }
                Spacer()
                ForEach(Array(e.sources).sorted(), id: \.self) { s in
                    Text(s).font(.system(size: 10, design: .monospaced))
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(s == "hints" ? Color(hex: 0x173325) : Color(hex: 0x332a14))
                        .foregroundColor(s == "hints" ? Pal.stream : Pal.orig)
                        .cornerRadius(3)
                }
                if e.nf > 0 {
                    Text("\(e.nf) NOT-FOUND").font(.system(size: 10, design: .monospaced))
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Color(hex: 0x3a1a1a)).foregroundColor(Pal.err).cornerRadius(3)
                }
                Text("\(e.usable.count) pt").font(.system(size: 10, design: .monospaced))
                    .foregroundColor(Pal.dim)
            }
        }
        .buttonStyle(.plain)
        .disabled(!usable)
        .opacity(usable ? 1 : 0.5)
        .listRowBackground(model.cur?.id == e.id ? Pal.accent.opacity(0.15) : nil)
    }
}

struct AnchorListView: View {
    @ObservedObject var model: ReviewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(model.cur?.pointsAll ?? [], id: \.id) { p in
                anchorRow(p)
            }
            Text(model.selInfo).font(.caption).foregroundColor(Pal.dim).padding(.top, 4)
        }
    }

    @ViewBuilder
    private func anchorRow(_ p: AnchorPoint) -> some View {
        let selected = model.sel.contains(p.id)
        // §6.2: locked = unusable, the LAST selected pair, or a running playback
        let locked = !p.usable || (selected && model.sel.count == 1) || model.playing
        HStack(spacing: 8) {
            Toggle("", isOn: Binding(
                get: { selected },
                set: { _ in model.toggleAnchor(p) }
            ))
            .toggleStyle(.checkbox)
            .disabled(locked)
            .labelsHidden()
            Text(p.k).font(.system(size: 12, design: .monospaced))
            let sTxt: String = p.streamS.map { String(format: "%.3f", $0) } ?? "—"
            let oTxt: String = p.origS.map { String(format: "%.3f", $0) } ?? "—"
            Text("s \(sTxt)  o \(oTxt)")
                .font(.system(size: 12, design: .monospaced)).foregroundColor(Pal.dim)
            Text(p.source).font(.system(size: 10, design: .monospaced))
                .padding(.horizontal, 4).padding(.vertical, 1)
                .background(p.source == "hints" ? Color(hex: 0x173325) : Color(hex: 0x332a14))
                .foregroundColor(p.source == "hints" ? Pal.stream : Pal.orig).cornerRadius(3)
            if let grade = p.grade {
                Text(grade).font(.system(size: 10, design: .monospaced))
                    .padding(.horizontal, 4).padding(.vertical, 1)
                    .background(p.isNotFound ? Color(hex: 0x3a1a1a) : Pal.line)
                    .foregroundColor(p.isNotFound ? Pal.err : Pal.dim).cornerRadius(3)
            }
            if let c = p.confidence {
                Text(String(format: "conf %.1f", c)).font(.system(size: 10, design: .monospaced))
                    .foregroundColor(Pal.dim)
            }
            if !p.usable, let why = p.why {
                Text(why).font(.system(size: 10)).foregroundColor(Pal.dim)
            }
            if model.usedAnchors.contains(p.id) {
                Text("used ✓").font(.system(size: 10, design: .monospaced)).foregroundColor(Pal.accent)
            }
            Spacer()
            // §6.2: per-point deviation from the single-rate fit, in ms, live
            if let fit = model.fit, !fit.assumed, let d = fit.devs[p.id] {
                Text(String(format: "%.1f ms", d))
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundColor(abs(d) > 25 ? Pal.warn : Pal.ink)
            }
        }
        .opacity(p.usable ? 1 : 0.45)
    }
}

struct ToolPane: View {
    @ObservedObject var model: ReviewModel
    @State private var showDriftInfo = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                waveformSection
                transportSection
                mappingSection
            }
            .padding(12)
        }
        .background(Pal.bg)
    }

    private var header: String {
        guard let cur = model.cur else { return "" }
        return "stream \(cur.stem) (left) over \(cur.origLabel) (right) — the viewport scrolls with playback"
    }

    @ViewBuilder
    private var waveformSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("WAVEFORM").font(.caption).foregroundColor(Pal.dim).kerning(1)
            Text(header).font(.caption).foregroundColor(Pal.dim)
            // DP-37: time-axis zoom — the max label is what the envelope DATA
            // honestly resolves to on this screen (model.maxZoom), never
            // interpolation
            HStack {
                Picker("", selection: $model.zoom) {
                    Text("×1").tag("1")
                    Text("×2").tag("2")
                    Text("max (×\(model.maxZoom))").tag("max")
                }
                .pickerStyle(.segmented).frame(width: 260).labelsHidden()
                Text(model.zoomInfo).font(.caption).foregroundColor(Pal.dim)
            }
            WaveformView(model: model)
            Text(model.windowInfo).font(.caption).foregroundColor(Pal.dim)
            if !model.waveInfo.isEmpty {
                Text(model.waveInfo).font(.caption).foregroundColor(Pal.warn)
            }
            if !model.markerInfo.isEmpty {
                Text(model.markerInfo).font(.caption).foregroundColor(Pal.ink)
            }
            if model.notePos != nil {
                noteBox
            } else {
                Text("pause, then click the waveform to place a note (drag or nudge to fine-tune)")
                    .font(.caption).foregroundColor(Pal.dim)
            }
        }
        .padding(10).background(Pal.panel).cornerRadius(8)
    }

    @ViewBuilder
    private var noteBox: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                if let np = model.notePos, let fit = model.fit {
                    Text("note at \(String(format: "%.3f", np)) s (orig " +
                         String(format: "%.3f", mapOrigClamped(np, fit: fit, effMode: model.effMode)) +
                         " s native)")
                        .font(.system(size: 12, design: .monospaced))
                }
                Button("−1s") { model.nudgeNote(-1) }
                Button("−0.1s") { model.nudgeNote(-0.1) }
                Button("+0.1s") { model.nudgeNote(0.1) }
                Button("+1s") { model.nudgeNote(1) }
            }
            TextField("free text — published to the PUBLIC analysis repo", text: $model.noteText)
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await model.saveNote() } }
            HStack {
                Button("save note") { Task { await model.saveNote() } }
                    .buttonStyle(.borderedProminent)
                Button("cancel") { model.cancelNote() }
                Text(model.noteStatus).font(.caption)
                    .foregroundColor(model.noteStatus.hasPrefix("saving") ? Pal.dim : Pal.err)
            }
        }
    }

    @ViewBuilder
    private var transportSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("TRANSPORT").font(.caption).foregroundColor(Pal.dim).kerning(1)
            HStack {
                Button(model.playing ? "❚❚ pause" : "▶ play") { model.playPause() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.audioReady || model.fit == nil)
                    .keyboardShortcut(.space, modifiers: [])
                Button("−30s") { model.seek(to: model.cursor - 30) }
                Button("−5s") { model.seek(to: model.cursor - 5) }
                Button("+5s") { model.seek(to: model.cursor + 5) }
                Button("+30s") { model.seek(to: model.cursor + 30) }
            }
            Slider(value: Binding(
                get: { model.cursor },
                set: { model.seek(to: $0) }
            ), in: min(model.winT0, model.winT1)...max(model.winT1, model.winT0 + 0.001))
            HStack {
                Text(fmtT(model.cursor)).font(.system(size: 13, design: .monospaced))
                if let fit = model.fit {
                    let o = mapOrig(model.cursor, fit: fit, effMode: model.effMode)
                    let oLen = model.engine.origLen ?? model.ov?.origLenS ?? .infinity
                    Text(o >= 0 && o <= oLen ? "· orig \(fmtT(o)) native" : "· stream solo (margin)")
                        .font(.system(size: 13, design: .monospaced)).foregroundColor(Pal.dim)
                }
                Spacer()
                Text("drift " + (model.driftMs.map { String(format: "%+.1f ms", $0) } ?? "— ms"))
                    .font(.system(size: 13, design: .monospaced))
                    .foregroundColor((model.driftMs.map { abs($0) > 30 } ?? false) ? Pal.err : Pal.ink)
                    .help(driftHelp)
                Button("ⓘ") { showDriftInfo.toggle() }
                    .buttonStyle(.plain).foregroundColor(Pal.dim)
                    .help(driftHelp)
                    .popover(isPresented: $showDriftInfo) {
                        Text(driftHelp).font(.system(size: 11))
                            .frame(width: 300).padding(10)
                    }
            }
            // the §6.10 proof line: telemetry only — no corrector exists in this app
            Text(telemetryLine).font(.system(size: 11, design: .monospaced)).foregroundColor(Pal.dim)
            HStack {
                Button("⇄ swap L/R") { model.swap.toggle() }
                holdButton
                Toggle("solo latch", isOn: $model.latchSolo).toggleStyle(.button)
            }
            // DP-36: per-source volume, 0–200 % (100 % = unmodified), live during
            // playback; double-click the % readout (or label) resets to 100 %
            volumeRow("stream", value: $model.volStream)
            volumeRow("original", value: $model.volOrig)
            Text(model.swap ? "stream RIGHT · original LEFT (swapped)" : "stream LEFT · original RIGHT")
                .font(.caption).foregroundColor(Pal.dim)
            if !model.audioStatus.isEmpty {
                Text(model.audioStatus).font(.caption)
                    .foregroundColor(model.audioStatus.contains("failed") ? Pal.err : Pal.dim)
            }
            if !model.audioReady {
                HStack {
                    Text("stream").font(.caption).foregroundColor(Pal.dim)
                    ProgressView(value: model.streamProgress)
                    Text("original").font(.caption).foregroundColor(Pal.dim)
                    ProgressView(value: model.origProgress)
                }
            }
        }
        .padding(10).background(Pal.panel).cornerRadius(8)
    }

    // DP-36 volume row: slider 0–2 (0–200 %), % readout amber above 100 % —
    // clipping is possible again there, by USER CHOICE (no limiter, on purpose)
    @ViewBuilder
    private func volumeRow(_ label: String, value: Binding<Double>) -> some View {
        let pct = Int((value.wrappedValue * 100).rounded())
        HStack {
            Text(label).font(.caption).foregroundColor(Pal.dim)
                .frame(width: 52, alignment: .trailing)
                .onTapGesture(count: 2) { value.wrappedValue = 1.0 }
            Slider(value: value, in: K.volMin...K.volMax)
            Text("\(pct)%")
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(pct > 100 ? Pal.warn : Pal.ink)
                .frame(width: 44, alignment: .trailing)
                .onTapGesture(count: 2) { value.wrappedValue = 1.0 }
        }
    }

    // §6.3: the readout carries an info affordance — what it measures, 0 ms =
    // on target, and where the explanation lives
    private let driftHelp = "How far the record's playback is from where the " +
        "fitted mapping says it should be right now — 0 ms = perfectly on target. " +
        "In this app the engine is sample-locked (no corrector): the readout is " +
        "telemetry proving the lock. See docs/PLAN_align_tool.md §6.3."

    private var telemetryLine: String {
        let sp = String(format: "stream %.3f s", model.cursor)
        let op = model.origPosNow.map { String(format: "orig %.3f s", $0) } ?? "orig —"
        let tg = model.targetNow.map { String(format: "target %.3f s", $0) } ?? "target —"
        let mx = model.maxAbsDriftMs.map { String(format: "max |drift| %.1f ms", $0) } ?? "max |drift| —"
        return "\(sp) · \(op) · \(tg) · \(mx) · corrections: none (sample-locked; no corrector exists)"
    }

    // §6.1 momentary hold: pressed = stream solo, released = stereo — one action back
    @ViewBuilder
    private var holdButton: some View {
        Text(model.holdSolo ? "🎙 holding — stream solo" : "🎙 hold: stream solo")
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(model.holdSolo ? Pal.warn : Color(hex: 0x12151a))
            .foregroundColor(model.holdSolo ? Color(hex: 0x241a05) : Pal.ink)
            .cornerRadius(6)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Pal.line))
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { _ in if !model.holdSolo { model.holdSolo = true } }
                .onEnded { _ in model.holdSolo = false })
    }

    @ViewBuilder
    private var mappingSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("MAPPING — §6.2/§6.3").font(.caption).foregroundColor(Pal.dim).kerning(1)
            HStack {
                Picker("", selection: Binding(
                    get: { model.mode },
                    set: { model.setMode($0) }
                )) {
                    Text("constant-rate").tag("constant-rate")
                    Text("re-anchor").tag("re-anchor")
                }
                .pickerStyle(.segmented).frame(width: 260)
                .disabled(model.playing)
                Text("mode + selection are frozen while playing")
                    .font(.caption).foregroundColor(Pal.dim)
            }
            if let fit = model.fit {
                Text("effective rate \(String(format: "%.6f", fit.r))" +
                     (fit.assumed ? " (assumed)"
                      : " · least-squares over \(fit.anchors.count) pairs, intercept " +
                        String(format: "%.3f", fit.a) + " s"))
                    .font(.system(size: 13, design: .monospaced))
                    .foregroundColor(fit.assumed ? Pal.warn : Pal.ink)
            }
            if let err = model.fitError {
                Text(err).font(.caption).foregroundColor(Pal.err)
            }
            if !model.verdict.isEmpty {
                Text(model.verdict).font(.caption).foregroundColor(Pal.dim)
            }
            if let banner = model.assumedBanner {
                Text(banner).font(.caption).foregroundColor(Pal.warn)
                    .padding(6).overlay(RoundedRectangle(cornerRadius: 6).stroke(Pal.warn))
            }
            AnchorListView(model: model)
        }
        .padding(10).background(Pal.panel).cornerRadius(8)
    }
}

struct ContentView: View {
    @StateObject private var model = ReviewModel()

    var body: some View {
        NavigationSplitView {
            PickerPane(model: model)
                .navigationSplitViewColumnWidth(min: 260, ideal: 320)
        } detail: {
            if model.cur != nil {
                ToolPane(model: model)
            } else {
                VStack(spacing: 8) {
                    Text("choose a capture ↔ original pair").foregroundColor(Pal.dim)
                    Text("DP-22 Mac POC: capture LEFT ear, rate-corrected original RIGHT — " +
                         "sample-locked by AVAudioEngine (no drift corrector).")
                        .font(.caption).foregroundColor(Pal.dim)
                    Text("server: \(model.api.base.absoluteString)")
                        .font(.system(size: 11, design: .monospaced)).foregroundColor(Pal.dim)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Pal.bg)
            }
        }
        .frame(minWidth: 1080, minHeight: 720)
        .preferredColorScheme(.dark)
        .task {
            await model.boot()
            model.startTimer()
            if ProcessInfo.processInfo.environment["NETRADIO_REVIEW_AUTOTEST"] == "1" {
                await model.autotest()
            }
        }
        .onAppear {
            NSApplication.shared.setActivationPolicy(.regular)
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }
}

struct ReviewAppUI: App {
    var body: some Scene {
        WindowGroup("align review — Mac POC (DP-22)") {
            ContentView()
        }
    }
}

// MARK: - entry point (--selftest short-circuits before any UI or audio)

@main
struct Main {
    static func main() {
        let args = CommandLine.arguments
        if args.count >= 2, args[1] == "--selftest" {
            let path = args.count >= 3 ? args[2] : "native/review_math_fixture.json"
            exit(Int32(runSelftest(fixturePath: path)))
        }
        // honest failures: an AVFoundation NSException must name itself before
        // the abort, and autotest logs must never be lost in a stdio buffer
        setvbuf(stdout, nil, _IONBF, 0)
        NSSetUncaughtExceptionHandler { e in
            fputs("UNCAUGHT EXCEPTION: \(e.name.rawValue) — \(e.reason ?? "?")\n" +
                  e.callStackSymbols.joined(separator: "\n") + "\n", stderr)
        }
        ReviewAppUI.main()
    }
}
