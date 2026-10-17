#!/usr/bin/env bash
# Build the engine of a Strata tree for these P100s (sm_60, CUDA 12.9 in ~/cuda-12.9) with the CMake options that
# tree's own setup.py uses for a CUDA 12 build (0.1.45 adds -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_MMQ_KQUANTS=ON; 0.1.43.1
# and 0.1.44.1 do not, and their CMake defaults apply).  Refuses the deployed tree (~/dev/strata).  For version A/B tests
# (docs/runbooks/upgrade-strata.md).  Low priority on CPUs 1-7, so a running service keeps CPU 0.
# The tree needs third_party/llama.cpp at the commit its setup.py pins (LLAMA_CPP_COMMIT): copy it from a tree with
# the same pin.  Writes engine-cuda12/strata, libstrata_kq.so when the build made one (as setup.py does; the engine
# opens it only for K-quant expert formats, not for IQ3_XXS) and engine-cuda12/BUILD.json (server.py reads the
# version from it; its src is git-<sha>, not setup.py's source hash, so ./setup.sh in that tree compiles again).
# Usage: bench/build-engine.sh <tree> <version>        e.g.  bench/build-engine.sh ~/dev/strata-0.1.45 0.1.45
set -u -o pipefail
T=$(cd "${1:?tree}" && pwd) || exit 1
V=${2:?version}
cd "$T" || exit 1
[ "$T" = "$(cd "$HOME/dev/strata" 2>/dev/null && pwd)" ] && { echo "refusing: $T is the deployed tree"; exit 2; }
extra=()
grep -q '"-DSTRATA_Q6K_EXPERTS=ON"' setup.py && extra+=(-DSTRATA_Q6K_EXPERTS=ON)
grep -q '"-DSTRATA_MMQ_KQUANTS=ON"' setup.py && extra+=(-DSTRATA_MMQ_KQUANTS=ON)
[ -d third_party/llama.cpp ] || { echo "missing $T/third_party/llama.cpp"; exit 1; }
export PATH=$HOME/dev/strata/.venv/bin:$HOME/cuda-12.9/bin:$PATH
T0=$(date +%s)
nice -n 19 taskset -c 1-7 cmake -G Ninja -DCMAKE_MAKE_PROGRAM="$HOME/dev/strata/.venv/bin/ninja" -S . -B build-cuda12 \
  -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_TESTS=OFF "${extra[@]}" \
  -DCMAKE_CUDA_ARCHITECTURES=60 -DCMAKE_CUDA_COMPILER="$HOME/cuda-12.9/bin/nvcc" \
  -DCUDAToolkit_ROOT="$HOME/cuda-12.9" -DSTRATA_GGML_DIR="$T/third_party/llama.cpp" -DSTRATA_EXPERIMENTAL_SM60=ON \
  > build-config.log 2>&1 &&
nice -n 19 taskset -c 1-7 cmake --build build-cuda12 --target strata -j 6 > build.log 2>&1 ||
  { echo "build failed: see $T/build-config.log and $T/build.log"; tail -20 build.log; exit 1; }
mkdir -p engine-cuda12 && cp -p build-cuda12/strata engine-cuda12/strata || exit 1
if [ -e build-cuda12/libstrata_kq.so ]; then cp -p build-cuda12/libstrata_kq.so engine-cuda12/ || exit 1
else rm -f engine-cuda12/libstrata_kq.so; fi
src="git-$(git rev-parse --short=9 HEAD 2>/dev/null || echo unknown)"
git diff --quiet HEAD -- src include 2>/dev/null || src="$src+local-changes"
python3 - "$V" "$src" "$HOME" > engine-cuda12/BUILD.json <<'EOF'
import json, sys
v, src, home = sys.argv[1:4]
print(json.dumps({"source": "local", "version": v, "archs": [60], "vision": "none", "toolkit": 12,
                  "cuda_dirs": [f"{home}/cuda-12.9/bin", f"{home}/cuda-12.9/lib64"], "src": src, "vision_src": None},
                 indent=1))
EOF
echo "built $T in $(( $(date +%s) - T0 )) s: engine sha256 $(sha256sum < engine-cuda12/strata | cut -c1-16) ($src)"
