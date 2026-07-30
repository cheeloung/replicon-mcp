FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY *.py ./

RUN pip install --no-cache-dir ".[remote]"

# Encrypted per-user Replicon credential store lives here — mount a volume
# over it in docker-compose so it survives container restarts/rebuilds.
ENV CREDENTIAL_DB_PATH=/data/replicon-mcp-credentials.db
RUN mkdir -p /data

ENV TRANSPORT=streamable-http
EXPOSE 8001

CMD ["replicon-mcp"]
