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
A_Align_Search = 2.5
# align all indexes by this # of samples to align with Audacity's minimum input unit of measure 0.001s
step_by = 1

def dprint(string):
    global debug
    if debug:
        print(string)

def parse_file_and_startstop(arg):
    parts = arg.split(":")
    if len(parts) == 1:
        return parts[0], 0, -1
    elif len(parts) == 2:
        return parts[0], int(parts[1]), -1
    else:
        return parts[0], int(parts[1]), int(parts[2])

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

# print results -- ignore A mark as we already know it
def print_wavesum(label, value, ts_b):
    global samplerate

    print("{:6s} WaveSum: {:12f}, Timestamp B: {} ({})".format(
        label, value, sample2ts(ts_b), samplerate2max(ts_b)))

# like pydub.effects.normalize, but only adjust volume to 'max'
def normalize_volume(audio, max):
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
    msec = int((seconds - sec)*1000)
    return f"{min:2d}:{sec:02d}.{msec:03d}"

# plot results
def makeplot(label,a_signal,a_signal_index,b_signal,b_signal_index,half_test_window):
    fig, axs= plt.subplots(1, 4)
    fig.suptitle(f'{label} Waves A, B, Added, Added(abs)')
    fig.set_figwidth(15)

    # calculate scale
    scale = 16000
    for val in [
            np.max(np.abs(a_signal[a_signal_index-half_test_window:a_signal_index+half_test_window])),
            np.max(np.abs(b_signal[b_signal_index-half_test_window:b_signal_index+half_test_window]))
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

    axs[0].plot(range(a_signal_index-half_test_window,a_signal_index+half_test_window),
            a_signal[a_signal_index-half_test_window:a_signal_index+half_test_window])
    axs[1].plot(range(b_signal_index-half_test_window,b_signal_index+half_test_window),
            b_signal[b_signal_index-half_test_window:b_signal_index+half_test_window])

    sum_array = a_signal[a_signal_index-half_test_window:a_signal_index+half_test_window] + b_signal[b_signal_index-half_test_window:b_signal_index+half_test_window]
    axs[2].plot(range(0,2*half_test_window),
            sum_array)
    axs[3].plot(range(0,2*half_test_window),
            np.abs(sum_array))
    plt.show()

def oldplot(a_signal,absmin_ts_a,half_test_window):
    plt.figure(1)
    time = np.linspace(
        absmin_ts_a-half_test_window, # start
        test_window / samplerate,
        num = test_window
    )
    plt.plot(time, 
        a_signal[absmin_ts_a-half_test_window:absmin_ts_a+half_test_window])

    plt.show()

def audcommand(string):
    reply = ''
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
def setlabel(LabelTrack,ts,label):
    setts = False   # by default, don't set the timestamp when setting label text, but do if we found it at ts=0

    dprint(f"setlabel({LabelTrack}, {ts}, {label})")
    ts=round(ts,3)
    dprint(f"setlabel: timestamp rounded down - {ts}")

    audcommand(f'Select: Track={LabelTrack} Start={ts} End={ts}')
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
    for lt in aud_labels:
        dprint(f"label track {lt[0]}")
        for l in lt[1]:
            dprint(f"label{labelnum}: {l}")
            if l[2] == '' and l[0] == ts and l[1] == ts:
                # this is our label number
                newlabelnum = labelnum
                break
            labelnum += 1
    if not newlabelnum:
        sys.stderr.write(f"Could not find the empty label we just created at {ts} in track {ALabelTrack}, "
                         + "trying at ts=0\n")
        setts = True
        for lt in aud_labels:
            for l in lt[1]:
                if l[2] == '' and l[0] == 0 and l[1] == 0:
                    # this is our label number
                    newlabelnum = labelnum
                    break
                labelnum += 1
        if not newlabelnum:
            sys.exit(f"Could not find the empty label we just created in track {ALabelTrack} at ts=0 or ts={ts}")
    # finally, set the label description
    command = f'SetLabel: Label={newlabelnum} Text="{label}"'
    if setts:
        command += f' Start={ts} End={ts}'
    audcommand(command)

# Given an alignment label, A track timestamp, and B track timestamp,
# Set labels on the A track for start of B and alignment, and set label on B for alignment
def setlabels(label, A_ts, B_ts):
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
    setlabel(ALabelTrack,A_ts,"note: "+label)

    # Set A start-of-B label
    setlabel(ALabelTrack,A_ts-B_ts,f"orig{BNumber} start: "+label)

    # Set B alignment label
    setlabel(BLabelTrack,BClipStart+B_ts,f"orig{BNumber} note: "+label)


# given initial proposed alignment point in signalarray, find point that meets
# these rules, searching in a `A_Align_Search` window around initial point
# - >1 sec from beginning
# - >1 sec from end
# - both of the above should ensure no search wrap-around (half_test_window < 1 sec)
# - highest data point available
def find_best_alignpoint(initialpoint, signalarray):
    global samplerate, A_Align_Search, step_by

    returnpoint = -1
    align_min = max(1*samplerate, initialpoint - A_Align_Search*samplerate)
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
    global max_samplerate, samplerate, fileA, fileB, debug, A_Align_Search,step_by

    # Read arguments
    fileA, start_timeA, stop_timeA = parse_file_and_startstop(args.fileA)
    fileB, start_timeB, stop_timeB = parse_file_and_startstop(args.fileB)
    align_points = args.alignpoints
    search_window = args.searchwindow
    test_window = args.testwindow
    debug = args.debug
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

    print(f".    Input: a length {len(a_audio)/1000}s, "
          + f"rate {a_audio.frame_rate/1000}kHz")
    print(f".    Input: b length {len(b_audio)/1000}s, "
          + f"rate {b_audio.frame_rate/1000}kHz")

    # Equalize sample rate
    if (a_audio.frame_rate < b_audio.frame_rate):
        print(f"Resampling A from {a_audio.frame_rate/1000}kHz to {b_audio.frame_rate/1000}kHz")
        max_samplerate = b_audio.frame_rate
        b_audio = b_audio.set_frame_rate(a_audio.frame_rate)
    elif (a_audio.frame_rate > b_audio.frame_rate):
        print(f"Resampling B from {b_audio.frame_rate/1000}kHz to {a_audio.frame_rate/1000}kHz")
        max_samplerate = a_audio.frame_rate
        a_audio = a_audio.set_frame_rate(b_audio.frame_rate)

    dprint("Use max possible amplitude in plots, instead of 8k? "+
           f"{a_audio.max_possible_amplitude }")

    # Normalize volumes
    print(f"Max volume A: {a_audio.max} B: {b_audio.max} -- normalizing both")
    a_audio = pydub.effects.normalize(a_audio)
    b_audio = pydub.effects.normalize(b_audio)

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
    search_window_samples = search_window * samplerate

    # Set stepby value -- Audacity keeps track of individual samples, but only
    # allows us to set labels by time down to the nearest 0.001 second.  So
    # let's be sure that A and B timestamps are aligned properly to these
    # times.
    step_by = int(0.001 * samplerate)
    test_window = int(test_window/step_by) * step_by
    half_test_window = int(half_test_window/step_by) * step_by

    # Step for alignment points
    step_a = len(a_signal) // align_points
    step_a = int(step_a/step_by) * step_by
    
    for apidx in range(align_points):
        # Select align point in A
        align_point_a = apidx * step_a
        align_search_direction = -1 if (apidx == align_points-1) else 1 # move backwards on last point

        # look over 6 windows until we find a minimal good amplitude
        for mult in range(5):
            startpoint = align_point_a + (mult * A_Align_Search*samplerate * align_search_direction)
            newpoint, max_amplitude = find_best_alignpoint(startpoint, a_signal)
            if max_amplitude > 6000:
                align_point_a = newpoint
                break

        # Establish range to search in B
        start_b = max(half_test_window, align_point_a - search_window_samples)
        end_b = min(len(b_signal)-half_test_window, align_point_a + search_window_samples)
        dprint(f"Search point B: {start_b} - {end_b}")

        print(f"\nChecking align point {apidx}: point A: "+sample2ts(align_point_a)
              +f"{' ':21s} / ({samplerate2max(start_sampleA + align_point_a)})\n"
              +f"{' ':23s} range B: "+sample2ts(start_b)+" - "+sample2ts(end_b)
              +f" : elapsed {time.time() - exec_start:.1f}s")

        alignvalues=[]

        for j in range(start_b, end_b, step_by):
            sum_array = (a_signal[align_point_a-half_test_window:align_point_a+half_test_window] +
                         invert * b_signal[j-half_test_window:j+half_test_window])

            # now that that is really an array, try mean of abs
            # wave_sum = np.mean(np.abs(sum_array))

            abssum_array = np.abs(sum_array)
            # we *should* be able to use np.square but... it isn't doing what it is supposed to do
            # square_array = [pow(x,2) for x in sum_array]
            # too slow -- were we wrapping around?
            square_array = np.square(sum_array, dtype=np.int64)

            # store multiple results
            alignvalues.append([
                j,
                np.sum(abssum_array),
                np.mean(abssum_array),
                np.std(abssum_array),
                np.sum(square_array),
                np.mean(abssum_array) + np.std(abssum_array)
                ])

        # find minimum scores in each column
        alignvalues=np.array(alignvalues)
        min_indices = np.argmin(alignvalues, axis=0)

        # select alignment point by mean + stddev
        align_point_b  = int(alignvalues[min_indices[5],0])
        min_meanstddev = alignvalues[min_indices[5],5]

        # summary of result
        print("")
        print_wavesum("Alignment by minimum (mean + stddev) of absolute value",
                      min_meanstddev, start_sampleB + align_point_b)

        # what were the other possibilities? -- show sample/timestamps with same minimum mean+stddev
        alignvalue_min_indices = np.where(alignvalues[:,5] == min_meanstddev)[0]
        print(f"All samples with this score: {alignvalues[alignvalue_min_indices,0]}")

        # what were the timestamps according to other scores?
        print("Timestamp  Scoring Method")
        for im in [("Sum",1), ("Mean",2), ("StdDev",3), ("SumSquared",4)]:
            print("{:9s}  {}".format(sample2ts(start_sampleB + alignvalues[min_indices[im[1]],0]), im[0]))

        # debug print of align point finding details
        if debug:
            print("{:9s}    {:>8s}\t{:>8s}\t{:>8s}\t{:>8s}\t{:>8s}\t{:>8s}".format(
                "Timestamp", "Sum", "Mean", "Std", "Mean+Std", "Sum(^2)", "TS(raw)"))
            for pair in [("Mean+Std",5),
                         ("Sum",1),
                         ("Mean",2),
                         ("Std",3),
                         ("Sum(Squared)",4)]:
                print(f"--- by {pair[0]}")
                # get indices that would sort by each method
                sorted_indices = np.argsort(alignvalues[:, pair[1]])

                # print lowest five values
                for k in range(5):
                    av=alignvalues[sorted_indices[k]]
                    print("{:9s}    {:8d}\t{:8.2f}\t{:8.2f}\t{:8.2f}\t{:8d}\t{:8d}".format(
                        sample2ts(start_sampleB + av[0]),int(av[1]),av[2],av[3],
                        av[5], int(av[4]), int(av[0])))

        # ask if we should show the plots
        print("View graphs? [yN]", end='')
        choice = input().lower()
        if 'y' in choice:
            # show plots of results
            makeplot("AbsMin",a_signal,align_point_a,b_signal,align_point_b,half_test_window)

        # ask if we should add it to Audacity
        print(f"Add align point {apidx} to Audacity? [yN] ", end='')
        choice = input().lower()
        if 'y' in choice:
            Apoint = (start_sampleA + align_point_a)/samplerate
            Bpoint = align_point_b/samplerate
            print("Adding points to Audacity A: {Apoint} B: {Bpoint}")
            setlabels(f"script align point {apidx}", Apoint, Bpoint)
        else:
            print("No labels added to Audacity.")

        if False:
            # keeping this for further manual/automatic comparison
            targettime=27.274
            targetsample=int(targettime*samplerate)
            print(f"--- AND AT OUR TARGET {targettime}s ({targetsample})")
            for j in alignvalues:
                if j[0] != targetsample:
                    continue
                print("{:9s}    {:8d}\t{:8.2f}\t{:8.2f}\t{:8.2f}\t{:8d}".format(
                    sample2ts(start_sampleB + j[0]),j[1],j[2],j[3],
                    j[2]+j[3], j[0]))
                makeplot("AbsMin",a_signal,align_point_a,b_signal,targetsample,half_test_window)
            print("---")

    print("Analysis complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Audio file alignment')
    parser.add_argument('fileA', help='File A path and optional start/stop time in seconds (e.g. fileA, fileA:10, or fileA:10:500)')
    parser.add_argument('fileB', help='File B path and optional start/stop time in seconds (e.g. fileA, fileB:20, or fileB:20:600)')
    parser.add_argument('--alignpoints', type=int, default=5, help='Number of alignment points (default: 5)')
    parser.add_argument('--searchwindow', type=int, default=60, help='Search window in seconds (default: 60)')
    parser.add_argument('--testwindow', type=int, default=100, help='Test window in samples (default: 100)')
    parser.add_argument('--debug', action='store_true', help='Enable debug text')
    parser.add_argument('--invert', action='store_true', help='Invert signal B before adding signals to compare them')

    args = parser.parse_args()
    main(args)
