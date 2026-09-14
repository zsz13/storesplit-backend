# Agent engineering on StoreSplit

The long version of the README's summary: what "agent-built" means here, what I did, what the
review loops were, and what this project does and does not claim to have measured.


StoreSplit is a personal, hobby-scale engineering project built in my free time, and it is
also a real test environment for my local AI coding-agent setup.

**StoreSplit is the application.** The reusable agent configuration — hooks, skills, review
agents, rules, and the benchmark methodology behind them — lives in a separate public
repository, [zsz13/claude-code-config](https://github.com/zsz13/claude-code-config), and is
not duplicated here.

What this codebase was useful for testing is the part that is hard to learn from a toy
project: whether agent workflows hold up on large multi-file changes, async scraping against
uncooperative third-party sites, database migrations and data integrity, debugging a defect
whose reported cause is wrong, review quality, dead-code detection, code simplification, and
validation discipline.

## How this was built

StoreSplit was implemented through coding agents working under my direction, as a deliberate
experiment in agent-based software engineering. I did not hand-type the implementation, and
this repository does not pretend otherwise.

What that means in practice: I identified the problem, defined the product and its
requirements, chose the architecture and the invariants the system has to hold, decomposed the
work into tasks an agent could execute, designed the prompts and constraints for those tasks,
reviewed what came back, rejected what was wrong, drove debugging and root-cause investigation
through the agents, and defined the validation every change had to survive before it landed.

**Agent-built does not mean unreviewed or blindly accepted.** Changes went through structured
review loops — adversarial review by independent reviewer agents, a dead-code audit, and a
complexity/simplification review — and through deterministic gates that do not care what
produced the diff: the test suite, Ruff, Pyright, and Alembic migration checks. Agent output
that failed a gate, contradicted an invariant, or proposed an abstraction the architecture did
not need was rejected rather than merged. Several design decisions recorded below exist
*because* a proposed implementation was wrong and the investigation found the real cause.

## My role

- Identifying the product opportunity and defining requirements
- Product decisions and scope
- System architecture and module boundaries
- Decomposing work into agent-executable tasks
- Designing prompts, constraints, and invariants
- Reviewing agent output, and rejecting it where warranted
- Debugging and root-cause investigation through agents
- Defining validation requirements and building the review loops
- Database and data-integrity safeguards
- Adversarial review, dead-code review, and simplification review
- Test strategy
- Benchmarking the agent workflows themselves
- Evaluating failures and rejected agent suggestions
- Iteration and orchestration across backend and frontend

## On benchmark claims

This project was used to *exercise* those workflows. That is a different statement from having
measured them, and the difference matters.

Where this README gives a number, it is a measurement taken on this system and is labelled as
one — concurrency throughput (`4 -> 19.8s, 6 -> 15.2s, 8 -> 12.9s, 12 -> 10.1s` over a whole
94105 fetch), the Target challenge rate over ten live runs, the 13-of-86 Whole Foods
availability flap, the 8-of-1260 Lucky `lowStock` reclassification. Those are measurements
about StoreSplit's behaviour against real retailer data.

No claim is made here that agents made development faster, produced better code than a human
would have, or improved quality by some percentage. I have not run the controlled comparison
that would support any of those claims, so this repository does not make them. Methodology and
results for the agent workflows themselves belong in
[claude-code-config](https://github.com/zsz13/claude-code-config), not here.

## Design specs

`docs/superpowers/specs/` holds the design documents written before the corresponding implementation —
each one stating the problem, the evidence it was diagnosed from, the decision, and the
rejected alternatives. They are the clearest record of how the work was actually directed.

---

