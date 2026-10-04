import os
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "")

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "10000"))

MAX_FILE_SIZE = 49 * 1024 * 1024


def is_allowed(user_id: int) -> bool:
    return bool(ALLOWED_USER_ID) and str(user_id) == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Mandami un link e proverò a scaricare il contenuto "
        "e rimandartelo qui."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Il tuo Telegram ID è:\n{update.effective_user.id}"
    )


def download_direct(url: str, folder: str) -> str:
    response = requests.get(
        url,
        stream=True,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    content_length = response.headers.get("content-length")

    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise ValueError("Il file è troppo grande.")

    filename = Path(urlparse(url).path).name or "download"
    filename = filename[:100]

    path = os.path.join(folder, filename)

    total = 0

    with open(path, "wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue

            total += len(chunk)

            if total > MAX_FILE_SIZE:
                file.close()
                os.remove(path)
                raise ValueError("Il file è troppo grande.")

            file.write(chunk)

    return path


def download_media(url: str, folder: str) -> str:
    output = os.path.join(
        folder,
        "%(title).80s.%(ext)s"
    )

    options = {
        "outtmpl": output,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return filename


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text(
            "🔐 Questo bot è privato e non è ancora configurato "
            "per il tuo account."
        )
        return

    text = update.message.text.strip()

    if not text.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❌ Mandami un link che inizi con http:// o https://"
        )
        return

    status = await update.message.reply_text(
        "⏳ Sto scaricando..."
    )

    with tempfile.TemporaryDirectory() as folder:
        try:
            try:
                file_path = await asyncio.to_thread(
                    download_media,
                    text,
                    folder
                )
            except Exception:
                file_path = await asyncio.to_thread(
                    download_direct,
                    text,
                    folder
                )

            if not os.path.exists(file_path):
                raise FileNotFoundError("File non trovato.")

            file_size = os.path.getsize(file_path)

            if file_size > MAX_FILE_SIZE:
                raise ValueError("Il file è troppo grande.")

            await status.edit_text(
                "📤 Download completato. Te lo invio..."
            )

            with open(file_path, "rb") as document:
                await update.message.reply_document(
                    document=document,
                    caption="✅ Ecco il tuo file."
                )

            await status.delete()

        except Exception:
            await status.edit_text(
                "❌ Non sono riuscito a scaricare questo link.\n\n"
                "Il sito potrebbe non essere supportato, "
                "il contenuto potrebbe essere protetto "
                "oppure il file potrebbe essere troppo grande."
            )


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
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("myid", myid)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
