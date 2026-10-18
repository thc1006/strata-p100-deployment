### What happened

`sycl/tools/build.sh` sets `pipefail` and has no `exit`, so the script exits with the status of its last pipeline:

```sh
grep -E '^FAILED' $b/build.log | sed 's#.*/##' | sort | uniq | head -60
```

After a successful build this `grep` finds nothing and returns 1. After a failed compile it finds ninja's `FAILED:` lines and returns 0. The `BUILD EXIT` line that the script prints is correct.

With the script from 0.1.45 (`477ac13`):

- In a SYCL build directory with nothing left to build, the script printed `BUILD EXIT 0` and `errors: 0`, and exited with 1.
- With `BUILD_DIR` set to the build directory of a small CMake project with a compile error, it printed `BUILD EXIT 1` and `errors: 1`, and exited with 0.

The image's entrypoint is `bash -lc`, so in `docker run ... strata-sycl-dev "cd /work/$H && bash sycl/tools/build.sh strata" && <next step>`, the next step is skipped after a good build and runs after a failed compile. `docs/A770_SPEED_SPEC.md` already notes the first case and says not to chain tests behind the script.

Saving `${PIPESTATUS[0]}` right after the build and ending the script with `exit` on that value would fix it.

The commands and their output are in [`sycl-build-sh-exit-20261018.txt`](https://github.com/thc1006/strata-p100-deployment/blob/main/results/sycl-build-sh-exit-20261018.txt).

### Strata version, GPU, OS

0.1.45 (477ac13) · no Intel GPU; the script ran outside the strata-sycl-dev image · Ubuntu 22.04.5

### Engine log

_No response_
