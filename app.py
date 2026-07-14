"""
MAL Direct stream-finder backend.

Ports the logic from anime_stream_finder.py into a small, cached HTTP API so
the browser (index.html "MAL Direct" panel) gets fast, repeat-friendly
lookups instead of re-downloading the whole index.json (which can be a few
MB) on every request.

Endpoints
---------
GET  /api/search?q=<name>&limit=25
    -> {"results": [{"mal_id": 21, "anime_name": "One Piece", "anime_url": "..."}]}

GET  /api/stream?mal_id=<id>&episode=<n>
    -> {
         "mal_id": 21, "anime_name": "One Piece", "anime_url": "...",
         "episode": 1, "episode_title": "...", "episode_url": "...",
         "streams": {"sub": {"server": {"key": "url", ...}}, "dub": {...}},
         "m3u8": ["https://...", ...]
       }

GET  /api/proxy?url=<url>&referer=<referer>&origin=<origin>
    Server-side passthrough used by the player for streams that require a
    specific Referer/Origin. Browsers block JS from setting those headers
    directly (they're on the fetch "forbidden header" list), so the page
    asks this endpoint to fetch the URL instead, where Referer/Origin are
    just ordinary headers. The upstream response (manifest, segment, or
    video file) is streamed straight back with permissive CORS and Range
    support so hls.js / <video> can consume it exactly like a direct URL.
    Supports GET and HEAD (HEAD is used by the frontend to sniff
    Content-Type when auto-detecting a stream's category).

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000

Deploy on Render (from GitHub):
    Build command : pip install -r requirements.txt
    Start command : uvicorn app:app --host 0.0.0.0 --port $PORT
(see render.yaml for a ready-to-use Blueprint)
"""

import time
import asyncio
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

INDEX_URL = "https://raw.githubusercontent.com/srtfile/alist/refs/heads/main/index.json"

# The player page lives right next to this file and is served at "/".
FRONTEND_FILE = Path(__file__).parent / "index.html"

INDEX_TTL_SECONDS = 30 * 60   # index.json rarely changes -> cache 30 min
ANIME_TTL_SECONDS = 5 * 60    # per-title episode data -> cache 5 min
HTTP_TIMEOUT = 15.0

app = FastAPI(title="MAL Direct Stream Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
)

DEFAULT_PROXY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Shared async client (connection pooling / keep-alive) for speed.
_client: Optional[httpx.AsyncClient] = None

# ── In-memory caches ──────────────────────────────────────────────────────
_index_cache: dict[str, Any] = {"data": None, "by_id": None, "fetched_at": 0.0}
_index_lock = asyncio.Lock()

_anime_cache: dict[str, tuple[float, Any]] = {}   # raw_url -> (fetched_at, data)
_anime_locks: dict[str, asyncio.Lock] = {}


@app.on_event("startup")
async def startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"Accept-Encoding": "gzip, deflate"},
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    if _client:
        await _client.aclose()


# ── Index handling ──────────────────────────────────────────────────────
async def get_index() -> dict[str, Any]:
    """Returns {"list": [...], "by_id": {mal_id: entry}}, cached with TTL."""
    now = time.time()
    if _index_cache["data"] is not None and (now - _index_cache["fetched_at"]) < INDEX_TTL_SECONDS:
        return {"list": _index_cache["data"], "by_id": _index_cache["by_id"]}

    async with _index_lock:
        # Re-check after acquiring the lock (another request may have refreshed it).
        now = time.time()
        if _index_cache["data"] is not None and (now - _index_cache["fetched_at"]) < INDEX_TTL_SECONDS:
            return {"list": _index_cache["data"], "by_id": _index_cache["by_id"]}

        assert _client is not None
        r = await _client.get(INDEX_URL)
        r.raise_for_status()
        raw = r.json()
        by_id = {entry["mal_id"]: entry for entry in raw if "mal_id" in entry}
        _index_cache["data"] = raw
        _index_cache["by_id"] = by_id
        _index_cache["fetched_at"] = time.time()
        return {"list": raw, "by_id": by_id}


async def get_anime_data(raw_url: str) -> Any:
    now = time.time()
    cached = _anime_cache.get(raw_url)
    if cached and (now - cached[0]) < ANIME_TTL_SECONDS:
        return cached[1]

    lock = _anime_locks.setdefault(raw_url, asyncio.Lock())
    async with lock:
        now = time.time()
        cached = _anime_cache.get(raw_url)
        if cached and (now - cached[0]) < ANIME_TTL_SECONDS:
            return cached[1]

        assert _client is not None
        r = await _client.get(raw_url)
        r.raise_for_status()
        data = r.json()
        _anime_cache[raw_url] = (time.time(), data)
        return data


def extract_streams(episode: dict, types=("sub", "dub", "hsub")) -> dict:
    """Same logic as anime_stream_finder.py's extract_streams()."""
    out: dict[str, Any] = {}
    for t in types:
        data = episode.get(t)
        if not data:
            continue
        servers = {}
        for server, links in data.items():
            clean = {k: v for k, v in links.items() if v}
            if clean:
                servers[server] = clean
        if servers:
            out[t] = servers
    return out


def collect_m3u8(streams: dict) -> list[str]:
    return [
        url
        for servers in streams.values()
        for links in servers.values()
        for key, url in links.items()
        if key.endswith("_m3u8")
    ]


# ── Routes ──────────────────────────────────────────────────────────────
@app.get("/")
async def serve_index():
    if not FRONTEND_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found next to app.py")
    return FileResponse(FRONTEND_FILE, media_type="text/html")


@app.get("/index.html")
async def serve_index_alias():
    return await serve_index()


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(25, ge=1, le=100)):
    idx = await get_index()
    ql = q.lower()
    results = [
        {
            "mal_id": e.get("mal_id"),
            "anime_name": e.get("anime_name"),
            "anime_url": e.get("anime_url"),
        }
        for e in idx["list"]
        if ql in (e.get("anime_name") or "").lower()
    ][:limit]
    return {"results": results}


@app.get("/api/stream")
async def stream(mal_id: int = Query(...), episode: int = Query(1, ge=1)):
    idx = await get_index()
    entry = idx["by_id"].get(mal_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No anime found with MAL ID {mal_id}")

    raw_url = entry.get("raw_url")
    if not raw_url:
        raise HTTPException(status_code=502, detail="Entry has no raw_url")

    try:
        anime_data = await get_anime_data(raw_url)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch episode data: {e}")

    episodes = anime_data.get("episodes", [])
    if not episodes:
        raise HTTPException(status_code=404, detail="No episodes in data")

    idx_ep = episode - 1
    if idx_ep < 0 or idx_ep >= len(episodes):
        raise HTTPException(
            status_code=404,
            detail=f"Episode {episode} out of range (1-{len(episodes)})",
        )

    ep_data = episodes[idx_ep]
    streams = extract_streams(ep_data)

    return {
        "mal_id": mal_id,
        "anime_name": entry.get("anime_name"),
        "anime_url": entry.get("anime_url"),
        "episode": episode,
        "episode_title": ep_data.get("episode_title", f"Episode {episode}"),
        "episode_url": ep_data.get("episode_url"),
        "streams": streams,
        "m3u8": collect_m3u8(streams),
    }


# ── Referer/Origin passthrough proxy ──────────────────────────────────────
# Browsers treat Referer/Origin as "forbidden request headers" — JS running
# in the page can never set them on a fetch/XHR, no matter what the code
# tries. The only real fix is to do the fetch on the server, where they're
# ordinary headers, and stream the result back to the browser. hls.js is
# told (via xhrSetup) to reroute every manifest/segment/key request through
# this endpoint whenever a Referer or Origin is configured, and <video>
# points straight at it for MP4/DASH. Range is forwarded both ways so
# seeking still works.
PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
    "last-modified",
    "etag",
)


@app.api_route("/api/proxy", methods=["GET", "HEAD"])
async def proxy(
    request: Request,
    url: str = Query(..., min_length=1, description="Absolute URL to fetch"),
    referer: Optional[str] = Query(None),
    origin: Optional[str] = Query(None),
):
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url must be an absolute http(s) URL")

    fwd_headers: dict[str, str] = {
        "User-Agent": request.headers.get("user-agent") or DEFAULT_PROXY_UA,
        "Accept": request.headers.get("accept", "*/*"),
    }
    if referer:
        fwd_headers["Referer"] = referer
    if origin:
        fwd_headers["Origin"] = origin
    range_header = request.headers.get("range")
    if range_header:
        fwd_headers["Range"] = range_header

    assert _client is not None
    try:
        upstream_req = _client.build_request(request.method, url, headers=fwd_headers)
        upstream = await _client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")

    resp_headers = {
        h: upstream.headers[h] for h in PASSTHROUGH_RESPONSE_HEADERS if h in upstream.headers
    }
    resp_headers.setdefault("Cache-Control", "no-store")
    media_type = upstream.headers.get("content-type")

    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=resp_headers, media_type=media_type)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(), status_code=upstream.status_code, headers=resp_headers, media_type=media_type
    )