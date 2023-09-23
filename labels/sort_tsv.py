#!/usr/bin/python3

import shutil
import re
import sys

# Check if a filename is provided as a command-line argument
if len(sys.argv) > 1:
    filename = sys.argv[1]
else:
    filename = None

# Create a backup of the original file if a filename is provided
if filename:
    backup_filename = filename + ".bak"
    shutil.copy(filename, backup_filename)

# Initialize lists to store lines to be sorted and unrecognized lines
file_start_lines = []
sort_lines = []
line_number = 0

# Define regular expressions for matching keywords
keyword_patterns = [
    r"file (start)? sync: (.+):? ([0-9.]+)",
    r"file (start|end): (.+)",
    r"start(\d+):\s*ID:\s*(.+)",
    r"track\s+sync:\s+(.)(.*)",
    r"orig(\d+)\s+(sync|start|end):\s+(.)(.*)",
    r"(file )?note: (.*)",
]

# Compile regular expressions
keyword_regexes = [re.compile(pattern) for pattern in keyword_patterns]

# Function to process lines
def process_line(line):
    global line_number
    line_number += 1
    parts = line.strip().split('\t')

    if re.match(r"file start", parts[2]):
        print(f"FIRST: {line.strip()}")
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
        print(f"Warning: Unrecognized line - {line.strip()}")


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
sort_lines.sort(key=lambda x: x[0])

# Write the sorted lines and file start lines to the appropriate output
if filename:
    with open(filename, 'w') as output_file:
        for line in file_start_lines:
            output_file.write(line)
        for _, line in sort_lines:
            output_file.write(line)
else:
    # Write to stdout
    for line in file_start_lines:
        sys.stdout.write(line)
    for _, line in sort_lines:
        sys.stdout.write(line)

# Remove the backup file if it was created
#if filename:
#   shutil.rmtree(backup_filename)

print("Processing complete.")
