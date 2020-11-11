# ELIXIR Cloud Service Registry

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]

## Synopsis

GA4GH Service Registry API implementation for the ELIXIR Cloud.

## Description
Service entries have to comply with the [GA4GH schema](https://raw.githubusercontent.com/ga4gh-discovery/ga4gh-service-info/v1.0.0/service-info.yaml#/components/schemas/Service)

## Usage

Once deployed and started ([see below](#Deployment)), the service is available at:  
<http://localhost:8080/ga4gh/registry/v1/>

You can explore the service via the Swagger UI:

```bash
firefox http://localhost:8080/ga4gh/registry/v1/ui/
```

> Note that host and port can be set manually in the [config] file. In that
> case, the values in the URLs above need to be replaced as well.

Endpoints can be probed with `curl`, for example:   
```bash
curl -X GET "http://localhost:8080/ga4gh/registry/v1/services" -H  "accept: application/json"
``` 

## Deployment

`cloud-registry` can be deployed via containers.    
The repository first needs to be cloned with:

```bash
git clone git@github.com:elixir-cloud-aai/cloud-registry.git
```

Afterwards traverse to the repository's root directory:

```bash
cd cloud-registry
```

### Containerized Deployment

> "Production-like" containerized deployment without HTTP server/load balancer
> etc.

#### Requirements (containerized deployment)

- [Git] (tested with version 2.17.1)
- [Docker] (tested with version 18.09.6)
- [docker-compose] (tested with version 1.24.0)

#### Building & starting the service

```bash
# Build application image
# [NOTE] Image re-building is not always necessary. Inspect the `Dockerfile`
#        to check which changes will need re-building.
docker-compose build
# Start service
docker-compose up -d
```

#### Other useful commands

```bash
# Check logs
docker-compose logs
# Shut down service
docker-compose down
```

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][res-elixir-cloud-contributing] if you are interested to contribute.
Please mind the [code of conduct][res-elixir-cloud-coc] for all interactions
with the community.

## Versioning

The project adopts the [semantic versioning][res-semver] scheme for versioning.
Currently the service is in beta stage, so the API may change without further
notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][org-elixir-cloud]. Follow the link to get in touch with us via chat or
email. Please mention the name of this service for any inquiry, proposal,
question etc.

[badge-build-status]:<https://travis-ci.com/elixir-cloud-aai/cloud-registry.svg?branch=dev>
[badge-coverage]:<https://img.shields.io/coveralls/github/elixir-cloud-aai/cloud-registry>
[badge-github-tag]:<https://img.shields.io/github/v/tag/elixir-cloud-aai/cloud-registry?color=C39BD3>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]:<https://travis-ci.com/elixir-cloud-aai/cloud-registry>
[badge-url-coverage]:<https://coveralls.io/github/elixir-cloud-aai/cloud-registry>
[badge-url-github-tag]:<https://github.com/elixir-cloud-aai/cloud-registry/releases>
[badge-url-license]:<http://www.apache.org/licenses/LICENSE-2.0>
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-semver]: <https://semver.org/>
