TL;DR: on a ZFS root, the per-row `MADV_WILLNEED` hint in `PleTable::gather_batch` makes ZFS read parts of the PLE table from disk on every prompt, even with `--ple-io ram`. On this machine (4x P100, IQ3_XXS, 0.1.44.1), each new ~16K-token prompt read 16-18 GB from disk and prefilled at 458-504 tok/s. With `--no-ple-prefetch`, it read 0.00 GB and prefilled at 704-709 tok/s.

This is the same hint this issue found, measured on 0.1.44.1, with one more cost on ZFS: disk reads. `--no-ple-prefetch` still fixes it, so I am not asking to reopen. If you would rather track the ZFS side in a new issue, I can open one.

Per new ~16K-token prompt, 0.1.44.1, `--ple-io ram`, same binary:

| PLE prefetch hint | prefill tok/s | TTFT s | disk read | engine `madvise` calls | `zpl_fadvise` calls |
|---|---:|---:|---:|---:|---:|
| on (default) | 458-504 | 31.8-35.0 | 16.4-18.3 GB | ~256,000 | ~256,000 |
| off (`--no-ple-prefetch`) | 704-709 | 22.6-22.8 | 0.00 GB | 64 | ~2 |

How the hint reaches the disk:

1. On Linux, `gather_batch` calls `madvise(MADV_WILLNEED)` once per row of a mapped table (`--ple-io mmap` or `ram`). It checks `g_ple_prefetch` but not `impl_->locked` ([ngram.cpp#L554-L577](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/kernels/ngram.cpp#L554-L577)). The prompt path and the decode verify windows both make this call.
2. For a file mapping, the kernel passes the hint on as `vfs_fadvise(..., POSIX_FADV_WILLNEED)` ([mm/madvise.c#L261-L307](https://github.com/torvalds/linux/blob/v6.8/mm/madvise.c#L261-L307)).
3. OpenZFS's `zpl_fadvise` then calls `dmu_prefetch` for that range, even when the pages are already in the page cache ([zpl_file.c#L943-L960](https://github.com/openzfs/zfs/blob/zfs-2.2.2/module/os/linux/zfs/zpl_file.c#L943-L960)). `dmu_prefetch` checks only ZFS's own caches, so locked pages do not stop it. Each hint becomes an ARC prefetch of the 128 KiB record that holds the row. The ARC here is 8 GiB and the table is 26.8 GiB, so most of those records are read from disk again on every prompt.

The call counts come from perf and ftrace on three more prompts of the same size: 16 `madvise` calls per prompt token, and one `zpl_fadvise` and one `dmu_prefetch` for each. ben0112 offered above to measure whether the cost is the syscall or the prefetch. Here, each call spent 92 µs on average inside `zpl_fadvise`.

ben0112's report in #2571 says their model is on ZFS too. Their machine has much more RAM, so the cost there may be time in `zpl_fadvise` more than disk reads; I have not measured that. If your 2x A4000 machine does not use ZFS, that may be part of why the hint slowed their machine and not yours.

A possible fix is the one ben0112 suggested above: skip the per-row hint when the table is locked. On Linux, only `hint()` checks `g_ple_prefetch` (the other three checks are inside `#if defined(_WIN32)`). So for a locked table, that change should behave like `--no-ple-prefetch`, the "off" row above. If you post a patch, I will test it here with the same prompts.

<details><summary>Environment and method</summary>

- Strata 0.1.44.1 (`296b59d`) and 0.1.43.1 (`a0116b2`), both built from source with CUDA 12.9.1 for sm_60, with the flags setup.py uses for a CUDA 12 build (Release, `STRATA_EXPERIMENTAL_SM60=ON`). The "on" and "off" rows above use the same 0.1.44.1 binary; only `--no-ple-prefetch` differs.
- 4x Tesla P100 PCIe 16 GB (driver 535.230.02). VMware VM with 8 vCPUs (Xeon E5-2696 v4) and 62.8 GiB of RAM.
- Ubuntu 22.04.5, kernel 6.8.0-138. Root on ZFS: OpenZFS 2.2.2-0ubuntu9.4 (Ubuntu's zfs-linux 2.2.2 source has the same `zpl_fadvise` as upstream 2.2.2), recordsize 128K, lz4. I set `zfs_arc_max` to 8 GiB to leave RAM for the locked table and the experts. A larger ARC would change the disk numbers.
- Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS. The PLE table is the second shard: 320,001,536 rows x 90 B = 26.8 GiB.
- Server: 4 slots, layer split 13,26,38 over the four cards. Engine flags (abridged, paths left out): `--max-context 32768 --kv int8 --expert-cache auto --prefill 2048 --pool-workers 6 --batch-groups auto --vram-reserve-mib 1536 --ple-io ram --spec 4 --spec-min-p 0.5 --resident-experts --trim-stage-weights --no-kv-grow --conversation-cache-mib 8192`, plus `--no-ple-prefetch` for the "off" row.
- Every run is a fresh server start under systemd with `LimitMEMLOCK=infinity`. Order of the runs: 0.1.43.1, 0.1.44.1, 0.1.44.1 with the hint on, 0.1.44.1, 0.1.43.1.
- Prompts: slices of Strata's own docs, ~16K tokens each, with a random nonce first so no prefix is reused. Thinking off, temperature 0, 32 tokens out, one request at a time. Speed and TTFT come from three slices without tracing. The call counts come from three other slices with perf and ftrace on.
- Disk reads: `/sys/block/sda/sda4/stat` (the pool's partition) before and after each request. ARC misses: `prefetch_data_misses` in `/proc/spl/kstat/zfs/arcstats`.
- Calls: `perf stat -e syscalls:sys_enter_madvise -p <engine pid>`, and the ftrace function profiler on `vfs_fadvise`, `zpl_fadvise` and `dmu_prefetch` (all CPUs; the time includes waits).
- OpenZFS 2.3.4 and 2.4.4 call `dmu_prefetch` the same way. Master calls `dmu_prefetch_user`, the same prefetch with a cap on the bytes in flight.
- Not tested: a filesystem other than ZFS on this machine, a larger ARC, `--ple-io mmap` (where the hint is meant to help, #1984), and ben0112's machine.

</details>

<details><summary>All numbers, and 0.1.44.1 against 0.1.43.1</summary>

Untraced prompts, one at a time (prefill rate as the engine reports it; disk = the pool's partition; misses = ARC prefetch misses):

| run | hint | tokens | prefill tok/s | TTFT s | disk read GB | ARC prefetch misses |
|---|---|---:|---:|---:|---:|---:|
| 0.1.44.1 | on | 15,985 | 503.6 | 31.79 | 16.39 | 125,028 |
| 0.1.44.1 | on | 16,012 | 458.3 | 34.99 | 18.05 | 137,679 |
| 0.1.44.1 | on | 16,000 | 470.9 | 34.03 | 18.29 | 139,565 |
| 0.1.44.1, 1st start | off | 15,986 | 708.3 | 22.63 | 0.00 | 0 |
| 0.1.44.1, 1st start | off | 16,012 | 703.7 | 22.81 | 0.00 | 0 |
| 0.1.44.1, 1st start | off | 16,000 | 707.5 | 22.67 | 0.00 | 1 |
| 0.1.44.1, 2nd start | off | 15,987 | 706.7 | 22.67 | 0.00 | 2 |
| 0.1.44.1, 2nd start | off | 16,012 | 703.8 | 22.81 | 0.00 | 0 |
| 0.1.44.1, 2nd start | off | 16,001 | 709.2 | 22.62 | 0.00 | 0 |

Misses x 128 KiB match the disk reads (125,028 x 131,072 B = 16.39 GB).

Traced prompts (three other slices; perf counts the engine's system calls, ftrace counts on all CPUs, so the few `zpl_fadvise` calls in the "off" row need not be the engine's):

| hint | tokens, 3 prompts | `madvise` (engine) | `vfs_fadvise` | `zpl_fadvise` | `dmu_prefetch` | time in `zpl_fadvise` | prefill tok/s while traced | disk read GB per prompt |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| on | 47,885 | 768,048 | 767,856 | 767,864 | 767,892 | 70.8 s, 92 µs per call | 464.8-470.8 | 15.65-16.35 |
| off (2nd start) | 47,883 | 192 | 0 | 5 | 102 | 0.1 ms | 702.8-704.8 | 0.00-0.01 |

With the hint on, the time inside `zpl_fadvise` is about 24 s per prompt and TTFT is 9-12 s longer. The gather runs beside the GPU work, so only part of that time adds to TTFT.

The engine's own `file tier I/O this request: the OS read ...` line showed 0.59-1.05 GB per prompt with the hint on, against 16-18 GB read from the disk. Most of the reads are ZFS's asynchronous prefetch, which `/proc/self/io` does not charge to the engine (an earlier probe here put them on ZFS's `z_rd_int` threads). So that line does not show the size of the problem.

0.1.43.1 shows the same. Earlier the same day, with the same three prompts but an earlier config (12,24,36 split, `--batch-groups 4`, no conversation cache) and the whole-disk counter: 407-440 tok/s and 15.5-17.8 GB read per prompt with the hint, 670-682 tok/s and 0.00-0.04 GB without it.

Short prompts (`tools/serve_load.py`, 512 tokens out, total tok/s for 1 / 2 / 4 clients): hint on 32.9 / 59.4 / 100.5; off 35.6 / 60.3 / 100.2 and 33.9 / 60.4 / 100.6 (two fresh starts). There is only one run with the hint on, and the two "off" starts differ by 1.7 tok/s, so I would not read a decode difference into this.

0.1.44.1 against 0.1.43.1, both with `--no-ple-prefetch`, two fresh starts each (run order 0.1.43.1, 0.1.44.1, 0.1.44.1, 0.1.43.1; the hint-on run sat between the two 0.1.44.1 runs):

| | 0.1.43.1 | 0.1.44.1 |
|---|---|---|
| short prompts, total tok/s, 1 / 4 clients | 34.0 / 100.3, 33.4 / 100.3 | 35.6 / 100.2, 33.9 / 100.6 |
| ~4.4K prompt, prefill tok/s (median of 3) | 392.3, 391.4 | 394.8, 397.0 |
| ~32K prompt, prefill tok/s (median of 3) | 778.0, 782.0 | 784.1, 783.8 |
| new ~16K prompts, prefill tok/s | 699.0-707.0, 699.8-706.6 | 703.7-708.3, 703.8-709.2 |
| four ~16K prompts at once, last TTFT s | 94.6, 94.5 | 94.0, 94.2 |

Within about 1% everywhere except one-client short prompts, where two starts of the same version already differ by up to 1.7 tok/s. 0.1.44.1 is ahead in both pairs on the ~4.4K prompt (0.6-1.4%), and by less on the ~32K prompt and the four-prompt burst.

</details>

<details><summary>Server log lines (0.1.44.1, excerpt)</summary>

```text
# --ple-io ram, hint on (default)
strata generate: layer split across 4 GPUs: CUDA0, then CUDA1,2,3 (split 13,26,38)
strata generate: PLE table locked in RAM (--ple-io ram) in 7.0 s
strata generate: session is up (engine 0.1.44.1)
strata serve: prompt 15985 tokens = 0 reused + 15985 read in 31741 ms (503.6 tok/s), 32 generated in 724 ms (44.2 tok/s), drafts accepted 17 of 21, 1 checkpoints
strata serve: file tier I/O this request: the OS read 1054.3 MB from storage (405 major faults) for 0.0 MB of expert reads
strata serve: prompt 16012 tokens = 0 reused + 16012 read in 34938 ms (458.3 tok/s), 32 generated in 664 ms (48.2 tok/s), drafts accepted 22 of 27, 1 checkpoints
strata serve: file tier I/O this request: the OS read 629.3 MB from storage (23 major faults) for 0.0 MB of expert reads
strata serve: prompt 16000 tokens = 0 reused + 16000 read in 33976 ms (470.9 tok/s), 32 generated in 983 ms (32.5 tok/s), drafts accepted 14 of 32, 1 checkpoints
strata serve: file tier I/O this request: the OS read 591.9 MB from storage (0 major faults) for 0.0 MB of expert reads

# same binary, --no-ple-prefetch (second start)
strata generate: session is up (engine 0.1.44.1)
strata serve: prompt 15987 tokens = 0 reused + 15987 read in 22622 ms (706.7 tok/s), 32 generated in 764 ms (41.9 tok/s), drafts accepted 16 of 24, 1 checkpoints
strata serve: file tier I/O this request: the OS read 0.0 MB from storage (0 major faults) for 0.0 MB of expert reads
strata serve: prompt 16012 tokens = 0 reused + 16012 read in 22751 ms (703.8 tok/s), 32 generated in 661 ms (48.4 tok/s), drafts accepted 21 of 27, 1 checkpoints
strata serve: file tier I/O this request: the OS read 0.0 MB from storage (0 major faults) for 0.0 MB of expert reads
strata serve: prompt 16001 tokens = 0 reused + 16001 read in 22561 ms (709.2 tok/s), 32 generated in 783 ms (40.8 tok/s), drafts accepted 21 of 32, 1 checkpoints
strata serve: file tier I/O this request: the OS read 0.0 MB from storage (0 major faults) for 0.0 MB of expert reads
```

</details>

---

Not part of the comment as posted: the files in `results/` that hold its numbers.

- The TL;DR, the first table, and the call counts and the 92 µs below "How the hint reaches the disk": `AB-0.1.44.1.md`, which summarizes `v441pf-probe.txt`, `v441-1-probe.txt` and `v441-2-probe.txt` (untraced) and `v441pf-traced-trace.txt` and `v441-2-traced-trace.txt` (traced).
- Step 3: the 128 KiB record, `zfs-dataset-20261022.txt`; the ARC at its 8 GiB cap, `arc_max_gib` in `v441-1-monitor-all.json`; the 26.8 GiB table, `iq3s-memory-budget-20261019.txt`.
- "Environment and method": the GPUs, the driver and the CPU, `setup-20261016.log`; Ubuntu 22.04.5 and the VMware VM, `host-os-20261025.txt`; the kernel and the OpenZFS module, `host-versions-20261024.txt`; the 90 B a row, shard 2's 28,800,138,432 bytes in `iq3s-model-files-20261019.txt` over its rows; the 8 vCPUs, `AB-0.1.45-ple-hint.md`; 62.8 GiB of RAM and the 320,001,536 rows, `v441pf-engine-lines.txt`; recordsize 128K and lz4, `zfs-dataset-20261022.txt`.
- "All numbers": the untraced table and the misses × 128 KiB, the three `*-probe.txt` files above; the traced table and the paragraph after it, `AB-0.1.44.1.md` and the two `*-traced-trace.txt` files above; the engine's `the OS read` figures, `v441pf-engine-lines.txt`; 0.1.43.1 earlier the same day, `ple_prefetch_console.txt` and `engine-log-20261016-extract.txt`; the short prompts and the table of 0.1.44.1 against 0.1.43.1, `AB-0.1.44.1.md`.
- "Server log lines": the start with the hint on, `v441pf-engine-lines.txt`; the second start with `--no-ple-prefetch`, `strata-0.1.44.1-log-extract-20261022.txt`.
