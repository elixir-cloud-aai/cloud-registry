##### BASE IMAGE #####
FROM elixircloud/foca:0.14.0-py3.12

##### METADATA #####
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation for the ELIXIR Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Copy app files
COPY ./ /app

## Install app; the FOCA image runs as non-root user and has no curl or jq,
## which are needed by `update-config-map.sh` (as is yq; remove with #67)
USER root
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl jq \
  && rm -rf /var/lib/apt/lists/* \
  && cd /app \
  && pip install --no-cache-dir -e . \
  && cd / \
  && pip install --no-cache-dir yq==3.2.3
USER 1000

WORKDIR /app

CMD ["python", "-m", "cloud_registry.app"]
