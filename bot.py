import os
import asyncio
import tempfile
import threading
import json
import re
import html
import subprocess

from pathlib import Path
from urllib.parse import urlparse, urljoin
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import yt_dlp
import imageio_ffmpeg

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURAZIONE
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "")

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "10000"))

# Telegram Bot API: teniamo un piccolo margine sotto 50 MB.
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
# UTILITÀ
# ============================================================

def is_allowed(user_id: int) -> bool:
    return (
        bool(ALLOWED_USER_ID)
        and str(user_id) == str(ALLOWED_USER_ID).strip()
    )


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip()
    return name[:100] or "video"


def is_direct_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()

    return path.endswith((
        ".mp4",
        ".m4v",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
        ".m3u8",
    ))


def is_m3u8_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower() or ".m3u8" in url.lower()


# ============================================================
# DOWNLOAD DIRETTO
# ============================================================

def download_direct(url: str, folder: str) -> str:
    response = requests.get(
        url,
        stream=True,
        timeout=(20, 60),
        headers=HEADERS,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (
        response.headers.get("content-type", "")
        .lower()
    )

    content_length = response.headers.get("content-length")

    if content_length:
        try:
            if int(content_length) > MAX_FILE_SIZE * 20:
                raise ValueError(
                    "Il file è troppo grande per essere gestito."
                )
        except ValueError:
            pass

    path_name = Path(
        urlparse(response.url).path
    ).name

    if not path_name:
        path_name = "video.mp4"

    path_name = safe_filename(path_name)

    if not Path(path_name).suffix:
        if "webm" in content_type:
            path_name += ".webm"
        elif "quicktime" in content_type:
            path_name += ".mov"
        else:
            path_name += ".mp4"

    output = os.path.join(folder, path_name)

    total = 0

    with open(output, "wb") as file:
        for chunk in response.iter_content(
            chunk_size=1024 * 256
        ):
            if not chunk:
                continue

            total += len(chunk)

            # Evitiamo di scaricare file enormi senza controllo.
            if total > MAX_FILE_SIZE * 20:
                file.close()

                try:
                    os.remove(output)
                except OSError:
                    pass

                raise ValueError(
                    "Il file è troppo grande."
                )

            file.write(chunk)

    if total == 0:
        raise ValueError(
            "Il server ha restituito un file vuoto."
        )

    return output


# ============================================================
# YT-DLP
# ============================================================

def download_with_ytdlp(url: str, folder: str) -> str:
    output = os.path.join(
        folder,
        "%(title).100s.%(ext)s"
    )

    options = {
        "outtmpl": output,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # Prova prima video+audio.
        "format": (
            "bestvideo+bestaudio/"
            "best"
        ),

        # Non lasciare playlist.
        "playlistend": 1,

        # Header realistici.
        "http_headers": HEADERS,

        # Evita file singoli giganteschi quando yt-dlp
        # conosce già la dimensione.
        "max_filesize": MAX_FILE_SIZE * 2,

        # Se il sito richiede un formato compatibile,
        # preferiamo MP4 quando disponibile.
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True
        )

        filename = ydl.prepare_filename(info)

    if os.path.exists(filename):
        return filename

    base = os.path.splitext(filename)[0]

    for extension in (
        ".mp4",
        ".webm",
        ".mkv",
        ".mov",
        ".m4v",
        ".avi",
    ):
        possible = base + extension

        if os.path.exists(possible):
            return possible

    # Ultima ricerca nella cartella.
    files = list(Path(folder).glob("*"))

    video_files = [
        p for p in files
        if p.suffix.lower() in (
            ".mp4",
            ".webm",
            ".mkv",
            ".mov",
            ".m4v",
            ".avi",
        )
        and p.is_file()
    ]

    if video_files:
        video_files.sort(
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        return str(video_files[0])

    raise FileNotFoundError(
        "yt-dlp non ha prodotto un file video."
    )


# ============================================================
# ESTRAZIONE VIDEO DALLA PAGINA HTML
# ============================================================

def clean_extracted_url(value: str, page_url: str) -> str:
    value = html.unescape(value)

    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")
    value = value.replace("\\u003d", "=")
    value = value.replace("\\u002F", "/")
    value = value.strip()

    if value.startswith("//"):
        value = "https:" + value

    if value.startswith("/"):
        value = urljoin(page_url, value)

    if not value.startswith(("http://", "https://")):
        return ""

    return value


def extract_video_urls_from_html(
    page_url: str,
    page_html: str
):
    found = []

    def add(url: str):
        url = clean_extracted_url(
            url,
            page_url
        )

        if not url:
            return

        if url not in found:
            found.append(url)

    # --------------------------------------------------------
    # 1. EroThots / player specifico
    # --------------------------------------------------------

    patterns = [
        r'class=["\'][^"\']*v-player[^"\']*["\'][^>]*>'
        r'.{0,500}?'
        r'<(?:video|source)[^>]+src=["\']([^"\']+)',

        r'<(?:video|source)[^>]+src=["\']([^"\']+)',

        r'<video[^>]+data-src=["\']([^"\']+)',

        r'<source[^>]+data-src=["\']([^"\']+)',

        r'<video[^>]+data-video=["\']([^"\']+)',

        r'<source[^>]+data-video=["\']([^"\']+)',
    ]

    for pattern in patterns:
        for match in re.findall(
            pattern,
            page_html,
            flags=re.IGNORECASE | re.DOTALL
        ):
            add(match)

    # --------------------------------------------------------
    # 2. OpenGraph
    # --------------------------------------------------------

    og_patterns = [
        r'<meta[^>]+property=["\']og:video["\'][^>]+'
        r'content=["\']([^"\']+)',

        r'<meta[^>]+property=["\']og:video:url["\'][^>]+'
        r'content=["\']([^"\']+)',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
        r'property=["\']og:video["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
        r'property=["\']og:video:url["\']',
    ]

    for pattern in og_patterns:
        for match in re.findall(
            pattern,
            page_html,
            flags=re.IGNORECASE
        ):
            add(match)

    # --------------------------------------------------------
    # 3. URL .mp4 / .m3u8 / altri formati video
    # --------------------------------------------------------

    url_patterns = [
        r'https?://[^"\'>\s\\]+?\.mp4(?:\?[^"\'>\s\\]*)?',
        r'https?://[^"\'>\s\\]+?\.m3u8(?:\?[^"\'>\s\\]*)?',
        r'https?://[^"\'>\s\\]+?\.webm(?:\?[^"\'>\s\\]*)?',
        r'https?://[^"\'>\s\\]+?\.m4v(?:\?[^"\'>\s\\]*)?',
        r'https?://[^"\'>\s\\]+?\.mov(?:\?[^"\'>\s\\]*)?',
    ]

    for pattern in url_patterns:
        for match in re.findall(
            pattern,
            page_html,
            flags=re.IGNORECASE
        ):
            add(match)

    # --------------------------------------------------------
    # 4. URL relative / escaped
    # --------------------------------------------------------

    relative_patterns = [
        r'["\']([^"\']+\.mp4(?:\?[^"\']*)?)["\']',
        r'["\']([^"\']+\.m3u8(?:\?[^"\']*)?)["\']',
        r'["\']([^"\']+\.webm(?:\?[^"\']*)?)["\']',
    ]

    for pattern in relative_patterns:
        for match in re.findall(
            pattern,
            page_html,
            flags=re.IGNORECASE
        ):
            add(match)

    return found


def extract_video_from_page(
    url: str,
    folder: str
) -> str:
    response = requests.get(
        url,
        headers={
            **HEADERS,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        },
        timeout=(20, 30),
        allow_redirects=True,
    )

    response.raise_for_status()

    page_html = response.text

    video_urls = extract_video_urls_from_html(
        response.url,
        page_html
    )

    if not video_urls:
        raise RuntimeError(
            "Nessuna sorgente video trovata nella pagina."
        )

    print(
        f"Trovate {len(video_urls)} possibili sorgenti video."
    )

    last_error = None

    for video_url in video_urls:
        try:
            print(
                f"Provo sorgente: {video_url[:180]}"
            )

            if is_m3u8_url(video_url):
                output = os.path.join(
                    folder,
                    "video_from_m3u8.mp4"
                )

                convert_with_ffmpeg(
                    video_url,
                    output,
                    compress=False
                )

                if os.path.exists(output):
                    return output

            else:
                return download_direct(
                    video_url,
                    folder
                )

        except Exception as error:
            last_error = error
            print(
                f"Sorgente non utilizzabile: {error}"
            )

    raise RuntimeError(
        f"Nessuna sorgente video utilizzabile. "
        f"Ultimo errore: {last_error}"
    )


# ============================================================
# FFmpeg
# ============================================================

def run_ffmpeg(
    input_path: str,
    output_path: str,
    extra_args=None
):
    command = [
        FFMPEG_PATH,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
    ]

    if extra_args:
        command.extend(extra_args)

    command.extend([
        "-movflags",
        "+faststart",
        output_path,
    ])

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
    )

    if result.returncode != 0:
        print("FFmpeg error:")
        print(result.stderr)

        raise RuntimeError(
            result.stderr[-2000:]
            or "Errore FFmpeg."
        )


def convert_with_ffmpeg(
    input_path: str,
    output_path: str,
    compress: bool = False
):
    if compress:
        args = [
            "-vf",
            "scale='min(1280,iw)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
        ]
    else:
        args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]

    run_ffmpeg(
        input_path,
        output_path,
        args
    )


def probe_video(input_path: str):
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-i",
        input_path,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )

    output = (
        result.stdout
        + "\n"
        + result.stderr
    )

    video_codec = ""
    audio_codec = ""

    video_match = re.search(
        r"Video:\s*([a-zA-Z0-9_]+)",
        output,
        flags=re.IGNORECASE
    )

    audio_match = re.search(
        r"Audio:\s*([a-zA-Z0-9_]+)",
        output,
        flags=re.IGNORECASE
    )

    if video_match:
        video_codec = video_match.group(1).lower()

    if audio_match:
        audio_codec = audio_match.group(1).lower()

    return video_codec, audio_codec


def is_telegram_compatible(input_path: str) -> bool:
    suffix = Path(input_path).suffix.lower()

    if suffix != ".mp4":
        return False

    try:
        video_codec, audio_codec = probe_video(
            input_path
        )

        print(
            "Codec video:",
            video_codec,
            "| Codec audio:",
            audio_codec
        )

        # H.264 + AAC è la combinazione più sicura
        # per la riproduzione video su Telegram/iPhone.
        return (
            video_codec in (
                "h264",
                "avc1",
            )
            and audio_codec in (
                "aac",
                "mp4a",
            )
        )

    except Exception as error:
        print(
            f"Probe video fallito: {error}"
        )

        return False


# ============================================================
# PREPARAZIONE VIDEO
# ============================================================

def prepare_video(
    input_path: str,
    folder: str
) -> str:

    file_size = os.path.getsize(
        input_path
    )

    print(
        f"File originale: "
        f"{file_size / 1024 / 1024:.2f} MB"
    )

    # --------------------------------------------------------
    # CASO IDEALE:
    # MP4 + H264/AAC + sotto il limite.
    #
    # Lo inviamo direttamente senza conversione.
    # --------------------------------------------------------

    if (
        file_size <= MAX_FILE_SIZE
        and is_telegram_compatible(input_path)
    ):
        print(
            "Video già compatibile: "
            "nessuna conversione necessaria."
        )

        return input_path

    # --------------------------------------------------------
    # Se è sotto il limite ma non è compatibile,
    # convertiamo per evitare il famoso video bianco.
    # --------------------------------------------------------

    if file_size <= MAX_FILE_SIZE:
        output = os.path.join(
            folder,
            "telegram_compatible.mp4"
        )

        print(
            "Video sotto il limite ma non "
            "compatibile: conversione."
        )

        convert_with_ffmpeg(
            input_path,
            output,
            compress=False
        )

        if os.path.getsize(output) > MAX_FILE_SIZE:
            raise ValueError(
                "La conversione ha prodotto "
                "un file troppo grande."
            )

        return output

    # --------------------------------------------------------
    # FILE TROPPO GRANDE:
    # solo ora facciamo compressione.
    # --------------------------------------------------------

    print(
        "Video sopra il limite: "
        "inizio compressione."
    )

    compressed = os.path.join(
        folder,
        "telegram_compressed.mp4"
    )

    convert_with_ffmpeg(
        input_path,
        compressed,
        compress=True
    )

    compressed_size = os.path.getsize(
        compressed
    )

    print(
        f"Prima compressione: "
        f"{compressed_size / 1024 / 1024:.2f} MB"
    )

    if compressed_size <= MAX_FILE_SIZE:
        return compressed

    # --------------------------------------------------------
    # Secondo tentativo più aggressivo.
    # --------------------------------------------------------

    compressed2 = os.path.join(
        folder,
        "telegram_compressed_2.mp4"
    )

    run_ffmpeg(
        input_path,
        compressed2,
        [
            "-vf",
            "scale='min(854,iw)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
        ]
    )

    compressed2_size = os.path.getsize(
        compressed2
    )

    print(
        f"Seconda compressione: "
        f"{compressed2_size / 1024 / 1024:.2f} MB"
    )

    if compressed2_size <= MAX_FILE_SIZE:
        return compressed2

    raise ValueError(
        "Il video rimane troppo grande anche "
        "dopo la compressione."
    )


# ============================================================
# DOWNLOAD INTELLIGENTE
# ============================================================

def download_video(
    url: str,
    folder: str
) -> str:

    errors = []

    # --------------------------------------------------------
    # METODO 1:
    # URL direttamente video.
    # --------------------------------------------------------

    if is_direct_video_url(url):
        try:
            print(
                "Metodo 1: download diretto."
            )

            return download_direct(
                url,
                folder
            )

        except Exception as error:
            errors.append(
                f"diretto: {error}"
            )

    # --------------------------------------------------------
    # METODO 2:
    # yt-dlp.
    # --------------------------------------------------------

    try:
        print(
            "Metodo 2: yt-dlp."
        )

        return download_with_ytdlp(
            url,
            folder
        )

    except Exception as error:
        print(
            f"yt-dlp fallito: {error}"
        )

        errors.append(
            f"yt-dlp: {error}"
        )

    # --------------------------------------------------------
    # METODO 3:
    # estrazione HTML / player.
    # --------------------------------------------------------

    try:
        print(
            "Metodo 3: estrazione HTML/player."
        )

        return extract_video_from_page(
            url,
            folder
        )

    except Exception as error:
        print(
            f"HTML/player fallito: {error}"
        )

        errors.append(
            f"html: {error}"
        )

    raise RuntimeError(
        "Nessun metodo è riuscito.\n"
        + "\n".join(errors[-3:])
    )


# ============================================================
# TELEGRAM
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "👋 Mandami un link video e proverò "
        "automaticamente diversi metodi per "
        "scaricarlo e inviartelo come video."
    )


async def myid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        f"Il tuo Telegram ID è:\n"
        f"{update.effective_user.id}"
    )


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text(
            "🔐 Questo bot non è configurato "
            "per questo account."
        )
        return

    text = update.message.text.strip()

    if not text.startswith((
        "http://",
        "https://"
    )):
        await update.message.reply_text(
            "❌ Mandami un link che inizi "
            "con http:// o https://"
        )
        return

    status = await update.message.reply_text(
        "🔎 Analizzo il link e cerco il video..."
    )

    with tempfile.TemporaryDirectory() as folder:

        try:

            # ------------------------------------------------
            # DOWNLOAD
            # ------------------------------------------------

            file_path = await asyncio.to_thread(
                download_video,
                text,
                folder
            )

            if not os.path.exists(file_path):
                raise FileNotFoundError(
                    "File video non trovato."
                )

            await status.edit_text(
                "🎬 Video trovato. "
                "Controllo formato e dimensione..."
            )

            # ------------------------------------------------
            # PREPARAZIONE
            # ------------------------------------------------

            final_path = await asyncio.to_thread(
                prepare_video,
                file_path,
                folder
            )

            if not os.path.exists(final_path):
                raise FileNotFoundError(
                    "Video finale non trovato."
                )

            final_size = os.path.getsize(
                final_path
            )

            if final_size > MAX_FILE_SIZE:
                raise ValueError(
                    "Il video finale supera "
                    "il limite di Telegram."
                )

            await status.edit_text(
                "📤 Video pronto. Te lo invio..."
            )

            # ------------------------------------------------
            # INVIO COME VIDEO
            # ------------------------------------------------

            with open(
                final_path,
                "rb"
            ) as video:

                await update.message.reply_video(
                    video=video,
                    caption="✅ Ecco il tuo video.",
                    supports_streaming=True,
                )

            try:
                await status.delete()
            except Exception:
                pass

        except Exception as error:

            print(
                "ERRORE COMPLETO:"
            )
            print(error)

            try:
                await status.edit_text(
                    "❌ Non sono riuscito a "
                    "scaricare/preparare questo video.\n\n"
                    "Ho provato automaticamente "
                    "più metodi di estrazione."
                )
            except Exception:
                pass


# ============================================================
# WEBHOOK SERVER
# ============================================================

async def process_update_safe(
    application,
    update
):
    try:
        await application.process_update(
            update
        )

    except Exception as error:
        print(
            "Errore durante l'elaborazione "
            "dell'update:"
        )
        print(error)


class TelegramWebhookHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path in (
            "/",
            "/health"
        ):
            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain"
            )

            self.end_headers()

            self.wfile.write(
                b"OK"
            )

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        if self.path != "/telegram":
            self.send_response(404)
            self.end_headers()
            return

        try:

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            body = self.rfile.read(
                content_length
            )

            data = json.loads(
                body.decode("utf-8")
            )

            update = Update.de_json(
                data,
                self.server.application.bot
            )

            # IMPORTANTISSIMO:
            #
            # NON aspettiamo che il download finisca.
            #
            # Rispondiamo subito a Telegram con HTTP 200.
            # In questo modo Telegram non reinvia lo stesso
            # messaggio mentre il bot sta scaricando il video.

            asyncio.run_coroutine_threadsafe(
                process_update_safe(
                    self.server.application,
                    update
                ),
                self.server.loop
            )

            self.send_response(200)
            self.end_headers()

            self.wfile.write(
                b"OK"
            )

        except Exception as error:

            print(
                f"Webhook error: {error}"
            )

            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(
        self,
        format,
        *args
    ):
        return


class TelegramHTTPServer(
    HTTPServer
):

    def __init__(
        self,
        server_address,
        application,
        loop
    ):
        super().__init__(
            server_address,
            TelegramWebhookHandler
        )

        self.application = application
        self.loop = loop


# ============================================================
# AVVIO
# ============================================================

async def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN non configurato."
        )

    if not RENDER_EXTERNAL_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL non configurato."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "myid",
            myid
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_link
        )
    )

    await application.initialize()
    await application.start()

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True
    )

    loop = asyncio.get_running_loop()

    server = TelegramHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        application,
        loop
    )

    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    server_thread.start()

    print(
        f"Bot avviato."
    )

    print(
        f"Webhook: {webhook_url}"
    )

    try:

        await asyncio.Event().wait()

    finally:

        server.shutdown()
        server.server_close()

        await application.bot.delete_webhook()

        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
