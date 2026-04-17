import os
import uuid
import glob
import json
import subprocess
import tempfile
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

AUDIO_FORMATS = ["mp3", "aac", "opus", "flac", "wav", "m4a"]


def _cookie_to_tmp(cookie_content):
    """Write cookie content to a temp file, return path. Caller must clean up."""
    if not cookie_content:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="reclip_cookies_")
    try:
        os.write(fd, cookie_content.encode("utf-8"))
        os.close(fd)
        return path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(path):
            os.remove(path)
        return None


def _cleanup_cookie(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def run_download(job_id, url, format_choice, format_id, audio_format="mp3"):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        safe_audio_fmt = audio_format if audio_format in AUDIO_FORMATS else "mp3"
        cmd += ["-x", "--audio-format", safe_audio_fmt]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            safe_audio_fmt = audio_format if audio_format in AUDIO_FORMATS else "mp3"
            target = [f for f in files if f.endswith(f".{safe_audio_fmt}")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    cookie_content = data.get("cookie")
    cookie_path = None
    if cookie_content:
        cookie_path = _cookie_to_tmp(cookie_content)
        if cookie_path:
            cmd += ["--cookies", cookie_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if cookie_path:
            _cleanup_cookie(cookie_path)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — group by height, keep best per height+codec
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                key = height
                tbr = f.get("tbr") or 0
                if key not in best_by_height or tbr > (best_by_height[key].get("tbr") or 0):
                    best_by_height[key] = f

        formats = []
        for height, f in sorted(best_by_height.items(), key=lambda x: x[0], reverse=True):
            vcodec = f.get("vcodec", "none")
            codec_label = vcodec.split(".")[0] if vcodec != "none" else ""
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
                "codec": codec_label,
            })

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    audio_format = data.get("audio_format", "mp3")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if audio_format not in AUDIO_FORMATS:
        audio_format = "mp3"

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, audio_format))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job.get("filename"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
