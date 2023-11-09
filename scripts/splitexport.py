import argparse
import sys
import json
import re
import time
import pprint
import pyaudacity as pa

outline = """
    - create a label track if necessary
    - label based on track lengths
    - export in wav 32-bit based on labels
    - rename files and tag based on labels

    $0 labelfile
"""

debug = False                           # debug level

aud_tracks = None                       # list of audacity tracks
aud_clips = None                        # list of audacity audio clips
audacitytest = False                    # audacity debug
samplerate = None                       # sample rate of the audio track

metadataformat = r"timestamp=(.+?), trackName=(.+?), artistName=(.+?), albumName=(.+?), trackNumber=(\d+), albumTrackCount=(\d+), genre=(.+?), year=(\d+), trackDuration=([\d.]+), playerPosition=([\d.]+)"
metadata_re = re.compile(metadataformat)

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
    global aud_tracks, aud_clips, samplerate

    # Pull Track info if we don't have them already
    # probably not needed but good as an init test
    if not aud_tracks:
        jdata = pa.get_info(info_type='Tracks')
        jdata = (jdata [:jdata.rfind(']')+1])
        aud_tracks = json.loads(jdata)
        dprint(aud_tracks)
        # format of response
        # [{dict about track}, ...]

    if not aud_clips:
        jdata = pa.get_info(info_type='Clips')
        jdata = (jdata [:jdata.rfind(']')+1])
        aud_clips = json.loads(jdata)

    if not samplerate:
        jdata = pa.get_info(info_type='Preferences')
        jdata = (jdata [:jdata.rfind(']')+1])
        prefs = json.loads(jdata)
        for p in prefs:
            if p['id'] == '/SamplingRate/DefaultProjectSampleRate':
                samplerate = int(p['default'])
                dprint(f".found samplerate = {samplerate}")
                break

    if not samplerate:
        sys.exit("Could not find samplerate in GetInfo: Type=Preferences")

# given appropriate details, set a label on a given track
def setlabel(LabelTrack,ts,label,endts=None):
    global samplerate

    setts = False   # by default, don't set the timestamp when setting label text, but do if we found it at ts=0

    dprint(f"setlabel({LabelTrack}, {ts}, {label}, endts={endts})")
    if (LabelTrack is None) or (ts is None) or (label is None):
        sys.exit(f"invalid arguments to setlabel({LabelTrack}, {ts}, {label}, endts={endts})")

    ts = [ts, endts if endts else ts]
    note = ""

    # Adjust the timestamps
    for i in [0,1]:
        # round down ts to 8 digits to avoid python math wierdness
        dprint(f"ts[{i}] fractions of a second in samples: {(ts[i] - int(ts[i]))*samplerate}")
        ts[i]=round(ts[i],8)

        # check precision of the ts
        pass_ts=round(ts[i],3)
        if (ts[i] != pass_ts):
            # add sample offset to label -- format is HH:MM:SS + samples
            min = int(ts[i]/60)
            sec = int(ts[i])%60
            samples = int((ts[i] - int(ts[i])) * samplerate)
            if note and (ts[0] != pass_ts):
                dprint(f"ADDING TO NOTE AS ts0 ({ts[0]}) != ts1/pass_ts ({pass_ts})")
                note += "-"
            if not note or (ts[0] != pass_ts):
                note += f"({min:02d}:{sec:02d} + {samples} samples)"
            sys.stderr.write(f"NOTE: adding {note} to label to note correct label placement\n")
        ts[i] = pass_ts

    label += " " + note

    audcommand(f'Select: Track={LabelTrack} Start={ts[0]} End={ts[1]}')
    audcommand("SetTrackStatus: Focused=1")
    audcommand("AddLabel:")

    # now, stupidly, we need to find the # of the label we just added
    jdata = pa.get_info(info_type='Labels')
    jdata = (jdata [:jdata.rfind(']')+1])
    aud_labels = json.loads(jdata)
        # [labeltrack, ...]
        # where each label track is [tracknumber, [[ts1, ts2, label], ...]
    labelnum = 0
    newlabelnum = None
    justblank = []
    for lt in aud_labels:
        dprint(f"label track {lt[0]}")
        for l in lt[1]:
            dprint(f"label{labelnum}: {l}")
            if l[2] == '' and l[0] == ts[0] and l[1] == ts[1]:
                # this is our label number
                newlabelnum = labelnum
                break
            elif l[2] == '':
                print(f"Blank label# {labelnum} start={l[0]} end={l[1]}")
                justblank.append(labelnum)
            labelnum += 1
    if (newlabelnum is None) and not audacitytest:
        sys.stderr.write(f"Could not find the empty label we just created at {ts[0]}-{ts[1]} in track {LabelTrack}, "
                         + "trying at ts=0\n")
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
            sys.stderr.write(f"Could not find the empty label we just created in track {LabelTrack} "
                             + f"at ts=0 or ts={ts[0]} but we found these blank labels {justblank}\n")
            if len(justblank) == 1:
                print(f"Using only blank, label# {justblank[0]}")
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

# read metadata from file with given filename, return list of dicts containing
# unique metadata
def parse_metadatafile(filename):
    global metadata_re

    returnlist = []
    lastentry = None

    try:
        input_file = open(filename, 'r')
    except Exception as inst:
        print(f"Unable to open {filename}: {inst}")
        sys.exit('Exiting.')

    linenum = 0
    for line in input_file:
        linenum += 1
        line = line.strip()
        entry = None
        #            1                2                 3                4                  5
        # timestamp=(.+*), trackName=(.+?), artistName=(.+?), albumName=(.+?), trackNumber=(\d+),
        #                  6            7           8                    9                        10
        # albumTrackCount=(\d+), genre=(.+?), year=(\d+), trackDuration=([\d.]+), playerPosition=([\d.]+)"
        if match := metadata_re.match(line):
            entry = {"track": match.group(2), "artist": match.group(3), "album": match.group(4),
                     "num": match.group(5)+"/"+match.group(6), "genre": match.group(7), "year": match.group(8),
                     "duration": match.group(9)}
            if lastentry:
                lastentry_match = True
                for k in lastentry:
                    if lastentry[k] != entry[k]:
                        lastentry_match = False
                        break
                if not lastentry_match:
                    # new entry, add it to our data
                    returnlist.append(entry)
                    lastentry = entry
            else:
                # no last entry
                returnlist.append(entry)
                lastentry = entry
        else:
            # line did not match regex
            sys.stederr.write(f"Unexpected input line {linenum}: {line}")
    # end for line in input_file
    input_file.close()
    return(returnlist)


def main(args):
    global audacitytest, debug, aud_clips, aud_tracks

    # Read arguments
    metadatafile = args.metadata
    debug = args.debug
    debug_dump = args.dump
    audacitytest = args.audacitytest
    exec_start = time.time()
    pp = pprint.PrettyPrinter(indent=4)

    dprint("DEBUG mode on")

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
    audio_track = None
    label_track = None
    audio_start = None
    audio_end   = None
    for at_i in range(len(aud_tracks)):
        if (audio_track == None) and (aud_tracks[at_i]['kind'] == 'wave'):
            audio_track = at_i
            audio_start = aud_tracks[at_i]['start']
            audio_end   = aud_tracks[at_i]['end']
        elif (label_track == None) and (aud_tracks[at_i]['kind'] == 'label'):
            label_track = at_i
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

    # Create label for each entry in metadata
    start = 0
    for track in metadata:
        if (start > audio_end):
            print(f"Track {track['track']} starts after end of recorded audio, skipping it and all remaining.")
            break
            
        try:
            duration = float(track['duration'])
        except:
            sys.exit(f"Issue with setting duration to float on entry {track}")

        end = start + duration
        print(f"Label: '{track['track']:40s}' len: {duration:7.3f} start: {start:7.3f} end: {end:7.3f}")
        setlabel(label_track,start,track['track'],endts=end)

        start = end

    # Verify everything looks ok with user

    # Export wav files

    # Invoke wavepack to pack exported files

    # Label wavepack files

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
        trackDuration=355.013000488281, playerPosition=12.192999839783
    """
    parser = argparse.ArgumentParser(description=description, epilog=epilog)
    parser.add_argument('metadata', help='Metadata file')
    parser.add_argument('--debug', action='store_true', help='Enable debug text')
    parser.add_argument('--dump', action='store_true', help='Dump data structures as they are read/created')
    parser.add_argument('--audacitytest', action='store_true', help='Print Audacity commands, but do not send them')

    args = parser.parse_args()
    main(args)
