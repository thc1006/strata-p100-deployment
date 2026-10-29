#!/usr/bin/python3 -I
# bench/rehearsal_probe.py TOKEN: the R0 write probes of the rebuild rehearsal (the rehearsal's r0-probe.py), run inside
# bench/rehearsal_rr.sh by bench/rehearsal_selftest.sh, which copies it into ~/rebuild-rehearsal/verify first (the
# repository is not visible inside).  In bench/: run it only when the maintainer asks (AGENTS.md).
"""R0 write probes, run inside rr.  For each production path: create a file, create a directory, open an existing
file for writing (no byte is written), delete a name, and rename an existing file to itself.  Every one must fail.
The delete probe names a file that does not exist, and the rename is to the same name, so that neither could change
anything even if the kernel allowed it; on a read-only mount the kernel refuses both before it looks the name up
(EROFS), which an allowed operation on a missing name would not give (ENOENT).  An unexpected success of a create
is undone at once and reported."""
import errno, os, sys
tok = sys.argv[1]
P = "/home/thc1006/rebuild-rehearsal/prod/dev"
C = "/home/thc1006/rebuild-rehearsal/prod/cuda-12.9"
targets = [  # (directory, an existing file in it or None)
    (P + "/strata", P + "/strata/strata-iq3_xxs.json"),
    (P + "/strata/engine-cuda12", P + "/strata/engine-cuda12/strata"),
    (P + "/strata/.venv", P + "/strata/.venv/pyvenv.cfg"),
    (P + "/Strata-data", None),
    (P + "/Strata-data/models", None),
    (P + "/Strata-data/models/IQ3_S", P + "/Strata-data/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf"),
    (P + "/Strata-data/models/IQ3_XXS", P + "/Strata-data/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf"),
    (P + "/Strata-data/packs", None),
    (P + "/Strata-data/packs/iq3_s", P + "/Strata-data/packs/iq3_s/experts.bin"),
    (P + "/Strata-data/packs/iq3_xxs", P + "/Strata-data/packs/iq3_xxs/index.txt"),
    (P + "/Strata-data/mtp", P + "/Strata-data/mtp/mtp-inventory.json"),
    (P + "/Strata-data/mtp/rt", P + "/Strata-data/mtp/rt/experts.bin"),
    (P + "/p100-q3-deployment", P + "/p100-q3-deployment/config/profile.conf"),
    (C, C + "/version.json"),
    ("/home/thc1006/.config/strata", "/home/thc1006/.config/strata/settings.json"),
    ("/etc", "/etc/hostname"),
    ("/etc/systemd/system", "/etc/systemd/system/strata-q3.service"),
    ("/usr/local/sbin", "/usr/local/sbin/strata-q3-health"),
]
bad = 0
def probe(what, fn):
    global bad
    try:
        r = fn()
    except OSError as e:
        return f"{what} refused ({errno.errorcode.get(e.errno, e.errno)})"
    bad += 1
    if isinstance(r, int):
        os.close(r)
    return f"{what} ALLOWED (unexpected)"
def create(d):
    p = f"{d}/.rr-probe-{tok}"
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise
    os.close(fd); os.unlink(p)            # undone at once; reported as allowed
    return None
def mkdir(d):
    p = f"{d}/.rr-probe-dir-{tok}"
    os.mkdir(p); os.rmdir(p)
for d, f in targets:
    out = [probe("create", lambda: create(d)), probe("mkdir", lambda: mkdir(d)),
           probe("delete", lambda: os.unlink(f"{d}/.rr-probe-absent-{tok}"))]
    if f:
        out += [probe("open for writing", lambda: os.open(f, os.O_WRONLY | os.O_APPEND)),
                probe("rename to itself", lambda: os.rename(f, f))]
    else:
        out += [probe("rename dir to itself", lambda: os.rename(d, d))]
    print(f"{d}: " + "; ".join(out))
print(f"probes allowed: {bad}")
sys.exit(1 if bad else 0)
