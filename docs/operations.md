# Operations

Tasks of the admins of a deployment (see [Admins](access-control.md#admins))
and the logs that record them. Admins also [verify
services](api.md#verified-services) and resolve conflicts over [duplicate
services](api.md#duplicate-services). Before deploying, operators can
[verify the images and charts](#verifying-images-and-charts).

## Audit logs

For auditing, the app logs (at level `INFO`) who changed which roles of whom
(users or groups) on which service, who deleted services, and who verified
services or removed their verifications (also by updating their URL or API
type). Role changes are logged by FOCA's ownership helpers (logger
`foca.security.access_control.ownership`), naming the service by its path,
e.g., `Role 'maintainer' on '/ga4gh/registry/v1/services/ABC123' granted to
user '<sub>' by user '<sub>' (previous roles: none)`; deletions and
verifications by the app (loggers `cloud_registry.*`).
Changes via the [permissions API](#permissions-api) are not logged by
the app itself: FOCA logs the requests (method, path and client address) and
the identifiers of the added, updated or deleted policy rules, but not who
changed them, or how.

## Permissions API

FOCA also serves its permissions API at `/admin/access-control/permissions`
(not part of the registry's API specification), which lists, adds, changes and
deletes the stored policy rules directly. Only admins can use it. Use the
registry's endpoints (see [Access control](access-control.md)) to manage roles
on services: the permissions API bypasses the registry's checks, e.g., it can
remove the last owner of a service (without `409 Conflict`) and leaves the
stored names of users (see `GET /services/{serviceId}/permissions`) in place.
The configured policies (`security.access_control.policies`) are managed on
startup and can't be changed via the API.

### More admins

Admins can make other users admins with it, by granting them the role `admin`
(a `g` rule for the user identifier with the prefix `user:`); `GET /me`
reports them as admins, too:

```bash
curl -X POST "http://localhost:8080/admin/access-control/permissions" \
  -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"policy_type": "g", "rule": {"v0": "user:<user-id>", "v1": "admin"}}'
```

The request returns the identifier of the rule; `DELETE` on
`/admin/access-control/permissions/<rule-id>` revokes it. Prefer the group
mapping for admins, as it is configured with the deployment; roles granted via
the API are stored in the database. The API is served like the registry's
endpoints (e.g., the Helm chart's Ingress routes all paths); to keep it off
the internet, route only `/ga4gh/registry/v1/` through a reverse proxy (see
[Ingress](deployment.md#ingress) for the Helm chart).

## Verifying images and charts

From version 0.3.0 on, images on Docker Hub (`elixircloud/cloud-registry`)
are built by this repository's workflows on GitHub Actions, with an SBOM and
the build provenance attached as attestations, a [GitHub artifact
attestation](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
of their provenance, and a keyless [Sigstore](https://www.sigstore.dev/)
signature. To check that an image was built by the release workflow, run
(with the [GitHub CLI](https://cli.github.com/) or
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3 or
later, which reads the signatures in the Sigstore bundle format):

```bash
gh attestation verify oci://docker.io/elixircloud/cloud-registry:0.3.0 \
    --repo elixir-cloud-aai/cloud-registry \
    --signer-workflow elixir-cloud-aai/cloud-registry/.github/workflows/release.yaml \
    --source-ref refs/tags/v0.3.0
cosign verify docker.io/elixircloud/cloud-registry:0.3.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity https://github.com/elixir-cloud-aai/cloud-registry/.github/workflows/release.yaml@refs/tags/v0.3.0
```

Replace `0.3.0` with the version to verify. `--source-ref` and the ref in
`--certificate-identity` check that the workflow ran for the release tag,
not for another branch. Images of commits on `dev`
(`dev`, `sha-<commit>`) are signed and attested by the CI workflow instead:
use `--signer-workflow
elixir-cloud-aai/cloud-registry/.github/workflows/checks.yaml`,
`--source-ref refs/heads/dev` and `--certificate-identity
https://github.com/elixir-cloud-aai/cloud-registry/.github/workflows/checks.yaml@refs/heads/dev`.
To show an image's SBOM or
provenance, run:

```bash
docker buildx imagetools inspect docker.io/elixircloud/cloud-registry:0.3.0 \
    --format '{{ json .SBOM }}'
docker buildx imagetools inspect docker.io/elixircloud/cloud-registry:0.3.0 \
    --format '{{ json .Provenance }}'
```

The Helm chart attached to a release (`cloud-registry-0.3.0.tgz`) has a
GitHub artifact attestation of its provenance and a Sigstore bundle with its
signature (`cloud-registry-0.3.0.tgz.sigstore.json`, attached too):

```bash
gh release download v0.3.0 --repo elixir-cloud-aai/cloud-registry \
    --pattern 'cloud-registry-0.3.0.tgz*'
gh attestation verify cloud-registry-0.3.0.tgz \
    --repo elixir-cloud-aai/cloud-registry \
    --signer-workflow elixir-cloud-aai/cloud-registry/.github/workflows/release.yaml \
    --source-ref refs/tags/v0.3.0
cosign verify-blob cloud-registry-0.3.0.tgz \
    --bundle cloud-registry-0.3.0.tgz.sigstore.json \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity https://github.com/elixir-cloud-aai/cloud-registry/.github/workflows/release.yaml@refs/tags/v0.3.0
```

Images are scanned for known vulnerabilities with [Trivy](https://trivy.dev/)
in CI and again before a release is published (both fail on fixable critical
and high vulnerabilities), and the `latest` image weekly. The repository's
code scanning alerts show the findings that have a fix, which the maintainers
can act on; findings without a fix (often OS packages of the base image)
appear there once a fix is released. The weekly scan also keeps a full report,
those findings included, as a workflow artifact of each run. From version
0.3.0 on, the published images (releases and `dev`) are the scanned ones: the
workflows push each image by digest, untagged, scan exactly that digest, and
only if the scan passes (no fixable critical or high vulnerabilities, except
accepted ones) sign and tag it.
