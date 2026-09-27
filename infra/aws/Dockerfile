FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements-aws.lock ./
RUN pip wheel --wheel-dir /wheels --require-hashes -r requirements-aws.lock

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 gosu && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app
COPY requirements-aws.lock ./
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements-aws.lock && rm -rf /wheels && python -m spacy download en_core_web_sm
COPY tools ./tools
COPY config ./config
COPY infra/aws/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh && chown -R app:app /app
USER app
EXPOSE 8080
CMD ["./entrypoint.sh"]
