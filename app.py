import base64
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

AUDIO_TTL = 20 * 60     # 署名付きURLの有効期限を考慮
SEARCH_TTL = 10 * 60
INFO_TTL = 60 * 60
RELATED_TTL = 30 * 60

# YT_COOKIES_B64 があれば cookies.txt を復元して yt-dlp に渡す（データセンターIP対策）
COOKIE_FILE = None
if os.environ.get("YT_COOKIES_B64"):
    COOKIE_FILE = "/tmp/yt_cookies.txt"
    Path(COOKIE_FILE).write_bytes(base64.b64decode(os.environ["YT_COOKIES_B64"]))


class TTLCache:
    """スレッドセーフな期限付きLRUキャッシュ"""

    def __init__(self, maxsize: int, ttl: float):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            ts, val = hit
            if time.time() - ts > self.ttl:
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return val

    def set(self, key, val):
        with self._lock:
            self._d[key] = (time.time(), val)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


audio_cache = TTLCache(256, AUDIO_TTL)
info_cache = TTLCache(512, INFO_TTL)
search_cache = TTLCache(256, SEARCH_TTL)
related_cache = TTLCache(256, RELATED_TTL)

# 同じ動画を同時に問い合わせたときに、yt-dlpを二重に走らせないためのロック
_key_locks: dict = {}
_key_locks_guard = threading.Lock()


def _lock_for(key):
    with _key_locks_guard:
        if key not in _key_locks:
            _key_locks[key] = threading.Lock()
        return _key_locks[key]


app = FastAPI()

NOISE = re.compile(
    r"[\(\[（【].*?[\)\]）】]|\b(official|music video|mv|lyrics?|audio|hd|4k)\b",
    re.IGNORECASE,
)


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


def thumb(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def resolve_audio(video_id: str, force: bool = False) -> dict:
    """音声ストリームのURLと、取得時に使ったHTTPヘッダー(UA含む)を返す"""
    if not force:
        hit = audio_cache.get(video_id)
        if hit:
            return hit

    with _lock_for(("audio", video_id)):
        if not force:
            hit = audio_cache.get(video_id)  # ロック待ちの間に別スレッドが作った場合
            if hit:
                return hit

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(_opts(format=AUDIO_FORMAT)) as ydl:
            info = ydl.extract_info(url, download=False)

        data = {
            "id": video_id,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": thumb(video_id),
            "stream_url": info["url"],
            "headers": dict(info.get("http_headers") or {}),
        }
        audio_cache.set(video_id, data)
        return data


def fetch_info(video_id: str) -> dict:
    """曲の詳細情報（説明文・再生回数など）"""
    hit = info_cache.get(video_id)
    if hit:
        return hit

    with _lock_for(("info", video_id)):
        hit = info_cache.get(video_id)
        if hit:
            return hit

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

        data = {
            "id": video_id,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": thumb(video_id),
            "description": info.get("description") or "",
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
        }
        info_cache.set(video_id, data)
        return data


def run_search(q: str, n: int) -> list[dict]:
    """ytsearch を実行して軽量な結果を返す（キャッシュ付き）"""
    key = (q.strip().lower(), n)
    hit = search_cache.get(key)
    if hit is not None:
        return hit

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
            "thumbnail": thumb(e["id"]),
        })
    search_cache.set(key, results)
    return results


def fetch_related(video_id: str, n: int = 20) -> list[dict]:
    """
    似た曲: 曲名（括弧やOfficial等を除去）＋チャンネル名で検索して候補を集める。
    YouTube公式の「関連動画」は取れないため、検索ベースの近似です。
    """
    hit = related_cache.get(video_id)
    if hit is not None:
        return hit

    info = fetch_info(video_id)
    base = re.sub(r"\s+", " ", NOISE.sub(" ", info.get("title") or "")).strip()
    channel = (info.get("channel") or "").strip()

    queries = [q for q in (f"{base} {channel}".strip(), f"{channel} 曲".strip()) if q]
    if not queries:
        return []

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        batches = list(pool.map(lambda q: run_search(q, 25), queries))

    seen = {video_id}
    out: list[dict] = []
    for batch in batches:
        for t in batch:
            if t["id"] in seen:
                continue
            d = t.get("duration")
            # 曲として妥当な長さだけ残す（1分未満・15分超を除外）
            if d is not None and not (60 <= d <= 900):
                continue
            seen.add(t["id"])
            out.append(t)

    out = out[:n]
    related_cache.set(video_id, out)
    return out


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
    q = q.strip()[:200]
    if not q:
        raise HTTPException(400, "empty query")
    n = max(1, min(n, 50))
    try:
        return run_search(q, n)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(502, str(e))


@app.get("/api/track/{video_id}")
def track(video_id: str):
    try:
        return fetch_info(video_id)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(502, str(e))


@app.get("/api/related/{video_id}")
def related(video_id: str, n: int = 20):
    n = max(1, min(n, 40))
    try:
        return fetch_related(video_id, n)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(502, str(e))


@app.get("/api/resolve/{video_id}")
def warm(video_id: str):
    """音声URLを事前に解決しておく（再生開始を速くするため）"""
    try:
        resolve_audio(video_id)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


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
