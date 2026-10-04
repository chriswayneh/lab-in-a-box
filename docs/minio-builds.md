# MinIO builds

MinIO withdrew its public community images. Lab in a Box builds the same server/client from
pinned official source. No registry login or host Go installation is required.

## Install and update

Run the normal docker compose up -d or make up. The two MinIO services use pull_policy: build,
so Compose builds before startup and reuses verified build layers on later runs.
The first build needs internet access, extra disk space and several minutes of CPU time.
BuildKit and Linux containers are required; Docker Desktop supplies both.

To fetch/build without starting services:

```bash
docker compose pull --ignore-buildable
docker compose build minio minio-init
```

Make pull and make update use the same commands. Ordinary upstream image pull failures are
reported instead of ignored. Existing minio_data, credentials, buckets, policies, versioning,
lifecycle rules and routes are preserved. Back up valuable data before upgrades; no reset is needed.

## Verified inputs

The input manifest at images/minio/inputs.json records official source commits, archive SHA-256
values and multi-architecture Go/Alpine image digests. The Dockerfile verifies archives before
extracting/building. Go uses checked-in dependency manifests with targeted security fixes, verifies modules and disables automatic
toolchain downloads. Both binaries are rebuilt with the pinned current toolchain.

The server is [RELEASE.2025-10-15T17-29-55Z](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z),
which fixes session-policy privilege escalation. The client uses official
[RELEASE.2025-08-13T08-35-41Z](https://github.com/minio/mc/releases/tag/RELEASE.2025-08-13T08-35-41Z)
source. Both upstream repositories are archived; builds restore installability without
creating a maintained upstream project.

## Licenses and validation

Local builds are not official binary releases. MinIO retains GNU AGPL v3-or-later licensing.
Each image contains its upstream license, original source archive, security-patched go.mod/go.sum,
build recipe, input manifest and notice
under /usr/share/minio/; the build recipe ships with this repository.
Base image/package licenses remain under their upstream terms.

Security CI builds and scans the actual resulting images and checks revision/license/source contents.
Fresh acceptance builds from an empty daemon and verifies initialization, S3 reads/writes,
persistence, health, identity and observability. No cached withdrawn server is used as fallback.

## Dependency scan interpretation

The pinned security manifests update dependencies with available fixes; no scanner ignore rule is
used for these images. Trivy also reports GO-2026-5932 against the whole golang.org/x/crypto module.
This advisory concerns the deprecated openpgp package and has no fixed version. Neither server
nor client links golang.org/x/crypto/openpgp or any of its subpackages: the built source-stage
package graphs from go list -mod=readonly -deps . contain none. The module remains necessary for
other cryptographic functions. Keep reviewing full scan output when updating these inputs.
