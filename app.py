import re
import json
import uuid
import time
import shutil
import subprocess
import threading
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
COOKIES = BASE_DIR / "cookies.txt"
TEMP = BASE_DIR / "temp"
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="YTE")
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 1800

def job_set(job_id, state, data=None, error=None, step=None):
    with JOBS_LOCK:
        JOBS[job_id] = {"state": state, "data": data or {}, "error": error, "step": step, "ts": time.time()}

def job_step(job_id, step):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["step"] = step
            JOBS[job_id]["ts"] = time.time()

def _prune_jobs():
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL
        with JOBS_LOCK:
            stale = [k for k, v in JOBS.items() if v.get("ts", 0) < cutoff]
            for k in stale:
                del JOBS[k]
        for f in TEMP.glob("music_*"):
            try:
                if f.is_file() and (time.time() - f.stat().st_mtime) > JOB_TTL:
                    f.unlink(missing_ok=True)
                elif f.is_dir() and (time.time() - f.stat().st_mtime) > JOB_TTL:
                    shutil.rmtree(f, ignore_errors=True)
            except Exception:
                pass

threading.Thread(target=_prune_jobs, daemon=True).start()

def seconds_to_ts(s: float) -> str:
    s = round(s, 3)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"

def strip_tracking(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("si", None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=clean_query))

def to_music_url(url: str) -> str:
    url = strip_tracking(url)
    return url.replace("www.youtube.com", "music.youtube.com").replace(
        "youtube.com", "music.youtube.com"
    )

def ytdlp_base():
    cmd = ["yt-dlp"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    return cmd

def extract_plain_text_from_vtt(vtt_path: Path) -> str:
    if not vtt_path.exists():
        return ""
    lines = []
    with open(vtt_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not ('-->' in line or line.startswith('WEBVTT') or line.isdigit()):
                clean = re.sub(r'<[^>]+>', '', line)
                if clean:
                    lines.append(clean)
    return ' '.join(lines)

def get_video_metadata(url: str) -> dict:
    cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout.strip())

def get_music_metadata(url: str) -> dict:
    cmd = ytdlp_base() + ["--dump-json", "--no-playlist", "--extractor-args", "youtube:player_client=web_music", url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout.strip())

def normalize_formats(metadata: dict) -> list:
    formats = metadata.get("formats", [])
    res_map = defaultdict(list)
    container_priority = {"mp4": 1, "m4a": 2, "webm": 3, "mkv": 4, "avi": 5, "mov": 6}
    codec_priority = {"hevc": 1, "h265": 1, "avc1": 2, "h264": 2, "vp9": 3, "av01": 4, "vp8": 5}
    for f in formats:
        height = f.get("height")
        if not height and "p" in f.get("format_note", ""):
            try:
                height = int(f.get("format_note", "").replace("p", ""))
            except:
                height = None
        if height is None or height < 360:
            continue
        size_mb = None
        if f.get("filesize"):
            size_mb = round(f["filesize"] / (1024 * 1024), 1)
        elif f.get("filesize_approx"):
            size_mb = round(f["filesize_approx"] / (1024 * 1024), 1)
        ext = f.get("ext", "").lower()
        vcodec = f.get("vcodec", "").lower()
        codec = vcodec.split('.')[0] if vcodec else "unknown"
        has_both = (f.get("vcodec") != "none" and f.get("acodec") != "none")
        rank = (container_priority.get(ext, 99), codec_priority.get(codec, 99),
                size_mb if size_mb is not None else 1e9, -10 if has_both else 0)
        res_map[height].append({"format_id": f["format_id"], "resolution": f"{height}p",
                                 "ext": ext, "size_mb": size_mb, "rank": rank})
    unique = []
    for height, entries in res_map.items():
        entries.sort(key=lambda x: x["rank"])
        b = entries[0]
        unique.append({"format_id": b["format_id"], "resolution": b["resolution"],
                       "ext": b["ext"], "size_mb": b["size_mb"]})
    unique.sort(key=lambda x: int(x["resolution"].rstrip("p")), reverse=True)
    return unique

def subtitle_worker(job_id, url):
    try:
        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd = ytdlp_base() + ["--skip-download", "--write-auto-subs", "--write-subs",
                                  "--sub-langs", "en", "--sub-format", "vtt", "-o", str(tmp_base), url]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)
        job_set(job_id, "done", {"subtitle_text": sub_text})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def music_download_worker(job_id, url, fmt, is_playlist_item, track_number, playlist_count):
    out_dir = TEMP / f"music_{job_id}"
    out_dir.mkdir(exist_ok=True)
    try:
        music_url = to_music_url(url)

        job_step(job_id, "fetching metadata")
        meta = get_music_metadata(music_url)
        artist = meta.get("artist") or meta.get("uploader") or meta.get("channel") or "Unknown Artist"
        title = meta.get("track") or meta.get("title") or "Unknown Track"
        album = meta.get("album") or meta.get("playlist") or ""

        safe = lambda s: re.sub(r'[\\/*?:"<>|]', '', s).strip()[:80]

        if is_playlist_item and track_number:
            out_template = str(out_dir / f"{track_number:02d} - {safe(artist)} - {safe(title)}.%(ext)s")
        else:
            out_template = str(out_dir / f"{safe(artist)} - {safe(title)}.%(ext)s")

        job_step(job_id, "extracting audio")

        cmd = ytdlp_base() + [
            "--extractor-args", "youtube:player_client=web_music",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--no-playlist",
        ]

        if fmt == "mp3":
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            cmd += ["--remux-video", "m4a"]

        cmd += [
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
            "-o", out_template,
            music_url,
        ]

        if is_playlist_item and track_number:
            cmd += ["--parse-metadata", f"{track_number}:%(track_number)s"]
        if album:
            cmd += ["--parse-metadata", f"{album}:%(album)s"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "yt-dlp failed")

        job_step(job_id, "finalizing")

        ext = "mp3" if fmt == "mp3" else "m4a"
        files = list(out_dir.glob(f"*.{ext}"))
        if not files:
            files = list(out_dir.glob("*.*"))
        if not files:
            raise RuntimeError("No output file produced")

        out_file = files[0]
        job_set(job_id, "done", {
            "file": str(out_file),
            "filename": out_file.name,
        })
    except Exception as ex:
        shutil.rmtree(out_dir, ignore_errors=True)
        job_set(job_id, "error", error=str(ex))

class UrlRequest(BaseModel):
    url: str

class MusicSearchRequest(BaseModel):
    q: str
    limit: int = 12

class MusicDownloadRequest(BaseModel):
    url: str
    fmt: str = "mp3"
    track_number: int = 0
    is_playlist_item: bool = False
    playlist_count: int = 0

def is_playlist_url(url: str) -> bool:
    return "list=" in url and "watch?v=" not in url

@app.post("/api/detect")
async def api_detect(req: UrlRequest):
    url = strip_tracking(req.url)
    return {"is_playlist": is_playlist_url(url)}

@app.post("/api/playlist")
async def api_playlist(req: UrlRequest):
    try:
        url = strip_tracking(req.url)
        cmd = ytdlp_base() + ["--flat-playlist", "--dump-single-json", "--no-warnings", url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip())
        data = json.loads(res.stdout.strip())
        entries = data.get("entries", [])
        videos = []
        for i, e in enumerate(entries):
            vid_id = e.get("id") or e.get("url", "").split("v=")[-1]
            videos.append({
                "index": i, "id": vid_id,
                "title": e.get("title") or e.get("webpage_url_basename") or f"Video {i+1}",
                "thumbnail": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg" if vid_id else None),
                "duration_str": seconds_to_ts(e.get("duration") or 0),
                "url": e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}",
            })
        return {"playlist_title": data.get("title", "Playlist"), "count": len(videos), "videos": videos}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/formats")
async def api_formats(req: UrlRequest):
    try:
        url = strip_tracking(req.url)
        meta = get_video_metadata(url)
        formats = normalize_formats(meta)
        job_id = str(uuid.uuid4())
        job_set(job_id, "running")
        threading.Thread(target=subtitle_worker, args=(job_id, url), daemon=True).start()
        return {
            "video_id": meta.get("id"), "title": meta.get("title"),
            "thumbnail": meta.get("thumbnail"),
            "duration_str": seconds_to_ts(meta.get("duration", 0)),
            "formats": formats, "subtitle_job_id": job_id
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/stream")
async def stream_video(url: str, format_id: str):
    url = strip_tracking(url)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")
    if not re.match(r'^[\w\+]+$', format_id):
        raise HTTPException(400, "Invalid format_id")
    cmd = ytdlp_base() + ["-f", format_id, "-o", "-", "--no-playlist", url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def generate():
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            yield chunk
    return StreamingResponse(generate(), media_type="video/mp4",
                             headers={"Content-Disposition": f"attachment; filename=video_{format_id}.mp4"})

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {"state": "not_found"}))

@app.post("/api/music/search")
async def music_search(req: MusicSearchRequest):
    try:
        search_url = f"https://music.youtube.com/search?q={req.q.replace(' ', '+')}"
        ytmusic_search = f"ytsearchurl{req.limit}:{search_url}"
        cmd = ytdlp_base() + [
            "--flat-playlist", "--dump-single-json", "--no-warnings",
            f"ytsearch{req.limit}:{req.q} official audio site:music.youtube.com"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip())
        data = json.loads(res.stdout.strip())
        entries = data.get("entries", [])
        tracks = []
        for i, e in enumerate(entries):
            vid = e.get("id") or e.get("url", "").split("v=")[-1]
            tracks.append({
                "id": vid,
                "title": e.get("title") or f"Track {i+1}",
                "uploader": e.get("uploader") or e.get("channel") or "Unknown",
                "thumbnail": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None),
                "duration_str": seconds_to_ts(e.get("duration") or 0),
                "url": f"https://music.youtube.com/watch?v={vid}" if vid else "",
            })
        return {"results": tracks}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/music/link")
async def music_link(req: UrlRequest):
    try:
        url = to_music_url(req.url)
        if is_playlist_url(url):
            cmd = ytdlp_base() + [
                "--flat-playlist", "--dump-single-json", "--no-warnings",
                "--extractor-args", "youtube:player_client=web_music",
                url
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip())
            data = json.loads(res.stdout.strip())
            entries = data.get("entries", [])
            tracks = []
            for i, e in enumerate(entries):
                if not e:
                    continue
                vid = e.get("id") or e.get("url", "").split("v=")[-1]
                tracks.append({
                    "id": vid,
                    "index": i,
                    "track_number": i + 1,
                    "title": e.get("title") or f"Track {i+1}",
                    "uploader": e.get("uploader") or e.get("channel") or data.get("title") or "Unknown",
                    "thumbnail": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None),
                    "duration_str": seconds_to_ts(e.get("duration") or 0),
                    "url": f"https://music.youtube.com/watch?v={vid}" if vid else "",
                })
            return {"results": tracks, "title": data.get("title", "Playlist"), "is_album": True, "count": len(tracks)}
        else:
            meta = get_music_metadata(url)
            vid = meta.get("id")
            track = {
                "id": vid,
                "index": 0,
                "track_number": 0,
                "title": meta.get("track") or meta.get("title") or "Unknown Track",
                "uploader": meta.get("artist") or meta.get("uploader") or meta.get("channel") or "Unknown",
                "thumbnail": meta.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None),
                "duration_str": seconds_to_ts(meta.get("duration") or 0),
                "url": url,
            }
            return {"results": [track], "title": track["title"], "is_album": False, "count": 1}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/music/download")
async def music_download_start(req: MusicDownloadRequest):
    url = strip_tracking(req.url)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")
    job_id = str(uuid.uuid4())
    job_set(job_id, "running", step="queued")
    threading.Thread(
        target=music_download_worker,
        args=(job_id, url, req.fmt, req.is_playlist_item, req.track_number, req.playlist_count),
        daemon=True
    ).start()
    return {"job_id": job_id}

@app.get("/api/music/file/{job_id}")
async def music_file(job_id: str):
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id, {}))
    if not job or job.get("state") != "done":
        raise HTTPException(404, "File not ready")
    file_path = Path(job["data"]["file"])
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    filename = job["data"]["filename"]
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/mp4"
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YTE</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #080809;
  --surface: #0f0f12;
  --surface2: #16161b;
  --surface3: #1c1c22;
  --border: #1e1e26;
  --border2: #2a2a35;
  --text: #e2e2ea;
  --muted: #55556a;
  --accent: #c8ff57;
  --accent-dim: rgba(200,255,87,0.07);
  --err: #ff4f4f;
  --err-dim: rgba(255,79,79,0.08);
  --r: 10px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'DM Mono', monospace;
  min-height: 100vh;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 3rem 1rem 6rem;
}
.wrap { width: 100%; max-width: 720px; display: flex; flex-direction: column; }
header { display: flex; align-items: baseline; justify-content: space-between; padding: 0 2px 28px; }
.logo { font-family: 'Syne', sans-serif; font-weight: 800; font-size: 1.05rem; letter-spacing: 0.18em; color: var(--accent); text-transform: uppercase; }
.logo-sub { font-size: 0.65rem; color: var(--muted); letter-spacing: 0.04em; }
.tabs { display: flex; margin-bottom: 1px; border-bottom: 1px solid var(--border); padding: 0 2px; }
.tab-btn {
  background: transparent; border: none; color: var(--muted);
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 0.68rem;
  letter-spacing: 0.12em; text-transform: uppercase;
  padding: 10px 18px 9px; cursor: pointer;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
.input-row { display: flex; align-items: center; }
.url-input {
  flex: 1; background: transparent; border: none; outline: none;
  color: var(--text); font-family: 'DM Mono', monospace; font-size: 0.78rem;
  padding: 18px 20px; caret-color: var(--accent); letter-spacing: 0.01em;
}
.url-input::placeholder { color: var(--muted); }
.run-btn {
  background: var(--accent); color: #080809; border: none;
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 0.68rem;
  letter-spacing: 0.12em; text-transform: uppercase;
  padding: 10px 20px; margin: 8px 10px; border-radius: 6px;
  cursor: pointer; white-space: nowrap;
  transition: opacity 0.15s, transform 0.1s; flex-shrink: 0;
}
.run-btn:hover { opacity: 0.85; }
.run-btn:active { transform: scale(0.97); }
.run-btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
.progress-track { height: 2px; background: var(--border); overflow: hidden; opacity: 0; transition: opacity 0.2s; }
.progress-track.active { opacity: 1; }
.progress-bar { height: 100%; width: 0%; background: var(--accent); border-radius: 2px; }
.progress-bar.indeterminate { width: 40%; animation: slide 1.2s ease-in-out infinite; }
@keyframes slide { 0% { transform: translateX(-120%); } 100% { transform: translateX(340%); } }
.status {
  font-size: 0.7rem; letter-spacing: 0.04em; padding: 12px 20px;
  color: var(--muted); min-height: 42px; display: flex; align-items: center; gap: 8px;
}
.status.ok { color: var(--accent); }
.status.err { color: var(--err); background: var(--err-dim); }
.status-dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.meta-panel { display: none; padding: 20px; gap: 16px; align-items: flex-start; }
.meta-panel.visible { display: flex; }
.thumb { width: 100px; aspect-ratio: 16/9; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: var(--surface2); }
.meta-info { flex: 1; min-width: 0; }
.meta-title { font-family: 'Syne', sans-serif; font-weight: 600; font-size: 0.88rem; line-height: 1.35; margin-bottom: 6px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.meta-duration { font-size: 0.65rem; color: var(--muted); letter-spacing: 0.06em; }
.formats-header { display: grid; grid-template-columns: 1fr 60px 80px 90px; gap: 8px; padding: 10px 20px; font-size: 0.6rem; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.format-row { display: grid; grid-template-columns: 1fr 60px 80px 90px; gap: 8px; padding: 13px 20px; align-items: center; border-bottom: 1px solid var(--border); transition: background 0.12s; }
.format-row:last-child { border-bottom: none; }
.format-row:hover { background: var(--surface2); }
.res-badge { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 0.8rem; }
.ext-badge { font-size: 0.65rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.size-val { font-size: 0.72rem; color: var(--muted); }
.dl-btn { background: transparent; border: 1px solid var(--border2); color: var(--text); font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.06em; padding: 6px 12px; border-radius: 5px; cursor: pointer; transition: border-color 0.15s, color 0.15s, background 0.15s; white-space: nowrap; }
.dl-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.actions-row { display: none; padding: 16px 20px; border-top: 1px solid var(--border); gap: 10px; align-items: center; }
.actions-row.visible { display: flex; }
.sub-btn { background: transparent; border: 1px solid var(--border2); color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.06em; padding: 7px 14px; border-radius: 5px; cursor: pointer; transition: all 0.15s; }
.sub-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.copied-flash { font-size: 0.65rem; color: var(--accent); opacity: 0; transition: opacity 0.3s; letter-spacing: 0.06em; }
.copied-flash.show { opacity: 1; }
.playlist-header { display: none; padding: 16px 20px; border-top: 1px solid var(--border); gap: 10px; align-items: baseline; }
.playlist-header.visible { display: flex; }
.playlist-title { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 0.82rem; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.playlist-count { font-size: 0.65rem; color: var(--muted); letter-spacing: 0.06em; flex-shrink: 0; }
.playlist-list { display: none; border-top: 1px solid var(--border); }
.playlist-list.visible { display: block; }
.pl-item { border-bottom: 1px solid var(--border); }
.pl-item:last-child { border-bottom: none; }
.pl-row { display: flex; align-items: center; gap: 14px; padding: 12px 20px; cursor: pointer; transition: background 0.12s; user-select: none; }
.pl-row:hover { background: var(--surface2); }
.pl-row.open { background: var(--surface2); }
.pl-thumb { width: 72px; aspect-ratio: 16/9; object-fit: cover; border-radius: 4px; flex-shrink: 0; background: var(--surface3); }
.pl-info { flex: 1; min-width: 0; }
.pl-title { font-family: 'Syne', sans-serif; font-weight: 600; font-size: 0.78rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
.pl-dur { font-size: 0.62rem; color: var(--muted); letter-spacing: 0.05em; }
.pl-chevron { width: 16px; height: 16px; flex-shrink: 0; color: var(--muted); transition: transform 0.2s ease; }
.pl-row.open .pl-chevron { transform: rotate(180deg); }
.pl-formats { display: none; background: var(--surface3); border-top: 1px solid var(--border); padding-bottom: 2px; }
.pl-formats.visible { display: block; }
.pl-formats-header { display: grid; grid-template-columns: 1fr 60px 80px 90px; gap: 8px; padding: 8px 20px 8px 106px; font-size: 0.58rem; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; border-bottom: 1px solid var(--border); }
.pl-format-row { display: grid; grid-template-columns: 1fr 60px 80px 90px; gap: 8px; padding: 11px 20px 11px 106px; align-items: center; border-bottom: 1px solid var(--border); transition: background 0.1s; }
.pl-format-row:last-child { border-bottom: none; }
.pl-format-row:hover { background: rgba(255,255,255,0.02); }
.pl-loading { padding: 14px 20px 14px 106px; font-size: 0.68rem; color: var(--muted); display: flex; align-items: center; gap: 8px; }
.spinner { width: 12px; height: 12px; border: 1.5px solid var(--border2); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
.pl-err { padding: 12px 20px 12px 106px; font-size: 0.68rem; color: var(--err); }
.pl-sub-row { display: flex; align-items: center; gap: 10px; padding: 10px 20px 12px 106px; border-top: 1px solid var(--border); }
.pl-sub-btn { background: transparent; border: 1px solid var(--border2); color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.62rem; letter-spacing: 0.06em; padding: 5px 12px; border-radius: 5px; cursor: pointer; transition: all 0.15s; }
.pl-sub-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.pl-copied { font-size: 0.62rem; color: var(--accent); opacity: 0; transition: opacity 0.3s; letter-spacing: 0.06em; }

/* ── music tab ── */
.track-list { display: none; border-top: 1px solid var(--border); }
.track-list.visible { display: block; }
.album-header { display: none; padding: 14px 20px; gap: 10px; align-items: baseline; border-top: 1px solid var(--border); }
.album-header.visible { display: flex; }
.album-title { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 0.82rem; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.album-count { font-size: 0.65rem; color: var(--muted); letter-spacing: 0.06em; flex-shrink: 0; }
.track-row { display: flex; align-items: center; gap: 12px; padding: 11px 20px; border-bottom: 1px solid var(--border); transition: background 0.12s; }
.track-row:last-child { border-bottom: none; }
.track-row:hover { background: var(--surface2); }
.track-num { font-size: 0.62rem; color: var(--muted); width: 20px; text-align: right; flex-shrink: 0; letter-spacing: 0.04em; }
.track-thumb { width: 40px; height: 40px; object-fit: cover; border-radius: 4px; flex-shrink: 0; background: var(--surface3); }
.track-info { flex: 1; min-width: 0; }
.track-title { font-family: 'Syne', sans-serif; font-weight: 600; font-size: 0.76rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
.track-meta { font-size: 0.6rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track-actions { display: flex; gap: 5px; flex-shrink: 0; }
.fmt-btn { background: transparent; border: 1px solid var(--border2); color: var(--text); font-family: 'DM Mono', monospace; font-size: 0.6rem; letter-spacing: 0.06em; padding: 5px 9px; border-radius: 5px; cursor: pointer; transition: all 0.15s; white-space: nowrap; min-width: 36px; text-align: center; }
.fmt-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.fmt-btn.loading { color: var(--muted); border-color: var(--border); cursor: not-allowed; }
.fmt-btn.done { color: var(--accent); border-color: var(--accent); }
.fmt-btn.error { color: var(--err); border-color: var(--err); cursor: default; }

.dl-progress {
  display: none;
  font-size: 0.58rem;
  color: var(--muted);
  letter-spacing: 0.04em;
  padding: 0 20px 10px 72px;
  gap: 6px;
  align-items: center;
}
.dl-progress.visible { display: flex; }
.dl-progress .spinner { width: 9px; height: 9px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="logo">YTE</span>
    <span class="logo-sub">youtube extractor</span>
  </header>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('video')">Video</button>
    <button class="tab-btn" onclick="switchTab('music')">Music</button>
  </div>

  <!-- VIDEO TAB -->
  <div class="tab-panel active" id="tab-video">
    <div class="panel">
      <div class="input-row">
        <input class="url-input" id="url" type="text" placeholder="paste youtube url or playlist" autocomplete="off" spellcheck="false" />
        <button class="run-btn" id="runBtn" onclick="run()">Run</button>
      </div>
      <div class="progress-track" id="track"><div class="progress-bar indeterminate" id="bar"></div></div>
      <div class="status" id="status"></div>
      <div class="meta-panel" id="meta">
        <img class="thumb" id="thumb" src="" alt="" />
        <div class="meta-info">
          <div class="meta-title" id="title"></div>
          <div class="meta-duration" id="dur"></div>
        </div>
      </div>
      <div id="singleFormatsWrap" style="display:none">
        <div class="formats-header"><span>Resolution</span><span>Ext</span><span>Size</span><span></span></div>
        <div id="formatRows"></div>
      </div>
      <div class="actions-row" id="actionsRow">
        <button class="sub-btn" id="subBtn" onclick="copySubtitle()" style="display:none">copy transcript</button>
        <span class="copied-flash" id="copiedFlash">copied</span>
      </div>
      <div class="playlist-header" id="plHeader">
        <span class="playlist-title" id="plTitle"></span>
        <span class="playlist-count" id="plCount"></span>
      </div>
      <div class="playlist-list" id="plList"></div>
    </div>
  </div>

  <!-- MUSIC TAB -->
  <div class="tab-panel" id="tab-music">
    <div class="panel">
      <div class="input-row">
        <input class="url-input" id="musicInput" type="text" placeholder="search or paste music.youtube.com url" autocomplete="off" spellcheck="false" />
        <button class="run-btn" id="musicBtn" onclick="musicRun()">Run</button>
      </div>
      <div class="progress-track" id="musicTrack"><div class="progress-bar indeterminate"></div></div>
      <div class="status" id="musicStatus"></div>
      <div class="album-header" id="albumHeader">
        <span class="album-title" id="albumTitle"></span>
        <span class="album-count" id="albumCount"></span>
      </div>
      <div class="track-list" id="trackList"></div>
    </div>
  </div>
</div>

<script>
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
async function clip(text) {
  try { await navigator.clipboard.writeText(text); } catch {
    const t = document.createElement('textarea');
    t.value = text; document.body.appendChild(t); t.select();
    document.execCommand('copy'); document.body.removeChild(t);
  }
}
function stripTracking(url) {
  try {
    const u = new URL(url);
    u.searchParams.delete('si');
    return u.toString();
  } catch { return url; }
}
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', ['video','music'][i] === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
}

/* ── auto-strip &si= on paste ── */
function attachPasteStrip(inputId) {
  document.getElementById(inputId).addEventListener('paste', e => {
    e.preventDefault();
    const pasted = (e.clipboardData || window.clipboardData).getData('text');
    const clean = stripTracking(pasted.trim());
    const input = e.target;
    const start = input.selectionStart, end = input.selectionEnd;
    const current = input.value;
    input.value = current.slice(0, start) + clean + current.slice(end);
    input.selectionStart = input.selectionEnd = start + clean.length;
    input.dispatchEvent(new Event('input'));
  });
}
attachPasteStrip('url');
attachPasteStrip('musicInput');

/* ══ VIDEO TAB ══ */
let _currentUrl = "", _subtitlePollTimer = null, _subtitleText = "";
const formatCache = {};

function setStatus(type, msg) {
  const el = document.getElementById('status');
  el.className = 'status' + (type ? ' ' + type : '');
  el.innerHTML = msg ? `<span class="status-dot"></span>${msg}` : '';
}
function setLoading(on) {
  document.getElementById('track').classList.toggle('active', on);
  document.getElementById('runBtn').disabled = on;
}
function resetAll() {
  if (_subtitlePollTimer) clearInterval(_subtitlePollTimer);
  _subtitleText = "";
  document.getElementById('subBtn').style.display = 'none';
  document.getElementById('actionsRow').className = 'actions-row';
  document.getElementById('meta').className = 'meta-panel';
  document.getElementById('singleFormatsWrap').style.display = 'none';
  document.getElementById('formatRows').innerHTML = '';
  document.getElementById('plHeader').className = 'playlist-header';
  document.getElementById('plList').className = 'playlist-list';
  document.getElementById('plList').innerHTML = '';
}
async function run() {
  const url = stripTracking(document.getElementById('url').value.trim());
  if (!url) return;
  document.getElementById('url').value = url;
  _currentUrl = url;
  resetAll();
  setLoading(true);
  setStatus('', 'detecting...');
  try {
    const det = await fetch('/api/detect', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url})
    }).then(r => r.json());
    if (det.is_playlist) await loadPlaylist(url);
    else await loadSingle(url);
  } catch (err) {
    setStatus('err', err.message);
  } finally {
    setLoading(false);
  }
}
async function loadSingle(url) {
  setStatus('', 'fetching...');
  const res = await fetch('/api/formats', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url})
  });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  document.getElementById('thumb').src = data.thumbnail || '';
  document.getElementById('title').textContent = data.title || '';
  document.getElementById('dur').textContent = data.duration_str || '';
  document.getElementById('meta').classList.add('visible');
  renderFormatRows(data.formats, document.getElementById('formatRows'), url);
  document.getElementById('singleFormatsWrap').style.display = '';
  document.getElementById('actionsRow').classList.add('visible');
  setStatus('ok', `${data.formats.length} formats`);
  if (data.subtitle_job_id) {
    _subtitlePollTimer = setInterval(async () => {
      try {
        const j = await fetch('/api/job/' + data.subtitle_job_id).then(r => r.json());
        if (j.state === 'done') {
          clearInterval(_subtitlePollTimer);
          if (j.data.subtitle_text) { _subtitleText = j.data.subtitle_text; document.getElementById('subBtn').style.display = ''; }
        } else if (j.state === 'error') clearInterval(_subtitlePollTimer);
      } catch { clearInterval(_subtitlePollTimer); }
    }, 900);
  }
}
async function loadPlaylist(url) {
  setStatus('', 'loading playlist...');
  const res = await fetch('/api/playlist', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url})
  });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  document.getElementById('plTitle').textContent = data.playlist_title || 'Playlist';
  document.getElementById('plCount').textContent = `${data.count} videos`;
  document.getElementById('plHeader').classList.add('visible');
  const list = document.getElementById('plList');
  list.innerHTML = '';
  data.videos.forEach(v => list.appendChild(buildPlItem(v)));
  list.classList.add('visible');
  setStatus('ok', `${data.count} videos`);
}
function buildPlItem(v) {
  const item = document.createElement('div');
  item.className = 'pl-item';
  const row = document.createElement('div');
  row.className = 'pl-row';
  row.innerHTML = `
    <img class="pl-thumb" src="${v.thumbnail || ''}" alt="" loading="lazy" />
    <div class="pl-info">
      <div class="pl-title">${escHtml(v.title)}</div>
      <div class="pl-dur">${v.duration_str || ''}</div>
    </div>
    <svg class="pl-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6l4 4 4-4"/></svg>
  `;
  const formatsBox = document.createElement('div');
  formatsBox.className = 'pl-formats';
  item.appendChild(row);
  item.appendChild(formatsBox);
  let loaded = false, open = false;
  row.addEventListener('click', async () => {
    open = !open;
    row.classList.toggle('open', open);
    formatsBox.classList.toggle('visible', open);
    if (open && !loaded) {
      loaded = true;
      if (formatCache[v.id]) {
        renderPlFormats(formatCache[v.id], formatsBox, v.url, formatCache[v.id + '_sub'] || null);
      } else {
        formatsBox.innerHTML = `<div class="pl-loading"><span class="spinner"></span>fetching formats...</div>`;
        try {
          const res = await fetch('/api/formats', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url: v.url})
          });
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          formatCache[v.id] = data.formats;
          renderPlFormats(data.formats, formatsBox, v.url, null);
          if (data.subtitle_job_id) pollPlSubtitle(data.subtitle_job_id, v.id, formatsBox);
        } catch (err) {
          formatsBox.innerHTML = `<div class="pl-err">${escHtml(err.message)}</div>`;
          loaded = false;
        }
      }
    }
  });
  return item;
}
function renderPlFormats(formats, container, videoUrl, subtitleText) {
  let html = `<div class="pl-formats-header"><span>Resolution</span><span>Ext</span><span>Size</span><span></span></div>`;
  formats.forEach(f => {
    html += `<div class="pl-format-row">
      <span class="res-badge">${f.resolution}</span>
      <span class="ext-badge">${f.ext}</span>
      <span class="size-val">${f.size_mb ? f.size_mb + ' mb' : '—'}</span>
      <button class="dl-btn" onclick="dlStream(${JSON.stringify(videoUrl)}, ${JSON.stringify(f.format_id)})">download</button>
    </div>`;
  });
  html += `<div class="pl-sub-row">
    <button class="pl-sub-btn" style="${subtitleText ? '' : 'display:none'}" onclick="copyPlSub(this, ${JSON.stringify(subtitleText || '')})">copy transcript</button>
    <span class="pl-copied">copied</span>
  </div>`;
  container.innerHTML = html;
}
function pollPlSubtitle(jobId, vidId, formatsBox) {
  const timer = setInterval(async () => {
    try {
      const j = await fetch('/api/job/' + jobId).then(r => r.json());
      if (j.state === 'done') {
        clearInterval(timer);
        if (j.data.subtitle_text) {
          formatCache[vidId + '_sub'] = j.data.subtitle_text;
          const btn = formatsBox.querySelector('.pl-sub-btn');
          if (btn) { btn.setAttribute('onclick', `copyPlSub(this, ${JSON.stringify(j.data.subtitle_text)})`); btn.style.display = ''; }
        }
      } else if (j.state === 'error') clearInterval(timer);
    } catch { clearInterval(timer); }
  }, 900);
}
async function copyPlSub(btn, text) {
  if (!text) return;
  await clip(text);
  const flash = btn.nextElementSibling;
  flash.style.opacity = '1';
  setTimeout(() => flash.style.opacity = '0', 1800);
}
function renderFormatRows(formats, container, videoUrl) {
  container.innerHTML = '';
  formats.forEach(f => {
    const row = document.createElement('div');
    row.className = 'format-row';
    row.innerHTML = `
      <span class="res-badge">${f.resolution}</span>
      <span class="ext-badge">${f.ext}</span>
      <span class="size-val">${f.size_mb ? f.size_mb + ' mb' : '—'}</span>
      <button class="dl-btn" onclick="dlStream(${JSON.stringify(videoUrl)}, ${JSON.stringify(f.format_id)})">download</button>
    `;
    container.appendChild(row);
  });
}
function dlStream(url, formatId) {
  window.location.href = `/api/stream?url=${encodeURIComponent(url)}&format_id=${encodeURIComponent(formatId)}`;
}
async function copySubtitle() {
  if (!_subtitleText) return;
  await clip(_subtitleText);
  const f = document.getElementById('copiedFlash');
  f.classList.add('show');
  setTimeout(() => f.classList.remove('show'), 1800);
}
document.getElementById('url').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });

/* ══ MUSIC TAB ══ */
function setMusicStatus(type, msg) {
  const el = document.getElementById('musicStatus');
  el.className = 'status' + (type ? ' ' + type : '');
  el.innerHTML = msg ? `<span class="status-dot"></span>${msg}` : '';
}
function setMusicLoading(on) {
  document.getElementById('musicTrack').classList.toggle('active', on);
  document.getElementById('musicBtn').disabled = on;
}
function isUrl(s) { return s.startsWith('http://') || s.startsWith('https://'); }

async function musicRun() {
  const raw = document.getElementById('musicInput').value.trim();
  if (!raw) return;
  const q = isUrl(raw) ? stripTracking(raw) : raw;
  if (isUrl(q)) document.getElementById('musicInput').value = q;

  setMusicLoading(true);
  setMusicStatus('', isUrl(q) ? 'resolving...' : 'searching...');
  document.getElementById('trackList').className = 'track-list';
  document.getElementById('trackList').innerHTML = '';
  document.getElementById('albumHeader').className = 'album-header';

  try {
    let tracks = [], albumTitle = '', isAlbum = false;
    if (isUrl(q)) {
      const res = await fetch('/api/music/link', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url: q})
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      tracks = data.results;
      albumTitle = data.title;
      isAlbum = data.is_album;
    } else {
      const res = await fetch('/api/music/search', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({q, limit: 12})
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      tracks = data.results;
      albumTitle = '';
      isAlbum = false;
    }

    if (albumTitle) {
      document.getElementById('albumTitle').textContent = albumTitle;
      document.getElementById('albumCount').textContent = `${tracks.length} track${tracks.length !== 1 ? 's' : ''}`;
      document.getElementById('albumHeader').classList.add('visible');
    }

    renderTracks(tracks, isAlbum, tracks.length);
    setMusicStatus('ok', `${tracks.length} track${tracks.length !== 1 ? 's' : ''}`);
  } catch (err) {
    setMusicStatus('err', err.message);
  } finally {
    setMusicLoading(false);
  }
}

function renderTracks(tracks, isAlbum, total) {
  const list = document.getElementById('trackList');
  list.innerHTML = '';
  tracks.forEach(t => {
    const row = document.createElement('div');
    row.className = 'track-row';
    row.dataset.url = t.url;
    row.dataset.trackNumber = t.track_number || 0;
    row.dataset.isAlbum = isAlbum ? '1' : '0';
    row.dataset.total = total;

    const numHtml = isAlbum
      ? `<span class="track-num">${String(t.track_number || (t.index + 1)).padStart(2,'0')}</span>`
      : '';

    row.innerHTML = `
      ${numHtml}
      <img class="track-thumb" src="${t.thumbnail || ''}" alt="" loading="lazy" />
      <div class="track-info">
        <div class="track-title">${escHtml(t.title)}</div>
        <div class="track-meta">${escHtml(t.uploader)} · ${t.duration_str || ''}</div>
      </div>
      <div class="track-actions">
        <button class="fmt-btn" data-fmt="mp3" onclick="startMusicDl(this)">mp3</button>
        <button class="fmt-btn" data-fmt="m4a" onclick="startMusicDl(this)">m4a</button>
      </div>
    `;

    const progressRow = document.createElement('div');
    progressRow.className = 'dl-progress';
    progressRow.innerHTML = `<span class="spinner"></span><span class="dl-step"></span>`;

    list.appendChild(row);
    list.appendChild(progressRow);
  });
  list.classList.add('visible');
}

async function startMusicDl(btn) {
  const row = btn.closest('.track-row');
  const progressRow = row.nextElementSibling;
  const stepEl = progressRow.querySelector('.dl-step');
  const url = row.dataset.url;
  const fmt = btn.dataset.fmt;
  const trackNumber = parseInt(row.dataset.trackNumber) || 0;
  const isAlbumItem = row.dataset.isAlbum === '1';
  const total = parseInt(row.dataset.total) || 0;

  const allBtns = row.querySelectorAll('.fmt-btn');
  allBtns.forEach(b => { b.disabled = true; b.classList.add('loading'); });
  btn.textContent = '...';
  progressRow.classList.add('visible');
  stepEl.textContent = 'queued';

  try {
    const res = await fetch('/api/music/download', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url, fmt, track_number: trackNumber, is_playlist_item: isAlbumItem, playlist_count: total })
    });
    if (!res.ok) throw new Error(await res.text());
    const { job_id } = await res.json();

    await new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const j = await fetch('/api/job/' + job_id).then(r => r.json());
          if (j.step) stepEl.textContent = j.step;
          if (j.state === 'done') {
            clearInterval(timer);
            resolve(j.data);
          } else if (j.state === 'error') {
            clearInterval(timer);
            reject(new Error(j.error || 'download failed'));
          }
        } catch(e) { clearInterval(timer); reject(e); }
      }, 800);
    }).then(data => {
      btn.textContent = fmt;
      btn.classList.remove('loading');
      btn.classList.add('done');
      stepEl.textContent = 'ready';
      window.location.href = `/api/music/file/${job_id}`;
      setTimeout(() => {
        progressRow.classList.remove('visible');
        btn.classList.remove('done');
        allBtns.forEach(b => { b.disabled = false; b.classList.remove('loading'); });
      }, 3000);
    });
  } catch(err) {
    stepEl.textContent = err.message;
    btn.textContent = fmt;
    btn.classList.remove('loading');
    btn.classList.add('error');
    allBtns.forEach(b => { b.disabled = false; });
    setTimeout(() => {
      progressRow.classList.remove('visible');
      btn.classList.remove('error');
    }, 4000);
  }
}

document.getElementById('musicInput').addEventListener('keydown', e => { if (e.key === 'Enter') musicRun(); });
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
