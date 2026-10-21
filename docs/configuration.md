# Configuration

The app is configured by
[`cloud_registry/config.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml),
the base config, which documents its values. To override some of them, write
them to a config overlay file and list it in environment variable
`FOCA_CONFIG_OVERLAY` (several files separated by `:`); FOCA merges overlays
into the config on startup (see the [FOCA deployment
guide](https://foca.readthedocs.io/en/latest/deployment/)). The [Helm
chart](deployment.md) uses the same mechanism: it renders its `config` value
(in a values file per deployment) into an overlay. Overlays cannot change the
address the image listens on: `server.host` and `server.port` apply to the
development server only, not to Gunicorn (see [Server](#server)).

Each deployment must set two values that depend on it, otherwise the app
refuses to start:

- `security.auth.audience`: the client IDs of the apps whose LS Login tokens
  are accepted (e.g., the registry's front end; see
  [Authentication](access-control.md#authentication));
- `security.access_control.group_mapping`: the group(s) whose members are
  admins (see [Access control](access-control.md#admins)), or
  `custom.access_control.require_admin: false` to run without admins.

## Local overrides

Docker Compose mounts
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml),
an overlay for local development that accepts tokens for any audience and
requires no admins. To override more values locally, put them in
`config.local.yaml` and mount it in a `docker-compose.override.yaml` (both are
ignored by Git):

```yaml
services:
    cloud-registry:
        environment:
            FOCA_CONFIG_OVERLAY: /etc/cloud-registry/config.dev.yaml:/etc/cloud-registry/config.local.yaml
        volumes:
            - ./config.local.yaml:/etc/cloud-registry/config.local.yaml:ro
```

## Public URLs

Public URLs, e.g., in `Location` headers, are built from
`custom.endpoints.service` (scheme, host and port). The Helm chart sets them
from its `hostName` value; other deployments must set them, as the defaults
(`https://0.0.0.0:8080`) aren't reachable (the Docker Compose setup sets
`http://localhost:8080` in
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml)).
Set `api_path` to the path prefix in front of the API base path when the app
is served behind a reverse proxy under a subpath (e.g., `registry`), or leave
it empty. Previously, `api_path` held the API base path itself; see
[Upgrading](upgrading.md#public-urls).

## Request size limit

Request bodies are limited to `server.max_content_length` bytes (default:
1048576, i.e., 1 MiB), by
[FOCA](https://foca.readthedocs.io/en/latest/configuration/server/). Larger
requests are rejected with `413 Content Too Large` before the access token is
checked and the body is parsed: requests whose `Content-Length` exceeds the
limit are rejected without reading the body (and requests with an invalid
`Content-Length` with `400`). Bodies of unknown size (`Transfer-Encoding:
chunked`) are read up to one byte over the limit, and rejected if they are
larger. The app refuses to start with `null` (which would remove the limit in
FOCA).

Set the same limit at the ingress or reverse proxy in front of the app, so
that it rejects larger requests itself; the Helm chart sets the limit of
ingress-nginx from `server.max_content_length` (see
[Ingress](deployment.md#ingress)).

## Server

The image serves the app with [Gunicorn](https://gunicorn.org/) on port 8080,
with settings for a small container in
[`cloud_registry/gunicorn_config.py`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/gunicorn_config.py):
one worker process with 4 threads, workers restarted if their main loop
doesn't respond for 30 seconds (requests run in threads, so slow requests
don't count), 20 seconds to finish requests on shutdown, idle keep-alive
connections closed after 75 seconds, and Gunicorn's size limits of the request
line and headers. Keep the keep-alive timeout longer than that of a reverse
proxy in front of the app for its connections to the app (60 seconds for
ingress-nginx): otherwise, the proxy may reuse a connection just as Gunicorn
closes it, and the request fails (`502`). To change the settings, set
`GUNICORN_CMD_ARGS` to [Gunicorn
options](https://gunicorn.org/reference/settings/) (e.g., `--workers 2
--threads 8`); Docker Compose passes it on from the host, and the Helm chart
sets it from its `cloudRegistry.gunicorn` values.

Each worker loads the whole app (about 55 MiB of memory), with its own
database client, and runs the startup checks (e.g., of the service info and
the admin groups); if they fail, Gunicorn exits. Gunicorn always listens on
`0.0.0.0:8080` (its `bind` setting): `server.host` and `server.port` of the
config apply only to Flask's development server
(`python -m cloud_registry.app`), which is not for production, and changing
them does not move the image's traffic.

Serve the app behind a reverse proxy that buffers requests (e.g.,
ingress-nginx, as with the Helm chart): Gunicorn's threads read requests
themselves, so slow clients can tie them up.

## Service info

`GET /service-info` serves the service info from the config
(`service_info.service`), without the database; it is served by
[FOCA](https://foca.readthedocs.io/en/latest/configuration/service-info/). On
startup, the service info is checked against the GA4GH `Service` schema, and
the app refuses to start if it is invalid, naming the invalid field. `version`
defaults to the version of the installed app (`service_info.package`);
`createdAt` and `updatedAt` are served only if set, and must be RFC 3339
date-times (e.g., `2020-11-08T00:00:00Z`). The defaults describe the app, not a
deployment: deployments set their own `id` (unique, e.g., in reverse domain
name notation), `name`, `description`, `organization` and `contactUrl`. Fields
and list items set to `null` (e.g., in a config overlay) are not served. To
change the service info, change the config and restart the app (the Helm chart
restarts it when the `config` value changes). Previously, service info was set
via the API; see [Upgrading](upgrading.md#service-info).
