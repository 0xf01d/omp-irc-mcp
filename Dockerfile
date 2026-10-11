FROM python:3.12-slim

# omp CLI (oh-my-pi) — installed via npm-less bun runtime shipped by omp's installer
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git bash \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://bun.sh/install | bash \
 && ~/.bun/bin/bun install -g @oh-my-pi/pi-coding-agent \
 && ln -s ~/.bun/bin/omp /usr/local/bin/omp \
 && apt-get purge -y curl && apt-get autoremove -y

WORKDIR /app
COPY mcp_irc_server.py loop.py PROMPT.md entrypoint.sh ./
RUN chmod +x entrypoint.sh mcp_irc_server.py

# Durable state lives on a volume so the task queue and IRC spool survive
# container restarts.
VOLUME ["/state"]
ENTRYPOINT ["./entrypoint.sh"]
