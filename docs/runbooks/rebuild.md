# Rebuilding the Strata deployment from scratch

- When to use: the whole VM is rebuilt, or `~/dev/strata` or `~/dev/Strata-data` is lost. The steps have not been brought up to date for IQ3_S (see the last two points below): if only `~/dev/Strata-data` is lost, today's deployment needs IQ3_S's files ([Change the model](change-config.md#change-the-model-gsq-rco-iq3_xxs-or-iq3_s), steps 1 and 2), shard 2 of IQ3_XXS (step 4 below) and the MTP head, which setup.sh downloads (step 5).
- Prerequisites:
  - NVIDIA driver 535.230.02 is installed (do not install the 575 driver that comes with the CUDA runfile). The root file system is on ZFS. You have sudo. `jq` is installed (`check-deployment.sh` uses it).
  - The account is `thc1006`, uid 1003. The units and logrotate file in `systemd/`, the preflight, watchdog and `check-deployment.sh` in `scripts/`, and the two config snapshots in `config/` all hard-code `/home/thc1006`; the drop-in directory `systemd/user-1003.slice.d/` and `check-deployment.sh` hard-code the uid. To use another account, change these files first; otherwise the comparison in step 5 will not match either.
  - At least 233.1 GiB (250.3 GB) of space for today's deployment, by apparent size (ZFS's compression makes the disk use smaller): `~/dev/Strata-data` with both models 217.8 GiB (`results/strata-data-du-20261021.txt`), `~/cuda-12.9` 9.35 GiB, the CUDA installer 5.46 GiB and `~/dev/strata` 0.46 GiB (`results/rebuild-sizes-20261022.txt`).
- The steps rebuild the deployment of 2026-10-16: Strata 0.1.43.1 (`a0116b2`) with GSQ-RCO IQ3_XXS. The service runs 0.1.45 since 2026-10-19 ([decision 0005](../decisions/0005-strata-0.1.45-group-sort-concurrent-read.md)) and IQ3_S since 2026-10-20 ([decision 0004](../decisions/0004-iq3-s.md)): after the last step, switch to 0.1.45 with step 6 of [Evaluating and upgrading Strata](upgrade-strata.md), then to IQ3_S with [Change the model](change-config.md#change-the-model-gsq-rco-iq3_xxs-or-iq3_s).
- This repository's `config/` describes IQ3_S on 0.1.45, not what the steps build: step 5's comparison matches the `config/` of that time (commit `b9ec0ff`), and `scripts/check-drift.sh` in step 7 reports the differences until both switches are done.
- Last verified: the downloads, installs and setup.sh in steps 2–5 are the commands that were run during the deployment on 2026-10-16. The repo and scripts used in steps 1, 6 and 7 were created only on 10-17 and 10-18, and have not been used in a rebuild. The whole procedure has never been rerun from scratch. Step 5 is verified: recomputing from the config that setup.sh produced at the time gave a result byte-for-byte identical to the live config.

## Steps

1. Get this repo and put it in its fixed location (`strata-q3.service` calls the scripts in `scripts/` by absolute path):
   ```bash
   git clone https://github.com/thc1006/strata-p100-deployment.git ~/dev/p100-q3-deployment
   ```
2. Get the Strata source code (a zip of a pinned version, not a git checkout; upstream is currently the only source):
   ```bash
   cd ~/dev && test ! -e strata && curl -fL -o strata-a0116b2.zip https://github.com/Niko1221/Strata/archive/a0116b2782a5b82622358c04a06496485d12b2d6.zip
   unzip -z strata-a0116b2.zip | grep -qx a0116b2782a5b82622358c04a06496485d12b2d6 && echo "zip is commit a0116b2"
   unzip -q strata-a0116b2.zip && mv Strata-a0116b2782a5b82622358c04a06496485d12b2d6 strata
   echo a0116b2782a5b82622358c04a06496485d12b2d6 > strata/PINNED_COMMIT.txt
   ```
   Expected: `zip is commit a0116b2`. GitHub writes the commit into the zip comment, so the commands check the comment; GitHub does not guarantee that the bytes of the zip itself are the same every time.
   `test ! -e strata` prevents the new directory from being moved into the old one when the old directory still exists.
3. Install the CUDA 12.9.1 toolkit, only the toolkit, in your home directory:
   ```bash
   mkdir -p ~/downloads/cuda/tmp && cd ~/downloads/cuda
   curl -fL -o cuda_12.9.1_575.57.08_linux.run https://developer.download.nvidia.com/compute/cuda/12.9.1/local_installers/cuda_12.9.1_575.57.08_linux.run
   echo "a52d6c204bd4268627dfdab8bfeeb0d1  cuda_12.9.1_575.57.08_linux.run" | md5sum -c -
   bash cuda_12.9.1_575.57.08_linux.run --silent --toolkit --toolkitpath="$HOME/cuda-12.9" --defaultroot="$HOME/cuda-12.9" \
     --tmpdir="$HOME/downloads/cuda/tmp" --no-man-page --no-opengl-libs --no-drm
   ~/cuda-12.9/bin/nvcc --version | tail -2
   ```
   Expected: `Cuda compilation tools, release 12.9, V12.9.86` (as in `results/setup-20261016.log`). When you install as a regular user, `/tmp/cuda-installer.log` contains WARNING lines. On 2026-10-16 there were 132 (`results/cuda-installer-20261016.log`): 126 were about being unable to write to system directories such as `/var/log` and `/usr/share/applications`, and 6 were about a skipped copy because `~/cuda-12.9` already had a file with the same name. None of them affect the toolkit.
4. Download the model (about 71 GiB, 75.8 GB: `results/iq3s-model-files-20261019.txt`; pinned Hugging Face revision `ed59f92`), and check the hashes after the download:
   ```bash
   D=$HOME/dev/Strata-data/models/IQ3_XXS; mkdir -p "$D" && cd "$D"
   BASE=https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/ed59f92082b1e93c0e96d60a8b11aab089b52f09/IQ3_XXS
   for i in 1 2; do f=Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-0000$i-of-00002.gguf; curl -fL --retry 20 --retry-delay 5 --retry-all-errors -C - -o "$f" "$BASE/$f"; done
   printf '%s  %s\n' 219ea929900dfa9ef091f3aa473fdba6874b65fcb36526d7d851ac9e95856d15 Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
     316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113 Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf | sha256sum -c -
   ```
   The hashes are the ones `config/deployed-artifacts.sha256` listed while the service ran IQ3_XXS (commit `7d542b3`); it lists IQ3_S's shards now. Shard 2, the PLE table, is the same file in IQ3_S.
   Expected: both lines show `OK`. If `FAILED` appears, first rerun the `for` line (`-C -` resumes the download where it stopped). If a file is already fully downloaded and still shows `FAILED`, delete that file and rerun.
5. Run setup.sh. It builds the engine, generates the pack from the model (`experts.bin` is about 40 GiB) and downloads the multi-token prediction (MTP) tensors (about 5 GB). Then apply the deployed configuration:
   ```bash
   cd ~/dev/strata && export STRATA_NVCC=$HOME/cuda-12.9/bin/nvcc PATH=$HOME/cuda-12.9/bin:$PATH
   ./setup.sh --setup --no-start --yes --build --family qwen --model IQ3_XXS --cuda 12 --gpus 0,1,2,3 \
     --layer-split 12,24,36 --context 32768 --kv int8 --parallel 4 --vision no --draft-vocab cjk \
     --low-ram resident --kv-streaming off --vram-reserve-mib 1536 --data-dir $HOME/dev/Strata-data --no-browser
   cd ~/dev/p100-q3-deployment
   python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf)
   cmp ~/dev/strata/strata-iq3_xxs.json config/strata-iq3_xxs.deployed.json && echo "config matches"
   ```
   Expected: `config matches`. If the files differ, find the differences with `diff`, and do not start the service yet. The full output of setup.sh from 2026-10-16 is in `results/setup-20261016.log`.
6. Install the system files and start the service:
   ```bash
   scripts/install-system-files.sh --enable
   sudo systemctl start strata-q3.service strata-q3-health.timer
   ```
7. Verify:
   ```bash
   scripts/check-drift.sh --full      # also hashes the files over 1 GiB, the model shards and the pack's weights; took about 11 minutes on 2026-10-27 (results/check-drift-full-20261027.txt)
   scripts/check-deployment.sh
   ```
   Expected: the model shards and the config must match. The rebuilt engine and the regenerated pack and MTP files can have hashes that differ from the manifest, and the venv's packages can differ from `config/strata-venv.txt`: setup.sh's pip may install other versions, and it writes each package's `RECORD`, whose sha256 the list pins. Whether they are reproducible has not been verified. After the service has finished loading, `/health` returns `"loaded": true`.

## Rollback

Remove each item as described in the "How to undo" column of [docs/reference/system-changes.md](../reference/system-changes.md).
