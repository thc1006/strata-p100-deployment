---
status: accepted
date: 2026-10-18
decision-makers: thc1006
---

# Organize top-level directories by purpose and documents by Diátaxis

## Context and Problem Statement

When the repository was created on 2026-10-17, it kept the layout of the original working directory:

- The root directory mixed tools, tests, documents, the original setup guide and example requests.
- `deploy/` held deployment files, load-test tools, test results and upstream records together.
- A single deployment record served as operations manual, list of system changes, design rationale and measurement results.

The decision was how to arrange directories and documents so that operators can find things and change them safely. The constraint: `strata-q3.service` calls the ExecStartPre and ExecStartPost scripts in the repository by absolute path, so a move requires reinstalling the units at the same time and verifying with one restart.

## Considered Options

1. Keep the current layout
2. Organize top-level directories by purpose and documents by Diátaxis
3. Apply golang-standards/project-layout in full

## Decision Outcome

Chosen option: 2.

- Top-level directories: `config/` (desired state), `systemd/`, `scripts/` (run on the host), `tools/`, `tests/`, `bench/`, `results/`, `examples/`, `docs/`.
  - `tools/`, `docs/` and `examples/` have the same purpose as in golang-standards/project-layout. Its `scripts/` is for build, install and analysis scripts; here it also holds the scripts that the units call at startup.
  - It puts systemd units in `init/` and configuration templates in `configs/`, and its `test/` is for external test programs; this repository uses the plainer `systemd/`, `config/` and `tests/`. `bench/` and `results/` have no matching directory there.
- Tests: in `tests/`, one of the layouts that the pytest documentation lists. They run with unittest from the standard library.
- Documents: split by Diátaxis into `docs/runbooks/` (how-to: only steps, expected results, verification and rollback), `docs/reference/` and the deployment record (explanation and measurements).
- Decision records: in MADR 4.0.0 format, in `docs/decisions/`.
- Changelog: follows Keep a Changelog 1.1.0. This repository has no version numbers, so sections are by date.

### Consequences

- Good:
  - The root directory holds only `README.md`, `CHANGELOG.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `AGENTS.md`, `.gitignore` and `.gitattributes`.
  - Each kind of file has a fixed place, and runbooks no longer mix in design rationale.
- Bad:
  - Historical output in `results/` still shows the old paths (`deploy/...`); `results/README.md` explains this.
  - The move required reinstalling the systemd units and restarting the service once (it loaded after 438 s: `results/service-load-times.txt`).
  - `scripts/check-drift.sh` changed so much during the move that by default git does not treat it as a move. The public repository starts from a single commit; the history before the move is not part of it.

### Confirmation

- `scripts/test.sh` checks that the file each Exec line in the systemd units points to exists and is executable. `systemd-analyze verify` checks only the first line of each Exec type and skips lines that start with `-` (tested with systemd 249 on this machine), so this check was added.
- On 2026-10-18, the units were reinstalled with `scripts/install-system-files.sh` and the service was restarted once:
  - preflight reported `ok`: all four GPUs, enough memory, and a config that matches `--profile deployed` in everything that decides memory and placement.
  - wait-ready reported `loaded after 438 s` (`results/service-load-times.txt`, `results/journal-20261018-0023.txt`, which also holds the preflight's line).

## Pros and Cons of the Options

Option 1, keep the current layout:
- Good: the paths of the production service do not change.
- Bad: files pile up in the order they were created, and the purpose of `deploy/` is unclear; operating steps and design rationale are mixed in one document.

Option 3, the full golang-standards/project-layout:
- Bad: most of its directories, such as `cmd/`, `internal/`, `pkg/`, `api/` and `web/`, would have no content here; that document also says that small projects do not need the full structure.

## More Information

- [golang-standards/project-layout](https://github.com/golang-standards/project-layout) (the purpose of each directory; its README states that it is not an official standard)
- [pytest Good Integration Practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
- [Diátaxis](https://diataxis.fr/), [How-to guides](https://diataxis.fr/how-to-guides/)
- [MADR 4.0.0](https://adr.github.io/madr/)
- [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- [ngc-shj/inference-deploy](https://github.com/ngc-shj/inference-deploy): a repository for a self-hosted inference engine with systemd, which keeps the units and the install scripts together
