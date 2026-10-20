##### BASE IMAGE #####
FROM elixircloud/foca:0.14.0-py3.12

##### METADATA #####
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation for the ELIXIR Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Install system packages and dependencies first, so that code changes do
## not reinstall them; the FOCA image runs as non-root user and has no curl or
## jq, which are needed by `update-config-map.sh` (as is yq; remove with #67)
USER root
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl jq \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir yq==3.2.3
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

## Copy app; it runs from `/app` rather than being installed, because the
## Helm chart mounts the config at `/app/cloud_registry/config.yaml`
COPY update-config-map.sh /app/
COPY cloud_registry /app/cloud_registry
USER 1000

WORKDIR /app
ENV PYTHONPATH=/app

## All API endpoints require authentication, so probe the Swagger UI instead
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ga4gh/registry/v1/ui/', timeout=4)"]

CMD ["python", "-m", "cloud_registry.app"]
