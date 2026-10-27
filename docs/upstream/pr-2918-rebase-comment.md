Dear @nilp0inter, thank you for reproducing this on 0.2.1. This PR removes those per-row calls for a locked table: the POSIX branch of the hint in `gather_batch` returns when `impl_->locked` is set. A table whose lock failed, `--ple-io mmap`, Windows and `--no-ple-prefetch` are unchanged.

I have rebased the branch onto main (`9ae93f7`, 0.2.1 with the 0.1.46.1 hotfix); it applied without conflicts. Built for the CPU only (g++ 11.4), `ple_reader_test --selftest` passes with `RLIMIT_MEMLOCK` at 8037 MiB, 64 KiB and 0. With the `locked` check removed again, it fails at 8037 MiB and 64 KiB: `a locked table: gather_batch made 256 row hints, not 0`. At 0 the lock is refused, so that case cannot run.

---

Not part of the comment as posted: the selftest runs at 8037 MiB, 64 KiB and 0, with and without the `locked` check, are in `results/pr2918-selftest-021-20261020.txt`.
