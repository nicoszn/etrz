import os
import re
import json
import uuid
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, validator

# ---------------------------------------------------------------------------
# STARTUP
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
# IN-MEMORY JOB STORE
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

# ---------------------------------------------------------------------------
# UTILITIES
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

# ---------------------------------------------------------------------------
# WORKERS (from app-1)
# ---------------------------------------------------------------------------
def check_worker(job_id: str, url: str):
    """Check metadata + whether file already exists, without downloading."""
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])

        meta       = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id   = meta.get("id", "unknown")
        title      = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except (TypeError, ValueError):
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"
        thumbnail    = meta.get("thumbnail", "")

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

def download_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])

        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id = meta.get("id", "unknown")
        title = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except (TypeError, ValueError):
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

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
            "-S", "vcodec:h264,res,acodec:aac",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(out_file),
            url
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

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

def subtitle_only_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "-o", str(tmp_base),
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "title": title,
            "subtitle_text": sub_text,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def subtitle_segments_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"segments_{job_id}"
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "json3",
            "-o", str(tmp_base),
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)

        json3_path = Path(str(tmp_base) + ".en.json3")
        if not json3_path.exists():
            raise RuntimeError("No JSON3 subtitles found (English).")

        with open(json3_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        segments = []
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
            "segments": segments,
            "title": title,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str, mode: str = "normal"):
    try:
        if not validate_timestamp(ts_from):
            raise ValueError(
                f"Invalid start timestamp: {ts_from}. "
                "Use HH:MM:SS or HH:MM:SS.mmm"
            )

        if not validate_timestamp(ts_to):
            raise ValueError(
                f"Invalid end timestamp: {ts_to}. "
                "Use HH:MM:SS or HH:MM:SS.mmm"
            )

        source = DOWNLOADS / source_filename

        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")

        start_seconds = _timestamp_to_seconds(ts_from)
        end_seconds = _timestamp_to_seconds(ts_to)

        if end_seconds <= start_seconds:
            raise ValueError("End timestamp must be greater than start timestamp")

        duration = str(end_seconds - start_seconds)

        # Simple UUID-based filename (from app-1)
        clip_name = f"clip_{uuid.uuid4().hex[:8]}.mp4"
        out_file = CLIPS / clip_name

        if mode == "9:16":
            # Letterbox pipeline (Fits the entire source video, adds black bars)
            video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd = [
                "ffmpeg", "-y",
                "-threads", "1",
                "-i", str(source),
                "-ss", ts_from,
                "-t", duration,
                "-vf", video_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_file)
            ]
        else:
            # Normal stream copy mode
            cmd = [
                "ffmpeg", "-y",
                "-ss", ts_from,
                "-i", str(source),
                "-t", duration,
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(out_file)
            ]

        # Single execution point for both modes
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            if result.returncode == -9:
                raise RuntimeError("FFmpeg process was killed by the system (Out of Memory). Try upgrading server RAM or adding a swap file.")
            raise RuntimeError(result.stderr.strip()[-600:])

        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError(f"Output file missing or empty. FFmpeg stderr: {result.stderr.strip()[-400:]}")

        # Return combined fields from app-1 (mode, size_mb) and app-2 (from, to)
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
    try:
        source = CLIPS / filename
        if not source.exists():
            raise RuntimeError(f"Clip not found: {filename}")

        # Create new filename with suffix
        stem = source.stem
        new_filename = f"{stem}_9_16.mp4"
        out_file = CLIPS / new_filename

        # Single‑step re‑encode with scale+pad filter
        video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source),
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_file)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)  # 3 minutes max for small clips

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError("Output file missing or empty")

        job_set(job_id, "done", {"new_filename": new_filename, "size_mb": get_file_size_mb(out_file)})

    except Exception as ex:
        job_set(job_id, "error", error=str(ex))



# ---------------------------------------------------------------------------
# INLINE HTML (updated with check button and full integration like app-1)
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

/* Video info box */
.vinfo{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;display:none;gap:12px}
.vinfo.show{display:flex}
.vinfo-thumb{width:100px;height:58px;object-fit:cover;border-radius:6px;background:var(--border);flex-shrink:0}
.vinfo-body{flex:1;min-width:0}
.vinfo-title{font-size:12px;font-weight:600;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vinfo-meta{font-size:10px;color:var(--muted);margin-bottom:8px}
.vinfo-actions{display:flex;gap:6px;flex-wrap:wrap}

/* Exists banner */
.exists-banner{background:rgba(76,175,136,0.1);border:1px solid rgba(76,175,136,0.3);border-radius:8px;padding:10px 14px;font-size:11px;color:var(--ok);display:none;align-items:center;gap:8px}
.exists-banner.show{display:flex}
.exists-banner button{padding:5px 12px;font-size:11px}

.subtitle-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;margin-top:12px;padding:12px;position:relative}
.subtitle-box .lbl{font-size:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.subtitle-box textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-family:inherit;font-size:12px;resize:vertical}
.copy-btn{background:var(--surface2);border:1px solid var(--border);padding:4px 10px;border-radius:6px;font-size:10px;cursor:pointer}
.copy-btn:hover{background:var(--accent);color:#000}

.file-list{display:flex;flex-direction:column;gap:8px;max-height:400px;overflow-y:auto}
.file-item{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.file-info{flex:1;min-width:150px}
.file-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-meta{font-size:10px;color:var(--muted);margin-top:3px}
.file-actions{display:flex;gap:6px}
.refresh-btn{background:var(--surface2);border:1px solid var(--border);padding:6px 12px;font-size:11px}
</style>
</head>
<body>
<header>
  <h1>⬡ CLIPFORGE</h1>
  <span class="badge">yt-dlp · FFmpeg · Subtitles</span>
</header>

<div class="main">

  <!-- CARD 1: VIDEO SOURCE -->
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
          <button id="check-btn" onclick="startCheck()">Check</button>
        </div>
      </div>
      <div class="pw"><div class="pb" id="check-pb"></div></div>
      <div class="msg" id="check-msg"></div>

      <div class="exists-banner" id="exists-banner">
        <span>✓ Already downloaded</span>
        <button class="sec" onclick="useExisting()">Use This Video</button>
        <button class="warn-btn" onclick="forceDownload()">Re-download</button>
      </div>

      <div class="vinfo" id="vinfo">
        <img class="vinfo-thumb" id="vinfo-thumb" src="" alt=""/>
        <div class="vinfo-body">
          <div class="vinfo-title" id="vinfo-title"></div>
          <div class="vinfo-meta" id="vinfo-meta"></div>
          <div class="vinfo-actions">
            <button id="dl-btn" onclick="startDownload()" style="display:none">⬇ Download Video</button>
            <button class="warn-btn" onclick="fetchSubtitlesOnly()">📝 Subtitles Only</button>
          </div>
        </div>
      </div>

      <div class="pw"><div class="pb" id="dl-pb"></div></div>
      <div class="msg" id="dl-msg"></div>

      <div id="subtitle-area" style="display:none" class="subtitle-box">
        <div class="lbl">📝 Transcript (English)
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
        <div class="vt" id="cut-title"></div>
        <div class="vm" id="cut-mode" style="margin-top:4px"></div>
        <div class="vm" id="cut-size" style="margin-top:2px"></div>
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
// ── State ──────────────────────────────────────────────────────────────────
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
  checkData: null,
};

// Hardcoded JSON schema (transcript array is empty – will be replaced at runtime)
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
    "source_video_id": "vid_abc123xyz",
    "transcript": []   // This will be replaced with state.subtitleSegments at export time
  }
};

// ── Robust clipboard copy ─────────────────────────────────────────────────
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

// ── Helpers ────────────────────────────────────────────────────────────────
function setMsg(id, cls, txt) {
  const el = document.getElementById(id);
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = txt;
}
function setPb(id, on) {
  const pb = document.getElementById(id);
  on ? (pb.classList.add('spin'), pb.style.width='35%') : (pb.classList.remove('spin'));
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
function poll(jid, pbId, msgId, onDone, onFail) {
  setPb(pbId, true);
  const iv = setInterval(async () => {
    try {
      const d = await (await fetch('/api/job/' + jid)).json();
      if (d.state === 'done') {
        clearInterval(iv); setPb(pbId, false);
        document.getElementById(pbId).style.width = '100%';
        onDone(d.data);
      } else if (d.state === 'error') {
        clearInterval(iv); setPb(pbId, false);
        document.getElementById(pbId).style.width = '0%';
        setMsg(msgId, 'err', '✗ ' + (d.error || 'error'));
        if (onFail) onFail();
      }
    } catch(e) {
      clearInterval(iv); setPb(pbId, false);
      setMsg(msgId, 'err', '✗ Network error');
    }
  }, 900);
}
function showVinfo(data, showDlBtn) {
  document.getElementById('vinfo-thumb').src = data.thumbnail || '';
  document.getElementById('vinfo-title').textContent = data.title || '';
  document.getElementById('vinfo-meta').textContent = 'Duration: ' + (data.duration_str || '') + (data.size_mb ? '  ·  ' + data.size_mb + ' MB' : '');
  document.getElementById('vinfo').classList.add('show');
  document.getElementById('dl-btn').style.display = showDlBtn ? '' : 'none';
}

// ── STEP 1a: CHECK ─────────────────────────────────────────────────────────
async function startCheck() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  state = { ...state, videoId: null, title: null, filename: null, clipFilename: null, checkData: null };
  document.getElementById('exists-banner').classList.remove('show');
  document.getElementById('vinfo').classList.remove('show');
  document.getElementById('subtitle-area').style.display = 'none';
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('dl-pb').style.width = '0%';
  document.getElementById('check-pb').style.width = '0%';
  setMsg('check-msg', '', 'Checking…');
  setMsg('dl-msg', '', '');
  document.getElementById('check-btn').disabled = true;
  document.getElementById('export-schema-btn').disabled = true;
  setMsg('export-msg', '', '');
  try {
    const res = await fetch('/api/check', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    poll(data.job_id, 'check-pb', 'check-msg', (d) => {
      document.getElementById('check-btn').disabled = false;
      state.checkData = d;
      state.videoId = d.video_id;
      state.title = d.title;
      state.durationStr = d.duration_str;
      state.durationSec = d.duration_sec;
      state.thumbnail = d.thumbnail;
      if (d.exists) {
        state.filename = d.filename;
        setMsg('check-msg', 'ok', '✓ Found in library');
        showVinfo({ ...d, size_mb: d.size_mb }, false);
        document.getElementById('exists-banner').classList.add('show');
        document.getElementById('cut-btn').disabled = false;
        document.getElementById('cut-hint').textContent = 'Ready: ' + d.filename;
      } else {
        setMsg('check-msg', '', 'Not downloaded yet');
        showVinfo(d, true);
      }
    }, () => { document.getElementById('check-btn').disabled = false; });
  } catch(e) {
    document.getElementById('check-btn').disabled = false;
    setMsg('check-msg', 'err', '✗ Request failed');
  }
}
function useExisting() {
  document.getElementById('cut-btn').disabled = false;
  document.getElementById('cut-hint').textContent = 'Ready: ' + (state.filename || '');
  document.getElementById('exists-banner').classList.remove('show');
  setMsg('check-msg', 'ok', '✓ Using existing: ' + state.filename);
}
function forceDownload() {
  document.getElementById('exists-banner').classList.remove('show');
  startDownload();
}

// ── STEP 1b: DOWNLOAD ──────────────────────────────────────────────────────
async function startDownload() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL first'); return; }
  document.getElementById('dl-btn').disabled = true;
  document.getElementById('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Downloading video and subtitles...');
  try {
    const res = await fetch('/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      state.filename = d.filename;
      state.subtitleText = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', `✓ Downloaded: ${d.filename} (${d.size_mb} MB)`);
      showVinfo({ ...state, size_mb: d.size_mb }, false);
      document.getElementById('cut-btn').disabled = false;
      document.getElementById('cut-hint').textContent = 'Ready: ' + d.filename;
      if (state.subtitleText) {
        document.getElementById('subtitle-area').style.display = '';
        document.getElementById('subtitle-text').value = state.subtitleText;
      }
      loadDownloadsList();
    }, () => { document.getElementById('dl-btn').disabled = false; });
  } catch(e) {
    document.getElementById('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

// ── Subtitles only ─────────────────────────────────────────────────────────
async function fetchSubtitlesOnly() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL first'); return; }
  setMsg('dl-msg', '', 'Fetching subtitles...');
  document.getElementById('dl-pb').style.width = '0%';
  try {
    const res = await fetch('/api/subtitles-only', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      state.subtitleText = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', `✓ Subtitles ready: ${d.title}`);
      document.getElementById('subtitle-area').style.display = '';
      document.getElementById('subtitle-text').value = state.subtitleText || '(none found)';
    });
  } catch(e) { setMsg('dl-msg', 'err', '✗ Request failed'); }
}

function copyTranscript() {
  if (state.subtitleText) {
    copyToClipboard(state.subtitleText, '✓ Transcript copied');
  } else {
    setMsg('dl-msg', 'err', '✗ No transcript available');
  }
}

async function copySegments() {
  if (state.subtitleSegments.length > 0) {
    const jsonStr = JSON.stringify(state.subtitleSegments, null, 2);
    copyToClipboard(jsonStr, `✓ ${state.subtitleSegments.length} segments copied`);
    return;
  }
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL first'); return; }
  setMsg('dl-msg', '', 'Fetching segments...');
  try {
    const res = await fetch('/api/subtitles-segments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      state.subtitleSegments = d.segments || [];
      const jsonStr = JSON.stringify(state.subtitleSegments, null, 2);
      copyToClipboard(jsonStr, `✓ ${state.subtitleSegments.length} segments copied`);
      document.getElementById('subtitle-text').value = jsonStr;
      document.getElementById('export-schema-btn').disabled = false;
    });
  } catch(e) {
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

async function exportSegmentsWithSchema() {
  if (!state.subtitleSegments || state.subtitleSegments.length === 0) {
    setMsg('export-msg', 'err', '✗ No segmented subtitles available. First fetch segments using "Copy Segments (JSON)" button.');
    return;
  }
  const exportObj = JSON.parse(JSON.stringify(HARDCODED_SCHEMA));
  exportObj.data_payload.transcript = state.subtitleSegments;
  if (state.videoId) exportObj.data_payload.source_video_id = state.videoId;
  const jsonStr = JSON.stringify(exportObj, null, 2);
  await copyToClipboard(jsonStr, '✓ Schema + segments copied to clipboard');
  setMsg('export-msg', 'ok', `✓ Exported ${state.subtitleSegments.length} segments with schema`);
}

// ── 9:16 conversion for clips ──────────────────────────────────────────────
async function convertTo916(filename) {
  if (!confirm(`Convert "${filename}" to 9:16 vertical format? A new file will be created.`)) return;
  setMsg('dl-msg', '', `Converting ${filename}...`);
  try {
    const res = await fetch('/api/clip/convert-to-916/' + encodeURIComponent(filename), { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      setMsg('dl-msg', 'ok', `✓ Converted: ${d.new_filename} (${d.size_mb} MB)`);
      loadDownloadsList();
    }, () => {
      setMsg('dl-msg', 'err', '✗ Conversion failed');
    });
  } catch(e) {
    console.error(e);
    setMsg('dl-msg', 'err', '✗ Request failed: ' + e.message);
  }
}

// ── STEP 2: CUT ────────────────────────────────────────────────────────────
async function startCut() {
  if (!state.filename) { setMsg('cut-msg', 'err', '✗ No video selected'); return; }
  const from = document.getElementById('ts-from').value.trim();
  const to = document.getElementById('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg', 'err', '✗ Both timestamps required'); return; }
  const tsRe = /^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$/;
  if (!tsRe.test(from)) { setMsg('cut-msg', 'err', '✗ Invalid From timestamp'); return; }
  if (!tsRe.test(to)) { setMsg('cut-msg', 'err', '✗ Invalid To timestamp'); return; }
  const cutMode = document.querySelector('input[name="cut-mode"]:checked')?.value || 'normal';
  state.clipFilename = null;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('cut-pb').style.width = '0%';
  setMsg('cut-msg', '', cutMode === '9:16' ? 'Cutting and converting 9:16…' : 'Cutting clip…');
  try {
    const res = await fetch('/api/cut', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ source_filename: state.filename, ts_from: from, ts_to: to, mode: cutMode }) });
    const data = await res.json();
    poll(data.job_id, 'cut-pb', 'cut-msg', (d) => {
      state.clipFilename = d.clip_filename;
      document.getElementById('cut-btn').disabled = false;
      setMsg('cut-msg', 'ok', `✓ Done — ${d.from} → ${d.to}`);
      document.getElementById('cut-title').textContent = d.clip_filename;
      document.getElementById('cut-mode').textContent = 'Mode: ' + (d.mode === '9:16' ? '9:16 Vertical' : 'Original (stream copy)');
      document.getElementById('cut-size').textContent = 'Size: ' + (d.size_mb ? d.size_mb.toFixed(2) + ' MB' : 'unknown');
      document.getElementById('cut-info').style.display = '';
      document.getElementById('cut-dl-btn').disabled = false;
      loadDownloadsList();
    }, () => { document.getElementById('cut-btn').disabled = false; });
  } catch(e) {
    document.getElementById('cut-btn').disabled = false;
    setMsg('cut-msg', 'err', '✗ Request failed');
  }
}
function downloadClip() {
  if (state.clipFilename) window.location.href = '/api/download-file/clip/' + encodeURIComponent(state.clipFilename);
}

// ── Library interactions ───────────────────────────────────────────────────
function selectVideo(filename) {
  state.filename = filename;
  document.getElementById('cut-btn').disabled = false;
  document.getElementById('cut-hint').textContent = 'Ready: ' + filename;
  setMsg('cut-msg', '', '');
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
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) loadDownloadsList();
    else alert('Delete failed');
  } catch(e) { alert('Delete failed'); }
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
      clipContainer.innerHTML = data.clips.map(f => {
        const safeName = escapeHtml(f.filename);
        const encodedName = encodeURIComponent(f.filename);
        return `
          <div class="file-item">
            <div class="file-info">
              <div class="file-name">${safeName}</div>
              <div class="file-meta">${f.size_mb} MB · ${f.modified}</div>
            </div>
            <div class="file-actions">
              <button class="sec" style="padding:5px 12px;border-radius:6px;" onclick="convertTo916('${safeName.replace(/'/g, "\\'")}')">9:16</button>
              <a href="/api/download-file/clip/${encodedName}" class="sec" style="padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--text);border:1px solid var(--border);">⬇ Download</a>
              <button class="delete-btn" onclick="deleteClip('${safeName.replace(/'/g, "\\'")}')">🗑 Delete</button>
            </div>
          </div>
        `;
      }).join('');
    }
  } catch(e) {
    console.error('Library load error:', e);
    videoContainer.innerHTML = '<div class="msg err">Failed to load videos: ' + e.message + '</div>';
    clipContainer.innerHTML = '<div class="msg err">Failed to load clips: ' + e.message + '</div>';
  }
}
document.getElementById('url-input').addEventListener('keydown', e => { if (e.key === 'Enter') startCheck(); });
loadDownloadsList();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# PYDANTIC MODELS
# ---------------------------------------------------------------------------
class CheckRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str

class CutRequest(BaseModel):
    source_filename: str
    ts_from: str
    ts_to: str
    mode: str = "normal"

    @validator('ts_from', 'ts_to')
    def validate_timestamp(cls, v):
        if not validate_timestamp(v):
            raise ValueError(f"Invalid timestamp format: {v}. Use HH:MM:SS.mmm (e.g., 01:34:33.000)")
        return v

class SubtitleOnlyRequest(BaseModel):
    url: str

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

@app.post("/api/check")
async def api_check(req: CheckRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=check_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/download")
async def api_download(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=download_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-only")
async def api_subtitles_only(req: SubtitleOnlyRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_only_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-segments")
async def api_subtitles_segments(req: SubtitleOnlyRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_segments_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/cut")
async def api_cut(req: CutRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(
        target=cut_worker,
        args=(job_id, req.source_filename, req.ts_from, req.ts_to, req.mode),
        daemon=True
    ).start()
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
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.delete("/api/downloads/{filename}")
async def delete_download(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.get("/api/download-file/video/{filename}")
async def download_video(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    )

@app.get("/api/download-file/clip/{filename}")
async def download_clip(filename: str):
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    )

@app.post("/api/clip/convert-to-916/{filename}")
async def api_convert_to_916(filename: str):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=convert_to_916_worker, args=(job_id, filename), daemon=True).start()
    return {"job_id": job_id}
