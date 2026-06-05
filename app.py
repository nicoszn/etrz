import os
import re
import json
import uuid
import asyncio
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel, validator

# ---------------------------------------------------------------------------
# STARTUP & DIRECTORIES
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
COOKIES     = BASE_DIR / "cookies.txt"
DOWNLOADS   = BASE_DIR / "downloads"
CLIPS       = BASE_DIR / "clips"
TEMP        = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# IN-MEMORY JOB STORE with auto‑cleanup
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {
        "state": state,
        "data": data or {},
        "error": error,
        "created_at": time.time(),
        "expires_at": time.time() + 3600   # 1 hour TTL
    }

def cleanup_jobs():
    """Background thread: remove expired jobs every hour."""
    while True:
        time.sleep(3600)
        now = time.time()
        expired = [jid for jid, job in JOBS.items()
                   if job.get("expires_at", 0) < now]
        for jid in expired:
            del JOBS[jid]

threading.Thread(target=cleanup_jobs, daemon=True).start()

# ---------------------------------------------------------------------------
# UTILITIES (same as original, plus security)
# ---------------------------------------------------------------------------
def seconds_to_ts(s: float) -> str:
    s = round(s, 3)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

def _timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) != 3:
        raise ValueError("Timestamp must be in HH:MM:SS format")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds

def ts_to_seconds(ts: str) -> float:
    parts = ts.split(':')
    if len(parts) != 3:
        raise ValueError("Timestamp must be HH:MM:SS[.mmm]")
    h = int(parts[0])
    m = int(parts[1])
    sec_part = parts[2]
    if '.' in sec_part:
        s, ms = sec_part.split('.')
        seconds = float(s) + float(ms) / 1000
    else:
        seconds = float(sec_part)
    return h * 3600 + m * 60 + seconds

def validate_timestamp(ts: str) -> bool:
    pattern = r'^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$'
    if not re.match(pattern, ts):
        return False
    try:
        ts_to_seconds(ts)
        return True
    except:
        return False

def ytdlp_base() -> list:
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

def get_file_size_mb(path: Path) -> float:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else 0.0

def find_existing_download(video_id: str):
    """Return first mp4 in DOWNLOADS matching video_id prefix."""
    for f in DOWNLOADS.glob(f"{video_id}_*.mp4"):
        return f
    return None

def secure_filename(filename: str) -> str:
    """Prevent path traversal."""
    return Path(filename).name

# ---------------------------------------------------------------------------
# NEW: METADATA + FORMAT EXTRACTION (Phase 1)
# ---------------------------------------------------------------------------
def get_video_metadata(url: str) -> dict:
    cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout.strip())

def normalize_formats(metadata: dict) -> list:
    """Extract useful formats with resolution and estimated size."""
    formats = metadata.get("formats", [])
    video_formats = []
    for f in formats:
        # Must have video and audio, or at least video (best we can do)
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            height = f.get("height")
            if not height and "p" in f.get("format_note", ""):
                try:
                    height = int(f.get("format_note", "").replace("p", ""))
                except:
                    height = None
            if height is None:
                continue
            size_mb = None
            if f.get("filesize"):
                size_mb = round(f["filesize"] / (1024 * 1024), 1)
            elif f.get("filesize_approx"):
                size_mb = round(f["filesize_approx"] / (1024 * 1024), 1)
            video_formats.append({
                "format_id": f["format_id"],
                "resolution": f"{height}p",
                "ext": f.get("ext", "mp4"),
                "size_mb": size_mb,
            })
    # Sort by resolution descending
    video_formats.sort(key=lambda x: int(x["resolution"].rstrip("p")), reverse=True)
    return video_formats

# ---------------------------------------------------------------------------
# WORKERS (updated with format_id support)
# ---------------------------------------------------------------------------
def check_worker(job_id: str, url: str):
    """Legacy check – still used to see if file exists."""
    try:
        meta = get_video_metadata(url)
        video_id = meta.get("id", "unknown")
        title = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except:
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"
        thumbnail = meta.get("thumbnail", "")
        existing = find_existing_download(video_id)
        if existing:
            job_set(job_id, "done", {
                "exists": True,
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "thumbnail": thumbnail,
                "filename": existing.name,
                "size_mb": get_file_size_mb(existing),
            })
        else:
            job_set(job_id, "done", {
                "exists": False,
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "thumbnail": thumbnail,
                "filename": None,
                "size_mb": 0,
            })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def download_and_cache_worker(job_id: str, url: str, format_id: str):
    """Download specific format and save to DOWNLOADS (cached)."""
    try:
        meta = get_video_metadata(url)
        video_id = meta.get("id", "unknown")
        title = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except:
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

        # If already exists, just return
        if out_file.exists():
            vtt_path = out_file.with_suffix('.en.vtt')
            sub_text = extract_plain_text_from_vtt(vtt_path) if vtt_path.exists() else ""
            job_set(job_id, "done", {
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "filename": out_file.name,
                "subtitle_text": sub_text,
                "size_mb": get_file_size_mb(out_file),
            })
            return

        dl_cmd = ytdlp_base() + [
            "-f", format_id,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(out_file),
            url
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        # Subtitles
        vtt_path = out_file.with_suffix('.en.vtt')
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "-o", str(out_file.with_suffix('')),
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "video_id": video_id,
            "title": title,
            "duration_sec": duration_sec,
            "duration_str": duration_str,
            "filename": out_file.name,
            "subtitle_text": sub_text,
            "size_mb": get_file_size_mb(out_file),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def subtitle_package_worker(job_id: str, url: str):
    """Return both plain text and segments in one job."""
    try:
        meta = get_video_metadata(url)
        title = meta.get("title", "untitled")

        # Get plain text (vtt)
        tmp_base_vtt = TEMP / f"sub_{job_id}"
        sub_cmd_vtt = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "-o", str(tmp_base_vtt),
            url
        ]
        subprocess.run(sub_cmd_vtt, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base_vtt) + ".en.vtt")
        plain_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        # Get segments (json3)
        tmp_base_json = TEMP / f"segments_{job_id}"
        sub_cmd_json = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "json3",
            "-o", str(tmp_base_json),
            url
        ]
        subprocess.run(sub_cmd_json, capture_output=True, timeout=60)
        json3_path = Path(str(tmp_base_json) + ".en.json3")
        segments = []
        if json3_path.exists():
            with open(json3_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for event in data.get("events", []):
                start_ms = event.get("tStartMs", 0)
                duration_ms = event.get("dDurationMs", 0)
                text_parts = [seg.get("utf8", "") for seg in event.get("segs", [])]
                text = "".join(text_parts).strip()
                if not text or text == "\n":
                    continue
                start_sec = start_ms / 1000.0
                end_sec = (start_ms + duration_ms) / 1000.0
                segments.append({
                    "start": seconds_to_ts(start_sec),
                    "end": seconds_to_ts(end_sec),
                    "text": text
                })
            json3_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "title": title,
            "plain_text": plain_text,
            "segments": segments,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str, mode: str = "normal"):
    """Same as original, but with security."""
    try:
        source_filename = secure_filename(source_filename)
        source = DOWNLOADS / source_filename
        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")

        if not validate_timestamp(ts_from) or not validate_timestamp(ts_to):
            raise ValueError("Invalid timestamps")

        start_seconds = _timestamp_to_seconds(ts_from)
        end_seconds = _timestamp_to_seconds(ts_to)
        if end_seconds <= start_seconds:
            raise ValueError("End must be after start")
        duration = str(end_seconds - start_seconds)

        clip_name = f"clip_{uuid.uuid4().hex[:8]}.mp4"
        out_file = CLIPS / clip_name

        if mode == "9:16":
            video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd = [
                "ffmpeg", "-y", "-threads", "1",
                "-i", str(source),
                "-ss", ts_from, "-t", duration,
                "-vf", video_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_file)
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", ts_from, "-i", str(source),
                "-t", duration, "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(out_file)
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-600:])
        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError("Output missing or empty")

        job_set(job_id, "done", {
            "clip_filename": clip_name,
            "from": ts_from,
            "to": ts_to,
            "mode": mode,
            "size_mb": get_file_size_mb(out_file),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def convert_to_916_worker(job_id: str, filename: str):
    filename = secure_filename(filename)
    source = CLIPS / filename
    if not source.exists():
        job_set(job_id, "error", error="Clip not found")
        return
    stem = source.stem
    new_filename = f"{stem}_9_16.mp4"
    out_file = CLIPS / new_filename
    video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    cmd = [
        "ffmpeg", "-y", "-threads", "1",
        "-i", str(source),
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_file)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[-2000:]}")
        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError("Output missing or empty")
        job_set(job_id, "done", {"new_filename": new_filename, "size_mb": get_file_size_mb(out_file)})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

# ---------------------------------------------------------------------------
# API ENDPOINTS
# ---------------------------------------------------------------------------
class UrlRequest(BaseModel):
    url: str

class FormatRequest(BaseModel):
    url: str

class DownloadCacheRequest(BaseModel):
    url: str
    format_id: str

class CutRequest(BaseModel):
    source_filename: str
    ts_from: str
    ts_to: str
    mode: str = "normal"

    @validator('ts_from', 'ts_to')
    def validate_timestamp(cls, v):
        if not validate_timestamp(v):
            raise ValueError(f"Invalid timestamp format: {v}")
        return v

# Phase 1: get formats
@app.post("/api/formats")
async def api_formats(req: FormatRequest):
    try:
        meta = get_video_metadata(req.url)
        formats = normalize_formats(meta)
        return {
            "video_id": meta.get("id"),
            "title": meta.get("title"),
            "thumbnail": meta.get("thumbnail"),
            "duration_str": seconds_to_ts(meta.get("duration", 0)),
            "formats": formats
        }
    except Exception as e:
        raise HTTPException(400, str(e))

# Phase 2: stream directly (no cache)
@app.get("/api/stream")
async def stream_video(url: str, format_id: str):
    """Stream video directly to browser."""
    # Security: basic URL validation (just ensure it's not empty)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")
    # format_id: only alphanumeric, plus possibly + or - (yt-dlp format ids)
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

# Phase 3: download and cache (save to library)
@app.post("/api/download-cache")
async def api_download_cache(req: DownloadCacheRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=download_and_cache_worker, args=(job_id, req.url, req.format_id), daemon=True).start()
    return {"job_id": job_id}

# Legacy check (optional, but used in UI for "exists" detection)
@app.post("/api/check")
async def api_check(req: UrlRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=check_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

# Unified subtitle package (Phase 4)
@app.post("/api/subtitle-package")
async def api_subtitle_package(req: UrlRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_package_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

# Clipping
@app.post("/api/cut")
async def api_cut(req: CutRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=cut_worker, args=(job_id, req.source_filename, req.ts_from, req.ts_to, req.mode), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/clip/convert-to-916/{filename}")
async def api_convert_to_916(filename: str):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=convert_to_916_worker, args=(job_id, filename), daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

@app.get("/api/downloads/list")
async def list_downloads():
    videos = []
    for f in sorted(DOWNLOADS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        videos.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })
    clips = []
    for f in sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        clips.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })
    return {"videos": videos, "clips": clips}

@app.delete("/api/clips/{filename}")
async def delete_clip(filename: str):
    filename = secure_filename(filename)
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.delete("/api/downloads/{filename}")
async def delete_download(filename: str):
    filename = secure_filename(filename)
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.get("/api/download-file/video/{filename}")
async def download_video(filename: str):
    filename = secure_filename(filename)
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)

@app.get("/api/download-file/clip/{filename}")
async def download_clip(filename: str):
    filename = secure_filename(filename)
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)

# ---------------------------------------------------------------------------
#  HTML
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ClipForge</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f0f11;--surface:#1a1a1f;--surface2:#222228;--surface3:#1e1e24;
  --border:#2e2e38;--accent:#7c5cfc;--accent2:#fc5c7d;
  --text:#e8e8f0;--muted:#6b6b80;--ok:#4caf88;--err:#fc5c5c;--del:#e06c75;
}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:13px;min-height:100vh}
header{padding:15px 32px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}
header h1{font-size:16px;font-weight:700;letter-spacing:.06em;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:10px;color:var(--muted);border:1px solid var(--border);padding:2px 8px;border-radius:4px}
.main{max-width:1200px;margin:40px auto;padding:0 24px;display:flex;flex-direction:column;gap:28px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.card-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.card-header .num{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
.card-header .title{font-size:13px;font-weight:600;flex:1}
.card-body{padding:20px;display:flex;flex-direction:column;gap:14px}
label.lbl{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;display:block;margin-bottom:6px}
input[type=text]{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 13px;border-radius:8px;font-family:inherit;font-size:13px;outline:none;transition:border-color .15s}
input[type=text]:focus{border-color:var(--accent)}
.row{display:flex;gap:8px}
.row input{flex:1}
button{background:var(--accent);color:#fff;border:none;padding:10px 20px;border-radius:8px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;white-space:nowrap}
button:hover{opacity:.85}
button:active{transform:scale(.97)}
button.sec{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
button.warn-btn{background:#3b3b5c;color:#a0a0c0}
button.ok-btn{background:#2a6b4a}
button.delete-btn{background:rgba(224,108,117,0.15);border:1px solid var(--del);color:var(--del);padding:4px 10px;font-size:10px}
button:disabled{opacity:.35;cursor:not-allowed}
.pw{background:var(--border);border-radius:4px;height:3px;overflow:hidden}
.pb{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pb.spin{animation:spin 1.4s ease-in-out infinite;width:35%!important}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(380%)}}
.msg{font-size:11px;color:var(--muted);min-height:16px;line-height:1.5}
.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}
.vinfo{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;display:none;gap:12px;margin-bottom:12px}
.vinfo.show{display:flex}
.vinfo-thumb{width:100px;height:58px;object-fit:cover;border-radius:6px;background:var(--border);flex-shrink:0}
.vinfo-body{flex:1;min-width:0}
.vinfo-title{font-size:12px;font-weight:600;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vinfo-meta{font-size:10px;color:var(--muted);margin-bottom:8px}
.format-table{width:100%;border-collapse:collapse;margin-top:12px}
.format-table th,.format-table td{padding:8px 4px;text-align:left;border-top:1px solid var(--border)}
.format-table th{font-size:10px;color:var(--muted);font-weight:500}
.format-table button{padding:4px 12px;font-size:10px}
.checkbox-row{display:flex;align-items:center;gap:10px;margin-top:8px}
.checkbox-row label{font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer}
.subtitle-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;margin-top:12px;padding:12px;position:relative}
.subtitle-box .lbl{font-size:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.subtitle-box textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-family:inherit;font-size:12px;resize:vertical}
.copy-btn{background:var(--surface2);border:1px solid var(--border);padding:4px 10px;border-radius:6px;font-size:10px;cursor:pointer}
.copy-btn:hover{background:var(--accent);color:#000}
.file-list{display:flex;flex-direction:column;gap:8px;max-height:400px;overflow-y:auto}
.file-item{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.file-item.active{background:var(--surface3);border-color:var(--accent)}
.file-info{flex:1;min-width:150px}
.file-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-meta{font-size:10px;color:var(--muted);margin-top:3px}
.file-actions{display:flex;gap:6px}
.refresh-btn{background:var(--surface2);border:1px solid var(--border);padding:6px 12px;font-size:11px}
.hint{font-size:11px;color:var(--muted);margin-bottom:8px}
.info-box{background:var(--surface2);border-radius:8px;padding:12px;margin-top:12px}
</style>
</head>
<body>
<header>
  <h1>⬡ CLIPFORGE</h1>
  <span class="badge">yt-dlp · FFmpeg · Metadata · Stream</span>
</header>

<div class="main">
  <!-- CARD 1: VIDEO SOURCE (REFACTORED) -->
  <div class="card">
    <div class="card-header">
      <div class="num">1</div>
      <div class="title">Video Source</div>
    </div>
    <div class="card-body">
      <div>
        <label class="lbl">YouTube URL</label>
        <div class="row">
          <input type="text" id="url-input" placeholder="https://youtube.com/watch?v=..."/>
          <button id="analyze-btn" onclick="analyzeVideo()">Analyze</button>
        </div>
      </div>
      <div class="pw"><div class="pb" id="check-pb"></div></div>
      <div class="msg" id="check-msg"></div>

      <div class="vinfo" id="vinfo">
        <img class="vinfo-thumb" id="vinfo-thumb" src="" alt=""/>
        <div class="vinfo-body">
          <div class="vinfo-title" id="vinfo-title"></div>
          <div class="vinfo-meta" id="vinfo-meta"></div>
        </div>
      </div>

      <div id="format-container" style="display:none"></div>

      <div class="checkbox-row">
        <label>
          <input type="checkbox" id="cache-checkbox"/> 💾 Save to Library (cached)
        </label>
        <span style="font-size:10px; color:var(--muted)">Unchecked = stream directly (no disk usage)</span>
      </div>

      <div class="pw"><div class="pb" id="dl-pb"></div></div>
      <div class="msg" id="dl-msg"></div>

      <div style="margin-top:8px">
        <button class="sec" onclick="fetchSubtitlePackage()" style="width:100%">📝 Get Transcript & Segments (Package)</button>
      </div>

      <div id="subtitle-area" style="display:none" class="subtitle-box">
        <div class="lbl">📄 Transcript & Segments
          <div style="display: flex; gap: 8px;">
            <button class="copy-btn" onclick="copyTranscript()">Copy Text</button>
            <button class="copy-btn" onclick="copySegments()">Copy Segments (JSON)</button>
          </div>
        </div>
        <textarea id="subtitle-text" rows="6" readonly placeholder="Subtitle text will appear here..."></textarea>
      </div>
    </div>
  </div>

  <!-- CARD 2: CUT CLIP -->
  <div class="card">
    <div class="card-header">
      <div class="num">2</div>
      <div class="title">Cut Clip</div>
    </div>
    <div class="card-body">
      <div class="hint" id="cut-hint">Select or download a video first.</div>
      <div>
        <label class="lbl">From (HH:MM:SS.mmm)</label>
        <input type="text" id="ts-from" placeholder="00:00:00.000"/>
      </div>
      <div>
        <label class="lbl">To (HH:MM:SS.mmm)</label>
        <input type="text" id="ts-to" placeholder="00:00:30.000"/>
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label class="lbl" style="margin:0">Output format:</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
          <input type="radio" name="cut-mode" value="normal" checked/> Original (stream copy)
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
          <input type="radio" name="cut-mode" value="9:16"/> 9:16 Vertical (1080×1920)
        </label>
      </div>
      <div class="row">
        <button id="cut-btn" onclick="startCut()" disabled>✂ Cut Clip</button>
      </div>
      <div class="pw"><div class="pb" id="cut-pb"></div></div>
      <div class="msg" id="cut-msg"></div>
      <div class="info-box" id="cut-info" style="display:none">
        <div id="cut-title" style="font-size:12px;font-weight:500"></div>
        <div id="cut-mode" style="font-size:11px;color:var(--muted);margin-top:4px"></div>
        <div id="cut-size" style="font-size:11px;color:var(--muted);margin-top:2px"></div>
        <div style="margin-top:10px">
          <button class="ok-btn" id="cut-dl-btn" onclick="downloadClip()" disabled>⬇ Download Clip</button>
        </div>
      </div>
    </div>
  </div>

  <!-- CARD 3: LIBRARY -->
  <div class="card">
    <div class="card-header">
      <div class="num">3</div>
      <div class="title">Library</div>
      <button class="refresh-btn sec" onclick="loadDownloadsList()" style="margin-left:auto">↻ Refresh</button>
    </div>
    <div class="card-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:4px">DOWNLOADED VIDEOS</div>
      <div class="file-list" id="downloads-list"><div class="msg">Loading...</div></div>
      <div style="font-size:11px;color:var(--muted);margin-top:16px;margin-bottom:4px">CLIPS</div>
      <div class="file-list" id="clips-list"><div class="msg">Loading...</div></div>
    </div>
  </div>

  <!-- CARD 4: EXPORT SEGMENTS WITH SCHEMA -->
  <div class="card">
    <div class="card-header">
      <div class="num">4</div>
      <div class="title">Export Segments with Schema</div>
    </div>
    <div class="card-body">
      <div class="hint">Merges the current segmented subtitle (if any) into the hardcoded JSON schema and copies the result.</div>
      <div class="row">
        <button id="export-schema-btn" onclick="exportSegmentsWithSchema()" disabled>📋 Copy JSON (Schema + Segments)</button>
      </div>
      <div class="msg" id="export-msg"></div>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────
let state = {
  videoId: null,
  title: null,
  durationStr: null,
  durationSec: null,
  thumbnail: null,
  filename: null,
  clipFilename: null,
  subtitleText: '',
  subtitleSegments: [],
  formats: []
};

// ── HARDCODED SCHEMA (FULL, FROM ORIGINAL) ─────────────────────────────
const HARDCODED_SCHEMA = {
  "instruction_profile": {
    "system_instruction": "You are a short-form content strategist and clip intelligence engine. Your job is to analyze the provided transcript and produce a ranked list of clip blueprints, each conforming to the output schema defined below.",
    "analysis_protocol": {
      "step_1_signal_scan": "Read the full transcript. Identify moments that contain any of the following viral triggers: Counterintuitive claim ('most people think X, but actually Y'), Negative frame / fear hook ('the reason you're failing at...'), Hard number or stat ('97% of creators never do this'), Identity challenge ('if you're still doing X, you're not serious'), Curiosity gap opener ('what nobody tells you about...'), Conflict or tension beat (disagreement, pushback, revelation), Punchline / payoff moment (must have a clear buildup before it).",
      "step_2_boundary_detection": "For each signal found, determine the natural clip window: Start: 1–3 seconds BEFORE the hook lands (capture the setup). End: 1–2 seconds AFTER the payoff resolves (let it breathe). Duration must be between 20 and 89 seconds for Shorts/TikTok compliance. If a strong signal is buried mid-sentence, walk the timestamp back to a clean sentence start.",
      "step_3_content_type_classification": "For each clip window, classify it as one of: SLICED_FROM_SOURCE (The transcript segment alone carries the full narrative. Timestamps must be precise. hook_text_overlay must amplify, not explain. image_generation_prompt is REQUIRED to visualize the video's core concept) or SYNTHETIC_FROM_SCRATCH (The transcript segment contains a strong idea but the delivery is weak, fragmented, or context-dependent. Asset_assembly_instructions are REQUIRED — write a tighter TTS script that distills the core idea, select a voice profile, and write an image generation prompt that visualizes the concept).",
      "step_4_hook_text_engineering": "Write the hook_text_overlay as the first thing a viewer reads on screen. Rules: Maximum 50 characters. No full stops. No filler words (just, really, very, literally). Must create immediate tension or curiosity. Do NOT summarize the clip — destabilize the viewer.",
      "step_5_metadata_construction": "For each clip: title (Platform-native, front-load the hook, max 100 characters), description (2–3 sentences. First sentence repeats the hook with more context. Second sentence delivers the value proposition. Third is a soft CTA), hashtags (Generate 5–8 platform-ready hashtags. Format rule: You MUST include the '#' prefix, force lowercase, and merge multi-word phrases into a single string with no spaces, e.g., write '#cryptocurrency' and '#inheritancetax', never 'crypto currency' or 'inheritance tax').",
      "step_6_scoring_and_ranking": {
        "formula": "total_score = (hook * 0.35) + (retention * 0.25) + (novelty * 0.20) + (platform_fit * 0.10) + (clarity * 0.10)",
        "weights": {
          "hook_strength": 0.35,
          "retention_arc": 0.25,
          "novelty": 0.20,
          "platform_fit": 0.10,
          "standalone_clarity": 0.10
        },
        "rules": [
          "Sort all clips by total_score descending.",
          "CUTOFF RULE: Exclude any clip with total_score < 6.5.",
          "RENDER LIMIT: Return a maximum of 5 clips. If fewer than 3 clips score above 6.5, return only those that pass."
        ]
      }
    },
    "constraints": [
      "Never invent timestamps. Use only what exists in the provided transcript timeline.",
      "Never hallucinate video IDs. Use the source_video_id passed to you.",
      "If the transcript contains no segments scoring above 6.5, return clips as an empty array and set analysis_summary to explain why no viable clips were found.",
      "Dual-platform targeting: if a clip scores ≥ 8.0, generate two entries — one for YouTube Shorts and one for TikTok — with platform-appropriate metadata variations. TikTok titles skew casual and punchy. Shorts titles skew informational and search-optimized.",
      "The image_generation_prompt field is mandatory for all clip entries, regardless of whether they are SLICED_FROM_SOURCE or SYNTHETIC_FROM_SCRATCH.",
      "Strict Hashtag Constraint: All items within the hashtags array must strictly begin with '#' and contain zero whitespace characters."
    ],
    "expected_output_format": {
      "response_format": "json_object",
      "enforce": "Return a single JSON object. No markdown. No preamble. No explanation.",
      "schema": {
        "source_video_id": "STRING — the video ID this transcript belongs to",
        "analysis_summary": "STRING — 2 sentences max. What was the dominant content theme and which signal type appeared most?",
        "clips": [
          {
            "rank": "INTEGER — 1 is highest scoring",
            "content_generation_type": "SLICED_FROM_SOURCE | SYNTHETIC_FROM_SCRATCH",
            "target_platform": "YouTube Shorts | TikTok",
            "timestamp_start": "HH:MM:SS.mmm — required if SLICED_FROM_SOURCE, null if SYNTHETIC",
            "timestamp_end": "HH:MM:SS.mmm — required if SLICED_FROM_SOURCE, null if SYNTHETIC",
            "hook_text_overlay": "STRING — max 50 chars",
            "score": {
              "hook_strength": "FLOAT 1–10",
              "retention_arc": "FLOAT 1–10",
              "novelty": "FLOAT 1–10",
              "platform_fit": "FLOAT 1–10",
              "standalone_clarity": "FLOAT 1–10",
              "total_score": "FLOAT — weighted composite"
            },
            "publishing_metadata": {
              "title": "STRING — max 100 chars",
              "description": "STRING",
              "hashtags": ["STRING — e.g. '#cryptocurrency', '#wealthmanagement'. Mandatory '#' prefix, all lowercase, no spaces."]
            },
            "asset_assembly_instructions": {
              "text_to_speech_script": "STRING — required if SYNTHETIC, null if SLICED",
              "voice_profile": "STRING — e.g. 'authoritative male, measured pace, slight gravel' — required if SYNTHETIC, null if SLICED",
              "image_generation_prompt": "STRING — REQUIRED FOR ALL GENERATION TYPES. Provide a descriptive visual prompt mapping to the clip concept."
            }
          }
        ]
      }
    }
  },
  "data_payload": {
    "source_video_id": "",
    "transcript": []
  }
};

// ── Helper Functions ───────────────────────────────────────────────────
function setMsg(id, cls, txt) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = txt;
}
function setPb(id, on) {
  const pb = document.getElementById(id);
  if (!pb) return;
  on ? (pb.classList.add('spin'), pb.style.width = '35%') : (pb.classList.remove('spin'), pb.style.width = '0%');
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
async function copyToClipboard(text, successMsg = '✓ Copied to clipboard') {
  try {
    await navigator.clipboard.writeText(text);
    setMsg('dl-msg', 'ok', successMsg);
  } catch (err) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.top = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    setMsg('dl-msg', 'ok', successMsg);
  }
}
function poll(jobId, pbId, msgId, onDone, onFail) {
  setPb(pbId, true);
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/api/job/' + jobId);
      const data = await res.json();
      if (data.state === 'done') {
        clearInterval(interval);
        setPb(pbId, false);
        document.getElementById(pbId).style.width = '100%';
        onDone(data.data);
      } else if (data.state === 'error') {
        clearInterval(interval);
        setPb(pbId, false);
        setMsg(msgId, 'err', '✗ ' + (data.error || 'error'));
        if (onFail) onFail();
      }
    } catch (e) {
      clearInterval(interval);
      setPb(pbId, false);
      setMsg(msgId, 'err', '✗ Network error');
      if (onFail) onFail();
    }
  }, 900);
}

// ── Phase 1: Analyze & Show Formats ────────────────────────────────────
async function analyzeVideo() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  setMsg('check-msg', '', 'Fetching formats...');
  setPb('check-pb', true);
  document.getElementById('format-container').style.display = 'none';
  document.getElementById('vinfo').classList.remove('show');
  document.getElementById('subtitle-area').style.display = 'none';
  try {
    const res = await fetch('/api/formats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    state.videoId = data.video_id;
    state.title = data.title;
    state.durationStr = data.duration_str;
    state.thumbnail = data.thumbnail;
    state.formats = data.formats;

    document.getElementById('vinfo-thumb').src = data.thumbnail || '';
    document.getElementById('vinfo-title').textContent = data.title || 'Untitled';
    document.getElementById('vinfo-meta').textContent = 'Duration: ' + (data.duration_str || 'unknown');
    document.getElementById('vinfo').classList.add('show');

    let html = '<table class="format-table"><thead><tr><th>Quality</th><th>Format</th><th>Est. Size</th><th></th></tr></thead><tbody>';
    for (let f of data.formats) {
      const sizeText = f.size_mb ? f.size_mb + ' MB' : 'unknown';
      html += `<tr>
        <td>${f.resolution}</td>
        <td>${f.ext}</td>
        <td>${sizeText}</td>
        <td><button onclick="startDownloadWithFormat('${f.format_id}')">Select</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('format-container').innerHTML = html;
    document.getElementById('format-container').style.display = 'block';
    setMsg('check-msg', 'ok', `✓ ${data.formats.length} formats available`);
  } catch (err) {
    setMsg('check-msg', 'err', err.message);
  } finally {
    setPb('check-pb', false);
  }
}

function startDownloadWithFormat(formatId) {
  const url = document.getElementById('url-input').value.trim();
  const cache = document.getElementById('cache-checkbox').checked;
  if (cache) {
    setMsg('dl-msg', '', `Downloading format ${formatId} & saving to library...`);
    setPb('dl-pb', true);
    fetch('/api/download-cache', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format_id: formatId })
    })
      .then(res => res.json())
      .then(data => {
        poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
          state.filename = d.filename;
          state.subtitleText = d.subtitle_text || '';
          setMsg('dl-msg', 'ok', `✓ Saved: ${d.filename} (${d.size_mb} MB)`);
          if (state.subtitleText) {
            document.getElementById('subtitle-area').style.display = 'block';
            document.getElementById('subtitle-text').value = state.subtitleText;
          }
          document.getElementById('cut-btn').disabled = false;
          document.getElementById('cut-hint').textContent = 'Ready: ' + d.filename;
          loadDownloadsList();
        });
      })
      .catch(err => {
        setPb('dl-pb', false);
        setMsg('dl-msg', 'err', err.message);
      });
  } else {
    window.location.href = `/api/stream?url=${encodeURIComponent(url)}&format_id=${formatId}`;
  }
}

// ── Phase 4: Unified subtitle package ──────────────────────────────────
async function fetchSubtitlePackage() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL first'); return; }
  setMsg('dl-msg', '', 'Fetching subtitles (plain text + segments)...');
  setPb('dl-pb', true);
  try {
    const res = await fetch('/api/subtitle-package', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      state.subtitleText = d.plain_text || '';
      state.subtitleSegments = d.segments || [];
      document.getElementById('subtitle-area').style.display = 'block';
      document.getElementById('subtitle-text').value = state.subtitleText || '(no transcript found)';
      document.getElementById('export-schema-btn').disabled = false;
      setMsg('dl-msg', 'ok', `✓ Got ${state.subtitleSegments.length} segments`);
    });
  } catch (err) {
    setPb('dl-pb', false);
    setMsg('dl-msg', 'err', err.message);
  }
}

function copyTranscript() {
  if (state.subtitleText) copyToClipboard(state.subtitleText, 'Transcript copied');
  else setMsg('dl-msg', 'err', 'No transcript available');
}

function copySegments() {
  if (state.subtitleSegments.length) {
    copyToClipboard(JSON.stringify(state.subtitleSegments, null, 2), `${state.subtitleSegments.length} segments copied`);
  } else {
    setMsg('dl-msg', 'err', 'No segments loaded');
  }
}

function exportSegmentsWithSchema() {
  if (!state.subtitleSegments.length) {
    setMsg('export-msg', 'err', 'No segments to export');
    return;
  }
  const exportObj = JSON.parse(JSON.stringify(HARDCODED_SCHEMA));
  exportObj.data_payload.transcript = state.subtitleSegments;
  exportObj.data_payload.source_video_id = state.videoId || 'unknown';
  copyToClipboard(JSON.stringify(exportObj, null, 2), '✓ Schema + segments copied');
}

// ── Clipping (unchanged logic from original) ───────────────────────────
async function startCut() {
  if (!state.filename) { setMsg('cut-msg', 'err', '✗ No video selected'); return; }
  const from = document.getElementById('ts-from').value.trim();
  const to = document.getElementById('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg', 'err', '✗ Both timestamps required'); return; }
  const tsRe = /^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$/;
  if (!tsRe.test(from) || !tsRe.test(to)) { setMsg('cut-msg', 'err', '✗ Invalid timestamp format'); return; }
  const cutMode = document.querySelector('input[name="cut-mode"]:checked')?.value || 'normal';
  state.clipFilename = null;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('cut-info').style.display = 'none';
  setMsg('cut-msg', '', cutMode === '9:16' ? 'Cutting & converting to 9:16...' : 'Cutting clip...');
  setPb('cut-pb', true);
  try {
    const res = await fetch('/api/cut', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_filename: state.filename, ts_from: from, ts_to: to, mode: cutMode })
    });
    const data = await res.json();
    poll(data.job_id, 'cut-pb', 'cut-msg', (d) => {
      state.clipFilename = d.clip_filename;
      document.getElementById('cut-btn').disabled = false;
      setMsg('cut-msg', 'ok', `✓ Done — ${d.from} → ${d.to}`);
      document.getElementById('cut-title').textContent = d.clip_filename;
      document.getElementById('cut-mode').textContent = 'Mode: ' + (d.mode === '9:16' ? '9:16 Vertical' : 'Original (stream copy)');
      document.getElementById('cut-size').textContent = 'Size: ' + (d.size_mb ? d.size_mb.toFixed(2) + ' MB' : 'unknown');
      document.getElementById('cut-info').style.display = 'block';
      document.getElementById('cut-dl-btn').disabled = false;
      loadDownloadsList();
    });
  } catch (err) {
    document.getElementById('cut-btn').disabled = false;
    setPb('cut-pb', false);
    setMsg('cut-msg', 'err', err.message);
  }
}
function downloadClip() {
  if (state.clipFilename) window.location.href = '/api/download-file/clip/' + encodeURIComponent(state.clipFilename);
}

// ── Library functions ──────────────────────────────────────────────────
function selectVideo(filename) {
  state.filename = filename;
  document.getElementById('cut-btn').disabled = false;
  document.getElementById('cut-hint').textContent = 'Ready: ' + filename;
  setMsg('cut-msg', '');
  document.getElementById('cut-info').style.display = 'none';
  document.querySelectorAll('#downloads-list .file-item').forEach(el => {
    el.classList.toggle('active', el.dataset.fn === filename);
  });
}
async function deleteFile(filename) {
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/downloads/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) {
      if (state.filename === filename) {
        state.filename = null;
        document.getElementById('cut-btn').disabled = true;
        document.getElementById('cut-hint').textContent = 'Select or download a video first.';
      }
      loadDownloadsList();
    } else {
      const err = await res.json();
      setMsg('dl-msg', 'err', `✗ Delete failed: ${err.detail}`);
    }
  } catch(e) { setMsg('dl-msg', 'err', '✗ Delete request failed'); }
}
async function deleteClip(filename) {
  if (!confirm(`Delete clip "${filename}"?`)) return;
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) loadDownloadsList();
    else alert('Delete failed');
  } catch(e) { alert('Delete failed'); }
}
async function convertTo916(filename) {
  if (!confirm(`Convert "${filename}" to 9:16 vertical? A new clip will be created.`)) return;
  setMsg('dl-msg', '', `Converting ${filename}...`);
  try {
    const res = await fetch('/api/clip/convert-to-916/' + encodeURIComponent(filename), { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      setMsg('dl-msg', 'ok', `✓ Converted: ${d.new_filename} (${d.size_mb} MB)`);
      loadDownloadsList();
    });
  } catch(e) {
    setMsg('dl-msg', 'err', 'Conversion failed: ' + e.message);
  }
}
async function loadDownloadsList() {
  const videoContainer = document.getElementById('downloads-list');
  const clipContainer = document.getElementById('clips-list');
  videoContainer.innerHTML = '<div class="msg">Loading...</div>';
  clipContainer.innerHTML = '<div class="msg">Loading...</div>';
  try {
    const res = await fetch('/api/downloads/list');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.videos || !data.videos.length) {
      videoContainer.innerHTML = '<div class="msg">No downloaded videos yet.</div>';
    } else {
      videoContainer.innerHTML = data.videos.map(f => `
        <div class="file-item" data-fn="${escapeHtml(f.filename)}" onclick="selectVideo('${escapeHtml(f.filename)}')">
          <div class="file-info">
            <div class="file-name">${escapeHtml(f.filename)}</div>
            <div class="file-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="file-actions" onclick="event.stopPropagation()">
            <a href="/api/download-file/video/${encodeURIComponent(f.filename)}" class="sec" style="padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--text);border:1px solid var(--border);">⬇ Download</a>
            <button class="delete-btn" onclick="deleteFile('${escapeHtml(f.filename)}')">🗑 Delete</button>
          </div>
        </div>
      `).join('');
      if (state.filename) {
        document.querySelectorAll('#downloads-list .file-item').forEach(el => {
          el.classList.toggle('active', el.dataset.fn === state.filename);
        });
      }
    }
    if (!data.clips || !data.clips.length) {
      clipContainer.innerHTML = '<div class="msg">No clips yet.</div>';
    } else {
      clipContainer.innerHTML = data.clips.map(f => `
        <div class="file-item">
          <div class="file-info">
            <div class="file-name">${escapeHtml(f.filename)}</div>
            <div class="file-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="file-actions">
            <button class="sec" style="padding:5px 12px;border-radius:6px;" onclick="convertTo916('${escapeHtml(f.filename)}')">9:16</button>
            <a href="/api/download-file/clip/${encodeURIComponent(f.filename)}" class="sec" style="padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--text);border:1px solid var(--border);">⬇ Download</a>
            <button class="delete-btn" onclick="deleteClip('${escapeHtml(f.filename)}')">🗑 Delete</button>
          </div>
        </div>
      `).join('');
    }
  } catch(e) {
    videoContainer.innerHTML = '<div class="msg err">Failed to load library</div>';
    clipContainer.innerHTML = '<div class="msg err">Failed to load library</div>';
  }
}

// ── Initial load & event binding ───────────────────────────────────────
document.getElementById('url-input').addEventListener('keydown', e => { if (e.key === 'Enter') analyzeVideo(); });
loadDownloadsList();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

