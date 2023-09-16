#!/usr/bin/perl -w -pi.orig

use strict;

my $unixpath = "/net/freenas/mnt/Reliant/Media/Music/_new/jaz_links/";

my $HOSTNAME = `hostname -s`;
chomp ($HOSTNAME);
print STDERR "hostname is $HOSTNAME\n";
if ($HOSTNAME eq "M00845") {
    print STDERR "unix -> windows conversion\n";
} elsif ($HOSTNAME eq "timmbp") {
    print STDERR "windows -> unix conversion\n";
} else {
    print STDERR "not windows/os-x\n";
}

# unless we do this the first line seems to go missing
print;
while (<>) {
    if ($HOSTNAME eq "M00845") {
	if (/(aliasfile=\")\/net.*\/([^\/]+)\"/) {
	    # no operation..
	    # print STDERR ".$1 $2\n";
	} elsif (/(aliasfile=)(.+?)\"/) {
	    print STDERR "NON-MATCHING ALIASFILE: $1 $2\n";
	}
	s/(aliasfile=\")\/net.*\/([^\/]+)\"/$1D:\\tim\\Music\\netradioDNB\\$2\"/;
	# heavy DEBUG # print STDERR "..$_";
    }
    elsif ($HOSTNAME eq "timmbp") {
	s/(aliasfile=\")D:\\tim\\Music\\netradioDNB\\([^\\]+)\"/$1$unixpath$2\"/;
	s/d-14Nov10-(.)\"/d-14Nov10-$1.au\"/g;
	s/dnb-14Nov02-(.)\"/dnb-14Nov02-$1.au\"/g;
	if (/(d-14Nov10-.)\"\s/) { print STDERR ".$1\n" }
    }
    print;
} continue {
    if (eof()) {
	close ARGV;
	exit;
    }
}
