from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import subprocess
import json
import re
import uuid

app = Flask(__name__)
# Allow requests from any origin for development. Be more specific in production.
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Create a directory for downloads if it doesn't exist
DOWNLOAD_FOLDER = "/home/ubuntu/video_downloads"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

app.config["DOWNLOAD_FOLDER"] = DOWNLOAD_FOLDER

# --- Helper Functions --- #

import platform

def get_yt_dlp_path():
    return r"C:\Users\alaqe\anaconda3\Scripts\yt-dlp.exe"

def is_valid_url(url):
    # Basic check for YouTube or Twitter URLs
    return re.match(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/.+", url)

def sanitize_filename(name):
    # Remove invalid characters for filenames
    name = re.sub(r'[\\/*?"<>|]', "", name)
    # Replace spaces with underscores
    name = re.sub(r'\s+', '_', name)
    # Limit length
    return name[:100] # Limit filename length

# --- API Endpoints --- #

@app.route("/")
def hello_world():
    return "Video Downloader Backend is running!"
@app.route("/api/video-info", methods=["POST"])
def get_video_info():
    data = request.get_json()
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL. Only YouTube and Twitter/X links are supported."}), 400

    yt_dlp_path = get_yt_dlp_path()
    command = [
        yt_dlp_path,
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        url
    ]

    try:
        # نستدعي yt-dlp لإخراج بيانات الفيديو دون تحميله فعليًا
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
        video_data = json.loads(result.stdout)

        # --- معلومات أساسية ---
        title = video_data.get("title", "N/A")
        thumbnail = video_data.get("thumbnail", None)
        uploader = video_data.get("uploader", "N/A")
        original_url = video_data.get("original_url", url)

        # --- استخراج الصيغ الأصلية (Progressive) (فيديو + صوت فقط) ---
        formats = []
        for f in video_data.get("formats", []):
            fmt_id = f.get("format_id", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            url_f = f.get("url")  # هذا هو الرابط المباشر للصيغة (قد يحتاج إلى رؤوس معينة لتحميله)

            # شروط اختيار الصيغ الأصلية المباشرة:
            # 1) أن تكون صيغة progressive، أي vcodec != "none" و acodec != "none".
            # 2) أن لا يحتوي format_id على “+” (كي نتأكد أنها ليست صيغة بحاجة إلى دمج خارجي).
            # 3) أن تكون لها ملاحظة تنسيق أو ارتفاع لنظهر للمستخدم وصفًا واضحًا.
            if (
                vcodec != "none"
                and acodec != "none"
                and "+" not in fmt_id
                and url_f
                and (f.get("format_note") or f.get("height"))
            ):
                # بالإضافة للحقول القديمة، نضيف:
                #   - download_url: رابط التنزيل المباشر
                #   - http_headers: أي رؤوس HTTP يجب تزويدها عند تنزيل هذا الرابط
                formats.append({
                    "format_id": fmt_id,
                    "resolution": f.get("format_note", f.get("height")),
                    "height": f.get("height"),
                    "ext": f.get("ext"),
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "has_audio": True,
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

        # --- نُعيد كل شيء في JSON واحد للواجهة الأمامية ---
        response_data = {
            "title": title,
            "thumbnail": thumbnail,
            "uploader": uploader,
            "formats": formats,
            "subtitles": subtitles,
            "automatic_captions": auto_captions,
            "original_url": original_url
        }

        return jsonify(response_data)

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out. The video might be too long or the server is busy."}), 504
    except subprocess.CalledProcessError as e:
        error_message = f"yt-dlp error: {e.stderr}"
        if "Unsupported URL" in e.stderr:
            error_message = "Unsupported URL or video not found."
        elif "Video unavailable" in e.stderr:
            error_message = "This video is unavailable."
        return jsonify({"error": error_message}), 500
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse video data from yt-dlp."}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/video-info: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route("/api/download-video", methods=["POST"])
def download_video():
    data = request.get_json()
    url = data.get("url")  # Use the original URL passed back from video-info
    format_id = data.get("format_id")
    title = data.get("title", "video")  # Get title for filename

    if not url or not format_id:
        return jsonify({"error": "URL and format ID are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    # Generate a unique sub-folder for this download to avoid filename conflicts
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config['DOWNLOAD_FOLDER'], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    # Sanitize title for filename and define output template
    safe_title = sanitize_filename(title)
    output_template = os.path.join(specific_download_path, f"{safe_title}.%(ext)s")

    # --- التغيير الأساسي هنا: دمج الصيغة المختارة + أفضل صوت ---
    # إذا كانت الصيغة المختارة تحتوي فقط على فيديو (بدون صوت)، فسوف يضيف yt-dlp تلقائيًا أفضل مسار صوت متاح (bestaudio).
    # كما نضيف خيار --merge-output-format لدمج الملفات بصيغة MP4 إن لزم الأمر.
    merged_format = f"{format_id}+bestaudio"
    command = [
        yt_dlp_path,
        "-f", merged_format,
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--no-warnings",
        "--no-playlist",  # Ensure only single video is downloaded
        url
    ]

    try:
        # Increased timeout for download
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)

        # Find the downloaded file
        downloaded_files = os.listdir(specific_download_path)
        if not downloaded_files:
            return jsonify({"error": "Download failed, no file found."}), 500

        filename = downloaded_files[0]
        # Return a URL or identifier that the frontend can use to fetch the file
        # Using a separate endpoint to serve the file is safer
        return jsonify({"download_url": f"/api/serve/{download_id}/{filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out. The video might be too large or the connection slow."}), 504
    except subprocess.CalledProcessError as e:
        error_message = f"yt-dlp download error: {e.stderr}"
        if "HTTP Error 403" in e.stderr:
            error_message = "Access denied (403). The video might be private or require login."
        return jsonify({"error": error_message}), 500
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
        "--skip-download", # Only download subtitles
        "--write-subs" if not is_auto else "--write-auto-subs",
        "--sub-langs", lang,
        "--sub-format", "vtt", # Prefer vtt format
        "-o", output_template,
        "--no-warnings",
        "--no-playlist",
        url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)

        downloaded_files = os.listdir(specific_download_path)
        if not downloaded_files:
            # Check stderr for specific messages if no file is found
            if f"No subtitles for language {lang}" in result.stderr:
                 return jsonify({"error": f"No subtitles available for language: {lang}"}), 404
            return jsonify({"error": "Subtitle download failed, no file found."}), 500

        filename = downloaded_files[0]
        return jsonify({"download_url": f"/api/serve/{download_id}/{filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Subtitle download timed out."}), 504
    except subprocess.CalledProcessError as e:
        error_message = f"yt-dlp subtitle download error: {e.stderr}"
        return jsonify({"error": error_message}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/download-subtitle: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred during subtitle download: {str(e)}"}), 500

# Endpoint to serve downloaded files
@app.route('/api/serve/<download_id>/<filename>')
def serve_file(download_id, filename):
    directory = os.path.join(app.config['DOWNLOAD_FOLDER'], download_id)
    try:
        # Ensure the path is safe
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
    port = int(os.environ.get("PORT", 5000))  # Render يضبط PORT تلقائيًا
    app.run(host="0.0.0.0", port=port)
