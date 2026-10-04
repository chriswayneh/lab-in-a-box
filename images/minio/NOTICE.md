# MinIO local builds

These images contain MinIO server/client source with security-patched Go dependency manifests built locally for Lab in a Box.
MinIO is copyright MinIO, Inc. and its contributors, licensed under GNU AGPL v3 or later.
The upstream license and checksum-verified corresponding source archive are included in
/usr/share/minio/ in each image. Exact revisions, archive checksums and official image
digests are recorded in inputs.json. The build recipe is images/minio/Dockerfile in this
repository. Locally built images are not official MinIO binary releases.

Server: <https://github.com/minio/minio/tree/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a>
Client: <https://github.com/minio/mc/tree/7394ce0dd2a80935aded936b09fa12cbb3cb8096>

Application source is unchanged. Dependency manifests contain targeted security updates.
Both original source archives and the exact replacement go.mod/go.sum files are included;
copy the replacements into the unpacked source to obtain corresponding build source.
Module downloads remain checksum-verified by Go.
The official Go builder is discarded from runtime images. Alpine Linux is the pinned runtime;
installed package licenses remain under their upstream terms.

Security updates address the HIGH/CRITICAL image-scan findings present in the upstream
release graph. go-jose additionally uses v4.1.5 for its September 2026 upstream advisory.
The input manifest records the exact replacement file hashes. No application source
or existing object storage format is modified.
