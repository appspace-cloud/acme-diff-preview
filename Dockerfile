# Pin base image by digest to prevent silent upstream updates.
# To update: docker pull python:3.12-slim && docker inspect ... | grep RepoDigest
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ARG ARGOCD_VERSION=v3.4.3
ARG HELM_VERSION=v3.21.2

# Install curl, then download argocd and helm CLIs.
# v2.5.19 (M2, supply chain): both binaries are now checksum-verified before
# install. The Python base is digest-pinned; these two were curl'd unverified,
# so a compromised get.helm.sh / GitHub release asset would ship silently.
# helm publishes a `sha256sum -c`-compatible file next to the tarball; argocd
# publishes a combined `cli_checksums.txt` for the release. We fetch the
# official checksum file alongside each artifact and verify, failing the build
# on any mismatch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -fsSL \
        "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-amd64" \
        -o /tmp/argocd \
    && curl -fsSL \
        "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/cli_checksums.txt" \
        -o /tmp/argocd_checksums.txt \
    && ( cd /tmp && grep ' argocd-linux-amd64$' argocd_checksums.txt \
         | sed 's# argocd-linux-amd64# /tmp/argocd#' | sha256sum -c - ) \
    && install -m 0755 /tmp/argocd /usr/local/bin/argocd \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
        -o /tmp/helm.tar.gz \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" \
        -o /tmp/helm.tar.gz.sha256sum \
    && ( cd /tmp && sed 's#  helm-.*#  /tmp/helm.tar.gz#' helm.tar.gz.sha256sum \
         | sha256sum -c - ) \
    && tar -xf /tmp/helm.tar.gz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/helm.tar.gz.sha256sum /tmp/linux-amd64 \
        /tmp/argocd /tmp/argocd_checksums.txt \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Verify binaries
RUN argocd version --client 2>&1 | head -1 && helm version --short

COPY src/ /app/
WORKDIR /app

# Running version, surfaced by the app at startup and in /diff-preview/stats.
# docker.yml passes the git tag as the APP_VERSION build-arg (v2.5.19, F1).
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Run as nobody (uid 65534) — matches securityContext in Helm chart
USER 65534

CMD ["python3", "diff_preview.py"]
