#!/bin/sh
# bench/rehearsal_shim_nvidia_smi.sh: the rebuild rehearsal's stand-in for nvidia-smi, installed as
# ~/rebuild-rehearsal/shim/nvidia-smi, first on the jobs' PATH (bench/rehearsal_rr.sh), where /dev has no NVIDIA device.
# It answers the one query whose answer setup.py needs (setup.py:999-1010) with the four GPUs as
# results/setup-20261016.log:6-11 and results/deploy-0.1.45-20261019.txt:122-125 record them, and exits 6 for any other
# query, which setup.py takes as unknown (setup.py:549-553).  It writes nothing.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
case "$*" in
  *--query-gpu=index,name,memory.total,compute_cap,driver_version*)
    for i in 0 1 2 3; do echo "$i, Tesla P100-PCIE-16GB, 16384, 6.0, 535.230.02"; done ;;
  *) exit 6 ;;
esac
