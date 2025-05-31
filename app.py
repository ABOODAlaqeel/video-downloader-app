from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import subprocess
import json
import re
import uuid
import requests  # لاستخدام YouTube Data API

app = Flask(__name__)
# Allow requests from any origin for development. Be more specific in production.
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Create a directory for downloads in /tmp (Render يسمح فقط بالكتابة في /tmp)
DOWNLOAD_FOLDER = "/tmp/video_downloads"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

app.config["DOWNLOAD_FOLDER"] = DOWNLOAD_FOLDER

# --- YouTube Data API Key ---
# ضع مفتاحك هنا أو كمتغير بيئي: os.environ.get("YOUTUBE_API_KEY")
YOUTUBE_API_KEY = "AIzaSyAn2ullYW1sX6Na3do1jUntncu--COqHsY"

# --- Helper Functions --- #

def get_yt_dlp_path():
    # في بيئة Render (Linux)، yt-dlp يُنصب عبر pip ويصبح في PATH
    return "yt-dlp"

def is_valid_url(url):
    return re.match(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/.+", url)

def sanitize_filename(name):
    # Remove invalid characters for filenames
    name = re.sub(r'[\\/*?"<>|]', "", name)
    name = re.sub(r'\s+', '_', name)
    return name[:100]  # Limit filename length

def extract_youtube_video_id(url):
    """
    تحاول استخراج معرف الفيديو من روابط YouTube أو youtu.be
    """
    # أمثلة الروابط:
    #  https://www.youtube.com/watch?v=VIDEO_ID
    #  https://youtu.be/VIDEO_ID
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"youtu\.be\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def fetch_metadata_via_youtube_api(video_id):
    """
    يستدعي YouTube Data API للحصول على عنوان الفيديو، قناة اليوزر، والصورة المصغرة
    """
    api_url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    resp = requests.get(api_url, timeout=10)
    if resp.status_code != 200:
        return None, f"YouTube Data API error: {resp.text}"
    data = resp.json()
    items = data.get("items", [])
    if not items:
        return None, "No video found via YouTube Data API."
    snippet = items[0]["snippet"]
    title = snippet.get("title", "N/A")
    thumbnail = snippet.get("thumbnails", {}).get("high", {}).get("url") or snippet.get("thumbnails", {}).get("default", {}).get("url")
    uploader = snippet.get("channelTitle", "N/A")
    return {"title": title, "thumbnail": thumbnail, "uploader": uploader}, None

# --- API Endpoints --- #

@app.route("/")
def hello_world():
    return "Video Downloader Backend is running!"

@app.route("/api/video-info", methods=["POST"])
def get_video_info():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL. Only YouTube and Twitter/X links are supported."}), 400

    # أولاً، نجرب إذا كان رابط YouTube: نحصل على الميتاداتا عبر YouTube Data API
    video_meta = {"title": None, "thumbnail": None, "uploader": None}
    video_id = extract_youtube_video_id(url)
    if video_id:
        meta, err = fetch_metadata_via_youtube_api(video_id)
        if err:
            # إذا فشل استدعاء الـ API، نصعد الخطأ في الـ JSON
            return jsonify({"error": err}), 500
        video_meta = meta
        original_url = f"https://www.youtube.com/watch?v={video_id}"
    else:
        original_url = url  # إذا لم يكن YouTube، نستخدم الرابط الأصلي كما هو

    yt_dlp_path = get_yt_dlp_path()

    # نستخدم yt-dlp لاستخراج الصيغ. نحاول بدون كوكيز أولاً.
    command = [
        yt_dlp_path,
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        original_url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
        video_data = json.loads(result.stdout)

        # في حال لم نقم باستدعاء YouTube Data API أو فشل، نعتمد على yt-dlp للحصول على البيانات
        if not video_meta["title"]:
            video_meta["title"] = video_data.get("title", "N/A")
        if not video_meta["thumbnail"]:
            video_meta["thumbnail"] = video_data.get("thumbnail")
        if not video_meta["uploader"]:
            video_meta["uploader"] = video_data.get("uploader", "N/A")

        # --- استخراج الصيغ المتوفرة ---
        formats = []
        for f in video_data.get("formats", []):
            fmt_id = f.get("format_id", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            url_f = f.get("url")

            # نعرض كل صيغة تحتوي فيديو
            if vcodec != "none" and url_f and (f.get("format_note") or f.get("height")):
                formats.append({
                    "format_id": fmt_id,
                    "resolution": f.get("format_note", f.get("height")),
                    "height": f.get("height"),
                    "ext": f.get("ext"),
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "has_audio": (acodec != "none"),
                    "download_url": url_f,
                    "http_headers": f.get("http_headers", {})
                })

        # نرتب الصيغ بحسب الدقة تنازليًا
        formats.sort(key=lambda x: x.get("height", 0), reverse=True)

        # --- استخراج الترجمات (subtitles) ---
        subtitles = {}
        for lang, subs in video_data.get("subtitles", {}).items():
            vtt_sub = next((s for s in subs if s.get("ext") == "vtt"), subs[0] if subs else None)
            if vtt_sub:
                subtitles[lang] = {
                    "name": vtt_sub.get("name", lang),
                    "url": vtt_sub.get("url"),
                    "ext": vtt_sub.get("ext")
                }

        # --- استخراج الترجمات التلقائية (automatic_captions) ---
        auto_captions = {}
        for lang, caps in video_data.get("automatic_captions", {}).items():
            vtt_cap = next((c for c in caps if c.get("ext") == "vtt"), caps[0] if caps else None)
            if vtt_cap:
                auto_captions[lang] = {
                    "name": vtt_cap.get("name", lang + " (auto)"),
                    "url": vtt_cap.get("url"),
                    "ext": vtt_cap.get("ext")
                }

        response_data = {
            "title": video_meta["title"],
            "thumbnail": video_meta["thumbnail"],
            "uploader": video_meta["uploader"],
            "formats": formats,
            "subtitles": subtitles,
            "automatic_captions": auto_captions,
            "original_url": original_url
        }
        return jsonify(response_data)

    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        # في حال طلب YouTube تسجيل دخول (Bot check)، نخبر الواجهة بذلك
        if "Sign in to confirm you’re not a bot" in stderr:
            return jsonify({
                "error": "YouTube requires login (كوكيز) للوصول إلى هذا الفيديو. "
                         "API Key لا يكفي لتنزيل الفيديو محميًّا."
            }), 403
        return jsonify({"error": f"yt-dlp error: {stderr}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out. The video might be too long or the server is busy."}), 504
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse video data from yt-dlp."}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/video-info: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route("/api/download-video", methods=["POST"])
def download_video():
    data = request.get_json()
    url = data.get("url")
    format_id = data.get("format_id")
    title = data.get("title", "video")

    if not url or not format_id:
        return jsonify({"error": "URL and format ID are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config['DOWNLOAD_FOLDER'], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    output_template = os.path.join(specific_download_path, f"{safe_title}.%(ext)s")

    # نجرب تنزيل الصيغة المحددة بدون كوكيز أولاً
    merged_format = f"{format_id}+bestaudio"
    command = [
        yt_dlp_path,
        "-f", merged_format,
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--no-warnings",
        "--no-playlist",
        url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)
        downloaded_files = os.listdir(specific_download_path)
        if not downloaded_files:
            return jsonify({"error": "Download failed, no file found."}), 500

        filename = downloaded_files[0]
        return jsonify({"download_url": f"/api/serve/{download_id}/{filename}"})

    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "Sign in to confirm you’re not a bot" in stderr:
            return jsonify({
                "error": "YouTube يتطلب تسجيل دخول (كوكيز) لتنزيل هذا الفيديو. API Key لا يكفي لتنزيل فيديو محميّ."
            }), 403
        return jsonify({"error": f"yt-dlp download error: {stderr}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out. The video might be too large or the connection slow."}), 504
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/download-video: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred during download: {str(e)}"}), 500

@app.route("/api/download-subtitle", methods=["POST"])
def download_subtitle():
    data = request.get_json()
    url = data.get("url")
    lang = data.get("lang")
    is_auto = data.get("is_auto", False)
    title = data.get("title", "subtitle")

    if not url or not lang:
        return jsonify({"error": "URL and language code are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config['DOWNLOAD_FOLDER'], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    output_template = os.path.join(specific_download_path, f"{safe_title}.{lang}.%(ext)s")

    command = [
        yt_dlp_path,
        "--skip-download",
        "--write-subs" if not is_auto else "--write-auto-subs",
        "--sub-langs", lang,
        "--sub-format", "vtt",
        "-o", output_template,
        "--no-warnings",
        "--no-playlist",
        url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
        downloaded_files = os.listdir(specific_download_path)
        if not downloaded_files:
            if f"No subtitles for language {lang}" in result.stderr:
                return jsonify({"error": f"No subtitles available for language: {lang}"}), 404
            return jsonify({"error": "Subtitle download failed, no file found."}), 500

        filename = downloaded_files[0]
        return jsonify({"download_url": f"/api/serve/{download_id}/{filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Subtitle download timed out."}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"yt-dlp subtitle download error: {e.stderr}"}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/download-subtitle: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred during subtitle download: {str(e)}"}), 500

@app.route('/api/serve/<download_id>/<filename>')
def serve_file(download_id, filename):
    directory = os.path.join(app.config['DOWNLOAD_FOLDER'], download_id)
    try:
        safe_path = os.path.abspath(os.path.join(directory, filename))
        if not safe_path.startswith(os.path.abspath(directory)):
            return jsonify({"error": "Invalid path"}), 400

        return send_from_directory(directory, filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "File not found."}), 404
    except Exception as e:
        app.logger.error(f"Error serving file {filename} from {download_id}: {e}", exc_info=True)
        return jsonify({"error": "Could not serve file."}), 500

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
