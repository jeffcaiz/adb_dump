#!/bin/sh
# ubimount.sh — mount or extract a dumped <vol>.ubi via the REAL kernel UBI/UBIFS
# stack. Authoritative: works on images that ubireader 0.8.x fails to parse.
#
# Needs: Linux, root, mtd-utils (ubiformat/ubiattach/ubidetach/flash_erase), and the
# stock kernel modules nandsim/ubi/ubifs. Assumes the host has no *real* MTD/UBI
# (true for a normal PC) — it loads nandsim as mtd0 and tears the whole stack down.
#
#   sudo ./ubimount.sh <image.ubi> [mountpoint]   # mount read-only, leave it mounted
#   sudo ./ubimount.sh -x <image.ubi> <dest-dir>  # copy all contents to dest, then clean up
#   sudo ./ubimount.sh -u                          # unmount + detach + unload (clean up)
#
# Geometry defaults match the dumped NAND (2K page / 128K block, no subpage ->
# vid-hdr-offset 2048). Override via env NANDSIM_ID / VID_HDR_OFFSET if yours differs.
set -e
NANDSIM_ID="${NANDSIM_ID:-first_id_byte=0x2c second_id_byte=0xda third_id_byte=0x90 fourth_id_byte=0x95}"
VHO="${VID_HDR_OFFSET:-2048}"
MNT_DEFAULT=/mnt/ubi

[ "$(id -u)" = 0 ] || { echo "need root: run with sudo" >&2; exit 1; }

teardown() {
  set +e
  for m in $(awk '$3=="ubifs"{print $2}' /proc/mounts); do umount "$m" 2>/dev/null; done
  for u in $(ls /sys/class/ubi 2>/dev/null | grep -oE '^ubi[0-9]+$'); do ubidetach -d "${u#ubi}" 2>/dev/null; done
  rmmod ubi 2>/dev/null
  rmmod nandsim 2>/dev/null
}

if [ "$1" = "-u" ]; then teardown; echo "cleaned up (unmounted, detached, modules unloaded)"; exit 0; fi

EXTRACT=
if [ "$1" = "-x" ]; then EXTRACT=1; shift; fi
IMG="${1:?usage: ubimount.sh [-x] <image.ubi> [mountpoint|dest] | ubimount.sh -u}"
IMG="$(readlink -f "$IMG")"
[ -f "$IMG" ] || { echo "no such image: $IMG" >&2; exit 1; }

teardown   # clean slate (remove any leftover attach from a previous run)

modprobe ubi 2>/dev/null || true
modprobe nandsim $NANDSIM_ID
MTDDEV=$(grep -i 'NAND simulator' /proc/mtd | head -1 | cut -d: -f1); MTDNUM=${MTDDEV#mtd}
flash_erase "/dev/$MTDDEV" 0 0 >/dev/null            # clean 0xFF so empty PEBs read as empty
ubiformat "/dev/$MTDDEV" -f "$IMG" -y -q -O "$VHO" -s "$VHO"
ubiattach -m "$MTDNUM" -O "$VHO" >/dev/null
UBINUM=$(ls /sys/class/ubi | grep -oE '^ubi[0-9]+$' | head -1 | sed 's/ubi//')
VOL=$(ls -d /dev/ubi${UBINUM}_* | head -1)
NAME=$(cat "/sys/class/ubi/$(basename "$VOL")/name" 2>/dev/null)

if [ -n "$EXTRACT" ]; then
  DEST="${2:?usage: ubimount.sh -x <image.ubi> <dest-dir>}"
  MNT=$(mktemp -d)
  mount -t ubifs -o ro "$VOL" "$MNT"
  mkdir -p "$DEST"
  echo ">> extracting $(basename "$IMG") (vol '$NAME') -> $DEST"
  cp -a "$MNT"/. "$DEST"/
  N=$(find "$DEST" | wc -l)
  umount "$MNT"; rmdir "$MNT"
  teardown
  echo "OK: extracted $N entries to $DEST  (kernel stack torn down)"
else
  MNT="${2:-$MNT_DEFAULT}"
  mkdir -p "$MNT"
  mount -t ubifs -o ro "$VOL" "$MNT"
  echo "OK: vol '$NAME' from $(basename "$IMG") mounted read-only at $MNT"
  echo "    browse it, then clean up with:  sudo $0 -u"
fi
