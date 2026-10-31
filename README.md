<!-- markdownlint-disable MD033 MD041 -- logo first; <picture> for dark mode -->
<a href="https://elixir-on.cloud/">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo_elixir_on_cloud_night.png">
    <img src="docs/images/logo_elixir_on_cloud.png" alt="ELIXIR On-Cloud">
  </picture>
</a>
<!-- markdownlint-enable MD033 MD041 -->

# Cloud Registry

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![Docker_image][badge-docker]][badge-url-docker]
[![DOI][badge-doi]][badge-url-doi]

<!-- --8<-- [start:synopsis] -->
A registry of web services for your project, network or infrastructure,
brought to you by [ELIXIR On-Cloud](https://elixir-on.cloud/). It implements
the [GA4GH Service Registry
API](https://github.com/ga4gh-discovery/ga4gh-service-registry), so that
clients can discover your GA4GH services (or any other web services) and
their API types, versions and operators in one place. Service entries comply
with the [`ExternalService`
schema](https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158).
Anyone can read them (unless you require login); users logged in with an
OpenID Connect provider that you trust can register services and manage those
they own, and admins can verify them. Run it with Docker Compose or deploy it
to Kubernetes with its Helm chart; a [web front
end](https://github.com/elixir-cloud-aai/cloud-registry-ui) is available,
too.
<!-- --8<-- [end:synopsis] -->

**Read the [documentation][docs]** for the configuration, the API, access
control, the deployment with the Helm chart in [`deployment/`][deployment],
operations, upgrades and development.

## Known deployments

<!-- --8<-- [start:deployments] -->
| Deployment | Operated by | Notes |
| --- | --- | --- |
| <https://registry.elixir-on.cloud/> | [ELIXIR On-Cloud](https://elixir-on.cloud/) | With the [web front end](https://github.com/elixir-cloud-aai/cloud-registry-ui) |

Running the Cloud Registry yourself? Open a pull request that adds your
deployment to the table in
[`README.md`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/README.md).
<!-- --8<-- [end:deployments] -->

## Quick start

<!-- --8<-- [start:quick-start] -->
With [Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
(tested with 2.17.1) and [Docker](https://docs.docker.com/get-docker/) with
the [Compose plugin](https://docs.docker.com/compose/install/) (version 2)
installed, clone the repository and start the registry with its MongoDB
database:

```bash
git clone https://github.com/elixir-cloud-aai/cloud-registry.git
cd cloud-registry
docker compose up --build -d
```

The API is then served at `http://localhost:8080/ga4gh/registry/v1/`, with a
Swagger UI at
[http://localhost:8080/ga4gh/registry/v1/ui](http://localhost:8080/ga4gh/registry/v1/ui):

```bash
curl -X GET "http://localhost:8080/ga4gh/registry/v1/services" -H  "accept: application/json"
```
<!-- --8<-- [end:quick-start] -->

## Citation

<!-- --8<-- [start:citation] -->
If you use Cloud Registry, please cite it with its DOI
[10.5281/zenodo.23606856](https://doi.org/10.5281/zenodo.23606856), which
always resolves to the latest version on [Zenodo](https://zenodo.org/); the
Zenodo record lists the DOI of each version, too. The citation metadata is in
[`CITATION.cff`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/CITATION.cff):
on GitHub, "Cite this repository" in the sidebar of the repository formats it
as APA or BibTeX.
<!-- --8<-- [end:citation] -->

## Contributing

Contributions are welcome! See [Contributing][docs-contributing] in the
documentation and the organisation's [contributing guide][contributing], and
mind our [code of conduct][code-of-conduct].

[badge-build-status]: <https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/ci.yml/badge.svg?branch=dev>
[badge-coverage]: <https://codecov.io/gh/elixir-cloud-aai/cloud-registry/branch/dev/graph/badge.svg?branch=dev>
[badge-docker]: <https://img.shields.io/docker/v/elixircloud/cloud-registry?sort=semver&label=image&color=C39BD3>
[badge-doi]: <https://zenodo.org/badge/DOI/10.5281/zenodo.23606856.svg>
[badge-docs]: <https://img.shields.io/readthedocs/cloud-registry>
[badge-github-tag]: <https://img.shields.io/github/v/tag/elixir-cloud-aai/cloud-registry?color=C39BD3>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]: <https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/ci.yml?query=branch%3Adev>
[badge-url-coverage]: <https://codecov.io/gh/elixir-cloud-aai/cloud-registry?branch=dev>
[badge-url-docker]: <https://hub.docker.com/r/elixircloud/cloud-registry/tags>
[badge-url-doi]: <https://doi.org/10.5281/zenodo.23606856>
[badge-url-docs]: <https://cloud-registry.readthedocs.io/en/latest/>
[badge-url-github-tag]: <https://github.com/elixir-cloud-aai/cloud-registry/releases>
[badge-url-license]: <https://www.apache.org/licenses/LICENSE-2.0>
[code-of-conduct]: <https://github.com/elixir-cloud-aai/.github/blob/main/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/.github/blob/main/CONTRIBUTING.md>
[deployment]: deployment/
[docs]: <https://cloud-registry.readthedocs.io/en/latest/>
[docs-contributing]: <https://cloud-registry.readthedocs.io/en/latest/contributing/>
