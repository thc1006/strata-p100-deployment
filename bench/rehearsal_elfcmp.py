#!/usr/bin/env python3
"""bench/rehearsal_elfcmp.py A B: compare two ELF files with nvcc's temporary file names (tmpxft_<8 hex>_<8 hex>, made
from process IDs) and the GNU build ID masked; print both sizes, the masked files' sha256 (16 digits) and the number of
bytes that differ; exit 0 when the masked files are equal.  The rebuild rehearsal's ~/rebuild-rehearsal/bin/elfcmp.py
(R7 in results/rebuild-rehearsal-r6-r7-20261030.txt).  It is in bench/: run it only when the maintainer asks
(AGENTS.md).  It writes nothing, and reads both files whole (66 MB for the engine).  Limits: masked bytes are not
compared, so two files that differ only there count as equal; it runs /usr/bin/readelf.  The copy that the
records ran found readelf through PATH; the comparison is the same."""
import hashlib, re, subprocess, sys
def norm(p):
    d = open(p, "rb").read()
    m = re.search(r"Build ID: ([0-9a-f]+)", subprocess.run(["/usr/bin/readelf", "-n", p], capture_output=True, text=True).stdout)
    if m:
        d = d.replace(bytes.fromhex(m.group(1)), bytes(len(m.group(1)) // 2))
    return re.sub(rb"tmpxft_[0-9a-f]{8}_[0-9a-f]{8}", b"tmpxft_00000000_00000000", d)
a, b = norm(sys.argv[1]), norm(sys.argv[2])
n = sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))
print(len(a), len(b), hashlib.sha256(a).hexdigest()[:16], hashlib.sha256(b).hexdigest()[:16], "bytes that differ:", n)
sys.exit(0 if a == b else 1)
