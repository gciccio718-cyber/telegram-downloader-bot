import os
import re
import json
import html
import asyncio
import tempfile
import threading
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import yt_dlp
import imageio_ffmpeg

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import RetryAfter


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

MAX_FILE_SIZE = 49 * 1024 * 1024

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}


# ============================================================
# BASIC HELPERS
# ============================================================

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_ID:
        return False

    try:
        return int(ALLOWED_USER_ID) == int(user_id)
    except Exception:
        return False


def safe_filename(name: str) -> str:
    name = html.unescape(name or "video")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "video"

    return name[:150]


def looks_like_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text.strip(), re.I))


def is_video_extension(url: str) -> bool:
    path = urlparse(url).path.lower()

    extensions = (
        ".mp4",
        ".m4v",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
        ".wmv",
        ".flv",
        ".ts",
        ".m3u8",
    )

    return path.endswith(extensions)


def is_m3u8(url: str) -> bool:
    return ".m3u8" in url.lower()


# ============================================================
# DIRECT DOWNLOAD
# ============================================================

def download_direct(url: str, workdir: str) -> str:
    print(f"[DIRECT] {url}")

    response = requests.get(
        url,
        headers=HEADERS,
        stream=True,
        timeout=(20, 120),
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()

    if "text/html" in content_type:
        response.close()
        raise ValueError("URL restituisce HTML, non un file video diretto.")

    filename = ""

    content_disposition = response.headers.get("content-disposition", "")
    match = re.search(
        r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
        content_disposition,
        re.I,
    )

    if match:
        filename = match.group(1)

    if not filename:
        filename = Path(urlparse(response.url).path).name

    filename = safe_filename(filename)

    if not Path(filename).suffix:
        filename += ".mp4"

    output = Path(workdir) / filename

    total = 0

    try:
        with open(output, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue

                total += len(chunk)

                # Limite di sicurezza molto più alto del limite Telegram.
                # Serve per evitare di riempire il disco Render.
                if total > 1024 * 1024 * 1024:
                    raise ValueError("File oltre 1 GB: download interrotto.")

                f.write(chunk)

    finally:
        response.close()

    if not output.exists() or output.stat().st_size == 0:
        raise ValueError("Download vuoto.")

    print(f"[DIRECT] scaricato: {output} ({output.stat().st_size} bytes)")

    return str(output)


# ============================================================
# YT-DLP
# ============================================================

def download_with_ytdlp(url: str, workdir: str) -> str:
    print(f"[YT-DLP] {url}")

    output_template = str(
        Path(workdir) / "%(title).120s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "playlistend": 1,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "http_headers": HEADERS,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "max_downloads": 1,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise ValueError("yt-dlp non ha trovato il video.")

    files = [
        p for p in Path(workdir).iterdir()
        if p.is_file()
    ]

    if not files:
        raise ValueError("yt-dlp non ha prodotto alcun file.")

    # Preferiamo video conosciuti.
    video_files = [
        p for p in files
        if p.suffix.lower() in (
            ".mp4",
            ".m4v",
            ".mov",
            ".webm",
            ".mkv",
            ".avi",
            ".ts",
        )
    ]

    if video_files:
        result = max(video_files, key=lambda p: p.stat().st_mtime)
    else:
        result = max(files, key=lambda p: p.stat().st_mtime)

    print(f"[YT-DLP] trovato: {result}")

    return str(result)


# ============================================================
# HTML / PLAYER EXTRACTION
# ============================================================

def clean_url(value: str, base_url: str) -> str:
    if not value:
        return ""

    value = html.unescape(value)

    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.replace("\\u002f", "/")
    value = value.replace("&amp;", "&")

    value = value.strip().strip("'\"")

    if value.startswith("//"):
        parsed = urlparse(base_url)
        value = f"{parsed.scheme}:{value}"

    if value.startswith("/"):
        value = urljoin(base_url, value)

    if value.startswith("http://") or value.startswith("https://"):
        return value

    return ""


def extract_video_urls_from_html(page_url: str, text: str):
    candidates = []

    # --------------------------------------------------------
    # <video src="">
    # --------------------------------------------------------

    patterns = [
        r'<video[^>]+src=["\']([^"\']+)["\']',
        r'<source[^>]+src=["\']([^"\']+)["\']',
        r'<source[^>]+data-src=["\']([^"\']+)["\']',

        # data attributes
        r'data-video=["\']([^"\']+)["\']',
        r'data-src=["\']([^"\']+)["\']',
        r'data-url=["\']([^"\']+)["\']',
        r'data-file=["\']([^"\']+)["\']',

        # OpenGraph
        r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::url)?["\']',

        # JSON / JS
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'"video_url"\s*:\s*"([^"]+)"',
        r'"source"\s*:\s*"([^"]+)"',
        r'"src"\s*:\s*"([^"]+\.(?:mp4|m4v|webm|mov|m3u8)(?:\?[^"]*)?)"',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text, re.I):
            url = clean_url(match, page_url)

            if url:
                candidates.append(url)

    # --------------------------------------------------------
    # Cerca URL video generici nel codice HTML
    # --------------------------------------------------------

    generic_pattern = (
        r'https?://[^"\'\s<>\\]+'
        r'\.(?:mp4|m4v|webm|mov|m3u8)'
        r'(?:\?[^"\'\s<>\\]*)?'
    )

    for match in re.findall(generic_pattern, text, re.I):
        url = clean_url(match, page_url)

        if url:
            candidates.append(url)

    # --------------------------------------------------------
    # EroThots / player HTML
    # Il player usa spesso .v-player con il video come figlio.
    # --------------------------------------------------------

    player_match = re.search(
        r'<[^>]+class=["\'][^"\']*v-player[^"\']*["\'][^>]*>'
        r'(.*?)'
        r'</[^>]+>',
        text,
        re.I | re.S,
    )

    if player_match:
        block = player_match.group(1)

        for pattern in patterns:
            for match in re.findall(pattern, block, re.I):
                url = clean_url(match, page_url)

                if url:
                    candidates.append(url)

    # --------------------------------------------------------
    # Deduplica mantenendo l'ordine
    # --------------------------------------------------------

    result = []

    for url in candidates:
        if url not in result:
            result.append(url)

    return result


def extract_video_from_page(url: str, workdir: str) -> str:
    print(f"[HTML] analizzo pagina: {url}")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=(20, 60),
        allow_redirects=True,
    )

    response.raise_for_status()

    content = response.text

    urls = extract_video_urls_from_html(
        response.url,
        content,
    )

    if not urls:
        raise ValueError("Nessun video trovato nell'HTML.")

    print(f"[HTML] trovati {len(urls)} possibili sorgenti.")

    last_error = None

    for video_url in urls:
        try:
            print(f"[HTML] provo: {video_url}")

            if is_m3u8(video_url):
                output = str(Path(workdir) / "hls_video.mp4")

                convert_with_ffmpeg(
                    video_url,
                    output,
                    compress=False,
                )

                if Path(output).exists():
                    return output

            else:
                return download_direct(
                    video_url,
                    workdir,
                )

        except Exception as exc:
            print(f"[HTML] sorgente fallita: {exc}")
            last_error = exc

    raise ValueError(
        f"Tutte le sorgenti video trovate hanno fallito: {last_error}"
    )


# ============================================================
# FFMPEG
# ============================================================

def run_ffmpeg(input_file: str, output_file: str, extra_args):
    command = [
        FFMPEG_PATH,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_file,
    ]

    command.extend(extra_args)

    command.extend([
        "-movflags",
        "+faststart",
        output_file,
    ])

    print("[FFMPEG]", " ".join(command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-4000:] or "FFmpeg ha restituito un errore."
        )


def convert_with_ffmpeg(
    input_file: str,
    output_file: str,
    compress: bool = False,
    small: bool = False,
):
    if compress:
        if small:
            scale = "854:-2"
            crf = "32"
            audio = "64k"
        else:
            scale = "1280:-2"
            crf = "28"
            audio = "96k"

        args = [
            "-vf",
            f"scale={scale}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio,
            "-ar",
            "44100",
        ]

    else:
        args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
        ]

    run_ffmpeg(
        input_file,
        output_file,
        args,
    )


def probe_video(path: str):
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-i",
        path,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )

    text = result.stderr

    video_codec = None
    audio_codec = None

    video_match = re.search(
        r"Video:\s*([^,\s]+)",
        text,
        re.I,
    )

    audio_match = re.search(
        r"Audio:\s*([^,\s]+)",
        text,
        re.I,
    )

    if video_match:
        video_codec = video_match.group(1).lower()

    if audio_match:
        audio_codec = audio_match.group(1).lower()

    return video_codec, audio_codec


def is_telegram_compatible(path: str) -> bool:
    if Path(path).suffix.lower() != ".mp4":
        return False

    try:
        video_codec, audio_codec = probe_video(path)

        print(
            f"[PROBE] video={video_codec}, audio={audio_codec}"
        )

        video_ok = video_codec in (
            "h264",
            "avc1",
        )

        audio_ok = audio_codec in (
            "aac",
            "mp4a",
        )

        return video_ok and audio_ok

    except Exception as exc:
        print(f"[PROBE] errore: {exc}")
        return False


# ============================================================
# PREPARAZIONE VIDEO
# ============================================================

def prepare_video(path: str, workdir: str) -> str:
    file_path = Path(path)
    size = file_path.stat().st_size

    print(
        f"[PREPARE] file={file_path.name}, "
        f"size={size} bytes"
    )

    # --------------------------------------------------------
    # Se è già perfetto e sotto il limite:
    # NON convertire.
    # --------------------------------------------------------

    if size <= MAX_FILE_SIZE:
        if is_telegram_compatible(path):
            print("[PREPARE] già compatibile: invio diretto.")
            return path

        print("[PREPARE] non compatibile: converto in MP4 H264/AAC.")

        converted = str(
            Path(workdir) / "telegram_compatible.mp4"
        )

        convert_with_ffmpeg(
            path,
            converted,
            compress=False,
        )

        if Path(converted).stat().st_size <= MAX_FILE_SIZE:
            return converted

        path = converted
        size = Path(path).stat().st_size

    # --------------------------------------------------------
    # Sopra il limite: prima compressione.
    # --------------------------------------------------------

    print("[PREPARE] file oltre il limite: comprimo.")

    compressed = str(
        Path(workdir) / "compressed.mp4"
    )

    convert_with_ffmpeg(
        path,
        compressed,
        compress=True,
        small=False,
    )

    compressed_size = Path(compressed).stat().st_size

    print(
        f"[PREPARE] prima compressione: "
        f"{compressed_size} bytes"
    )

    if compressed_size <= MAX_FILE_SIZE:
        return compressed

    # --------------------------------------------------------
    # Seconda compressione più aggressiva.
    # --------------------------------------------------------

    print("[PREPARE] ancora troppo grande: seconda compressione.")

    compressed_small = str(
        Path(workdir) / "compressed_small.mp4"
    )

    convert_with_ffmpeg(
        compressed,
        compressed_small,
        compress=True,
        small=True,
    )

    final_size = Path(compressed_small).stat().st_size

    print(
        f"[PREPARE] seconda compressione: "
        f"{final_size} bytes"
    )

    if final_size <= MAX_FILE_SIZE:
        return compressed_small

    raise ValueError(
        "Il video rimane oltre il limite di Telegram "
        "anche dopo la compressione."
    )


# ============================================================
# DOWNLOAD CASCADE
# ============================================================

def download_video(url: str, workdir: str) -> str:
    errors = []

    # --------------------------------------------------------
    # 1. URL diretto
    # --------------------------------------------------------

    if is_video_extension(url):
        try:
            return download_direct(url, workdir)
        except Exception as exc:
            errors.append(f"direct: {exc}")

    # --------------------------------------------------------
    # 2. yt-dlp
    # --------------------------------------------------------

    try:
        return download_with_ytdlp(url, workdir)
    except Exception as exc:
        print(f"[YT-DLP] fallito: {exc}")
        errors.append(f"yt-dlp: {exc}")

    # --------------------------------------------------------
    # 3. HTML / player
    # --------------------------------------------------------

    try:
        return extract_video_from_page(url, workdir)
    except Exception as exc:
        print(f"[HTML] fallito: {exc}")
        errors.append(f"html: {exc}")

    raise ValueError(
        "Non sono riuscito a scaricare il video.\n\n"
        + "\n".join(errors[-5:])
    )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "Mandami il link del video."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    await update.message.reply_text(
        str(update.effective_user.id)
    )


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_allowed(update.effective_user.id):
        return

    text = (update.message.text or "").strip()

    if not looks_like_url(text):
        await update.message.reply_text(
            "Mandami un link http:// o https://"
        )
        return

    status = await update.message.reply_text(
        "⏳ Scarico il video..."
    )

    try:
        # Tutto il lavoro pesante fuori dal loop Telegram.
        result = await asyncio.to_thread(
            process_video,
            text,
        )

        await status.edit_text(
            "📤 Invio il video su Telegram..."
        )

        final_path, workdir = result

        try:
            with open(final_path, "rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60,
                )

            await status.delete()

        finally:
            # La directory temporanea viene eliminata dopo l'invio.
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    except Exception as exc:
        print(f"[ERROR] {exc}")

        try:
            await status.edit_text(
                "❌ Non sono riuscito a scaricare/inviare il video.\n\n"
                f"{str(exc)[:2500]}"
            )
        except Exception:
            pass


def process_video(url: str):
    workdir = tempfile.mkdtemp(
        prefix="telegram_video_"
    )

    try:
        downloaded = download_video(
            url,
            workdir,
        )

        final_path = prepare_video(
            downloaded,
            workdir,
        )

        if not Path(final_path).exists():
            raise ValueError(
                "Il file finale non esiste."
            )

        final_size = Path(final_path).stat().st_size

        if final_size > MAX_FILE_SIZE:
            raise ValueError(
                "Il file finale supera il limite Telegram."
            )

        print(
            f"[FINAL] {final_path} "
            f"({final_size} bytes)"
        )

        return final_path, workdir

    except Exception:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        raise


# ============================================================
# WEBHOOK SERVER
# ============================================================

APPLICATION = None
EVENT_LOOP = None


async def process_update_safe(update_data):
    global APPLICATION

    try:
        update = Update.de_json(
            update_data,
            APPLICATION.bot,
        )

        await APPLICATION.process_update(update)

    except Exception as exc:
        print(f"[UPDATE ERROR] {exc}")


class TelegramWebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Evita di riempire i log Render.
        return

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.end_headers()
        self.wfile.write(
            b"Telegram downloader bot is running."
        )

    def do_POST(self):
        global EVENT_LOOP

        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            body = self.rfile.read(length)

            update_data = json.loads(
                body.decode("utf-8")
            )

            # IMPORTANTISSIMO:
            # NON aspettiamo che il download finisca.
            #
            # Telegram riceve subito HTTP 200.
            # Questo evita retry, duplicati e Flood Control.

            asyncio.run_coroutine_threadsafe(
                process_update_safe(update_data),
                EVENT_LOOP,
            )

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain",
            )
            self.end_headers()
            self.wfile.write(b"OK")

        except Exception as exc:
            print(f"[WEBHOOK ERROR] {exc}")

            # Anche in caso di problema interno rispondiamo
            # rapidamente, evitando una valanga di retry.
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except Exception:
                pass


def start_http_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        TelegramWebhookHandler,
    )

    print(
        f"[HTTP] server listening on 0.0.0.0:{PORT}"
    )

    server.serve_forever()


# ============================================================
# TELEGRAM WEBHOOK SETUP
# ============================================================

async def set_webhook_safely():
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL non configurato."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    print(
        f"[TELEGRAM] imposto webhook: {webhook_url}"
    )

    while True:
        try:
            await APPLICATION.bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=False,
            )

            print(
                "[TELEGRAM] webhook impostato correttamente."
            )

            return

        except RetryAfter as exc:
            seconds = max(
                1,
                int(getattr(exc, "retry_after", 1)),
            )

            print(
                f"[TELEGRAM] Flood control. "
                f"Attendo {seconds} secondi..."
            )

            await asyncio.sleep(
                seconds + 1
            )

        except Exception as exc:
            print(
                f"[TELEGRAM] errore webhook: {exc}"
            )

            # Non facciamo crashare Render.
            # Ritentiamo automaticamente.
            await asyncio.sleep(10)


# ============================================================
# MAIN
# ============================================================

async def main():
    global APPLICATION
    global EVENT_LOOP

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN non configurato."
        )

    if not ALLOWED_USER_ID:
        raise RuntimeError(
            "ALLOWED_USER_ID non configurato."
        )

    EVENT_LOOP = asyncio.get_running_loop()

    APPLICATION = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    APPLICATION.add_handler(
        CommandHandler("start", start)
    )

    APPLICATION.add_handler(
        CommandHandler("myid", myid)
    )

    APPLICATION.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link,
        )
    )

    # --------------------------------------------------------
    # Avvio Application.
    # NON usiamo polling.
    # --------------------------------------------------------

    await APPLICATION.initialize()
    await APPLICATION.start()

    # --------------------------------------------------------
    # Server HTTP Render.
    # --------------------------------------------------------

    server_thread = threading.Thread(
        target=start_http_server,
        daemon=True,
    )

    server_thread.start()

    # --------------------------------------------------------
    # Webhook con gestione automatica Flood Control.
    # --------------------------------------------------------

    await set_webhook_safely()

    print("[BOT] avviato correttamente.")

    try:
        # Il processo rimane vivo.
        await asyncio.Event().wait()

    finally:
        print("[BOT] shutdown...")

        try:
            await APPLICATION.stop()
        finally:
            await APPLICATION.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
