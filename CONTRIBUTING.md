# Contributing

Thanks for your interest in the Cloud Registry! Contributions of all kinds
are welcome: bug reports, feature requests, discussions, documentation and
code. The project follows the [contributor guide of ELIXIR
On-Cloud](https://elixir-cloud-aai.github.io/contribute/); this is a summary
of its main points. **See [Contributing in the
documentation](https://cloud-registry.readthedocs.io/en/latest/contributing/)
for the details**, including the Python guidelines and the checks to run.

- **Code of conduct:** please follow our [code of conduct](CODE_OF_CONDUCT.md)
  in all interactions with the community.
- **Issues first:** propose features and report bugs in the [issue
  tracker](https://github.com/elixir-cloud-aai/cloud-registry/issues), with
  enough context for others to work on them (for bugs, steps to reproduce).
  Before working on a change, find or open an issue and agree on the approach
  with the maintainers, so that no work is in vain.
- **Getting in touch:** discuss issues and pull requests on GitHub. For
  everything else, join the ELIXIR On-Cloud chat and introduce yourself (see
  the [workflow](https://elixir-cloud-aai.github.io/contribute/workflow/));
  one-time contributors can skip that and open pull requests from forks.
- **Branches and pull requests:** branch off an up-to-date `dev` (e.g.,
  `fix/<topic>` or `docs/<topic>`), and open a pull request against `dev`
  that closes the issue. Keep pull requests small and focused; split large
  changes. Pull requests are squash-merged by the maintainers once the
  checks pass and the review comments are addressed.
- **Conventional Commits:** pull request titles follow [Conventional
  Commits](https://www.conventionalcommits.org/)
  (`<type>[(<scope>)]: <description>`, at most 50 characters, imperative
  mood), with the types `feat`, `fix`, `perf`, `refactor` and `style` for
  changes of the app, and `build`, `ci`, `docs`, `test`, `chore` or `revert`
  for everything else.
- **Quality:** follow the coding conventions, add tests (coverage stays at
  100%), and update the documentation. Run the checks locally (pre-commit and
  the tests) before asking for a review.
- **Reviews:** every change is reviewed by at least one other person; request
  a review once CI passes.
