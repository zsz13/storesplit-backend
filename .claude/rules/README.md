# Instruction architecture

Root `CLAUDE.md` is loaded into context for **every** task in this repository. Everything in
it is paid for on every request, whether or not it is relevant to the change being made — so
it holds only what applies to nearly every task: the purpose, the architecture map, the
invariants that must hold whatever file is open, the validation commands, and a pointer to
the rest.

The detail lives in this directory, one file per area, each declaring the paths it applies to
in YAML front matter:

```yaml
---
paths:
  - "app/services/**"
---
```

The rule is read when work touches a matching path (`app/retailers/**` → `retailers.md`). Root `CLAUDE.md` carries the
pointer table, so the relevant file is discoverable from the always-loaded context rather
than by searching.

## Why it is split this way

Before this split, the backend root file was several hundred lines, most of which was
area-specific: retailer-by-retailer payload semantics, per-component copy rules, migration
procedure. All of it was loaded to rename a CSS class.

Splitting it trades one large always-loaded file for a small one plus progressive disclosure.
The costs are real and worth naming: a rule can be missed if the pointer table goes stale, and
an invariant that belongs in both places has to be stated twice — once as a one-line rule at
the root, once with its reasoning in the scoped file.

So the split follows two conventions:

1. **An invariant that can be violated from anywhere stays at the root**, in one line, even
   though its full reasoning lives in a scoped file. The fail-closed database guard and the
   availability states are the clearest cases: you can break either from a file that matches
   no rule's paths.
2. **Nothing is only in a scoped file if a reviewer needs it without opening that area.** The
   settled decisions that must not be re-litigated in an audit are stated at the root for
   exactly this reason.

## What this is not

This is a project convention, not a platform guarantee. The `paths:` front matter mirrors the
layout used in this author's own global configuration; it is honoured because root
`CLAUDE.md` points here and because the agent is instructed to read the matching file, not
because a hook enforces it. There is no official line limit being satisfied — the target is
simply to keep always-loaded context small and let specialised guidance be loaded where it is
relevant.
