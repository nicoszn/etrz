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

# ---------------------------------------------------------------------------
# DIRECTORIES
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
COOKIES = BASE_DIR / "cookies.txt"
DOWNLOADS = BASE_DIR / "downloads"
TEMP = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="YTExtract (Beta)")

# ---------------------------------------------------------------------------
# JOB STORE (simple, no cleanup)
# ---------------------------------------------------------------------------
JOBS = {}

def job_set(job_id, state, data=None, error=None):
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

# ---------------------------------------------------------------------------
# METADATA & FORMATS (smart filter, min 360p, one per resolution)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# WORKERS
# ---------------------------------------------------------------------------
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

def subtitle_worker(job_id, url):
    try:
        meta = get_video_metadata(url)
        title = meta.get("title", "untitled")
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

# ---------------------------------------------------------------------------
# API ENDPOINTS (only needed ones)
# ---------------------------------------------------------------------------
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

@app.post("/api/subtitle")
async def api_subtitle(req: UrlRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

# ---------------------------------------------------------------------------
# MINIMAL HTML (video source only)
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>YTE · Video & Subtitle</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f11;color:#e8e8f0;font-family:monospace;padding:2rem}
.container{max-width:900px;margin:0 auto}
.card{background:#1a1a1f;border:1px solid #2e2e38;border-radius:16px;padding:24px}
h1{font-size:1.5rem;margin-bottom:0.5rem}
.sub{color:#6b6b80;margin-bottom:1.5rem}
input,button{font-family:inherit}
input[type=text]{width:100%;background:#222228;border:1px solid #2e2e38;color:#e8e8f0;padding:10px;border-radius:8px;margin-bottom:12px}
.row{display:flex;gap:10px;margin-bottom:16px}
button{background:#7c5cfc;color:#fff;border:none;padding:8px 16px;border-radius:8px;cursor:pointer}
button:hover{opacity:0.85}
button.sec{background:#222228;border:1px solid #2e2e38;color:#e8e8f0}
.pw{background:#2e2e38;border-radius:4px;height:3px;margin:8px 0}
.pb{height:100%;width:0%;background:linear-gradient(90deg,#7c5cfc,#fc5c7d);transition:width 0.3s}
.pb.spin{animation:spin 1.4s infinite;width:35%}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(380%)}}
.msg{font-size:12px;min-height:20px;margin:8px 0}
.msg.ok{color:#4caf88}.msg.err{color:#fc5c5c}
.vinfo{display:flex;gap:12px;background:#222228;border-radius:12px;padding:12px;margin-bottom:16px}
.vinfo img{width:120px;border-radius:8px}
.vinfo div{flex:1}
table{width:100%;border-collapse:collapse;margin:12px 0}
th,td{padding:8px;text-align:left;border-top:1px solid #2e2e38}
button.small{padding:4px 12px;font-size:11px}
.checkbox{display:flex;align-items:center;gap:8px;margin:12px 0}
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>⬡ ClipForge · Stream & Subtitle</h1>
    <div class="sub">YouTube → choose quality → stream or cache</div>

    <input type="text" id="url" placeholder="https://youtube.com/watch?v=..." />
    <div class="row">
      <button id="analyze" onclick="analyze()">Analyze</button>
      <button class="sec" id="subtitleBtn" onclick="fetchSubtitle()" disabled>Subtitle (copy)</button>
    </div>

    <div class="pw"><div class="pb" id="analyzePb"></div></div>
    <div class="msg" id="msg"></div>

    <div id="vinfo" style="display:none" class="vinfo">
      <img id="thumb" src="" />
      <div><strong id="title"></strong><br/><span id="duration"></span></div>
    </div>

    <div id="formatContainer"></div>

    <div class="checkbox">
      <input type="checkbox" id="cacheCheckbox" />
      <label>Save to Library (cached on server)</label>
    </div>

    <div class="pw"><div class="pb" id="dlPb"></div></div>
    <div class="msg" id="dlMsg"></div>
  </div>
</div>

<script>
let subtitleText = "";

async function analyze() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  setMsg('', 'Fetching formats...');
  setPb('analyzePb', true);
  document.getElementById('formatContainer').innerHTML = '';
  document.getElementById('vinfo').style.display = 'none';
  document.getElementById('subtitleBtn').disabled = true;
  try {
    const res = await fetch('/api/formats', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    document.getElementById('thumb').src = data.thumbnail || '';
    document.getElementById('title').innerText = data.title;
    document.getElementById('duration').innerText = 'Duration: ' + (data.duration_str || '?');
    document.getElementById('vinfo').style.display = 'flex';

    let html = '<table><thead><tr><th>Quality</th><th>Format</th><th>Size</th><th></th></tr></thead><tbody>';
    for (let f of data.formats) {
      let size = f.size_mb ? f.size_mb + ' MB' : 'unknown';
      html += `<tr>
        <td>${f.resolution}</td><td>${f.ext}</td><td>${size}</td>
        <td><button class="small" onclick="selectFormat('${f.format_id}')">Select</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('formatContainer').innerHTML = html;
    setMsg('ok', `✓ ${data.formats.length} formats available`);
    document.getElementById('subtitleBtn').disabled = false;
  } catch(err) {
    setMsg('err', err.message);
  } finally {
    setPb('analyzePb', false);
  }
}

function selectFormat(formatId) {
  const url = document.getElementById('url').value.trim();
  const cache = document.getElementById('cacheCheckbox').checked;
  if (cache) {
    setPb('dlPb', true);
    setMsg('', 'Downloading & saving...', 'dlMsg');
    fetch('/api/download-cache', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url, format_id: formatId})
    })
    .then(res => res.json())
    .then(data => {
      poll(data.job_id, 'dlPb', 'dlMsg', (d) => {
        subtitleText = d.subtitle_text || '';
        setMsg('ok', `✓ Saved: ${d.filename} (${d.size_mb} MB)`, 'dlMsg');
      });
    })
    .catch(err => { setPb('dlPb', false); setMsg('err', err.message, 'dlMsg'); });
  } else {
    window.location.href = `/api/stream?url=${encodeURIComponent(url)}&format_id=${formatId}`;
  }
}

async function fetchSubtitle() {
  const url = document.getElementById('url').value.trim();
  if (!url) { setMsg('err', 'Enter a URL first', 'dlMsg'); return; }
  setPb('dlPb', true);
  setMsg('', 'Fetching subtitles...', 'dlMsg');
  try {
    const res = await fetch('/api/subtitle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    poll(data.job_id, 'dlPb', 'dlMsg', (d) => {
      if (d.subtitle_text) {
        copyToClipboard(d.subtitle_text);
        setMsg('ok', '✓ Subtitle copied to clipboard', 'dlMsg');
      } else {
        setMsg('err', 'No English subtitles found', 'dlMsg');
      }
    });
  } catch(err) {
    setPb('dlPb', false);
    setMsg('err', err.message, 'dlMsg');
  }
}

function setMsg(cls, txt, id='msg') {
  const el = document.getElementById(id);
  el.className = 'msg ' + (cls === 'ok' ? 'ok' : (cls === 'err' ? 'err' : ''));
  el.textContent = txt;
}
function setPb(id, on) {
  const pb = document.getElementById(id);
  if (on) { pb.classList.add('spin'); pb.style.width = '35%'; }
  else { pb.classList.remove('spin'); pb.style.width = '0%'; }
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
function poll(jobId, pbId, msgId, onDone) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/api/job/' + jobId);
      const data = await res.json();
      if (data.state === 'done') {
        clearInterval(interval);
        setPb(pbId, false);
        onDone(data.data);
      } else if (data.state === 'error') {
        clearInterval(interval);
        setPb(pbId, false);
        setMsg('err', '✗ ' + data.error, msgId);
      }
    } catch(e) {
      clearInterval(interval);
      setPb(pbId, false);
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
