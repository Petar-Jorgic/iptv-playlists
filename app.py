import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import quote, unquote, urljoin, urlparse

import requests as http_requests
from flask import Flask, Response, redirect, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iptv-proxy")

app = Flask(__name__)

M3U_URL = os.environ.get(
    "M3U_URL",
    "https://raw.githubusercontent.com/MichaelJorky/Free-IPTV-M3U-Playlist/main/iptv-serbia.m3u",
)

SESSION = http_requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
)

CACHE_PATH = "/tmp/url_cache.json"
CACHE_TTL = 3600  # 1 hour
HEALTH_CHECK_INTERVAL = 900  # 15 minutes
MANIFEST_FILENAMES = ["index.m3u8", "playlist.m3u8", "live.m3u8", "master.m3u8", "mono.m3u8"]
PINK_EDGE_RANGE = range(1, 9)  # edge1 through edge8

# ---------------------------------------------------------------------------
# URL cache (file-based, shared across gunicorn workers)
# ---------------------------------------------------------------------------

def _read_cache() -> dict:
    try:
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache(cache: dict):
    tmp_fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(cache, f)
        os.rename(tmp_path, CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _cache_get(original_url: str):
    cache = _read_cache()
    entry = cache.get(original_url)
    if entry and time.time() - entry["timestamp"] < CACHE_TTL:
        return entry
    return None


def _cache_set(original_url: str, resolved_url: str, status: str):
    cache = _read_cache()
    cache[original_url] = {
        "resolved_url": resolved_url,
        "timestamp": time.time(),
        "status": status,
    }
    _write_cache(cache)

# ---------------------------------------------------------------------------
# URL variant generator
# ---------------------------------------------------------------------------

def _generate_variants(url: str) -> list[str]:
    """Generate alternative URLs to try when the original returns 404."""
    parsed = urlparse(url)
    path = parsed.path
    variants = []

    # Only generate variants for .m3u8 manifest URLs (not .ts segments)
    if not path.endswith(".m3u8"):
        return variants

    # Filename swap: try other common manifest names
    path_dir = path.rsplit("/", 1)[0] + "/" if "/" in path else "/"
    current_filename = path.rsplit("/", 1)[-1] if "/" in path else path
    for alt in MANIFEST_FILENAMES:
        if alt != current_filename:
            new_url = parsed._replace(path=path_dir + alt).geturl()
            variants.append(new_url)

    # Edge server swap for pink.rs domains
    if "pink.rs" in parsed.hostname:
        for edge_num in PINK_EDGE_RANGE:
            edge_host = f"edge{edge_num}.pink.rs"
            if edge_host != parsed.hostname:
                new_url = parsed._replace(netloc=edge_host).geturl()
                variants.append(new_url)

    return variants

# ---------------------------------------------------------------------------
# Probe a URL (HEAD then GET fallback)
# ---------------------------------------------------------------------------

def _probe_url(url: str, timeout: int = 10) -> bool:
    """Return True if the URL is reachable (2xx/3xx)."""
    try:
        r = SESSION.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
        # Some servers reject HEAD; try GET
        r = SESSION.get(url, timeout=timeout, stream=True)
        r.close()
        return r.status_code < 400
    except http_requests.RequestException:
        return False

# ---------------------------------------------------------------------------
# Resolve URL: check cache, try original, try variants
# ---------------------------------------------------------------------------

def _is_healable_url(url: str) -> bool:
    """Only heal top-level manifest URLs, not ephemeral chunklists/variants."""
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    # Only heal if the filename is a known manifest name (index.m3u8, playlist.m3u8, etc.)
    # Ephemeral URLs like chunklist_w12345.m3u8 or 0.m3u8 should NOT be healed
    return filename in MANIFEST_FILENAMES


def _resolve_url(original_url: str) -> tuple[str, str]:
    """Resolve a stream URL, trying variants if the original is broken.
    Returns (resolved_url, status) where status is 'ok', 'healed', or 'dead'.
    """
    # Only auto-heal known top-level manifest URLs, not chunklists/variants
    if not _is_healable_url(original_url):
        return original_url, "passthrough"

    # Check cache first
    cached = _cache_get(original_url)
    if cached:
        return cached["resolved_url"], cached["status"]

    # Try original
    if _probe_url(original_url):
        _cache_set(original_url, original_url, "ok")
        return original_url, "ok"

    # Try variants
    for variant in _generate_variants(original_url):
        if _probe_url(variant):
            log.warning("AUTO-HEALED: %s -> %s", original_url, variant)
            _cache_set(original_url, variant, "healed")
            return variant, "healed"

    # All variants failed
    log.error("DEAD STREAM: %s (all variants failed)", original_url)
    _cache_set(original_url, original_url, "dead")
    return original_url, "dead"


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
            # Encode the source URL into a .ts path (no '.m3u8' anywhere) so Jellyfin
            # detects the channel as MPEG-TS and does NOT force ffmpeg '-f hls'
            # (which fails on our continuous TS stream).
            tok = base64.urlsafe_b64encode(stripped.encode()).decode().rstrip("=")
            out.append(f"{base}/live/{tok}.ts")
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


@app.route("/live/<token>")
def live(token):
    """Serve a channel as a continuous MPEG-TS stream. <token> is base64url(source
    URL)+'.ts' so Jellyfin sees a .ts URL and uses the TS demuxer instead of -f hls."""
    if token.endswith(".ts"):
        token = token[:-3]
    try:
        pad = "=" * (-len(token) % 4)
        source_url = base64.urlsafe_b64decode(token + pad).decode()
    except Exception:
        return "bad token", 400
    return Response(_ts_stream(source_url), content_type="video/mp2t")


@app.route("/stream")
def stream():
    """Proxy a single stream URL through the VPN tunnel (with auto-healing)."""
    url = request.args.get("url")
    if not url:
        return "Missing url parameter", 400

    # Resolve URL (may auto-heal to a different variant)
    resolved_url, heal_status = _resolve_url(url)

    try:
        r = SESSION.get(resolved_url, stream=True, timeout=15)
    except http_requests.RequestException as e:
        return f"Upstream error: {e}", 502

    # If the cached resolved URL now also fails, invalidate and retry once
    if r.status_code >= 400 and heal_status in ("ok", "healed") and _is_healable_url(url):
        # Invalidate stale cache entry
        cache = _read_cache()
        cache.pop(url, None)
        _write_cache(cache)
        resolved_url, heal_status = _resolve_url(url)
        try:
            r = SESSION.get(resolved_url, stream=True, timeout=15)
        except http_requests.RequestException as e:
            return f"Upstream error: {e}", 502

    # Pass through upstream errors
    if r.status_code >= 400:
        return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "text/plain"))

    content_type = r.headers.get("Content-Type", "")

    # HLS playlists need URL rewriting so the player fetches segments through us
    # Use the ORIGINAL url as base for rewriting so relative segment paths resolve correctly
    if ("m3u8" in resolved_url or "mpegurl" in content_type.lower() or "m3u" in content_type.lower()) and "html" not in content_type.lower():
        # Serve live HLS as a continuous MPEG-TS stream instead of an HLS playlist.
        # The player then gets plain TS, so jellyfin-ffmpeg's allowed_segment_extensions
        # check (which rejects proxied /stream?url=... segment URLs and appends #EXTM3U)
        # never runs. Segments are downloaded server-side through the VPN and
        # concatenated; the source is re-resolved (master->media) each poll so
        # token-rotating live HLS keeps delivering fresh segments.
        r.close()
        return Response(_ts_stream(resolved_url), content_type="video/mp2t")

    # Everything else (TS segments, AAC, etc.) – stream through
    def generate():
        for chunk in r.iter_content(chunk_size=65536):
            yield chunk

    resp_headers = {}
    for h in ("Content-Type", "Content-Length", "Cache-Control"):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    return Response(generate(), headers=resp_headers)


def _first_variant_url(content: str, base_url: str) -> str | None:
    """Return the first variant stream URL from a master playlist (absolute)."""
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("#EXT-X-STREAM-INF"):
            for j in range(i + 1, len(lines)):
                u = lines[j].strip()
                if u and not u.startswith("#"):
                    return u if u.startswith("http") else urljoin(base_dir, u)
    return None


def _parse_media_segments(content: str, base_url: str):
    """Parse a media playlist -> ([(sequence, absolute_segment_url), ...], target_dur)."""
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    media_seq = 0
    target = 2.0
    segs = []
    idx = 0
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_seq = int(s.split(":", 1)[1])
            except ValueError:
                pass
        elif s.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(s.split(":", 1)[1])
            except ValueError:
                pass
        elif s and not s.startswith("#"):
            seg = s if s.startswith("http") else urljoin(base_dir, s)
            segs.append((media_seq + idx, seg))
            idx += 1
    return segs, target


def _resolve_media_playlist(channel_url: str):
    """Fetch a channel URL, follow master->media variant, return (segments, target_dur)."""
    base_url = channel_url
    for _ in range(4):
        try:
            r = SESSION.get(base_url, timeout=15)
        except http_requests.RequestException:
            return [], 2.0
        if r.status_code >= 400:
            return [], 2.0
        content = r.text
        if "#EXT-X-STREAM-INF" in content:
            v = _first_variant_url(content, base_url)
            if not v:
                break
            base_url = v
            continue
        return _parse_media_segments(content, base_url)
    return [], 2.0


def _ts_stream(channel_url: str):
    """Yield live HLS segments (downloaded through the VPN) as a continuous MPEG-TS
    byte stream, so the player gets plain TS instead of an HLS playlist."""
    last_seq = -1
    misses = 0
    while misses < 15:
        segs, target = _resolve_media_playlist(channel_url)
        new = [(seq, u) for (seq, u) in segs if seq > last_seq]
        if last_seq == -1 and len(new) > 3:
            new = new[-3:]  # start near the live edge, not the whole buffer
        if not new:
            misses += 1
            time.sleep(min(target, 3.0) if target else 1.0)
            continue
        misses = 0
        for seq, seg_url in new:
            try:
                rseg = SESSION.get(seg_url, stream=True, timeout=20)
                if rseg.status_code >= 400:
                    rseg.close()
                    continue
                for chunk in rseg.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
                rseg.close()
                last_seq = seq
            except http_requests.RequestException:
                return
        time.sleep(min(target, 3.0) * 0.4 if target else 1.0)


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


# ---------------------------------------------------------------------------
# /status endpoint
# ---------------------------------------------------------------------------

@app.route("/status")
def status():
    """Show cache state for debugging."""
    cache = _read_cache()
    now = time.time()
    entries = []
    counts = {"ok": 0, "healed": 0, "dead": 0, "expired": 0}
    for original, entry in sorted(cache.items()):
        age = now - entry["timestamp"]
        expired = age >= CACHE_TTL
        s = entry["status"]
        if expired:
            counts["expired"] += 1
        else:
            counts[s] = counts.get(s, 0) + 1
        entries.append({
            "original": original,
            "resolved": entry["resolved_url"],
            "status": s + (" (expired)" if expired else ""),
            "age_min": round(age / 60, 1),
        })
    summary = f"ok={counts['ok']} healed={counts['healed']} dead={counts['dead']} expired={counts['expired']} total={len(cache)}"
    return Response(
        json.dumps({"summary": summary, "streams": entries}, indent=2),
        mimetype="application/json",
    )

# ---------------------------------------------------------------------------
# Background health checker
# ---------------------------------------------------------------------------

def _collect_playlist_urls() -> list[str]:
    """Fetch the M3U playlist and extract all stream URLs."""
    urls = []
    try:
        r = SESSION.get(M3U_URL, timeout=15)
        r.raise_for_status()
        for line in r.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.startswith("http"):
                if stripped.endswith(".m3u8"):
                    urls.append(stripped)
    except Exception as e:
        log.error("Health checker: failed to fetch playlist: %s", e)
    return urls


def _health_check_loop():
    """Probe all playlist URLs periodically and pre-populate the cache."""
    time.sleep(30)  # Let VPN stabilize after boot
    log.info("Health checker started")
    while True:
        try:
            urls = _collect_playlist_urls()
            log.info("Health checker: probing %d stream URLs", len(urls))
            for url in urls:
                _resolve_url(url)
            log.info("Health checker: probe complete")
        except Exception as e:
            log.error("Health checker error: %s", e)
        time.sleep(HEALTH_CHECK_INTERVAL)


_health_thread = threading.Thread(target=_health_check_loop, daemon=True)
_health_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
