# app.py

import os
import asyncio
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="yt-dlp server", version="3.0.0")

DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "./downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


# ── Models ─────────────────────────────────────────────

class QuickDownloadRequest(BaseModel):
    url: str


# ── yt-dlp Options ─────────────────────────────────────

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "concurrent_fragment_downloads": 3,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    },
}


# ── Core Download ──────────────────────────────────────

def download_video(url: str) -> dict:
    output_template = str(DOWNLOADS_DIR / "%(title)s.%(ext)s")
    file_path = {"value": None}

    def hook(d):
        if d["status"] == "finished":
            file_path["value"] = d.get("filename")

    ydl_opts = {
        **BASE_OPTS,
        "format": "b",
        "outtmpl": output_template,
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not file_path["value"]:
        raise RuntimeError("Download failed")

    file = Path(file_path["value"])

    return {
        "title": info.get("title"),
        "file_path": str(file),
        "filename": file.name,
        "ext": file.suffix.lstrip("."),
        "filesize": file.stat().st_size,
    }


# ── UI Route ───────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html>
<head>
  <title>yt-dlp downloader</title>
  <style>
    body {
      font-family: Arial;
      background: #0f172a;
      color: #e2e8f0;
      display: flex;
      justify-content: center;
      padding-top: 80px;
    }
    .box {
      width: 500px;
    }
    input {
      width: 100%;
      padding: 12px;
      border-radius: 8px;
      border: none;
      margin-bottom: 10px;
    }
    button {
      width: 100%;
      padding: 12px;
      background: #3b82f6;
      color: white;
      border: none;
      border-radius: 8px;
      cursor: pointer;
    }
    .result {
      margin-top: 20px;
      background: #1e293b;
      padding: 15px;
      border-radius: 8px;
    }
    a {
      color: #22c55e;
    }
  </style>
</head>
<body>

<div class="box">
  <h2>yt-dlp downloader</h2>

  <input id="url" placeholder="Paste YouTube URL..." />
  <button onclick="download()">Download</button>

  <div id="output" class="result" style="display:none;"></div>
</div>

<script>
async function download() {
  const url = document.getElementById("url").value;
  const output = document.getElementById("output");

  output.style.display = "block";
  output.innerHTML = "Processing...";

  try {
    const res = await fetch("/quick", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ url })
    });

    const data = await res.json();

    if (!data.success) {
      output.innerHTML = "Error: " + JSON.stringify(data);
      return;
    }

    const d = data.data;

    output.innerHTML = `
      <strong>${d.title}</strong><br/><br/>
      File: ${d.filename}<br/>
      Size: ${(d.filesize / 1024 / 1024).toFixed(2)} MB<br/><br/>
      <a href="${d.fetch_url}" target="_blank">Download File</a>
    `;
  } catch (err) {
    output.innerHTML = "Error: " + err.message;
  }
}
</script>

</body>
</html>
"""


# ── API Routes ─────────────────────────────────────────

@app.post("/quick")
async def quick_download(req: QuickDownloadRequest):
    try:
        result = await asyncio.to_thread(download_video, req.url)

        return {
            "success": True,
            "data": {
                **result,
                "fetch_url": f"/download/file?path={result['filename']}",
            },
        }

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/file")
async def serve_file(path: str):
    file_path = DOWNLOADS_DIR / path

    if not file_path.exists():
        raise HTTPException(404, "File not found")

    if not str(file_path.resolve()).startswith(str(DOWNLOADS_DIR.resolve())):
        raise HTTPException(403, "Access denied")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )
