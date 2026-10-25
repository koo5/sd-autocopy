SD card autocopy
================

Copying photos from the SD card of my camera is a very common task. And always the
same pattern. Take the card out of the camera, put it into the sd card reader, wait
for the automount popup, browse to the picture directory, select all images, cut
all images, browse to my photo directory, create a new folder, name the folder,
move to the folder, paste the pictures into it, wait for the finished job.

Sound boring? Sure, it is!

Now, how to make this one a little smarter? Maybe scripting some of those steps is
a cool idea. So how would this work in a perfect world?

The idea
--------

Taking the SD card out of the camera and putting it into the SD card reader does not
seem to be easily scripted, that's why I focused on the other steps ;-). Okay, I'd
like to put the card into the reader, just wait for all the images to be moved and
then put the card back into the camera.

But how to reach this goal? Maybe you're interested in my solution...

One thing we need to know: When hotplugging devices to my Ubuntu system the kernel
tells the udev daemon about that new device. The kernel informs the udev daemon
about a lot of different events, like adding and removing hardware, volumes and so
on. But how to get closer to these events?

Digging into udev events
------------------------

There is an interesting tool named `udevadm` which can be used to monitor
the incoming events. Let's see which events are fired when I put the SD card into
the card reader:

    > udevadm monitor
    monitor will print the received events for:
    UDEV - the event which udev sends out after rule processing
    KERNEL - the kernel uevent

    KERNEL[1316365297.577602] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368 (mmc)
    KERNEL[1316365297.578091] add /devices/virtual/bdi/179:0 (bdi)
    UDEV [1316365297.579008] add /devices/virtual/bdi/179:0 (bdi)
    UDEV [1316365297.579058] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368 (mmc)
    KERNEL[1316365297.580552] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0 (block)
    KERNEL[1316365297.580644] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0/mmcblk0p1 (block)
    UDEV [1316365297.617556] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0 (block)
    UDEV [1316365298.314850] add /devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0/mmcblk0p1 (block)

These are a lot of of events, but there is only one needed, so it is needed to
choose the correct one. One way would be to try one event after each other, but
there is a better way: Now one need to know that it is possible to react on
specific events and run some script every time such an event is received. The
script is executed and has access to the whole environment which is populated
with some useful information.

To get a little more information create a matching udev rule and a small script
which appends the whole environment to a temporary dump file:

Store the udev rule in the rules directory `/etc/udev/rules.d/99-sd-autocopy.rules`:

    SUBSYSTEM=="block", ACTION=="add", RUN="/usr/local/bin/sd-autocopy"

The udev daemon should recognize that rule immediately. If a restart of the
udev daemon might help.

Now create the script to dump the environment at the path specified in the
udev rule `/usr/local/bin/sd-autocopy`:

    #!/bin/bash
    echo ======================== >> /tmp/test
    env >> /tmp/test

Then make the script executable:

    chmod +x /usr/local/bin/sd-autocopy

Now simply plug the SD card of your choice into your SD card reader and have a
look at the contents of `/tmp/test` afterwards. The file should contain
information of several events where the single events are separated by the
lines of equal sings.

Having these events recorded it is now possible to grab the important
information for the script. When looking at the single events we see that there
is one `ADD` event for the added disk and one `ADD` event for each partition.
We are only interested in the partition events and need the first partition of
the device, so add the following conditions to the udev rule:

    ENV{DEVTYPE}=="partition", ENV{UDISKS_PARTITION_NUMBER}=="1"

which should result in this rule:

    SUBSYSTEM=="block", ACTION=="add", ENV{DEVTYPE}=="partition", ENV{UDISKS_PARTITION_NUMBER}=="1", RUN="/usr/local/bin/sd-autocopy"

After changing the rule, deleting the `/tmp/test` file and an additional test
we can see that the test sd-autocopy script gets only called with the correct
event. This event comes with the following environment variables:

    DEVTYPE=partition
    SUBSYSTEM=block
    ID_SERIAL=0x000006d4
    ID_FS_UUID=F84E-1690
    UDISKS_PARTITION_OFFSET=4194304
    DEVPATH=/devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0/mmcblk0p1
    UDISKS_PARTITION_NUMBER=1
    ID_FS_VERSION=FAT32
    MINOR=1
    ACTION=add
    PWD=/
    UDISKS_PARTITION_ALIGNMENT_OFFSET=0
    ID_FS_TYPE=vfat
    ID_NAME=SDC
    MAJOR=179
    DEVLINKS=/dev/disk/by-id/mmc-SDC_0x000006d4-part1 /dev/disk/by-path/pci-0000:15:00.2-part1 /dev/disk/by-uuid/F84E-1690
    DEVNAME=/dev/mmcblk0p1
    SHLVL=1
    ID_FS_USAGE=filesystem
    ID_PART_TABLE_TYPE=dos
    UDISKS_PRESENTATION_NOPOLICY=0
    UDISKS_PARTITION_SIZE=16130244608
    ID_FS_UUID_ENC=F84E-1690
    UDISKS_PARTITION_SCHEME=mbr
    UDISKS_PARTITION_TYPE=0x0c
    UDISKS_PARTITION=1
    UDISKS_PARTITION_SLAVE=/sys/devices/pci0000:00/0000:00:1e.0/0000:15:00.2/mmc_host/mmc0/mmc0:b368/block/mmcblk0
    ID_PATH=pci-0000:15:00.2
    SEQNUM=2825
    _=/usr/bin/env

The sd-autocopy script
----------------------

Two scripts share the work. ``sd-autocopy`` (root, run by udev through
``systemd-run``) checks the partition's filesystem **label** against
``HANDLED_LABELS`` (labels survive camera reformats; filesystem UUIDs do
not), mounts it under ``/mnt/sd-autocopy/<utc stamp>`` owned by ``USER``,
and hands the mount point to ``sd-autocopy-copy``, run as that user.

``sd-autocopy-copy`` (Python 3, stdlib + ``exiftool``) copies every
``*.JPG|*.CR2|*.MOV`` on the card into two trees::

    ~/autobackup/<YYYY-MM-DD>/<DCIM/...>/<basename>      verbatim mirror
    ~/autocopy/<YYYY-MM-DD>/<ext>/<new name>             the pipeline workdir

Naming
~~~~~~

``<new name>`` is ``<YYYYMMDD>_<HHMMSS>_<mmm>Z_<camtoken>_<basename>``, e.g.
``20260915_125724_990Z_5ds_AAAE2634.CR2``:

* the capture instant in **UTC** — EXIF ``DateTimeOriginal`` +
  ``SubSecTimeOriginal`` converted through the camera's stamped
  ``OffsetTimeOriginal`` (MOV: the same tags, or QuickTime ``CreateDate``).
  Names therefore sort in capture order whatever the card's DCIM folders,
  wherever the frame counter wrapped (9999→0001 opens a new folder on
  Canon bodies — all folders of one card merge into one workdir), and
  whichever zone the camera or this host was set to. Machines in different
  zones feed one archive without disagreeing.
* a **camera token**: the alias configured for the body's serial, else
  ``<ModelShort>-<Serial>`` (``5DS-363055000036``). The workdir itself is
  single-body; a second body on the same day is split out by hand before
  the pipeline runs.
* the camera's own **basename**, untouched: the pipeline's bracket
  detection reads the frame counter from it, and it greps straight back to
  the card and ``autobackup``.

The date dir is the same UTC day. Caveat, accepted on purpose: a late
evening in Central Europe files under the next UTC day. A still with no
``OffsetTimeOriginal`` (an older body, a foreign file) is still copied —
its wall clock is taken as UTC and an alert is raised so the camera
setting gets fixed; the pipeline refuses zone-less frames anyway.

Flow and failure
~~~~~~~~~~~~~~~~

Per file, two stages, so ``exiftool`` never reads the card:

1. the copier (serial): card → ``autobackup/.../.<name>.partial``, size
   check, rename into place, fsync;
2. the namer (a small thread pool): ``exiftool`` on the local copy → the
   name; re-home the backup if its UTC day differs from the mtime guess;
   reflink-mirror into ``autocopy`` (again partial + rename); **only then**
   move the card file into ``<folder>/old/`` so the next insertion skips it.

Both destinations are placed atomically, so a final name never holds a
partial copy: a destination that already exists with the right size counts
as copied (re-inserting a card after a crash resumes cleanly), a different
size under the same name aborts rather than overwrites.

A failing read means the card or reader is dropping out, and there is no
recovering from that by moving on to the next file: the first failure of
either stage aborts the whole run, leaves the card as it is, logs ``ABORT``
to syslog and appends an alert through ``ALERT_PY`` (the synced per-host
alert tree, visible from any machine; clear it by hand once acted on).

Config
~~~~~~

``~/.config/sd-autocopy/env`` (``KEY=VALUE`` lines; all optional)::

    AUTOCOPY_TARGET=~/autocopy
    AUTOCOPY_BACKUP=~/autobackup
    ALERT_PY=/home/koom/hackery2/src/hackery2/lib/alert.py
    WORKERS=2
    CAM_ALIAS_363055000036=5ds        # camera token per body serial

Tokens are ``[A-Za-z0-9-]+`` — ``_`` is the name separator. An alias that
breaks that rule aborts the run at config load.

Running by hand
~~~~~~~~~~~~~~~

::

    sd-autocopy-copy --dry-run /mnt/sd-autocopy/<stamp>    # print the plan, write nothing
    sd-autocopy-copy -v /mnt/sd-autocopy/<stamp>           # copy, echoing the syslog lines
    journalctl -t sd-autocopy -f                            # watch an automatic run

Tests: ``uv run --locked --script tests/test_copy.py`` (a fake card of tiny
JPEGs stamped with exiftool; nothing touches the real trees).

Install: this checkout is canonical. The udev rule runs ``sd-autocopy``
from here by absolute path and the root script finds ``sd-autocopy-copy``
next to itself, so the scripts are never copied anywhere (a branch switch
changes what udev runs; anything left under ``/usr/local/bin`` is dead).
Only the rule is installed::

    sudo cp 99-sd-autocopy.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules
