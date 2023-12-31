#!python3

import os
import argparse
import sys
import json
import re
import time
import pprint
import pyaudacity as pa
import subprocess
import tempfile
from pydub import AudioSegment
from pydub.silence import detect_silence
import numpy as np


outline = """
    - create a label track if necessary
    - label based on track lengths
    - export in wav 32-bit based on labels
    - rename files and tag based on labels

    $0 labelfile
"""

debug = False                           # debug level
debug_dump = False                      # dump data structures after or as read

aud_tracks = None                       # list of audacity tracks
aud_clips = None                        # list of audacity audio clips
aud_labels = None                       # list of audacity labels
audacitytest = False                    # audacity debug
samplerate = None                       # sample rate of the audio track

args = None                             # share command-line argument globally

audio_track = None
label_track = None                      # audio and label track we operate on.
                                        # globals to make it easer to add labels from subroutines.

# Define your silence parameters
min_silence_length = 250   # in milliseconds
silence_find_window = 10   # in seconds -- window is actually 2x this figure.
                           # look this far before and after timestamp given to find_silence for a silence span


metadataformats = [r"timestamp=(.+?), trackName=(.+?), artistName=(.+?), albumName=(.+?), trackNumber=(\d+), "
                   + r"albumTrackCount=(\d+), genre=(.+?), year=(\d+), trackDuration=([\d.]+), playerPosition=([\d.]+), "
                   + r"albumartist=(.+?), composer=(.*?), disccount=(\d+), discnumber=(\d+), compilation=(true|false)",
                   r"timestamp=(.+?), trackName=(.+?), artistName=(.+?), albumName=(.+?), trackNumber=(\d+), "
                   + r"albumTrackCount=(\d+), genre=(.+?), year=(\d+), trackDuration=([\d.]+), playerPosition=([\d.]+)"]


def dprint(string):
    global debug
    if debug:
        print(string)

def audcommand(string):
    global audacitytest

    if audacitytest and (('AddLabel' in string) or ('SetLabel' in string)):
        print(f"TESTMODE - would send <<{string}>> to Audacity")
        return
    return pa.do(string)

# initialize audacity data
def audinit():
    global aud_tracks, aud_clips, samplerate, aud_labels

    # Pull Track info if we don't have them already
    # probably not needed but good as an init test
    if not aud_tracks:
        aud_tracks = pa.get_info(info_type='Tracks', format="JSONlist")
        dprint(aud_tracks)
        # format of response
        # [{dict about track}, ...]

    if not aud_clips:
        aud_clips = pa.get_info(info_type='Clips', format="JSONlist")

    if not samplerate:
        prefs = pa.get_info(info_type='Preferences', format="JSONlist")
        for p in prefs:
            if p['id'] == '/SamplingRate/DefaultProjectSampleRate':
                samplerate = int(p['default'])
                dprint(f".found samplerate = {samplerate}")
                break

    if not samplerate:
        sys.exit("Could not find samplerate in GetInfo: Type=Preferences")

    if not aud_labels:
        aud_labels = pa.get_info(info_type='Labels', format="JSONlist")


# given appropriate details, set a label on a given track
def setlabel(LabelTrack,ts,label,endts=None):
    global samplerate, debug_dump, aud_labels

    setts = False   # by default, don't set the timestamp when setting label text, but do if we found it at ts=0

    # remove quotes and other oddities from label
    label = re.sub(r'["]', '_', label)

    dprint(f".setlabel({LabelTrack}, {ts}, {label}, endts={endts})")
    if (LabelTrack is None) or (ts is None) or (label is None):
        sys.exit(f"invalid arguments to setlabel({LabelTrack}, {ts}, {label}, endts={endts})")

    ts = [ts, endts if endts else ts]
    note = ""

    # Adjust the timestamps
    for i in [0,1]:
        # round down ts to 8 digits to avoid python math wierdness
        dprint(f".ts[{i}] fractions of a second in samples: {(ts[i] - int(ts[i]))*samplerate}")
        ts[i]=round(ts[i],8)

        # check precision of the ts
        pass_ts=round(ts[i],3)
        if (ts[i] != pass_ts):
            # add sample offset to label -- format is HH:MM:SS + samples
            min = int(ts[i]/60)
            sec = int(ts[i])%60
            samples = int((ts[i] - int(ts[i])) * samplerate)
            if note and (ts[0] != pass_ts):
                dprint(f".ADDING TO NOTE AS ts0 ({ts[0]}) != ts1/pass_ts ({pass_ts})")
                note += "-"
            if not note or (ts[0] != pass_ts):
                note += f"({min:02d}:{sec:02d} + {samples} samples)"
            dprint(f".NOTE: adding {note} to label to note correct label placement\n")
        ts[i] = pass_ts

    label += " " + note

    audcommand(f'Select: Track={LabelTrack} Start={ts[0]} End={ts[1]}')
    audcommand("SetTrackStatus: Focused=1")
    audcommand("AddLabel:")

    # now, stupidly, we need to find the # of the label we just added
    aud_labels = pa.get_info(info_type='Labels', format="JSONlist")
        # [labeltrack, ...]
        # where each label track is [tracknumber, [[ts1, ts2, label], ...]
    labelnum = 0
    newlabelnum = None
    justblank = []
    for lt in aud_labels:
        if debug_dump: print(f"label track {lt[0]}")
        for l in lt[1]:
            if debug_dump: print(f"label{labelnum}: {l}")
            if l[2] == '' and l[0] == ts[0] and l[1] == ts[1]:
                # this is our label number
                newlabelnum = labelnum
                break
            elif l[2] == '':
                dprint(f".Blank label# {labelnum} start={l[0]} end={l[1]}")
                justblank.append(labelnum)
            labelnum += 1
    if (newlabelnum is None) and not audacitytest:
        dprint(f".Could not find the empty label we just created at {ts[0]}-{ts[1]} in track {LabelTrack}, "
                         + "trying at ts=0")
        setts = True
        labelnum = 0
        for lt in aud_labels:
            for l in lt[1]:
                if l[2] == '' and l[0] == 0 and l[1] == 0:
                    # this is our label number
                    newlabelnum = labelnum
                    break
                labelnum += 1
        if not newlabelnum:
            dprint(f".Could not find the empty label we just created in track {LabelTrack} "
                             + f"at ts=0 or ts={ts[0]} but we found these blank labels {justblank}\n")
            if len(justblank) == 1:
                dprint(f".Using only blank, label# {justblank[0]}")
                newlabelnum = justblank[0]
            elif len(justblank) == 0:
                sys.exit(f"Could not find any blank labels.")
            else:
                print("Please chose a label number from above list: ",end='')
                choice = int(input().lower())
                if choice in justblank:
                    print("Using label# {choice}")
                    newlabelnum = choice
                else:
                    sys.exit(f"Choice {choice} not a valid label number, exiting.")

    # finally, set the label description
    command = f'SetLabel: Label={newlabelnum} Text="{label}"'
    if setts:
        command += f' Start={ts[0]} End={ts[1]}'
    audcommand(command)


# Run a subprocess and catch errors
def runcommand(args):
    try:
        response = subprocess.run(args)
    except FileNotFoundError:
        sys.exit(f'command <<{args[0]}>> not found\n')
    except subprocess.CalledProcessError as e:
        sys.exit(f'Error occurred running {args}: {e}\n')
    if response.returncode != 0:
        sys.exit(f'Error occurred running {args}\n')


# read metadata from file with given filename, return list of dicts containing
# unique metadata
def parse_metadatafile(filename):
    global metadataformats

    metadataformat = None
    metadata_re = None

    returnlist = []
    lastentry = None

    try:
        input_file = open(filename, 'r', encoding="latin-1")
    except Exception as inst:
        print(f"Unable to open {filename}: {inst}")
        sys.exit('Exiting.')

    # read until we know which metadata format to use
    for line in input_file:
        if "timestamp=" in line:
            for f in metadataformats:
                if re.match(f, line, flags=re.IGNORECASE):
                    metadataformat = f
                    metadata_re = re.compile(metadataformat, flags=re.IGNORECASE)
                    input_file.seek(0)  # rewind to beginning
                    dprint(f"Working metadata format: {f}\nexample: {line}\n")
                    break
            # END for f in metadataformats:
            if metadataformat:
                break
            else:
                print(f"WARNING: {line} did not match a metadata format")
        # END if "trackName=" in line:
    # END for line in input_file:
    if not metadataformat:
        sys.exit("EXIT: unable to find working metadata format")

    linenum = 0
    for line in input_file:
        linenum += 1
        line = line.strip()
        entry = None
        #            1                2                 3                4                  5
        # timestamp=(.+*), trackName=(.+?), artistName=(.+?), albumName=(.+?), trackNumber=(\d+),
        #                  6            7           8                    9                        10
        # albumTrackCount=(\d+), genre=(.+?), year=(\d+), trackDuration=([\d.]+), playerPosition=([\d.]+)"
        #               11              12               13                14                 15
        # r"albumartist=(.+?), composer=(.+?), disccount=(\d+), discnumber=(\d+), compilation=(true|false)",
        if match := metadata_re.match(line):
            entry = {"track": match.group(2), "artist": match.group(3), "album": match.group(4),
                     "num": match.group(5)+"/"+match.group(6), "genre": match.group(7), "year": match.group(8),
                     "duration": match.group(9)}
            if len(match.groups()) > 10:
                entry.update({"albumartist": match.group(11), "composer": match.group(12), "disc": match.group(14)+"/"+match.group(13),
                              "compilation": (1 if match.group(15)=="true" else 0)})
            if lastentry:
                lastentry_match = True
                for k in lastentry:
                    # do not use duration, as oddly it can change?!
                    if k == 'duration': continue
                    if lastentry[k] != entry[k]:
                        lastentry_match = False
                        break
                if not lastentry_match:
                    # new entry, add it to our data
                    returnlist.append(entry)
                    dprint(f"New entry line {linenum}: {line}\n")
                    if lastentry['track'] == entry['track']:
                        print("WARNING: second entry with identical track name, "
                              + "use --metatest to debug if this is not expected.")
                    lastentry = entry
            else:
                # no last entry
                returnlist.append(entry)
                lastentry = entry
        else:
            # line did not match regex
            sys.stderr.write(f"Unexpected input line {linenum}: {line}\n")

    # end for line in input_file
    input_file.close()
    return(returnlist)


# like pydub detect_silence, but look for absolute zero signal only
def detect_fullsilence(audio_segment, min_silence_len=1000):
    """
    Returns a list of all silent sections [start, end] in milliseconds of audio.

    audio_segment - the segment to find silence in
    min_silence_len - the minimum length for any silent section
    """
    # Get the samples as a NumPy array
    samples = np.array(audio_segment.get_array_of_samples())

    # Convert stereo to mono by averaging the channels if necessary
    if audio_segment.channels == 2:
        samples = samples.reshape((-1, 2))
        samples = samples.mean(axis=1)

    # Find indices where the sample value is zero
    zero_indices = np.where(samples == 0)[0]

    # Compute minimum silence length in samples
    min_silence_samples = int(min_silence_len * audio_segment.frame_rate / 1000)

    # Find consecutive ranges of zeroes
    ranges = []
    start_index = None
    end_index = None
    # pad zero_indices with an ending 0 so that the ranges.append will trigger
    # even at the end
    np.append(zero_indices, 0)
    for i in zero_indices:
        if start_index is None:
            start_index = end_index = i
        elif i != end_index+1:
            if ((start_index != end_index) and
                (end_index - start_index >= min_silence_samples)):
                # THEN...
                ranges.append((start_index, end_index))
                print(f"update: {ranges=}")
            start_index = end_index = i
        else:
            end_index = i

    # Convert sample indices to time (milliseconds)
    print(ranges)
    silence_ranges = [(r[0] * 1000 / audio_segment.frame_rate, r[1] * 1000 / audio_segment.frame_rate) for r in ranges]

    return silence_ranges

# find a silence around timestamp, return start and end of biggest silence in
# 10 second window around timstamp
# if 'start' is set, don't let search start before it
def find_silence(audio_track, timestamp, start=None):
    global min_silence_length, args, label_track, debug
    silence_start = silence_end = timestamp

    # create a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        temp_filename = tmp_file.name

    # export the (silence_find_window) sec before & after timestamp to temp file
    audio_start = timestamp - silence_find_window
    if start and (audio_start < start):
        audio_start = start
    audio_end   = timestamp + silence_find_window
    pa.do(f"Select: Track={audio_track} Start={audio_start} End={audio_end}")
    pa.export(temp_filename + ".wav", num_channels=2)
    dprint(f"Exported from {audio_start} to {audio_end} into {temp_filename}")

    if debug:
        setlabel(label_track,audio_start,"SILENCE SEARCH",endts=audio_end)

    # import temp file, then remove it
    audio = AudioSegment.from_file(temp_filename + ".wav")
    dprint(f"Imported AudioSegment {temp_filename}")
    os.unlink(temp_filename)
    os.unlink(temp_filename + ".wav")

    # use pydub.detect_silence to find silence windows
    if args.fullsilence:
        silence_ranges = detect_fullsilence(audio, min_silence_len=min_silence_length)
    else:
        silence_ranges = detect_silence(audio, min_silence_len=min_silence_length,
                                        silence_thresh=args.silence_threshold)
    dprint(f"detect silence(audio, min={min_silence_length}, thresh={args.silence_threshold}\n"
            + f"...output: {silence_ranges}")

    if silence_ranges:
        # Find the longest silence
        longest_silence = max(silence_ranges, key=lambda x: x[1] - x[0])

        # Calculate the duration of the longest silence
        longest_silence_duration = longest_silence[1] - longest_silence[0]  # Duration in milliseconds
        dprint(f"Found {len(silence_ranges)} silence ranges, longest is {longest_silence_duration}ms "
              +f"starting at {longest_silence[0]/1000}")

        # return offsets
        silence_start = timestamp - silence_find_window + longest_silence[0]/1000
        silence_end   = timestamp - silence_find_window + longest_silence[1]/1000

        if debug:
            setlabel(label_track,silence_start,f"SILENCE longest of {len(silence_ranges)}",endts=silence_end)

    else:
        dprint("No silences found.")

    return (silence_start, silence_end)


# slightly following the example of vinyl2digital, use the labels to select ranges in audio, then use Export2 to export them
def export_by_label(audio_track, metadata, interactive=False):
    global aud_labels, args

    cwd = os.getcwd()
    print(f"Storing files in {cwd}")

    # get latest labels
    aud_labels = pa.get_info(info_type='Labels', format="JSONlist")

    labelnum = 0
    for lt in aud_labels:
        for l in lt[1]:
            if args.limit:
                if labelnum > args.limit:
                    print(f"Exiting after {args.limit} labels/exports as requested.")
                    break

            l_metadata = metadata[labelnum]
            if l_metadata['track'] not in l[2]:
                print(f"mismatch between label and metadata:\n"+
                      f"label: {l[2]:20s} {l}\nmetadata: {l_metadata['track']:20s} {l_metadata}")
                print("Continue? [Yn] ", end='')
                choice = input().lower()
                if 'n' in choice:
                    sys.exit("Exiting as requested")

            filename = f"{labelnum+1:02d} - {l_metadata['artist']} - {l_metadata['track']}"
            # remove samples annotations, bad-for-unix chars, and lead/trail whitespace
            filename = re.sub(r'\(\d+:\d+ \+ \d+ samples\)-?', '', filename)
            filename = re.sub(r'[<>:"\/\\\|\?\*]', '_', filename)

            # Check for existing wav/wavepack
            doexport = True
            if os.path.exists(filename + ".wav"):
                if args.overwrite:
                    print(f"--- Overwrite mode, removing {filename}.wav")
                    try:
                        os.remove(filename + ".wav")
                    except Exception as e:
                        sys.exit("Unable to remove {filename}.wav: {e}")
                else:
                    print(f"--- Existing wav file, will not recreate {filename}.wav")
                    doexport = False

            if os.path.exists(filename + ".wv"):
                if args.overwrite:
                    print(f"--- Overwrite mode, removing {filename}.wv")
                    try:
                        os.remove(filename + ".wv")
                    except Exception as e:
                        sys.exit("Unable to remove {filename}.wv: {e}")
                else:
                    print(f"--- Existing wavepack file {filename}.wv, skipping to next track")
                    labelnum += 1
                    continue

            # Export the track
            if doexport:
                print(f"--- Exporting #{labelnum} [{l[2]}] as {filename}.wav")
                pa.do(f"Select: Track={audio_track} Start={l[0]} End={l[1]}")
                fqfilename = cwd + "/" + filename + ".wav"
                pa.export(fqfilename, num_channels=2)

            # we're here, invoke wavpack
            print(f"--- Compacting {filename}.wav with wavpack")
            runcommand(["wavpack", filename + ".wav"])

            # ok, that went well, now wvtag
            # https://wiki.hydrogenaud.io/index.php?title=APE_key
            # should have had mixed case but we'll continue as we started
            print(f"--- Adding tags to {filename}.wv with wvtag")
            wvtagcall = ["wvtag"]
            for tpair in (['TITLE', 'track'], ['ARTIST', 'artist'], ['ALBUM', 'album'], ['DATE', 'year'], ['TRACKNUMBER', 'num'],
                          ['GENRE', 'genre'], ['ALBUM ARTIST', 'albumartist'], ['COMPOSER', 'composer'], ['DISC', 'disc'],
                          ['COMPILATION', 'compilation']):
                if tpair[1] in l_metadata:
                    wvtagcall.extend(["-w", tpair[0]+"="+str(l_metadata[tpair[1]])])
            wvtagcall.append(filename + ".wv")
            print(wvtagcall)
            runcommand(wvtagcall)

            # interactive check if desired
            if interactive:
                print("Check result. [yN]", end='')
                choice = input().lower()
                if not 'y' in choice:
                    sys.ext("Exiting")

            # remove wav file
            print(f"--- Removing {filename}.wav")
            try:
                os.remove(filename + ".wav")
            except Exception as e:
                sys.exit("Unable to remove {filename}.wav: {e}")

            # increment label number
            labelnum += 1


def main():
    global args, audacitytest, debug, debug_dump, aud_clips, aud_tracks, aud_labels
    global audio_track, label_track

    # Read arguments
    metadatafile = args.metadata
    debug = args.debug
    debug_dump = args.dump
    audacitytest = args.audacitytest
    exec_start = time.time()
    pp = pprint.PrettyPrinter(indent=4)

    dprint("DEBUG mode on")

    # if we are just testing metadata file, do that and exit
    if args.metatest:
        debug = True
        metadata = parse_metadatafile(metadatafile)
        dprint(f".metadata entries: {len(metadata)}")
        print("dump> metadata")
        pp.pprint(metadata)
        sys.exit('metadata test complete')

    # Initialize Audacity
    print("Initializing Audacity")
    audinit()
    dprint(f".tracks: {len(aud_tracks)}")
    if debug_dump:
        print("dump> aud_tracks")
        pp.pprint(aud_tracks)
        print("dump> aud_clips")
        pp.pprint(aud_clips)

    # Read metadata file
    metadata = parse_metadatafile(metadatafile)
    dprint(f".metadata entries: {len(metadata)}")
    if debug_dump:
        print("dump> metadata")
        pp.pprint(metadata)

    # Find audio and label track
    audio_start = None
    audio_end   = None
    for at_i in range(len(aud_tracks)):
        if (audio_track == None) and (aud_tracks[at_i]['kind'] == 'wave'):
            audio_track = at_i
            audio_start = aud_tracks[at_i]['start']
            audio_end   = aud_tracks[at_i]['end']
        elif (label_track == None) and (aud_tracks[at_i]['kind'] == 'label'):
            label_track = at_i
        elif (aud_tracks[at_i]['kind'] == 'label'):
            print("WARNING: more than one label track\n")
        else:
            dprint(f".t[{at_i}][kind]: {aud_tracks[at_i]['kind']}")
    if audio_track == None:
        sys.exit("Unable to find audio track in Audacity")
    else:
        dprint(f".audio track found at entry {audio_track}")

    if audio_start > 0:
        print("WARNING: audio does not start at zero ({audio_start}), results may be unexpected")

    # Create label track if one does not exist
    if label_track != None:
        dprint(f".label track found at entry {label_track}")
    else:
        print("Creating new label track.")
        audcommand("NewLabelTrack")
        # now we have to get the track list again
        aud_tracks = None
        audinit()
        for at_i in range(len(aud_tracks)):
            if aud_tracks[at_i]['kind'] == 'label':
                label_track = at_i
                break
        if label_track == None:
            sys.exit("Unable to find newly created label track in Audacity")
        else:
            dprint(f".label track found at entry {audio_track}")

    # Create label for each entry in metadata - if necessary
    total_labels = sum(len(track_info[1]) for track_info in aud_labels)
    create_labels = True

    if total_labels > 0:
        print(f"There are already {total_labels} labels created.  Add {len(metadata)} labels from {metadatafile}? [yN]", end='')
        choice = input().lower()
        if not 'y' in choice:
            create_labels = False

    if create_labels:
        start = args.start
        count = 0
        for track in metadata:
            if (start > audio_end):
                print(f"Track '{track['track']}' starts after end of recorded audio, skipping it and all remaining.")
                break
            if args.limit:
                if count > args.limit:
                    print(f"Exiting after {args.limit} labels as requested.")
                    break

            try:
                duration = float(track['duration'])
            except:
                sys.exit(f"Issue with setting duration to float on entry {track}")

            end = start + duration
            (new_end, next_start) = find_silence(audio_track, end, start=start)
            print(f"...silence {new_end} - {end} - {next_start}")
            if new_end > start:
                end = new_end
            printt = "'" + track['track'] + "'"
            print(f"Label: {printt:40s} len: {duration:7.3f} start: {start:7.3f} end: {end:7.3f}")
            setlabel(label_track,start,track['track'],endts=end)

            if next_start >= end:
                start = next_start
            else:
                start = end
            count += 1

    # Verify everything looks ok with user
    print("Please check the labels in Audacity, adjust if necessary,\nand enter y to continue. [yN]", end='')
    choice = input().lower()
    if not 'y' in choice:
        sys.exit("No 'y' in input, exiting.")

    # Export wav files, pack them with wavpack, label wavpack file, remove wav file
    export_by_label(audio_track, metadata, interactive=args.interactive)

    print("Work complete.")
    exit()

if __name__ == "__main__":
    description = """
    Given a metadata file, add labels marking individual tracks to the
    currently open Audacity file, then export those tracks in 32-bit float wav
    format, convert the wav files to wavepack, and add metadata from the
    metadata file.
    """
    epilog = """
    The labelfile is expected to be in the following format:
        timestamp=2023-November-7 2:8:51, trackName=Apple Tree Farm,
        artistName=AppleJack, albumName=Tree Farm Tapes (2014), trackNumber=8,
        albumTrackCount=9, genre=Spoken, year=2014,
        trackDuration=355.013, playerPosition=12.192
    """
    parser = argparse.ArgumentParser(description=description, epilog=epilog)
    parser.add_argument('metadata', help='Metadata file')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug text')
    parser.add_argument('--dump', action='store_true', help='Dump data structures as they are read/created')
    parser.add_argument('--audacitytest', action='store_true', help='Print Audacity commands, but do not send them')
    parser.add_argument('-i', '--interactive', action='store_true', help='Pause after creating each wavpack file')
    parser.add_argument('--metatest', action='store_true', help='Check input file for parseability, then exit')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing wav/wv files')
    parser.add_argument('--silence_threshold', type=int, default=-50,
                        help='Threshold for silence, in decibels (default: -50dB)')
    parser.add_argument('--limit', type=int, default=None, help='If set, only create this many labels')
    parser.add_argument('--start', type=float, default=0, help='If set, start labels at this timestamp')
    parser.add_argument('--fullsilence', action='store_true', help='Compute silence as zero signal only')

    args = parser.parse_args()
    main()
