FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY tnd_agent ./tnd_agent
CMD ["uv", "run", "--no-sync", "python", "-m", "tnd_agent.run"]
