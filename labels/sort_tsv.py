#!/usr/bin/python3

import shutil
import re
import sys
import argparse

# Initialize the argument parser
parser = argparse.ArgumentParser(description='Process and adjust input data.')
parser.add_argument('filename', nargs='?', help='Input filename (optional)')
parser.add_argument('--adjust', action='store_true', help='Subtract timestamp of last "file start" entry from all entries')
parser.add_argument('--test',   action='store_true', help='Test mode -- do not write or move files, just show notes')

# Parse the command-line arguments
args = parser.parse_args()

# Check if the '--adjust' flag is provided
do_adjustment = args.adjust
adjust_value = 0

# Check for testing-only
test_mode = args.test

# Get the filename from the command-line arguments
filename = args.filename

# Create a backup of the original file if a filename is provided
if filename and not test_mode:
    # If we have a .txt extension, move it to .tsv
    if filename.endswith("txt"):
        moveresult = shutil.move(filename, filename[:-3] + "tsv")
        print(f"Moved {filename} to {moveresult}")
        filename=filename[:-3] + "tsv"

    backup_filename = filename + ".bak"
    shutil.copy(filename, backup_filename)

# Initialize lists to store lines to be sorted and unrecognized lines
file_start_lines = []
sort_lines = []
line_number = 0

# Define regular expressions for matching keywords
keyword_patterns = [
    r"(start(\d+):\s*)?ID(\d+)?:\s*(.+)",
    r"file (start)? sync: (.+):? ([0-9.]+)",
    r"((track)(\d+)?|(orig)(\d+))\s+sync:\s+(.)(.*)",
    r"orig(\d+)\s+(start|end|note):\s+(.*)",
    r"(file|mix) (start|end|note): (.*)",
    r"note: (.*)",
]

# Compile regular expressions
keyword_regexes = [re.compile(pattern) for pattern in keyword_patterns]

# Function to process lines
def process_line(line):
    global line_number
    global adjust_value
    line_number += 1
    parts = line.strip().split('\t')
    if (len(parts) < 3):
        print(f"Warning: Unrecognized line - less than 3 fields - {line.strip()}")
        return


    if re.match(r"file start", parts[2]):
        adjust_value = float(parts[0])
        print(f"FIRST: adj({adjust_value}) {line.strip()}")
        file_start_lines.append(line)
        return
        
    matched_keyword = None
    # Check for keywords in the text portion of the line
    for pattern in keyword_regexes:
        match = pattern.match(parts[2])
        if match:
            matched_keyword = match.group(0)
            break

    if not matched_keyword:
        print(f"Warning: Unrecognized keywords ({parts[2]}) at line {line_number} ts {parts[0]}")

    try:
        first_float = float(parts[0])
        sort_lines.append((first_float, line))
    except ValueError:
        print(f"Warning: Unrecognized line - first value not a float - {line.strip()}")


# Process input based on whether a filename is provided or not
if filename:
    with open(filename, 'r') as input_file:
        for line in input_file:
            process_line(line)
else:
    # Read from stdin
    for line in sys.stdin:
        process_line(line)

# Sort the sort_lines by timestamp
sort_lines.sort(key=lambda x: (x[0], x[1]))

# adjust timestamps if appropriate
def adjust_line(line):
    global adjust_value
    parts = line.split('\t')
    for i in range(2):
        parts[i] = float(parts[i])
        parts[i] -= adjust_value
    return f"{parts[0]:.6f}\t{parts[1]:.6f}\t{parts[2]}"


if do_adjustment:
    file_start_lines = list(map(adjust_line, file_start_lines))
    sort_lines = [(number, adjust_line(line)) for number, line in sort_lines]

# Write the sorted lines and file start lines to the appropriate output
if filename and not test_mode:
    with open(filename, 'w') as output_file:
        for line in file_start_lines:
            output_file.write(line)
        for _, line in sort_lines:
            output_file.write(line)
elif not test_mode:
    # Write to stdout
    for line in file_start_lines:
        sys.stdout.write(line)
    for _, line in sort_lines:
        sys.stdout.write(line)

# Remove the backup file if it was created
#if filename:
#   shutil.rmtree(backup_filename)

print("Processing complete.")
