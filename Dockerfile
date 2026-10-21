##### BASE IMAGE #####
## Pinned by digest, so that rebuilds (e.g., of a release) use the same base
## image; Dependabot updates the tag and digest together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:0.14.0-py3.12@sha256:1c2ba7ea6a126d2be88c3f75c7b06a6cc6b4ac3b8b5590cd0df8460208e24da5

##### METADATA #####
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation for the ELIXIR Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Install dependencies first, so that code changes do not reinstall them;
## the FOCA image runs as non-root user
## TODO(#104): FOCA 0.15.0 is not released yet: `requirements.txt` installs its
## release candidate from Git (replacing the base image's FOCA 0.14.0), so Git
## is installed for the build and removed again; the build fails unless FOCA
## 0.15 (with `foca.api.service_info`) is installed. Once FOCA 0.15.0 is
## released, require it in `requirements.txt`, install the requirements with
## `pip` only and change the base image to a FOCA 0.15.0 image (if published)
USER root
COPY requirements.txt /tmp/src/
RUN apt-get update \
  && apt-get install -y --no-install-recommends git \
  && pip install --no-cache-dir -r /tmp/src/requirements.txt \
  && apt-get purge -y --auto-remove git \
  && rm -rf /var/lib/apt/lists/* \
  && python -c "import foca.api.service_info"

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
