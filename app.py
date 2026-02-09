import os
import re
import uuid
from urllib.parse import quote, unquote, urljoin

import requests as http_requests
from flask import Flask, Response, redirect, request

app = Flask(__name__)

M3U_URL = os.environ.get(
    "M3U_URL",
    "https://raw.githubusercontent.com/MichaelJorky/Free-IPTV-M3U-Playlist/main/iptv-serbia.m3u",
)

SESSION = http_requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


@app.route("/health")
def health():
    return "ok"


DEAT_URL = os.environ.get(
    "DEAT_URL",
    "https://raw.githubusercontent.com/Petar-Jorgic/iptv-playlists/master/playlist-deat.m3u",
)


@app.route("/playlist.m3u")
def playlist():
    """Fetch M3U from GitHub and rewrite stream URLs to go through this proxy."""
    r = SESSION.get(M3U_URL, timeout=15)
    r.raise_for_status()

    base = request.host_url.rstrip("/")
    lines = r.text.splitlines()
    out = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(f"{base}/stream?url={quote(stripped, safe='')}")
        else:
            out.append(line)

    return Response("\n".join(out), mimetype="audio/x-mpegurl")


@app.route("/playlist-deat.m3u")
def playlist_deat():
    """Serve DE/AT playlist directly from GitHub (no VPN proxy needed)."""
    r = SESSION.get(DEAT_URL, timeout=15)
    r.raise_for_status()
    return Response(r.text, mimetype="audio/x-mpegurl")


@app.route("/pluto/<channel_id>")
def pluto(channel_id):
    """Redirect to Pluto TV with fresh session params so the stream doesn't expire."""
    sid = str(uuid.uuid4())
    did = str(uuid.uuid4())
    url = (
        f"http://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv"
        f"/stitch/hls/channel/{channel_id}/master.m3u8"
        f"?appName=web&appVersion=unknown&clientTime=0&deviceDNT=0"
        f"&deviceId={did}&deviceMake=Chrome&deviceModel=web"
        f"&deviceType=web&deviceVersion=unknown"
        f"&includeExtendedEvents=false&serverSideAds=false&sid={sid}"
    )
    return redirect(url, code=302)


@app.route("/stream")
def stream():
    """Proxy a single stream URL through the VPN tunnel."""
    url = request.args.get("url")
    if not url:
        return "Missing url parameter", 400

    try:
        r = SESSION.get(url, stream=True, timeout=15)
    except http_requests.RequestException as e:
        return f"Upstream error: {e}", 502

    # Pass through upstream errors instead of mangling them
    if r.status_code >= 400:
        return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "text/plain"))

    content_type = r.headers.get("Content-Type", "")

    # HLS playlists need URL rewriting so the player fetches segments through us
    if ("m3u8" in url or "mpegurl" in content_type.lower() or "m3u" in content_type.lower()) and "html" not in content_type.lower():
        return _rewrite_hls(r.text, url)

    # Everything else (TS segments, AAC, etc.) – stream through
    def generate():
        for chunk in r.iter_content(chunk_size=65536):
            yield chunk

    resp_headers = {}
    for h in ("Content-Type", "Content-Length", "Cache-Control"):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    return Response(generate(), headers=resp_headers)


def _rewrite_hls(content: str, original_url: str) -> Response:
    """Rewrite URLs inside an HLS manifest to route through the proxy."""
    base = request.host_url.rstrip("/")
    stream_base = original_url.rsplit("/", 1)[0] + "/"

    lines = content.splitlines()
    out = []

    for line in lines:
        stripped = line.strip()

        # Rewrite URI="..." in EXT-X-KEY / EXT-X-MAP etc.
        if "URI=" in stripped:
            def _replace_uri(m):
                uri = m.group(1)
                if not uri.startswith("http"):
                    uri = urljoin(stream_base, uri)
                return f'URI="{base}/stream?url={quote(uri, safe="")}"'

            stripped = re.sub(r'URI="([^"]+)"', _replace_uri, stripped)
            out.append(stripped)
        elif stripped and not stripped.startswith("#"):
            # Stream/segment URL line
            full_url = stripped if stripped.startswith("http") else urljoin(stream_base, stripped)
            out.append(f"{base}/stream?url={quote(full_url, safe='')}")
        else:
            out.append(stripped)

    return Response("\n".join(out), mimetype="application/vnd.apple.mpegurl")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
