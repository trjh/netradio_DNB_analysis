#!/usr/bin/python3

import shutil
import re
import os
import sys
import argparse
import contextlib
import json
import time

# Initialize lists to store lines to be sorted and unrecognized lines
sort_lines = []         # stores entries of the form (timestamp1, timestamp2, label, line number)
                        # in preparation for sorting/writing
secondfiles = dict()    # entries for additional wav files, key is wav filename, value is list of timestamp/label entries
line_number = 0
adjust_value = -1       # value for adjusting timestamps if >0
debug = False           # print debug information
filename = None         # filename to read/write, is manipulated by read routine to make backups
secondaryfile = None    # when processing secondary files, use this to check tag against filename
primary_stem = None     # stem of the primary file (from filename/--stem); scopes LABELTRACK names
current_track_name = None  # active LABELTRACK scope while reading sequential input
prev_ts = None          # previous label's start timestamp, to detect label-track block boundaries
seen_labeltrack = False # True once any LABELTRACK marker is seen (i.e. the convention is in use)
missing_markers = []    # (line_number, reason) for label-track blocks lacking a LABELTRACK header
keyword_errors = []     # (line_number, label, reason) for labels that TRIED to be a keyword and failed
auto_notes = []         # (line_number, label) for free text auto-noted -- reported as a summary

# Define regular expressions for matching keywords
keyword_patterns = [
    r"(start(\d+):\s*)?ID(\d+)?:\s*(.+)",
    r"file (start )?sync: (.+):? ([0-9.]+)",
    r"((track)(\d+)?|(orig)(\d+))\s+sync:\s+(.)(.*)",
    r"orig(\d+)\s+(start|end|note):\s+(.*)",
    r"(file|mix) (start|end|note): (.*)",
    r"note(\s\S+?)?: (.*)",
    r"(\d{3})[se](.+)",
]

# Compile regular expressions
keyword_regexes = [re.compile(pattern, flags=re.IGNORECASE) for pattern in keyword_patterns]

# LABELTRACK <name> (Proposal A, PLAN_labelling_process.md): the first (earliest) label of
# each Audacity label track names that track. sort_tsv.py scopes every following label to
# <name> until the next LABELTRACK; the marker itself is stripped from the emitted .tsv.
labeltrack_regex = re.compile(r"^LABELTRACK\s+(\S+)\s*$", flags=re.IGNORECASE)

# Capture/recording stems follow the project's `d<digits>…` naming (e.g. d336-355, d356-375,
# d-25-000b, d180_d-14Nov10-a); originals/tracks are orig<NNN>/track<NNN>. A LABELTRACK whose
# name is a capture stem is re-homed onto that file (file_<name>:); any other name (orig069,
# track, free text) is prefix-expanded onto the active track.
capture_stem_regex = re.compile(r"^d-?\d", flags=re.IGNORECASE)

# Known label/audio suffixes, longest-first, so `d336-355.labels.tsv` -> `d336-355`,
# `070.labels` -> `070` and `d356-375.wav` -> `d356-375`, while a bare `orig069` is left
# untouched. `.labels` is listed in its own right: an in-Audacity track is named `070.labels`,
# with no file extension after it.
_STEM_SUFFIXES = (".starter.labels.tsv", ".auto.labels.tsv", ".labels.tsv", ".labels.txt",
                  ".labels", ".tsv", ".txt", ".wav", ".au", ".mp3")

# A LABELTRACK name is a *track* name, not a label qualifier: the tracks `070.labels`,
# `069-dig.labels` and `069.vinyl` all hold labels written about `orig070` / `orig069`.
# So the qualifier is derived from the track's 3-digit original, not from its stem.
_ORIGINAL_NUM_RE = re.compile(r"(\d{3})")
_ALREADY_QUALIFIER_RE = re.compile(r"^(orig|track)\d+$", flags=re.IGNORECASE)

# Does a label already name its subject? (`orig070 sync: A`, `file end: …`, `069s0`)
# Such a label is emitted as written -- never prefixed with the scope's qualifier.
_QUALIFIED_LABEL_RE = re.compile(
    r"^(orig\d+|track\d+|file_[^:]+:|file\b|mix\b|\d{3}[se])", flags=re.IGNORECASE)

# Did a label *try* to be a keyword and fail? This is the typo net, and it is deliberately
# narrow: a keyword is betrayed by an entity at the head (`orig070…`, `file…`), a verb with
# its colon (`sync:`, `start:`), or the compact sync form (`069s0`). Free text -- `start
# overlap`, `close next sync`, `peak` -- has none of those and is auto-noted, not flagged.
_KEYWORD_SHAPED_RES = [
    re.compile(r"^(orig|track)\s*\d*\b", flags=re.IGNORECASE),   # orig070 …, track sync: …
    re.compile(r"^(file|mix)\b", flags=re.IGNORECASE),           # file start sync: …
    re.compile(r"^(sync|start|end|note|ID)\d*\s*:", flags=re.IGNORECASE),  # sync: 0, start068: …
    re.compile(r"^\w{0,2}\d{2,3}[se]\w*$", flags=re.IGNORECASE), # 069s0, 071eA -- and s71e1
]

# The original a label's *head* refers to (`orig017 sync: A` -> 017, `069s0` -> 069). Anchored
# at the head on purpose: a note's free-text body may mention any number without being a claim
# about it.
_HEAD_REF_RE = re.compile(r"^(?:orig|track)(\d{3})\b|^(\d{3})[se]", flags=re.IGNORECASE)


def _stem(name):
    """Reduce a label filename / audio name / bare name to its capture stem."""
    base = os.path.basename((name or "").strip())
    lowered = base.lower()
    for suffix in _STEM_SUFFIXES:
        if lowered.endswith(suffix):
            return base[: -len(suffix)]
    return os.path.splitext(base)[0]


def is_capture_stem(name):
    """True if `name` looks like a capture-file stem (vs an orig/track/free name)."""
    return bool(capture_stem_regex.match(name or ""))


def classify_track(name, primary):
    """Map a LABELTRACK name to one of: 'primary', 'file', 'prefix'."""
    stem = _stem(name)
    if primary is not None and stem == _stem(primary):
        return "primary"
    if is_capture_stem(stem):
        return "file"
    return "prefix"


def track_qualifier(stem):
    """The label qualifier a prefix-scope LABELTRACK implies: `070.labels` -> `orig070`.

    `069-dig` and `069.vinyl` are two label tracks about the same original, so both yield
    `orig069`. A name that is already a qualifier (`orig069`) is returned as-is; a name with
    no original in it (`mix`) scopes to itself.
    """
    if _ALREADY_QUALIFIER_RE.match(stem):
        return stem
    match = _ORIGINAL_NUM_RE.search(stem)
    return "orig" + match.group(1) if match else stem


def scope_number(stem):
    """The 3-digit original a prefix-scope LABELTRACK is about, or None."""
    match = _ORIGINAL_NUM_RE.search(stem)
    return match.group(1) if match else None


def parses(label):
    """True if `label` matches the label grammar as written."""
    return any(rx.match(label) for rx in keyword_regexes)


def is_keyword_shaped(label):
    """True if `label` tried to be a keyword -- so failing to parse is a typo, not free text."""
    return any(rx.match(label) for rx in _KEYWORD_SHAPED_RES)


def check_scope_ref(label, scopenum):
    """In a numbered label track, every label is about *that* original. Flag any other."""
    match = _HEAD_REF_RE.match(label)
    if not match:
        return
    ref = match.group(1) or match.group(2)
    if ref != scopenum:
        keyword_errors.append(
            (line_number, label,
             "refers to %s inside the %s label track -- typo, or move it to the primary track"
             % (ref, scopenum)))


def expand_label(qualifier, label, scopenum=None):
    """Classify one label inside a LABELTRACK scope and return the text to emit.

    The rules are ordered so that free text costs nothing to write while a mistyped keyword
    still gets caught -- the distinction is *shape*, not whether the label happens to parse:

    0. already names its subject (`orig070 sync: A`, `069s0`) -> emitted as written
    1. `<qualifier> ` + label parses                          -> qualified (`sync: 0` -> `orig070 sync: 0`)
    2. keyword-shaped but parses under no rule                -> ERROR: a typo'd keyword
    3. free text                                              -> a note (listed in the run summary)
    """
    if _QUALIFIED_LABEL_RE.match(label):
        if not parses(label):
            keyword_errors.append((line_number, label, "looks like a keyword but does not parse"))
        elif scopenum:
            check_scope_ref(label, scopenum)
        return label

    if qualifier:
        candidate = qualifier + " " + label
        if parses(candidate):
            return candidate
    elif parses(label):
        return label

    if is_keyword_shaped(label):
        keyword_errors.append((line_number, label, "looks like a keyword but does not parse"))
        return label

    auto_notes.append((line_number, label))
    return "%s note: %s" % (qualifier, label) if qualifier else "note: " + label


def scope_label(name, primary, label):
    """Apply LABELTRACK scoping to one label's text per its name class."""
    kind = classify_track(name, primary)
    if kind == "primary":
        return expand_label(None, label)

    if kind == "file":
        stem = _stem(name)
        # A label may already carry its own `file_<name>:` prefix. Compare by *stem*, so the
        # `.wav` that gets typed in Audacity (`file_d376-395.wav:`) is recognised as this file
        # rather than double-prefixed into a second, bogus secondary (which then re-split on
        # re-entry and blew up the write pass).
        match = re.match(r"file_([^:]+):\s*(.*)", label)
        if match and _stem(match.group(1)) != stem:
            return label     # a label about a *different* file: it routes to that file's bucket
        inner = match.group(2) if match else label
        return "file_%s: %s" % (stem, expand_label(None, inner))

    stem = _stem(name)
    return expand_label(track_qualifier(stem), label, scope_number(stem))


def reset_state():
    """Reset module-level accumulators (used by tests and re-entrant runs)."""
    global sort_lines, secondfiles, line_number, adjust_value, secondaryfile
    global primary_stem, current_track_name, prev_ts, seen_labeltrack, missing_markers
    global keyword_errors, auto_notes
    sort_lines = []
    secondfiles = dict()
    line_number = 0
    adjust_value = -1
    secondaryfile = None
    primary_stem = None
    current_track_name = None
    prev_ts = None
    seen_labeltrack = False
    missing_markers = []
    keyword_errors = []
    auto_notes = []

# Function to sort lines -- put "file start" at the top, always, then sort by
# timestamp, then sort by label
# take in tuple (timestamp, label) -- return tuple (startline, timestamp, label)
def tracksort(entry):
    startline="B"
    if re.match(r"file start", entry[2]):
        startline="A"
    return (startline, entry[0], entry[1], entry[2])

# Function to place tracklist entries onto sorted_lines or second_files
def process_entry(parts):
    global adjust_value
    global sort_lines
    if debug:
        print(f"process_entry({parts})")

    # is this a 'second file' entry?
    if match := re.match(r"file_([^:]+):\s+(.+)",parts[2]):
        secondfile = _stem(match.group(1))
        label = match.group(2)
        if secondfile not in secondfiles:
            secondfiles[secondfile] = []
        # a list, not a tuple: adjust_line() writes back into the entry in place
        secondfiles[secondfile].append([parts[0],parts[1],label,parts[3]])
        return

    # Files using the LABELTRACK convention have already had every label classified by
    # scope_label() -- anything unparseable is either a reported keyword_error or was
    # deliberately auto-noted, so re-warning here would just double up.
    if not seen_labeltrack and not parses(parts[2]):
        sys.stderr.write(f"WARNING: Unrecognized keywords ({parts[2]}) at line {parts[3]} ts {parts[0]}\n")

    if re.match(r"file start", parts[2]):
        if (adjust_value < 0):
            adjust_value = parts[0]
            sys.stderr.write(f".FIRST: adj({adjust_value}) :: {parts[2]}\n")
        else:
            sys.stderr.write(f".Not using adjust {parts[0]} as adjust_value already set ({adjust_value})\n")

    # sanity checks for downstream
    if match := re.match(r"file (start )?sync: (.+):? ([0-9.]+)", parts[2]):
        if 'verified' not in parts[2]:
            sys.stderr.write(f"NOTICE: Sync entry found without 'verified' tag: {parts[2]}, line {parts[3]}\n")
        if secondaryfile and secondaryfile not in match.group(2):
            sys.stderr.write(f"WARN: File start -- secondary file ({secondaryfile}) doesn't match end tag {parts[2]}\n")
        elif secondaryfile and debug:
            print(f"....SF {secondaryfile} MG1 {match.group(1)}")

    if match := re.match(r"file end: (\S+)", parts[2]):
        if not re.search(r"COMPLETE", parts[2]):
            sys.stderr.write(f"NOTICE: File end -- not COMPLETE?  {parts[2]}, line {parts[3]}\n")
        if secondaryfile and secondaryfile not in match.group(1):
            sys.stderr.write(f"WARN: File end -- secondary file ({secondaryfile}) doesn't match end tag {parts[2]}\n")
        elif secondaryfile and debug:
            print(f"....SF {secondaryfile} MG1 {match.group(1)}")

    sort_lines.append((parts))

# Activate a LABELTRACK scope from a marker at start time `ts`
def set_track_scope(name, ts):
    global current_track_name, prev_ts, seen_labeltrack
    seen_labeltrack = True
    current_track_name = _stem(name)
    prev_ts = ts

# Record a missing-LABELTRACK marker at the start of file or a new label-track block.
# Audacity exports each label track as an internally time-sorted block, so a timestamp
# earlier than the previous line's signals a new block; a block that begins without a
# preceding LABELTRACK marker is flagged. The flag only becomes an error when the file
# actually uses the convention (see report_missing) -- legacy marker-less files are left alone.
def check_block_boundary(ts, lineno):
    if current_track_name is None and prev_ts is None:
        missing_markers.append((lineno, "no LABELTRACK marker at start of file"))
    elif prev_ts is not None and ts < prev_ts:
        missing_markers.append(
            (lineno, "backwards timestamp jump (%.6f < %.6f) without a LABELTRACK marker"
             % (ts, prev_ts)))

# Function to turn line strings into tracklist entries and process them with process_entry
def process_line(line):
    global line_number, prev_ts
    line_number += 1
    parts = line.strip().split('\t')
    parts.append(line_number)

    # line validation checks
    if (len(parts) < 4):
        sys.stderr.write(f"Warning: Unrecognized line - less than 3 fields - {line.strip()}\n")
        return

    # float check
    for i in [0,1]:
        try:
            partfloat = float(parts[i])
            parts[i] = partfloat
        except ValueError:
            sys.stderr.write(f"Warning: Unrecognized line - parts[{i}]={parts[i]} not a float - {line.strip()}\n")
            return

    # LABELTRACK <name> marker: scope the following labels and strip it from output
    if match := labeltrack_regex.match(parts[2]):
        set_track_scope(match.group(1), parts[0])
        return

    # flag blocks that start without a LABELTRACK header (missing-marker validation)
    check_block_boundary(parts[0], line_number)

    # scope this label to the active LABELTRACK (no-op for legacy, marker-less files)
    if current_track_name is not None:
        parts[2] = scope_label(current_track_name, primary_stem, parts[2])

    prev_ts = parts[0]
    process_entry(parts)

# adjust timestamps if appropriate
def adjust_line(entry):
    global adjust_value
    for i in range(2):
        entry[i] -= adjust_value
    if match := re.match(r"file (start )?sync: (.+):? ([0-9.]+) (.*)", entry[2]):
        # adjust sync time too
        try:
            syncfloat = float(match.group(3))
            syncfloat += adjust_value
        except:
            sys.stderr.write(f"ERROR in adjust_line: Unable to make {match.group(3)} into a float: {entry}\n")
            syncfloat = match.group(3)

        newlabel = f"file {match.group(1)}sync: {match.group(2)} {syncfloat}"
        if (len(match.groups()) > 3):
            newlabel += " " + match.group(4)
        sys.stderr.write(f"ADJUSTED START OLD: {entry[2]}\n")
        sys.stderr.write(f"ADJUSTED START NEW: {newlabel}\n")
        entry[2] = newlabel

    return entry

# Get labels from filename or stdin
def read_labels_filepipe(test_mode):
    global filename, debug

    # Create a backup of the original file if a filename is provided
    if filename and not test_mode:
        # If we have a .txt extension, move it to .tsv
        if filename.endswith("txt"):
            try:
                moveresult = shutil.move(filename, filename[:-3] + "tsv")
            except Exception as inst:
                print(f"Unable to move {filename} {filename[:-3] + 'tsv'}: {inst}")
                sys.exit('Exiting.')
            sys.stderr.write(f"Moved {filename} to {moveresult}\n")
            filename=filename[:-3] + "tsv"

        backup_filename = filename + ".bak"
        try:
            shutil.copy(filename, backup_filename)
        except Exception as inst:
            print(f"Unable to copy {filename} to {backup_filename}: {inst}")
            sys.exit('Exiting.')
        sys.stderr.write(f"Copied {filename} to {backup_filename}\n")

    # Process input based on whether a filename is provided or not
    sys.stderr.write("Processing primary entries\n")
    with contextlib.ExitStack() as stack:
        input = stack.enter_context(open(filename, 'r')) if filename else sys.stdin
        for line in input:
            if debug:
                print(f"...{line}")
            process_line(line)

# Get labels from current audacity
def read_labels_audacity():
    global debug

    sys.path.append('../scripts')
    import pipeclient

    client = pipeclient.PipeClient()
    client.write('GetInfo: Type=Labels')
    # Allow a little time for Audacity to return the data:
    time.sleep(0.1)
    reply = (client.read())
    if reply == '':
        sys.exit('No data returned.')
    jdata = (reply [:reply.rfind(']')+1])
    if not jdata:
        sys.exit(f"Cannot find JSON in Audacity reply (data below)\n{reply}\n")
    else:
        jresponse = reply[reply.rfind(']')+1:]
        sys.stderr.write(f"Audacity returns: {jresponse}\n")
        if not re.match(r"\s*BatchCommand finished: OK", jresponse):
            sys.exit(f"Unexpected Audacity response status: <<{jresponse}>>")

    data = json.loads(jdata)
    # format of response
    # [labeltrack, ...]
    # where each label track is [tracknumber, [[ts1, ts2, label], ...]
    track_count=0
    label_count=0
    for labels in data:
        track_count += 1
        # tracknumber=labels[0]
        for label in labels[1]:
            if debug:
                sys.stderr.write(f"..label: {label}\n")
            process_entry((label[0], label[1], label[2], label_count))
            label_count += 1
    sys.stderr.write(f"Audacity data: {track_count} tracks, {label_count} labels\n")

# compare two floating-point numbers -- assume there are more significant
# figures in the first than the second, round them both to the lower number of
# significant figures
def floatcmp(a,b):
    global debug

    sigdigits = 100

    for v in [a, b]:
        textb = str(v)
        if (textb.rfind('.') < 0):
            sigdigits = 0
        else:
            fraction = textb[textb.rfind('.')+1:]
            if (len(fraction) < sigdigits):
                sigdigits = len(fraction)

    cmpa = round(float(a),sigdigits)
    cmpb = round(float(b),sigdigits)

    if (debug):
        sys.stderr.write(f"->floatcmp: {textb} f {fraction} len {len(fraction)} ({cmpa} <=> {cmpb})\n")
    if cmpa == cmpb:
        return 0
    elif cmpa < cmpb:
        return -1
    return 1

# Print any missing-LABELTRACK errors; return the count that count as errors.
# Only files that actually use the LABELTRACK convention are held to it -- a legacy file
# with no markers at all is left alone (returns 0).
def report_missing():
    if not seen_labeltrack or not missing_markers:
        return 0
    sys.stderr.write("ERROR: %d label-track block(s) missing a LABELTRACK marker:\n"
                     % len(missing_markers))
    for lineno, reason in missing_markers:
        sys.stderr.write(f"  line {lineno}: {reason}\n")
    sys.stderr.write("  Add `LABELTRACK <name>` as the first label of each track in Audacity.\n")
    return len(missing_markers)

# Sort the primary entries and each secondary file's entries into the final write order,
# applying timestamp adjustment if requested. Returns the assembled list.
def assemble_write_lines(do_adjustment):
    global sort_lines, secondaryfile
    write_lines = []
    sort_lines.sort(key=tracksort)
    write_lines.extend(sort_lines)
    sort_lines = []
    # Work-queue, not `for sf in secondfiles`: process_entry() splits any `file_<other>:` label
    # it is handed, so a secondary's own labels can name a *further* file and add a key. Walking
    # the dict directly raised "dictionary changed size during iteration" the moment that
    # happened; draining a queue processes the new file instead of dying on it.
    spans = []          # (begin, end, stem) of each secondary's rows within write_lines
    done = set()
    while True:
        pending = [sf for sf in secondfiles if sf not in done]
        if not pending:
            break
        for sf in pending:
            done.add(sf)
            sys.stderr.write(f"---\nProcessing secondary entries for file {sf}\n")
            secondaryfile = sf
            for entry in secondfiles[sf]:
                process_entry(entry)
            sort_lines.sort(key=tracksort)
            begin = len(write_lines)
            write_lines.extend(sort_lines)
            spans.append((begin, len(write_lines), sf))
            sort_lines = []
    secondaryfile = None
    if do_adjustment:
        write_lines = list(map(adjust_line, write_lines))
    # Re-attach the `file_<stem>:` prefix process_entry() peeled off to bucket the row.
    # Peeling it and writing the bare inner label was DESTRUCTIVE: `streamalign starter`
    # reads exactly these rows back out of the committed `.labels.tsv` (emit_labels._LINK_RE,
    # `^file_([^:]+):`) to seed the neighbour's starter file. So a sort over a file carrying a
    # neighbour's label track silently ate the link the starter emitter depends on -- and left
    # the neighbour's labels behind as anonymous rows at ITS timestamps, belonging to no file.
    # Re-attaching makes the round-trip idempotent and keeps the link. After adjust_line(), so
    # its anchored `file (start )?sync:` match still sees the bare label.
    for begin, end, sf in spans:
        for entry in write_lines[begin:end]:
            entry[2] = "file_%s: %s" % (sf, entry[2])
    return write_lines


# Print the keyword errors -- labels that tried to be a keyword and failed. Free text is never
# in here; it is auto-noted and summarised by report_auto_notes(). Returns the error count.
def report_keyword_errors():
    if not keyword_errors:
        return 0
    sys.stderr.write("ERROR: %d label(s) look like a keyword but do not parse:\n"
                     % len(keyword_errors))
    for lineno, label, reason in keyword_errors:
        sys.stderr.write(f"  line {lineno}: {label!r} -- {reason}\n")
    return len(keyword_errors)


# Summarise the free-text labels that were auto-noted, so they can be eyeballed without
# having to prefix every one of them with `note:` by hand.
def report_auto_notes():
    if not auto_notes:
        return
    sys.stderr.write("\nAuto-noted %d free-text label(s) (no `note:` needed):\n" % len(auto_notes))
    for lineno, label in auto_notes:
        sys.stderr.write(f"  line {lineno}: {label}\n")

## MAIN ##

def main():
    global sort_lines, debug, filename, secondaryfile, primary_stem

    # Initialize the argument parser
    parser = argparse.ArgumentParser(description='Process and adjust input data.')
    parser.add_argument('filename', nargs='?', help='Input filename (optional)')
    parser.add_argument('--adjust', action='store_true', help='Subtract timestamp of last "file start" entry from all entries')
    parser.add_argument('--test',   action='store_true', help='Test mode -- do not write or move files, just show notes')
    parser.add_argument('--live',   action='store_true', help='Read labels from currently loaded file(s) in Audacity, but do not write')
    parser.add_argument('--stem',   help='Primary file stem for LABELTRACK scoping (defaults to the input filename; needed for stdin)')
    parser.add_argument('--debug',  action='store_true', help='Print debug information')

    # Parse the command-line arguments
    args = parser.parse_args()

    # Check if the '--adjust' flag is provided
    do_adjustment = args.adjust

    # Check for testing-only, live mode, debug
    test_mode = args.test
    live_mode = args.live
    debug = args.debug

    # Get the filename from the command-line arguments
    filename = args.filename

    # Primary stem scopes LABELTRACK names: --stem overrides, else derive from the filename
    # (None for stdin without --stem, in which case capture-stem tracks route as secondaries).
    primary_stem = _stem(args.stem) if args.stem else (_stem(filename) if filename else None)

    if live_mode:
        # If we are in live mode, get lines from Audacity
        read_labels_audacity()
    else:
        # Get lines from filename or stdin
        read_labels_filepipe(test_mode)

    # Fail early -- before writing anything -- on a missing LABELTRACK marker or a mistyped
    # keyword. The auto-note summary prints either way: it is information, not a fault.
    errors = report_missing() + report_keyword_errors()
    report_auto_notes()
    if errors:
        sys.exit(f"\nExiting: fix the {errors} error(s) above (nothing was written).")

    # Sort primary + secondary entries into final write order (with optional adjustment)
    write_lines = assemble_write_lines(do_adjustment)

    # Write the sorted lines and file start lines to the appropriate output
    if not test_mode and not live_mode:
        read_lines = []
        with contextlib.ExitStack() as stack:
            try:
                output = stack.enter_context(open(filename, 'w')) if filename else sys.stdout
            except Exception as inst:
                print(f"Unable to open {filename}: {inst}")
                sys.exit('Exiting.')
            for entry in write_lines:
                output.write("{:.6f}\t{:.6f}\t{}\n".format(*entry))

    # If we're in live mode, compare our entries to the ones written -- they
    # have 3 fewer significant digits, so we don't want to write them
    if live_mode:
        sys.stderr.write(f"Comparing live entries (less precise) with {filename} from File > Export > Export Labels\n")
        wi = 0
        diffs = 0
        with open(filename, 'r') as input_file:
            for line in input_file:
                line = line.strip()
                parts = line.split('\t')
                if (floatcmp(parts[0],write_lines[wi][0]) != 0):
                    sys.stderr.write(f"{wi:02d}: t1: {write_lines[wi]} != {line}\n")
                    diffs += 1
                elif (floatcmp(parts[1],write_lines[wi][1]) != 0):
                    sys.stderr.write(f"{wi:02d}: t2: {write_lines[wi]} != {line}\n")
                    diffs += 1
                elif(parts[2] != write_lines[wi][2]):
                    sys.stderr.write(f"{wi:02d}:  l: {write_lines[wi]} != {line}\n")
                    diffs += 1
                #else:
                #    print(f"{wi:02d}: {write_lines[wi]} == {line}")
                wi += 1
        sys.stderr.write(f"Comparison Result: {diffs} differences found.\n")

    sys.stderr.write("Processing complete.\n")

if __name__ == '__main__':
    main()
