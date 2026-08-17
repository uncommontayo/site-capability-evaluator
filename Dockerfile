FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY fixtures ./fixtures
COPY openapi.yaml .

# No secrets, no host assumptions baked in. Every setting below has a safe default
# (LLM_CLIENT=recorded means it boots without a real API key) and is overridable
# at `docker run` time via -e.
ENV CATALOG_BASE=http://localhost:8000 \
    LLM_PROVIDER=anthropic \
    LLM_MODEL=claude-sonnet-4-6 \
    LLM_CLIENT=recorded \
    EVALUATOR_VERSION=0.1.0 \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
