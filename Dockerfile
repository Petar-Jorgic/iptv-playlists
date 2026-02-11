FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      wireguard-tools \
      iproute2 \
      iptables \
      procps \
      wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY playlist-clean.m3u .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
