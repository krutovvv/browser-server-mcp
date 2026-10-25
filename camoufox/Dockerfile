# The Camoufox browser for Bench: an anti-detect Firefox, the Playwright-compatible MCP over
# streamable HTTP for the agent (/mcp) and, in the same process, the live view for the people (/live,
# live.py: Bench's /browser insert watches and steps in there).
# Headful in Xvfb (more natural fingerprint than headless); x11vnc for manual captcha/login.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify tini procps curl ca-certificates \
        libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxtst6 libxt6 libpci3 libegl1 libgl1 \
        fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# noVNC: open the client at / and connect automatically
RUN ln -sf vnc.html /usr/share/novnc/index.html && sed -i "s/UI.initSetting(.autoconnect., false)/UI.initSetting('autoconnect', true)/; s/UI.initSetting(.resize., .off.)/UI.initSetting('resize', 'scale')/" /usr/share/novnc/app/ui.js

ARG MCP_VERSION=0.1.5
ARG CAMOUFOX_VERSION=0.5.8
RUN pip install --no-cache-dir "camoufox-playwright-mcp==${MCP_VERSION}" "camoufox[geoip]==${CAMOUFOX_VERSION}"

# Browser binaries + GeoIP DB baked into the image (HOME=/opt/home so the cache is not on the volume)
ENV HOME=/opt/home
RUN mkdir -p $HOME && camoufox fetch && chmod -R a+rwX $HOME

COPY entrypoint.sh /entrypoint.sh
COPY live.py /opt/live.py
COPY config.json /etc/browser/config.json
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:99 SCREEN=1920x1080x24 MCP_PORT=8931 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0
EXPOSE 8931 6080
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
    CMD curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:${MCP_PORT}/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}" | grep -qE "200|400|406"
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
