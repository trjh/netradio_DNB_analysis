#!/usr/bin/perl -w -pi.orig

use strict;

my $unixpath = "/net/freenas/mnt/Reliant/Media/Music/_new/jaz_links/";
my %namemap = ();
&setnamemap;
print STDERR "namemap keys: ".(scalar keys %namemap)."\n";
my %revnamemap = ();
while (my ($k, $v) = each %namemap) {
    $revnamemap{$v} = $k;
}

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

my %mapped = ();	# filenames we've already mapped

while (<>) {
    if ($HOSTNAME eq "M00845") {
	if (/aliasfile=\"\/net.*\/([^\/]+?)\"/) {
	    my $f = $1;
	    if (defined($revnamemap{$f})) {
		unless (defined($mapped{$f})) {
		    print STDERR "revmap $f $revnamemap{$f}\n";
		    $mapped{$f}++;
		}
		$f = $revnamemap{$f};
	    }
	    my $replace = "aliasfile=\"D:\\tim\\Music\\netradioDNB\\$f\"";
	    s/aliasfile=\"\/net.*\/[^\/]+?\"/$replace/;
	}
	elsif (/(aliasfile=)(.+?)\"/) {
	    print STDERR "NON-MATCHING ALIASFILE: $1 $2\n";
	}
	# heavy DEBUG # print STDERR "..$_";
    }
    elsif ($HOSTNAME eq "timmbp") {
	if (/aliasfile=\"D:\\tim\\Music\\netradioDNB\\([^\\]+?)\"/) {
	    my $f = $1;
	    if (-f "$unixpath$f") {
		# don't make any changes
	    }
	    elsif (defined($namemap{$f})) {
		unless (defined($mapped{$f})) {
		    print STDERR "map $f $namemap{$f}\n";
		    $mapped{$f}++;
		}
		$f = $namemap{$f};
	    }
	    my $replace = "aliasfile=\"$unixpath$f\"";
	    s/aliasfile=\"D:\\tim\\Music\\netradioDNB\\([^\\]+?)\"/$replace/;
	}
	elsif (/(aliasfile=)(.+?)\"/) {
	    print STDERR "NON-MATCHING ALIASFILE: $1 $2\n";
	}
    }
    print;
} continue {
    if (eof()) {
	close ARGV;
	exit;
    }
}

exit;

sub setnamemap
{
    #
    # tim-timmbp(netradio-all)% ls -l /Media/Music/_new/jaz_links | \
    # 	cut -f14- -d" " | \
    # 	perl -ne 'if (s/@ -> .*\//\t/) {print}' | \
    # 	perl -ne \
    # 	'@s=split; if ($s[0] ne $s[1]) { print "\"$s[0]\" => \"$s[1]\",\n" }'
    #
    %namemap = (
	"d-14Nov10-a.au" => "d-14Nov10-a",
	"d-14Nov10-b.au" => "d-14Nov10-b",
	"d-14Nov10-c.au" => "d-14Nov10-c",
	"d-14Nov10-d.au" => "d-14Nov10-d",
	"d-14Nov10-e.au" => "d-14Nov10-e",
	"d-14Nov10-f.au" => "d-14Nov10-f",
	"d-14Nov10-h.au" => "d-14Nov10-h",
	"d-25-000b.wav" => "-25-000b.wav",
	"d-25-005b.wav" => "-25-005b.wav",
	"d000-018.wav" => "dnb000-018.wav",
	"d001-026b.wav" => "001-026b.wav",
	"d019-040.wav" => "dnb019-040.wav",
	"d026-073b.wav" => "026-073b.wav",
	"d041-064.wav" => "dnb041-064.wav",
	"d064-083b.wav" => "064-083b.wav",
	"d065-087.wav" => "dnb065-087.wav",
	"d084-103b.wav" => "084-103b.wav",
	"d088-107.wav" => "dnb088-107.wav",
	"d104-108b.wav" => "104-108b.wav",
	"d107-121b.wav" => "107-121b.wav",
	"d122-144b.wav" => "122-144b.wav",
	"d145-164b.wav" => "145-164b.wav",
	"d165-186b.wav" => "165-186b.wav",
	"d187-216b.wav" => "187-216b.wav",
	"d217-242.wav" => "dnb217-242.wav",
	"d242-268.wav" => "dnb242-268.wav",
	"d269-271b.wav" => "269-271b.wav",
	"d276-293b.wav" => "276-293b.wav",
	"d316-335.wav" => "dnb316-335.wav",
	"d336-355.wav" => "dnb336-355.wav",
	"d356-375.wav" => "dnb356-375.wav",
	"d376-395.wav" => "dnb376-395.wav",
	"d396-415.wav" => "dnb396-415.wav",
	"d416-435.wav" => "dnb416-435.wav",
	"d425-438b.wav" => "425-438b.wav",
	"d436-455.wav" => "dnb436-455.wav",
	"d456-470.wav" => "dnb456-470.wav",
	"d505-531b.wav" => "505-531b.wav",
	"tracklist" => "netradio",
    );
}
