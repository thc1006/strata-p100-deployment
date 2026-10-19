---
status: accepted
date: 2026-10-19
decision-makers: thc1006
---

# Run Strata 0.1.45 with its grouped prompt sort and concurrent prompt reads

## Context and Problem Statement

The service ran Strata 0.1.43.1 (`a0116b2`). While a long prompt was read, the other users' replies stopped: a
16K-token prompt joining three decoding users stopped them for 23 s (the deployment record, section 3.2).

Strata 0.1.45 (`477ac13cb`, released 2026-10-17) has two opt-in switches for reading prompts. The engine experiments
measured them on this machine, with the deployed configuration, against 0.1.45 without them
(`docs/engine-experiments.md`):

- `STRATA_MMQ_GROUP_SORT=1` takes the experts of each full 16-expert group of the prompt's MMQ in row-count order, which
  is exact by design. 1.2K to 16K-token prompts were read 9-18% faster, with the same answers (screen 2,
  `results/SCREEN-sched.md`).
- `STRATA_PIPED_CONCURRENT=1` reads a prompt on a thread of its own while the slot groups decode. While a 16K prompt was
  read, the three decoders' longest stall fell from 22.69 s to 0.49 s. The joining prompt's first token came 11.0%
  later, four users sent together got 5.2% fewer tokens per second, and 11 of 30 answers of requests decoded beside a
  read changed (screen 2).

The maintainer adopted both on 2026-10-19 at 08:00, to be confirmed together first. In the order K0, K1, K1, K0, by the
criteria fixed before the runs, the pair was confirmed (`results/SCREEN-combo.md`):

- every prompt size was read faster in each K1 run than in each K0 run: 1.2K +17.4%, 4K +10.7%, 16K +9.1%;
- while a 16K prompt joined three decoders, their longest stop was 0.55 s instead of 22.73 s, and the four requests
  finished 5.6% sooner; with a 28K prompt, 0.59 s instead of 35.81 s;
- the joining prompt's first token came 1.1% later (22.78 s), and the 16 requests that ran alone got the same answers.

0.1.45's `serve/server.py` also changes what users get. A request that sends no sampling settings is sampled with the
model card's settings (temperature 1.0, top_p 0.95 and top_k 20 with thinking; temperature 0.7, top_p 0.8, top_k 20
and presence_penalty 1.5 without), where 0.1.43.1 decoded greedily. An args element that holds a flag and its value is
split before the engine starts; `tools/configure_strata_q3.py` refuses one.

## Considered Options

1. Stay on 0.1.43.1.
2. 0.1.45 with both switches.
3. 0.1.45 with `STRATA_MMQ_GROUP_SORT=1` only.

## Decision Outcome

Chosen option: "0.1.45 with both switches", because the pair met its criteria, and a long prompt no longer stops the
other users' replies, at a cost the maintainer accepted when adopting the concurrent read: answers of requests decoded
beside a read are not those decoded without it.

### Consequences

- Good, because prompts are read 9-17% faster, and while a long prompt is read the other users keep receiving tokens.
- Bad, because a request decoded while another prompt is read can get another answer than it would alone, and another
  answer each time (the two K1 runs differed in 19 such answers). Upstream names one cause: the experts the read borrows
  run on the CPU for the slots' windows, which rounds differently (`docs/BATCHING.md` of 0.1.45, "Exactness"). It was not
  tested here.
- Bad, because a request without sampling settings is no longer decoded greedily; a client that needs greedy answers
  sends temperature 0.
- The same restart deploys decision 0004's host changes, the first step of its protocol: the ZFS ARC cap of 2 GiB and
  the 6 GiB RAM floors.

### Confirmation

- `scripts/check-drift.sh`: the engine (sha256 `00869ac7…`), `libstrata_kq.so` and the live config with the three
  environment settings match `config/`.
- The engine's start-up lines: "prompt MMQ sorts the full expert groups (STRATA_MMQ_GROUP_SORT=1): 48 of 48 layers",
  and no "STRATA_PIPED_CONCURRENT is off beside" line.
- `scripts/check-deployment.sh`.

## More Information

- Not adopted: `--prefill-pipe 768` (screen 2), and pull request #2985's MTP drafts in the pipelined groups (screen 3).
  The maintainer chose to test #2985 again once it is merged, with decode4 and eval4 as the declared target.
- The 0.1.43.1 tree stays at `~/dev/strata-0.1.43.1`; the rollback is in `docs/runbooks/upgrade-strata.md`.
