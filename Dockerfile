##### BASE IMAGE #####
FROM elixircloud/foca:0.14.0-py3.12

##### METADATA #####
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation for the ELIXIR Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Install dependencies first, so that code changes do not reinstall them;
## the FOCA image runs as non-root user
USER root
COPY requirements.txt /tmp/src/
RUN pip install --no-cache-dir -r /tmp/src/requirements.txt

## Install app (non-editable; the base config is installed with the package,
## and deployments override it via `FOCA_CONFIG_OVERLAY`)
## (`README.md` is the package's long description)
## The build context has no Git metadata, so the version is passed as a build
## argument (e.g., `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION=0.3.0`, as the
## workflows do); without it, the app reports version `0.0.0.dev0`
COPY pyproject.toml README.md /tmp/src/
COPY cloud_registry /tmp/src/cloud_registry
ARG SETUPTOOLS_SCM_PRETEND_VERSION
RUN pip install --no-cache-dir --no-deps /tmp/src \
  && rm -rf /tmp/src
USER 1000

## Probe the Swagger UI, which needs neither authentication nor the database;
## set `HEALTHCHECK_URL` if the config changes the port, the API base path or
## the Swagger UI setting (`server.port`, `api.specs[]`)
ENV HEALTHCHECK_URL=http://localhost:8080/ga4gh/registry/v1/ui/
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ['HEALTHCHECK_URL'], timeout=4)"]

CMD ["python", "-m", "cloud_registry.app"]
