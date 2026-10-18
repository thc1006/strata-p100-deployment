### What happened

`ple_reader_test --selftest` writes its test tables under fixed names in the temp directory (`ple_format_selftest.gguf`, `ple_reader_selftest_90.bin` and `ple_reader_selftest_110.bin`), and the ctest entry, `add_test(NAME ple_reader_selftest COMMAND ple_reader_test --selftest)`, passes no `--dir`. Two runs at the same time on one machine, for example ctest in a CUDA build and in a HIP build, overwrite and delete each other's tables while they are open.

I started two copies of the 0.1.45 selftest (`477ac13`, built for the CPU only) with the same `TMPDIR`, six times, the second one 0, 0.3, 1, 2, 4 or 6 s after the first. In 5 of the 6 pairs, at least one run failed:

- In two pairs, one run died with SIGBUS.
- In three pairs, one run printed one or two FAIL lines and then hung, with all its threads waiting on futexes, until it was killed after 182 to 764 s. A run alone takes about 13 s. The first FAIL line was `Q5_1: direct: row 908 differs from the format's own dequantizer` in one run and `collect failed: PleReader: short read inside the table` in the other two.
- In one of those three pairs, the other run failed too, starting with `open: DirectFile: cannot open .../ple_reader_selftest_90.bin`.

With a separate `TMPDIR` for each run and the same six delays, all 12 runs passed.

Adding `--dir ${CMAKE_CURRENT_BINARY_DIR}` to the `add_test` line would give each build its own files.

The scripts and their output are in [`ple-selftest-shared-tmp-20261018.txt`](https://github.com/thc1006/strata-p100-deployment/blob/main/results/ple-selftest-shared-tmp-20261018.txt).

### Strata version, GPU, OS

0.1.45 (477ac13) · GPU not used (the selftest runs on the CPU) · Ubuntu 22.04.5, kernel 6.8, g++ 11.4, TMPDIR on ZFS (OpenZFS 2.2.2)

### Engine log

_No response_
