##### TOOLS #####
## uv, to export the locked requirements (`uv.lock`) for the image build;
## mounted into the build step only, so that the image doesn't contain it.
## Pinned by digest; Dependabot updates the tag and digest together
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

##### BASE IMAGE #####
## Pinned by digest, so that rebuilds (e.g., of a release) use the same base
## image; Dependabot updates the tag and digest together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:0.18.0-py3.13@sha256:dd8dd927d8d5e7ccb616d441c8db7a5e2c0b24c52a3329f484f6edac082b7a26

##### METADATA #####
## OCI annotations; images built from the FOCA image inherit its own, so all
## static ones are set here. CI's `docker/metadata-action` sets the version,
## revision and creation date and overrides title, description, URLs and
## licenses with the repository's metadata
LABEL org.opencontainers.image.title="Cloud Registry"
LABEL org.opencontainers.image.description="GA4GH Service Registry API implementation by ELIXIR On-Cloud"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL org.opencontainers.image.documentation="https://cloud-registry.readthedocs.io/"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Install dependencies first, so that code changes do not reinstall them;
## the FOCA image runs as non-root user. Only the locked ones are installed:
## the runtime dependencies (`project.dependencies` of `pyproject.toml`; the
## export leaves out the extras, and `--no-dev` a `dev` dependency group) and
## all their own dependencies, at the exact versions of `uv.lock`, checked
## against its hashes, as wheels only, and without resolving anything else
## (`--no-deps`). `--frozen` takes the lock as is, unchecked against
## `pyproject.toml` (CI's `uv lock --check` checks it). A direct reference to
## a FOCA commit (temporary) has no hash and is left out, as the image has no
## Git: the build fails unless the base image has FOCA of that commit. It also
## fails unless the base image has the locked FOCA release, if one is locked
## (`scripts/image_requirements.py`)
USER root
COPY pyproject.toml uv.lock scripts/image_requirements.py /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  UV_NO_CACHE=1 uv export --frozen --no-dev --no-emit-project \
    --project /tmp/src --output-file /tmp/src/locked.txt \
  && python /tmp/src/image_requirements.py /tmp/src/locked.txt > /tmp/src/requirements.txt \
  && pip install --no-cache-dir --require-hashes --only-binary :all: --no-deps \
    -r /tmp/src/requirements.txt

## Install app (non-editable; the base config is installed with the package,
## and deployments override it via `FOCA_CONFIG_OVERLAY`)
## (`README.md` is the package's long description; the license text is
## installed with the package metadata, e.g.,
## `site-packages/cloud_registry-<version>.dist-info/licenses/LICENSE`)
## The build context has no Git metadata, so the version is passed as a build
## argument (e.g., `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION=1.2.3`, as the
## workflows do); without it, the app reports version `0.0.0.dev0`
COPY pyproject.toml README.md LICENSE /tmp/src/
COPY cloud_registry /tmp/src/cloud_registry
ARG SETUPTOOLS_SCM_PRETEND_VERSION
RUN pip install --no-cache-dir --no-deps /tmp/src \
  && rm -rf /tmp/src
USER 1000

## Probe the Swagger UI, which needs neither authentication nor the database;
## set `HEALTHCHECK_URL` if Gunicorn's `--bind` changes the port, or the config
## changes the API base path or the Swagger UI setting (`api.specs.registry`)
ENV HEALTHCHECK_URL=http://localhost:8080/ga4gh/registry/v1/ui/
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ['HEALTHCHECK_URL'], timeout=4)"]

## Serve with Gunicorn (FOCA's settings for containers, `foca.gunicorn_config`;
## override them via `GUNICORN_CMD_ARGS`, e.g., `--workers 2`); each worker
## creates the app (no preloading) and runs its startup checks, and Gunicorn
## exits if they fail. `python -m cloud_registry.app` runs Flask's development
## server instead
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", \
  "cloud_registry.app:create_app()"]
