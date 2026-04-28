FROM python:3.12-slim

WORKDIR /app

# tzdata is required for the TZ environment variable to take effect.
# Without it, the slim image only has UTC available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir paho-mqtt==2.1.0 pyyaml==6.0.2

COPY bambu_overlay.py /app/

RUN mkdir -p /data/overlay /config

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/bambu_overlay.py"]
