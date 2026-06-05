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

# ----------------------------------------------------------------------
# DIRECTORIES
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
COOKIES = BASE_DIR / "cookies.txt"
DOWNLOADS = BASE_DIR / "downloads"
TEMP = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ----------------------------------------------------------------------
# SIMPLE JOB STORE
# ----------------------------------------------------------------------
JOBS = {}

def job_set(job_id, state, data=None, error=None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

# ----------------------------------------------------------------------
# UTILITIES
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# METADATA & SMART FORMATS (min 360p, one per resolution)
# ----------------------------------------------------------------------
def get_video_metadata(url: str) -> dict:
    cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout.strip())

def normalize_formats(metadata: dict) -> list:
    formats = metadata.get("formats", [])
    res_map = defaultdict(list)

    container_priority = {"mp4": 1, "webm": 2, "mkv": 3}
    codec_priority = {"hevc": 1, "h265": 1, "avc1": 2, "h264": 2, "vp9": 3, "av01": 4}

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
    for entries in res_map.values():
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

# ----------------------------------------------------------------------
# WORKERS
# ----------------------------------------------------------------------
def download_cache_worker(job_id, url, format_id):
    try:
        meta = get_video_metadata(url)
        video_id = meta.get("id", "unknown")
        title = meta.get("title", "untitled")
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

        if out_file.exists():
            vtt = out_file.with_suffix('.en.vtt')
            sub_text = extract_plain_text_from_vtt(vtt) if vtt.exists() else ""
            job_set(job_id, "done", {
                "filename": out_file.name,
                "subtitle_text": sub_text,
                "size_mb": get_file_size_mb(out_file)
            })
            return

        dl_cmd = ytdlp_base() + ["-f", format_id, "--merge-output-format", "mp4",
                                 "--no-playlist", "-o", str(out_file), url]
        subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600, check=True)

        vtt_path = out_file.with_suffix('.en.vtt')
        sub_cmd = ytdlp_base() + ["--skip-download", "--write-auto-subs", "--write-subs",
                                  "--sub-langs", "en", "--sub-format", "vtt",
                                  "-o", str(out_file.with_suffix('')), url]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "filename": out_file.name,
            "subtitle_text": sub_text,
            "size_mb": get_file_size_mb(out_file)
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

# ----------------------------------------------------------------------
# API ENDPOINTS
# ----------------------------------------------------------------------
class UrlRequest(BaseModel):
    url: str

class DownloadCacheRequest(BaseModel):
    url: str
    format_id: str

@app.post("/api/formats")
async def api_formats(req: UrlRequest):
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

@app.post("/api/download-cache")
async def api_download_cache(req: DownloadCacheRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=download_cache_worker, args=(job_id, req.url, req.format_id), daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

# ----------------------------------------------------------------------
# MINIMAL HTML – ONE SCREEN, NO SCROLLING, COPY BUTTON ONLY AFTER CACHE
# ----------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=yes">
<title>ClipForge · Stream & Cache</title>
<style>
  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }
  body {
    background: #0f0f11;
    color: #e8e8f0;
    font-family: system-ui, -apple-system, 'Segoe UI', monospace;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
  }
  .card {
    max-width: 700px;
    width: 100%;
    background: #1a1a1f;
    border: 1px solid #2e2e38;
    border-radius: 1.5rem;
    padding: 1.5rem;
    box-shadow: 0 8px 20px rgba(0,0,0,0.4);
  }
  h1 {
    font-size: 1.6rem;
    font-weight: 600;
    background: linear-gradient(135deg, #7c5cfc, #fc5c7d);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    margin-bottom: 0.25rem;
  }
  .sub {
    font-size: 0.8rem;
    color: #6b6b80;
    margin-bottom: 1.2rem;
  }
  input {
    width: 100%;
    background: #222228;
    border: 1px solid #2e2e38;
    color: #e8e8f0;
    padding: 0.7rem 1rem;
    border-radius: 0.8rem;
    font-size: 0.9rem;
    margin-bottom: 0.8rem;
  }
  button {
    background: #7c5cfc;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 0.8rem;
    font-weight: 500;
    cursor: pointer;
    transition: opacity 0.2s;
    font-size: 0.85rem;
  }
  button:hover { opacity: 0.85; }
  button.sec {
    background: #222228;
    border: 1px solid #2e2e38;
    color: #e8e8f0;
  }
  .row {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }
  .pb {
    background: #2e2e38;
    border-radius: 0.25rem;
    height: 3px;
    overflow: hidden;
    margin: 0.5rem 0;
  }
  .bar {
    width: 0%;
    height: 100%;
    background: linear-gradient(90deg, #7c5cfc, #fc5c7d);
    transition: width 0.2s;
  }
  .bar.spin {
    animation: spin 1.4s infinite ease-in-out;
    width: 35% !important;
  }
  @keyframes spin {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(380%); }
  }
  .msg {
    font-size: 0.75rem;
    min-height: 1.4rem;
    margin: 0.3rem 0;
  }
  .ok { color: #4caf88; }
  .err { color: #fc5c5c; }
  .vinfo {
    display: flex;
    gap: 0.8rem;
    background: #222228;
    border-radius: 1rem;
    padding: 0.8rem;
    margin: 0.8rem 0;
  }
  .vinfo img {
    width: 90px;
    border-radius: 0.5rem;
    object-fit: cover;
  }
  .vinfo div {
    overflow: hidden;
  }
  .vinfo strong {
    font-size: 0.9rem;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .vinfo span {
    font-size: 0.75rem;
    color: #6b6b80;
  }
  .format-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
  }
  .format-table th, .format-table td {
    padding: 0.5rem 0.2rem;
    text-align: left;
    border-top: 1px solid #2e2e38;
  }
  .format-table th {
    color: #6b6b80;
    font-weight: 500;
  }
  button.small {
    padding: 0.25rem 0.7rem;
    font-size: 0.7rem;
  }
  .checkbox {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.8rem 0;
  }
  .copy-row {
    display: flex;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }
  @media (max-width: 550px) {
    .card { padding: 1rem; }
    .vinfo img { width: 70px; }
    .format-table td, .format-table th { font-size: 0.7rem; padding: 0.4rem 0.1rem; }
    button.small { padding: 0.2rem 0.5rem; }
  }
</style>
</head>
<body>
<div class="card">
  <h1>⬡ ClipForge</h1>
  <div class="sub">YouTube → choose quality → stream or cache</div>

  <input type="text" id="url" placeholder="https://youtube.com/watch?v=..." />
  <div class="row">
    <button id="analyzeBtn" onclick="analyze()">Analyze</button>
  </div>

  <div class="pb"><div class="bar" id="analyzeBar"></div></div>
  <div class="msg" id="msg"></div>

  <div id="vinfo" style="display:none" class="vinfo">
    <img id="thumb" src="" />
    <div><strong id="title"></strong><span id="duration"></span></div>
  </div>

  <div id="formatContainer"></div>

  <div class="checkbox">
    <input type="checkbox" id="cacheCheckbox" />
    <label>💾 Save to server (cached) – shows subtitle copy button</label>
  </div>

  <div class="pb"><div class="bar" id="dlBar"></div></div>
  <div class="msg" id="dlMsg"></div>

  <div id="copyContainer" class="copy-row" style="display:none">
    <button id="copySubBtn" onclick="copySubtitle()" class="sec">📋 Copy subtitle</button>
  </div>
</div>

<script>
let state = { subtitleText: "", jobSubtitleText: "" };

async function analyze() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  setMsg('', 'Fetching formats...');
  setBar('analyzeBar', true);
  document.getElementById('formatContainer').innerHTML = '';
  document.getElementById('vinfo').style.display = 'none';
  document.getElementById('copyContainer').style.display = 'none';
  state.subtitleText = '';
  try {
    const res = await fetch('/api/formats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    document.getElementById('thumb').src = data.thumbnail || '';
    document.getElementById('title').innerText = data.title || '';
    document.getElementById('duration').innerText = '  Duration: ' + (data.duration_str || '?');
    document.getElementById('vinfo').style.display = 'flex';

    let html = '<table class="format-table"><thead><tr><th>Quality</th><th>Format</th><th>Size</th><th></th></tr></thead><tbody>';
    for (let f of data.formats) {
      let size = f.size_mb ? f.size_mb + ' MB' : 'unknown';
      html += `<tr>
        <td>${f.resolution}</td><td>${f.ext}</td><td>${size}</td>
        <td><button class="small" onclick="selectFormat('${f.format_id}')">Select</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('formatContainer').innerHTML = html;
    setMsg('ok', `✓ ${data.formats.length} format(s) available`);
  } catch(err) {
    setMsg('err', err.message);
  } finally {
    setBar('analyzeBar', false);
  }
}

function selectFormat(formatId) {
  const url = document.getElementById('url').value.trim();
  const cache = document.getElementById('cacheCheckbox').checked;
  if (cache) {
    setBar('dlBar', true);
    setMsg('', 'Downloading & caching...', 'dlMsg');
    fetch('/api/download-cache', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format_id: formatId })
    })
    .then(res => res.json())
    .then(data => {
      poll(data.job_id, 'dlBar', 'dlMsg', (d) => {
        state.subtitleText = d.subtitle_text || '';
        setMsg('ok', `✓ Saved: ${d.filename} (${d.size_mb} MB)`, 'dlMsg');
        if (state.subtitleText) {
          document.getElementById('copyContainer').style.display = 'flex';
        } else {
          document.getElementById('copyContainer').style.display = 'none';
        }
      });
    })
    .catch(err => { setBar('dlBar', false); setMsg('err', err.message, 'dlMsg'); });
  } else {
    window.location.href = `/api/stream?url=${encodeURIComponent(url)}&format_id=${formatId}`;
  }
}

function copySubtitle() {
  if (state.subtitleText) {
    copyToClipboard(state.subtitleText);
    setMsg('ok', '✓ Subtitle copied to clipboard', 'dlMsg');
  } else {
    setMsg('err', 'No subtitle available', 'dlMsg');
  }
}

// helpers
function setMsg(cls, txt, id='msg') {
  const el = document.getElementById(id);
  el.className = 'msg ' + (cls === 'ok' ? 'ok' : (cls === 'err' ? 'err' : ''));
  el.textContent = txt;
}
function setBar(id, on) {
  const bar = document.getElementById(id);
  if (on) { bar.classList.add('spin'); bar.style.width = '35%'; }
  else { bar.classList.remove('spin'); bar.style.width = '0%'; }
}
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
}
function poll(jobId, barId, msgId, onDone) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/api/job/' + jobId);
      const data = await res.json();
      if (data.state === 'done') {
        clearInterval(interval);
        setBar(barId, false);
        onDone(data.data);
      } else if (data.state === 'error') {
        clearInterval(interval);
        setBar(barId, false);
        setMsg('err', '✗ ' + data.error, msgId);
      }
    } catch(e) {
      clearInterval(interval);
      setBar(barId, false);
      setMsg('err', 'Network error', msgId);
    }
  }, 900);
}
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
