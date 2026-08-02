# Releasing

Releases are fully automatic. A squash-merge to `main` that contains a
release-worthy change produces — in that same pipeline run — the `vX.Y.Z`
git tag, a GitHub Release with generated notes, the versioned Docker image,
and (when deploy variables are configured) a production deploy. There is no
release pull request and no manual step.

Everything below is implemented by `.github/workflows/pipeline.yml` and
`.github/scripts/compute-release.sh`; the design rationale is
[ADR 0009](adr/0009-tag-on-merge-releases.md).

## What each merge produces

The repository squash-merges, so the **PR title** becomes the commit message
on `main` and is what the version analyser (commitizen) reads. A required
status check (`pr-title`) rejects titles that are not Conventional Commits.

| PR title | Version bump | Example |
|---|---|---|
| `fix: …` | patch | 0.2.0 → 0.2.1 |
| `feat: …` | minor | 0.2.1 → 0.3.0 |
| `feat!: …`, `fix!: …`, or a `BREAKING CHANGE:` footer | major — but **minor while the version is 0.x** | 1.4.0 → 2.0.0, 0.3.0 → 0.4.0 |
| `docs:` `chore:` `ci:` `test:` `refactor:` `build:` `perf:` `style:` | none — no tag, no release, pipeline still succeeds | — |

If several unreleased commits have accumulated on `main` (because earlier
merges were no-release types), the next release takes the **highest**
applicable bump across all of them and cuts a single tag.

## Breaking changes under squash merge

Two ways to signal one:

- Put `!` in the PR title: `feat!: drop spec format v1`.
- Put a `BREAKING CHANGE: <description>` footer in the **squash commit
  body**, which is editable in the merge dialog.

The footer must survive into the squash body — a `BREAKING CHANGE:` note
that lives only in a PR comment or in an intermediate commit's body that
gets squashed away is silently lost, and the release comes out one bump too
small. When in doubt, use `!` in the title.

## Forcing a version

Add this footer (its own line, exact syntax) to the squash commit body of
the merge that should carry the forced version:

```
Release-As: 1.0.0
```

The newest `Release-As:` footer since the last tag wins over the computed
bump. This is the intended way to reach `1.0.0` — a breaking change at 0.x
never promotes to 1.0.0 on its own; going stable is a deliberate maintainer
act. Once a `v1.*` tag exists, breaking changes bump major automatically.

## Where the version lives

Nowhere in the tree. `CHANGELOG.md` is frozen (release notes are generated
onto the [GitHub Releases page](../../../releases) from merged PR titles),
and `pyproject.toml` declares `dynamic = ["version"]` — setuptools-scm
derives the version from the `v*` tags at build time. Nothing is committed
back to `main` on release, which is what keeps the pipeline loop-free and
compatible with branch protection.

Docker builds receive the version as the `APP_VERSION` build arg; images are
tagged `X.Y.Z`, `X.Y`, `X`, `latest` (releases move all four) and with the
immutable commit sha (every `main` push, release or not — a non-release push
publishes *only* the sha tag and does not move `latest`).

## Failure recovery

The tag and the GitHub Release are created by one API call, so a tag can
never exist without its Release. If the pipeline fails **after** the release
job (image build or deploy), the tag and Release exist but the versioned
image or deploy is missing. Recover with **"Re-run failed jobs"** on that
workflow run: the release job's outputs (version, released flag) are
preserved from the successful attempt, and the build/deploy jobs pick them
up. Do not use "Re-run all jobs" — that re-runs the release job, which sees
the commit already tagged and correctly declines to release, skipping the
build.

Version tags are immutable twice over: the release script refuses to reuse
an existing tag, and the `protect-release-tags` ruleset blocks tag updates
and deletion for everyone.

## Rollback

Releasing is additive-only; rolling back means pointing the runtime at the
previous version's image (see "Rollback" in
[docs/deployment/azure.md](deployment/azure.md)), then merging a `fix:` that
produces the next patch release. Never delete or move a published tag.
