FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1

# Dependencies first - cached unless pyproject.toml/uv.lock changes
COPY pyproject.toml uv.lock* ./
RUN uv pip install .

# OpenTelemetry dependency
RUN uv pip install aws-opentelemetry-distro==0.12.2

# Application code last - changes here won't reinstall dependencies
COPY *.py ./

RUN useradd -m -u 1000 bedrock_agentcore

USER bedrock_agentcore

EXPOSE 9000
EXPOSE 8000
EXPOSE 8080

CMD ["opentelemetry-instrument", "python", "-m", "main"]
