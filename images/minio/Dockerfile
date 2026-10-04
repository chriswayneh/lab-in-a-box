# Official upstream source and official build/runtime images are pinned.
FROM golang:1.27.1-alpine3.24@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS compiler
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOMAXPROCS=2 GOMEMLIMIT=768MiB
WORKDIR /src

FROM compiler AS server-build
RUN wget -q -O /source.tar.gz https://codeload.github.com/minio/minio/tar.gz/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && echo "45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841  /source.tar.gz" | sha256sum -c - \
    && tar -xzf /source.tar.gz --strip-components=1
COPY server/go.mod server/go.sum ./
RUN go mod download && go mod verify \
    && go build -mod=readonly -p=2 -trimpath -buildvcs=false -tags kqueue \
    -ldflags="-s -w -X github.com/minio/minio/cmd.Version=2025-10-15T17:29:55Z -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2025-10-15T17-29-55Z.lab-build -X github.com/minio/minio/cmd.CommitID=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a -X github.com/minio/minio/cmd.ShortCommitID=9e49d5e7a648f -X github.com/minio/minio/cmd.CopyrightYear=2025" \
    -o /out/minio .

FROM compiler AS client-build
RUN wget -q -O /source.tar.gz https://codeload.github.com/minio/mc/tar.gz/7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && echo "95cd293c7119f16921a6dc515a1fb74a2227f19fd994b9c8b770a154e802ac44  /source.tar.gz" | sha256sum -c - \
    && tar -xzf /source.tar.gz --strip-components=1
COPY client/go.mod client/go.sum ./
RUN go mod download && go mod verify \
    && go build -mod=readonly -p=2 -trimpath -buildvcs=false -ldflags="-s -w" -o /out/mc .

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS runtime
COPY inputs.json NOTICE.md Dockerfile /usr/share/minio/

FROM runtime AS client
LABEL org.opencontainers.image.source="https://github.com/minio/mc" \
      org.opencontainers.image.revision="7394ce0dd2a80935aded936b09fa12cbb3cb8096" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
COPY --from=client-build /out/mc /usr/bin/mc
COPY --from=client-build /src/LICENSE /usr/share/minio/LICENSE
COPY --from=client-build /source.tar.gz /usr/share/minio/client-source.tar.gz
COPY --from=client-build /src/go.mod /src/go.sum /usr/share/minio/
ENTRYPOINT ["mc"]

FROM runtime AS server
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
COPY --from=server-build /out/minio /usr/bin/minio
COPY --from=server-build /src/dockerscripts/docker-entrypoint.sh /usr/bin/docker-entrypoint.sh
COPY --from=server-build /src/LICENSE /usr/share/minio/LICENSE
COPY --from=server-build /source.tar.gz /usr/share/minio/server-source.tar.gz
COPY --from=server-build /src/go.mod /src/go.sum /usr/share/minio/
RUN chmod 755 /usr/bin/docker-entrypoint.sh
EXPOSE 9000 9001
ENTRYPOINT ["/usr/bin/docker-entrypoint.sh"]
CMD ["minio"]
