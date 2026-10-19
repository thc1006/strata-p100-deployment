### Area

[Engine]  (speed, kernels, models, the server and API)

### Desktop app, Terminal or source?

Source (START-HERE.bat, setup.sh or a build)

### What happened

`ple_reader_test --selftest` writes its test tables under fixed names in the temp directory (`ple_format_selftest.gguf`, `ple_reader_selftest_90.bin` and `ple_reader_selftest_110.bin`), and the ctest entry, `add_test(NAME ple_reader_selftest COMMAND ple_reader_test --selftest)`, passes no `--dir`. Two runs at the same time on one machine, for example ctest in a CUDA build and in a HIP build, overwrite and delete each other's tables while they are open.

I started two copies of the 0.2.0-beta selftest (`3d1fb48`, built for the CPU only) with the same `TMPDIR`, six times, the second one 0, 0.3, 1, 2, 4 or 6 s after the first. In all 6 pairs, at least one run failed:

- In two pairs, one run died with SIGBUS.
- In two pairs, one run printed `collect failed: PleReader: short read inside the table` and then hung, with all its threads waiting on futexes, until it was killed after 182 and 186 s. A run alone takes about 13 s.
- In the pair with no delay, both runs failed with `mapped open: PLE table size mismatch`. In the 6 s pair, both failed, one with `collect failed: PleReader: short read inside the table` and the other with `open: DirectFile: cannot open`, as did the second run of the 4 s pair.

With a separate `TMPDIR` for each run and the same six delays, all 12 runs passed. With 0.1.45 (`477ac13`), 5 of the 6 pairs failed, with SIGBUS, hangs and `cannot open` as here; those scripts and their output are in [`ple-selftest-shared-tmp-20261018.txt`](https://github.com/thc1006/strata-p100-deployment/blob/main/results/ple-selftest-shared-tmp-20261018.txt).

Adding `--dir ${CMAKE_CURRENT_BINARY_DIR}` to the `add_test` line would give each build its own files.

### Smallest steps or command that reproduce it

Built for the CPU only from 0.2.0-beta's `src/kernels/ngram.cpp`, `src/ngram/ple_reader.cpp`, `src/platform/direct_file.cpp` and `src/ngram/ple_reader_test.cpp`:

```sh
g++ -std=c++20 -O2 -pthread -Iinclude -Isrc src/kernels/ngram.cpp src/ngram/ple_reader.cpp src/platform/direct_file.cpp src/ngram/ple_reader_test.cpp -o ple_reader_test
```

Then, for each delay, two runs with the same `TMPDIR` (the file above has the whole script, `race.sh`; a run still alive after 180 s was killed):

```sh
( TMPDIR=$PWD/a nice -n 19 ./ple_reader_test --selftest > out_a.txt 2>&1; echo $? > rc_a ) &
sleep $off
( TMPDIR=$PWD/a nice -n 19 ./ple_reader_test --selftest > out_b.txt 2>&1; echo $? > rc_b ) &
wait
```

The first FAIL line of each run with the shared `TMPDIR` (rc 135 is SIGBUS, 143 the kill after 180 s):

```
delay 0    a rc=1    FAIL src/ngram/ple_reader_test.cpp:287: mapped open: PLE table size mismatch: 499 rows x 90 B = 44910 at offset 0, but the file holds 24660 from data_start 160
delay 0    b rc=1    FAIL src/ngram/ple_reader_test.cpp:287: mapped open: PLE table size mismatch: 2000 rows x 170 B = 340000 at offset 0, but the file holds 41480 from data_start 160
delay 0.3  a rc=135
delay 0.3  b rc=0
delay 1    a rc=135
delay 1    b rc=0
delay 2    a rc=143  FAIL src/ngram/ple_reader_test.cpp:65: bulk: collect failed: PleReader: short read inside the table
delay 2    b rc=0
delay 4    a rc=143  FAIL src/ngram/ple_reader_test.cpp:65: decode: collect failed: PleReader: short read inside the table
delay 4    b rc=1    FAIL src/ngram/ple_reader_test.cpp:358: open: DirectFile: cannot open <work>/r/shared-4/a/ple_reader_selftest_90.bin
delay 6    a rc=1    FAIL src/ngram/ple_reader_test.cpp:65: keep-alive, caller thread: collect failed: PleReader: short read inside the table
delay 6    b rc=1    FAIL src/ngram/ple_reader_test.cpp:358: open: DirectFile: cannot open <work>/r/shared-6/a/ple_reader_selftest_110.bin
```

### Reproduction status

Reproduced locally

### Strata version

0.2.0-beta (`3d1fb48`)

### Graphics card, memory and system

GPU not used (the selftest runs on the CPU) · 4× Tesla P100 16 GB · 62.8 GiB RAM · Ubuntu 22.04.5, kernel 6.8, g++ 11.4, TMPDIR on ZFS (OpenZFS 2.2.2)

