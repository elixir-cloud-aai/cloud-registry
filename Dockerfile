##### TOOLS #####
## uv, to export the locked requirements (`uv.lock`) for the image build;
## mounted into the steps that export them only, so that the image doesn't
## contain it. Pinned by digest; Dependabot updates the tag and digest
## together. CI's `push` job exports the build requirements with this image
## too (`.github/workflows/ci.yml`), as the `build` stage below does
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

##### BASE IMAGE #####
## Pinned by digest, so that rebuilds (e.g., of a release) use the same base
## image; Dependabot updates the tag and digest together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:0.18.0-py3.13@sha256:dd8dd927d8d5e7ccb616d441c8db7a5e2c0b24c52a3329f484f6edac082b7a26 AS base

##### BUILD #####
## Build the app's wheel, in a stage of its own, so that the image doesn't
## contain the build requirements. They (`build-system.requires` of
## `pyproject.toml`) are locked in `uv.lock`, as the dependency group
## `build-system`, and installed from there, as the runtime dependencies are
## below: at the exact versions of the lock, checked against its hashes, as
## wheels only, into a virtual environment of their own (with the pip of the
## Python of the base image, `ensurepip`). The wheel is then built with them,
## without an isolated build environment (which pip would fill with
## downloads that aren't hash-checked), without an index and without
## network, so that it downloads nothing; `--check-build-dependencies` fails
## it unless they satisfy `build-system.requires`
FROM base AS build
USER root
COPY pyproject.toml uv.lock /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  UV_NO_CACHE=1 uv export --frozen --only-group build-system --no-emit-project \
    --project /tmp/src --output-file /tmp/build-requirements.txt \
  && python -m venv /tmp/build-env \
  && /tmp/build-env/bin/pip install --no-cache-dir --require-hashes \
    --only-binary :all: --no-deps -r /tmp/build-requirements.txt
## (`README.md` is the package's long description; the license text is
## installed with the package metadata, e.g.,
## `site-packages/cloud_registry-<version>.dist-info/licenses/LICENSE`)
## The build context has no Git metadata, so the version is passed as a build
## argument (e.g., `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION=1.2.3`, as the
## workflows do); without it, the app reports version `0.0.0.dev0`
COPY README.md LICENSE /tmp/src/
COPY cloud_registry /tmp/src/cloud_registry
ARG SETUPTOOLS_SCM_PRETEND_VERSION
RUN --network=none \
  /tmp/build-env/bin/pip wheel --no-cache-dir --no-index --no-deps \
    --no-build-isolation --check-build-dependencies --wheel-dir /tmp/dist \
    /tmp/src

##### IMAGE #####
FROM base

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

## Install app from the wheel of the build stage (non-editable; the base
## config is installed with the package, and deployments override it via
## `FOCA_CONFIG_OVERLAY`), without its dependencies, installed above, and
## without an index and without network, so that nothing is downloaded
RUN --network=none --mount=from=build,source=/tmp/dist,target=/tmp/dist \
  pip install --no-cache-dir --no-index --no-deps /tmp/dist/*.whl \
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
