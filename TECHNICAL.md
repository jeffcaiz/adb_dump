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
| **adbd version** | Fastest binary-safe transport: `exec-out` → `encoded` (`base64`/`openssl`/`xxd`, +`gzip`) → `nc`. Old adbd (no exec-out, PTY mangles binary) auto-uses encoded + host-side CR strip. |
| **Thin device (few utils)** | exec-out needs ~nothing on-device. If no encoder/exec-out/nc, it **stops with remedies** instead of silently corrupting. `--push-bin <static busybox>` uploads a helper. |
| **Root escalation** | `--root auto` = already-root / `adb root` / `su`. Custom: `--root-cmd 'su -c'`. |
| **Busybox quirks** | Strips ANSI colour + PTY `\r`; `</dev/null` so adb shell can't swallow loop stdin. |

## Storage class drives the plan

`probe` classifies storage — **NAND-MTD / NOR-MTD / eMMC-block / SD-block /
UFS-block / NVMe-block** — from `/sys/class/mtd/*/type`, `/proc/mtd` and the block
devices. The per-partition plan and hazards branch on it:

- **NAND** skips the partition table (`mibib`), modem-EFS (`efs2`/`fsg`/`modemst*`)
  and active-UBI partitions (a raw read can hang the NAND controller), and 1-page
  **wedge-probes** the rest (a hang → abort + 15 s long-press recovery).
- **eMMC/UFS** skips RPMB, treats `bootX` as separate hw partitions, and does **not**
  wedge-probe (block reads are safe).
- **NOR** reads raw safely.

Internally this is an intrinsic `nand_raw_safe` property on each partition rule (not
a match against the human-readable role text), so display wording and dump logic stay
decoupled.

> The NAND path is validated on hardware; the eMMC/NOR branches are scaffolded and
> not yet hardware-tested.

### Active UBI

MTD partitions backing live UBI (via `/sys/class/ubi/ubiX/mtd_num`) are never
raw-read. In **full-auto `dump`** (no names) they're **auto-converted to a `ubidump`**
(frozen UBI-volume read) appended to the same run. If you **name** one explicitly
(`dump system`), it's **refused with a report** — you opted into a specific
partition, so the tool won't silently do something else.

### Consistent UBI image

Live rw ubifs has an uncommitted journal → `ubireader` chokes / md5 won't match. So
`ubidump` **freezes writers by default**: it detects the writer processes dynamically
(scans `/proc/*/fd` for the rw mountpoint), `kill -STOP`s them, `sync`, reads
`/dev/ubiX_Y`, `kill -CONT`. Override with `--freeze name1 name2` (specific procs) or
`--no-freeze` (read live). Read-only volumes have no writers → freeze is a no-op.

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
