# ELIXIR Cloud service registry

[![License][badge-license]][badge-url-license]
[![Docs][badge-docs]][badge-url-docs]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]

<!-- --8<-- [start:synopsis] -->
GA4GH Service Registry API implementation for the ELIXIR Cloud. The ELIXIR
Cloud registry, with its [web front
end](https://github.com/elixir-cloud-aai/cloud-registry-ui), is deployed at
[https://registry.elixir-on.cloud/](https://registry.elixir-on.cloud/).

Service entries comply with the [`ExternalService`
schema](https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158)
defined in the [GA4GH Service Registry
API](https://github.com/ga4gh-discovery/ga4gh-service-registry). Anyone can
read them; users logged in with [LS
Login](https://lifescience-ri.eu/ls-login/) can register services and manage
those they own.
<!-- --8<-- [end:synopsis] -->

**Read the [documentation][docs]** for the configuration, the API, access
control, the deployment with the Helm chart in [`deployment/`][deployment],
operations, upgrades and development.

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

[badge-build-status]:<https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/checks.yaml/badge.svg>
[badge-coverage]:<https://codecov.io/gh/elixir-cloud-aai/cloud-registry/branch/dev/graph/badge.svg>
[badge-docs]: <https://readthedocs.org/projects/cloud-registry/badge/?version=latest>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]:<https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/checks.yaml>
[badge-url-coverage]:<https://codecov.io/gh/elixir-cloud-aai/cloud-registry>
[badge-url-docs]: <https://cloud-registry.readthedocs.io/en/latest/?badge=latest>
[badge-url-license]:<https://www.apache.org/licenses/LICENSE-2.0>
[deployment]: deployment/
[docs]: <https://cloud-registry.readthedocs.io/en/latest/>
