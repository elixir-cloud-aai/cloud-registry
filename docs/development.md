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

## Dependencies

The dependencies are declared, with version ranges, in `pyproject.toml`
(which installs of the package, e.g., the development environment, use) and
locked in `uv.lock`, with exact versions and hashes, by
[uv](https://docs.astral.sh/uv/), which comes with the dev dependencies.
The image build installs the locked runtime dependencies only
(`uv export --frozen --no-dev --no-emit-project`, which leaves out the
extras, then `pip install --require-hashes --only-binary :all: --no-deps`,
see the `Dockerfile`), so
two builds of the same commit get the same packages, and a new release of a
dependency gets into the image only through a reviewed change of the lock.
The build requirements of the app (`build-system.requires`), with which the
image build builds the app's wheel, are locked the same way, as the
dependency group `build-system`, and installed from the lock with hash
checks, too; the wheel is built without network, so the image build
downloads nothing that isn't hash-checked. Keep the group and
`build-system.requires` the same (the tests fail if they differ).
After adding, removing or changing a dependency in `pyproject.toml`, update
the lock and commit both:

```bash
uv lock  # the versions that satisfy the new ranges, others are kept
uv lock --upgrade-package <name>  # a newer version of one package
uv lock --check  # what CI checks: the lock matches `pyproject.toml`
```

Dependabot updates `pyproject.toml` and `uv.lock` together, including the
`build-system` group (`uv` entry of `.github/dependabot.yml`), and the image
of uv that the `Dockerfile` uses (`docker` entry). The lock also pins FOCA,
and the image build fails unless the base image has exactly the locked FOCA,
so FOCA is updated by hand, in the base image and the lock together
(`uv lock --upgrade-package foca`); Dependabot ignores FOCA in both entries.

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

The current release version is taken from one source, so that a release
doesn't need to bump it in many places: the release tag and, on `dev`, the
`version` in `CITATION.cff` (set before tagging, see below; from then on, the
latest docs show the upcoming version). The docs and the chart's README
(`deployment/README.md`) write it as a placeholder, `version` in double curly
braces with a space inside each pair, and the chart version (e.g.,
`1.2.3-rc1` for `1.2.3rc1`, as in the names of the packaged charts) as
`chart_version` in the same braces (`VARIABLE` and `CHART_VARIABLE` in
`scripts/chart_readme.py`; this page can't show them, as they would be
replaced), also in commands and links. The docs hooks (`docs/hooks.py`)
replace them with the version of `CITATION.cff`, and
`scripts/chart_readme.py` with the release version when the chart is
packaged; files read on GitHub (e.g., `deployment/README.md`) show the
placeholders, and sections of `README.md` included as snippets can't use
them. Comments, templates and tests use placeholders (e.g., `1.2.3`) and
refer to sections by anchor (e.g., `deployment/README.md#to-app-version-040`).
`tests/test_version_mentions.py` fails if the current release version is
mentioned as a release of the app (as a tag, image or chart, or as "release"
or "version" in prose; not, e.g., as the version of a dependency) anywhere
else than in `CITATION.cff`, `docs/releases/`, the tests of the release
tooling, links to the notes and assets of a release, and migration headings
(e.g., "From versions before X"), which are about that release itself.

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. First, shortly before the
release, set its version and date in the citation metadata, which Zenodo
archives with each release, and merge the change into `dev` together with
the release's curated notes (`docs/releases/X.Y.Z.md`, see below; the CI job
"Release readiness" fails on `dev` without them); each version, also each
pre-release (e.g., `X.Y.Zrc1`), needs its own such change:

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
python scripts/release_notes.py require X.Y.Z  # curated notes are required
# (unless no release has notes: then skip the next two lines)
! grep -n TODO docs/releases/X.Y.Z.md
python scripts/release_notes.py check docs/releases/X.Y.Z.md
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
tagging, therefore. The CI job "Release readiness" runs the pin and
the notes checks, too, for the version in `CITATION.cff` while it has no
tag (or its tag is on the checked commit): on pull requests as warnings, on
pushes to `dev` as errors, which fail
the CI run that the release workflow requires for the release commit. The
image scan of the release workflow can still fail after the tag, e.g., on a
vulnerability published since CI last scanned the image; the remedy is the
same.

The tag must be annotated and signed (`git tag -s`), with a key that GitHub can
verify (added to the tagger's GitHub account). The [release
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/release.yml),
triggered by the tag, checks that GitHub verified the signature, that the
tag's commit is on `main` and its `CITATION.cff` has the release version and
a date that is not in the future (UTC), and that the [CI
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/ci.yml)
passed for it (a run for the push of the commit to `dev`; if CI hasn't
finished or failed, re-run the release workflow once it passed). It packages
the Helm chart with the release version as `version` and `appVersion` (the
default image tag). In the packaged README, links to the latest docs and to
files on branch `dev` point to the release (`/en/vX.Y.Z/`, `blob/vX.Y.Z/`);
the release fails if any link to a version that moves is left
(`scripts/chart_readme.py`). The README in the repository is unchanged.
After approval in the `release` environment, it builds
the image and pushes it by digest (untagged, with SBOM and provenance
attestations) and checks that the app reports the release version; it then
scans exactly that digest for vulnerabilities, in a job without credentials.
After a second approval in the `release` environment (once the scan passed),
it attests the build provenance of the scanned digest and signs it (see
[Verifying images and charts](operations.md#verifying-images-and-charts)),
tags exactly that image `X.Y.Z` (and `latest`, if it is the highest stable
release on `main`), checks that the chart it downloads is the one it
packaged (a single file with the name and SHA-256 recorded when packaging),
attests and signs the chart, and creates a GitHub
release with the chart (`.tgz`), its Sigstore bundle (`.tgz.sigstore.json`)
and the release notes (see below). Releases are immutable (see the repository
settings below): once published, their assets can't be added, replaced or
deleted. The workflow therefore creates the release as a draft, uploads
the assets and publishes it last. A re-run finishes a draft left by an
earlier attempt; if the release is already published (e.g., by hand), it
uploads nothing and fails unless the release has exactly the assets
that the run built (only the Sigstore bundles may differ, as every
attempt signs anew). The error says what to do: a new patch version, or,
if releases are not immutable, deleting the release and re-running. Add
curated notes to the [changelog](changelog.md) in `mkdocs.yml` (`nav`; the docs build fails
otherwise), and refer to pages of the docs by name rather than by relative
links, which don't work in GitHub releases. No packages are published to
PyPI. Tags of alpha, beta and release candidate versions (e.g., `vX.Y.Zrc1`,
also `aN` and `bN`) get chart version `X.Y.Z-rc1` (or `-aN`, `-bN`) and never
update the `latest` image. These and, until 1.0.0, all other releases are
GitHub pre-releases (never marked as the latest release). Images of commits
on `dev` are published by the CI workflow (environment `dev-images`).

The release workflow derives the version, the chart version, the kind of
release (stable, GitHub pre-release), whether the image is tagged `latest`
and the previous release (for the generated notes) from the tag with
`scripts/release_version.py`, which is tested over a table of tags
(`tests/test_release_version.py`) and checked like the app (ruff, mypy,
coverage); change these rules there, not in the workflow. It finds the
GitHub release of the tag, if any, and checks the assets of a published one
with `scripts/release_assets.py` (tested in `tests/test_release_assets.py`).

To test unreleased changes of FOCA, `pyproject.toml` may pin a FOCA commit
(`foca @ git+https://github.com/elixir-cloud-aai/foca@<sha>`), with FOCA's
development image of that commit as the base image (tagged by date, e.g.,
`elixircloud/foca:20261022-py3.13`, pinned by digest, as the tag moves with
later commits of that day; the `Dockerfile` records the commit as
`## FOCA commit: <sha>`, which the tests check, and the image build doesn't
reinstall FOCA, but fails unless the base image has FOCA of that commit,
`scripts/image_requirements.py`; the lock has no hash for such a pin, so the
image build leaves it out, while FOCA's dependencies stay locked).
Dependabot ignores `elixircloud/foca` (all updates, see
[Dependencies](#dependencies)), so it doesn't update the dated tag's digest
either, which would change FOCA. Such pins are temporary: the release workflow fails
while a requirement (also an optional one, a dependency group or a build
requirement) is a direct reference or names a pre-release or a local version
(`+<label>`), a FOCA base image
(in any stage) has no release tag, or a chart dependency has no version,
another version than a release or no chart repository (`oci://`,
`https://`; e.g., `file://`) (`scripts/check_release_pins.py`,
tested in `tests/test_check_release_pins.py`), so replace them by the
released versions before releasing. It also fails unless
`deployment/Chart.yaml` and `Chart.lock` pin the version of the library
chart that the `$comment` of `deployment/values.schema.json` names
(`foca-common X.Y.Z`): the version that the release requires, whose values
schema the chart copies. When moving to a new FOCA minor version, update
the comment with the library chart. This applies to pre-releases of the
app, too: they are built on FOCA releases.

Release notes follow the format agreed for all repositories of ELIXIR
On-Cloud:

1. An intro paragraph saying what the release brings, with a link to the
   upgrade steps (e.g., on the [Upgrading](upgrading.md) page) if there are
   any.
2. Grouped sections, with empty ones left out, in this order:
   `## Breaking changes`, `## Features`, `## Security`, `## Fixes`,
   `## Documentation`, `## Maintenance`. They hold bullets only, and each
   bullet ends with its issue and pull request numbers, e.g., `(#12, #34)`
   (several pairs separated by semicolons, e.g., `(#12, #34; #56, #78)`).
3. `## Commits`: GitHub's generated notes, verbatim (including "New
   Contributors"), without their `## What's Changed` header, as they credit
   the authors.
4. Last, GitHub's generated `**Full Changelog**: ...` line (no compare link
   of our own).

The curated notes in `docs/releases/X.Y.Z.md` (for pre-releases, e.g.,
`X.Y.Zrc1.md`) hold parts 1 and 2, without a title (the release's title is
its tag); the release workflow adds parts 3 and 4 from GitHub's
[generate-notes](https://docs.github.com/en/rest/releases/releases#generate-release-notes-content-for-a-release)
API, for the changes since the previous published release in the history of the
release commit (for stable releases the previous stable release, or the previous
pre-release if there is no stable one, as the generated notes can't start
before the first tag; for alpha, beta and release candidate versions the
previous release of any kind). For the first release (no published release
before it, even if older tags exist), the API would start at an older tag of
its choice, so the workflow lists all merged pull requests in the history of
the release commit itself, in the API's format: `scripts/release_notes.py
first` reads the closed pull requests (`gh api --paginate --slurp
"repos/<owner>/<repo>/pulls?state=closed&per_page=100"`, for which the job
has `pull-requests: read`) and the commits (`git rev-list HEAD` at the
release commit); run these commands locally to preview the notes. Generated
notes in an unexpected format (without the `**Full Changelog**` line) are
kept as they are, with a warning, as the image is published by then.
Curated notes are required once any other release has notes (in this
repository, for every release now, pre-releases included): the release
workflow and the CI job "Release readiness" fail without them
(`scripts/release_notes.py require`; only a repository without any notes in
`docs/releases/` gets GitHub's generated notes alone). Curated notes are
published as written, so the release workflow fails if they contain
`TODO`, are not in this format or link to versions that move (the latest
docs, `/en/latest/`, or files on branch `dev`, `blob/dev/`; link the
release's, e.g., `/en/vX.Y.Z/`, `blob/vX.Y.Z/`, as the published notes
don't change) (`scripts/release_notes.py check`, tested in
`tests/test_release_notes.py`, which also checks the notes in
`docs/releases/`), and so does the CI job "Release readiness" on pushes
(see above); on re-runs, it replaces the notes of an existing release (also
overwriting manual edits). Detailed
upgrade steps belong on the [Upgrading](upgrading.md) page, linked from the
intro.

Maintainers: the workflows rely on these repository settings: environment
`dev-images` limited to branch `dev`; environment `release` with a required
reviewer and a deployment tag rule `v*` (so that no workflow of another
branch can use its secrets); a tag ruleset for `v*` that restricts who may
create them and prevents moving or deleting them; immutable releases;
a ruleset for `dev` that requires the check `CI passed` (the gate job of
`ci.yml`, which needs all of its other jobs) as the only check from GitHub
Actions; `main` protected against non-fast-forward pushes; and the Docker Hub
secrets (`DOCKERHUB_LOGIN`, `DOCKERHUB_TOKEN`) only in the two
environments, not as repository secrets. The Docker Hub
organization is the repository variable `DOCKERHUB_ORG` (by default
`elixircloud`; forks set it to the organization they publish to), so that
the scan jobs, which have no secrets, know the image name too, and the
released chart's default image (`cloudRegistry.image`) is the published
one (the chart's README still names upstream's default).
On Read the Docs, the default branch is set to `dev` explicitly, so that
`latest` shows `dev` whatever the repository's default branch is, and an
automation rule activates the versions of tags (`v*`), so that the docs
linked from packaged charts exist.

The CI workflow builds the image of every pull request and scans it with
[Trivy](https://trivy.dev/) (the shared action
[`scan-image`](https://github.com/elixir-cloud-aai/.github/tree/main/actions/scan-image)
of the organisation, pinned to the commit of a release), and a weekly
workflow (`scan.yml`) scans `latest`. For pushes to `dev` and for releases,
the workflows push the image by digest, untagged, scan exactly that digest in
a job without credentials or signing identity, and only if the scan passes
attest, sign and tag it, so that the published images are the scanned ones; a
digest that fails the scan stays untagged and unsigned. The scans fail on
critical and high vulnerabilities that have a fix, and upload the findings
that have a fix (all severities) to code scanning (Security tab; not for pull
requests from forks, nor in the release workflow); findings without a fix
appear there once a fix is released. The weekly scan also uploads a full
report, findings without a fix included, as the workflow artifact
`trivy-report-release-image` of each run (`trivy-report.json` and
`trivy-report.txt`, a table; Actions tab, workflow "Scan"); it is not
uploaded to code scanning. Accepted findings are left out of all results. To
accept a finding (e.g., if it doesn't affect the app), add it to
`.trivyignore.yaml` with the affected package versions (`purls`), the reason
(`statement`) and an expiry date (`expired_at`), after which the scans fail
again. Trivy also scans for secrets (critical and high ones fail the scans
too); accept those under `secrets:` in the same file. Dependabot updates the
actions, including the pins of the shared actions (`@<commit> # vX.Y.Z`), to
new releases of `elixir-cloud-aai/.github`; the Trivy version is the
default of the shared action's `trivy-version` input, which Dependabot
doesn't bump here: change it in the shared action, whose release then
reaches this repository as a Dependabot update of the pins.
Images of commits on `dev` are signed and attested like release images, by
the CI workflow. All of this needs no secrets beyond the Docker Hub ones:
signing uses the workflows' OIDC tokens (permission `id-token: write`). Re-running
all jobs of the release workflow builds, scans, signs and tags a new image
digest (the image build is not reproducible); an earlier digest that was
published stays signed. Re-running only the failed jobs reuses the digest
already pushed. Re-running all jobs also packages the chart again and
replaces the chart artifact; the release checks the new chart against its new
SHA-256.

The CI workflow (which tests the chart) and the release workflow (which
packages it) install the same Helm version (`version:` of `azure/setup-helm`
in `ci.yml` and `release.yml`). Dependabot updates the action, but not
that version: bump it by hand, in both workflows together.

The chart is a thin chart over FOCA's library chart `foca-common` (see the
[library chart's
documentation](https://github.com/elixir-cloud-aai/foca/blob/chart-v0.2.0/docs/helm-chart.md)):
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
`deployment/values.schema.json` with the changes noted in its `$comment`
(which names the new version, `foca-common X.Y.Z`, as the release pin check
requires),
add values it introduces to `deployment/values.yaml`, update the links to
the library's docs in `deployment/README.md` (the tests check that they point
to the pinned version, `chart-vX.Y.Z`), and check the rendered chart with the
tests and `helm template`.
