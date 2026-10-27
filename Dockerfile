FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIODOCK_RUNTIME_DIR=/tmp/audiodock

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && mkdir -p /app/licenses \
    && ffmpeg -version > /app/licenses/ffmpeg-build.txt 2>&1 \
    && cp /usr/share/doc/ffmpeg/copyright /app/licenses/ffmpeg-copyright
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend /app/backend
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
RUN useradd --uid 10001 --create-home audiodock && chown -R audiodock:audiodock /app
USER audiodock
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log --no-proxy-headers"]
