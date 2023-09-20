#!/usr/bin/perl -w

use strict;

# only one destination now, just fix it to that

my $newpath = "/Volumes/Media/Netradio/sources/";

my $unixpath = quotemeta("/net/freenas/mnt/Reliant/Media/Music/_new/jaz_links/");

my $winpath = quotemeta('D:\\tim\\Music\\netradioDNB\\');

#	    my $replace = "aliasfile=\"D:\\tim\\Music\\netradioDNB\\$f\"";
#	    s/aliasfile=\"\/net.*\/[^\/]+?\"/$replace/;
#	    my $replace = "aliasfile=\"$unixpath$f\"";
#	    s/aliasfile=\"D:\\tim\\Music\\netradioDNB\\([^\\]+?)\"/$replace/;
#	elsif (/(aliasfile=)(.+?)\"/) {

while (@ARGV) {
    my $f = shift @ARGV;
    my $out = "";
    open(IN, $f) or die "Can't open $f: $!";
    while (<IN>) {
	if (/aliasfile=/) {
	    if (/$unixpath/) {
		s/$unixpath/$newpath/;
	    }
	    elsif (/$winpath/) {
		s/$winpath/$newpath/;
	    }
	    else {
		print "WARNING: unexpected path: $_";
	    }
	}
	$out .= $_;
    }
    close (IN);
    open (OUT, "> $f") or die "can't open/w $f: $!";
    print OUT $out;
    close (OUT);
}
