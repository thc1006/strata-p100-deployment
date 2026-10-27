I amended the commit with the test I offered. On Linux with glibc, `ple_reader_test` now counts the `MADV_WILLNEED` calls from `gather_batch`. Where the table can be locked, the selftest now fails with the `locked` check removed: "a locked table: gather_batch made 256 row hints, not 0". I also built the HIP (gfx1100) and SYCL engines here, but I have no AMD or Intel GPU to run them. The change to `ngram.cpp` is the same, and the description has the details.

---

Not part of the comment as posted: the selftest that fails with "256 row hints, not 0" is in `results/pr2918-test-mutants.txt`, and the HIP and SYCL builds are in `results/pr2918-builds.txt`.
