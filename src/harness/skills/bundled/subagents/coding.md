---
name: coding
description: Engineering discipline for implementing modules to a fixed contract — read the contract first, small verified steps, boundary/concurrency robustness, security defaults, no AI-default code.
---

# Coding Discipline

Follow these steps for every module you implement. The point is a module that
meets its contract exactly and survives hostile inputs and concurrent callers —
not code that merely works on the happy path.

## 1. Read the contract before writing anything

The task brief fixes exact class names, method signatures, return types, and
behavioral rules (e.g. "out-of-order transitions are no-ops"). Write them down
and implement TO them, not to your own idea of the API. A module that renames a
method or changes a return type fails even if it "works".

## 2. Small steps, each verified

Implement one method, then prove it with a focused test before moving on. Do not
write the whole module and check at the end. Prefer TDD: write the failing test,
implement the minimum, watch it pass.

## 3. Boundary and concurrency robustness

- Validate inputs: reject missing/non-positive/oversized values with the
  documented error, never a crash.
- Thread-safety: a module that other threads may call concurrently (state
  machines, storage) must not corrupt or raise under concurrent use. Guard
  shared state with a lock — for a shared sqlite connection set
  `check_same_thread=False` and serialize access with the lock, or use a
  connection per call.
- Persistence must survive close-and-reopen.

## 4. Security defaults

- SQL: ALWAYS parameter binding (`?` placeholders). Never build SQL by f-string
  or string concatenation.
- No eval(), no exec(), no hardcoded passwords/secrets/API keys.
- Enforce the documented request limits; reject oversized input cleanly without
  crashing the server.

## 5. No AI-default code

If you write something because it "looks standard", stop and ask what it buys.
Every non-trivial choice needs a one-line reason (in a comment or your report):
why this structure, this data layout, this boundary. Deleting a needless
abstraction is an improvement.

## 6. Verify before finishing

Run the contract's verification (the harness writes a fixed verify_impl.py into
the out dir) and the module tests. Report exactly which files you wrote and the
verification result. Never report success on an unverified module.
