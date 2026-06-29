# Pin base image by digest to prevent silent upstream updates.
# To update: docker pull python:3.12-slim && docker inspect ... | grep RepoDigest
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ARG ARGOCD_VERSION=v3.4.3
ARG HELM_VERSION=v3.21.2

# Install curl, then download argocd and helm CLIs
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -sSL \
        "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-amd64" \
        -o /usr/local/bin/argocd \
    && chmod +x /usr/local/bin/argocd \
    && curl -sSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
        -o /tmp/helm.tar.gz \
    && tar -xf /tmp/helm.tar.gz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Verify binaries
RUN argocd version --client 2>&1 | head -1 && helm version --short

COPY src/ /app/
WORKDIR /app

# Run as nobody (uid 65534) — matches securityContext in Helm chart
USER 65534

CMD ["python3", "diff_preview.py"]
