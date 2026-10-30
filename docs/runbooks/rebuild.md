# Rebuilding the deployment from scratch

- When to use: on a new VM, to build today's deployment where none of it exists.
- Not covered: this machine, and the loss of only `~/dev/strata`, `~/dev/Strata-data` or `~/cuda-12.9` on it. No procedure for a partial loss has been worked out or run. What each directory holds comes from these steps: `~/cuda-12.9` from step 2, `~/dev/Strata-data` from steps 4, 5 (the IQ3_XXS pack and the MTP files), 7 and 8.
- Do not run any step of this runbook on this machine. The steps are blocks to paste, without `set -e`, so a line that fails does not stop the lines after it. Here:
  - steps 1 and 3 fail, because their directories exist;
  - step 2 installs over the live `~/cuda-12.9`;
  - step 5 compiles again in the live tree and rewrites its config, and setup.py would move into `~/dev/Strata-data` the model files that other `~/dev/strata*` folders hold ([Before you start](#before-you-start));
  - in step 6 only the first line fails: the next lines rebuild `~/dev/strata-0.1.45`, and the last one replaces the live engine's directory;
  - step 7 moves a new download over the live IQ3_S shard 1, step 8 rewrites the live IQ3_S pack, and step 9 rewrites the live config without `--dry-run`;
  - steps 12–14 change the system with sudo, and step 15's engine screen stops the service while it runs.
- Scope: today's deployment, Strata 0.1.45 (`477ac13cb`) with GSQ-RCO IQ3_S and `--profile deployed --model iq3_s`. The commands that built 0.1.43.1 with IQ3_XXS on 2026-10-16 are this file at commit `68cb17f`.
- Last verified: steps 1–3 and 5–11 and the file checks of step 15 were rehearsed on 2026-10-30 on this machine, in a sandbox, while the service ran ([the rehearsal](#the-rehearsal-of-2026-10-30)). Some inputs were not fetched as the steps fetch them (they were copies or links of production's, or an earlier step's download); each step says which, and which record covers it. Step 4 was not downloaded. Steps 0 and 12–14 change the system or need the GPUs, so they were not rehearsed. The whole procedure has not been run on a new VM.
- Prerequisites:
  - The account is `thc1006`, uid 1003. `strata-q3.service` and the logrotate file in `systemd/`, the preflight, watchdog and `check-deployment.sh` in `scripts/`, the two config snapshots and `deployed-artifacts.sha256` (the CUDA libraries) in `config/`, and `scripts/test.sh` hard-code `/home/thc1006`; the drop-in directory `systemd/user-1003.slice.d/` and `check-deployment.sh` hard-code the uid. To use another account, change these files first; otherwise the comparisons below do not match either.
  - At least 233.1 GiB (250.3 GB) of space by apparent size (ZFS's compression makes the disk use smaller): `~/dev/Strata-data` with both models 217.8 GiB (`results/strata-data-du-20261021.txt`), `~/cuda-12.9` 9.35 GiB, the CUDA installer 5.46 GiB and `~/dev/strata` 0.46 GiB (`results/rebuild-sizes-20261022.txt`).

## Before you start

- setup.py moves model files. Its `data_folder()` (`setup.py:4459-4510` at `477ac13cb`) takes as sources the data folder that `~/.config/strata/settings.json` names, the Strata folders remembered there, every folder whose name starts with `strata` (in any case) and holds a `setup.py`, in the tree's parent and grandparent directories or one level inside such a folder (`other_installs()`, `setup.py:4380-4398`), and a `Strata-data` folder inside any of these. From each one on the same file system it moves `models/`, `packs/*` and `mtp/` into the new data folder, and points that folder's configs there (`setup.py:4490-4502`). On a new VM there is nothing to move. Do not run step 5 where another Strata tree or data folder must stay where it is.
- In the rehearsal, setup.sh's scope reached a memory peak of 6.38 GiB (R6 in `results/rebuild-rehearsal-r6-r7-20261030.txt`; samples in `results/rebuild-rehearsal-memory-20261030.txt`). It came while setup read the MTP files through mmap, which charges their page cache to the scope; through the engine build the peak was 2.30 GiB. How much of it setup needs was not measured, and a run under the account's 6 GiB, which step 12 installs ([decision 0007](../decisions/0007-memory-limit-of-the-maintainers-account.md)), was not tried. So steps 5 and 6 come before step 12. Where the limit is already in force, lift it for them as [Daily operations](operations.md#the-memory-limit-of-the-maintainers-account) says (the rehearsal used 12G), and set it back after.
- The engine binaries cannot be rebuilt byte for byte. nvcc puts temporary file names made from process IDs into them. With those names and the GNU build ID masked, the rehearsal's `strata` and `libstrata_kq.so` equal the deployed ones (R7 in `results/rebuild-rehearsal-r6-r7-20261030.txt`), on this machine's CPU and compiler. ggml-cpu is built with `-march=native` (`results/rebuild-rehearsal-setup-20261030.log`, line 81), so on another CPU or compiler the code can differ too. Step 15's `check-drift.sh` reports the two files; commit their new sha256 only after the engine screen of step 15.

## Steps

### 0. The system

Ubuntu 22.04.5 with the kernel 6.8.0-138-generic and the root file system on ZFS; 4x Tesla P100 PCIe 16 GB; 8 CPUs (Xeon E5-2696 v4) and 65,846,904 kB of RAM; sudo (`results/host-os-20261025.txt`, `results/host-versions-20261024.txt`, `results/zfs-dataset-20261022.txt`, `results/setup-20261016.log`). `bench/build-engine.sh` builds on CPUs 1–7, and the preflight waits for 42 GiB of available memory before a start (`scripts/strata-preflight.sh`). Steps 1–7 download from GitHub, NVIDIA, Hugging Face and pip's package index.

- NVIDIA driver 535.230.02: Ubuntu's `nvidia-driver-535` 535.230.02-0ubuntu0.22.04.1, with the NVIDIA packages on hold. Do not install the 575 driver that comes with the CUDA runfile.
- gcc and g++ 11.4.0-1ubuntu1~22.04.3 (they built the engine), `git`, `curl` and `jq` (`check-deployment.sh` uses it).
- setup.sh installs `python3-venv` and `python3-pip` with sudo when they are missing (`results/apt-history-20261016.txt`). The venv of `config/strata-venv.txt` was made with python3.10 3.10.12-1~22.04.18, python3-pip-whl 22.0.2+dfsg-1ubuntu0.7 and python3-setuptools-whl 59.6.0-1.2ubuntu0.22.04.3; another revision of either can change pip's or setuptools' line in that file.

Not rehearsed. The versions are this machine's (`results/host-state-20261030.txt`).

### 1. The ops repo

```bash
git clone https://github.com/thc1006/strata-p100-deployment.git ~/dev/p100-q3-deployment
```
`strata-q3.service` calls the scripts in `scripts/` by this absolute path.

Rehearsed (R1, `results/rebuild-rehearsal-r1-r5-20261030.txt`): the clone at `7886a39` equals the live checkout file for file.

### 2. The CUDA 12.9.1 toolkit

Only the toolkit, in your home directory, as your user (not as root):
```bash
mkdir -p ~/downloads/cuda/tmp && cd ~/downloads/cuda
curl -fL -o cuda_12.9.1_575.57.08_linux.run https://developer.download.nvidia.com/compute/cuda/12.9.1/local_installers/cuda_12.9.1_575.57.08_linux.run
echo "a52d6c204bd4268627dfdab8bfeeb0d1  cuda_12.9.1_575.57.08_linux.run" | md5sum -c -
bash cuda_12.9.1_575.57.08_linux.run --silent --toolkit --toolkitpath="$HOME/cuda-12.9" --defaultroot="$HOME/cuda-12.9" \
  --tmpdir="$HOME/downloads/cuda/tmp" --no-man-page --no-opengl-libs --no-drm
~/cuda-12.9/bin/nvcc --version | tail -2
```
Expected: `Cuda compilation tools, release 12.9, V12.9.86` (`results/sm60_dp4a_check.txt`). `/tmp/cuda-installer.log` has WARNING lines. On 2026-10-16 there were 132 (`results/cuda-installer-20261016.log`): 126 about system directories it cannot write, and 6 about a file it skipped because it was already in `~/cuda-12.9`. None of them affect the toolkit. When `~/.local/share/applications/` exists, the installer also puts four desktop files there ([system changes](../reference/system-changes.md)); when it does not, it writes none and logs 8 more warnings (140 in the rehearsal: `results/cuda-installer-20261030.log`).

Rehearsed (R2, `results/rebuild-rehearsal-r1-r5-20261030.txt`): the toolkit equals production's file for file (5684 files and 112 links, the same sha256 and link targets), and the three libraries of `config/deployed-artifacts.sha256` are OK.

### 3. The Strata 0.1.45 tree

```bash
git clone --filter=blob:none https://github.com/Niko1221/Strata.git ~/dev/strata-git
git -C ~/dev/strata-git worktree add --detach ~/dev/strata 477ac13cbe8e582c20484871ce459d20d82df09d
git -C ~/dev/strata status --porcelain       # prints nothing
```
The expert profile, `data/expert-profile.bin`, is a file of this commit.

Rehearsed (R3, same file): all 3286 files of the commit have the same sha256 as in production's `~/dev/strata`, and `strata/data/expert-profile.bin` is OK against the manifest.

### 4. The IQ3_XXS shards

setup.sh is run for IQ3_XXS (step 5), IQ3_S's shard 2 is IQ3_XXS's (step 7), and `bench/switch_model.sh` falls back to IQ3_XXS. Download the model (about 71 GiB, 75.8 GB: `results/iq3s-model-files-20261019.txt`; pinned Hugging Face revision `ed59f92`), and check the hashes after the download:
```bash
D=$HOME/dev/Strata-data/models/IQ3_XXS; mkdir -p "$D" && cd "$D"
BASE=https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/ed59f92082b1e93c0e96d60a8b11aab089b52f09/IQ3_XXS
for i in 1 2; do f=Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-0000$i-of-00002.gguf; curl -fL --retry 20 --retry-delay 5 --retry-all-errors -C - -o "$f" "$BASE/$f"; done
printf '%s  %s\n' 219ea929900dfa9ef091f3aa473fdba6874b65fcb36526d7d851ac9e95856d15 Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
  316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113 Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf | sha256sum -c -
```
Expected: both lines show `OK`. If one shows `FAILED`, rerun the `for` line first (`-C -` resumes the download). If a fully downloaded file still shows `FAILED`, delete it and rerun. The hashes are the ones `config/deployed-artifacts.sha256` listed while the service ran IQ3_XXS (commit `7d542b3`).

Not rehearsed: the rehearsal linked production's two files instead (R5). The download ran on 2026-10-16 (setup found both files: `results/setup-20261016.log`, lines 2031–2032); the hash check dates from 2026-10-27 (`CHANGELOG.md`).

### 5. setup.sh

It makes the venv, downloads llama.cpp `3cf0325` into `third_party/`, builds an engine, makes the IQ3_XXS pack (`experts.bin` is about 40 GiB), downloads the multi-token prediction (MTP) tensors (5.2 GB) and makes the MTP head, and writes the config:
```bash
cd ~/dev/strata && export STRATA_NVCC=$HOME/cuda-12.9/bin/nvcc PATH=$HOME/cuda-12.9/bin:$PATH
./setup.sh --setup --no-start --yes --build --family qwen --model IQ3_XXS --cuda 12 --gpus 0,1,2,3 \
  --layer-split 12,24,36 --context 32768 --kv int8 --parallel 4 --vision no --draft-vocab cjk \
  --low-ram resident --kv-streaming off --vram-reserve-mib 1536 --data-dir $HOME/dev/Strata-data --no-browser
cmp ~/dev/strata/strata-iq3_xxs.json ~/dev/p100-q3-deployment/config/strata-iq3_xxs.setup.json && echo "setup's config as on 2026-10-16"
```
Rehearsed (R6, `results/rebuild-rehearsal-r6-r7-20261030.txt`; output `results/rebuild-rehearsal-setup-20261030.log`): 772 s, of which 6 min 39 s for the engine. The config equals `config/strata-iq3_xxs.setup.json`, which 0.1.43.1's setup wrote on 2026-10-16. The venv's packages, `pyvenv.cfg`, `.strata-pip.json`, `third_party/llama.cpp` and the MTP head equal production's. Setup's downloads were seeded, not run: R4 had downloaded the llama.cpp zip and written setup's `.done` mark for it, and R5 had copied production's MTP tensors, linked production's IQ3_XXS shards and pack, and written `.done` marks for the shards, so setup skipped them without asking the server (`setup.py:1616-1618`) and made no IQ3_XXS pack. On 2026-10-16 setup downloaded and made all of these (`results/setup-20261016.log`).

### 6. The deployed build of the engine

setup's engine is not the deployed build: that one came from `bench/build-engine.sh` in a tree at `~/dev/strata-0.1.45`, whose path is in the binary, and its BUILD.json names the commit. Build it in a second worktree and put it in place of setup's:
```bash
git -C ~/dev/strata-git worktree add --detach ~/dev/strata-0.1.45 477ac13cbe8e582c20484871ce459d20d82df09d
cp -a ~/dev/strata/third_party/llama.cpp ~/dev/strata-0.1.45/third_party/
~/dev/p100-q3-deployment/bench/build-engine.sh ~/dev/strata-0.1.45 0.1.45
rm -rf ~/dev/strata/engine-cuda12 ~/dev/strata/build-cuda12 && cp -a ~/dev/strata-0.1.45/engine-cuda12 ~/dev/strata/
```
`build-engine.sh` uses the venv of `~/dev/strata`, so it comes after step 5. Do not run setup.sh again after this step: it compiles again when BUILD.json's `src` is not its own source hash (`setup.py:4047-4050`).

Rehearsed (R7, same file): 305 s, a memory peak of 3.04 GiB. BUILD.json is OK against the manifest. With nvcc's temporary names and the build ID masked, `strata` and `libstrata_kq.so` are byte for byte the deployed ones (`bench/rehearsal_elfcmp.py`); unmasked they differ in 298 and 30 bytes. The rehearsal ran the `mv`, `rm` and `cp` steps of R7 and R8 outside the sandbox, on the sandbox's paths.

### 7. IQ3_S's shards

```bash
M=~/dev/Strata-data/models; mkdir -p $M/IQ3_S
f=Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf
nice -n 19 ionice -c 3 curl -L --fail -C - --limit-rate 60M -o $M/IQ3_S/$f.part \
  https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/ed59f92082b1e93c0e96d60a8b11aab089b52f09/IQ3_S/$f
sha256sum $M/IQ3_S/$f.part    # must be 4c1eb2ceb4915e1192f4f386021897bde56a97f40a0bb78bb86465e0f7d2aca3
mv $M/IQ3_S/$f.part $M/IQ3_S/$f
ln $M/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf $M/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf
```
Expected: shard 1 has 54,817,524,224 bytes (`results/iq3s-model-files-20261019.txt`). The hard link takes no space.

Rehearsed (R8, `results/rebuild-rehearsal-r8-r11-20261030.txt`): the sha256 is the pinned one; 1728 s with `--limit-rate 30M`. The hard link is of R5's symbolic link, so it leads to production's shard 2.

### 8. The IQ3_S pack

In the shell of step 7 (it uses `$M` and `$f`):
```bash
cd ~/dev/strata && PYTHONDONTWRITEBYTECODE=1 STRATA_GGUF_PY=third_party/llama.cpp/gguf-py nice -n 19 ionice -c 3 \
  .venv/bin/python tools/iq_pack.py --gguf $M/IQ3_S/$f --out ~/dev/Strata-data/packs/iq3_s --experts-bin
```
Expected: the last line is `experts.bin: 48 layers, 46.84 GiB`.

Rehearsed (R9, same file): 1209 s, at most 2.67 GiB of anonymous memory in a 4 GiB scope. All 11 files of the pack are OK against `config/deployed-artifacts.sha256`.

### 9. The live config

```bash
cd ~/dev/p100-q3-deployment
python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf)
cmp ~/dev/strata/strata-iq3_xxs.json config/strata-iq3_xxs.deployed.json && echo "config matches"
python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf) --dry-run
```
Expected: `config matches`, and a dry run with no change and no critical change (the preflight's verdict). If the `cmp` of step 5 failed, start from setup's config of 2026-10-16 instead, then run this step's commands again:
```bash
cp config/strata-iq3_xxs.setup.json ~/dev/strata/strata-iq3_xxs.json
```

Rehearsed (R10, same file): the result equals `config/strata-iq3_xxs.deployed.json` byte for byte.

### 10. The venv

```bash
for d in ~/dev/strata/.venv/lib/python3*/site-packages/*.dist-info; do
  echo "$(sed -n 's/^Name: //p' "$d/METADATA" | head -1)==$(sed -n 's/^Version: //p' "$d/METADATA" | head -1)  $(sha256sum < "$d/RECORD" | cut -d' ' -f1)"
done | LC_ALL=C sort | diff <(grep -v '^#' ~/dev/p100-q3-deployment/config/strata-venv.txt) -
```
Expected: no output. Each RECORD holds the sha256 of the venv's scripts, whose first line names the venv's path, so the venv must be made at `~/dev/strata/.venv`.

Rehearsed (R6, `results/rebuild-rehearsal-r6-r7-20261030.txt`): all 17 packages as `config/strata-venv.txt`.

### 11. The model files, the pack and the MTP head

```bash
cd ~/dev && grep -E '  (strata/|Strata-data/)' p100-q3-deployment/config/deployed-artifacts.sha256 | sha256sum -c -
```
Expected: `OK` for every line but `strata/engine-cuda12/strata` and `strata/engine-cuda12/libstrata_kq.so` (see [Before you start](#before-you-start)).

Rehearsed (R6 in `results/rebuild-rehearsal-r6-r7-20261030.txt`, R9 and R11 in `results/rebuild-rehearsal-r8-r11-20261030.txt`): every one of these lines was `OK` in the sandbox but those two.

### 12. The system files

Steps 12–15 run in the ops repo:
```bash
cd ~/dev/p100-q3-deployment
scripts/install-system-files.sh --dry-run
scripts/install-system-files.sh --enable
```
It installs the units, the drop-in `user-1003.slice.d/50-memory.conf` (the account's 6 GiB), the logrotate rule and the watchdog, enables and starts `zfs-arc-max.service` (the ARC cap of 2 GiB) and enables `strata-q3` and its timer (`scripts/install-system-files.sh:25-37`).

Not rehearsed (sudo, `/etc`). The script, without `--enable`, installed `strata-q3.service` and `zfs-arc-max.service` on 2026-10-19 (`results/deploy-0.1.45-20261019.txt`); no record shows a run with `--enable`. Production's `check-drift.sh` of 2026-10-30 found the installed files as the repository's (`results/rebuild-rehearsal-r8-r11-20261030.txt`, after R11).

### 13. The host units

```bash
scripts/host-units.sh apply
```
It sets the units and then checks them. Not rehearsed. The states before the change are in `results/host-units-before-20261018.txt`; `check-drift.sh` checks them (the same run as in step 12).

### 14. Start

```bash
sudo systemctl start strata-q3.service strata-q3-health.timer
```
A restart is ready after about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`). The first start reads the new pack from disk and can take longer: IQ3_S's first start on 2026-10-20 was still loading at 900 s (`results/ab-iq3s-1-switch.txt`); `strata-wait-ready.sh` allows 1800 s (`systemd/strata-q3.service`).

Not rehearsed (GPUs). The start of 0.1.45 on 2026-10-19 is in `results/deploy-0.1.45-20261019.txt`, and a start with this repository's preflight and wait-ready of `7886a39`, on 2026-10-29, in `results/restart-after-confirm-20261029.txt`.

### 15. Verify

```bash
nice -n 19 ionice -c3 scripts/check-drift.sh --full      # 11 min 24 s on 2026-10-27 (results/check-drift-full-20261027.txt)
scripts/check-deployment.sh
```
Expected: drift on the two engine binaries only, and `no problems found` from `check-deployment.sh`.

Then screen the rebuilt engine on the GPUs. The arm R1 of `bench/arms-rebuild.json` is the deployed configuration (K1 of `bench/arms-iq3s.json`) run from the rebuilt engine in `~/dev/strata-0.1.45`; the baseline is K1's four runs of 2026-10-29 with the deployed binary (`cf-k1-1` to `cf-k1-4`, [Engine experiments](../engine-experiments.md)). `bench/ab_engine.sh` stops the service for each run and starts it again at the end; the eight runs of 2026-10-29 took 15:51–19:20 (`results/ab_engine-confirm-20261029.log`).
```bash
bench/make_engine_configs.py ~/dev/strata-0.1.45 cfg-s-k1
ENGINE_SCREEN_PHASES=prefill,decode,four,decode4,eval4,admit,admit28,turns bench/ab_engine.sh bench/arms-rebuild.json rb-1:R1 rb-2:R1
python3 -B bench/ab_engine_summary.py --arms bench/arms-rebuild.json --baseline K1 cf-k1-1 cf-k1-2 cf-k1-3 cf-k1-4 rb-1 rb-2
```
`make_engine_configs.py` prints how `cfg-s-k1.json` differs from the live config: in `exe`, `cwd` and `log` only. The check, from what the baseline shows: K1's four runs gave the same answers to the 12 requests that run alone (9 in the prefill phase, 3 in the decode phase), and each run's median rates lay within 200.0–200.6, 378.1–379.4 and 679.5–681.5 tokens/s for reading 1.2K, 4K and 16K prompts and 43.8–44.6 tokens/s for one user's decode (`results/engine-screen.jsonl`). Pass: R1 gives those 12 answers (the summary's Answers part shows no difference in the prefill or decode phase), and its rates lie in those ranges. These are the screens' own measures; no rebuilt engine has been screened, and on another CPU (see [Before you start](#before-you-start)) a difference is not by itself a fault: the maintainer decides then. After a pass, commit the new sha256 of the two binaries in `config/deployed-artifacts.sha256` and run `check-drift.sh` again: `no drift`.

Rehearsed in part (R11, `results/rebuild-rehearsal-r8-r11-20261030.txt`): `check-drift.sh --full` in the sandbox found the config, the tree, the venv, the checkout and every file of the manifest as production has them, but the two engine binaries. Its other DRIFT lines came from the sandbox (root's files show as uid 65534, and it has no user bus and its own PID namespace); the same check outside, on production, reported no drift. `check-deployment.sh` and the engine screen need the running service and were not rehearsed; `check-deployment.sh`'s output after the start of 2026-10-19 is in `results/deploy-0.1.45-20261019.txt`.

## The rehearsal of 2026-10-30

- Each step ran as a job of a sandbox runner: in a capped user scope at idle priority, inside a bubblewrap namespace in which the root file system is read-only, production's `~/dev` and `~/cuda-12.9` are read-only views, and `~/dev`, `~/cuda-12.9`, `~/downloads` and `/tmp` are directories of `~/rebuild-rehearsal`. The files it made carry production's paths, so they compare byte for byte with production's. There is no GPU device inside, and `sudo` and `nvidia-smi` are stand-ins.
- The runner, guard and self-test that ran were copies in `~/rebuild-rehearsal/bin`, named in the records. Their successors are `bench/rehearsal_*`, whose headers say how they differ. The runner that ran showed the whole home directory read-only to every job but the four of R2a and R2b with `--home`, credentials such as gh's GitHub token too, and gave every job the host's network; R6's `setup.sh` ran pip that way. The records do not show whether a job read them.
- The self-test showed that writes into production fail inside (`results/rebuild-rehearsal-r0-20261030.txt`).
- The guard was ready to freeze the jobs while the service was not active, under 12 GiB was available or `/` was more than 80 % full; it froze none of the steps' jobs (`results/rebuild-rehearsal-guard-20261030.log`).
- The account's limit was lifted to 12G for steps 5 and 6 only.
- A second run of the self-test after R11 removed the rehearsal's tree and data folder from the sandbox, not from production (`results/rebuild-rehearsal-selftest-rerun-20261030.txt`). A run at 04:14 of the self-test with its first refusals is in `results/rebuild-rehearsal-selftest-committed-20261030.txt`.
- The tools are in `bench/`, so run them only when the maintainer asks.

## Rollback

Remove each item as described in the "How to undo" column of [docs/reference/system-changes.md](../reference/system-changes.md).
