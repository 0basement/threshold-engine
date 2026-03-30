FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY threshold_engine/ threshold_engine/

RUN pip install --no-cache-dir .

# Config is mounted at runtime — see docker-compose.yml
CMD ["python", "-m", "threshold_engine", "--config", "/config/config.yaml"]
