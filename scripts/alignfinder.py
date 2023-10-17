import numpy as np
import soundfile as sf
import resampy
import argparse
import datetime
import sys

def parse_file_and_startstop(arg):
    parts = arg.split(":")
    if len(parts) == 1:
        return parts[0], 0, -1
    elif len(parts) == 2:
        return parts[0], int(parts[1]), -1
    else:
        return parts[0], int(parts[1]), int(parts[2])

def main(args):
    # Read arguments
    fileA, start_timeA, stop_timeA = parse_file_and_startstop(args.fileA)
    fileB, start_timeB, stop_timeB = parse_file_and_startstop(args.fileB)
    align_points = args.alignpoints
    search_window = args.searchwindow
    test_window = args.testwindow

    # Load tracks
    try:
        a_signal, a_samplerate = sf.read(fileA)
    except Exception as e:
        sys.exit(f"Cannot read {fileA}: {e}")

    try:
        b_signal, b_samplerate = sf.read(fileB)
    except Exception as e:
        sys.exit(f"Cannot read {fileA}: {e}")
    
    print(f".Start: a has {len(a_signal)} samples, or {int(len(a_signal)/(a_samplerate*60))}:{int(len(a_signal)/a_samplerate)%60} at {a_samplerate/1000}kHz")
    print(f".       b has {len(b_signal)} samples, or {int(len(b_signal)/(b_samplerate*60))}:{int(len(b_signal)/b_samplerate)%60} at {b_samplerate/1000}kHz")

    # Truncate based on end time if necessary
    if stop_timeA != -1:
        a_signal = a_signal[:int(stop_timeA * a_samplerate)]
    if stop_timeB != -1:
        b_signal = b_signal[:int(stop_timeB * b_samplerate):]

    print(f".wStop: a has {len(a_signal)} samples, or {int(len(a_signal)/(a_samplerate*60))}:{int(len(a_signal)/a_samplerate)%60} at {a_samplerate/1000}kHz")
    print(f".       b has {len(b_signal)} samples, or {int(len(b_signal)/(b_samplerate*60))}:{int(len(b_signal)/b_samplerate)%60} at {b_samplerate/1000}kHz")

    # Truncate based on start time if necessary
    a_signal = a_signal[int(start_timeA * a_samplerate):]
    b_signal = b_signal[int(start_timeB * b_samplerate):]

    print(f".wStrt: a has {len(a_signal)} samples, or {int(len(a_signal)/(a_samplerate*60))}:{int(len(a_signal)/a_samplerate)%60} at {a_samplerate/1000}kHz")
    print(f".       b has {len(b_signal)} samples, or {int(len(b_signal)/(b_samplerate*60))}:{int(len(b_signal)/b_samplerate)%60} at {b_samplerate/1000}kHz")

    # Resample track B to 16kHz
    sys.stderr.write("We should resample to whatever the lower of the two is\n")
    if b_samplerate != 16000:
        b_signal = resampy.resample(b_signal, b_samplerate, 16000)
        b_samplerate = 16000
        sys.stderr.write("Track B resampled to 16kHz\n")

    # Convert search window value from seconds to samples
    search_window_samples = search_window * 16000

    # Step for alignment points
    step_a = len(a_signal) // align_points
    
    max_wave_sum = float("-inf")
    min_wave_sum = float("inf")
    max_ts_a = max_ts_b = 0
    min_ts_a = min_ts_b = 0
    
    for i in range(align_points):
        align_point_a = i * step_a

        start_b = max(0, align_point_a - search_window_samples)
        end_b = min(len(b_signl), align_point_a + search_window_samples)

        now = datetime.now()
        print(f"Checking align point {i} : {int(align_point_a/(a_samplerate*60))}:{int(align_point_a/a_samplerate)%60}"
              +f"-- testing {end_b - start_b} samples : "
              +now.strftime("%H:%M:%S"))

        for j in range(start_b, end_b):
            half_test_window = test_window // 2
            wave_sum = np.sum(a_signal[align_point_a-half_test_window:align_point_a+half_test_window] + b_signal[j-half_test_window:j+half_test_window])

            if wave_sum > max_wave_sum:
                max_wave_sum = wave_sum
                max_ts_a = align_point_a
                max_ts_b = j

            if wave_sum < min_wave_sum:
                min_wave_sum = wave_sum
                min_ts_a = align_point_a
                min_ts_b = j

    print(f"Max WaveSum: {max_wave_sum}, Timestamps: A({max_ts_a}), B({max_ts_b})")
    print(f"Min WaveSum: {min_wave_sum}, Timestamps: A({min_ts_a}), B({min_ts_b})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Audio file alignment')
    parser.add_argument('fileA', help='File A path and optional start/stop time in seconds (e.g. fileA, fileA:10, or fileA:10:500)')
    parser.add_argument('fileB', help='File B path and optional start/stop time in seconds (e.g. fileA, fileB:20, or fileB:20:600)')
    parser.add_argument('--alignpoints', type=int, default=5, help='Number of alignment points (default: 5)')
    parser.add_argument('--searchwindow', type=int, default=60, help='Search window in seconds (default: 60)')
    parser.add_argument('--testwindow', type=int, default=100, help='Test window in samples (default: 100)')

    args = parser.parse_args()
    main(args)
