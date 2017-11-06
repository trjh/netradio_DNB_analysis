#!/usr/bin/perl -w

use strict;
use Data::Dumper;

# pull samples from labels that mention shazam

#                     (via http://audiotag.info/index.php,
#                      http://homepage/d019-040024.mp3)
#                     sox d041-064.wav d041-064.mp3 trim 0 45 : newfile : restart
#                     scp *.mp3 homepage
#                     perl -e '$t=0; for ($i=1; $i<=33; $i++) { $end=$t+45;
#                     printf "file %2d: %02d:%02d - %02d:%02d\n", $i,
#                     int($t/60), $t%60, int($end/60), $end%60; $t=$end }'

#  <wavetrack name="dnb336-355" channel="0" linked="1" mute="0" solo="0"
#  height="159" minimized="0" isSelected="0" rate="16000" gain="1.0"
#  pan="0.0">
#   <waveclip offset="0.00000000">
#     <sequence ...>
#      <waveblock start="0">
#      <pcmaliasblockfile summaryfile="e0002448.auf" aliasfile="/net/freen...
#      ../>
#      </waveblock>
#     </sequence>
#   </waveclip>
#   </wavetrack>
#
# from the above, save track name, filename, and offset
#
# <label t="497.4980453474" t1="497.4980453474" title="shazam unknown"/>
# so -- 497 + 45sec in filename

my $clips = 0; 		# clip count
my @clips = ();
    # clips to get -- key=$clips+1, value { start=seconds, filename }
    # also value: {labelsec = t, labeltitle }
my %tracks = ();
    # tracks seen -- key is offset in seconds
    # value: { name, filename }

unless (defined($ARGV[0])) {
    die "Usage: $0 [filename.aup]\n";
}
my $input = $ARGV[0];

open IN, $input or die;
my ($wavetrackname, $offset) = ("", -1); # current wavetrack values

while (<IN>) {
    chomp;

    if (/^\s*<wavetrack\s.*\bname="([^"]+)"/) {
	$wavetrackname = $1;
    }
    elsif (/^\s*<waveclip\s.*\boffset="([^"]+)"/) {
	$offset = $1;
	$tracks{$offset}->{"name"} = $wavetrackname;
    }
    elsif (/^\s*<pcmaliasblockfile\s.*\baliasfile="([^"]+)"/) {
	$tracks{$offset}->{"filename"} = $1;
    }
    elsif (/^\s*<\/waveclip/) {
	($wavetrackname, $offset) = ("", -1); # current wavetrack values
    }
    elsif (/^\s*<label\s+(.*)/) {
	my $search = $1;
	my $clipid = $clips;
	while ($search =~ /^\s*(\w+)="([^"]+)"\s*(.*)$/) {
	    my ($key, $value) = ($1, $2);
	    $search = $3;
	    #print "KVS: $key / $value / $search\n";
	    if ($key eq "t") {
		$clips[$clipid]->{"labelsec"} = $value;
	    }
	    elsif ($key eq "title") {
		print ".$value\n";
		$clips[$clipid]->{"labeltitle"} = $value;
		# only move on to the next clip if this has a string we want
		if ($clips[$clipid]->{"labeltitle"} =~ /shazam/i) {
		    $clips++;
		    print "will check label ".$clips[$clipid]->{"labelsec"}.
			  ": ".$clips[$clipid]->{"labeltitle"}."\n";
		}
	    }
	}
    }
}
close (IN);

# now align clips with tracks
my @offsets = sort { $a <=> $b } keys %tracks;
print "tracks\n------\n";
foreach my $o (@offsets) {
    printf "%6d : %s\n", $o, $tracks{$o}->{"filename"};
}

CLIP: for (my $i = 0; $i <= $#clips; $i++) {
    my $labeltime = $clips[$i]->{"labelsec"};
    OFFSET: for (my $oindex = 0; $oindex < $#offsets; $oindex++) {
	my $tracko = $offsets[$oindex];
	my $tracko_next = $offsets[$oindex+1];
	if (($labeltime > $tracko) && ($labeltime < $tracko_next)) {
	    # must be in this track
	    my $tracktime = $labeltime - $tracko;
	    $clips[$i]->{"start"} = $tracktime;
	    $clips[$i]->{"filename"} = $tracks{$tracko}->{"filename"};
	    printf "clip %2d set:  %s\n", $i,$clips[$i]->{"labeltitle"};
	    # print "track set: ".Dumper($clips[$i])."\n";
	    next CLIP;
	}
    }
    # if we're here, no match yet, so it's in the last track
    my $tracko = $offsets[$#offsets];
    my $tracktime = $labeltime - $tracko;
    $clips[$i]->{"start"} = $tracktime;
    $clips[$i]->{"filename"} = $tracks{$tracko}->{"filename"};
    # print "track set:: ".Dumper($clips[$i])."\n";
    printf "clip %2d set:: %s\n", $i,$clips[$i]->{"labeltitle"};
}

print "\n";
CLIP: for (my $i = 0; $i <= $#clips; $i++)
{
    if (defined($clips[$i]->{"start"})) {
	my $s = int($clips[$i]->{"start"});
	my $f = $clips[$i]->{"filename"};
	$f =~ s/^.*\///;
	$f =~ s/^.*\\//;

	printf "sox %s %s trim %d 45\n\t# %s\n",
	    $f, "sample".$i."_$s.mp3", $s, $clips[$i]->{"labeltitle"};
    }
}

