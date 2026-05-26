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
BASE_DIR  = Path(__file__).parent
COOKIES   = BASE_DIR / "cookies.txt"
DOWNLOADS = BASE_DIR / "downloads"
CLIPS     = BASE_DIR / "clips"
TEMP      = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# JOB STORE
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
        raise ValueError("Timestamp must be HH:MM:SS")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

def validate_timestamp(ts: str) -> bool:
    pattern = r'^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$'
    if not re.match(pattern, ts):
        return False
    try:
        _timestamp_to_seconds(ts)
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
    seen = set()
    with open(vtt_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not ('-->' in line or line.startswith('WEBVTT') or line.isdigit()):
                clean = re.sub(r'<[^>]+>', '', line).strip()
                if clean and clean not in seen:
                    seen.add(clean)
                    lines.append(clean)
    return ' '.join(lines)

def get_file_size_mb(path: Path) -> float:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else 0.0

def find_existing_download(video_id: str):
    for f in DOWNLOADS.glob(f"{video_id}_*.mp4"):
        return f
    return None

# ---------------------------------------------------------------------------
# WORKERS
# ---------------------------------------------------------------------------
def check_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta         = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id     = meta.get("id", "unknown")
        title        = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except (TypeError, ValueError):
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"
        thumbnail    = meta.get("thumbnail", "")
        existing     = find_existing_download(video_id)
        if existing:
            job_set(job_id, "done", {
                "exists": True, "video_id": video_id, "title": title,
                "duration_sec": duration_sec, "duration_str": duration_str,
                "thumbnail": thumbnail, "filename": existing.name,
                "size_mb": get_file_size_mb(existing),
            })
        else:
            job_set(job_id, "done", {
                "exists": False, "video_id": video_id, "title": title,
                "duration_sec": duration_sec, "duration_str": duration_str,
                "thumbnail": thumbnail, "filename": None, "size_mb": 0,
            })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def download_worker(job_id: str, url: str, video_id: str, title: str,
                    duration_sec: float, duration_str: str):
    try:
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file   = DOWNLOADS / f"{video_id}_{safe_title}.mp4"
        existing   = find_existing_download(video_id)
        if existing:
            job_set(job_id, "done", {
                "video_id": video_id, "title": title,
                "duration_sec": duration_sec, "duration_str": duration_str,
                "filename": existing.name, "size_mb": get_file_size_mb(existing),
                "subtitle_text": "",
            })
            return
        dl_cmd = ytdlp_base() + [
            "-S", "vcodec:h264,res,acodec:aac",
            "--merge-output-format", "mp4",
            "--no-playlist", "-o", str(out_file),
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])
        vtt_path = out_file.with_suffix('.en.vtt')
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "vtt",
            "-o", str(out_file.with_suffix('')),
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)
        job_set(job_id, "done", {
            "video_id": video_id, "title": title,
            "duration_sec": duration_sec, "duration_str": duration_str,
            "filename": out_file.name, "size_mb": get_file_size_mb(out_file),
            "subtitle_text": sub_text,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def subtitle_only_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta  = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")
        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "vtt",
            "-o", str(tmp_base), url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)
        job_set(job_id, "done", {"title": title, "subtitle_text": sub_text})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def subtitle_segments_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta  = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")
        tmp_base = TEMP / f"segments_{job_id}"
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "json3",
            "-o", str(tmp_base), url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        json3_path = Path(str(tmp_base) + ".en.json3")
        if not json3_path.exists():
            raise RuntimeError("No JSON3 subtitles found (English).")
        with open(json3_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        segments = []
        for event in data.get("events", []):
            start_ms    = event.get("tStartMs", 0)
            duration_ms = event.get("dDurationMs", 0)
            text        = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
            if not text or text == "\n":
                continue
            segments.append({
                "start": seconds_to_ts(start_ms / 1000.0),
                "end":   seconds_to_ts((start_ms + duration_ms) / 1000.0),
                "text":  text
            })
        json3_path.unlink(missing_ok=True)
        job_set(job_id, "done", {"segments": segments, "title": title})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str,
               mode: str = "normal"):
    temp_file = None
    try:
        if not validate_timestamp(ts_from):
            raise ValueError(f"Invalid start timestamp: {ts_from}")
        if not validate_timestamp(ts_to):
            raise ValueError(f"Invalid end timestamp: {ts_to}")
        source = DOWNLOADS / source_filename
        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")
        start_s  = _timestamp_to_seconds(ts_from)
        end_s    = _timestamp_to_seconds(ts_to)
        if end_s <= start_s:
            raise ValueError("End timestamp must be after start timestamp")
        duration  = str(end_s - start_s)
        clip_name = f"clip_{uuid.uuid4().hex[:8]}.mp4"
        out_file  = CLIPS / clip_name
        if mode == "9:16":
            temp_file = TEMP / f"tmp_{uuid.uuid4().hex[:8]}.mp4"
            cut_cmd   = [
                "ffmpeg", "-y", "-ss", ts_from, "-i", str(source),
                "-t", duration, "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(temp_file)
            ]
            cr = subprocess.run(cut_cmd, capture_output=True, text=True, timeout=120)
            if cr.returncode != 0:
                raise RuntimeError(f"Cut step failed: {cr.stderr.strip()[-300:]}")
            vf  = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd = [
                "ffmpeg", "-y", "-i", str(temp_file),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out_file)
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-ss", ts_from, "-i", str(source),
                "-t", duration, "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", str(out_file)
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if result.returncode == -9:
                raise RuntimeError("FFmpeg killed by OS (out of memory). Try a shorter clip.")
            raise RuntimeError(result.stderr.strip()[-600:])
        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError(f"Output file missing or empty. stderr: {result.stderr.strip()[-300:]}")
        job_set(job_id, "done", {
            "clip_filename": clip_name, "from": ts_from, "to": ts_to,
            "mode": mode, "size_mb": get_file_size_mb(out_file),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        if temp_file and temp_file.exists():
            temp_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ClipForge</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
:root {
  --bg: #080810;
  --s1: #0f0f1a;
  --s2: #161624;
  --s3: #1e1e30;
  --border: #252538;
  --border2: #2e2e48;
  --accent: #6c47ff;
  --accent-glow: rgba(108,71,255,0.25);
  --pink: #ff4785;
  --cyan: #00d4ff;
  --text: #e2e2f0;
  --muted: #6060a0;
  --ok: #00e5a0;
  --err: #ff4757;
  --font-head: 'Syne', sans-serif;
  --font-mono: 'DM Mono', monospace;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── Header ────────────────────────────────────────────────────────────── */
.hd {
  position: sticky; top: 0; z-index: 100;
  background: rgba(8,8,16,0.85);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  height: 52px;
  display: flex; align-items: center; gap: 12px;
}
.hd-logo {
  font-family: var(--font-head);
  font-size: 18px; font-weight: 800;
  background: linear-gradient(120deg, var(--accent), var(--cyan));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  flex: 1;
}
.hd-badge {
  font-size: 9px; color: var(--muted);
  border: 1px solid var(--border2); padding: 2px 8px; border-radius: 20px;
  letter-spacing: .08em;
}

/* ── Bottom tab bar (mobile only) ──────────────────────────────────────── */
.tab-bar {
  display: none;
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 200;
  background: rgba(15,15,26,0.97);
  backdrop-filter: blur(20px);
  border-top: 1px solid var(--border2);
  height: 60px;
}
.tab-bar-inner {
  display: flex; height: 100%;
}
.tb-btn {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 3px;
  background: none; border: none; color: var(--muted);
  font-family: var(--font-mono); font-size: 9px; letter-spacing:.04em;
  cursor: pointer; transition: color .15s;
  padding: 0;
}
.tb-btn svg { width: 20px; height: 20px; stroke-width: 1.8; }
.tb-btn.active { color: var(--accent); }
.tb-btn.active svg { filter: drop-shadow(0 0 6px var(--accent)); }

/* ── Desktop layout ────────────────────────────────────────────────────── */
.layout {
  display: grid;
  grid-template-columns: 1fr 360px;
  gap: 0;
  max-width: 1320px;
  margin: 0 auto;
  min-height: calc(100vh - 52px);
}
.col-main {
  padding: 24px 24px 24px 24px;
  display: flex; flex-direction: column; gap: 20px;
  border-right: 1px solid var(--border);
}
.col-side {
  padding: 24px 20px;
  display: flex; flex-direction: column; gap: 16px;
  background: var(--s1);
}

/* ── Mobile panels ─────────────────────────────────────────────────────── */
.mob-panel { display: none; }

/* ── Cards ─────────────────────────────────────────────────────────────── */
.card {
  background: var(--s1);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  transition: border-color .2s;
}
.card:focus-within { border-color: var(--border2); }
.card-hd {
  padding: 13px 18px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
}
.card-hd .step {
  width: 22px; height: 22px; border-radius: 7px;
  background: linear-gradient(135deg, var(--accent), var(--pink));
  color: #fff; font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.card-hd .card-title {
  font-family: var(--font-head); font-size: 13px; font-weight: 700; flex: 1;
  letter-spacing: .02em;
}
.card-bd { padding: 18px; display: flex; flex-direction: column; gap: 14px; }

/* ── Inputs ────────────────────────────────────────────────────────────── */
.lbl {
  font-size: 9px; color: var(--muted);
  letter-spacing: .14em; text-transform: uppercase;
  display: block; margin-bottom: 6px;
}
input[type=text] {
  width: 100%;
  background: var(--s2); border: 1px solid var(--border2);
  color: var(--text); padding: 10px 13px; border-radius: 9px;
  font-family: var(--font-mono); font-size: 13px;
  outline: none; transition: border-color .15s, box-shadow .15s;
}
input[type=text]:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.row { display: flex; gap: 8px; }
.row input { flex: 1; }

/* ── Buttons ───────────────────────────────────────────────────────────── */
button {
  background: var(--accent);
  color: #fff; border: none;
  padding: 10px 18px; border-radius: 9px;
  font-family: var(--font-mono); font-size: 12px; font-weight: 500;
  cursor: pointer; transition: opacity .15s, transform .1s, box-shadow .15s;
  white-space: nowrap;
}
button:hover { opacity: .88; box-shadow: 0 4px 16px var(--accent-glow); }
button:active { transform: scale(.97); }
button.sec {
  background: var(--s2); border: 1px solid var(--border2); color: var(--text);
}
button.sec:hover { border-color: var(--accent); box-shadow: none; }
button.ghost {
  background: transparent; border: 1px solid var(--border2); color: var(--muted);
  padding: 6px 12px; font-size: 11px;
}
button.ghost:hover { border-color: var(--cyan); color: var(--cyan); box-shadow: none; }
button.ok-btn { background: #1a5c42; border: 1px solid var(--ok); }
button.ok-btn:hover { box-shadow: 0 4px 16px rgba(0,229,160,.2); }
button.warn-btn { background: #2a1f5c; border: 1px solid var(--accent); color: #a090ff; }
button.del-btn {
  background: rgba(255,71,87,.1); border: 1px solid rgba(255,71,87,.3);
  color: var(--err); padding: 5px 10px; font-size: 10px; border-radius: 6px;
}
button.del-btn:hover { box-shadow: none; background: rgba(255,71,87,.2); }
button:disabled { opacity: .3; cursor: not-allowed; box-shadow: none !important; }

/* ── Progress ──────────────────────────────────────────────────────────── */
.pw { background: var(--border); border-radius: 3px; height: 2px; overflow: hidden; }
.pb {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--cyan));
  border-radius: 3px; transition: width .3s;
}
.pb.spin { animation: spin 1.3s ease-in-out infinite; width: 40% !important; }
@keyframes spin { 0%{transform:translateX(-120%)} 100%{transform:translateX(360%)} }

.msg { font-size: 11px; color: var(--muted); min-height: 15px; line-height: 1.6; }
.msg.ok { color: var(--ok); }
.msg.err { color: var(--err); }

/* ── Video info ────────────────────────────────────────────────────────── */
.vinfo {
  display: none;
  background: var(--s2); border: 1px solid var(--border2); border-radius: 11px;
  padding: 14px; gap: 12px; align-items: flex-start;
}
.vinfo.show { display: flex; }
.vthumb {
  width: 96px; height: 54px; object-fit: cover; border-radius: 7px;
  background: var(--border); flex-shrink: 0;
}
.vbody { flex: 1; min-width: 0; }
.vtitle { font-family: var(--font-head); font-size: 12px; font-weight: 700; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vmeta { font-size: 10px; color: var(--muted); margin-bottom: 10px; }
.vactions { display: flex; gap: 6px; flex-wrap: wrap; }

/* ── Exists banner ─────────────────────────────────────────────────────── */
.exists-strip {
  display: none;
  background: rgba(0,229,160,.07); border: 1px solid rgba(0,229,160,.25);
  border-radius: 9px; padding: 10px 14px;
  align-items: center; gap: 10px; flex-wrap: wrap;
}
.exists-strip.show { display: flex; }
.exists-strip .e-label { font-size: 11px; color: var(--ok); flex: 1; }

/* ── Subtitle accordion ────────────────────────────────────────────────── */
.sub-wrap { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.sub-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 11px 14px;
  background: var(--s2); cursor: pointer;
  user-select: none; gap: 8px;
}
.sub-head:hover { background: var(--s3); }
.sub-head-left { display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 500; }
.sub-head-right { display: flex; align-items: center; gap: 6px; }
.sub-copy-btn {
  background: var(--s3); border: 1px solid var(--border2); color: var(--text);
  padding: 4px 10px; border-radius: 6px; font-size: 10px;
  font-family: var(--font-mono); cursor: pointer;
  transition: border-color .15s, color .15s;
}
.sub-copy-btn:hover { border-color: var(--accent); color: var(--accent); }
.sub-arrow { font-size: 9px; color: var(--muted); transition: transform .2s; }
.sub-arrow.open { transform: rotate(180deg); }
.sub-body { display: none; padding: 12px; background: var(--bg); border-top: 1px solid var(--border); }
.sub-body.open { display: block; }
.sub-ta {
  width: 100%; background: var(--s1); border: 1px solid var(--border);
  color: var(--text); padding: 10px; border-radius: 7px;
  font-family: var(--font-mono); font-size: 11px; resize: vertical;
  outline: none; line-height: 1.7;
}

/* ── Timestamp grid ────────────────────────────────────────────────────── */
.ts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.mode-row { display: flex; gap: 18px; flex-wrap: wrap; }
.mode-opt { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); cursor: pointer; }
.mode-opt input[type=radio] { accent-color: var(--accent); }
.mode-opt:has(input:checked) { color: var(--text); }

/* ── Clip result ───────────────────────────────────────────────────────── */
.clip-done {
  display: none;
  background: rgba(0,229,160,.06); border: 1px solid rgba(0,229,160,.3);
  border-radius: 10px; padding: 12px 14px;
  align-items: center; justify-content: space-between; gap: 10px;
}
.clip-done.show { display: flex; }
.clip-done-info .clip-name { font-size: 11px; font-weight: 500; color: var(--ok); }
.clip-done-info .clip-meta { font-size: 10px; color: var(--muted); margin-top: 3px; }

/* ── Library ───────────────────────────────────────────────────────────── */
.lib-hd {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.lib-section {
  font-family: var(--font-head); font-size: 10px; font-weight: 700;
  color: var(--muted); letter-spacing: .12em; text-transform: uppercase;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
  margin-bottom: 8px; margin-top: 4px;
}
.file-list { display: flex; flex-direction: column; gap: 6px; max-height: 320px; overflow-y: auto; }
.file-list::-webkit-scrollbar { width: 3px; }
.file-list::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

.fitem {
  background: var(--s2); border: 1px solid var(--border);
  border-radius: 9px; padding: 9px 12px;
  display: flex; align-items: center; gap: 8px;
  cursor: pointer; transition: border-color .15s, background .15s;
}
.fitem:hover { border-color: var(--accent); }
.fitem.active { border-color: var(--accent); background: rgba(108,71,255,.08); }
.fitem-icon { font-size: 14px; flex-shrink: 0; }
.fitem-info { flex: 1; min-width: 0; }
.fitem-name { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fitem-meta { font-size: 9px; color: var(--muted); margin-top: 2px; }
.fitem-acts { display: flex; gap: 5px; flex-shrink: 0; }
.fitem-dl { font-size: 10px; color: var(--cyan); text-decoration: none; padding: 3px 8px; border: 1px solid rgba(0,212,255,.25); border-radius: 5px; }
.fitem-dl:hover { background: rgba(0,212,255,.1); }

.empty-state { text-align: center; padding: 24px 16px; color: var(--muted); font-size: 11px; }
.empty-state .ei { font-size: 24px; margin-bottom: 8px; }

/* ── Divider ───────────────────────────────────────────────────────────── */
.sep { height: 1px; background: var(--border); }

/* ── Responsive: mobile ────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .tab-bar { display: flex; }
  .layout { display: block; }
  .col-main, .col-side { display: none; border: none; padding: 16px 16px 80px; }
  .col-main.mob-active, .col-side.mob-active { display: flex; }
  body { padding-bottom: 0; }
  .ts-grid { grid-template-columns: 1fr; }
  .mode-row { flex-direction: column; gap: 10px; }
  .vinfo { flex-direction: column; }
  .vthumb { width: 100%; height: 140px; }
}
@media (min-width: 769px) {
  .col-main, .col-side { display: flex !important; }
}
</style>
</head>
<body>

<!-- Header -->
<div class="hd">
  <div class="hd-logo">⬡ ClipForge</div>
  <span class="hd-badge">yt-dlp · FFmpeg</span>
</div>

<!-- Mobile tab bar -->
<div class="tab-bar">
  <div class="tab-bar-inner">
    <button class="tb-btn active" id="tb-source" onclick="mobTab('source')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>
      Source
    </button>
    <button class="tb-btn" id="tb-cut" onclick="mobTab('cut')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/></svg>
      Cut
    </button>
    <button class="tb-btn" id="tb-lib" onclick="mobTab('lib')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      Library
    </button>
  </div>
</div>

<!-- Layout -->
<div class="layout">

  <!-- LEFT: Source + Cut -->
  <div class="col-main mob-active" id="mob-source">

    <!-- Card: Video Source -->
    <div class="card">
      <div class="card-hd">
        <div class="step">1</div>
        <div class="card-title">Video Source</div>
      </div>
      <div class="card-bd">
        <div>
          <label class="lbl">YouTube URL</label>
          <div class="row">
            <input type="text" id="url-input" placeholder="youtube.com/watch?v= or youtu.be/..."/>
            <button id="check-btn" onclick="startCheck()">Check</button>
          </div>
        </div>
        <div class="pw"><div class="pb" id="check-pb"></div></div>
        <div class="msg" id="check-msg"></div>

        <!-- Exists strip -->
        <div class="exists-strip" id="exists-strip">
          <span class="e-label">✓ Already in library</span>
          <button class="ghost" onclick="useExisting()">Use for cutting</button>
          <button class="ghost" onclick="forceDownload()">Re-download</button>
        </div>

        <!-- Video info -->
        <div class="vinfo" id="vinfo">
          <img class="vthumb" id="vthumb" src="" alt=""/>
          <div class="vbody">
            <div class="vtitle" id="vtitle"></div>
            <div class="vmeta" id="vmeta"></div>
            <div class="vactions">
              <button id="dl-btn" onclick="startDownload()" style="display:none">⬇ Download</button>
              <button class="ghost" onclick="fetchSubOnly()">📝 Subs Only</button>
            </div>
          </div>
        </div>

        <div class="pw"><div class="pb" id="dl-pb"></div></div>
        <div class="msg" id="dl-msg"></div>

        <!-- Subtitle accordion -->
        <div class="sub-wrap" id="sub-wrap" style="display:none">
          <div class="sub-head" onclick="toggleSub()">
            <div class="sub-head-left">📝 <span>Transcript</span></div>
            <div class="sub-head-right">
              <button class="sub-copy-btn" onclick="event.stopPropagation();copySubText()">Copy Text</button>
              <button class="sub-copy-btn" onclick="event.stopPropagation();copySubSegments()">Copy Segments</button>
              <span class="sub-arrow" id="sub-arrow">▼</span>
            </div>
          </div>
          <div class="sub-body" id="sub-body">
            <textarea class="sub-ta" id="sub-ta" rows="7" readonly></textarea>
          </div>
        </div>
      </div>
    </div>

    <!-- Card: Cut Clip -->
    <div class="card" id="cut-card">
      <div class="card-hd">
        <div class="step">2</div>
        <div class="card-title">Cut Clip</div>
      </div>
      <div class="card-bd">
        <div class="msg" id="cut-hint" style="color:var(--muted)">Check or download a video first, then set timestamps.</div>
        <div class="ts-grid">
          <div>
            <label class="lbl">From</label>
            <input type="text" id="ts-from" placeholder="00:00:00.000"/>
          </div>
          <div>
            <label class="lbl">To</label>
            <input type="text" id="ts-to" placeholder="00:00:30.000"/>
          </div>
        </div>
        <div>
          <label class="lbl">Format</label>
          <div class="mode-row">
            <label class="mode-opt"><input type="radio" name="cut-mode" value="normal" checked/> Original (stream copy)</label>
            <label class="mode-opt"><input type="radio" name="cut-mode" value="9:16"/> 9:16 Vertical</label>
          </div>
        </div>
        <button id="cut-btn" onclick="startCut()" disabled>✂ Cut Clip</button>
        <div class="pw"><div class="pb" id="cut-pb"></div></div>
        <div class="msg" id="cut-msg"></div>
        <div class="clip-done" id="clip-done">
          <div class="clip-done-info">
            <div class="clip-name" id="clip-name"></div>
            <div class="clip-meta" id="clip-meta"></div>
          </div>
          <button class="ok-btn" onclick="downloadClip()">⬇ Download</button>
        </div>
      </div>
    </div>

  </div>

  <!-- RIGHT: Library -->
  <div class="col-side" id="mob-lib">

    <div class="lib-hd">
      <span style="font-family:var(--font-head);font-size:13px;font-weight:700">Library</span>
      <button class="ghost" onclick="loadLibrary()" style="padding:5px 10px">↻ Refresh</button>
    </div>

    <div>
      <div class="lib-section">Videos</div>
      <div class="file-list" id="videos-list">
        <div class="empty-state"><div class="ei">📭</div>No videos yet</div>
      </div>
    </div>

    <div class="sep"></div>

    <div>
      <div class="lib-section">Clips</div>
      <div class="file-list" id="clips-list">
        <div class="empty-state"><div class="ei">✂️</div>No clips yet</div>
      </div>
    </div>

  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const S = {
  videoId: null, title: null, durationStr: null, durationSec: null,
  thumbnail: null, filename: null, clipFilename: null,
  subText: '',       // plain text subtitle
  subSegs: null,     // parsed segments array (populated on demand)
  subView: 'text',   // 'text' | 'segments'
};

// ── Mobile tabs ────────────────────────────────────────────────────────────
function mobTab(tab) {
  const panels = { source: 'mob-source', cut: 'mob-source', lib: 'mob-lib' };
  const srcEl  = document.getElementById('mob-source');
  const libEl  = document.getElementById('mob-lib');
  // Show/hide panels
  srcEl.classList.toggle('mob-active', tab !== 'lib');
  libEl.classList.toggle('mob-active', tab === 'lib');
  // On cut tab, scroll to cut card
  if (tab === 'cut') {
    setTimeout(() => document.getElementById('cut-card').scrollIntoView({behavior:'smooth'}), 50);
  }
  // Update tab buttons
  ['source','cut','lib'].forEach(t => {
    document.getElementById('tb-' + t).classList.toggle('active', t === tab);
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
function setMsg(id, cls, txt) {
  const el = $(id);
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = txt;
}
function setPb(id, on) {
  const pb = $(id);
  on ? (pb.classList.add('spin'), pb.style.width='40%')
     : pb.classList.remove('spin');
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, m =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
// Always plain text — no textarea.select() which causes underline on some browsers
function copyPlain(text) {
  return navigator.clipboard.writeText(text);
}
function poll(jid, pbId, msgId, onDone, onFail) {
  setPb(pbId, true);
  const iv = setInterval(async () => {
    try {
      const d = await (await fetch('/api/job/' + jid)).json();
      if (d.state === 'done') {
        clearInterval(iv); setPb(pbId, false);
        $(pbId).style.width = '100%';
        onDone(d.data);
      } else if (d.state === 'error') {
        clearInterval(iv); setPb(pbId, false);
        $(pbId).style.width = '0%';
        setMsg(msgId, 'err', '✗ ' + (d.error || 'error'));
        if (onFail) onFail();
      }
    } catch(e) {
      clearInterval(iv); setPb(pbId, false);
      setMsg(msgId, 'err', '✗ Network error');
    }
  }, 900);
}

// ── Show video info ────────────────────────────────────────────────────────
function showVinfo(d, showDl) {
  $('vthumb').src = d.thumbnail || '';
  $('vtitle').textContent = d.title || '';
  $('vmeta').textContent  = 'Duration: ' + (d.duration_str || '') +
    (d.size_mb ? '  ·  ' + d.size_mb + ' MB' : '');
  $('vinfo').classList.add('show');
  $('dl-btn').style.display = showDl ? '' : 'none';
}

// ── CHECK ──────────────────────────────────────────────────────────────────
async function startCheck() {
  const url = $('url-input').value.trim();
  if (!url) return;

  Object.assign(S, { videoId:null, title:null, filename:null, clipFilename:null, subText:'', subSegs:null });
  $('exists-strip').classList.remove('show');
  $('vinfo').classList.remove('show');
  $('sub-wrap').style.display = 'none';
  $('cut-btn').disabled = true;
  $('clip-done').classList.remove('show');
  $('check-pb').style.width = '0%';
  $('dl-pb').style.width = '0%';
  setMsg('check-msg', '', 'Checking…');
  setMsg('dl-msg', '', '');
  $('check-btn').disabled = true;

  try {
    const res  = await fetch('/api/check', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const data = await res.json();
    poll(data.job_id, 'check-pb', 'check-msg', d => {
      $('check-btn').disabled = false;
      Object.assign(S, { videoId:d.video_id, title:d.title, durationStr:d.duration_str, durationSec:d.duration_sec, thumbnail:d.thumbnail });
      if (d.exists) {
        S.filename = d.filename;
        setMsg('check-msg', 'ok', '✓ Found in library');
        showVinfo({...d}, false);
        $('exists-strip').classList.add('show');
      } else {
        setMsg('check-msg', '', '');
        showVinfo(d, true);
      }
    }, () => { $('check-btn').disabled = false; });
  } catch(e) {
    $('check-btn').disabled = false;
    setMsg('check-msg', 'err', '✗ Request failed');
  }
}

function useExisting() {
  $('exists-strip').classList.remove('show');
  $('cut-btn').disabled = false;
  $('cut-hint').textContent = 'Ready: ' + S.filename;
  setMsg('check-msg', 'ok', '✓ Using: ' + S.filename);
}

function forceDownload() {
  $('exists-strip').classList.remove('show');
  startDownload();
}

// ── DOWNLOAD ───────────────────────────────────────────────────────────────
async function startDownload() {
  if (!S.videoId) { setMsg('dl-msg','err','✗ Check a URL first'); return; }
  $('dl-btn').disabled = true;
  $('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Downloading…');
  try {
    const res  = await fetch('/api/download', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_id:S.videoId,title:S.title,duration_sec:S.durationSec,duration_str:S.durationStr})});
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', d => {
      $('dl-btn').disabled = false;
      S.filename  = d.filename;
      S.subText   = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', '✓ ' + d.filename + (d.size_mb ? ' · ' + d.size_mb + ' MB' : ''));
      showVinfo({...S, size_mb: d.size_mb}, false);
      $('cut-btn').disabled = false;
      $('cut-hint').textContent = 'Ready: ' + d.filename;
      if (S.subText) {
        $('sub-ta').value = S.subText;
        S.subView = 'text';
        $('sub-wrap').style.display = '';
      }
      loadLibrary();
    }, () => { $('dl-btn').disabled = false; });
  } catch(e) {
    $('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

// ── SUBTITLES ──────────────────────────────────────────────────────────────
async function fetchSubOnly() {
  const url = $('url-input').value.trim();
  if (!url) { setMsg('dl-msg','err','✗ Enter a URL first'); return; }
  setMsg('dl-msg','','Fetching subtitles…');
  $('dl-pb').style.width = '0%';
  try {
    const res  = await fetch('/api/subtitles-only',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', d => {
      S.subText = d.subtitle_text || '';
      S.subView = 'text';
      $('sub-ta').value = S.subText || '(No English subtitles found)';
      $('sub-wrap').style.display = '';
      setMsg('dl-msg','ok','✓ Subtitles ready');
    });
  } catch(e) { setMsg('dl-msg','err','✗ Request failed'); }
}

// Subtitle accordion toggle
function toggleSub() {
  $('sub-body').classList.toggle('open');
  $('sub-arrow').classList.toggle('open');
}

// Copy plain subtitle text — uses navigator.clipboard.writeText (no DOM selection, no underline)
function copySubText() {
  if (!S.subText) { setMsg('dl-msg','err','✗ No subtitle text loaded'); return; }
  // Show text view if currently in segments view
  if (S.subView !== 'text') {
    S.subView = 'text';
    $('sub-ta').value = S.subText;
  }
  copyPlain(S.subText).then(() => {
    setMsg('dl-msg','ok','✓ Transcript text copied');
  }).catch(() => {
    setMsg('dl-msg','err','✗ Clipboard access denied');
  });
}

// Copy segments — uses cached S.subSegs if available, else fetches once
async function copySubSegments() {
  const url = $('url-input').value.trim();
  if (!url) { setMsg('dl-msg','err','✗ Enter a URL'); return; }

  // Use cached segments if already fetched
  if (S.subSegs) {
    const md = segmentsToMarkdown(S.subSegs);
    S.subView = 'segments';
    $('sub-ta').value = md;
    $('sub-wrap').style.display = '';
    // Open accordion
    $('sub-body').classList.add('open');
    $('sub-arrow').classList.add('open');
    copyPlain(md).then(() => setMsg('dl-msg','ok','✓ Segments copied as markdown'));
    return;
  }

  setMsg('dl-msg','','Fetching segments…');
  $('dl-pb').style.width = '0%';
  try {
    const res  = await fetch('/api/subtitles-segments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', d => {
      S.subSegs = d.segments;
      const md  = segmentsToMarkdown(d.segments);
      S.subView = 'segments';
      $('sub-ta').value = md;
      $('sub-wrap').style.display = '';
      $('sub-body').classList.add('open');
      $('sub-arrow').classList.add('open');
      copyPlain(md).then(() => setMsg('dl-msg','ok',`✓ ${d.segments.length} segments copied as markdown`));
    });
  } catch(e) { setMsg('dl-msg','err','✗ Request failed'); }
}

// Convert segments to readable markdown table
function segmentsToMarkdown(segs) {
  const rows = segs.map(s => `| ${s.start} | ${s.end} | ${s.text.replace(/\|/g,'\\|')} |`);
  return [
    '| Start | End | Text |',
    '|-------|-----|------|',
    ...rows
  ].join('\n');
}

// ── CUT ────────────────────────────────────────────────────────────────────
async function startCut() {
  if (!S.filename) { setMsg('cut-msg','err','✗ No video selected'); return; }
  const from = $('ts-from').value.trim();
  const to   = $('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg','err','✗ Both timestamps required'); return; }
  const tsRe = /^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$/;
  if (!tsRe.test(from)) { setMsg('cut-msg','err','✗ Invalid From (use HH:MM:SS.mmm)'); return; }
  if (!tsRe.test(to))   { setMsg('cut-msg','err','✗ Invalid To (use HH:MM:SS.mmm)'); return; }

  const mode = document.querySelector('input[name="cut-mode"]:checked')?.value || 'normal';
  S.clipFilename = null;
  $('cut-btn').disabled = true;
  $('clip-done').classList.remove('show');
  $('cut-pb').style.width = '0%';
  setMsg('cut-msg','', mode === '9:16' ? 'Cutting and converting 9:16…' : 'Cutting…');

  try {
    const res  = await fetch('/api/cut',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_filename:S.filename,ts_from:from,ts_to:to,mode})});
    const data = await res.json();
    poll(data.job_id,'cut-pb','cut-msg', d => {
      S.clipFilename = d.clip_filename;
      $('cut-btn').disabled = false;
      setMsg('cut-msg','ok',`✓ Done — ${d.from} → ${d.to}`);
      $('clip-name').textContent = d.clip_filename;
      $('clip-meta').textContent = (d.size_mb ? d.size_mb + ' MB' : '') + (d.mode === '9:16' ? ' · 9:16 vertical' : '');
      $('clip-done').classList.add('show');
      loadLibrary();
    }, () => { $('cut-btn').disabled = false; });
  } catch(e) {
    $('cut-btn').disabled = false;
    setMsg('cut-msg','err','✗ Request failed');
  }
}

function downloadClip() {
  if (S.clipFilename) window.location.href = '/api/download-file/clip/' + encodeURIComponent(S.clipFilename);
}

// ── Library ────────────────────────────────────────────────────────────────
function selectVideo(fn) {
  S.filename = fn;
  $('cut-btn').disabled = false;
  $('cut-hint').textContent = 'Ready: ' + fn;
  $('clip-done').classList.remove('show');
  setMsg('cut-msg','','');
  document.querySelectorAll('#videos-list .fitem').forEach(el => {
    el.classList.toggle('active', el.dataset.fn === fn);
  });
  // On mobile, switch to source/cut view
  if (window.innerWidth <= 768) mobTab('cut');
}

async function delVideo(fn, ev) {
  ev.stopPropagation();
  if (!confirm(`Delete "${fn}"?`)) return;
  await fetch('/api/downloads/' + encodeURIComponent(fn), {method:'DELETE'});
  if (S.filename === fn) { S.filename = null; $('cut-btn').disabled = true; $('cut-hint').textContent = 'Select or download a video.'; }
  loadLibrary();
}

async function delClip(fn, ev) {
  ev.stopPropagation();
  if (!confirm(`Delete "${fn}"?`)) return;
  await fetch('/api/clips/' + encodeURIComponent(fn), {method:'DELETE'});
  loadLibrary();
}

async function loadLibrary() {
  const vl = $('videos-list'), cl = $('clips-list');
  try {
    const data = await (await fetch('/api/downloads/list')).json();

    if (!data.videos.length) {
      vl.innerHTML = '<div class="empty-state"><div class="ei">📭</div>No videos yet</div>';
    } else {
      vl.innerHTML = data.videos.map(f => `
        <div class="fitem" data-fn="${esc(f.filename)}" onclick="selectVideo('${esc(f.filename)}')">
          <div class="fitem-icon">🎬</div>
          <div class="fitem-info">
            <div class="fitem-name">${esc(f.filename)}</div>
            <div class="fitem-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="fitem-acts" onclick="event.stopPropagation()">
            <a class="fitem-dl" href="/api/download-file/video/${encodeURIComponent(f.filename)}" download>⬇</a>
            <button class="del-btn" onclick="delVideo('${esc(f.filename)}',event)">🗑</button>
          </div>
        </div>`).join('');
      if (S.filename) {
        document.querySelectorAll('#videos-list .fitem').forEach(el => {
          el.classList.toggle('active', el.dataset.fn === S.filename);
        });
      }
    }

    if (!data.clips.length) {
      cl.innerHTML = '<div class="empty-state"><div class="ei">✂️</div>No clips yet</div>';
    } else {
      cl.innerHTML = data.clips.map(f => `
        <div class="fitem">
          <div class="fitem-icon">🎞️</div>
          <div class="fitem-info">
            <div class="fitem-name">${esc(f.filename)}</div>
            <div class="fitem-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="fitem-acts">
            <a class="fitem-dl" href="/api/download-file/clip/${encodeURIComponent(f.filename)}" download>⬇</a>
            <button class="del-btn" onclick="delClip('${esc(f.filename)}',event)">🗑</button>
          </div>
        </div>`).join('');
    }
  } catch(e) {
    vl.innerHTML = '<div class="empty-state" style="color:var(--err)">Failed to load</div>';
  }
}

$('url-input').addEventListener('keydown', e => { if (e.key === 'Enter') startCheck(); });
loadLibrary();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class CheckRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    video_id: str
    title: str
    duration_sec: float
    duration_str: str

class CutRequest(BaseModel):
    source_filename: str
    ts_from: str
    ts_to: str
    mode: str = "normal"

    @validator('ts_from', 'ts_to')
    def val_ts(cls, v):
        if not validate_timestamp(v):
            raise ValueError(f"Invalid timestamp: {v}")
        return v

class SubRequest(BaseModel):
    url: str

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

@app.post("/api/check")
async def api_check(req: CheckRequest):
    jid = str(uuid.uuid4())
    job_set(jid, "running")
    threading.Thread(target=check_worker, args=(jid, req.url), daemon=True).start()
    return {"job_id": jid}

@app.post("/api/download")
async def api_download(req: DownloadRequest):
    jid = str(uuid.uuid4())
    job_set(jid, "running")
    threading.Thread(target=download_worker, args=(jid, None, req.video_id, req.title, req.duration_sec, req.duration_str), daemon=True).start()
    return {"job_id": jid}

@app.post("/api/subtitles-only")
async def api_sub_only(req: SubRequest):
    jid = str(uuid.uuid4())
    job_set(jid, "running")
    threading.Thread(target=subtitle_only_worker, args=(jid, req.url), daemon=True).start()
    return {"job_id": jid}

@app.post("/api/subtitles-segments")
async def api_sub_segs(req: SubRequest):
    jid = str(uuid.uuid4())
    job_set(jid, "running")
    threading.Thread(target=subtitle_segments_worker, args=(jid, req.url), daemon=True).start()
    return {"job_id": jid}

@app.post("/api/cut")
async def api_cut(req: CutRequest):
    jid = str(uuid.uuid4())
    job_set(jid, "running")
    threading.Thread(target=cut_worker, args=(jid, req.source_filename, req.ts_from, req.ts_to, req.mode), daemon=True).start()
    return {"job_id": jid}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

@app.get("/api/downloads/list")
async def list_downloads():
    videos = []
    for f in sorted(DOWNLOADS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = f.stat()
        videos.append({"filename": f.name, "size_mb": round(s.st_size/1048576,2),
                        "modified": datetime.fromtimestamp(s.st_mtime).strftime("%b %d %H:%M")})
    clips = []
    for f in sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = f.stat()
        clips.append({"filename": f.name, "size_mb": round(s.st_size/1048576,2),
                       "modified": datetime.fromtimestamp(s.st_mtime).strftime("%b %d %H:%M")})
    return {"videos": videos, "clips": clips}

@app.delete("/api/downloads/{filename}")
async def del_download(filename: str):
    path = DOWNLOADS / filename
    if not path.exists(): raise HTTPException(404, "Not found")
    path.unlink()
    return {"ok": True}

@app.delete("/api/clips/{filename}")
async def del_clip(filename: str):
    path = CLIPS / filename
    if not path.exists(): raise HTTPException(404, "Not found")
    path.unlink()
    return {"ok": True}

@app.get("/api/download-file/video/{filename}")
async def dl_video(filename: str):
    path = DOWNLOADS / filename
    if not path.exists(): raise HTTPException(404, "Not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

@app.get("/api/download-file/clip/{filename}")
async def dl_clip(filename: str):
    path = CLIPS / filename
    if not path.exists(): raise HTTPException(404, "Not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
