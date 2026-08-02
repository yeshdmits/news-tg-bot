# ADR 0009 — Releases are tagged on merge, by one workflow, with nothing committed back

## Status

Accepted. Replaces the release-please setup (removed in the PR that added
this ADR).

## Context

release-please gates every release behind a generated release PR: merge a
`feat:`, wait for the bot to open "chore(main): release X.Y.Z", merge that
too. Two human steps per release, and the bot PR sits open collecting
required-status-check friction. What we want: a merge to `main` that
warrants a release produces the tag, the GitHub Release, the versioned
image, and the deploy in that single pipeline run.

Two GitHub constraints shape the design more than any tool choice:

1. **The default `GITHUB_TOKEN` does not trigger workflows.** A tag pushed
   or a Release created with it will never start a workflow listening on
   `push: tags` or `release: published`. Event-driven chaining
   (release workflow → tag → build workflow) silently does nothing.
2. **`main` is protected** (PR + required checks, squash-merge only). The
   default token cannot push a version-bump commit to it; a PAT or App
   token could, but then the bump push *does* re-trigger the release
   workflow — an infinite bump loop unless explicitly guarded.

## Decision

- **One workflow, sequential jobs**: `release` (compute version → tag +
  GitHub Release) → `build-and-push` (`needs: release`) → `deploy`
  (`needs: build-and-push`). The version flows between jobs as a job
  output and is never re-derived. Nothing listens on `push: tags` or
  `release: published` — because of constraint 1, nothing ever may.
- **No commit back to `main`.** No file in the tree records the version:
  `pyproject.toml` uses setuptools-scm (version derived from `v*` tags at
  build time), and `CHANGELOG.md` is frozen in favour of generated notes on
  the GitHub Release. This sidesteps constraint 2 entirely — no bypass
  token, and with no push there is no loop to guard. The default token's
  no-retrigger property (constraint 1) then works *for* us as defence in
  depth.
- **Commitizen** (`cz bump --get-next`) is the version analyser — Python
  native, Conventional Commits, `--major-version-zero` while the latest tag
  is `v0.*` so pre-1.0 breaking changes bump minor. A `Release-As: X.Y.Z`
  squash-body footer forces an exact version; reaching 1.0.0 happens only
  that way. Tag + Release are created together by one `gh release create`
  call, so a tag cannot exist without its Release.
- A `concurrency` group serialises `main` runs (`cancel-in-progress:
  false`), so two quick merges compute their versions strictly one after
  the other.

## Consequences

- Anyone who splits the pipeline into a tag-triggered build or
  release-triggered deploy workflow breaks it **silently** — the new
  workflow never fires, because of constraint 1. That is the trap this ADR
  exists to document. Extend the job chain instead.
- Since the PR title becomes the squash commit, the `pr-title` required
  check enforces Conventional Commit titles, and a `BREAKING CHANGE:`
  footer must be placed in the squash body at merge time (docs/releasing.md).
- The version exists only as git tags. Builds without git metadata get
  `0.0.0` unless CI injects the real version (Docker build arg / pretend
  version env). `git describe` — not any file — answers "what version is
  this checkout".
- Re-running a release run on an already-tagged commit is a no-op by
  design; recovery from a failure between tag and image is "Re-run failed
  jobs" (job outputs are preserved across re-runs). See docs/releasing.md.
