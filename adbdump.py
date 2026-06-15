#!/usr/bin/env python3
"""
adbdump — portable flash dumper for embedded Linux devices over ADB.

Host brains in Python (stdlib only); the device side is ONE embedded busybox
recon script (RECON_SH) run in a single `adb shell` — no escaped-script-in-string
sprawl. Drives adb for transport/dump/verify.

Precondition: target exposes an ADB *root* shell (already root / `adb root` / su).

Subcommands:
  probe              fingerprint device: platform/SoC/CPU, adbd caps, applets,
                     flash manifest (per-partition DUMP plan + role), UBI vols, writers
  list               partition table
  dump [name...]     dump readable MTD partitions (default: all non-special, non-UBI)
  ubidump [vol...]   dump /dev/ubiX_Y volumes; --freeze auto stops writers first
  freezehold         diagnostic: freeze writers, hold (no dump), watch for a
                     reboot, then thaw & watch — tells watchdog vs thaw-triggered

It adapts: fastest binary-safe transport (exec-out > encoded > nc), root method,
classifies partitions by platform (Qualcomm-aware) so special ones (mibib/efs2,
active UBI) are skipped automatically, and finds rw-volume writers dynamically.
"""
import argparse, base64, gzip, hashlib, os, re, shlex, shutil, subprocess, sys, time
from collections import namedtuple

# --------------------------------------------------------------- device recon ---
# Single busybox script. Emits @SECTION headers + lines. Pure read-only.
RECON_SH = r'''
echo "@KV"
echo "ARCH=$(uname -m)"
echo "KREL=$(uname -r)"
echo "CORES=$(grep -c ^processor /proc/cpuinfo 2>/dev/null)"
echo "HW=$(grep -m1 -i '^Hardware' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
echo "CPUPART=$(grep -m1 -i 'CPU part' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | tr -d ' ')"
echo "DTMODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\000')"
echo "DTCOMPAT=$(cat /proc/device-tree/compatible 2>/dev/null | tr '\000' ',')"
echo "SOC_VENDOR=$(cat /sys/devices/soc0/vendor 2>/dev/null)"
echo "SOC_MACHINE=$(cat /sys/devices/soc0/machine 2>/dev/null)"
echo "SOC_FAMILY=$(cat /sys/devices/soc0/family 2>/dev/null)"
echo "SOC_ID=$(cat /sys/devices/soc0/soc_id 2>/dev/null)"
echo "SOC_HW=$(cat /sys/devices/soc0/hw_platform 2>/dev/null)"
echo "MTDTYPES=$(cat /sys/class/mtd/mtd[0-9]*/type 2>/dev/null | sort -u | tr '\n' ',')"
echo "HASMTD=$(test -e /proc/mtd && echo 1)"
echo "BLK=$(for d in /dev/mmcblk0 /dev/sda /dev/nvme0n1; do [ -e $d ] && echo ${d##*/}; done | tr '\n' ' ')"
echo "MMC_TYPE=$(cat /sys/block/mmcblk0/device/type 2>/dev/null)"
echo "EMMC_SPECIAL=$(for d in /dev/mmcblk0boot0 /dev/mmcblk0boot1 /dev/mmcblk0rpmb /dev/mmcblk0gp0; do [ -e $d ] && echo ${d##*/}; done | tr '\n' ' ')"
echo "IDU=$(id -u)"
echo "@APPLETS"
for a in dd cat base64 openssl xxd gzip md5sum tar nc nanddump busybox flash_erase ubinfo ubiupdatevol stty; do
  command -v $a >/dev/null 2>&1 && echo "$a=1" || echo "$a=0"
done
echo "@DF"
df -h 2>/dev/null
echo "@MTD"
cat /proc/mtd 2>/dev/null
echo "@MOUNTS"
grep -i ubifs /proc/mounts 2>/dev/null
echo "@UBIVOL"
for v in /dev/ubi[0-9]*_[0-9]*; do
  [ -e "$v" ] || continue
  n=${v#/dev/}
  echo "$n name=$(cat /sys/class/ubi/$n/name 2>/dev/null)"
done
echo "@UBIMTD"
cat /sys/class/ubi/ubi[0-9]*/mtd_num 2>/dev/null
echo "@UBIGEO"
for u in /sys/class/ubi/ubi[0-9]*; do
  [ -e "$u/mtd_num" ] || continue
  ud=${u##*/}; mn=$(cat $u/mtd_num)
  echo "DEV $ud mtd=$mn peb=$(cat /sys/class/mtd/mtd$mn/erasesize 2>/dev/null) mio=$(cat /sys/class/mtd/mtd$mn/writesize 2>/dev/null) sub=$(cat /sys/class/mtd/mtd$mn/subpagesize 2>/dev/null) leb=$(cat $u/eraseblock_size 2>/dev/null) mtdname=$(cat /sys/class/mtd/mtd$mn/name 2>/dev/null)"
  for v in ${u}_*; do
    [ -e "$v/name" ] || continue
    vd=${v##*/}
    echo "VOL $ud $vd id=${vd##*_} type=$(cat $v/type 2>/dev/null) name=$(cat $v/name 2>/dev/null) reserved_ebs=$(cat $v/reserved_ebs 2>/dev/null)"
  done
done
echo "@BLOCK"
for bn in /dev/block/bootdevice/by-name/* /dev/block/by-name/*; do
  [ -e "$bn" ] || continue
  tgt=$(readlink -f "$bn" 2>/dev/null); [ -n "$tgt" ] || continue
  echo "${bn##*/} $tgt $(cat /sys/class/block/${tgt##*/}/size 2>/dev/null)"
done
for d in /dev/mmcblk0boot0 /dev/mmcblk0boot1; do
  [ -e "$d" ] && echo "${d##*/} $d $(cat /sys/class/block/${d##*/}/size 2>/dev/null)"
done
echo "@END"
'''

# Writer scan is split out of RECON_SH: it walks /proc and is the slow part. One
# `ls -l` per pid (not one readlink per fd) keeps it ~10x cheaper.
WRITERS_SH = r'''
for mnt in $(awk '$3=="ubifs" && $4 ~ /(^|,)rw(,|$)/ {print $2}' /proc/mounts 2>/dev/null); do
  out=""
  for d in /proc/[0-9]*; do
    if ls -l "$d/fd" "$d/cwd" 2>/dev/null | grep -q -- "-> $mnt"; then
      out="$out ${d#/proc/}($(cat $d/comm 2>/dev/null))"
    fi
  done
  printf "%s\t%s\n" "$mnt" "$out"
done
'''

ANSI = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')
def clean(b: bytes) -> str:
    return ANSI.sub(b'', b).replace(b'\r', b'').decode('utf-8', 'replace')

# ------------------------------------------------------------- classification ---
# (regex, role, nand_raw_safe) — `role` is a human-readable descriptor of WHAT the
# partition is (display only, never branched on). `nand_raw_safe` is the intrinsic
# property logic keys off: False = a direct read of the raw mtd char dev can hang
# the NAND controller or is a special on-NAND region, so it's never raw-read on
# NAND/MTD storage. (On block storage every partition reads safely regardless.)
PART_RULES = [
    (r'rpmb',                                          'replay-protected block', True),
    (r'(^|:)mibib$|^partition$|p?gpt|sgpt|backup_?gpt', 'partition table',       False),
    (r'^efs\d*|^fsg$|^fsc$|modemst|fsstore|encrypt',   'modem EFS',             False),
    (r'^(sbl|xbl|abl|aboot)\w*|appsboot',              'bootloader',            True),
    (r'^(tz|qsee|hyp|keymaster|cmnlib|sec|devcfg)\w*', 'TrustZone / secure',    True),
    (r'^(rpm|pmic|sdi|dbi)\w*',                        'platform firmware',     True),
    (r'^(boot|recovery)\w*$',                          'boot image',            True),
    (r'^misc$',                                        'boot cookie',           True),
    (r'^(cust_info|sys_rev|rawdata|cdt|ddr|limits|dip|splash|oem|config)\w*$', 'OEM / config data', True),
    (r'modem|^mdm|non-?hlos|^dsp$|adsp',               'modem firmware',        True),
    (r'system|userdata|^data$|persist|^vendor$|rootfs|usrfs|^usr|^cache$', 'filesystem', True),
]
def classify(name):
    """-> (role_label, nand_raw_safe). Unknown -> safe-by-default (wedge-probed)."""
    n = name.lower()
    for pat, role, nand_safe in PART_RULES:
        if re.search(pat, n):
            return role, nand_safe
    return 'unknown', True

# Storage-class-aware plan for one partition.
#   action: 'OK'  = read/dump it here
#           'SKIP' = don't read it here (owned/special/handled elsewhere)
#   reason: stable code logic branches on (None when OK). `role`/`note` are
#           display-only and must never be compared.
Plan = namedtuple('Plan', 'action role reason note')
def dump_plan(name, sclass, is_ubi=False, skip_globs=()):
    role, nand_safe = classify(name)
    nand = sclass in ('NAND-MTD', 'NOR-MTD', 'MTD')
    if any(re.fullmatch(p.replace('*', '.*'), name) for p in skip_globs):
        return Plan('SKIP', role, 'skip-list', 'excluded (skip list)')
    if is_ubi:
        return Plan('SKIP', 'UBI filesystem', 'ubi', 'via ubidump')
    if 'rpmb' in name.lower():
        return Plan('SKIP', role, 'rpmb', 'needs RPMB protocol')
    if nand and not nand_safe:
        return Plan('SKIP', role, 'nand-unsafe', 'unsafe to raw-read on NAND (special region / controller contention)')
    return Plan('OK', role, None, '')

def wants_wedge_probe(sclass):
    return sclass in ('NAND-MTD', 'MTD')   # only raw NAND can hang the controller on a read

# platform-typical manifests (for "what should be here" cross-check)
EXPECTED = {
    'Qualcomm': ['sbl', 'mibib', 'efs2', 'tz', 'rpm', 'aboot', 'boot', 'misc', 'system', 'data'],
}
CPU_PART = {
    '0xc05': 'Cortex-A5', '0xc07': 'Cortex-A7', '0xc08': 'Cortex-A8', '0xc09': 'Cortex-A9',
    '0xc0f': 'Cortex-A15', '0xc0e': 'Cortex-A17', '0xd03': 'Cortex-A53', '0xd04': 'Cortex-A35',
    '0xd07': 'Cortex-A57', '0xd08': 'Cortex-A72', '0xb76': 'ARM1176',
}
def storage_class(facts):
    """-> (class, detail, hazard_note). The read-hazard model is storage-specific."""
    types = [t for t in facts.get('MTDTYPES', '').split(',') if t]
    blk = facts.get('BLK', '').split()
    if facts.get('HASMTD') and types:
        k = 'NAND-MTD' if any('nand' in t for t in types) else ('NOR-MTD' if any('nor' in t for t in types) else 'MTD')
        detail = 'MTD type(s): ' + ','.join(types)
    elif any('mmcblk' in b for b in blk):
        k = 'SD-block' if facts.get('MMC_TYPE') == 'SD' else 'eMMC-block'
        detail = 'block: ' + ' '.join(blk)
        sp = facts.get('EMMC_SPECIAL', '').strip()
        if sp:
            detail += '   special: ' + sp
    elif any(b.startswith('sd') for b in blk):
        k, detail = 'SCSI/UFS-block', 'block: ' + ' '.join(blk)
    elif any('nvme' in b for b in blk):
        k, detail = 'NVMe-block', 'block: ' + ' '.join(blk)
    else:
        k, detail = 'unknown', ''
    hz = {
        'NAND-MTD': 'modem-EFS / partition-table / active-UBI partitions are skipped (a raw read can hang the NAND controller); others are probed once before a full read.',
        'NOR-MTD': 'memory-mapped; raw reads are safe. Reads via the mtd char device.',
        'eMMC-block': 'block reads are safe (no controller hang). RPMB is skipped (special protocol); bootX are separate hw partitions; a mounted rw fs should be frozen for a consistent image.',
        'SD-block': 'block reads are safe; freeze a mounted rw fs for consistency.',
        'SCSI/UFS-block': 'block reads are safe; RPMB lives on a separate LUN; freeze a mounted rw fs.',
        'NVMe-block': 'block reads are safe; freeze a mounted rw fs for consistency.',
    }.get(k, 'unknown storage — a partition is probed once before any full read.')
    return k, detail, hz

def classify_vendor(facts):
    hay = ' '.join(facts.get(k, '') for k in
                   ('DTCOMPAT', 'SOC_VENDOR', 'SOC_MACHINE', 'SOC_FAMILY', 'HW', 'DTMODEL', 'KREL')).lower()
    for vendor, keys in [
        ('Qualcomm', ('qualcomm', 'qcom', 'msm', 'mdm', 'snapdragon', 'apq')),
        ('MediaTek', ('mediatek', 'mt6', 'mt7', 'mt8')),
        ('Rockchip', ('rockchip', 'rk3')),
        ('Allwinner', ('allwinner', 'sun4i', 'sun5i', 'sun7i', 'sun8i', 'sun50i')),
        ('Amlogic', ('amlogic', 'meson')),
        ('Broadcom', ('broadcom', 'bcm')),
        ('HiSilicon', ('hisilicon', 'hi3')),
    ]:
        if any(k in hay for k in keys):
            return vendor
    return 'unknown'

# ------------------------------------------------------------------- helpers ---
def hsize(n):
    n = float(n); u = ['B', 'KB', 'MB', 'GB', 'TB']; i = 0
    while n >= 1024 and i < 4:
        n /= 1024; i += 1
    return f'{n:.1f}{u[i]}'

C = dict(R='\033[31m', G='\033[32m', Y='\033[33m', B='\033[36m', Z='\033[0m')
def err(m): print(f"{C['R']}{m}{C['Z']}", file=sys.stderr)
def warn(m): print(f"{C['Y']}{m}{C['Z']}", file=sys.stderr)
def ok(m): print(f"{C['G']}{m}{C['Z']}", file=sys.stderr)
def info(m): print(m, file=sys.stderr)
def hr(): print('-' * 60, file=sys.stderr)

# -------------------------------------------------------------------- Device ---
class Device:
    def __init__(self, a):
        self.base = [a.adb] + (['-s', a.serial] if a.serial else [])
        self.root = a.root          # none|adbroot|su|custom (resolved later for auto)
        self.root_pre = a.root_cmd
        self.devpre = ''            # PATH prefix after --push-bin
        self.enc = ''; self.gzip = False; self.execout = False
        self.transport = a.transport
        self.ncport = a.ncport
        self.allow_corrupt = a.allow_corrupt
        self.applets = {}
        self.sclass = ''            # storage class, set by setup()

    # ---- low level ----
    def wrap(self, cmd):
        # shlex.quote escapes embedded single quotes so scripts that contain them
        # (RECON_SH/WRITERS_SH) survive su/custom wrapping. $PATH from devpre still
        # expands: the inner shell that su/-c invokes re-parses the quoted content.
        cmd = self.devpre + cmd
        if self.root == 'su':     return "su -c %s" % shlex.quote(cmd)
        if self.root == 'custom': return "%s %s" % (self.root_pre, shlex.quote(cmd))
        return cmd

    def _run(self, argv, timeout=None, want_bytes=False):
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None, b''
        return p.returncode, p.stdout

    def shtext(self, cmd, root=True, timeout=40):
        c = self.wrap(cmd) if root else cmd
        rc, out = self._run(self.base + ['shell', c], timeout=timeout)
        return None if rc is None else clean(out)

    def adb(self, *args, timeout=60):
        return self._run(self.base + list(args), timeout=timeout)

    # ---- capability resolution ----
    def resolve_root(self):
        idu = (self.shtext('id -u', root=False) or '').strip()
        if idu == '0':
            self.root = 'none'; return
        if self.root in ('none', 'su', 'custom'):
            return
        self.adb('root'); self.adb('wait-for-device', timeout=30)
        idu = (self.shtext('id -u', root=False) or '').strip()
        if idu == '0':
            self.root = 'none'; return
        if (self.shtext("su -c 'id -u'", root=False) or '').strip() == '0':
            self.root = 'su'; return
        self.root = 'none'
        warn("Could not confirm root (id -u != 0); continuing. Use --root-cmd for custom escalation.")

    def detect_execout(self):
        rc, out = self.adb('exec-out', 'echo', 'eok', timeout=20)
        self.execout = (rc == 0 and out.strip() == b'eok')

    def detect_codec(self):
        self.gzip = bool(self.applets.get('gzip'))
        for e in ('base64', 'openssl', 'xxd'):
            if self.applets.get(e):
                self.enc = e; break

    def choose_transport(self):
        if self.transport != 'auto':
            return
        if self.execout:      self.transport = 'execout'
        elif self.enc:        self.transport = 'encoded'
        elif self.applets.get('nc'): self.transport = 'nc'
        elif self.allow_corrupt:     self.transport = 'raw'
        else:
            err("No binary-safe transport on this device:")
            err(f"  exec-out={self.execout}  encoder=none(base64/openssl/xxd)  nc={bool(self.applets.get('nc'))}")
            err("Fixes: --push-bin <static busybox>  |  --transport nc  |  --allow-corrupt")
            sys.exit(3)

    def push_bin(self, path):
        if not os.path.isfile(path):
            err(f"--push-bin: not found: {path}"); sys.exit(1)
        tmp = None
        for d in ('/data/local/tmp', '/tmp', '/var/volatile', '/dev'):
            r = self.shtext(f'mkdir -p {d}/.adbdump 2>/dev/null && test -w {d}/.adbdump && echo ok')
            if r and 'ok' in r:
                tmp = d + '/.adbdump'; break
        if not tmp:
            err("--push-bin: no writable tmp on device"); sys.exit(1)
        b = os.path.basename(path)
        info(f"pushing {path} -> {tmp}/{b}")
        if self.adb('push', path, f'{tmp}/{b}')[0] != 0:
            err("adb push failed"); sys.exit(1)
        self.shtext(f'chmod 755 {tmp}/{b}')
        if 'busybox' in b:
            self.shtext(f'{tmp}/{b} --install -s {tmp} 2>/dev/null')
        self.devpre = f'PATH={tmp}:$PATH '
        ok(f"pushed; device PATH now prefers {tmp}")

    # ---- recon ----
    def recon(self):
        raw = self.shtext(RECON_SH, timeout=120) or ''
        sec = {}; cur = None
        for line in raw.splitlines():
            if line.startswith('@'):
                cur = line[1:]; sec.setdefault(cur, [])
            elif cur is not None:
                sec[cur].append(line)
        facts = {}
        for ln in sec.get('KV', []):
            if '=' in ln:
                k, v = ln.split('=', 1); facts[k] = v
        self.applets = {ln.split('=')[0]: ln.split('=')[1] == '1'
                        for ln in sec.get('APPLETS', []) if '=' in ln}
        return facts, sec

    # ---- transport: emit ascii ----
    def _emit_cmd(self, dev):
        rd = f'gzip -c {dev} 2>/dev/null' if self.gzip else f'cat {dev} 2>/dev/null'
        return {'base64': f'{rd} | base64',
                'openssl': f'{rd} | openssl base64',
                'xxd': f'{rd} | xxd -p'}[self.enc]

    def _decode(self, data: bytes) -> bytes:
        data = data.replace(b'\r', b'').replace(b'\n', b'')
        raw = bytes.fromhex(data.decode()) if self.enc == 'xxd' else base64.b64decode(data)
        return gzip.decompress(raw) if self.gzip else raw

    def stream(self, dev, outpath, timeout):
        """Dump whole device -> outpath. Returns True on (transport) success."""
        if self.transport == 'execout':
            rd = f'dd if={dev} bs=131072 2>/dev/null' if self.applets.get('dd') else f'cat {dev} 2>/dev/null'
            rc, data = self.adb('exec-out', self.wrap(rd), timeout=timeout)
            if rc != 0:  # None (timeout) or non-zero exit -> don't leave a partial file
                return False
            with open(outpath, 'wb') as f:
                f.write(data)
            return True
        if self.transport == 'encoded':
            rc, data = self._run(self.base + ['shell', self.wrap(self._emit_cmd(dev))], timeout=timeout)
            if rc is None: return False
            try:
                with open(outpath, 'wb') as f:
                    f.write(self._decode(data))
            except Exception as e:
                err(f"   decode error: {e}"); return False
            return True
        if self.transport == 'nc':
            self.adb('forward', f'tcp:{self.ncport}', f'tcp:{self.ncport}')
            srv = subprocess.Popen(self.base + ['shell', self.wrap(f'nc -l -p {self.ncport} < {dev}')],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            try:
                with open(outpath, 'wb') as f:
                    p = subprocess.run(['nc', '127.0.0.1', str(self.ncport)], stdout=f, timeout=timeout)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = None
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                srv.kill()
            self.adb('forward', '--remove', f'tcp:{self.ncport}')
            return rc == 0
        if self.transport == 'raw':
            rc, data = self._run(self.base + ['shell', self.wrap(f'cat {dev}')], timeout=timeout)
            if rc is None: return False
            with open(outpath, 'wb') as f:
                f.write(data)
            return True
        return False

    def dev_md5(self, dev):
        r = self.shtext(f'md5sum {dev}', timeout=600) or ''
        m = r.split()
        return m[0] if m else ''

    def wedge_probe(self, dev, timeout):
        rd = f'dd if={dev} bs=2048 count=1 2>/dev/null' if self.applets.get('dd') else f'head -c 2048 {dev} 2>/dev/null'
        rc, _ = self.adb('shell', self.wrap(f'{rd} | wc -c'), timeout=timeout)
        return rc == 0  # False == wedge/timeout/error

    def scan_writers(self):
        """-> dict mountpoint -> list of (pid, comm). Walks /proc (slow-ish)."""
        raw = self.shtext(WRITERS_SH, timeout=120) or ''
        res = {}
        for line in raw.splitlines():
            if '\t' in line:
                mnt, procs = line.split('\t', 1)
                res[mnt] = re.findall(r'(\d+)\(([^)]*)\)', procs)
        return res

    def rw_ubifs_mounts(self):
        """-> list of rw-mounted ubifs mountpoints."""
        raw = self.shtext(r'''awk '$3=="ubifs" && $4 ~ /(^|,)rw(,|$)/ {print $2}' /proc/mounts 2>/dev/null''') or ''
        return [m for m in raw.split() if m]

    def remount(self, mnt, mode):
        """remount mnt 'ro' or 'rw'. -> True iff /proc/mounts confirms `mode` after."""
        self.shtext(f"mount -o remount,{mode} {mnt}")
        raw = self.shtext(f"awk -v m={mnt} '$2==m {{print $4}}' /proc/mounts 2>/dev/null") or ''
        return mode in raw.split(',')


# ------------------------------------------------------------------ inventory ---
def host_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def parse_mtd(sec):
    out = []
    for ln in sec.get('MTD', []):
        m = re.match(r'(mtd\d+):\s+([0-9a-f]+)\s+([0-9a-f]+)\s+"([^"]*)"', ln)
        if m:
            out.append((f'/dev/{m.group(1)}', int(m.group(2), 16), m.group(4)))
    return out

def parse_block(sec):
    """eMMC/UFS block partitions from /dev/block(/bootdevice)/by-name + boot HW parts."""
    seen = set(); out = []
    for ln in sec.get('BLOCK', []):
        t = ln.split()
        if len(t) < 3 or t[1] in seen:
            continue
        seen.add(t[1])
        out.append((t[1], int(t[2] or 0) * 512, t[0]))
    return out

def ubi_mtds(sec):
    return set(x.strip() for x in sec.get('UBIMTD', []) if x.strip())

def is_ubi_backed(dev, umtds):
    m = re.match(r'/dev/mtd(\d+)$', dev)
    return bool(m and m.group(1) in umtds)

# ----------------------------------------------------------------- subcommands ---
def setup(dev, a):
    info(f"{C['B']}· connecting + resolving root…{C['Z']}")
    dev.resolve_root()
    if a.push_bin:
        dev.push_bin(a.push_bin)
    dev.detect_execout()
    info(f"{C['B']}· reading device recon (platform, flash, ubi)…{C['Z']}")
    facts, sec = dev.recon()
    dev.detect_codec()
    dev.choose_transport()
    dev.sclass = storage_class(facts)[0]
    return facts, sec

def cmd_probe(dev, a):
    facts, sec = setup(dev, a)
    vendor = classify_vendor(facts)
    cpu = CPU_PART.get(facts.get('CPUPART', ''), '') or facts.get('HW', 'arm')
    sk, sdetail, shz = storage_class(facts)

    hr(); ok('PLATFORM'); hr()
    info(f"  vendor       {vendor}")
    info(f"  model        {facts.get('DTMODEL') or '?'}")
    info(f"  soc          machine={facts.get('SOC_MACHINE') or '?'} family={facts.get('SOC_FAMILY') or '?'} "
         f"soc_id={facts.get('SOC_ID') or '?'} hw={facts.get('SOC_HW') or ''}")
    info(f"  compatible   {facts.get('DTCOMPAT') or '?'}")
    info(f"  cpu          {facts.get('CORES','?')}x {cpu} ({facts.get('ARCH','?')}) part={facts.get('CPUPART','?')}")
    info(f"  kernel       {facts.get('KREL','?')}")
    info(f"  storage      {C['B']}{sk}{C['Z']}   {sdetail}")
    info(f"  hazard       {shz}")
    info(f"  root         method={dev.root} (id -u={facts.get('IDU','?')})")

    hr(); ok('ADBD / TRANSPORT'); hr()
    info(f"  exec-out     {'yes' if dev.execout else 'NO (old adbd; PTY mangles binary)'}")
    info(f"  encoder      {dev.enc or 'none'}  (gzip={'yes' if dev.gzip else 'no'})")
    info(f"  chosen       {dev.transport}")

    hr(); ok('APPLETS'); hr()
    info('  ' + '  '.join(f"{k}={'y' if v else '-'}" for k, v in dev.applets.items()))

    parts = enum_parts(sk, sec); umtds = ubi_mtds(sec)
    hr(); ok('FLASH MANIFEST'); hr()
    info(f"  {'DEV':<16}{'SIZE':>9}  {'NAME':<12} {'DUMP':<5} {'ROLE':<20} NOTE")
    present = set()
    for d, sz, nm in parts:
        present.add(nm)
        p = dump_plan(nm, sk, is_ubi_backed(d, umtds))
        info(f"  {d:<16}{hsize(sz):>9}  {nm:<12} {p.action:<5} {p.role:<20} {p.note}")
    if vendor in EXPECTED and present:
        missing = [p for p in EXPECTED[vendor] if p not in present]
        info(f"  {vendor}-typical set: {' '.join(EXPECTED[vendor])}"
             + (f"   (absent: {' '.join(missing)})" if missing else "   (all present)"))

    hr(); ok('UBI VOLUMES + MOUNTS'); hr()
    for ln in sec.get('UBIVOL', []): info('  ' + ln)
    for ln in sec.get('MOUNTS', []): info('  ' + ln)

    info(f"{C['B']}· scanning rw-volume writers (walking /proc)…{C['Z']}")
    writers = dev.scan_writers()
    hr(); ok('RW-VOLUME WRITERS  (auto-detected; ubidump --freeze auto stops these)'); hr()
    if writers:
        for mnt, ps in writers.items():
            info(f"  {mnt}: " + (', '.join(f"{c}({p})" for p, c in ps) or '(none)'))
    else:
        info("  (no rw ubifs writers found)")
    hr()
    info("Next:")
    info("  dump      full auto: safe MTD partitions + UBI volumes (frozen) + flashable .ubi")
    info("  ubidump   only the UBI volumes (also frozen by default)")

def enum_parts(sclass, sec):
    return parse_mtd(sec) if sclass in ('NAND-MTD', 'NOR-MTD', 'MTD') else parse_block(sec)

def inventory(dev, a):
    _, sec = setup(dev, a)
    return enum_parts(dev.sclass, sec), ubi_mtds(sec), dev.sclass, sec

def cmd_list(dev, a):
    parts, umtds, sclass, _ = inventory(dev, a)
    for d, sz, nm in parts:
        p = dump_plan(nm, sclass, is_ubi_backed(d, umtds))
        print(f"{d:<16}{hsize(sz):>9}  {nm:<12} {p.action:<5} {p.role:<20} {p.note}")

def dump_one_mtd(dev, a, d, sz, nm):
    """Wedge-probe + stream + verify one raw partition. Returns 0 ok / 1 warn / 2 wedge-abort."""
    out = os.path.join(a.out, nm + '.bin')
    if not a.force and os.path.exists(out) and os.path.getsize(out) == sz:
        ok(f">> {nm}: up-to-date ({hsize(sz)}), skip"); return 0
    if not a.no_probe and wants_wedge_probe(dev.sclass):
        info(f"{C['B']}>> {nm}: probing...{C['Z']}")
        if not dev.wedge_probe(d, a.probe_timeout):
            err(f">> {nm}: 1-page read timed out — NAND controller may be hung. Stopping.")
            err(f"   Recover: hold POWER ~15s (cold power-cycle), then re-run with --skip {nm}")
            return 2
    info(f">> {nm}  ({d}, {hsize(sz)})  ->  {out}")
    if not dev.stream(d, out, a.dd_timeout):
        err("   transport error/timeout"); return 1
    rc = 0; got = os.path.getsize(out)
    if got != sz:
        warn(f"   size mismatch got {hsize(got)} expected {hsize(sz)}"); rc = 1
    if a.verify:
        dh, hh = dev.dev_md5(d), host_md5(out)
        if dh and dh == hh: ok(f"   md5 OK  {hh}")
        else: warn(f"   md5 MISMATCH dev={dh} host={hh}"); rc = 1
    else:
        ok(f"   wrote {hsize(got)}")
    return rc

def ubi_vols_from_sec(sec):
    return ['/dev/' + ln.split()[0] for ln in sec.get('UBIVOL', []) if ln.strip()]

def parse_ubigeo(sec):
    """-> {ubiDev: {mtd,peb,mio,sub,leb,mtdname, vols:[{vd,id,type,name,reserved_ebs}]}}"""
    devs = {}
    for ln in sec.get('UBIGEO', []):
        t = ln.split()
        if not t:
            continue
        if t[0] == 'DEV':
            kv = dict(x.split('=', 1) for x in t[2:] if '=' in x)
            devs[t[1]] = {**kv, 'vols': []}
        elif t[0] == 'VOL':
            kv = dict(x.split('=', 1) for x in t[3:] if '=' in x)
            devs.setdefault(t[1], {'vols': []})['vols'].append({'vd': t[2], **kv})
    return devs

REPACK_SH = '''#!/bin/sh
# Rebuild flashable .ubi image(s) from the dumped .ubifs + saved geometry.
# Needs mtd-utils (ubinize) on the host. Run from this directory.
#
# Flashing a .ubi gives a FUNCTIONAL CLONE, not a bit-identical raw NAND: the
# device rebuilds EC/VID headers, PEB placement and bad-block handling.
set -e
cd "$(dirname "$0")"
for g in *.geom; do
  [ -e "$g" ] || continue
  SUB=
  . "./$g"
  echo ">> ubinize -> $OUT"
  ubinize -o "$OUT" -p "$PEB" -m "$MIO" ${SUB:+-s "$SUB"} "$CFG"
  echo "   flash: ubiformat /dev/mtd$MTD -f $OUT   (or vendor tool -> partition ${OUT%.ubi})"
done
'''

def make_flashable(a, sec, dumped):
    """For each UBI device with dumped volumes, ubinize the .ubifs into a flashable
    <mtdname>.ubi using device geometry. A built .ubi is self-contained, so the
    intermediate cfg (and the raw .ubifs, unless --keep-ubifs) are dropped. If the
    .ubi can't be built here (--no-ubinize / no host ubinize / failure), the .ubifs
    is kept alongside a <mtdname>.geom params file and a shared repack.sh that
    rebuilds it later."""
    devs = parse_ubigeo(sec)
    have = shutil.which('ubinize')
    pending = False
    for ud, di in devs.items():
        vols = [v for v in di.get('vols', []) if v['vd'] in dumped]
        if not vols:
            continue
        mname = di.get('mtdname') or ud
        peb, mio, sub = di.get('peb'), di.get('mio'), di.get('sub')
        leb = int(di.get('leb') or 0)
        if not (peb and mio and leb):
            warn(f"   {mname}: incomplete UBI geometry (peb={peb} mio={mio} leb={leb}); "
                 f"cannot build flashable .ubi — keeping .ubifs")
            continue
        cfg = os.path.join(a.out, f'{mname}.ubinize.cfg')
        with open(cfg, 'w') as f:
            for v in vols:
                size = int(v.get('reserved_ebs') or 0) * leb
                f.write(f"[{v.get('name') or v['vd']}]\nmode=ubi\nimage={v['vd']}.ubifs\n"
                        f"vol_id={v.get('id', '0')}\nvol_type={v.get('type', 'dynamic')}\n"
                        f"vol_name={v.get('name', '')}\nvol_size={size}\n\n")
        cmd = ['ubinize', '-o', f'{mname}.ubi', '-p', peb, '-m', mio]
        if sub and sub != '0':
            cmd += ['-s', sub]
        cmd += [os.path.basename(cfg)]

        built = False
        if a.no_ubinize:
            warn(f"   --no-ubinize: keeping .ubifs + repack recipe for {mname}")
        elif not have:
            warn(f"   host has no ubinize; keeping .ubifs + repack recipe for {mname}")
        else:
            rc = subprocess.run(cmd, cwd=a.out).returncode
            if rc == 0:
                built = True
                ok(f"   ubinize -> {a.out}/{mname}.ubi  (flashable to {mname} / mtd{di.get('mtd')})")
            else:
                warn(f"   ubinize failed (rc={rc}); keeping .ubifs + repack recipe for {mname}")

        if built:
            # .ubi is self-contained & flashable -> drop the intermediate cfg, and
            # (unless asked to keep) the raw .ubifs which is recoverable via ubireader.
            os.remove(cfg)
            if not a.keep_ubifs:
                for v in vols:
                    fp = os.path.join(a.out, v['vd'] + '.ubifs')
                    if os.path.exists(fp):
                        os.remove(fp); ok(f"   removed {v['vd']}.ubifs (extract from .ubi via ubireader if needed)")
        else:
            # not built -> save the geometry so repack.sh can rebuild it later.
            with open(os.path.join(a.out, f'{mname}.geom'), 'w') as f:
                f.write(f"OUT={mname}.ubi\n"
                        f"PEB={peb}\nMIO={mio}\nSUB={sub if sub and sub != '0' else ''}\n"
                        f"CFG={mname}.ubinize.cfg\nMTD={di.get('mtd')}\n")
            pending = True

    if pending:
        rp = os.path.join(a.out, 'repack.sh')
        with open(rp, 'w') as f:
            f.write(REPACK_SH)
        os.chmod(rp, 0o755)
        info(f"  not packed here — rebuild later: cd {a.out} && ./repack.sh")

def collect_writers(dev, a):
    """Resolve the writer pids. -> (sorted pid list, auto_bool).
    auto: walk /proc for rw-ubifs writers; named: pidof each --writers NAME."""
    names = getattr(a, 'writers', None)
    frozen = []
    auto = names in (None, [], ['auto'])
    if auto:
        for mnt, ps in dev.scan_writers().items():
            info(f"  writers of {mnt}: " + (', '.join(f"{c}({p})" for p, c in ps) or '(none)'))
            frozen += [p for p, _ in ps]
    else:
        for name in names:
            frozen += re.findall(r'\d+', dev.shtext(f"pidof {name} 2>/dev/null") or '')
    return sorted(set(frozen), key=int), auto

def cmd_freezehold(dev, a):
    """Diagnostic: STOP the rw-volume writers, then HOLD (no dump) watching for a
    reboot, then CONT and watch again. Discriminates the two reboot hypotheses:
      - reboot WHILE frozen, at a fixed delay from STOP  -> watchdog (countdown
        starts at freeze; a slow dump would race it)
      - reboot only AFTER thaw (kill -CONT)              -> thaw-triggered
      - survives both                                    -> reboot is not from
        freeze/thaw alone (dd read pressure / NAND contention?)"""
    setup(dev, a)
    frozen, auto = collect_writers(dev, a)
    if not frozen:
        err("No writers to freeze — nothing to test (try --writers NAME)."); sys.exit(1)

    def uptime():
        t = (dev.shtext("cat /proc/uptime 2>/dev/null") or '').split()
        return float(t[0]) if t else None

    base = uptime()
    if base is None:
        err("Could not read /proc/uptime before freezing."); sys.exit(1)
    def alive():
        """-> (is_alive, uptime_or_None). A reboot resets uptime toward 0."""
        rc, _ = dev.adb('get-state', timeout=5)
        if rc != 0:
            return False, None
        up = uptime()
        if up is None or up < base - 5:
            return False, up
        return True, up

    hr(); warn(f"FREEZE-HOLD diagnostic — hold={a.hold}s thaw-watch={a.thaw_watch}s  (NO dump)")
    info(f"  baseline uptime={base:.0f}s   writers [{'auto' if auto else 'named'}]: {' '.join(frozen)}")
    info(f"  >> kill -STOP {len(frozen)} writer(s) + sync")
    dev.shtext(f"kill -STOP {' '.join(frozen)}; sync")
    stopped = time.time()

    rebooted = False; reboot_at = None
    while True:
        el = time.time() - stopped
        if el >= a.hold:
            break
        time.sleep(min(3, max(1, a.hold - el)))
        live, up = alive()
        el = time.time() - stopped
        if not live:
            rebooted = True; reboot_at = el
            warn(f"  [{el:5.0f}s] REBOOT while FROZEN  (uptime={'lost' if up is None else f'{up:.0f}s'})")
            break
        info(f"  [{el:5.0f}s] alive  uptime={up:.0f}s")

    if rebooted:
        hr(); warn("VERDICT: HYPOTHESIS 1 — watchdog. Reboot fired while frozen, "
                   f"~{reboot_at:.0f}s after STOP (≈ the watchdog timeout).")
        warn("  => the countdown starts at freeze; a slow/large dump can lose the race.")
        sys.exit(0)

    info(f"  held {a.hold}s with no reboot. >> kill -CONT (thaw) and watch {a.thaw_watch}s")
    dev.shtext(f"kill -CONT {' '.join(frozen)}")
    thawed = time.time()
    while True:
        el = time.time() - thawed
        if el >= a.thaw_watch:
            break
        time.sleep(min(3, max(1, a.thaw_watch - el)))
        live, up = alive()
        el = time.time() - thawed
        if not live:
            hr(); warn(f"VERDICT: HYPOTHESIS 2 — thaw-triggered. Survived {a.hold}s frozen, "
                       f"rebooted {el:.0f}s after kill -CONT  (uptime={'lost' if up is None else f'{up:.0f}s'}).")
            warn("  => freezing is safe; the reboot is provoked by resuming the writers.")
            sys.exit(0)
        info(f"  [thaw+{el:5.0f}s] alive  uptime={up:.0f}s")

    hr(); ok(f"VERDICT: NEITHER — survived {a.hold}s frozen AND {a.thaw_watch}s after thaw.")
    ok("  => reboot is NOT from freeze/thaw alone; suspect dd read pressure / NAND contention during the dump.")
    sys.exit(0)

def dump_ubi_volumes(dev, a, vols, sec, only_names=None):
    """Quiesce the rw ubifs, then dump /dev/ubiX_Y. Returns fail count.

    Mode is --freeze:
      stop  (default): reversible kill -STOP + sync (no kill/remount) — best-effort:
                       can't stop the kernel commit/GC thread, so a long read of a
                       busy volume may still tear (md5-verify catches it).
      kill           : kill the writers so they release the mount, then
                       `mount -o remount,ro` it — genuinely stops ubifs (commit +
                       no bgt/GC), so the read can't tear. DESTRUCTIVE: services are
                       killed and not restarted; caller auto-reboots on a clean run.
      live           : read live, touch nothing."""
    ro_mounts = []   # remounted ro by us -> restore rw in finally
    frozen = []      # --freeze stop: STOPped pids -> CONT in finally
    killed = False   # did we kill writers? (caller decides whether to auto-reboot)
    fail = 0
    if a.freeze == 'live':
        warn("  --freeze live: reading live rw volume(s); image may be inconsistent / md5 may differ")
    elif a.freeze == 'stop':
        frozen, auto = collect_writers(dev, a)
        if frozen:
            info(f"  --freeze stop: kill -STOP {len(frozen)} writer(s): {' '.join(frozen)}"
                 f"   {C['B']}(reversible, best-effort){C['Z']}")
            dev.shtext(f"kill -STOP {' '.join(frozen)}; sync")
        elif auto:
            info("  no rw-volume writers to freeze (read-only volumes)")
    else:   # 'kill': consistent but destructive (opt-in)
        writers, _ = collect_writers(dev, a)
        if writers:
            warn(f"  killing {len(writers)} writer(s) for a consistent image: {' '.join(writers)}"
                 f"   {C['B']}(reboot to restore; --freeze stop=reversible, --freeze live=read live){C['Z']}")
            dev.shtext(f"kill -9 {' '.join(writers)}"); killed = True; time.sleep(1)
            still = [p for ps in dev.scan_writers().values() for p, _ in ps]
            if still:   # respawned (procd/systemd) -> one more swing before remount
                dev.shtext(f"kill -9 {' '.join(still)}"); time.sleep(1)
        for mnt in dev.rw_ubifs_mounts():
            if dev.remount(mnt, 'ro'):
                ro_mounts.append(mnt); info(f"  remounted ro: {mnt}")
            else:
                warn(f"  could NOT remount {mnt} ro (writer respawned?) — its image may be inconsistent")
                fail = 1   # not a clean quiesce -> counts as "not smooth" (blocks auto-reboot)
    dumped = set()
    try:
        for v in vols:
            nm = os.path.basename(v)
            if only_names and nm not in only_names and v not in only_names:
                continue
            out = os.path.join(a.out, nm + '.ubifs')
            info(f">> {nm}  ({v})  ->  {out}")
            if not dev.stream(v, out, a.dd_timeout):
                err("   transport error"); fail = 1; continue
            got = os.path.getsize(out)
            if a.verify:
                dh, hh = dev.dev_md5(v), host_md5(out)
                if dh and dh == hh: ok(f"   md5 OK  {hh}  ({hsize(got)})"); dumped.add(nm)
                else: warn(f"   md5 MISMATCH dev={dh} host={hh} (live writes? still inconsistent)"); fail = 1
            else:
                ok(f"   wrote {hsize(got)}"); dumped.add(nm)
    finally:
        for mnt in ro_mounts:
            dev.remount(mnt, 'rw')
        if ro_mounts:
            info(f"  restored rw: {' '.join(ro_mounts)}")
        if frozen:
            dev.shtext(f"kill -CONT {' '.join(frozen)}")
            info(f"  thawed: {' '.join(frozen)}")
    if dumped:
        make_flashable(a, sec, dumped)
    return fail, killed

def reboot_after_kill(dev, killed, fail):
    """We kill writers to get a consistent image; that's destructive, so restore the
    device by rebooting once the dump is done — but ONLY on a clean run. If anything
    went wrong (fail), stop and leave it up so the user can inspect."""
    if not killed:
        return
    if fail:
        warn("Writers were killed but the run had warnings — NOT rebooting. "
             "Inspect the output, then `adb reboot` manually to restore services.")
    else:
        warn("Writers were killed for a consistent image — rebooting to restore services…")
        dev.adb('reboot')

def cmd_dump(dev, a):
    parts, umtds, sclass, sec = inventory(dev, a)
    os.makedirs(a.out, exist_ok=True)
    info(f"transport={C['B']}{dev.transport}{C['Z']}  storage={C['B']}{sclass}{C['Z']}  "
         f"root={C['B']}{dev.root}{C['Z']}  out={C['B']}{a.out}{C['Z']}")
    skip_globs = (a.skip_list.split() if a.skip_list is not None else []) + (a.skip or [])
    want = a.names

    sel = []
    for d, sz, nm in parts:
        is_ubi = is_ubi_backed(d, umtds)
        p = dump_plan(nm, sclass, is_ubi, skip_globs)
        if want:
            if nm not in want and d not in want:
                continue
            if p.action == 'SKIP':   # explicitly named, but not readable here -> report & stop
                warn(f">> {nm} ({d}): SKIP — {p.role}, {p.note}.")
                hint = {
                    'ubi':       'Capture it with: adbdump.py ubidump',
                    'skip-list': 'Remove it from --skip / --skip-list to dump it.',
                }.get(p.reason, f'Auto-skipped ({p.role}); a raw read is unsafe and not done here.')
                warn(f"   {hint}")
                continue
        else:
            if p.action == 'SKIP':   # full-auto: UBI handled below; others just skipped
                if not is_ubi:
                    info(f"SKIP {nm}  ({p.role}, {p.note})")
                continue
        sel.append((d, sz, nm))

    fail = 0; killed = False
    for d, sz, nm in sel:
        rc = dump_one_mtd(dev, a, d, sz, nm)
        if rc == 2:
            sys.exit(2)
        fail |= rc

    # full-auto: active UBI partitions were skipped above -> auto-convert to ubidump
    if not want and umtds:
        vols = ubi_vols_from_sec(sec)
        if vols:
            hr(); info(f"{C['B']}active UBI -> ubidump ({len(vols)} volume(s), quiescing writers){C['Z']}")
            f2, killed = dump_ubi_volumes(dev, a, vols, sec)
            fail |= f2

    if not sel and not (not want and umtds):
        err("Nothing to dump (check names / skip list)."); sys.exit(1)
    hr()
    (ok if not fail else warn)(f"DONE{'' if not fail else ' with warnings'} -> {a.out}")
    reboot_after_kill(dev, killed, fail)
    sys.exit(fail)

def cmd_ubidump(dev, a):
    _, sec = setup(dev, a)
    os.makedirs(a.out, exist_ok=True)
    info(f"transport={C['B']}{dev.transport}{C['Z']}  root={C['B']}{dev.root}{C['Z']}  out={C['B']}{a.out}{C['Z']}")
    vols = ubi_vols_from_sec(sec)
    if not vols:
        err("No UBI volumes (/dev/ubiX_Y)."); sys.exit(1)
    fail, killed = dump_ubi_volumes(dev, a, vols, sec, only_names=a.names or None)
    hr()
    (ok if not fail else warn)(f"DONE -> {a.out}")
    info(f"Extract on host:  ubireader_extract_files {a.out}/<vol>.ubi   (or <vol>.ubifs if kept)")
    reboot_after_kill(dev, killed, fail)
    sys.exit(fail)

# ---------------------------------------------------------------------- main ---
def main():
    # Shared option groups live in parent parsers so each subcommand's --help and
    # usage line show only the options it actually reads.
    common = argparse.ArgumentParser(add_help=False)           # every subcommand (connect/root/transport)
    common.add_argument('--adb', default=os.environ.get('ADB', 'adb'))
    common.add_argument('-s', dest='serial', default='')
    common.add_argument('--root', default='auto', choices=['auto', 'none', 'adbroot', 'su', 'custom'])
    common.add_argument('--root-cmd', dest='root_cmd', default='', help="custom root wrapper, e.g. 'su -c' (implies --root custom)")
    common.add_argument('--transport', default='auto', choices=['auto', 'execout', 'encoded', 'nc', 'raw'])
    common.add_argument('--ncport', type=int, default=15999)
    common.add_argument('--push-bin', dest='push_bin', default='', help='push a static busybox/helper when device utils are thin')
    common.add_argument('--allow-corrupt', dest='allow_corrupt', action='store_true', help='permit binary-unsafe raw PTY transport')

    freezeopt = argparse.ArgumentParser(add_help=False)        # dump / ubidump / freezehold
    freezeopt.add_argument('--writers', nargs='*', help='target these writer process names instead of auto-detecting')

    readopts = argparse.ArgumentParser(add_help=False)         # dump / ubidump (write files, verify, freeze, ubinize)
    readopts.add_argument('-o', '--out', default='out')
    readopts.add_argument('--dd-timeout', dest='dd_timeout', type=int, default=1800)
    readopts.add_argument('--no-verify', dest='verify', action='store_false')
    readopts.add_argument('--freeze', choices=['kill', 'stop', 'live'], default='stop',
                          help="rw-UBI consistency: stop=reversible kill -STOP + sync (best-effort, "
                               "md5 catches tearing); kill=kill writers + remount ro (consistent but "
                               "destructive -> auto-reboot on success); live=read live  (default: stop)")
    readopts.add_argument('--no-ubinize', dest='no_ubinize', action='store_true', help='keep only .ubifs, do NOT build flashable .ubi')
    readopts.add_argument('--keep-ubifs', dest='keep_ubifs', action='store_true', help='keep the .ubifs after building .ubi (default: remove the duplicate)')

    p = argparse.ArgumentParser(prog='adbdump.py', description='Portable ADB flash dumper.')
    sub = p.add_subparsers(dest='cmd', required=True, metavar='{probe,list,dump,ubidump,freezehold}')

    sp = sub.add_parser('probe', parents=[common], help='fingerprint device + flash manifest')
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser('list', parents=[common], help='partition table + per-part dump plan')
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser('dump', parents=[common, readopts, freezeopt], help='dump safe MTD partitions + UBI volumes')
    sp.add_argument('--skip-list', dest='skip_list', default=None, help='extra space-separated name globs to skip (UBI/special partitions are already auto-skipped by role)')
    sp.add_argument('--skip', action='append', default=[], help='extra name to skip (repeatable)')
    sp.add_argument('--probe-timeout', dest='probe_timeout', type=int, default=15, help='seconds for the 1-page wedge probe (default 15)')
    sp.add_argument('--no-probe', dest='no_probe', action='store_true')
    sp.add_argument('--force', action='store_true')
    sp.add_argument('names', nargs='*')
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser('ubidump', parents=[common, readopts, freezeopt], help='dump /dev/ubiX_Y volumes (frozen)')
    sp.add_argument('names', nargs='*')
    sp.set_defaults(func=cmd_ubidump)

    sp = sub.add_parser('freezehold', parents=[common, freezeopt], help='diagnostic: freeze writers, hold, watch for reboot')
    sp.add_argument('--hold', type=int, default=120, help='seconds to hold writers STOPped while watching for a reboot (default 120)')
    sp.add_argument('--thaw-watch', dest='thaw_watch', type=int, default=30, help='seconds to watch after kill -CONT for a thaw-triggered reboot (default 30)')
    sp.set_defaults(func=cmd_freezehold)

    a = p.parse_args()
    if a.root_cmd:
        a.root = 'custom'

    dev = Device(a)
    if shutil.which(a.adb) is None and not os.path.isfile(a.adb):
        err(f"adb not found: {a.adb!r} (set --adb or $ADB)."); sys.exit(1)
    rc, _ = dev.adb('get-state', timeout=10)
    if rc != 0:
        err("No ADB device (set -s SERIAL?)."); sys.exit(1)
    a.func(dev, a)

if __name__ == '__main__':
    main()
