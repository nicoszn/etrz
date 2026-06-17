import os
import re
import json
import uuid
import subprocess
import threading
from pathlib import Path
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
COOKIES = BASE_DIR / "cookies.txt"
DOWNLOADS = BASE_DIR / "downloads"
TEMP = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="YTE")

JOBS = {}

def job_set(job_id, state, data=None, error=None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

def seconds_to_ts(s: float) -> str:
    s = round(s, 3)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

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

def get_file_size_mb(path):
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else 0.0

def get_video_metadata(url: str) -> dict:
    cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
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

        container_score = container_priority.get(ext, 99)
        codec_score = codec_priority.get(codec, 99)
        has_both = (f.get("vcodec") != "none" and f.get("acodec") != "none")
        both_bonus = -10 if has_both else 0
        size_score = size_mb if size_mb is not None else 1e9

        rank = (container_score, codec_score, size_score, both_bonus)
        res_map[height].append({
            "format_id": f["format_id"],
            "resolution": f"{height}p",
            "ext": ext,
            "size_mb": size_mb,
            "rank": rank
        })

    unique = []
    for height, entries in res_map.items():
        entries.sort(key=lambda x: x["rank"])
        best = entries[0]
        unique.append({
            "format_id": best["format_id"],
            "resolution": best["resolution"],
            "ext": best["ext"],
            "size_mb": best["size_mb"],
        })
    unique.sort(key=lambda x: int(x["resolution"].rstrip("p")), reverse=True)
    return unique

def subtitle_worker(job_id, url):
    try:
        meta = get_video_metadata(url)
        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd = ytdlp_base() + ["--skip-download", "--write-auto-subs", "--write-subs",
                                  "--sub-langs", "en", "--sub-format", "vtt",
                                  "-o", str(tmp_base), url]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)
        job_set(job_id, "done", {"subtitle_text": sub_text})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

class UrlRequest(BaseModel):
    url: str

@app.post("/api/formats")
async def api_formats(req: UrlRequest):
    try:
        meta = get_video_metadata(req.url)
        formats = normalize_formats(meta)

        job_id = str(uuid.uuid4())
        job_set(job_id, "running")
        threading.Thread(target=subtitle_worker, args=(job_id, req.url), daemon=True).start()

        return {
            "video_id": meta.get("id"),
            "title": meta.get("title"),
            "thumbnail": meta.get("thumbnail"),
            "duration_str": seconds_to_ts(meta.get("duration", 0)),
            "formats": formats,
            "subtitle_job_id": job_id
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/stream")
async def stream_video(url: str, format_id: str):
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
    return JOBS.get(job_id, {"state": "not_found"})

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
  --border: #1e1e26;
  --border2: #2a2a35;
  --text: #e2e2ea;
  --muted: #55556a;
  --accent: #c8ff57;
  --accent-dim: rgba(200,255,87,0.08);
  --accent-glow: rgba(200,255,87,0.15);
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

.wrap {
  width: 100%;
  max-width: 680px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 2px 28px;
}

.logo {
  font-family: 'Syne', sans-serif;
  font-weight: 800;
  font-size: 1.05rem;
  letter-spacing: 0.18em;
  color: var(--accent);
  text-transform: uppercase;
}

.logo-sub {
  font-size: 0.65rem;
  color: var(--muted);
  letter-spacing: 0.04em;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r);
  overflow: hidden;
}

.panel + .panel { margin-top: 1px; }

.input-row {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0;
}

.url-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text);
  font-family: 'DM Mono', monospace;
  font-size: 0.78rem;
  padding: 18px 20px;
  caret-color: var(--accent);
  letter-spacing: 0.01em;
}

.url-input::placeholder { color: var(--muted); }

.analyze-btn {
  background: var(--accent);
  color: #080809;
  border: none;
  font-family: 'Syne', sans-serif;
  font-weight: 700;
  font-size: 0.68rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding: 10px 20px;
  margin: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
  transition: opacity 0.15s, transform 0.1s;
  flex-shrink: 0;
}

.analyze-btn:hover { opacity: 0.85; }
.analyze-btn:active { transform: scale(0.97); }
.analyze-btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }

.divider { height: 1px; background: var(--border); }

.progress-track {
  height: 2px;
  background: var(--border);
  position: relative;
  overflow: hidden;
  opacity: 0;
  transition: opacity 0.2s;
}

.progress-track.active { opacity: 1; }

.progress-bar {
  height: 100%;
  width: 0%;
  background: var(--accent);
  transition: width 0.3s ease;
  border-radius: 2px;
}

.progress-bar.indeterminate {
  width: 40%;
  animation: slide 1.2s ease-in-out infinite;
}

@keyframes slide {
  0% { transform: translateX(-120%); }
  100% { transform: translateX(340%); }
}

.status {
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  padding: 12px 20px;
  color: var(--muted);
  min-height: 42px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.status.ok { color: var(--accent); }
.status.err { color: var(--err); background: var(--err-dim); }
.status:empty { display: none; }

.status-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.meta-panel {
  display: none;
  padding: 20px;
  gap: 16px;
  align-items: flex-start;
}

.meta-panel.visible { display: flex; }

.thumb {
  width: 100px;
  aspect-ratio: 16/9;
  object-fit: cover;
  border-radius: 6px;
  flex-shrink: 0;
  background: var(--surface2);
}

.meta-info { flex: 1; min-width: 0; }

.meta-title {
  font-family: 'Syne', sans-serif;
  font-weight: 600;
  font-size: 0.88rem;
  line-height: 1.35;
  margin-bottom: 6px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.meta-duration {
  font-size: 0.65rem;
  color: var(--muted);
  letter-spacing: 0.06em;
}

.formats-panel { display: none; }
.formats-panel.visible { display: block; }

.formats-header {
  display: grid;
  grid-template-columns: 1fr 60px 80px 90px;
  gap: 8px;
  padding: 10px 20px;
  font-size: 0.6rem;
  color: var(--muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.format-row {
  display: grid;
  grid-template-columns: 1fr 60px 80px 90px;
  gap: 8px;
  padding: 13px 20px;
  align-items: center;
  border-bottom: 1px solid var(--border);
  transition: background 0.12s;
  cursor: pointer;
}

.format-row:last-child { border-bottom: none; }
.format-row:hover { background: var(--surface2); }

.res-badge {
  font-family: 'Syne', sans-serif;
  font-weight: 700;
  font-size: 0.8rem;
  color: var(--text);
}

.ext-badge {
  font-size: 0.65rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.size-val {
  font-size: 0.72rem;
  color: var(--muted);
}

.dl-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--text);
  font-family: 'DM Mono', monospace;
  font-size: 0.65rem;
  letter-spacing: 0.06em;
  padding: 6px 12px;
  border-radius: 5px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
  white-space: nowrap;
}

.dl-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-dim);
}

.actions-row {
  display: none;
  padding: 16px 20px;
  border-top: 1px solid var(--border);
  gap: 10px;
}

.actions-row.visible { display: flex; }

.sub-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--muted);
  font-family: 'DM Mono', monospace;
  font-size: 0.65rem;
  letter-spacing: 0.06em;
  padding: 7px 14px;
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.15s;
}

.sub-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-dim);
}

.copied-flash {
  font-size: 0.65rem;
  color: var(--accent);
  opacity: 0;
  transition: opacity 0.3s;
  align-self: center;
  letter-spacing: 0.06em;
}

.copied-flash.show { opacity: 1; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="logo">YTE</span>
    <span class="logo-sub">youtube extractor</span>
  </header>

  <div class="panel">
    <div class="input-row">
      <input class="url-input" id="url" type="text" placeholder="paste youtube url" autocomplete="off" spellcheck="false" />
      <button class="analyze-btn" id="analyzeBtn" onclick="analyze()">Run</button>
    </div>

    <div class="progress-track" id="track">
      <div class="progress-bar indeterminate" id="bar"></div>
    </div>

    <div class="status" id="status"></div>

    <div class="meta-panel" id="meta">
      <img class="thumb" id="thumb" src="" alt="" />
      <div class="meta-info">
        <div class="meta-title" id="title"></div>
        <div class="meta-duration" id="duration"></div>
      </div>
    </div>

    <div class="divider" id="metaDivider" style="display:none"></div>

    <div class="formats-panel" id="formats">
      <div class="formats-header">
        <span>Resolution</span>
        <span>Ext</span>
        <span>Size</span>
        <span></span>
      </div>
      <div id="formatRows"></div>
    </div>

    <div class="actions-row" id="actionsRow">
      <button class="sub-btn" id="subBtn" onclick="copySubtitle()" style="display:none">copy transcript</button>
      <span class="copied-flash" id="copiedFlash">copied</span>
    </div>
  </div>
</div>

<script>
let _subtitleText = "";
let _subtitleJobId = null;
let _subtitleReady = false;
let _subtitlePollTimer = null;
let _currentUrl = "";

function setStatus(type, msg) {
  const el = document.getElementById('status');
  el.className = 'status' + (type ? ' ' + type : '');
  el.innerHTML = msg ? `<span class="status-dot"></span>${msg}` : '';
}

function setLoading(on) {
  const track = document.getElementById('track');
  track.classList.toggle('active', on);
  document.getElementById('analyzeBtn').disabled = on;
}

function showMeta(data) {
  document.getElementById('thumb').src = data.thumbnail || '';
  document.getElementById('title').textContent = data.title || '';
  document.getElementById('duration').textContent = data.duration_str || '';
  document.getElementById('meta').classList.add('visible');
  document.getElementById('metaDivider').style.display = '';
}

function showFormats(formats) {
  const rows = document.getElementById('formatRows');
  rows.innerHTML = '';
  formats.forEach(f => {
    const row = document.createElement('div');
    row.className = 'format-row';
    row.innerHTML = `
      <span class="res-badge">${f.resolution}</span>
      <span class="ext-badge">${f.ext}</span>
      <span class="size-val">${f.size_mb ? f.size_mb + ' mb' : '—'}</span>
      <button class="dl-btn" onclick="download('${f.format_id}')">download</button>
    `;
    rows.appendChild(row);
  });
  document.getElementById('formats').classList.add('visible');
}

function pollSubtitle(jobId) {
  _subtitlePollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/job/' + jobId);
      const data = await res.json();
      if (data.state === 'done') {
        clearInterval(_subtitlePollTimer);
        if (data.data.subtitle_text) {
          _subtitleText = data.data.subtitle_text;
          _subtitleReady = true;
          document.getElementById('subBtn').style.display = '';
        }
      } else if (data.state === 'error') {
        clearInterval(_subtitlePollTimer);
      }
    } catch { clearInterval(_subtitlePollTimer); }
  }, 900);
}

async function analyze() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  _currentUrl = url;
  _subtitleText = "";
  _subtitleReady = false;
  _subtitleJobId = null;
  if (_subtitlePollTimer) clearInterval(_subtitlePollTimer);

  document.getElementById('subBtn').style.display = 'none';
  document.getElementById('actionsRow').classList.remove('visible');
  document.getElementById('meta').classList.remove('visible');
  document.getElementById('formats').classList.remove('visible');
  document.getElementById('metaDivider').style.display = 'none';
  document.getElementById('formatRows').innerHTML = '';

  setLoading(true);
  setStatus('', 'fetching...');

  try {
    const res = await fetch('/api/formats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    showMeta(data);
    showFormats(data.formats);
    setStatus('ok', `${data.formats.length} formats`);
    document.getElementById('actionsRow').classList.add('visible');

    if (data.subtitle_job_id) {
      _subtitleJobId = data.subtitle_job_id;
      pollSubtitle(_subtitleJobId);
    }
  } catch (err) {
    setStatus('err', err.message);
  } finally {
    setLoading(false);
  }
}

function download(formatId) {
  const url = _currentUrl;
  if (!url) return;
  window.location.href = `/api/stream?url=${encodeURIComponent(url)}&format_id=${formatId}`;
}

async function copySubtitle() {
  if (!_subtitleText) return;
  try {
    await navigator.clipboard.writeText(_subtitleText);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = _subtitleText;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
  const flash = document.getElementById('copiedFlash');
  flash.classList.add('show');
  setTimeout(() => flash.classList.remove('show'), 1800);
}

document.getElementById('url').addEventListener('keydown', e => {
  if (e.key === 'Enter') analyze();
});
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)
