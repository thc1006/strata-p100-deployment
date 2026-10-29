# Security policy

## Scope

This policy covers the scripts, systemd units and configuration tool in this repository. The watchdog
(`scripts/strata-health.sh`) runs as root through a systemd timer, so problems in it are in scope.

Vulnerabilities in the Strata engine or its server belong to the upstream project. Report them through
[Strata's security policy](https://github.com/Niko1221/Strata/security/policy).

## Supported version

Only the default branch is supported. The repository has no releases.

## Report a vulnerability

Report it through GitHub's private vulnerability reporting: open the repository's Security tab and select
"Report a vulnerability". Do not open a public issue for it. Include what you found, how to reproduce it and what an
attacker gains.

This repository has one maintainer and no fixed response time.

## Notes on the deployment

The service listens only on `127.0.0.1:8080` and has no API key set, as described in the
[deployment record](docs/deployment-record.md#5-known-limits-to-share-with-lab-members). If you expose the service
beyond the local machine, set an API key first.
