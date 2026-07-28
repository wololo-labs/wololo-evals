# Contributing to `wololo-evals`

Every change to `main` goes through a pull request approved by
[@stevengonsalvez](https://github.com/stevengonsalvez). No exceptions, including
changes authored by agents.

## The flow

```
┌──────────┐   ┌───────────────┐   ┌──────────┐   ┌────────────────┐   ┌───────┐
│  branch  │──▶│ atomic commits│──▶│ open PR  │──▶│ @stevengonsalvez│──▶│ merge │
│  off main│   │ signed        │   │          │   │ approves        │   │       │
└──────────┘   └───────────────┘   └──────────┘   └────────────────┘   └───────┘
                                                          ▲
                                              Approval Gate check reports
                                              red until this approval lands
```

## Rules

| Rule | Detail |
|------|--------|
| **No direct pushes to `main`** | Branch, PR, approve, merge. Always. |
| **Owner approval required** | @stevengonsalvez is the sole CODEOWNER. A PR without their approving review is not mergeable in policy terms. |
| **Signed commits** | `git commit -S`. Unsigned commits are rejected at review. |
| **Atomic commits** | One concern per commit. Never bulk-commit unrelated changes. |
| **Conventional Commits** | `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`, `build:`. |
| **No attribution noise** | Commit messages describe the change. No tool, assistant, or agent attribution. |
| **Green before review** | Run the repo's tests and linters locally; do not open a PR you know is red. |

### A note on enforcement

This organisation runs on GitHub Free, which cannot apply branch protection or
rulesets to private repositories, so neither "require a review" nor "require
signed commits" can be switched on. Two workflow checks stand in for them:

| Check | Reports red when |
|---|---|
| `Owner approval` | no approving review from @stevengonsalvez |
| `Signed commits` | any commit in the PR is not a verified signature |

CODEOWNERS also requests @stevengonsalvez's review automatically. None of this
physically blocks a merge or a direct push to `main`.

Note the difference in strength between the two checks. `Signed commits` tests a
cryptographic property, so it cannot be talked around. `Owner approval` tests an
identity claim, and anyone acting with the owner's credentials satisfies it
trivially, so treat it as a reminder rather than a control.

**The gate is therefore a convention, and the convention is binding.** Treat a
red Approval Gate as a hard stop. If you find a commit that reached `main`
without a PR, open an issue rather than quietly building on top of it.

## Branch naming

| Prefix | Use for |
|--------|---------|
| `feat/` | new capability |
| `fix/` | bug fix |
| `docs/` | documentation only |
| `chore/` | dependencies, tooling, housekeeping |
| `refactor/` | behaviour-preserving restructuring |

Suffix with a short slug: `feat/scim-group-sync`, `fix/token-expiry-off-by-one`.

## Opening a pull request

1. Fill in the PR template. The "how this was verified" section is not optional.
   Say what you ran and what you saw.
2. Keep PRs small enough to review in one sitting. If a PR touches more than a
   handful of concerns, split it.
3. Link the issue or bead it closes.
4. Wait for the Approval Gate to go green before merging.

## Reporting a bug

Open an issue with: what you expected, what happened, the exact command or
request, and the full error output. Redact secrets. Never paste a live token,
key, or tenant identifier into an issue.

## Security

Do not open a public issue for a security problem. Email
steven.gonsalvez@gmail.com directly.
