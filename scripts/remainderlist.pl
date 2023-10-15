#!/usr/bin/perl -w

use strict;

my $lastts = -1;
my $lastlabel = "";
my $skipto = 15516;	# don't print timestamps before this one
my $tracknum = 51;	# start track numbers as this +1
my $debug = 0;

# 399:27	08:12.994 :: Ja Know Ya Big / Dillinja
# 404:35	13:20.829 :: The Flute Tune / Hidden Agenda
#     ~15:31	  :: Is It Love?

open(IN, "../tracklist-2017.txt") or die;
open(OUT, "> remainder.tsv") or die;

sub printnote($$) {
    my $t = shift @_;
    my $l = shift @_;
    $tracknum++;
    my $out = sprintf("%d\t%d\tstart%03d: ID: %s\n", $t, $t, $tracknum, $l);
    print $out;
    print OUT $out;
}

while(<IN>) {
    print ".$_" if ($debug>1);
    if (/::/) {
	print ".$_" if ($debug==1);
    }
    chomp;
    if (/^\s*(~?[\d:.]+)\s+(~?[\d:.]+)\s+::\s+(.+)/) {
	my $ts1 = $1;
	my $ts2 = $2;
	my $label = $3;
	my $ts = -1;
	if ($lastlabel && ($lastts>$skipto)) {
	    printnote($lastts, $lastlabel);
	}
	if ($ts1 =~ /^(\d+):(\d+)/) {
	    $ts = $1 * 60 + $2;
	} else {
	    printf("WARNING: invalid timestamp: $ts1 -- line: $_");
	    next;
	}
	$lastts = $ts;
	$lastlabel = $label;
    }
    elsif (/^\s+(~?[\d:.]+)?\s+::\s+(.+)/) {
	my $add = $2;
	if ($lastlabel !~ / $/) {
	    $lastlabel .= " ";
	}
	$lastlabel .= $add;
    }
}
close(IN);
if ($lastlabel && ($lastts>$skipto)) {
    printnote($lastts, $lastlabel);
}

__DATA__

if false; then
    cat auplist | \
    while read f; do
	rf=${f%%	*};
	rfn=${rf##*/};
	ts=${f##*	};
	# echo RF: $rf TS: $ts
	tsm=${ts%%:*}
	tss=${ts##*:}
	# echo TSM: $tsm TSS: $tss
	seconds=$(( ${tsm} * 60 + $tss ))
	echo "### new file"
	echo "0.0	0.0	file start sync: $rfn ${seconds}.0 NOT VERIFIED start $ts"
	../scripts/getlabels.pl $rf;
    done | \
    tee remainder.tsv
fi

grep :: ../tracklist-2017.txt | \
    echo $entry | perl -pe 'if (/^([\d.]+)\s+([\d.]+)\s+(.
    while read entry; do
	if [[ $entry = "^ *::" ]]; then
	    echo "continue line: $entry"
	else
	    echo "ts line: $entry"
	fi
    done
	
=== auplist
../audacity/d145-164.aup	144:16
../audacity/d165-186.aup	164:36
../audacity/d-14Nov10-a.aup	180:52
../audacity/d187-200.aup*	186:16
../audacity/d-14Nov10-b.aup	200:52
../audacity/d-14Nov10-c.aup	220:52
../audacity/d-14Nov10-d.aup*	240:52
../audacity/d-14Nov10-e.aup*	260:52
../audacity/d288-307.aup	287:16
../audacity/d308-327.aup	307:15
../audacity/d328-342.aup	327:15
../audacity/d336-355.aup	331:14
../audacity/d376-395.aup	371:14
../audacity/d416-435.aup	411:14
../audacity/d456-504.aup	451:14
