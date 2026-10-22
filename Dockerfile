##### BASE IMAGE #####
## Pinned by digest, so that rebuilds (e.g., of a release) use the same base
## image; Dependabot updates the tag and digest together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:0.16.0-py3.13@sha256:b401fbd4c56a6978118dca85dacf88cf5b8c17593b03a302537b9b48cb17eeef

##### METADATA #####
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation by ELIXIR On-Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR On-Cloud"

## Install dependencies (`project.dependencies` of `pyproject.toml`, read
## with `tomllib` of the base image's Python, 3.11 or later) first, so that
## code changes do not reinstall them; the FOCA image runs as non-root user
USER root
COPY pyproject.toml /tmp/src/
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/src/pyproject.toml', 'rb'))['project']['dependencies']))" \
    > /tmp/src/requirements.txt \
  && pip install --no-cache-dir -r /tmp/src/requirements.txt

## Install app (non-editable; the base config is installed with the package,
## and deployments override it via `FOCA_CONFIG_OVERLAY`)
## (`README.md` is the package's long description; the license text is
## installed with the package metadata, e.g.,
## `site-packages/cloud_registry-<version>.dist-info/licenses/LICENSE`)
## The build context has no Git metadata, so the version is passed as a build
## argument (e.g., `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION=0.3.0`, as the
## workflows do); without it, the app reports version `0.0.0.dev0`
COPY pyproject.toml README.md LICENSE /tmp/src/
COPY cloud_registry /tmp/src/cloud_registry
ARG SETUPTOOLS_SCM_PRETEND_VERSION
RUN pip install --no-cache-dir --no-deps /tmp/src \
  && rm -rf /tmp/src
USER 1000

## Probe the Swagger UI, which needs neither authentication nor the database;
## set `HEALTHCHECK_URL` if Gunicorn's `--bind` changes the port, or the config
## changes the API base path or the Swagger UI setting (`api.specs[]`)
ENV HEALTHCHECK_URL=http://localhost:8080/ga4gh/registry/v1/ui/
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ['HEALTHCHECK_URL'], timeout=4)"]

## Serve with Gunicorn (settings in `cloud_registry/gunicorn_config.py`;
## override them via `GUNICORN_CMD_ARGS`, e.g., `--workers 2`); each worker
## creates the app (no preloading) and runs its startup checks, and Gunicorn
## exits if they fail. `python -m cloud_registry.app` runs Flask's development
## server instead
CMD ["gunicorn", "--config", "python:cloud_registry.gunicorn_config", \
  "cloud_registry.app:create_app()"]
