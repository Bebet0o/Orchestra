# Contributing to Orchestra

Thank you for helping improve Orchestra. Contributions should preserve the
project's explicit authority boundaries and leave behavior understandable from
code, tests, and public documentation.

## Development principles

- Keep operation local-first and operator-controlled.
- Prefer explicit, typed authority over names, tags, or incidental local state.
- Fail closed when persisted state, artifact identity, or recovery evidence is
  malformed or ambiguous.
- Use immutable OCI `repository@sha256:digest` identity for published worker
  artifacts. A local Docker image ID is not cross-host authority.
- Preserve historical integrity during migrations; do not rewrite old schema
  history to make current code simpler.
- Add or update tests with behavior changes.
- Keep secrets, runtime state, databases, generated caches, and local project
  registrations out of commits.

## Development setup

Clone the repository and work from a topic branch. The CI environment uses
Ubuntu 24.04 and installs `python3-yaml`, `rsync`, `sqlite3`, and `util-linux`
before running static validation. The public installer supports Debian 12+ or
Ubuntu 22.04+ on amd64; see
[public installation](docs/PUBLIC_INSTALLATION.md) for deployment requirements.

Run these commands from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  python3 -m unittest discover -s tests -p 'test_*.py'

PYTHONDONTWRITEBYTECODE=1 ./validate.sh --static --quiet
git diff --check
```

`./validate.sh` runs the repository's complete validation mode when the
required host/runtime services are available. Use `./validate.sh --help` to see
the supported modes.

## Branching

Use a focused topic branch. Clear prefixes include:

- `feat/...` for product behavior;
- `fix/...` for corrections;
- `docs/...` for documentation;
- `milestone/...` for a coordinated milestone branch.

Do not force-push shared branches without agreement from their collaborators.

## Making changes

Keep changes narrow enough to review and explain the user-visible behavior and
failure modes in the pull request.

- Add tests for new behavior, regressions, and fail-closed boundaries.
- Update OpenAPI, schemas, and their tests together when a public contract
  changes.
- Update Blueprint documentation, schema, examples, parser tests, and lifecycle
  tests together when the Blueprint contract changes.
- Keep the worker and reviewer on the shared environment materialization and
  sandbox verification path.
- Do not add mutable-tag or local-image-ID fallbacks for published environments.
- Update relevant public or operational documentation with behavior changes.

## Database migrations

Migrations are forward-only historical records.

- Add the next contiguous numbered migration; never edit a migration that has
  shipped or been used as an upgrade base.
- Preserve historical table data, source bytes, canonical bytes, hashes,
  request routes, integrity domains, and external identities unless the new
  migration explicitly and safely transforms them.
- Do not casually recompute persisted integrity data under current naming or
  current code rules.
- Make malformed, unsupported, or internally inconsistent input fail atomically.
- Test fresh creation, the real previous-version upgrade, restart/readback,
  linkage, rerun behavior, and expected failure cases.

The current schema version is 23. Treat that as repository state, not a reason
to make contributor guidance depend permanently on one version number.

## Console generated assets

Console source lives in `console/src/`; the committed deterministic build lives
in `console/dist/`. When source assets change, rebuild them with:

```bash
python3 scripts/hermesops-console-build.py build \
  --source console/src \
  --output console/dist
```

Static validation reconstructs the distribution and requires it to match the
committed output. Review source and generated changes together.

## Tests and validation

Choose focused tests while developing, then run the full unit suite and static
validation before submitting. Do not weaken unrelated tests to make a change
pass. If a contract test encodes obsolete behavior, update it only alongside a
well-supported change to that contract.

The secret scanner is also available directly:

```bash
./scripts/check-secrets.sh --root "$PWD"
```

## Commits and pull requests

Use an understandable imperative commit subject and keep unrelated cleanup out
of the same change. Pull requests should summarize:

- the behavior or contract changed;
- important authority, migration, security, and compatibility implications;
- tests and validation performed;
- intentionally deferred work.

There is no requirement to expose internal review ceremony in a public pull
request. The evidence should be sufficient for another contributor to evaluate
the change.

## Security issues

Do not disclose an unpatched vulnerability in a normal public issue or pull
request. Follow [SECURITY.md](SECURITY.md) for the currently available reporting
route.
