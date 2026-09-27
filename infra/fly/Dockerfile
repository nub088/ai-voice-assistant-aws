# --- builder: compile wheels with the full toolchain (S17) -------------------
# Native-extension deps (spacy/presidio/etc.) may need a compiler at
# wheel-build time; it lives here and never ships in the runtime image.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# requirements.lock is the uv-generated hash-pinned resolution of
# requirements.txt (S16); regenerate on intentional bumps with
#   uv pip compile --generate-hashes -o requirements.lock requirements.txt
COPY requirements.lock ./
RUN pip wheel --wheel-dir /wheels --require-hashes -r requirements.lock

# --- runtime: no compiler, only runtime libs (S17) ----------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
      sqlite3 \
      gosu \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user to run the app (S9). The container still STARTS as root so
# entrypoint.sh can chown the Fly volume, then drops to this user via gosu.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

# Hash verification happened at download time in the builder (--require-hashes
# on pip wheel); wheels rebuilt from sdists there carry new hashes, so the
# runtime install pins exact versions from the lock but skips re-hashing.
COPY requirements.lock ./
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements.lock \
 && rm -rf /wheels \
 && python -m spacy download en_core_web_sm

COPY tools ./tools
COPY config ./config
COPY infra/fly/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
 && chown -R app:app /app

EXPOSE 8080

CMD ["./entrypoint.sh"]
