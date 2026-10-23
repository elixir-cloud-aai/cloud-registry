# Development

See [Contributing](contributing.md) for how to contribute, including the
Python guidelines; this page describes the development setup, the checks, the
documentation and the releases.

Development needs Python 3.13 or later, and [Helm](https://helm.sh/) for the
tests of the chart (skipped without it). To set up a development environment
(e.g., in a virtual environment) and run the checks that CI runs:

```bash
pip install -e ".[dev]"  # the app, with the dev dependencies
pre-commit install  # run the checks on every commit
pre-commit run --all-files  # ruff, mypy, semgrep, ast-grep, actionlint
pre-commit run --all-files --hook-stage manual gitleaks-all  # secrets
helm dependency build deployment  # the library chart, for the chart tests
pytest
```

The tests need no database: they use an in-memory one
([mongomock](https://github.com/mongomock/mongomock)). CI also runs them
against MongoDB 7.0; to do so locally, set `MONGODB_TEST_URI` to a
single-node MongoDB without other data, one per test run (the tests drop its
database `serviceStore` before each test, and refuse to start if it exists),
e.g.:

```bash
docker run -d --rm --name mongodb-test -p 127.0.0.1:27017:27017 mongo:7.0
MONGODB_TEST_URI=mongodb://localhost:27017 pytest
docker stop mongodb-test
```

To try changes in the running app, rebuild it with
`docker compose up --build -d` (see [Getting started](getting-started.md)).
`python -m cloud_registry.app` runs the app with Flask's development server
instead of Gunicorn (see [Server](configuration.md#server)); it needs a
MongoDB at `db.host` of the config.

Run `pre-commit` in the environment with the dev dependencies: the mypy and
ast-grep hooks use the installed packages. The first run sets up the other
hooks, which takes a few minutes.

The checks are configured in
[`.pre-commit-config.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.pre-commit-config.yaml)
and
[`pyproject.toml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/pyproject.toml).
Project-specific rules are in `.semgrep/` (security) and `.ast-grep/`
(anti-patterns such as HTTP status codes as strings).
[actionlint](https://github.com/rhysd/actionlint) checks the workflows in
`.github/workflows/`, and their shell scripts with
[ShellCheck](https://www.shellcheck.net/) if it is installed (as on
GitHub-hosted runners, so CI may find more than a local run without it). To
suppress a false positive, add a comment with the reason to the line:
`# noqa: <code>` (ruff), `# type: ignore[<code>]` (mypy),
`# nosemgrep: <rule-id>`, `# ast-grep-ignore: <rule-id>` or
`# gitleaks:allow`.

## Documentation

This documentation is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), from
`docs/` and
[`mkdocs.yml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/mkdocs.yml),
by Read the Docs (configured in
[`.readthedocs.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.readthedocs.yaml))
and in CI; warnings, e.g., of broken links, fail the build. The [API
reference](reference.md) is generated from the docstrings with
[mkdocstrings](https://mkdocstrings.github.io/). To build it locally, or serve
it with live reloading, in the dev environment set up as above:

```bash
pip install -e ".[docs]"
mkdocs build --strict  # into `site/`, ignored by Git
mkdocs serve
```

Parts of the docs are kept elsewhere, so that they are written only once: the
introduction and the quick start of the home and getting started pages are
sections of `README.md` (between `--8<-- [start:<name>]` and
`--8<-- [end:<name>]` comments), and the [deployment page](deployment.md) is
the README of the Helm chart (`deployment/README.md`), which `docs/hooks.py`
includes, with its links to pages of the docs (`../docs/<page>.md`) and other
files of the repository adapted.

## Versioning and releases

The project adopts the [semantic versioning](https://semver.org/) scheme for
versioning. Currently the service is in beta stage, so the API may change
without further notice.

The version is only stored in Git tags (`vX.Y.Z`): the package version is
derived from them (via
[setuptools-scm](https://setuptools-scm.readthedocs.io/)), and the service
info reports it. Builds without Git metadata, such as local Docker builds
(e.g., with Docker Compose), report `0.0.0.dev0`; the workflows pass the
version to the image build. Images on Docker Hub
(`elixircloud/cloud-registry`) are tagged:

- `X.Y.Z`: release `X.Y.Z`; `latest`: the highest stable release (not an
  alpha, beta or release candidate) on `main`;
- `dev`: the latest commit on `dev`; `sha-<commit>`: commit `<commit>` on
  `dev` (full SHA), to deploy a fixed commit.

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. First, shortly before the
release, set its version and date in the citation metadata, which Zenodo
archives with each release, and merge the change into `dev`; each version,
also each pre-release (e.g., `X.Y.Zrc1`), needs its own such change:

```bash
python scripts/citation.py set X.Y.Z  # date: today (UTC), or --date YYYY-MM-DD
```

Then, to release version `X.Y.Z` from commit `<sha>` on `dev` (with that
change), after CI passed for it:

```bash
git switch --detach <sha>
# The release workflow's checks that need no tag, as it runs them
python scripts/release_version.py outputs vX.Y.Z
python scripts/citation.py check X.Y.Z
python scripts/check_release_pins.py
! grep -n TODO docs/releases/X.Y.Z.md  # curated notes, if any
readme="$(mktemp -d)/README.md" && cp deployment/README.md "${readme}"
python scripts/chart_readme.py "${readme}" vX.Y.Z  # links of the chart
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

Tags can't be moved or deleted (see the repository settings below), and the
release workflow checks the tagged commit, so a check that fails after the
tag is pushed fails again on re-runs: the release then needs a new patch
version, with its own citation metadata and notes. Run these checks before
tagging, therefore. The image scan of the release workflow can still fail
after the tag, e.g., on a vulnerability published since CI last scanned the
image; the remedy is the same.

The tag must be annotated and signed (`git tag -s`), with a key that GitHub can
verify (added to the tagger's GitHub account). The [release
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/release.yaml),
triggered by the tag, checks that GitHub verified the signature, that the
tag's commit is on `main` and its `CITATION.cff` has the release version and
a date that is not in the future (UTC), and that the [CI
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/checks.yaml)
passed for it (a run for the push of the commit to `dev`; if CI hasn't
finished or failed, re-run the release workflow once it passed). It packages
the Helm chart with the release version as `version` and `appVersion` (the
default image tag). In the packaged README, links to the latest docs and to
files on branch `dev` point to the release (`/en/vX.Y.Z/`, `blob/vX.Y.Z/`);
the release fails if any link to a version that moves is left
(`scripts/chart_readme.py`). The README in the repository is unchanged.
After approval in the `release` environment, it builds
the image and pushes it by digest (untagged, with SBOM and provenance
attestations), checks that the app reports the release version, attests
its build provenance and signs it (see [Verifying
images and charts](operations.md#verifying-images-and-charts)), tags exactly
that image `X.Y.Z` (and `latest`, if it is the highest stable release on
`main`), attests and signs the chart, and creates a GitHub release with the
chart (`.tgz`), its Sigstore bundle (`.tgz.sigstore.json`) and the
notes in `docs/releases/X.Y.Z.md` (for pre-releases, e.g., `X.Y.Zrc1.md`),
or, if there are none, notes generated from the merged pull requests. Curated
notes are published as written, so the release workflow fails if they contain
`TODO`; on re-runs, it replaces the notes of an existing release with them
(also overwriting manual edits). Add curated notes to the
[changelog](changelog.md) in `mkdocs.yml` (`nav`; the docs build fails
otherwise), and refer to pages of the docs by name rather than by relative
links, which don't work in GitHub releases. No packages are published to
PyPI. Tags of alpha, beta and release candidate versions (e.g., `vX.Y.Zrc1`,
also `aN` and `bN`) get chart version `X.Y.Z-rc1` (or `-aN`, `-bN`) and never
update the `latest` image. These and, until 1.0.0, all other releases are
GitHub pre-releases (never marked as the latest release). Images of commits
on `dev` are published by the CI workflow (environment `dev-images`).

The release workflow derives the version, the chart version, the kind of
release (stable, GitHub pre-release) and whether the image is tagged `latest`
from the tag with `scripts/release_version.py`, which is tested over a table
of tags (`tests/test_release_version.py`) and checked like the app (ruff,
mypy, coverage); change these rules there, not in the workflow.

To test unreleased changes of FOCA, `pyproject.toml` may pin a FOCA commit
(`foca @ git+https://github.com/elixir-cloud-aai/foca@<sha>`), with FOCA's
development image of that commit as the base image (tagged by date, e.g.,
`elixircloud/foca:20261022-py3.13`, pinned by digest, as the tag moves with
later commits of that day; the `Dockerfile` records the commit as
`## FOCA commit: <sha>`, which the tests check, and the image build doesn't
reinstall FOCA, but fails unless the base image has FOCA of that commit,
`scripts/image_requirements.py`).
Dependabot ignores the base image meanwhile. Such pins are temporary: the
release workflow fails
while a requirement (also an optional one, a dependency group or a build
requirement) is a direct reference or names a pre-release, a FOCA base image
(in any stage) has no release tag, or a chart dependency has no version,
another version than a release or no chart repository (`oci://`,
`https://`; e.g., `file://`) (`scripts/check_release_pins.py`,
tested in `tests/test_check_release_pins.py`), so replace them by the
released versions before releasing. This applies to pre-releases of the
app, too: they are built on FOCA releases.

In curated notes, link all changes of the release as the compare view from
the previous release's tag (e.g.,
`https://github.com/elixir-cloud-aai/cloud-registry/compare/vA.B.C...vX.Y.Z`)
rather than naming a range of pull requests, which goes stale when more pull
requests are merged before the release.

Maintainers: the workflows rely on these repository settings: environment
`dev-images` limited to branch `dev`; environment `release` with a required
reviewer; a tag ruleset for `v*` that restricts who may create them and
prevents moving or deleting them; `main` protected against non-fast-forward
pushes; and the Docker Hub secrets (`DOCKERHUB_LOGIN`, `DOCKERHUB_TOKEN`,
`DOCKERHUB_ORG`) only in the two environments, not as repository secrets.
On Read the Docs, an automation rule activates the versions of tags (`v*`),
so that the docs linked from packaged charts exist.

The CI workflow builds the image of every pull request and push and scans
it with [Trivy](https://trivy.dev/) (`.github/actions/scan-image`); the
release workflow builds and scans the release commit's image again before
publishing (in a job without its credentials), and a weekly workflow
(`scan.yaml`) scans `latest`. They fail on critical and high
vulnerabilities that have a fix, and upload all findings to code scanning
(not for pull requests from forks, nor in the release workflow). To accept
a finding (e.g., if it doesn't affect the app), add it to `.trivyignore.yaml`
with the affected package versions (`purls`), the reason (`statement`) and an
expiry date (`expired_at`), after which the scans fail again. Trivy also
scans for secrets (critical and high ones fail the scans too); accept those
under `secrets:` in the same file. Dependabot
updates the actions, but not the Trivy version (`version:` in
`.github/actions/scan-image/action.yml`): bump it by hand. Images of
commits on `dev` are signed and attested like release images, by the CI
workflow. All of this needs no secrets beyond the Docker Hub ones: signing
uses the workflows' OIDC tokens (permission `id-token: write`). Re-running
the release workflow builds, signs and tags a new image digest (the image
build is not reproducible); the earlier digest stays signed.

The CI workflow (which tests the chart) and the release workflow (which
packages it) install the same Helm version (`version:` of `azure/setup-helm`
in `checks.yaml` and `release.yaml`). Dependabot updates the action, but not
that version: bump it by hand, in both workflows together.

The chart is a thin chart over FOCA's library chart `foca-common` (see the
[library chart's
documentation](https://github.com/elixir-cloud-aai/foca/blob/chart-v0.1.0/docs/helm-chart.md)):
`deployment/Chart.yaml` pins its version, and `deployment/Chart.lock` locks
it; `helm dependency build deployment` downloads it into `deployment/charts/`
(ignored by Git), and the release workflow packages it with the chart. CI
and the release workflow check the downloaded archive against its digest in
`deployment/charts.sha256`, as OCI tags can be pushed again.
Dependabot doesn't update it: to bump it, change the version in
`Chart.yaml`, run `helm dependency update deployment` (which rewrites
`Chart.lock`), update `deployment/charts.sha256` (`cd deployment && sha256sum
charts/foca-common-X.Y.Z.tgz > charts.sha256`, after checking the archive),
copy the library's `app-values.schema.json` to
`deployment/values.schema.json` with the changes noted in its `$comment`,
add values it introduces to `deployment/values.yaml`, update the links to
the library's docs in `deployment/README.md` (the tests check that they point
to the pinned version, `chart-vX.Y.Z`), and check the rendered chart with the
tests and `helm template`.
