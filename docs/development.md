# Development

Development needs Python 3.10 or later, and [Helm](https://helm.sh/) for the
tests of the chart (skipped without it). To set up a development environment
(e.g., in a virtual environment) and run the checks that CI runs:

```bash
pip install -e ".[dev]"  # the app, with the dev dependencies
pre-commit install  # run the checks on every commit
pre-commit run --all-files  # ruff, mypy, semgrep, ast-grep, actionlint
pre-commit run --all-files --hook-stage manual gitleaks-all  # secrets
pytest
```

The tests need no database (they use `mongomock`). To try changes in the
running app, rebuild it with `docker compose up --build -d` (see [Getting
started](getting-started.md)). `python -m cloud_registry.app` runs the app
with Flask's development server instead of Gunicorn (see
[Server](configuration.md#server)); it needs a MongoDB at `db.host` of the
config.

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
`# gitleaks:allow`. See [Contributing](contributing.md) for how to contribute,
including the Python guidelines.

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
it with live reloading, in the dev environment set up as above (Python 3.11
or later):

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
forward to commits on `dev` that are released. To release version `X.Y.Z`
from commit `<sha>` on `dev`, after CI passed for it:

```bash
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag must be annotated and signed (`git tag -s`), with a key that GitHub can
verify (added to the tagger's GitHub account). The [release
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/release.yaml),
triggered by the tag, checks that GitHub verified the signature, that the
tag's commit is on `main`, and that the [CI
workflow](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/.github/workflows/checks.yaml)
passed for it (a run for the push of the commit to `dev`; if CI hasn't
finished or failed, re-run the release workflow once it passed). It packages
the Helm chart with the release version as `version` and `appVersion` (the
default image tag). After approval in the `release` environment, it builds
the image, checks that the app reports the release version, pushes exactly
that image tagged `X.Y.Z` (and `latest`, if it is the highest stable release
on `main`), and creates a GitHub release with the chart (`.tgz`) and the
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

The CI workflow (which tests the chart) and the release workflow (which
packages it) install the same Helm version (`version:` of `azure/setup-helm`
in `checks.yaml` and `release.yaml`). Dependabot updates the action, but not
that version: bump it by hand, in both workflows together.
