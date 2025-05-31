from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import subprocess
import json
import re
import uuid
import urllib.parse
from googletrans import Translator  # مكتبة الترجمة المجانية

app = Flask(__name__)
CORS(app)

# مجلد التنزيلات (ضمن /tmp لأن معظم البيئات المستضافة تسمح بالكتابة فيه فقط)
DOWNLOAD_FOLDER = "/tmp/video_downloads"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)
app.config["DOWNLOAD_FOLDER"] = DOWNLOAD_FOLDER

# مُترجم Google Translate 
translator = Translator()


# --- دوال مساعدة --- #

def get_yt_dlp_path():
    return "yt-dlp"

def is_valid_url(url):
    pattern = r"^(https?://)?(www\.)?(youtu\.be|youtube\.com|twitter\.com|x\.com)/.+"
    return re.match(pattern, url)

def sanitize_filename(name):
    """
    تنظيف اسم الملف من الأحرف غير الصالحة واستبدال الفراغات بشرطة سفلية
    """
    name = re.sub(r'[\\/*?"<>|]', "", name)
    name = re.sub(r'\s+', '_', name)
    return name[:100]


def parse_vtt_and_translate(vtt_content: str, target_lang: str) -> str:
    """
    يستقبل محتوى ملف VTT كنص، ثم يترجم أسطر الحوار إلى اللغة target_lang،
    ويعيد نص VTT جديد مع الترجمة.
    """
    lines = vtt_content.splitlines()
    output_lines = []
    for line in lines:
        if "-->" in line or line.strip() == "" or line.strip().isdigit():
            # سطر توقيت أو رقم كتلة أو سطر فارغ: نحتفظ به كما هو
            output_lines.append(line)
        else:
            # سطر نص: نترجمه
            try:
                translated = translator.translate(line, dest=target_lang).text
                output_lines.append(translated)
            except Exception:
                # إذا فشل الترجمة لأي سبب، نحتفظ بالنص الأصلي
                output_lines.append(line)
    return "\n".join(output_lines)


# --- نقاط النهاية (Endpoints) --- #

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
        # نستدعي yt-dlp لإخراج JSON دون تحميل الفيديو
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
        video_data = json.loads(result.stdout)

        title = video_data.get("title", "N/A")
        thumbnail = video_data.get("thumbnail", None)
        uploader = video_data.get("uploader", "N/A")
        original_url = video_data.get("original_url", url)

        # 1) اكتشاف المنصة بناءً على extractor_key أو عنوان URL
        lower_url = url.lower()
        extractor = video_data.get("extractor_key", "").lower()
        if ("twitter.com" in lower_url) or ("x.com" in lower_url) or any(k in extractor for k in ("twitter", "x", "xcom")):
            platform = "twitter"
        else:
            platform = "youtube"

        # 2) استخراج الصيغ (formats)
        formats = []
        for f in video_data.get("formats", []):
            fmt_id = f.get("format_id", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            url_f = f.get("url")

            if vcodec != "none" and url_f and (f.get("format_note") or f.get("height")):
                # بناء نص الدقة
                if f.get("format_note"):
                    resolution = f.get("format_note")
                else:
                    h = f.get("height")
                    resolution = f"{h}p" if h else "N/A"

                # الحجم (filesize) من yt-dlp إن وجد، أو تقريبًا
                filesize = f.get("filesize") or f.get("filesize_approx") or 0

                formats.append({
                    "format_id": fmt_id,
                    "resolution": resolution,
                    "height": f.get("height", 0),
                    "ext": f.get("ext", ""),
                    "filesize": filesize,
                    "has_audio": (acodec != "none"),
                    "download_url": url_f,
                    "http_headers": f.get("http_headers", {})
                })

        # نرتب الصيغ بحسب الدقة تنازليًا
        formats.sort(key=lambda x: x.get("height", 0), reverse=True)

        # 3) استخراج الترجمات المتوفرة والآلية (حتى لو فارغ)
        subtitles = {}
        for lang, subs in video_data.get("subtitles", {}).items():
            vtt_sub = next((s for s in subs if s.get("ext") == "vtt"), subs[0] if subs else None)
            if vtt_sub:
                subtitles[lang] = {
                    "name": vtt_sub.get("name", lang),
                    "url": vtt_sub.get("url"),
                    "ext": vtt_sub.get("ext")
                }

        auto_captions = {}
        for lang, caps in video_data.get("automatic_captions", {}).items():
            vtt_cap = next((c for c in caps if c.get("ext") == "vtt"), caps[0] if caps else None)
            if vtt_cap:
                auto_captions[lang] = {
                    "name": vtt_cap.get("name", lang + " (auto)"),
                    "url": vtt_cap.get("url"),
                    "ext": vtt_cap.get("ext")
                }

        # ←ــــــــــــ هنا قمنا بنقل بناء response_data إلى خارج حلقة الـ for الخاصة بالترجمات الآلية
        response_data = {
            "title": title,
            "thumbnail": thumbnail,
            "uploader": uploader,
            "formats": formats,
            "subtitles": subtitles,
            "automatic_captions": auto_captions,
            "original_url": original_url,
            "platform": platform
        }

        return jsonify(response_data)

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out. The video might be too long or the server is busy."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        error_message = f"yt-dlp error: {stderr}"
        if "Unsupported URL" in stderr:
            error_message = "Unsupported URL or video not found."
        elif "Video unavailable" in stderr:
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
    url = data.get("url")
    format_id = data.get("format_id")
    title = data.get("title", "video")

    if not url or not format_id:
        return jsonify({"error": "URL and format ID are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config["DOWNLOAD_FOLDER"], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    output_template = os.path.join(specific_download_path, f"{safe_title}.%(ext)s")

    # دمج الصيغة المطلوبة مع أفضل صوت دائمًا
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
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)

        downloaded_files = os.listdir(specific_download_path)
        if not downloaded_files:
            return jsonify({"error": "Download failed, no file found."}), 500

        # نبحث أولًا عن ملف به امتداد
        valid_files = [
            f for f in downloaded_files
            if os.path.isfile(os.path.join(specific_download_path, f)) and "." in f
        ]
        filename = valid_files[0] if valid_files else downloaded_files[0]
        encoded_filename = urllib.parse.quote(filename, safe="")

        return jsonify({"download_url": f"/api/serve/{download_id}/{encoded_filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out. The video might be too large or the connection slow."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        error_message = f"yt-dlp download error: {stderr}"
        if "HTTP Error 403" in stderr:
            error_message = "Access denied (403). The video might be private or require login."
        return jsonify({"error": error_message}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/download-video: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred during download: {str(e)}"}), 500


@app.route("/api/download-subtitle", methods=["POST"])
def download_subtitle():
    """
    يُنَزِّل ملف الترجمة الأصلي (يدوي أو آلي) للغة source_lang.
    استقبال:
      {
        "url": "<video_url>",
        "lang": "<language_code>",
        "is_auto": <true_or_false>,
        "title": "<video_title>"
      }
    """
    data = request.get_json()
    url = data.get("url")
    lang = data.get("lang")
    is_auto = data.get("is_auto", False)
    title = data.get("title", "subtitle")

    if not url or not lang:
        return jsonify({"error": "URL and language code are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config["DOWNLOAD_FOLDER"], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    output_template = os.path.join(specific_download_path, f"{safe_title}.{lang}.%(ext)s")

    command = [
        yt_dlp_path,
        "--skip-download",
        "--sub-langs", lang,
        "--sub-format", "vtt",
        "-o", output_template,
        "--no-warnings",
        "--no-playlist",
        url
    ]
    if is_auto:
        command.insert(1, "--write-auto-subs")
    else:
        command.insert(1, "--write-subs")

    try:
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)

        downloaded_files = os.listdir(specific_download_path)
        vtt_files = [
            f for f in downloaded_files
            if os.path.isfile(os.path.join(specific_download_path, f)) and f.endswith(".vtt")
        ]
        if not vtt_files:
            return jsonify({"error": "No VTT file found after download."}), 500

        filename = vtt_files[0]
        encoded_filename = urllib.parse.quote(filename, safe="")

        return jsonify({"download_url": f"/api/serve/{download_id}/{encoded_filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Subtitle download timed out."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        return jsonify({"error": f"yt-dlp subtitle error: {stderr}"}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/download-subtitle: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route("/api/translate-subtitle", methods=["POST"])
def translate_subtitle():
    """
    ينشئ ترجمة جديدة (مثلاً إلى العربية) لملف VTT الأصلي/الآلي.
    استقبال:
      {
        "url": "<video_url>",
        "source_lang": "<original_subtitle_language_code>",
        "target_lang": "<desired_translation_language_code>",
        "is_auto": <true_or_false>,
        "title": "<video_title>"
      }
    """
    data = request.get_json()
    url = data.get("url")
    source_lang = data.get("source_lang")
    target_lang = data.get("target_lang")
    is_auto = data.get("is_auto", False)
    title = data.get("title", "translated_subtitle")

    if not url or not source_lang or not target_lang:
        return jsonify({"error": "URL, source_lang, and target_lang are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config["DOWNLOAD_FOLDER"], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    orig_output_template = os.path.join(
        specific_download_path, f"{safe_title}.{source_lang}.%(ext)s"
    )

    command = [
        yt_dlp_path,
        "--skip-download",
        "--sub-langs", source_lang,
        "--sub-format", "vtt",
        "-o", orig_output_template,
        "--no-warnings",
        "--no-playlist",
        url
    ]
    if is_auto:
        command.insert(1, "--write-auto-subs")
    else:
        command.insert(1, "--write-subs")

    try:
        # نحمّل ملف الترجمة الأصلي أولًا
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)

        downloaded_files = os.listdir(specific_download_path)
        vtt_files = [
            f for f in downloaded_files
            if os.path.isfile(os.path.join(specific_download_path, f)) and f.endswith(".vtt")
        ]
        if not vtt_files:
            return jsonify({"error": "No VTT file found after download."}), 500

        orig_vtt_filename = vtt_files[0]
        orig_vtt_path = os.path.join(specific_download_path, orig_vtt_filename)

        # نقرأ محتوى الـ VTT
        with open(orig_vtt_path, "r", encoding="utf-8") as f:
            vtt_content = f.read()

        # نترجم المحتوى إلى اللغة target_lang
        translated_vtt_content = parse_vtt_and_translate(vtt_content, target_lang)

        # نكتب الملف المترجم باسم جديد
        translated_filename = f"{safe_title}.{source_lang}_to_{target_lang}.vtt"
        translated_filepath = os.path.join(specific_download_path, translated_filename)
        with open(translated_filepath, "w", encoding="utf-8") as f:
            f.write(translated_vtt_content)

        encoded_filename = urllib.parse.quote(translated_filename, safe="")
        return jsonify({"download_url": f"/api/serve/{download_id}/{encoded_filename}"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Subtitle translation timed out."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        return jsonify({"error": f"yt-dlp translation error: {stderr}"}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/translate-subtitle: {e}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route("/api/generate-translation", methods=["POST"])
def generate_translation():
    """
    مسار جديد لتوليد ترجمة عربية (أو لغة أخرى) لفيديو تويتر أو يوتيوب.
    الخطوات:
      1. نحمل الصوت فقط (best audio) من الفيديو.
      2. نستعمل whisper CLI لاستخلاص ملف VTT (باللغة الأصلية).
      3. نقرأ ملف VTT الأصلي، نترجمه إلى العربية (target_lang) عبر parse_vtt_and_translate.
      4. نرسل رابط تحميل الملف المترجم.
    استقبال:
      {
        "url": "<video_url>",
        "target_lang": "<desired_translation_language_code>",
        "title": "<video_title>"
      }
    """
    data = request.get_json()
    url = data.get("url")
    target_lang = data.get("target_lang", "ar")
    title = data.get("title", "whisper_subtitle")

    if not url or not target_lang:
        return jsonify({"error": "URL and target_lang are required"}), 400

    yt_dlp_path = get_yt_dlp_path()
    download_id = str(uuid.uuid4())
    specific_download_path = os.path.join(app.config["DOWNLOAD_FOLDER"], download_id)
    os.makedirs(specific_download_path, exist_ok=True)

    safe_title = sanitize_filename(title)
    audio_output = os.path.join(specific_download_path, f"{safe_title}.m4a")

    # 1. نحمل مسار الصوت فقط (bestaudio)
    download_audio_cmd = [
        yt_dlp_path,
        "-f", "bestaudio",
        "-o", audio_output,
        "--no-warnings",
        "--no-playlist",
        url
    ]

    try:
        subprocess.run(download_audio_cmd, capture_output=True, text=True, check=True, timeout=300)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Audio download timed out."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        return jsonify({"error": f"yt-dlp audio error: {stderr}"}), 500
    except Exception as e:
        app.logger.error(f"Error downloading audio: {e}", exc_info=True)
        return jsonify({"error": f"Unexpected error during audio download: {e}"}), 500

    # 2. نستدعي whisper CLI لإنتاج VTT تلقائي بناءً على الصوت
    #    نفترض أن الأمر "whisper" مثبت في PATH.
    whisper_cmd = [
        "whisper",
        audio_output,
        "--model", "small",
        "--output_format", "vtt",
        "--output_dir", specific_download_path
    ]
    try:
        subprocess.run(whisper_cmd, capture_output=True, text=True, check=True, timeout=600)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Whisper transcription timed out."}), 504
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        return jsonify({"error": f"Whisper error: {stderr}"}), 500
    except Exception as e:
        app.logger.error(f"Error running whisper: {e}", exc_info=True)
        return jsonify({"error": f"Unexpected error during transcription: {e}"}), 500

    # بعد انتهاء whisper، يجب أن يكون ملف VTT باسم "<safe_title>.vtt" داخل المجلد
    vtt_filename = f"{safe_title}.vtt"
    vtt_filepath = os.path.join(specific_download_path, vtt_filename)
    if not os.path.isfile(vtt_filepath):
        return jsonify({"error": "Whisper VTT file not found."}), 500

    # 3. نقرأ محتوى الـ VTT الأصلي ثم نترجمه
    try:
        with open(vtt_filepath, "r", encoding="utf-8") as f:
            vtt_content = f.read()
        translated_vtt_content = parse_vtt_and_translate(vtt_content, target_lang)
    except Exception as e:
        app.logger.error(f"Error reading/translating VTT: {e}", exc_info=True)
        return jsonify({"error": f"Failed to translate VTT: {e}"}), 500

    # 4. نكتب الملف المترجم باسم جديد
    translated_filename = f"{safe_title}_to_{target_lang}.vtt"
    translated_filepath = os.path.join(specific_download_path, translated_filename)
    try:
        with open(translated_filepath, "w", encoding="utf-8") as f:
            f.write(translated_vtt_content)
    except Exception as e:
        app.logger.error(f"Error writing translated VTT: {e}", exc_info=True)
        return jsonify({"error": f"Failed to save translated VTT: {e}"}), 500

    encoded_filename = urllib.parse.quote(translated_filename, safe="")
    return jsonify({"download_url": f"/api/serve/{download_id}/{encoded_filename}"})


@app.route("/api/serve/<download_id>/<path:filename>")
def serve_file(download_id, filename):
    directory = os.path.join(app.config["DOWNLOAD_FOLDER"], download_id)
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
