# Release management

Mantis uses [release-please](https://github.com/googleapis/release-please)
to turn conventional-commit history on `main` into reviewable,
SemVer-tagged GitHub Releases — no manual version bumping, changelog
editing, or tagging.

## Flow

1. Normal development: PRs merge to `main` as usual (see
   [docs/development.md](development.md)), each with a
   [conventional-commit](https://www.conventionalcommits.org/)-style
   message.
2. `.github/workflows/release-please.yml` runs on every push to `main`.
   It maintains a single open **release PR** that accumulates the
   version bump and changelog entry for every qualifying commit merged
   since the last release. Non-qualifying commits (`docs:`, `chore:`,
   `test:`, `ci:`, `refactor:`) still land in the changelog but don't by
   themselves move the release PR's version — see
   [Versioning policy](#versioning-policy).
3. Merging the release PR (a normal, reviewable PR merge — you decide
   when a release actually ships) creates the Git tag and GitHub Release
   in the same workflow run.
4. That Release event is what a future GHCR-publish workflow (#43) will
   trigger on, reading the version from the release itself rather than
   parsing changelog text.

The release PR is always safe to leave open — every subsequent
qualifying merge to `main` just updates it in place. Nothing is
released until you merge it.

## Versioning policy

Standard [SemVer](https://semver.org/), driven by conventional commit
type — including pre-1.0: this project does **not** use the
npm-style "anything can break before 1.0" dampening. That keeps the
version bump predictable from the commit type alone, and since every
release ships via a PR you review, an unexpected jump to `1.0.0` from
an early breaking change is never a surprise — you'll see it in the
release PR's diff before merging.

| Commit type | Effect |
| --- | --- |
| `fix: ...` | patch bump (`0.1.0` → `0.1.1`) |
| `feat: ...` | minor bump (`0.1.0` → `0.2.0`) |
| `feat!: ...` or `fix!: ...` or a `BREAKING CHANGE:` footer | major bump (`0.1.0` → `1.0.0`) |
| `docs:`, `chore:`, `test:`, `ci:`, `refactor:` | changelog entry only (some hidden from the rendered changelog — see `release-please-config.json`), never triggers a release PR bump by itself |

To intentionally trigger a specific bump, use the corresponding commit
type/marker:

```bash
git commit -m "fix: correct AWX stdout truncation boundary"          # patch
git commit -m "feat: add Prometheus query tool"                       # minor
git commit -m "feat!: rename ToolResult.meta to ToolResult.query_meta"  # major

# or, any type, with a footer:
git commit -m "refactor: restructure tool registry

BREAKING CHANGE: ToolRegistry.get() now raises KeyError instead of
returning None for an unknown tool name."
```

## Configuration

- `release-please-config.json` — package/release-type config (Python,
  single package at repo root), changelog section mapping.
- `.release-please-manifest.json` — tracks the last-released version
  per package (bootstrapped at `0.1.0`, matching `pyproject.toml`,
  since no prior tags/releases exist).
- The canonical version lives in `pyproject.toml` (`[project].version`)
  and is mirrored to `src/mantis/__init__.__version__` via the
  `extra-files` config — release-please updates both in the same
  release PR, so they can never drift.

## Why a GitHub App instead of the default token

The workflow authenticates as a GitHub App
(`actions/create-github-app-token`, using the `RELEASE_PLEASE_APP_ID` /
`RELEASE_PLEASE_APP_PRIVATE_KEY` repo secrets) rather than the default
`GITHUB_TOKEN`. GitHub deliberately does not let a `GITHUB_TOKEN`-authored
push/release trigger further workflow runs, to prevent accidental
recursion — but that would silently break the handoff to the GHCR
publish workflow, which needs to react to the Release this workflow
creates. A GitHub App installation token isn't subject to that
restriction and isn't a long-lived personal-access-token either.

## Idempotency

release-please tracks release state via the manifest file and the Git
tag history, not local workflow state, so re-running the workflow (or
having it run on a merge that doesn't change any package) never creates
a duplicate release PR or duplicate tag/release for a version that's
already shipped.

## Container images

`.github/workflows/container-publish.yml` publishes to
`ghcr.io/jamestrichardson/mantis`. Every tag is a `linux/amd64` +
`linux/arm64` multi-platform manifest — `docker pull`/`docker run`
picks the right one automatically, no `--platform` flag needed. There
are three independent tag policies:

| Trigger | Tags | Notes |
| --- | --- | --- |
| Pull request | `pr-<number>`, `sha-<short-sha>` | Rebuilt on every push to the PR branch, so you can pull and test a change before merging it. Skipped for Dependabot PRs. This repo has no external-fork PRs — a fork PR's token wouldn't have `packages: write` anyway, so this fails closed rather than leaking anything. |
| Push to `main` | `dev`, `sha-<short-sha>` | Rebuilt on every merge to `main`. `dev` always points at the current tip of `main`. For quickly trying out unreleased work. |
| Release published | `<version>`, `<major>.<minor>`, `<major>`, `latest` | Only ever produced from an actual GitHub Release (see [Flow](#flow) above). `latest` **only** moves here — an ordinary merge to `main` or PR build never touches it. |

```bash
docker pull ghcr.io/jamestrichardson/mantis:latest      # newest stable release
docker pull ghcr.io/jamestrichardson/mantis:1.0.0        # pinned version
docker pull ghcr.io/jamestrichardson/mantis:dev          # tip of main, unreleased
docker pull ghcr.io/jamestrichardson/mantis:pr-58        # a specific PR's build
docker pull ghcr.io/jamestrichardson/mantis:sha-0a25bfe  # exact commit
```

All three jobs build the image, load it locally, and run a smoke test
(`docker run <image>` must print the CLI usage banner) *before* logging
in and pushing — a broken image is never published.

Configuration and secrets are never baked into the image; everything
is supplied at container-run time via environment variables — see
[docs/configuration.md](configuration.md) for the full list
(`LITELLM_URL`, `AWX_TOKEN`, etc.):

```bash
docker run --rm \
  -e LITELLM_URL=https://litellm.internal \
  -e LITELLM_API_KEY=... \
  -e AWX_URL=https://awx.internal \
  -e AWX_TOKEN=... \
  ghcr.io/jamestrichardson/mantis:latest \
  awx-troubleshooter
```
