# Spec NNN: <name>

Status: draft | approved | implemented (PR #) | released (vX.Y.Z)
Plan: docs/architecture/tech-debt-plan.md, step NNN

## Responsibility

One paragraph: the single responsibility this spec moves, where it lives today (file and line ranges), and why it changes independently of what it sits beside.

## Current behaviour this must preserve

Every observable output the moved code affects, each with the code location that produces it today and the test or golden-master entry that pins it. Observable means: entity state, attributes, entity ids and names, log lines tests assert on, stored file formats, and timing guarantees (off the event loop, no calibration inside a state write).

| Behaviour | Produced at | Pinned by |
|---|---|---|
| | `module.py:line` | `tests/test_x.py::test_y` / golden entry |

## Interfaces

The new modules, classes and protocols as typed signatures, with the layer each belongs to. No bodies.

```python
class Example(Protocol):
    def method(self, arg: Type) -> Result: ...
```

## Invariants

Statements that must hold after the change and that a test can check: ordering, idempotence, exact numerical equality, thread or loop affinity.

## Migration

Numbered, each step leaving the suite green:

1. Add the new unit beside the old code, with contract tests.
2. Point callers at it one at a time.
3. Delete the old code; keep a façade only where the spec says so, with the step that removes it.

## Non-goals

What this spec does not change, including defects noticed on the way (file them as issues and link them here).

## Acceptance

The verifier checks each item and cites evidence.

- [ ] Golden master identical for every recorded run.
- [ ] Full suite passes; no existing assertion edited; new tests listed here: ...
- [ ] No source line executed before is unexecuted after.
- [ ] mypy count lower than before (from N to M); zero in new modules.
- [ ] Import contracts: no new violation; moved code sits in its target layer.
- [ ] No new function over 60 lines or class over 250 lines or 15 methods.
- [ ] No method shared by assignment introduced.
- [ ] Spec-specific: ...

## Rollback

How to revert if the live shadow comparison differs: which commit, and whether any stored data needs attention.
