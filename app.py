import base64
import os
import threading
import time
from pathlib import Path

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
CACHE_TTL = 20 * 60  # 署名付きURLの有効期限を考慮して20分
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

# YT_COOKIES_B64 があれば cookies.txt を復元して yt-dlp に渡す（データセンターIP対策）
COOKIE_FILE = None
if os.environ.get("YT_COOKIES_B64"):
    COOKIE_FILE = "/tmp/yt_cookies.txt"
    Path(COOKIE_FILE).write_bytes(base64.b64decode(os.environ["YT_COOKIES_B64"]))

app = FastAPI()
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _opts(**extra):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    opts.update(extra)
    return opts


def resolve_audio(video_id: str, force: bool = False) -> dict:
    """音声ストリームのURLと、取得時に使ったHTTPヘッダー(UA含む)を返す"""
    now = time.time()
    if not force:
        with _lock:
            hit = _cache.get(video_id)
            if hit and now - hit[0] < CACHE_TTL:
                return hit[1]

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(_opts(format=AUDIO_FORMAT)) as ydl:
        info = ydl.extract_info(url, download=False)

    data = {
        "id": video_id,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "stream_url": info["url"],
        "headers": dict(info.get("http_headers") or {}),
    }
    with _lock:
        _cache[video_id] = (now, data)
    return data


def open_upstream(video_id: str, range_header: str | None):
    """yt-dlpと同じヘッダーでYouTubeの音声を開く。失敗時は1回URLを取り直す"""
    status = None
    for attempt in range(2):
        data = resolve_audio(video_id, force=attempt > 0)
        headers = dict(data["headers"])
        if range_header:
            headers["Range"] = range_header

        client = httpx.Client(timeout=30, follow_redirects=True)
        try:
            resp = client.send(
                client.build_request("GET", data["stream_url"], headers=headers),
                stream=True,
            )
        except httpx.HTTPError:
            client.close()
            status = 502
            continue

        status = resp.status_code
        if status < 400:
            return client, resp
        resp.close()
        client.close()

    raise HTTPException(502, f"upstream error: {status}")


@app.get("/api/search")
def search(q: str, n: int = 25):
    n = max(1, min(n, 50))
    with yt_dlp.YoutubeDL(_opts(extract_flat="in_playlist")) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)

    results = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        results.append({
            "id": e["id"],
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"),
            "duration": e.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
        })
    return results


@app.get("/stream/{video_id}")
def stream(video_id: str, request: Request):
    client, resp = open_upstream(video_id, request.headers.get("range"))

    fwd = {}
    for k in ("content-type", "content-length", "content-range", "accept-ranges"):
        if k in resp.headers:
            fwd[k] = resp.headers[k]

    def body():
        try:
            for chunk in resp.iter_bytes(64 * 1024):
                yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(body(), status_code=resp.status_code, headers=fwd)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
