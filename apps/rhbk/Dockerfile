# Supply the official RHBK image (or its verified mirror) by sha256 digest.
# No database password, bootstrap credential, certificate or realm is built in.
ARG RHBK_IMAGE
FROM ${RHBK_IMAGE} AS builder
ARG RHBK_IMAGE
WORKDIR /opt/keycloak
RUN digest="${RHBK_IMAGE##*@sha256:}"; \
    test "$digest" != "$RHBK_IMAGE" && test "${#digest}" -eq 64 && \
    case "$digest" in *[!0-9a-f]*) exit 1;; esac && \
    /opt/keycloak/bin/kc.sh build \
      --features=client-secret-rotation \
      --db=postgres \
      --health-enabled=true \
      --metrics-enabled=true

FROM ${RHBK_IMAGE}
COPY --from=builder /opt/keycloak/ /opt/keycloak/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized"]
