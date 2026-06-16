# adbdump — internals

Technical detail behind [README.md](README.md). Host brains are in Python; the
device side is one embedded busybox recon script run in a single `adb shell`.

## What `probe` reports

- **Platform** — vendor (Qualcomm/MediaTek/Rockchip/…), SoC machine/family/soc_id,
  CPU core, kernel, flash kind. From `/sys/devices/soc0`, device-tree
  `compatible`/`model`, `/proc/cpuinfo`.
- **Transport** — exec-out support, detected encoder, chosen transport.
- **Flash manifest** — every partition gets a `DUMP` plan (`OK`/`SKIP`) and a
  `ROLE` (bootloader, TrustZone, modem EFS, partition table, boot image, UBI
  filesystem, …), plus the platform-typical partition set with a present/absent
  cross-check.
- **UBI volumes + mounts**, and the **rw-volume writers** auto-detected (the
  processes `ubidump` will freeze).

## How it adapts

| Concern | Behaviour |
|---|---|
| **adbd version** | Fastest binary-safe transport: `exec-out` → `encoded` (compact `base64`/`openssl`/`xxd` ahead of `nc`; bulky `od` hex behind it, +`gzip`) → `nc` → `od`. Old adbd (no exec-out, PTY mangles binary) auto-uses encoded + host-side CR strip. |
| **Thin device (few utils)** | exec-out needs ~nothing on-device. If no encoder/exec-out/nc, it **stops with remedies** instead of silently corrupting. `--push-bin <static busybox>` uploads a helper. |
| **Stripped toolbox (busybox-only utils)** | Old AOSP / Spreadtrum 4.x ship `nc`/`md5sum`/`od`/`uname`/`cut`/… only as busybox applets, with no busybox standalone-shell mode, so unprefixed calls fail and recon comes back empty. On-device busybox applets are auto-detected (`busybox <applet>`) and the coreutils needed by recon are symlinked into a tmp dir prepended to `PATH` — no binary pushed. |
| **busybox `nc` transport** | The host side reads via a Python socket (no host `nc` needed). busybox `nc -l` often sends the whole file but never `shutdown()`s the socket, so a naive reader blocks forever after the last byte → the read stops on clean EOF *or* a 20 s idle gap (data is all there; md5 verify confirms). Connects are retried because `adb forward` accepts before the device `nc` has bound. |
| **Root escalation** | `--root auto` = already-root → **`su`** → `adb root`. `su` is tried *before* `adb root` because on production/Magisk builds `adb root` is refused **and tears down the current adb transport** (next command → `error: closed`, USB re-enumeration can even drop the device to `unauthorized`), which would corrupt the rest of the run when `su` is what's actually available. Custom: `--root-cmd 'su -c'`. Root check tolerates old toolbox `id -u` that ignores `-u` and prints the full `uid=0(root) …` line. |
| **Busybox quirks** | Strips ANSI colour + PTY `\r`; `</dev/null` so adb shell can't swallow loop stdin. |

## Storage class drives the plan

`probe` classifies storage — **NAND-MTD / NOR-MTD / eMMC-block / SD-block /
UFS-block / NVMe-block** — from `/sys/class/mtd/*/type`, `/proc/mtd` and the block
devices. The per-partition plan and hazards branch on it:

- **NAND** skips the partition table (`mibib`), modem-EFS (`efs2`/`fsg`/`modemst*`)
  and active-UBI partitions (a raw read can hang the NAND controller), and 1-page
  **wedge-probes** the rest (a hang → abort + 15 s long-press recovery).
- **eMMC/UFS** skips RPMB, treats `bootX` as separate hw partitions, and does **not**
  wedge-probe (block reads are safe). The live `userdata`/`data` fs is **skipped by
  default** (a huge, live, FBE/FDE-encrypted raw image is rarely useful) and steered to
  `--files`; `--force` opts back into a raw image. Whole-disk **LUN aliases** — some
  devices (seen on OnePlus 6T) expose `by-name/sda…sdf` pointing at the entire disk
  next to the real partitions — are filtered so a full dump doesn't image the LUN *and*
  every partition inside it (would have been 238 GB vs 9 GB on the 6T). A whole disk is
  dropped only when a partition of it is also enumerated, so a partition-table-less raw
  disk is still kept.
- **NOR** reads raw safely.

MTD char nodes are **resolved to a node that actually opens**: some platforms (e.g.
Spreadtrum) keep stale flat `/dev/mtdN` nodes whose `open()` returns `ENOENT` while
the live nodes live under `/dev/mtd/mtdN`. Recon probes each with a read-only open
(`(: < $p)`, no page read — safe on the active-UBI mtd) and uses the working path.

Internally this is an intrinsic `nand_raw_safe` property on each partition rule (not
a match against the human-readable role text), so display wording and dump logic stay
decoupled.

> The NAND and UFS-block paths are validated on hardware (Spreadtrum/Qualcomm NAND;
> OnePlus 6T SDM845 UFS); the eMMC/NOR branches are scaffolded and not yet
> hardware-tested.

### Active UBI

MTD partitions backing live UBI (via `/sys/class/ubi/ubiX/mtd_num`) are never
raw-read. In **full-auto `dump`** (no names) they're **auto-converted to a `ubidump`**
(frozen UBI-volume read) appended to the same run. If you **name** one explicitly
(`dump system`), it's **refused with a report** — you opted into a specific
partition, so the tool won't silently do something else.

### Consistent UBI image (`--freeze`)

A whole-volume read of `/dev/ubiX_Y` is **not atomic** — it takes seconds to minutes.
A live rw UBIFS changes underneath it the whole time, and not only from userspace:

- the **UBIFS background commit thread** (`ubifs_bgt`) commits the journal on a timer
  even with zero userspace writers, and **GC** rewrites LEBs internally;
- so the early LEBs come from before a commit and the later ones from after → a
  **torn image** whose wandering-tree index points at nodes that aren't where the
  copy says → `ubireader` fails ("Node size smaller than expected" / "Bad Read
  Offset"). md5 (a second whole read) catches *drift between two reads* but can't
  prove a single read was internally atomic.

`kill -STOP`-ing the userspace writers (the default) doesn't stop the kernel
commit/GC thread, so it's only best-effort — but it's reversible and harmless, and
the device-vs-host md5 check flags a torn read. The only way to *guarantee* a clean
image is to make the filesystem genuinely read-only — but `mount -o remount,ro`
returns `EBUSY` while any file is open for write, and `kill -STOP` doesn't close fds.
So the `kill` mode **kills** the writers (fds close), after which `remount,ro`
succeeds and UBIFS stops writing entirely. That's destructive (services don't come
back) so it's opt-in, not the default — in practice many rw volumes barely change and
`stop` produces a matching md5.

`--freeze` modes (detector: scans `/proc/*/fd` for the rw mountpoint; `--writers
NAME…` overrides it):

| mode | mechanism | consistency | reversible |
|---|---|---|---|
| `stop` *(default)* | `kill -STOP` writers + `sync` → read → `kill -CONT` | best-effort (kernel commit/GC still runs); md5-verify catches tearing | yes |
| `kill` | `kill -9` writers → `mount -o remount,ro` → read → `remount,rw` | clean | no — services killed; auto-`adb reboot` on a clean run |
| `live` | nothing | none | n/a |

On an md5 mismatch the volume is re-read up to `--retry N` times (default 2). This is
sound because md5 OK is itself a proof of consistency: the device re-read matching the
streamed bytes means the volume didn't change across the whole read, so any verified
read *is* a point-in-time snapshot. Retry just samples again hoping to land a quiet
window — cheap when writes are sporadic, and it keeps the capture in one full run (a
manual single-volume rerun can't merge back into the aggregate `.ubi`, and would
clobber it — so a named subset never rebuilds the `.ubi`). The per-attempt success
probability falls as the read window (∝ volume size) grows, so a read slower than
`--retry-slow` (default 180 s) is capped to one retry and pointed at `--files`.

In `kill` mode, if a writer respawns (procd/systemd) before the remount, it swings
once more, then verifies the mount actually went `ro` in `/proc/mounts`; a mount it
can't quiesce is flagged and counts as "not clean" (which blocks the auto-reboot).
Read-only volumes have no writers → all modes are a no-op for them.

`fsfreeze` (FIFREEZE) would be the textbook primitive (it quiesces with files still
open), but busybox usually lacks it and UBIFS freeze support is uncertain, so it's
not used.

### `--files` — file-level capture when a block image can't be made consistent

For a big, busy rw volume (e.g. `userdata`/`/data`) on a watchdog device, `--freeze
kill` reboots (killing `system_server` is fatal to the framework) and `--freeze stop`
can't guarantee a point-in-time block image. Worse, a **block-level smear of UBIFS
can corrupt the *whole* image**: UBIFS has global structures (master node, the wandering
index/TNC, the LPT) captured at different instants — unlike an atomic power-cut, recovery
can't assume a single point in time, so the volume may not mount at all.

`--files` sidesteps this: it captures a **`tar`(`.gz`) of the mountpoint** instead of the
volume bytes (`tar -cf - -C <mnt> . | gzip`, streamed over the same transport, `compressed`
so the encoded path doesn't re-gzip). Reading through the live kernel gives a coherent
per-file view, so the only inconsistency is per-file (a file written *during* its own copy)
or cross-file (point-in-time skew) — never an unmountable image. Unmounted volumes
(firmware/NV) have no fs to tar, so they still get a block image even under `--files`.
On an md5 mismatch in normal (block) mode, the tool points the user at `--files`.

The same mechanism applies to **block storage** (`dump --files`): any mounted block
partition is captured as a tar of its mountpoint (resolved device→mountpoint from
`/proc/mounts`), unmounted ones get a block image. This is also the answer to
**encryption**: under FBE/FDE a raw `userdata` image is ciphertext, but a root tar of
`/data` reads the tree the kernel has already decrypted — so `userdata`/`data` is
skipped by default on block and steered here (`--force` overrides to a raw image).
Caveat: busybox/toybox `tar` has no portable `--one-file-system`, so a submount nested
under the target (e.g. a tmpfs or a bind under `/data`) is descended into; the run
warns which submounts it will include.

## UBI output: flashable, or a self-contained repack kit

`ubidump` (and the auto-UBI part of full-auto `dump`) reads each `/dev/ubiX_Y` and,
using the device's UBI/MTD geometry, **ubinize-wraps it into a flashable
`<mtdname>.ubi`**.

- **`.ubi` built OK** → that's all you keep. It's self-contained and flashable, so
  the intermediate `.ubinize.cfg` is dropped and the raw `<vol>.ubifs` too (it's
  recoverable from the `.ubi` via `ubireader`). Keep the `.ubifs` with `--keep-ubifs`.
- **`.ubi` not built** (`--no-ubinize`, no host `ubinize`, or it failed) → you get a
  **repack kit**: the raw `<vol>.ubifs`, a `<mtdname>.ubinize.cfg`, a `<mtdname>.geom`
  params file, and one `repack.sh` that reads the `.geom` files and rebuilds the
  `.ubi`(s). `cd` in and run `./repack.sh` once `ubinize` (mtd-utils) is available.

Flashing a `.ubi` back is a **functional clone, NOT bit-identical** to the original
raw NAND — the device regenerates EC/VID headers, PEB placement and bad-block map (as
the official tool does).

### NAND geometry

The repack uses geometry the kernel exposes; it's an intrinsic property of the NAND
chip (page/erase-block size come from the ONFI parameter page or legacy READ-ID at
boot), not data stored in a normal partition:

| `ubinize` flag | field | sysfs source | meaning |
|---|---|---|---|
| `-p` | PEB  | `mtd<N>/erasesize`   | physical erase block (smallest erasable unit) |
| `-m` | page | `mtd<N>/writesize`   | min I/O (smallest read/write unit) |
| `-s` | subpage | `mtd<N>/subpagesize` | sub-page write granularity (== page when unsupported) |
| —    | LEB  | `ubi<N>/eraseblock_size` | usable bytes/block after UBI's EC+VID headers; UBI-computed |

`vol_size` in the cfg is `reserved_ebs × LEB`.

### Reading / extracting a dumped .ubi

To read the contents back on the host there are two paths:

- **`ubireader`** (`ubireader_extract_files <vol>.ubi`) — userspace, no root, but it's
  a third-party reimplementation of the UBIFS parser and **0.8.x throws false
  failures** on some valid images (e.g. `Bad Read Offset Request` / `Node size smaller
  than expected`, 0 files), so a failure here does **not** mean the dump is bad.
- **Kernel mount** (authoritative) — `tools/ubimount.sh` feeds the image to the real
  kernel UBI/UBIFS stack via `nandsim` and mounts it read-only:

  ```sh
  sudo ./tools/ubimount.sh out/data.ubi            # mount ro at /mnt/ubi
  sudo ./tools/ubimount.sh -x out/data.ubi dest/   # copy all contents out, then clean up
  sudo ./tools/ubimount.sh -u                       # unmount + detach + unload
  ```

  UBIFS isn't a block fs — you can't `mount -o loop` it — so the script recreates the
  MTD→UBI→UBIFS stack: `nandsim` (2K page / 128K block, matching the device) →
  `flash_erase` (so empty PEBs read as clean `0xFF`) → `ubiformat -f` → `ubiattach -O
  2048` (the device has `subpagesize==pagesize`, so the VID header sits at 2048 and
  data at 4096) → `mount -t ubifs -o ro`. Needs root + mtd-utils + the stock
  nandsim/ubi/ubifs modules. Verified: both `data` and `system` dumps mount and read
  back every file cleanly this way.

## `freezehold` — freeze/thaw diagnostic

Some devices reboot during a `ubidump`. Two mechanisms can cause it and need opposite
responses:

- a **watchdog** fires *while frozen* (its countdown starts at `kill -STOP`) — then a
  slow/large dump can lose the race and you get a truncated image;
- the reboot is **thaw-triggered**, fired only *after* `kill -CONT` resumes the
  writers — harmless, because the dump (and md5) already completed.

`freezehold` tells them apart without dumping: it freezes the writers, **holds**
`--hold` seconds while polling `/proc/uptime` for a reboot, then thaws and watches
`--thaw-watch` seconds more. It prints one of three verdicts: *watchdog* (rebooted
while frozen, reports the ≈timeout), *thaw-triggered* (rebooted after CONT), or
*neither* (suspect dd read pressure / NAND contention during the dump). Run it first
if a new device reboots mid-dump. Set `--hold` to ~1.5–2× your normal dump time.

> Field note (M212 / MDM9607): verified **thaw-triggered** — the reboot fires on
> `kill -CONT`, after the dump and md5 are done. So the dump is unaffected and a slow
> link is fine; only resuming the writers provokes the supervisor to restart.

## Safety model

- Never raw-reads `/dev/mtdblockN`; uses `/dev/mtdN` char (and `/dev/ubiX_Y` for UBI).
- Special or UBI-backed partitions are skipped automatically by role; naming one
  explicitly still refuses (UBI → use `ubidump`).
- Unknown partitions are wedge-probed before a full read; a wedge aborts the run
  (recover with a ~15 s power long-press, then `--skip` it).
- Every dump is md5-verified device-side vs host-side.

### modem-EFS (efs2) is not dumped

On NAND, `efs2` (and `fsg`/`modemst*`) is the modem's own EFS and the running modem
DSP shares the NAND controller, so a raw read of the char device hangs the
controller. There is no reliable way to stop the modem first on this device class
(attempts to shut down the subsystem don't actually release the controller — the read
still hangs), so modem-EFS is **always `SKIP`** with no override.
