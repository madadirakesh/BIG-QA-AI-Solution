# ─────────────────────────────────────────────────────────────────────────────
# BIG QA AI Solution — Docker Image
# Includes: Python 3.11, Google Chrome, ChromeDriver, Java 17, Apache JMeter
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# --------------------------------------------------------------------------- #
#  Build arguments
# --------------------------------------------------------------------------- #
ARG JMETER_VERSION=5.6.3
ARG DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------- #
#  Labels
# --------------------------------------------------------------------------- #
LABEL maintainer="BIG QA Team" \
      description="BIG QA AI Solution – performance & functional test automation platform" \
      version="2.0"

# --------------------------------------------------------------------------- #
#  System dependencies + Google Chrome + Java 17
# --------------------------------------------------------------------------- #
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        # Core utilities
        curl wget gnupg ca-certificates unzip \
        # Required by Chrome
        fonts-liberation libxss1 libasound2 \
        libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-xcb1 \
        libxcb-dri3-0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 xdg-utils \
        # SQLite CLI (optional, useful for debugging)
        sqlite3 \
        # Java 17 for JMeter
        openjdk-17-jre-headless \
        # Git (used by some project scaffolding steps)
        git \
    && \
    # ── Google Chrome stable ─────────────────────────────────────────────── #
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
          https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update -qq && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    # ── Clean up apt cache ───────────────────────────────────────────────── #
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------- #
#  Apache JMeter
# --------------------------------------------------------------------------- #
RUN mkdir -p /opt/jmeter && \
    curl -fsSL \
        "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz" \
        -o /tmp/jmeter.tgz && \
    tar -xzf /tmp/jmeter.tgz -C /opt/jmeter --strip-components=1 && \
    rm /tmp/jmeter.tgz && \
    ln -s /opt/jmeter/bin/jmeter /usr/local/bin/jmeter

ENV JMETER_HOME=/opt/jmeter
ENV PATH="${JMETER_HOME}/bin:${PATH}"

# --------------------------------------------------------------------------- #
#  Node.js + Playwright MCP (Test Execution module)
# Bookworm ships Node 18, the minimum @playwright/mcp supports. The package is
# fetched at build time so the first run doesn't download it; it drives the
# Google Chrome installed above.
# --------------------------------------------------------------------------- #
ARG PLAYWRIGHT_MCP_PACKAGE=@playwright/mcp@0.0.83
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends nodejs npm && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    npx -y ${PLAYWRIGHT_MCP_PACKAGE} --version
ENV PLAYWRIGHT_MCP_PACKAGE=${PLAYWRIGHT_MCP_PACKAGE} \
    # Root inside a container: Chrome needs --no-sandbox
    TEST_EXECUTION_NO_SANDBOX=true

# --------------------------------------------------------------------------- #
#  Java environment (for JMeter and any Maven/Gradle projects)
# --------------------------------------------------------------------------- #
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# --------------------------------------------------------------------------- #
#  Python dependencies
# --------------------------------------------------------------------------- #
WORKDIR /app

# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # Install webdriver-manager so ChromeDriver is fetched/cached at install time
    pip install --no-cache-dir webdriver-manager

# --------------------------------------------------------------------------- #
#  Application code
# --------------------------------------------------------------------------- #
COPY . .

# --------------------------------------------------------------------------- #
#  Runtime environment
# --------------------------------------------------------------------------- #
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Tell the app it is running inside a container
    DOCKER=true \
    HEADLESS=true \
    HEADLESS_CLOUD=true \
    # Chrome flags needed inside a container (no sandbox, shared memory workaround)
    CHROME_NO_SANDBOX=true \
    # Workspace volume mount point for generated projects
    WORKSPACE_PATH=/workspace \
    # Flask
    FLASK_ENV=production \
    # Prevent pip auto-upgrade nag inside container
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Ensure ChromeDriver managed by webdriver-manager is cached across runs
    WDM_CACHE=/root/.wdm

# --------------------------------------------------------------------------- #
#  Ports
# --------------------------------------------------------------------------- #
# 5000 → Flask web UI
# 8000 → FastAPI AI backend
EXPOSE 5000 8000

# --------------------------------------------------------------------------- #
#  Workspace volume
# All generated/scaffolded test projects are written here so they survive
# container restarts when this directory is bind-mounted from the host.
# --------------------------------------------------------------------------- #
VOLUME ["/workspace"]

# --------------------------------------------------------------------------- #
#  Health check — verifies the Flask app is responding
# --------------------------------------------------------------------------- #
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# --------------------------------------------------------------------------- #
#  Entrypoint
# --------------------------------------------------------------------------- #
CMD ["python", "ScriptGenerator/app.py"]
