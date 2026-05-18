FROM node:24-bookworm-slim AS node-runtime

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=node-runtime /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN chmod +x /app/docker/start-render.sh

EXPOSE 10000

CMD ["/app/docker/start-render.sh"]
