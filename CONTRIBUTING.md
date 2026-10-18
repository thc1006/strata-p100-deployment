# Contributing

This repository describes one production machine: Strata serving Qwen3.8-Flash-Next IQ3_XXS on 4× Tesla P100. The
configuration and the measurements are specific to that hardware.

## Issues

Open an issue for a mistake in the documentation, a procedure that does not work as written, or a question about a
measurement. For a security problem, follow the [security policy](SECURITY.md) instead.

## Pull requests

- Run `scripts/test.sh --repo-only` before you open a pull request, on Linux with Python 3.10, git and
  `systemd-analyze`. It must end with `all checks passed`. The `test` workflow runs the same command on every pull
  request. Without `--repo-only`, the script also checks programs that exist only on the production machine.
- A change to `tools/configure_strata_q3.py` needs a unit test in `tests/`.
- A change to the deployed configuration needs measurements from this hardware. Put the raw output in `results/` and
  explain the change in [docs/deployment-record.md](docs/deployment-record.md).
- Do not edit existing files in `results/`. They are raw measurements.
- Write documentation in plain English, and add an entry to [CHANGELOG.md](CHANGELOG.md) for a notable change.

## License

This repository is licensed under Apache-2.0. Under section 5 of the [license](LICENSE), a contribution you submit is
under the same terms.
