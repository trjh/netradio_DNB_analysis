#!/usr/bin/python3

import shutil
import re
import sys
import argparse
import contextlib

# Initialize the argument parser
parser = argparse.ArgumentParser(description='Process and adjust input data.')
parser.add_argument('filename', nargs='?', help='Input filename (optional)')
parser.add_argument('--adjust', action='store_true', help='Subtract timestamp of last "file start" entry from all entries')
parser.add_argument('--test',   action='store_true', help='Test mode -- do not write or move files, just show notes')

# Parse the command-line arguments
args = parser.parse_args()

# Check if the '--adjust' flag is provided
do_adjustment = args.adjust
adjust_value = -1

# Check for testing-only
test_mode = args.test

# Get the filename from the command-line arguments
filename = args.filename

# Create a backup of the original file if a filename is provided
if filename and not test_mode:
    # If we have a .txt extension, move it to .tsv
    if filename.endswith("txt"):
        moveresult = shutil.move(filename, filename[:-3] + "tsv")
        sys.stderr.write(f"Moved {filename} to {moveresult}\n")
        filename=filename[:-3] + "tsv"

    backup_filename = filename + ".bak"
    shutil.copy(filename, backup_filename)

# Initialize lists to store lines to be sorted and unrecognized lines
sort_lines = []         # stores entries of the form (timestamp1, timestamp2, label, line number)
                        # in preparation for sorting/writing
write_lines = []        # entries ready for writing
secondfiles = dict()    # entries for additional wav files, key is wav filename, value is list of timestamp/label entries
line_number = 0

# Define regular expressions for matching keywords
keyword_patterns = [
    r"(start(\d+):\s*)?ID(\d+)?:\s*(.+)",
    r"file (start)? sync: (.+):? ([0-9.]+)",
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
        sys.stderr.write(f"Warning: Unrecognized keywords ({parts[2]}) at line {parts[3]} ts {parts[0]}\n")

    if re.match(r"file start", parts[2]):
        if (adjust_value < 0):
            adjust_value = parts[0]
            sys.stderr.write(f".FIRST: adj({adjust_value}) :: {parts[2]}\n")
        else:
            sys.stderr.write(f".Not using adjust {parts[0]} as adjust_value already set ({adjust_value})\n")

    sort_lines.append((parts))

# Function to turn line strings into tracklist entries and process them with process_entry
def process_line(line):
    global line_number
    line_number += 1
    parts = line.strip().split('\t')
    parts.append(line_number)

    # line validation checks
    if (len(parts) < 3):
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
    return entry

## MAIN ##

# Process input based on whether a filename is provided or not
sys.stderr.write("Processing primary entries\n")
with contextlib.ExitStack() as stack:
    input = stack.enter_context(open(filename, 'r')) if filename else sys.stdin
    for line in input:
        process_line(line)

sort_lines.sort(key=tracksort)

# now copy entries into write_lines, and process any secondary files similarly
write_lines.extend(sort_lines)
sort_lines=[]
for sf in secondfiles:
    sys.stderr.write(f"Processing secondary entries for file {sf}\n")
    for entry in secondfiles[sf]:
        process_entry(entry)
    sort_lines.sort(key=tracksort)
    write_lines.extend(sort_lines)
    sort_lines=[]

# adjust all lines we are writing
if do_adjustment:
    write_lines = map(adjust_line, write_lines)

# Write the sorted lines and file start lines to the appropriate output
if not test_mode:
    with contextlib.ExitStack() as stack:
        output = stack.enter_context(open(filename, 'w')) if filename else sys.stdout
        for entry in write_lines:
            output.write("{:.6f}\t{:.6f}\t{}\n".format(*entry))

sys.stderr.write("Processing complete.\n")
