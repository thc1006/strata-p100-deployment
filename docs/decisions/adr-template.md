---
status: proposed   # proposed | accepted | rejected | deprecated | superseded by NNNN
date: YYYY-MM-DD
decision-makers: thc1006
---

<!-- Adapted from MADR 4.0.0 (https://adr.github.io/madr/). File name: NNNN-title-with-dashes.md, in lowercase English.
     Do not rewrite an accepted record. When a decision changes, write a new record and change the status of the old one to superseded by NNNN.
     Delete an optional section that has no content. -->

# {Title: the decision, stated in one sentence}

## Context and Problem Statement

{What happened and what has to be decided. Write only facts and measurements, and give the source of each measurement.}

## Considered Options

1. {title of option 1}
2. {title of option 2}

## Decision Outcome

Chosen option: "{title of the chosen option}", because {justification}.

### Consequences

- Good, because {…}
- Bad, because {…}

### Confirmation

{Which check shows that the decision is being followed, for example `scripts/check-drift.sh`.}

## Pros and Cons of the Options

{Optional.}

## More Information

{Optional: references, and when to re-evaluate the decision.}
