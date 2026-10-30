#!/usr/bin/python3 -I
# bench/rehearsal_probe.py TOKEN: the R0 write probes of the rebuild rehearsal, run inside bench/rehearsal_rr.sh by
# bench/rehearsal_selftest.sh, which copies it into ~/rebuild-rehearsal/verify first (the repository is not visible
# inside).  In bench/: run it only when the maintainer asks (AGENTS.md).
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/r0-probe.py, sha256 ca9543a7... in
# results/rebuild-rehearsal-r0-20261030.txt): that copy counted every error as a refusal, ENOENT too, and did not check
# where it ran.
"""R0 write probes, run inside rr.  For each production path: create a file, create a directory, open an existing
file for writing (no byte is written), delete a name, and rename an existing file to itself.  Every one must fail.
The delete probe names a file that does not exist, and the rename is to the same name, so that neither could change
anything even if the kernel allowed it; on a read-only mount the kernel refuses both before it looks the name up
(EROFS), which an allowed operation on a missing name would not give (ENOENT).  So create, mkdir, delete and rename
count as refused only with EROFS.  The open of an existing file counts as refused with EROFS, or with ETXTBSY (a
program that runs, such as the engine) or EACCES (a file of root's), which the kernel checks before the mount.  Any
other error (ENOENT: the path is not production's) and any success count as bad; an unexpected success of a create is
undone at once.  It refuses to run (exit 2) unless the sandbox marker is in /home/thc1006/dev and the views of
production, ~/.config/strata and /etc are on read-only mounts, which is the case inside rr and not on the host."""
import errno, os, sys
P = "/home/thc1006/rebuild-rehearsal/prod/dev"
C = "/home/thc1006/rebuild-rehearsal/prod/cuda-12.9"
if len(sys.argv) != 2 or not sys.argv[1].isalnum():
    sys.exit("usage: rehearsal_probe.py TOKEN (letters and digits)")
tok = sys.argv[1]
def read_only(p):
    try:
        return bool(os.statvfs(p).f_flag & os.ST_RDONLY)
    except OSError:
        return False
if not (os.path.exists("/home/thc1006/dev/.rehearsal-sandbox")
        and all(read_only(p) for p in (P + "/strata", C, "/home/thc1006/.config/strata", "/etc"))):
    print("rehearsal_probe: not inside the rehearsal's sandbox (no marker, or a view on a writable mount): nothing run",
          file=sys.stderr)
    sys.exit(2)
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
def probe(what, fn, ok=(errno.EROFS,)):
    global bad
    try:
        r = fn()
    except OSError as e:
        name = errno.errorcode.get(e.errno, str(e.errno))
        if e.errno in ok:
            return f"{what} refused ({name})"
        bad += 1
        return f"{what} UNEXPECTED ({name})"
    bad += 1
    if isinstance(r, int):
        os.close(r)
    return f"{what} ALLOWED (unexpected)"
def create(d):
    p = f"{d}/.rr-probe-{tok}"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd); os.unlink(p)            # undone at once; reported as allowed
def mkdir(d):
    p = f"{d}/.rr-probe-dir-{tok}"
    os.mkdir(p); os.rmdir(p)
for d, f in targets:
    out = [probe("create", lambda: create(d)), probe("mkdir", lambda: mkdir(d)),
           probe("delete", lambda: os.unlink(f"{d}/.rr-probe-absent-{tok}"))]
    if f:
        out += [probe("open for writing", lambda: os.open(f, os.O_WRONLY | os.O_APPEND),
                      (errno.EROFS, errno.ETXTBSY, errno.EACCES)),
                probe("rename to itself", lambda: os.rename(f, f))]
    else:
        out += [probe("rename dir to itself", lambda: os.rename(d, d))]
    print(f"{d}: " + "; ".join(out))
print(f"probes allowed or with an unexpected error: {bad}")
sys.exit(1 if bad else 0)
