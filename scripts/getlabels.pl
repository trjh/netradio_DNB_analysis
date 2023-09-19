#!/usr/bin/perl -w

use strict;

while (@ARGV) {
    my $f = shift @ARGV;
    open(IN, $f) or die "Can't open $f: $!";
    while (<IN>) {
	if (/<label\s+t=\"([0-9.]+)\"\s+t1=\"([0-9.]+)\"\s+title=\"([^\"]+)\"/)
	{
	    print "$1\t$2\t$3\n"
	}
    }
    close (IN)
}
