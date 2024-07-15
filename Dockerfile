##### BASE IMAGE #####
FROM elixircloud/foca:20240711-py3.12

##### METADATA ##### 
LABEL software="Cloud Registry"
LABEL software.description="GA4GH Service Registry API implementation for the ELIXIR Cloud"
LABEL software.website="https://github.com/elixir-cloud-aai/cloud-registry"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Copy app files
COPY ./ /app

## Install app
RUN cd /app \
  && pip install -e . \
  && cd / \
  && chmod g+w /app/cloud_registry/api/ \
  && pip install yq

WORKDIR /app

CMD ["python", "cloud_registry/app.py"]
