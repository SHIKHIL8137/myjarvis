# Pinned, integrity-verified non-root OpenCode execution image for Jarvis Dev Workforce
# Build: docker build -f deploy/opencode.Dockerfile -t jarvis-opencode:1.19.5 .
FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=1.19.5
ARG OPENCODE_META_SRI="sha512-tEqMvQahfhdDj8vceYpXtoIrvjPXm5XRfetWnPN5YISueU7adNx9JcUA3zZ+2GKuDVgDpGdQe1ZLtCZDjqodvA=="
ARG OPENCODE_LINUX_X64_SRI="sha512-i1qh7VooLGML9xX6ud93y0JA8j/1aPeDYRKjsmYXrJX1TjIWHgBBMkEbEJPZthpWbsJg//XfBgR6Kb7+NPaJiQ=="
ARG OPENCODE_LINUX_ARM64_SRI="sha512-u2HFRI6v1nNMVKiV34An8XrGBxJeZ+RCg2tYu3SqlGODKqKsr5RoEbaX1tnsfGb+rETh9AJVX9kS8dSZXPufsA=="
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    HOME=/home/jarvis

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Download exact pinned npm tarballs and verify SHA-512 SRI integrity BEFORE extracting (never execute remote install scripts or unverified `latest`)
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    if [ "$arch" = "amd64" ] || [ "$arch" = "x86_64" ]; then \
        BIN_PKG="opencode-linux-x64"; \
        BIN_SRI="${OPENCODE_LINUX_X64_SRI}"; \
    elif [ "$arch" = "arm64" ] || [ "$arch" = "aarch64" ]; then \
        BIN_PKG="opencode-linux-arm64"; \
        BIN_SRI="${OPENCODE_LINUX_ARM64_SRI}"; \
    else \
        echo "Unsupported container architecture: $arch" >&2; exit 1; \
    fi; \
    mkdir -p /tmp/oc-stage /opt/opencode /workspace; \
    node -e ' \
      const fs = require("fs"); \
      const crypto = require("crypto"); \
      const https = require("https"); \
      function fetchAndVerify(url, expectedSri, outPath) { \
        return new Promise((resolve, reject) => { \
          https.get(url, (res) => { \
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) { \
              return fetchAndVerify(res.headers.location, expectedSri, outPath).then(resolve, reject); \
            } \
            if (res.statusCode !== 200) return reject(new Error("HTTP " + res.statusCode + " for " + url)); \
            const chunks = []; \
            res.on("data", (c) => chunks.push(c)); \
            res.on("end", () => { \
              const buf = Buffer.concat(chunks); \
              const actual = "sha512-" + crypto.createHash("sha512").update(buf).digest("base64"); \
              if (actual !== expectedSri) { \
                return reject(new Error("SRI mismatch for " + url + ": expected " + expectedSri + " got " + actual)); \
              } \
              fs.writeFileSync(outPath, buf); \
              resolve(); \
            }); \
          }).on("error", reject); \
        }); \
      } \
      Promise.all([ \
        fetchAndVerify(`https://registry.npmjs.org/opencode-ai/-/opencode-ai-${process.env.OPENCODE_VERSION}.tgz`, process.env.OPENCODE_META_SRI, "/tmp/oc-stage/meta.tgz"), \
        fetchAndVerify(`https://registry.npmjs.org/${process.env.BIN_PKG}/-/${process.env.BIN_PKG}-${process.env.OPENCODE_VERSION}.tgz`, process.env.BIN_SRI, "/tmp/oc-stage/bin.tgz") \
      ]).catch((e) => { console.error(e); process.exit(1); }); \
    ' OPENCODE_VERSION="${OPENCODE_VERSION}" OPENCODE_META_SRI="${OPENCODE_META_SRI}" BIN_PKG="${BIN_PKG}" BIN_SRI="${BIN_SRI}"; \
    tar -xzf /tmp/oc-stage/meta.tgz -C /opt/opencode --strip-components=1; \
    mkdir -p /opt/opencode/platform-bin; \
    tar -xzf /tmp/oc-stage/bin.tgz -C /opt/opencode/platform-bin --strip-components=1; \
    chmod 0755 /opt/opencode/platform-bin/bin/opencode; \
    ln -sf /opt/opencode/platform-bin/bin/opencode /usr/local/bin/opencode; \
    rm -rf /tmp/oc-stage; \
    opencode --version | grep -F "${OPENCODE_VERSION}"

# Create deterministic unprivileged user with no host credentials
RUN groupadd --gid 10001 jarvis \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/jarvis --shell /bin/bash jarvis \
    && mkdir -p /home/jarvis/.local/state /home/jarvis/.config/opencode /workspace \
    && chown -R 10001:10001 /home/jarvis /workspace

USER 10001:10001
WORKDIR /workspace

LABEL org.opencontainers.image.title="jarvis-opencode-sandbox" \
      org.opencontainers.image.version="1.19.5" \
      ai.jarvis.opencode.version="1.19.5" \
      ai.jarvis.nonroot="true"

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=2 CMD opencode --version >/dev/null 2>&1 || exit 1

CMD ["opencode", "--version"]

