#!/usr/bin/perl -w

use strict;

while (@ARGV) {

    $in = shift @ARGV;

#    <wavetrack name="dnb376-395" channel="0" linked="1" mute="0"...
#	<waveclip offset="0.00000000">
#	    <label t="0009.1877013544" t1="0009.1877013544" title="second"/>

    open (IN, $in) or die "can't open/r $in: $!\n";

    my ($wavetrack, $offset
    while (<IN>) {

grep label d376-395.aup | perl -ne 's/^\s+//; s/\&quot\;/\"/g; if (/<label t="(\d+.\d+)" .*? title="(.*)"\/>/) { $m=int($1/60); $s=$1-($m*60); printf "\t%02d:%06.3f .. %s\n", $m, $s, $2 }' | less
