### Area

[Other]  (anything else, including setup and installation)

### Desktop app, Terminal or source?

Source (START-HERE.bat, setup.sh or a build)

### What happened

`sycl/tools/build.sh` sets `pipefail` and has no `exit`, so the script exits with the status of its last pipeline:

```sh
grep -E '^FAILED' $b/build.log | sed 's#.*/##' | sort | uniq | head -60
```

After a successful build this `grep` finds nothing and returns 1. After a failed compile it finds ninja's `FAILED:` lines and returns 0. The `BUILD EXIT` line that the script prints is correct.

With the script from 0.2.0-beta (`3d1fb48`), which is byte-identical to the one in 0.1.45 (`477ac13`):

- With `BUILD_DIR` set to the build directory of a small CMake project that builds, it printed `BUILD EXIT 0` and `errors: 0`, and exited with 1.
- With `BUILD_DIR` set to the same project with a compile error, it printed `BUILD EXIT 1` and `errors: 1`, and exited with 0.

With 0.1.45 it also exited with 1 in a SYCL build directory with nothing left to build, after printing `BUILD EXIT 0` and `errors: 0`.

The image's entrypoint is `bash -lc`, so in `docker run ... strata-sycl-dev "cd /work/$H && bash sycl/tools/build.sh strata" && <next step>`, the next step is skipped after a good build and runs after a failed compile. `docs/A770_SPEED_SPEC.md` already notes the first case and says not to chain tests behind the script.

Saving `${PIPESTATUS[0]}` right after the build and ending the script with `exit` on that value would fix it.

### Smallest steps or command that reproduce it

Two small CMake projects with one target named `strata` (Ninja, g++ 11.4), configured with `cmake -G Ninja`: `src-good/main.cpp` is `int main() { return 0; }`, `src-bad/main.cpp` is `int main() { return x; }`. Then, for each:

```sh
REPO=<work> BUILD_DIR=<work>/b-$kind bash sycl/tools/build.sh strata; echo "### build.sh exit status: $?"
```

Output with the script from 0.2.0-beta (sha256 `a5f95ab736c072b9160d523456454c8689791750cfae91f82f03e2a97374b122`, the same as 0.1.45's):

```
### good build:
[2/2] Linking CXX executable strata
BUILD EXIT 0
errors: 0
### build.sh exit status: 1
### bad build:
FAILED: [code=1] CMakeFiles/strata.dir/main.cpp.o
<work>/src-bad/main.cpp:1:21: error: ‘x’ was not declared in this scope
ninja: build stopped: cannot make progress due to previous errors.
BUILD EXIT 1
errors: 1
main.cpp.o
### build.sh exit status: 0
```

The 0.1.45 runs, the SYCL build directory included, are in [`sycl-build-sh-exit-20261018.txt`](https://github.com/thc1006/strata-p100-deployment/blob/main/results/sycl-build-sh-exit-20261018.txt).

### Reproduction status

Reproduced locally

### Strata version

0.2.0-beta (`3d1fb48`); `sycl/tools/build.sh` is unchanged since 0.1.45 (`477ac13`)

### Graphics card, memory and system

No Intel GPU (the script ran outside the strata-sycl-dev image) · 4× Tesla P100 16 GB · 62.8 GiB RAM · Ubuntu 22.04.5

