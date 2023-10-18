import numpy as np
from pydub import AudioSegment
import pydub
#import soundfile as sf
import resampy
import argparse
import datetime
import sys
import matplotlib.pyplot as plt

samplerate = 0      # sample rate for comparisons

def parse_file_and_startstop(arg):
    parts = arg.split(":")
    if len(parts) == 1:
        return parts[0], 0, -1
    elif len(parts) == 2:
        return parts[0], int(parts[1]), -1
    else:
        return parts[0], int(parts[1]), int(parts[2])

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

def print_wavesum(label, value, ts_a, ts_b):
    global samplerate

    print("{:6s} WaveSum: {:9f}, Timestamps A: {:8.3f} ({}) B: {:8.3f}".format(
        label, value, ts_a/samplerate, sample2ts(ts_a),
        ts_b/samplerate))

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
    min = int(sample/(samplerate*60))
    sec = int(sample/samplerate)%60
    msec = samplerate%1000
    return f"{min:2d}:{sec:02d}.{msec:03d}"

def main(args):
    global samplerate

    # Read arguments
    fileA, start_timeA, stop_timeA = parse_file_and_startstop(args.fileA)
    fileB, start_timeB, stop_timeB = parse_file_and_startstop(args.fileB)
    align_points = args.alignpoints
    search_window = args.searchwindow
    test_window = args.testwindow
    half_test_window = test_window // 2

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
    print(f"a_audio first 10 samples: {a_audio[:10]}")

    # Equalize sample rate
    if (a_audio.frame_rate < b_audio.frame_rate):
        print(f"Resampling A from {a_audio.frame_rate/1000}kHz to {b_audio.frame_rate/1000}kHz")
        b_audio = b_audio.set_frame_rate(a_audio.frame_rate)
    elif (a_audio.frame_rate > b_audio.frame_rate):
        print(f"Resampling B from {b_audio.frame_rate/1000}kHz to {a_audio.frame_rate/1000}kHz")
        a_audio = a_audio.set_frame_rate(b_audio.frame_rate)

    # Normalize volumes
    print(f"Max volume A: {a_audio.max} B: {b_audio.max} -- equalizing")
    if (a_audio.max > b_audio.max):
        print("Adjusting A to B volume")
        normalize_volume(b_audio, a_audio.max)
    elif (a_audio.max < b_audio.max):
        print("Adjusting B to A volume")
        normalize_volume(a_audio, b_audio.max)

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

    # Step for alignment points
    step_a = len(a_signal) // align_points

    # Convert search window value from seconds to samples
    search_window_samples = search_window * samplerate

    # Step for alignment points
    step_a = len(a_signal) // align_points
    
    for i in range(align_points):
        align_point_a = i * step_a

        max_wave_sum = float("-inf")
        min_wave_sum = float("inf")
        absmin_wave_sum = float("inf")
        max_ts_a = max_ts_b = 0
        min_ts_a = min_ts_b = 0
        absmin_ts_a = absmin_ts_b = 0

        # we don't want test windows to wrap around the array
        if (align_point_a < half_test_window):
            align_point_a = half_test_window
        elif (align_point_a + half_test_window > len(a_signal)):
            align_point_a = len(a_signal) - half_test_window

        print(f"align point a: {align_point_a}")
        start_b = max(half_test_window, align_point_a - search_window_samples)
        print(f"start_b {start_b} = max({half_test_window}, {align_point_a} - {search_window_samples})")
        end_b = min(len(b_signal)-half_test_window,
                    align_point_a + search_window_samples)

        now = datetime.datetime.now()
        print(f"\nChecking align point {i} : "
              +sample2ts(align_point_a)
              +" -- B "+sample2ts(start_b)+" - "+sample2ts(end_b)
              +f"({end_b - start_b} samples) : "
              +now.strftime("%H:%M:%S"))

        for j in range(start_b, end_b):
            # print(f"wave_sum = np.sum(a_signal[{align_point_a-half_test_window}:{align_point_a+half_test_window}] + b_signal[{j-half_test_window}:{j+half_test_window}])")
            # wave_sum = np.sum(a_signal[align_point_a-half_test_window:align_point_a+half_test_window] + b_signal[j-half_test_window:j+half_test_window])
            sum_array = np.sum(a_signal[align_point_a-half_test_window:align_point_a+half_test_window] + b_signal[j-half_test_window:j+half_test_window])
            #wave_sum = np.mean(sum_array)
            # mean doesn't do it -- maybe mean, plus std deviation, or just
            # std deviation?
            #wave_sum = np.std(sum_array)
            # no
            l1_norm = np.sum(np.abs(sum_array))
            std_dev = np.std(sum_array)
            wave_sum = l1_norm + std_dev

            if wave_sum > max_wave_sum:
                max_wave_sum = wave_sum
                max_ts_a = align_point_a
                max_ts_b = j

            if wave_sum < min_wave_sum:
                min_wave_sum = wave_sum
                min_ts_a = align_point_a
                min_ts_b = j

            if abs(wave_sum) < absmin_wave_sum:
                absmin_wave_sum = abs(wave_sum)
                absmin_ts_a = align_point_a
                absmin_ts_b = j

        print_wavesum("Max", max_wave_sum, start_sampleA + max_ts_a,
                             start_sampleB + max_ts_b)
        print_wavesum("Min", min_wave_sum, start_sampleA + min_ts_a,
                             start_sampleB + min_ts_b)
        print_wavesum("AbsMin", absmin_wave_sum, start_sampleA + absmin_ts_a,
                             start_sampleB + absmin_ts_b)

        if True:
            fig, (ax1, ax2) = plt.subplots(1, 2)
            fig.suptitle('AbsMin Waves A and B')
            plt.ylabel("Freq")
            ax1.set_ylim(-8000,8000)
            ax2.set_ylim(-8000,8000)
            ax1.axhline(0) #y-axis line
            ax2.axhline(0) #y-axis line

            print(f"A range: {absmin_ts_a-half_test_window}-{absmin_ts_a+half_test_window}")
            ax1.plot(range(absmin_ts_a-half_test_window,absmin_ts_a+half_test_window),
                     a_signal[absmin_ts_a-half_test_window:absmin_ts_a+half_test_window])
            ax2.plot(range(absmin_ts_b-half_test_window,absmin_ts_b+half_test_window),
                     b_signal[absmin_ts_b-half_test_window:absmin_ts_b+half_test_window])
        if False:
            plt.figure(1)
            time = np.linspace(
                absmin_ts_a-half_test_window, # start
                test_window / samplerate,
                num = test_window
            )
            plt.plot(time, 
                a_signal[absmin_ts_a-half_test_window:absmin_ts_a+half_test_window])

        plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Audio file alignment')
    parser.add_argument('fileA', help='File A path and optional start/stop time in seconds (e.g. fileA, fileA:10, or fileA:10:500)')
    parser.add_argument('fileB', help='File B path and optional start/stop time in seconds (e.g. fileA, fileB:20, or fileB:20:600)')
    parser.add_argument('--alignpoints', type=int, default=5, help='Number of alignment points (default: 5)')
    parser.add_argument('--searchwindow', type=int, default=60, help='Search window in seconds (default: 60)')
    parser.add_argument('--testwindow', type=int, default=100, help='Test window in samples (default: 100)')

    args = parser.parse_args()
    main(args)
