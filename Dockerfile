# Bullet Time studio server: one container for the team, generation still runs on Comfy Cloud.
#   docker build -t bullettime .
#   docker run -p 8790:8790 -e COMFY_API_KEY=... -e BULLETTIME_PASSWORD=... -v bullettime-data:/data bullettime
FROM python:3.12-slim

# ffmpeg for the splice; libegl/libgles for mediapipe (the eye check); nothing needs a GPU here
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libegl1 libgles2 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY bullettime ./bullettime
COPY workflows ./workflows
RUN pip install --no-cache-dir -e .

# the face landmarker model (3.7 MB) is fetched at build time so the container works offline from Google
ENV BULLETTIME_MODELS=/models BULLETTIME_DATA=/data HOME=/data
RUN python -c "from bullettime import eyes; print('eye model:', eyes._model())"

VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/api/health').status==200 else 1)"
CMD ["bullettime", "serve", "--host", "0.0.0.0", "--port", "8790", "--data", "/data"]
