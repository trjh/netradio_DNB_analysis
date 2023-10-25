import numpy as np
from pydub import AudioSegment
import pydub
import argparse
import sys
import matplotlib.pyplot as plt
# import pipeclient from scripts for Audacity
sys.path.append('/Users/timh/Downloads/Netradio/netradio_DNB_localdisk/scripts')
import pipeclient
import json
import time
import re

samplerate = 0      # sample rate for comparisons
debug = False       # debug level
audacitytest = False # audacity debug?
max_samplerate = 0  # maximum sample rate between A and B
# audacity connection
audacity = pipeclient.PipeClient()
# audacity shared data
aud_tracks = None
aud_clips = None
ALabelTrack = BLabelTrack = BAudioTrack = None
BNumber = None  # as in 051: 051-E-Sassin - Nightrider.wv
# filenames so we can use them in Audacity
fileA = fileB = None
# how far to search(in sec) either side of align point in A for best match
A_Align_Search = 0.5
# align all indexes by this # of samples to align with Audacity's minimum input unit of measure 0.001s
# unless precise flag is set
step_by = 1
precise = False
# figure to leave in background until next graph started
fig = None
# advice
advice = """
    - Ensure that your starting points match up closely to the start of each song
    - Try expanding your test window to more than the default number of samples
    - Check that your search window is not too narrow
"""

def dprint(string):
    global debug
    if debug:
        print(string)

def parse_file_and_startstop(arg):
    parts = arg.split(":")
    if len(parts) == 1:
        return parts[0], 0, -1
    elif len(parts) == 2:
        return parts[0], float(parts[1]), -1
    else:
        return parts[0], float(parts[1]), float(parts[2])

# convert samplerate to max for use in file
def samplerate2max(sr):
    return f"{int(sr*max_samplerate/samplerate):8d} in {max_samplerate/1000}kHz samples"

def audiosegment_to_numpy(audio):
    # chatgpt suggested way -- get samples in the format
    # [sample0_l, sample0_r, sample1_l, sample1_r], ...]
    # then rehsape to
    # [[sample0_l, sample0_r], [sample1_l, sample1_r], ...]
    #samples = np.array(audio.get_array_of_samples())
    #if audio.channels == 2:
    #    samples = samples.reshape((-1, 2))
    
    # but comparing two signals in parallel isn't working.  let's just go with
    # the first channel for now
    channels = audio.split_to_mono()
    samples = np.array(channels[0].get_array_of_samples())
    return samples

def sample_profile(samples, sr):
    return "{:8d} samples, or {} at {:.1f}kHz".format(
        samples,
        sample2ts(samples),
        sr/1000)

def np_rms(np_array):
    return np.sqrt(np.mean(np.square(np_array, dtype=np.int64)))

# works for both AudioSegments and numpy arrays
def adjust_volume(a, b):
    # AudioSegment
    if isinstance(a, pydub.audio_segment.AudioSegment):
        dprint(f"Max volume A: {a.max:5d} B: {b.max:5d}\n"+
               f"RMS        A: {a.rms:5d} B: {b.rms:5d}")

        adjustedA = adjustedB = ""
        if a.rms < b.rms:
            needed_boost = pydub.utils.ratio_to_db(b.rms / a.rms)
            a = a.apply_gain(needed_boost)
            adjustedA = "adjusted"
        elif a.rms > b.rms:
            needed_boost = pydub.utils.ratio_to_db(a.rms / b.rms)
            b = b.apply_gain(needed_boost)
            adjustedB = "adjusted"
        else:
            print("No difference in RMS, no volume adjustment.")

        dprint(f"Max volume {adjustedA}A: {a.max:5d} {adjustedB}B: {b.max:5d}\n"+
               f"RMS        {adjustedA}A: {a.rms:5d} {adjustedB}B: {b.rms:5d}")
        return

    # signal array
    elif isinstance(a, np.ndarray):
        # https://superkogito.github.io/blog/2020/04/30/rms_normalization.html
        signals = {'a': a, 'b': b}
        adjust = source = ""
        sig_rms = {}
        maxvol = {}

        dprint("   Max Volume      RMS")
        for k in signals.keys():
            sig_rms[k] = np_rms(signals[k])
            maxvol[k] = np.abs(signals[k]).max()
            dprint(f"{k}: {maxvol[k]:10d} {sig_rms[k]:8.2f}")

        # set this to sig_rms to use RMS instead of amplitude
        adjust_value = maxvol
        adjust_string = "Amplitude"

        if adjust_value['a'] < adjust_value['b']:
            adjust = 'a'
            source = 'b'
        elif adjust_value['a'] > adjust_value['b']:
            adjust = 'b'
            source = 'a'
        else:
            dprint(f"No difference in {adjust_string}, no volume adjustment.")

        if adjust:
            orig = signals[adjust]
            # uncomment to adjust by RMS
            # factor  sig_rms[source]/sig_rms[adjust]
            factor = adjust_value[source]/adjust_value[adjust]
            dprint(f"Adjusting {adjust} by {factor}")
            signals[adjust] = np.rint(signals[adjust]*factor).astype(int)
            sig_rms[adjust] = np_rms(signals[adjust])
            maxvol[adjust] = np.abs(signals[adjust]).max()
            if debug:
                print("Showing volume adjustment plot, hit return to continue.")
                volumeplot(f"Original {adjust}, Original {source}, Adjusted {adjust} - byamplitude",
                           orig,signals[source],signals[adjust])
                input()

        dprint("   Max Volume      RMS")
        for k in signals.keys():
            dprint(f"{k}: {maxvol[k]:10d} {sig_rms[k]:8.2f}")

        return (signals['a'],signals['b'])

    if False:
        # second way -- normalize both, but this just adjust to max volume,
        # which doesn't always adjust the low-bandwith signal appropriately
        print(f"Max volume A: {a_audio.max} B: {b_audio.max} -- normalizing both")
        a_audio = pydub.effects.normalize(a_audio)
        b_audio = pydub.effects.normalize(b_audio)
        print(f"Max volume A: {a_audio.max} B: {b_audio.max} -- after normalization")
    if False:
        # def normalize_volume(audio, max):
        # first way -- adjust one sample to the volume of the other
        peak_sample_val = audio.max
            
        if peak_sample_val == 0:
            return seg
                                
        needed_boost = pydub.utils.ratio_to_db(max / peak_sample_val)
        return audio.apply_gain(needed_boost)
    
# convert a sample # into a timestamp
def sample2ts(sample):
    global samplerate
    seconds = sample/samplerate
    min = int(seconds/60)
    sec = int(seconds)%60
    msec = int(1000*(seconds - int(seconds)))
    return f"{min:2d}:{sec:02d}.{msec:03d}"

# plot results
def makeplot(label,a_signal,a_signal_index,b_signal,b_signal_index,half_test_window):
    global fig

    fig, axs= plt.subplots(1, 4)
    fig.suptitle(f'{label} Waves A, B, Added, Added(abs)')
    fig.set_figwidth(15)
    orig_a_min = orig_a_max = orig_b_min = orig_b_max = 0

    # calculate range -- we could alternatively use max amplitude a_audio.max_possible_amplitude
    orig_a_min = a_min=max(0,a_signal_index-half_test_window)
    orig_a_max = a_max=min(len(a_signal),a_signal_index+half_test_window)
    orig_b_min = b_min=max(0,b_signal_index-half_test_window)
    orig_b_max = b_max=min(len(b_signal),b_signal_index+half_test_window)

    dprint(f"makeplot: A range {a_min} - {a_max}\n          B range {b_min} - {b_max}")

    # check size, for when we've really expanded the scale
    if ((a_max - a_min) < (b_max-b_min)):
        correctvalue = ((b_max-b_min) - (a_max - a_min))
        dprint(f"Adjusting B scale as it is {correctvalue} larger than A")
        a_left = a_signal_index-a_min
        a_right = a_max-a_signal_index
        b_min = b_signal_index-a_left
        b_max = b_signal_index+a_right
        dprint(f"     now: A range {a_min} - {a_max}\n          B range {b_min} - {b_max}")
    elif ((a_max - a_min) > (b_max-b_min)):
        correctvalue = ((a_max-a_min) - (b_max - b_min))
        dprint(f"Adjusting A scale as it is {correctvalue} larger than B")
        b_left = b_signal_index-b_min
        b_right = b_max-b_signal_index
        a_min = a_signal_index-b_left
        a_max = a_signal_index+b_right
        dprint(f"     now: A range {a_min} - {a_max}\n          B range {b_min} - {b_max}")

    # calculate scale
    scale = 16000
    for val in [
            np.max(np.abs(a_signal[a_min:a_max])),
            np.max(np.abs(b_signal[b_min:b_max]))
            ]:
        if val > scale:
            scale = val

    for ax in axs:
        ax.set(ylim=(-1*scale,scale))
        ax.axhline(0)
    axs[0].axvline(a_signal_index) #x-axis line
    axs[1].axvline(b_signal_index) #x-axis line
    axs[2].axvline(half_test_window)
    axs[3].axvline(half_test_window)

    if half_test_window < 25:
        axs[0].stem(range(orig_a_min,orig_a_max), a_signal[orig_a_min:orig_a_max])
        axs[1].stem(range(orig_b_min,orig_b_max), b_signal[orig_b_min:orig_b_max])
    else:
        axs[0].plot(range(orig_a_min,orig_a_max), a_signal[orig_a_min:orig_a_max])
        axs[1].plot(range(orig_b_min,orig_b_max), b_signal[orig_b_min:orig_b_max])

    sum_array = a_signal[a_min:a_max] + b_signal[b_min:b_max]
    print(f"sum_array = a_signal[{a_min=}:{a_max=}] + b_signal[{b_min=}:{b_max=}] -- oh and 2*{half_test_window=}")
    axs[2].plot(range(0,a_max-a_min), sum_array)
    axs[3].plot(range(0,a_max-a_min), np.abs(sum_array))
    plt.draw()
    plt.pause(1)


# plot before/after for adjust_volume
def volumeplot(label,a_signal,b_signal,c_signal):
    global fig

    signals = [a_signal, b_signal, c_signal]

    fig, axs= plt.subplots(1, 3)
    fig.suptitle(label)
    fig.set_figwidth(15)

    # calculate scale
    scale = 16000
    for s in signals:
        val = np.max(np.abs(s))
        if val > scale:
            scale = val

    for ax in axs:
        ax.set(ylim=(-1*scale,scale))
        ax.axhline(0)

    for s in range(len(signals)):
        l = len(signals[s])
        axs[s].axvline(l/2)   # x-axis line
        axs[s].plot(range(l), signals[s])

    plt.draw()
    plt.pause(1)

def audcommand(string):
    global audacitytest

    reply = ''
    if audacitytest and (('AddLabel' in string) or ('SetLabel' in string)):
        print(f"TESTMODE - would send <<{string}>> to Audacity")
        return
    audacity.write(string + '\n')
    # Allow a little time for Audacity to return the data:
    for wait in [0.1, 0.2, 0.4, 0.8, 1.0]:
        time.sleep(wait)
        reply = (audacity.read())
        if reply != '':
            break
    if reply == '':
        sys.exit(f'Audacity: No data returned for {string} ({reply}).')
    if not re.search(r"BatchCommand finished: OK", reply):
        sys.stderr.write(f"Unexpected Audacity response: <<{reply}>>\n\n")
        if re.search(r"BatchCommand finished: Failed!", reply):
            sys.exit("Failed command, exiting.")
    return reply

# initialize audacity data
def audinit():
    global aud_tracks, aud_clips, fileA, fileB, ALabelTrack, BLabelTrack, BAudioTrack, BNumber

    # Pull Track info if we don't have them already
    if not aud_tracks:
        jdata = audcommand('GetInfo: Type=Tracks')
        jdata = (jdata [:jdata.rfind(']')+1])
        aud_tracks = json.loads(jdata)
        # format of response
        # [{dict about track}, ...]
        
    # Set ALabelTrack and BLabelTrack if not already set
    if not ALabelTrack:
        if match := re.match(r"(.*/)?([^/]+)\..{2,4}$", fileA, re.IGNORECASE):
            baseA = match.group(2)
            count = 0
            for l in aud_tracks:
                if baseA in l['name'] and 'labels' in l['name'] and (l['kind'] == 'label'):
                    ALabelTrack = count
                    dprint(f"A Label Track {ALabelTrack} - {l['name']}")
                    break
                count += 1
            if not ALabelTrack:
                sys.exit(f'Could not find baseA: {baseA} in label track')
        else:
            sys.exit(f'Could not find base filename in fileA: {fileA}')
    if not BLabelTrack:
        if match := re.match(r"(.*/)?(\d{3})([^/]+)\..{2,4}", fileB, re.IGNORECASE):
            BNumber = match.group(2)
            count = 0
            for l in aud_tracks:
                if BNumber in l['name']:
                    if 'labels' in l['name'] and (l['kind'] == 'label'):
                        BLabelTrack = count
                        dprint(f"B Label Track {BLabelTrack} - {l['name']}")
                    elif l['kind'] == "wave":
                        BAudioTrack = count
                        dprint(f"B Audio Track {BAudioTrack} - {l['name']}")
                count += 1
            if not BLabelTrack:
                sys.exit(f'Could not find label track {BNumber}.labels')
        else:
            sys.exit(f'Could not find tracknum/base filename in fileB: {fileB}')
        

# given appropriate details, set a label on a given track
def setlabel(LabelTrack,ts,label,endts=None):
    global step_by, precise

    setts = False   # by default, don't set the timestamp when setting label text, but do if we found it at ts=0

    dprint(f"setlabel({LabelTrack}, {ts}, {label}, endts={endts})")
    if (not LabelTrack) or (not ts) or (not label):
        sys.exit(f"invalid arguments to setlabel({LabelTrack}, {ts}, {label}, endts={endts})")

    ts = [ts, endts if endts else ts]
    note = ""

    # Adjust the timestamps
    for i in [0,1]:
        # round down ts to 8 digits to avoid python math wierdness
        dprint(f"ts[{i}] fractions of a second in samples: {(ts[i] - int(ts[i]))*max_samplerate}")
        ts[i]=round(ts[i],8)

        # check precision of the ts
        # - if it's more precise than step_by, note this
        # - if precise flag is set, and it's more precise than 0.001s,
        #   add sample offset to label
        pass_ts=round(ts[i],3)
        if (ts[i] != pass_ts):
            if (step_by > 1):
                sys.stderr.write(
                    f"WARNING: setlabel given timestamp {ts[i]} with greater granularity " +
                    f"than expected {step_by * samplerate}s")
            elif precise:
                # add sample offset to label -- format is HH:MM:SS + samples
                min = int(ts[i]/60)
                sec = int(ts[i])%60
                samples = int((ts[i] - int(ts[i])) * max_samplerate)
                if note and (ts[0] != pass_ts):
                    dprint(f"ADDING TO NOTE AS ts0 ({ts[0]}) != ts1/pass_ts ({pass_ts})")
                    note += "-"
                if not note or (ts[0] != pass_ts):
                    note += f"({min:02d}:{sec:02d} + {samples} samples)"
                sys.stderr.write(f"NOTE: adding {note} to label to note correct label placement\n")
            else:
                dprint(f"setlabel: timestamp rounded down from {ts[i]} to {pass_ts[i]}")
        ts[i] = pass_ts

    label += " " + note

    audcommand(f'Select: Track={LabelTrack} Start={ts[0]} End={ts[1]}')
    audcommand("SetTrackStatus: Focused=1")
    audcommand("AddLabel:")
        # now, stupidly, we need to find the # of the label we just added
    jdata = audcommand('GetInfo: Type=Labels')
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
    if not newlabelnum and not audacitytest:
        sys.stderr.write(f"Could not find the empty label we just created at {ts[0]}-{ts[1]} in track {ALabelTrack}, "
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
            sys.stderr.write(f"Could not find the empty label we just created in track {ALabelTrack} "
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

# Given an alignment label, A track timestamp, and B track timestamp,
# Set labels on the A track for start of B and alignment, and set label on B for alignment
def setlabels(label, A_ts, B_ts, B_start=None, B_end=None, justA=False):
    global aud_tracks, aud_clips, fileA, fileB, ALabelTrack, BLabelTrack, BAudioTrack, BNumber
    aud_labels = None

    # Pull Track info if we don't have them already
    if not aud_tracks or not ALabelTrack or not BLabelTrack:
        audinit()
        
    # Get/Update Clip Info
    jdata = audcommand('GetInfo: Type=Clips')
    jdata = (jdata [:jdata.rfind(']')+1])
    aud_clips = json.loads(jdata)
    # format of response
    # [{dict about clips}, ...]
    BClipStart = None
    for c in aud_clips:
        if c['track'] == BAudioTrack:
            BClipStart = c['start']
            dprint(f"B Audio Track start: {BClipStart}")
            break

    # Set A alignment label
    setlabel(ALabelTrack,A_ts,"NOTE: "+label)
    if justA:
        return

    # Set A start-of-B label
    setlabel(ALabelTrack,A_ts-B_ts,f"ORIG{BNumber} start: "+label)

    # Set B alignment label
    setlabel(BLabelTrack,BClipStart+B_ts,f"ORIG{BNumber} NOTE: "+label)

    # Set B range label
    if B_start:
        setlabel(BLabelTrack,BClipStart+B_start,f"ORIG{BNumber} NOTE: search range "+label,endts=BClipStart+B_end)


# given initial proposed alignment point in signalarray, find point that meets
# these rules, searching in a `A_Align_Search` window around initial point
# - >1 sec from beginning
# - >1 sec from end
# - both of the above should ensure no search wrap-around (half_test_window < 1 sec)
# - highest data point available
def find_best_alignpoint(initialpoint, signalarray):
    global samplerate, A_Align_Search, step_by

    returnpoint = -1
    align_min = max(int(0.5*samplerate), initialpoint - A_Align_Search*samplerate)
    align_max = min(initialpoint + A_Align_Search*samplerate, len(signalarray) - samplerate)
    max_amplitude = 0
    dprint(f"find_best_alignpoint: initial ({initialpoint}) min ({align_min}) max ({align_max})")
    for ap_i in range(int(align_min),int(align_max),step_by):
        if max_amplitude < abs(signalarray[ap_i]):
            max_amplitude = abs(signalarray[ap_i])
            returnpoint = ap_i
    dprint(f"find_best_alignpoint: final   ({returnpoint}) -- max signal value {max_amplitude}")

    return(returnpoint, max_amplitude)


def main(args):
    global max_samplerate, samplerate, fileA, fileB, debug, A_Align_Search, step_by, precise
    global audacitytest

    # Read arguments
    fileA, start_timeA, stop_timeA = parse_file_and_startstop(args.fileA)
    fileB, start_timeB, stop_timeB = parse_file_and_startstop(args.fileB)
    align_points = args.alignpoints
    search_window = args.searchwindow
    test_window = args.testwindow
    precise = args.precise
    debug = args.debug
    debug_dump = args.dump
    audacitytest = args.audacitytest
    localnorm = args.localnorm
    half_test_window = test_window // 2
    invert = -1 if args.invert else 1
    exec_start = time.time()

    dprint("DEBUG mode on")

    # Initialize Audacity
    print("Initializing Audacity")
    audinit()

    # Load tracks
    print(f"Loading {fileA}")
    try:
        a_audio = AudioSegment.from_file(fileA)
    except Exception as e:
        sys.exit(f"Cannot read {fileA}: {e}")

    print(f"Loading {fileB}")
    try:
        b_audio = AudioSegment.from_file(fileB)
    except Exception as e:
        sys.exit(f"Cannot read {fileA}: {e}")

    print(f".    Input: a length {len(a_audio)/1000}s, rate {a_audio.frame_rate/1000}kHz")
    print(f".    Input: b length {len(b_audio)/1000}s, rate {b_audio.frame_rate/1000}kHz")

    # Equalize sample rate
    if (a_audio.frame_rate < b_audio.frame_rate):
        print(f"Resampling A from {a_audio.frame_rate/1000}kHz to {b_audio.frame_rate/1000}kHz")
        max_samplerate = b_audio.frame_rate
        b_audio = b_audio.set_frame_rate(a_audio.frame_rate)
    elif (a_audio.frame_rate > b_audio.frame_rate):
        print(f"Resampling B from {b_audio.frame_rate/1000}kHz to {a_audio.frame_rate/1000}kHz")
        max_samplerate = a_audio.frame_rate
        a_audio = a_audio.set_frame_rate(b_audio.frame_rate)

    # Adjust volume
    adjust_volume(a_audio,b_audio)

    # Collect left side of audio as numpy array
    samplerate = a_audio.frame_rate             # same on both now
    a_signal = audiosegment_to_numpy(a_audio)
    b_signal = audiosegment_to_numpy(b_audio)

    # Truncate based on end time if necessary
    if stop_timeA != -1:
        a_signal = a_signal[:int(stop_timeA * samplerate)]
    if stop_timeB != -1:
        b_signal = b_signal[:int(stop_timeB * samplerate):]

    print(".StopTrunc: a has " + sample_profile(len(a_signal), samplerate))
    print(".           b has " + sample_profile(len(b_signal), samplerate))

    # Truncate based on start time if necessary
    start_sampleA = int(start_timeA * samplerate)
    start_sampleB = int(start_timeB * samplerate)

    a_signal = a_signal[start_sampleA:]
    b_signal = b_signal[start_sampleB:]

    print(".StartTrnc: a has " + sample_profile(len(a_signal), samplerate))
    print(".           b has " + sample_profile(len(b_signal), samplerate))

    # Convert search window value from seconds to samples
    search_window_samples = int(search_window * samplerate)
    dprint(f"Search window in seconds: {search_window}, in samples: {search_window_samples}")

    # Set stepby value -- Audacity keeps track of individual samples, but only
    # allows us to set labels by time down to the nearest 0.001 second.  So
    # let's be sure that A and B timestamps are aligned properly to these
    # times.  (Unless precise flag is set)
    if not precise:
        step_by = int(0.001 * samplerate)
    test_window = int(test_window/step_by) * step_by
    half_test_window = int(half_test_window/step_by) * step_by

    # Choose alignment points -- one at start, one at end, distribute the rest
    align_indices = [0, len(a_signal)-int(0.5*samplerate)]
    if align_points > 2:
        step_a = len(a_signal) // (align_points - 1)
        step_a = int(step_a/step_by) * step_by
        for step_a_i in range(1,align_points - 1):
            align_indices.append(step_a_i * step_a)
    align_indices.sort()

    # save results for examation at the end
    align_results = []
    
    for apidx in range(len(align_indices)):
        align_point_a = align_indices[apidx]
        # Select best point near initiall chosen align point
        align_search_direction = -1 if (align_point_a == align_indices[-1]) else 1 # move backwards on last point

        # look over 6 windows until we find a minimal good amplitude
        for mult in range(5):
            startpoint = align_point_a + (mult * A_Align_Search*samplerate * align_search_direction)
            newpoint, max_amplitude = find_best_alignpoint(startpoint, a_signal)
            if max_amplitude > 6000:
                align_point_a = newpoint
                break

        # build seperate array of A signals we are searching
        a_signal_offset = align_point_a - half_test_window
        a_signal_slice = np.array(a_signal[a_signal_offset:align_point_a+half_test_window])
        a_signal_rms = np_rms(a_signal_slice)

        # Establish range to search in B
        dprint(f"Search point B theory: {align_point_a - search_window_samples} - "+
               f"{align_point_a + search_window_samples}, width {2*search_window_samples}")
        last_point_adjust = 0
        if align_results:
            last_point_adjust = align_results[-1][2] - align_results[-1][1]
            dprint(f"Shifting B window by {last_point_adjust} to account for diff between last point A&B")
        start_b = max(half_test_window, (align_point_a - search_window_samples)+last_point_adjust)
        end_b   = min(len(b_signal)-half_test_window, align_point_a + search_window_samples + last_point_adjust)
        dprint(f"Search point B actual: {start_b} - {end_b}, width {end_b - start_b}")

        # make B search range a seperate slice
        b_signal_offset = start_b - half_test_window
        b_signal_slice = np.array(b_signal[b_signal_offset:end_b+half_test_window])

        print(f"\nChecking align point {apidx}: point A: "+sample2ts(align_point_a)
              +f" (from checking start) : ({align_point_a} samples, {samplerate2max(align_point_a)})\n"
              +f"{' ':23s} range B: "+sample2ts(start_b)+" - "+sample2ts(end_b)
              +f" : clocktime elapsed {time.time() - exec_start:.1f}s")

        alignvalues=[]

        # at this point we could normalize the volumes of a_signal_slice and b_signal_slice
        if localnorm:
            (a_signal_slice, b_signal_slice) = adjust_volume(a_signal_slice,b_signal_slice)

        for j in range(start_b, end_b, step_by):
            b_j_start = j-b_signal_offset-half_test_window
            b_j_end   = j-b_signal_offset+half_test_window
            sum_array = a_signal_slice + b_signal_slice[b_j_start:b_j_end]

            # reject the point of RMS of sum is greater than RMS of A or of B
            # -- adding the signals should in *some* way lessen the signal
            b_signal_rms = np_rms(b_signal_slice[b_j_start:b_j_end])
            sum_rms = np_rms(sum_array)
            reject = False
            if (sum_rms > a_signal_rms) or (sum_rms > b_signal_rms):
                reject = True

            # Add element to score -- difference between RMS
            diff_rms = abs(a_signal_rms - b_signal_rms)

            # Calculate various scoring values, we'll select one later
            abssum_array = np.abs(sum_array)
            mean = np.mean(abssum_array)
            std  = np.std(abssum_array)

            # store multiple results
            j_entry = [
                j,
                np.sum(abssum_array),
                mean,
                std,
                np.sum(np.square(sum_array, dtype=np.int64)),
                mean + std + diff_rms,
                sum_rms,
                reject
                ]
            alignvalues.append(j_entry)
            if debug_dump:
                print(f"{sample2ts(j)} {j_entry}")

        # find minimum scores in each column -- where sum RMS is less than A RMS and B RMS
        alignvalues=np.array(alignvalues)
        filtered_rows = alignvalues[alignvalues[:, -1] == False]
        print(f"Data points examined: {alignvalues.shape[0]}, results where sum lowered RMS: {filtered_rows.shape[0]}")
        if filtered_rows.shape[0] == 0:
            print(f"\nWARNING: No good signal matches according to RMS tests, ignoring the test :WARNING")
            filtered_rows = alignvalues
        min_indices = np.argmin(filtered_rows, axis=0)

        # select alignment point by mean + stddev
        align_point_b  = int(filtered_rows[min_indices[5],0])
        min_meanstddev = filtered_rows[min_indices[5],5]

        # summary of result
        print(f"\n                 RESULT point B: "+sample2ts(align_point_b)+
              f" ({align_point_b} samples, {samplerate2max(align_point_b)})"+
              f"\n{' ':18s} Alignment by minimum (mean + stddev) of absolute value, score {min_meanstddev}")

        # save results
        align_results.append((apidx, align_point_a, align_point_b))

        # what were the other possibilities? -- show sample/timestamps with same minimum mean+stddev
        alignvalue_min_indices = np.where(filtered_rows[:,5] == min_meanstddev)[0]
        alignvalue_min_count = len(alignvalue_min_indices)
        if alignvalue_min_count == 1:
            print ("Good Sample -- this is the only index with this score")
        elif alignvalue_min_count > 1:
            print(f"WARNING: {alignvalue_min_count} samples had the same score -- printing first 5 timestamps")
            amatch_ts = []
            for amatch_i in range(5):
                amatch_ts.append(sample2ts(filtered_rows[alignvalue_min_indices[amatch_i],0]
                                 + start_sampleB))
            print(f"         {', '.join(amatch_ts)}")

        # what were the timestamps according to other scores?
        print("Timestamp  Scoring Method")
        for im in [("Sum",1), ("Mean",2), ("StdDev",3), ("SumSquared",4), ("RMS", 6)]:
            im_ts = filtered_rows[min_indices[im[1]],0]
            print("{:9s}  {:10s} {}".format(sample2ts(im_ts), im[0],
                "MATCH" if (im_ts == align_point_b) else "(differs)"
                ))

        # debug print of align point finding details
        if debug:
            print("{:9s}    {:>8s}\t{:>8s}\t{:>8s}\t{:>8s}\t{:>8s}\t{:>8s}\t{:>8s}".format(
                "Timestamp", "Sum", "Mean", "Std", "Mean+Std", "Sum(^2)", "RMS", "TS(raw)"))
            for pair in [("Mean+Std",5),
                         ("Sum",1),
                         ("Mean",2),
                         ("Std",3),
                         ("Sum(Squared)",4),
                         ("RMS",6)]:
                print(f"--- by {pair[0]}")
                # get indices that would sort by each method
                sorted_indices = np.argsort(filtered_rows[:, pair[1]])

                # print lowest five values
                for k in range(5):
                    if k >= len(sorted_indices): continue
                    av=filtered_rows[sorted_indices[k]]
                    print("{:9s}    {:8d}\t{:8.2f}\t{:8.2f}\t{:8.2f}\t{:8d}\t{:8.2f}\t{:8d}".format(
                        sample2ts(start_sampleB + av[0]),int(av[1]),av[2],av[3],
                        av[5], int(av[4]), av[6], int(av[0])))

            # summarize data that will have scrolled away
            print(f"alignvalues#: {alignvalues.shape[0]} filtered#: {filtered_rows.shape[0]} matches to our point: {alignvalue_min_count}")

        # for comparing program results to manually found right results, plug
        # your point # in here and change False to True
        if False and apidx==0:
            # keeping this for further manual/automatic comparison
            targettime=0.513
            #targetsample=int(targettime*samplerate)
            targetsample=8207
            # find the sample
            alignvalue_target = np.where(filtered_rows[:,0] == targetsample)[0][0]
            av = filtered_rows[alignvalue_target]
            print(f"--- AND AT OUR TARGET {targettime}s ({targetsample})")
            print("{:9s}    {:8d}\t{:8.2f}\t{:8.2f}\t{:8.2f}\t{:8d}\t{:8d}".format(
                sample2ts(start_sampleB + av[0]),int(av[1]),av[2],av[3],
                av[5], int(av[4]), int(av[0])))
            sys.stderr.write("NOTE -- half_test_window adjusted to 90x here!  NOTE NOTE\n")
            makeplot(f"Mean+StDev (manual)",a_signal,align_point_a,b_signal,targetsample,90*half_test_window)
            print("OURGRAPH - original signal array -- hit return", end='')
            choice = input().lower()
            print("")
            makeplot(f"Mean+StDev (manual)",a_signal_slice,align_point_a-a_signal_offset,
                                            b_signal_slice,targetsample-b_signal_offset,2*half_test_window)
            print("OURGRAPH - signal slice array -- hit return", end='')
            choice = input().lower()
            print("---")
        ## END MANUAL DEBUG SECTION

        # ask if we should show the plots
        print("View graphs? [yN]", end='')
        choice = input().lower()
        if 'y' in choice:
            # show plots of results
            makeplot(f"FULL RANGE Mean+StDev #{apidx}{' w/localnorm' if localnorm else ''}",
                     a_signal_slice, align_point_a - a_signal_offset,
                     b_signal_slice, align_point_b - b_signal_offset, half_test_window)
            if (half_test_window > 1000):
                print("Hit return for closeup graph", end='')
                choice = input().lower()
                makeplot(f"Range:200 Mean+StDev #{apidx}{' w/localnorm' if localnorm else ''}",
                         a_signal_slice, align_point_a - a_signal_offset,
                         b_signal_slice, align_point_b - b_signal_offset, 100)
            print("Super closeup graph? [yN]", end='')
            choice = input().lower()
            if 'y' in choice:
                makeplot(f"Range:20/Original sig Mean+StDev #{apidx}{' w/localnorm' if localnorm else ''}",
                         a_signal, align_point_a,
                         b_signal, align_point_b, 10)
            # old way -- using original signal
            #makeplot(f"Mean+StDev #{apidx}{' w/localnorm' if localnorm else ''}",
            #         a_signal,align_point_a,b_signal,align_point_b,half_test_window)

        # ask if we should add it to Audacity
        print(f"Add align point {apidx} to Audacity? [y - all, j - just align point, N - none] ", end='')
        choice = input().lower()
        if 'y' in choice:
            Apoint  = (start_sampleA + align_point_a)/samplerate
            Bpoint  = align_point_b/samplerate
            B_start = start_b/samplerate
            B_end   = end_b/samplerate
            print(f"Adding points to Audacity A: {Apoint} B: {Bpoint}")
            dprint(f"setlabels(f'script align pt {apidx}', {Apoint=}, {Bpoint=}, B_start={B_start=}, B_end={B_end=})")
            setlabels(f"script align point {apidx}", Apoint, Bpoint, B_start=B_start, B_end=B_end)
        elif 'j' in choice:
            Apoint  = (start_sampleA + align_point_a)/samplerate
            Bpoint  = align_point_b/samplerate
            setlabels(f"script align point {apidx}", Apoint, Bpoint, justA=True)
        else:
            print("No labels added to Audacity.")

        # Keep a log of scores and audacity
        try:
            scorelog = open("alignscore.log", "a")
            scorelog.write(f"{fileA=} {fileB=} {apidx=} score={min_meanstddev} addtoaudacity={choice} "+
                           f"{start_timeA=} {align_point_a=} ({samplerate2max(align_point_a)}) "+
                           f"{start_timeB=} checked {start_b=} - {end_b=} "+
                           f"result {align_point_b=} ({samplerate2max(align_point_b)})\n")
            scorelog.close()
        except Exception as e:
            sys.exit(f"Cannot open/append alignscore.log: {e}")


        # END: for j in range(start_b, end_b, step_by):

    # END: for apidx in range(len(align_indices)):

    print("Analysis complete.")
    print("Calculating speed difference between A and B")
    print("Point  "+"  ".join([f'SpeedVSPt{n}' for n in range(len(align_results)-1)]))
    for ar_i in range(1,len(align_results)):
        print(f"{ar_i:5d}  ",end='')
        for comparepoint in range(ar_i):
            # mix/original A/B
            delta = 100 * ((align_results[ar_i][1] - align_results[comparepoint][1]) /
                           (align_results[ar_i][2] - align_results[comparepoint][2]))
            print(f"{delta:10.2f}  ",end='')
        print('')
    print(advice)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Audio file alignment')
    parser.add_argument('fileA', help='File A path and optional start/stop time in seconds (e.g. fileA, fileA:10.1, or fileA:10.1:500.2)')
    parser.add_argument('fileB', help='File B path and optional start/stop time in seconds (e.g. fileB, fileB:20, or fileB:20.5:600.0)')
    parser.add_argument('--alignpoints', type=int, default=5, help='Number of alignment points (default: 5)')
    parser.add_argument('--searchwindow', type=float, default=5.0, help='Search window in seconds (default: 5.0)')
    parser.add_argument('--testwindow', type=int, default=800, help='Test window in samples (default: 800)')
    parser.add_argument('--debug', action='store_true', help='Enable debug text')
    parser.add_argument('--dump', action='store_true', help='Show whole score table for align attempt')
    parser.add_argument('--invert', action='store_true', help='Invert signal B before adding signals to compare them')
    parser.add_argument('--localnorm', action='store_true',
                        help='By default, this program normalizes both samples so that theoretically the volume '+
                             'levels should be the same.  Sometimes this doesn\'t seem to work.  In this '+
                             'case, using this flag will normalize each A and B search window -- however, '+
                             'this may lead to false positives')
    parser.add_argument('--precise', action='store_true',
                        help='By default we search for timestamps to the nearest 0.001s, as we cannot pass '+
                             'anything more precise to Audacity.  With this flag, search for timestamps to '+
                             'the precision of the lowest rate between A and B, and reflect this in the notes '+
                             'to Audacity')
    parser.add_argument('--audacitytest', action='store_true', help='Print Audacity commands, but do not send them')

    args = parser.parse_args()
    main(args)
