# Orchestra Blueprint v1 Specification

Status: **Executable source contract**

API version: `hermesops.dev/v1`

Kind: `SandboxProfile`

Machine schema:
[`../../specs/blueprint-v1.schema.json`](../../specs/blueprint-v1.schema.json)

## Purpose

A Blueprint is one strict YAML source file describing a reproducibility-oriented
Orchestra sandbox profile.

A Blueprint is **not a project configuration**. Project identity, repository
paths, Git policy, task concurrency, role definitions, objective scheduling and
review requirements remain in their existing Controller, project-registry,
role and policy contracts.

The current implementation makes source parsing, validation, canonicalization,
fingerprinting, persistence, and bounded lifecycle operations executable. It
does not build or activate images from a Blueprint.

```text
Blueprint
    ↓ strict YAML parsing
validated SandboxProfile
    ↓ deterministic canonicalization
canonical JSON + canonical SHA-256
    ↓ later sandbox-builder milestone
immutable image build and validation
```

## Conventional file names

The conventional source name is:

```text
Blueprint
```

Alternative profiles may use names such as:

```text
Blueprint.python
Blueprint.cpp
Blueprint.docs
```

The file name does not change the schema or grant permissions.

## Top-level structure

```yaml
apiVersion: hermesops.dev/v1
kind: SandboxProfile
metadata:
  name: default-python
spec:
  base: {}
  build: {}
  workspace: {}
  runtime: {}
  network: {}
  security: {}
  mounts: []
  validation: {}
```

Unknown fields are rejected.

## Parsing boundary

Blueprint v1 uses YAML data compatible with the YAML 1.2 core data model.

The parser:

- accepts exactly one UTF-8 YAML document;
- rejects a UTF-8 BOM;
- rejects aliases, anchors and merge keys;
- rejects duplicate mapping keys;
- requires string mapping keys;
- rejects non-finite numbers and unsupported YAML object types;
- bounds source bytes, line count, nesting depth, node count, scalar size and
  diagnostics;
- never evaluates tags or executes source content;
- never includes rejected scalar values in diagnostics.

YAML 1.1 words such as `on`, `off`, `yes` and `no` remain strings. Only
`true` and `false` are booleans.

## `apiVersion`

Required exact value:

```yaml
apiVersion: hermesops.dev/v1
```

`v0alpha1` remains a historical experimental design contract and is not
silently rewritten. Migration must create a new source revision.

## `kind`

Required exact value:

```yaml
kind: SandboxProfile
```

No project, role, objective or orchestration kinds are accepted by this schema.

## Metadata

`metadata.name` is the immutable logical profile name after the first successful
build.

It must be lowercase ASCII, may contain hyphens, and is limited to 63
characters.

Optional display metadata:

```yaml
metadata:
  name: python-project
  displayName: Python Project Worker
  description: Reproducible Python sandbox
  labels:
    language: python
    purpose: general
```

Labels are operator metadata only. They grant no permissions.

## Base image

```yaml
spec:
  base:
    registry: docker.io
    image: library/python
    tag: 3.12.10-slim-bookworm
    digest: sha256:...
```

The OCI digest is mandatory and authoritative. A mutable tag is optional and
informational.

The registry value cannot contain a URL scheme, embedded credentials, path or
secret-like value. Registry allow/deny policy remains external to the
Blueprint.

## Build declaration

Supported declarative sections are:

```yaml
build:
  apt:
    update: true
    packages: []
  python:
    interpreter: python3
    packages: []
  node:
    packageManager: npm
    packages: []
  environment: {}
  steps: []
```

Package entries are bounded and unique. Unpinned package declarations produce a
warning because the final resolution record must retain the exact resolved
version.

Build environment values must be non-secret strings. Secret-like environment
names, interpolation markers, credential assignments, private-key material and
URLs with embedded credentials are rejected.

### Command vectors

Build and validation commands are argument vectors:

```yaml
run: [python3, --version]
```

They are not shell strings. Shell executables such as `sh`, `bash`, PowerShell
or `cmd` are rejected, including shell dispatch through `env` or `busybox`.

Blueprint v1 does not provide host command execution.

## Workspace

```yaml
workspace:
  user: hermes
  group: hermes
  directory: /workspace
  sourceMode: worktree
```

The sandbox identity cannot be root.

Supported source modes:

```text
worktree
clone
readOnly
```

The directory is a canonical absolute container path. Host source paths are not
accepted.

Project policy may narrow the source modes accepted for a particular role or
project.

## Runtime bounds

```yaml
runtime:
  cpu: 4
  memory: 8GiB
  pids: 512
  timeout: 2h
  stopGracePeriod: 30s
  tmpfsSize: 1GiB
```

CPU, memory, PID and duration values are structurally bounded. Global and
project policies may impose lower limits.

Concurrency is intentionally absent. One image may back several containers,
but a Blueprint never authorizes parallel writers.

## Network

```yaml
network:
  build:
    mode: allowlist
    allow:
      - deb.debian.org
      - pypi.org
  runtime:
    mode: none
    allow: []
```

Modes:

```text
none
allowlist
full
```

Destinations must be credential-free DNS names or CIDRs. URLs, user information,
query strings and secret-like data are rejected.

`full` is syntactically valid but produces a warning and always remains subject
to external policy and confirmation.

The declaration alone does not enforce network policy. The dedicated sandbox
infrastructure remains responsible for enforcement.

## Security invariants

Required values:

```yaml
security:
  privileged: false
  noNewPrivileges: true
  readOnlyRoot: false
  capabilities:
    drop: [ALL]
    add: []
  seccompProfile: default
  secrets: false
  allowDockerSocket: false
  allowDeviceAccess: false
```

Blueprint v1 cannot weaken these invariants.

A Blueprint does not contain secret values and does not contain secret
references. Runtime secret binding remains a separate future Controller
contract.

The schema rejects:

- privileged containers;
- added Linux capabilities;
- Docker socket access;
- hardware device access;
- build-time and runtime secret declarations;
- arbitrary host mounts.

## Logical mounts

Supported logical types:

```text
workspace
cache
tmpfs
artifact
```

A mount declares only a logical type and a container target. It cannot provide a
host source path.

Targets:

- are canonical safe absolute container paths;
- cannot cover protected system paths;
- must be unique and non-overlapping;
- cannot overlap the workspace unless the mount is the single explicit
  workspace mount;
- require a bounded size for `tmpfs`.

The Controller or sandbox manager resolves logical storage under approved roots.

## Validation commands

A profile must include at least one validation command:

```yaml
validation:
  commands:
    - name: python
      run: [python3, --version]
      timeout: 30s
      expectExitCode: 0
```

The current source validator validates this declaration only. Running these
commands belongs to the separate sandbox-builder lifecycle.

## Canonicalization

After successful validation, Orchestra:

1. applies explicit defaults;
2. normalizes CPU integers;
3. normalizes IEC quantities to the largest exact unit;
4. normalizes durations to the largest exact unit;
5. sorts mapping keys during serialization;
6. preserves array order;
7. serializes UTF-8 canonical JSON without insignificant whitespace;
8. computes a SHA-256 over the canonical bytes.

The result records:

```text
source_sha256
canonical_sha256
canonical_size
api_version
source_format
profile name
diagnostics
```

Comments, source formatting, mapping order and equivalent duration or quantity
spellings do not change the canonical SHA-256. Array order remains significant.

The source SHA-256 and canonical SHA-256 serve different purposes and are both
retained.

## Diagnostics

Diagnostics are bounded objects:

```json
{
  "severity": "error",
  "code": "security_invariant_violation",
  "path": "/spec/security/privileged",
  "message": "This security value cannot be weakened.",
  "documentation": "docs/blueprint/SPECIFICATION_V1.md"
}
```

Severities:

```text
info
warning
error
```

Errors block canonicalization. Warnings remain visible to later policy and build
stages.

Diagnostics do not include raw secret-like values, source snippets, host paths
or stack traces.

## CLI

```bash
scripts/orchestra-blueprint.py validate Blueprint
scripts/orchestra-blueprint.py validate Blueprint --json
scripts/orchestra-blueprint.py fingerprint Blueprint --json
scripts/orchestra-blueprint.py canonicalize Blueprint
scripts/orchestra-blueprint.py canonicalize Blueprint --output canonical.json
```

Input paths must be regular files and cannot be symlinks.

Canonical output replacement requires `--force`.

## Current implementation boundary

Implemented:

- v1 JSON Schema;
- strict YAML parser;
- semantic validation;
- deterministic canonicalization;
- source and canonical SHA-256;
- bounded safe diagnostics;
- CLI and example source;
- immutable source revisions in SQLite;
- authenticated Controller validation and lifecycle endpoints;
- Console validation, editing, history, and comparison;
- regression and adversarial tests.

Not implemented:

- build-plan compilation;
- package resolution;
- image build;
- validation-container execution;
- profile activation;
- rollback;
- runtime secret binding.

Those capabilities remain separate lifecycle concerns.
