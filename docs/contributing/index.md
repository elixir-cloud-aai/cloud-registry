# Contributing

This project is a community effort and lives off your contributions, be it
bug reports, feature requests, discussions, documentation or code. It follows
the [contributing guide of ELIXIR
On-Cloud](https://github.com/elixir-cloud-aai/.github/blob/main/CONTRIBUTING.md),
which covers getting started, issues, branches and pull requests, commit
messages and pull request titles, the code of conduct and the security
policy. This page covers what is specific to this repository, which is also
the template for other apps built with [FOCA](https://foca.readthedocs.io/):
see [Development](../development.md) for how to set up a development
environment.

## Commit messages and pull request titles

Use `feat`, `fix`, `perf`, `refactor` and `style` only for changes of what is
deployed: the app (`cloud_registry/`) and its Helm chart (`deployment/`).

## Citation metadata

Cloud Registry is cited with the metadata in
[`CITATION.cff`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/CITATION.cff)
([Citation File Format](https://citation-file-format.github.io/)), which
GitHub ("Cite this repository") and Zenodo use. Its `doi` is the Zenodo
concept DOI, which stays the same for all versions (see
[Citation](../index.md#citation)). Its authors follow the [citation metadata
rule of the contributing
guide](https://github.com/elixir-cloud-aai/.github/blob/main/CONTRIBUTING.md#citation-metadata)
(not counting vendored files and the reformatting commits in
`.git-blame-ignore-revs`). CI validates the file (`cffconvert --validate`). Before each release, `python
scripts/citation.py set X.Y.Z` sets its `version` and `date-released`, and
the release workflow fails if they don't match the release (`python
scripts/citation.py check X.Y.Z`; see [Versioning and
releases](../development.md#versioning-and-releases)).

## Python guidelines

The app targets one Python version, that of the image (3.13;
`requires-python` in `pyproject.toml`), which CI tests.
Most of the following is checked by pre-commit and CI (see
[Development](../development.md) for how to run the checks locally); the rest is
checked in reviews.

- **Packaging:** the package, its dependencies and the configuration of the
  tools are in `pyproject.toml`; runtime dependencies in
  `project.dependencies`, those for development and the documentation in the
  extras `dev` and `docs`. `uv.lock` locks them all, at exact versions and
  with hashes; the image installs the locked runtime dependencies only, and
  CI's checks run on the locked versions of all of them. After
  changing dependencies, run `uv lock` and commit `uv.lock` with the change
  (see [Dependencies](../development.md#dependencies)). Don't add `setup.py` or
  requirements files.
- **Formatting and linting:** [ruff](https://docs.astral.sh/ruff/) formats
  the code (`ruff format`, compatible with Black, 88 characters per line) and
  lints it (`ruff check`) with the rule set shared by FOCA and FOCA apps:
  pycodestyle, Pyflakes, isort, flake8-bugbear, flake8-comprehensions,
  flake8-simplify, Pylint, Ruff's own rules, pyupgrade, bandit (security),
  logging format and pydocstyle, also for the tests (`pyproject.toml`, where
  each ignored rule says why). Prefer explicit checks to suppressions
  (`# noqa`, `# type: ignore`); justify the rare exceptions in the comment.
- **Docstrings:** Google-style docstrings at all levels: packages, modules,
  classes, methods and functions, also private ones and tests, with a
  one-line summary and, where applicable, the sections `Args`, `Returns` (or
  `Yields`), `Raises` and, for classes, `Attributes`; consider `Examples` and
  `Note`. Ruff's pydocstyle rules (Google convention) check those of public
  objects and their `Args`; reviews check the others. The [API
  reference](../reference.md) is generated from them.
- **Type hints:** complete type hints for all functions and methods (also of
  the tests), including `*args` and `**kwargs`, and generic types with their
  parameters (e.g., `dict[str, Any]`, not `dict`); add hints to module-level
  variables and to local variables whose type isn't obvious.
  [mypy](https://mypy.readthedocs.io/) checks the hints, and that functions
  and generic types have them (`disallow_untyped_defs`,
  `disallow_any_generics`); reviews check those of variables.
- **Tests:** [pytest](https://docs.pytest.org/), with coverage measured by
  [coverage](https://coverage.readthedocs.io/): keep it at 100% of
  `cloud_registry` and `scripts`, which is enforced: `coverage report` (and
  CI) fails below 100% (`fail_under` in `pyproject.toml`), and so do
  Codecov's checks of the whole code and of the changes of a pull request
  (`codecov.yml`). Add tests that fail without your change.
- **Further checks:** [ast-grep](https://ast-grep.github.io/) (rules in
  `.ast-grep/`, with tests), [semgrep](https://semgrep.dev/) (security rules
  in `.semgrep/`), [gitleaks](https://github.com/gitleaks/gitleaks) (secrets)
  and [actionlint](https://github.com/rhysd/actionlint) (workflows), and
  pre-commit's checks of YAML, whitespace, merge conflicts and large files.

Suppress a finding only if it is a false positive or can't be avoided, on its
line, with a comment with the reason (see [Development](../development.md)).

## Documentation

Update the documentation with every change that affects it: the pages in
`docs/` (built with MkDocs, see [Documentation](../development.md#documentation)),
the README of the Helm chart and the docstrings. Describe user-visible
changes in the pull request, and in the notes of the next release if they
exist already (`docs/releases/`, see [Versioning and
releases](../development.md#versioning-and-releases)).
