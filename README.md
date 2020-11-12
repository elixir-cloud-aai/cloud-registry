# ELIXIR Cloud Service Registry

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]

## Synopsis

GA4GH Service Registry API implementation for the ELIXIR Cloud.

## Description

Service entries comply with the [`ExternalService` schema][schema-service]
defined in the [GA4GH Service Registry API][ga4gh-registry].

## Usage

Once [deployed](#Installation), the API will be served here:

```text
http://localhost:8080/ga4gh/registry/v1/
```

You can explore the service via the [Swagger UI][url-swagger].

> Note that host and port can be adapted in the [`docker-compose`
> configuration][config]. In that case, the values in the URLs above need to be
> updated accordingly.

[Endpoints][schema-endpoints] can be probed with `curl`, for example:

```bash
curl -X GET "http://localhost:8080/ga4gh/registry/v1/services" -H  "accept: application/json"
```

## Installation

To quickly install the service for development/testing purposes, we recommend
deployment via [`docker-compose`][docker-compose], as described below. For
more durable deployments on cloud native infrastructure, we also provide a
[Helm][helm] chart in the [`deployments/`][deployment] directory, together with
[basic deployment instructions][deployment-instructions] (details may need to
be adapted for your specific infrastructure).

### Requirements

The following software needs to be available on your system:

- [`git 2.17.1`][git]
- [`docker 18.09.6`][docker]
- [`docker-compose 1.23.1`][docker-compose]

> Versions used to test deployment are indicated. Other versions, especially
> newer ones, will likely work as well.

### Deployment

First, clone the repository and traverse into the root directory with:

```bash
git clone git@github.com:elixir-cloud-aai/cloud-registry.git
cd cloud-registry
```

The simply start up the service with:

```bash
docker-compose up --build -d
```

_**That't it!**_ You should now be able to use/explore the API as described in
the [usage section](#Usage).

#### Other useful commands

To shut down the service, run:

```bash
docker-compose down
```

If you need to inspect the logs, call:

```bash
docker-compose logs
```

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][contributing] if you are interested to contribute. Please mind the
[code of conduct][coc] for all interactions with the community.

## Versioning

The project adopts the [semantic versioning][semver] scheme for versioning.
Currently the service is in beta stage, so the API may change without further
notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][elixir-cloud]. Follow the link to get in touch with us via chat or email.
Please mention the name of this service for any inquiry, proposal, question
etc.

[badge-build-status]:<https://travis-ci.com/elixir-cloud-aai/cloud-registry.svg?branch=dev>
[badge-coverage]:<https://img.shields.io/coveralls/github/elixir-cloud-aai/cloud-registry>
[badge-github-tag]:<https://img.shields.io/github/v/tag/elixir-cloud-aai/cloud-registry?color=C39BD3>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]:<https://travis-ci.com/elixir-cloud-aai/cloud-registry>
[badge-url-coverage]:<https://coveralls.io/github/elixir-cloud-aai/cloud-registry>
[badge-url-github-tag]:<https://github.com/elixir-cloud-aai/cloud-registry/releases>
[badge-url-license]:<http://www.apache.org/licenses/LICENSE-2.0>
[ga4gh-registry]: <https://github.com/ga4gh-discovery/ga4gh-service-registry>
[config]: docker-compose.yaml
[deployment]: deployment/
[deployment-instructions]: deployment/README.md
[docker]: <https://docs.docker.com/get-docker/>
[docker-compose]: <https://docs.docker.com/compose/install/>
[git]: <https://git-scm.com/book/en/v2/Getting-Started-Installing-Git>
[helm]: <https://helm.sh/>
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[semver]: <https://semver.org/>
[schema-service]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158>
[schema-endpoints]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L16>
[url-swagger]: <http://localhost:8080/ga4gh/registry/v1/ui>
