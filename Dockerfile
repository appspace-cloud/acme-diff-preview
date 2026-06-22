FROM python:3.12-slim

ARG ARGOCD_VERSION=v3.4.3

# Install curl for argocd CLI download
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -sSL \
        "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-amd64" \
        -o /usr/local/bin/argocd \
    && chmod +x /usr/local/bin/argocd \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Verify argocd binary
RUN argocd version --client 2>&1 | head -1

COPY src/ /app/
WORKDIR /app

# Run as nobody (uid 65534) — matches securityContext in Helm chart
USER 65534

CMD ["python3", "diff_preview.py"]
