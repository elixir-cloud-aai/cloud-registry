##### BASE IMAGE #####
FROM elixircloud/foca:20221107-py3.10

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

CMD ["bash", "-c", "cd /app/cloud_registry; python app.py"]
