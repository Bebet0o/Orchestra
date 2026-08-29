# Orchestra worker OCI distribution

Slice 3A adds the publication and materialization architecture without
publishing or activating an image. The production worker and reviewer continue
to use the explicit legacy same-daemon preparation until the S3-B transaction.

The only runtime authority for a published image is the complete reference
`ghcr.io/bebet0o/orchestra-worker@sha256:<64 lowercase hex>`. Candidate tags are
discovery aids and must never be written to the environment lock or used for
sandbox execution.

The manual publication workflow builds and tests `linux/amd64`, pushes the
candidate, captures the registry-produced digest, and uploads a JSON publication
record. That generated record is evidence for S3-B; it is not source-controlled.

The workflow must be dispatched from its trusted `main` copy with both a full
repository branch ref and its exact lowercase 40-hex head commit. It checks out
the trusted publisher under `trusted/`, fetches the repository-owned branch into
a fixed local ref, requires exact branch-head equality, and checks the candidate
out detached under `candidate/`. The trusted helper then requires the requested,
fetched, checked-out, and OCI-revision identities to agree before it can create a
publication record. The candidate checkout never supplies authorization helpers
or the image checker.

For S3-B, bootstrap only this audited trusted publication foundation onto main,
preserve the candidate commit in repository history, push its branch, and invoke
the main workflow with inputs such as:

```text
candidate_ref=refs/heads/milestone/3a-distribution-foundation
candidate_sha=<exact candidate branch-head commit>
```

Before activation, an operator must first prove public distribution from a
genuinely empty, ephemeral DIND daemon. The trusted harness uses the same
digest-pinned DIND image as the installed sandbox engine, mounts no Docker
credentials or persistent image store, verifies the worker is initially absent,
and destroys the daemon afterward:

```sh
python3 .github/scripts/anonymous_worker_pull.py \
  --image 'ghcr.io/bebet0o/orchestra-worker@sha256:<real-digest>'
```

Then run the image contract against that exact published reference:

```sh
python3 scripts/check-worker-oci-image.py \
  --image 'ghcr.io/bebet0o/orchestra-worker@sha256:<real-digest>' \
  --expected-revision '<source-commit>' \
  --anonymous-pull
```

The contract command also uses a fresh temporary `DOCKER_CONFIG`, but that is
not a substitute for the clean-daemon proof above. S3-B must record
`GHCR_PACKAGE_PUBLIC=YES`, `ANONYMOUS_DIGEST_PULL=PASS`, and
`ANONYMOUS_PULL_FRESH_DAEMON=YES` before changing the default-worker lock to
`published`.
