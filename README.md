# IPTV-Proxy

Flask-based HTTP proxy that tunnels IPTV streams through a WireGuard VPN (ProtonVPN Serbia). Runs as a privileged Kubernetes pod to manage the VPN interface.

## Architecture

```mermaid
graph TB
    subgraph "Jellyfin Pod"
        JF[Jellyfin]
    end

    subgraph "IPTV-Proxy Pod (privileged)"
        EP[entrypoint.sh] -->|1. wg-quick up| WG[WireGuard wg0]
        EP -->|2. start| GU[Gunicorn :8080]

        subgraph "Flask App"
            P1["/playlist.m3u<br/>Fetch + rewrite URLs"]
            P2["/playlist-deat.m3u<br/>Pass-through"]
            P3["/stream?url=...<br/>Proxy via VPN"]
            P4["/pluto/channel_id<br/>Fresh session redirect"]
            P5["/health"]
        end

        GU --> P1 & P2 & P3 & P4 & P5
    end

    JF -->|"Tuner 1"| P1
    JF -->|play channel| P3
    JF -->|"Tuner 2 (NFL)"| P4

    P1 -->|fetch M3U| GH[GitHub Raw]
    P3 -->|stream via VPN| WG
    WG -->|WireGuard tunnel| VPN[ProtonVPN Serbia<br/>37.46.115.5]
    VPN --> IPTV[Serbian IPTV CDNs]

    P4 -->|302 redirect| PLUTO[Pluto TV<br/>fresh session]
```

## How it works

```mermaid
sequenceDiagram
    participant J as Jellyfin
    participant P as IPTV-Proxy
    participant G as GitHub
    participant V as VPN Tunnel
    participant S as Stream CDN

    J->>P: GET /playlist.m3u
    P->>G: Fetch playlist-clean.m3u
    G-->>P: M3U with direct URLs
    P-->>J: M3U with URLs rewritten to /stream?url=...

    J->>P: GET /stream?url=https://edge8.pink.rs/...
    P->>V: GET https://edge8.pink.rs/... (via WireGuard)
    V->>S: Request (Serbian IP)
    S-->>V: HLS manifest
    V-->>P: Response
    P-->>J: Rewritten HLS (segment URLs also proxied)

    J->>P: GET /stream?url=.../segment001.ts
    P->>V: Proxy segment through VPN
    V-->>P: Video data
    P-->>J: Stream video chunks
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness/readiness probe |
| `/playlist.m3u` | GET | Serbian playlist with URLs rewritten through VPN proxy |
| `/playlist-deat.m3u` | GET | DE/AT playlist passed through from GitHub (no VPN) |
| `/stream?url=<encoded>` | GET | Proxy any URL through VPN tunnel with HLS rewriting |
| `/pluto/<channel_id>` | GET | 302 redirect to Pluto TV with fresh session params |

## Files

| File | Description |
|------|-------------|
| `app.py` | Flask application with proxy logic and HLS URL rewriting |
| `Dockerfile` | Container image (python:3.12-slim + wireguard-tools) |
| `entrypoint.sh` | Starts WireGuard, sets DNS, launches Gunicorn |
| `requirements.txt` | Python deps: flask, gunicorn, requests |
| `wg0.conf` | WireGuard config (mounted from K8s secret) |
| `k8s/deployment.yaml` | Kubernetes deployment (privileged, 1 replica) |
| `k8s/service.yaml` | NodePort service on port 8080 |

## Playlists

Playlists are hosted at [Petar-Jorgic/iptv-playlists](https://github.com/Petar-Jorgic/iptv-playlists):

| Playlist | Channels | VPN | Content |
|----------|----------|-----|---------|
| `playlist-clean.m3u` | 16 | Serbia | Serbian TV, Music, News, Religious |
| `playlist-deat.m3u` | 30 | No | German, Austrian, American Football |

## Deployment

```bash
# Build image
buildah bud --no-cache -t iptv-proxy:latest .

# Export and import to k3s
buildah push iptv-proxy:latest docker-archive:/tmp/iptv-proxy.tar:docker.io/library/iptv-proxy:latest
k3s ctr images import /tmp/iptv-proxy.tar

# Deploy
kubectl apply -f k8s/deployment.yaml -f k8s/service.yaml

# Restart after image update
kubectl rollout restart deployment/iptv-proxy
```

## Resources

| | Requests | Limits |
|---|----------|--------|
| CPU | 100m | 500m |
| Memory | 128Mi | 256Mi |
