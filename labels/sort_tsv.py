#!/usr/bin/python3

import shutil
import re
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

# Define regular expressions for matching keywords
keyword_patterns = [
    r"(start(\d+):\s*)?ID(\d+)?:\s*(.+)",
    r"file (start )?sync: (.+):? ([0-9.]+)",
    r"((track)(\d+)?|(orig)(\d+))\s+sync:\s+(.)(.*)",
    r"orig(\d+)\s+(start|end|note):\s+(.*)",
    r"(file|mix) (start|end|note): (.*)",
    r"note(\s\S+?)?: (.*)",
]

# Compile regular expressions
keyword_regexes = [re.compile(pattern) for pattern in keyword_patterns]

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
        secondfile = match.group(1)
        label = match.group(2)
        if secondfile not in secondfiles:
            secondfiles[secondfile] = []
        secondfiles[secondfile].append((parts[0],parts[1],label,parts[3]))
        return

    matched_keyword = None
    # Check for keywords in the text portion of the line
    for pattern in keyword_regexes:
        match = pattern.match(parts[2])
        if match:
            matched_keyword = match.group(0)
            break

    if not matched_keyword:
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

# Function to turn line strings into tracklist entries and process them with process_entry
def process_line(line):
    global line_number
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

    process_entry(parts)

# adjust timestamps if appropriate
def adjust_line(entry):
    global adjust_value
    for i in range(2):
        entry[i] -= adjust_value
    if match := re.match(r"file (start )?sync: (.+):? ([0-9.]+)(.*)", entry[2]):
        # adjust sync time too
        try:
            syncfloat = float(match.group(3))
            syncfloat += adjust_value
        except:
            sys.stderr.write(f"ERROR in adjust_line: Unable to make {match.group(3)} into a float: {entry}\n")
            syncfloat = match.group(3)

        newlabel = f"file {match.group(1)}sync: {match.group(2)} {syncfloat}"
        if (len(match.groups()) > 3):
            newlabel += match.group(4)
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

## MAIN ##

def main():
    global sort_lines, debug, filename, secondaryfile

    write_lines = []        # entries ready for writing

    # Initialize the argument parser
    parser = argparse.ArgumentParser(description='Process and adjust input data.')
    parser.add_argument('filename', nargs='?', help='Input filename (optional)')
    parser.add_argument('--adjust', action='store_true', help='Subtract timestamp of last "file start" entry from all entries')
    parser.add_argument('--test',   action='store_true', help='Test mode -- do not write or move files, just show notes')
    parser.add_argument('--live',   action='store_true', help='Read labels from currently loaded file(s) in Audacity, but do not write')
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

    if live_mode:
        # If we are in live mode, get lines from Audacity
        read_labels_audacity()
    else:
        # Get lines from filename or stdin
        read_labels_filepipe(test_mode)

    sort_lines.sort(key=tracksort)

    # now copy entries into write_lines, and process any secondary files similarly
    write_lines.extend(sort_lines)
    sort_lines=[]
    for sf in secondfiles:
        sys.stderr.write(f"---\nProcessing secondary entries for file {sf}\n")
        secondaryfile = sf
        for entry in secondfiles[sf]:
            process_entry(entry)
        sort_lines.sort(key=tracksort)
        write_lines.extend(sort_lines)
        sort_lines=[]

    # adjust all lines we are writing
    if do_adjustment:
        write_lines = map(adjust_line, write_lines)

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
