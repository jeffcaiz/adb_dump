# adbdump

Portable, self-adapting **flash dumper over ADB** for embedded Linux devices
(Qualcomm/MTD modems, eMMC SoCs, OpenWrt-ish boxes). Single file, Python 3 stdlib
only, no dependencies.

## Precondition

An **ADB root shell** — already root, or root reachable via `adb root` / `su` / a
custom escalation (see `--root`).

## Usage

```sh
./adbdump.py probe                 # fingerprint device + per-partition dump plan (do this first)
./adbdump.py list                  # partition table only
./adbdump.py dump                  # full auto: all safe MTD partitions + UBI volumes
./adbdump.py dump boot aboot tz    # only the named partitions
./adbdump.py ubidump               # just the UBI volumes
./adbdump.py freezehold            # diagnostic: why a device reboots mid-dump
```

**Run `probe` first.** It prints the platform, the chosen transport, and a
per-partition `OK`/`SKIP` plan — so you know exactly what `dump` will and won't read
before you run it. The tool auto-detects everything (transport, root method, storage
type) and skips partitions that are unsafe to read; you normally don't need flags.

Every subcommand has its own `-h` with the options that apply to it.

### Common options

```
-s SERIAL          target a specific adb device
-o DIR             output directory (default ./out)
--root M           auto|none|adbroot|su|custom        (default auto)
--push-bin FILE    push a static busybox/helper for thin devices
--skip NAME        skip an extra partition (repeatable)
--freeze MODE      rw-UBI consistency: stop|kill|live   (default stop, see below)
--retry N          re-read a volume on md5 mismatch (default 2; md5 OK = consistent)
--files            capture a mounted fs as a file-level tar.gz (busy/large/encrypted rw volumes)
--no-ubinize       dump/ubidump: keep .ubifs + repack kit, don't build the .ubi
```

### rw-UBI consistency (`--freeze`)

A live read-write UBIFS can change under a long whole-volume read (journal commit,
GC), tearing the image so it won't parse. `--freeze` picks how hard to quiesce it:

| `--freeze` | what it does | consistency | cost |
|---|---|---|---|
| `stop` *(default)* | reversible `kill -STOP` + `sync` | best-effort (can't stop kernel commit/GC) — md5-verify catches tearing | reversible, no reboot |
| `kill` | kill the writers, then `mount -o remount,ro` | clean image | **destructive** — services die; **auto-`adb reboot` after a clean dump** |
| `live` | nothing | none | reads a moving target |

The default `stop` flushes (`sync`) and pauses the userspace writers reversibly, then
reads; the device-vs-host md5 check flags any residual tearing. On a mismatch it
**re-reads up to `--retry N` times** (default 2): an md5 match *is* a proof the volume
held still for the whole read, so if write activity is sporadic a retry usually lands a
clean point-in-time snapshot — and folding this into one full dump avoids a manual
single-volume rerun (which won't merge back into the `.ubi`). A volume too big to read
inside a quiet window (`> --retry-slow` s) is steered to `--files` instead. If a volume is
busy enough that retries keep mismatching, escalate to `--freeze kill` — it
**kills the writers and remounts the fs read-only** for a guaranteed-consistent image,
then **`adb reboot`s the device** (only on a clean run) to restore the killed
services. Read-only volumes need no quiescing regardless.

### Big busy / encrypted volumes (`--files`)

A huge live rw filesystem (e.g. `userdata`/`/data`) is a poor fit for a raw block
image three ways: it's enormous, it tears under a long read (live writes), and on a
modern phone it's **encrypted** (FBE/FDE) so the raw image is just ciphertext. On UBI
a torn block image of UBIFS can be unmountable entirely (its index/master are smeared
across time, unlike an atomic power-cut).

`--files` sidesteps all three: it captures a **`tar`(`.gz`) of the mountpoint** —
read through the live kernel, which under root serves the files **decrypted**, so the
worst case is a single mid-write file or cross-file skew, never a dead or ciphertext
image. Any mounted volume can be captured this way; unmounted volumes (firmware/NV)
still get a block image.

On **block** storage `userdata`/`data` is **skipped by default** in full-auto `dump`
(a 110 GB encrypted, live raw image is rarely what you want) and you're steered to
`--files`; on UBI an md5 mismatch suggests it automatically.

```
./adbdump.py dump userdata --files              # block: -> out/userdata.tar.gz (decrypted files)
./adbdump.py dump userdata --force              # block: force a raw block image anyway (md5 will drift)
./adbdump.py ubidump --files ubi0_userdata      # UBI:   -> out/ubi0_userdata.tar.gz
tar -xzf out/userdata.tar.gz -C userdata/       # unpack on host
```

## Output

Images land in `./out` (change with `-o`):

- **MTD partitions** → `<name>.bin`, md5-verified device-side vs host-side.
- **UBI volumes** → a flashable `<mtdname>.ubi`. If the host has no `ubinize`
  (mtd-utils), you instead get a self-contained *repack kit* (`.ubifs` + geometry +
  `repack.sh`) to build the `.ubi` later. Flash with `ubiformat /dev/mtdN -f <mtdname>.ubi`.

To read a `.ubi`'s contents on the host, `tools/ubimount.sh` mounts it read-only
through the real kernel UBI/UBIFS stack (`sudo ./tools/ubimount.sh -x <vol>.ubi dest/`)
— authoritative, and it works on images that `ubireader` fails to parse. See
[TECHNICAL.md](TECHNICAL.md#reading--extracting-a-dumped-ubi).

## Safety

- Never raw-reads `/dev/mtdblockN`; uses the `/dev/mtdN` char device (and
  `/dev/ubiX_Y` for UBI).
- Special or UBI-backed partitions are skipped automatically; naming one explicitly
  still refuses (UBI → use `ubidump`).
- On block storage, whole-disk LUN aliases (`by-name/sda…` pointing at the entire
  disk) are filtered so they don't duplicate every partition under them, and the live
  `userdata`/`data` fs is skipped by default (→ `--files`, or `--force` for a raw image).
- Unknown NAND partitions are wedge-probed before a full read; a hang aborts the run
  (recover with a ~15 s power long-press, then `--skip` it).
- Every dump is md5-verified.

Host side assumes Linux.

---

See **[TECHNICAL.md](TECHNICAL.md)** for the internals: how it adapts (transport /
root / storage classes), the UBI repack kit, the `freezehold` diagnostic, NAND
geometry, and the full safety model.
