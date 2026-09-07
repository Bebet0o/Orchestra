# Release manifest authority

Orchestra release manifests use `specs/release-manifest-v1.schema.json`. The
manifest binds one source revision and one `linux/amd64` platform to the exact
application, private-runtime, and accepted worker OCI digests.

`config/releases/v0.2.0.manifest.template.json` intentionally contains `null`
application and runtime digests. It is not installation authority. The final
accepted authority is tracked as `config/releases/v0.2.0.manifest.json` so the
standalone release installer can resolve it from the immutable source tag. The
trusted candidate publisher creates a provisional manifest only after both validated
local images have been pushed and bound to registry RepoDigests. The separate
acceptance workflow creates an accepted manifest only after one fresh,
credential-free DIND daemon pulls and validates both immutable references.

An installer accepts only an `accepted` manifest promoted to `v0.2.0`. The
trusted promotion workflow verifies that the input artifact came from a
successful `accept-official-images` run, reauthorizes the exact candidate
branch/SHA binding, validates the accepted manifest and anonymous-pull proof,
and changes only the manifest version. The installer then verifies the fixed
repositories, exact digest/reference agreement, source revision, platform, and
the already accepted worker digest before invoking the canonical two-service
Compose definition.

Candidate tags use `candidate-<40-hex-source-SHA>` for both repositories. Tags
are navigation aids; only the `repository@sha256:digest` values in an accepted
manifest are runtime authority.
