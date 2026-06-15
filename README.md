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
--no-freeze        dump/ubidump: read live, don't freeze UBI writers
--no-ubinize       dump/ubidump: keep .ubifs + repack kit, don't build the .ubi
```

## Output

Images land in `./out` (change with `-o`):

- **MTD partitions** → `<name>.bin`, md5-verified device-side vs host-side.
- **UBI volumes** → a flashable `<mtdname>.ubi`. If the host has no `ubinize`
  (mtd-utils), you instead get a self-contained *repack kit* (`.ubifs` + geometry +
  `repack.sh`) to build the `.ubi` later. Extract contents with
  `ubireader_extract_files <vol>.ubi`; flash with `ubiformat /dev/mtdN -f <mtdname>.ubi`.

## Safety

- Never raw-reads `/dev/mtdblockN`; uses the `/dev/mtdN` char device (and
  `/dev/ubiX_Y` for UBI).
- Special or UBI-backed partitions are skipped automatically; naming one explicitly
  still refuses (UBI → use `ubidump`).
- Unknown NAND partitions are wedge-probed before a full read; a hang aborts the run
  (recover with a ~15 s power long-press, then `--skip` it).
- Every dump is md5-verified.

Host side assumes Linux.

---

See **[TECHNICAL.md](TECHNICAL.md)** for the internals: how it adapts (transport /
root / storage classes), the UBI repack kit, the `freezehold` diagnostic, NAND
geometry, and the full safety model.
