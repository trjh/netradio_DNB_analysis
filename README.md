# Netradio Drum and Bass ISDN - mix analysis

## Introduction

In 1998 I discovered Drum & Bass courtesy of a netradio.com station called
something like "Drum & Bass ISDN". I was completely entranced by these dark
intelligent quiet/driving tunes.  The thing was, a lot of music was long DJ
mixes and there were no annotations.  I became obsessed, documented a lot of
it by sounds and clips of lyrics, and wrote some hacky code to record the
RealAudio stream on my Sun workstation.

The station was an almost nine hour loop of music, and I believe I captured
the entirety of it in 70 WAV and AU format files, comprising almost 23 hours
of audio.  I've listened to these files a lot over the last 25 years, and
along the way I found a lot of the original music.  However, over the last few
years I decided to try to identify the whole playlist.

I started this project in 2017, loading the files into [Audacity](https://www.audacityteam.org/),
making notes, piping audio into Shazam and other music ID programs, and
eventually starting to compare the mix with the original tracks to learn more
about start/stop points and rate changes.  I tracked the notes in a text file,
but when I picked this up again I decided a Google Sheet would be a better
help in summarizing and cross-referencing the information.

## What's here

* [tracklog-1998.txt](./tracklog-1998.txt) -- The text file I used in 1998 to try and ensure I'd recorded the entire stream
* [tracklist-2017.txt](./tracklist-2017.txt) -- Notes on stream details, tracklist, and clues to unknown songs
* [audacity](./audacity) -- Audacity version 2.1.x metadata files from 2017.  Audacity 3.3 stores data in one big file,
  so these remain as 2017 artifacts.
* [labels](./labels) -- Audacity 3.x label export files -- notes on file start/stop, track start/stop, sync points with original tracks, etc.
* [logo](./logo) -- netradio.com logo files retrieved from archive.org and other places
* [scripts](./scripts) -- misc. helper scripts mostly oriented around Audacity 2.1.x metadata files

## TODO

What I'd like to accomplish
* [ ] Determine complete tracklist
* [ ] Build playlist of tracks from the stream (as much as possible) on YouTube, Apple Music, and Soundcloud
* [ ] Compile definitive recording of stream, perhaps in five 1-2 hour chunks
* [ ] Publish recording on YouTube and Soundcloud -- it's not very high quality (16kHz, originally [RealAudio](https://en.wikipedia.org/wiki/RealAudio)) so I doubt I'll be chased for copyright claims, and maybe the original DJ will appear to tell us more about the mix.

It's also slightly tempting to think about remaking the mix with better quality original sources, but that's probably a step too far.

## Links

* [YouTube Playlist](https://www.youtube.com/playlist?list=PLei572m3gA_kAghvCs4L5pbCZjmzi5Hhh)
