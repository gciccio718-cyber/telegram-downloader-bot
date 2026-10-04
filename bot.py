import os
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "")

MAX_FILE_SIZE = 49 * 1024 * 1024  # circa 49 MB


def is_allowed(user_id: int) -> bool:
    return bool(ALLOWED_USER_ID) and str(user_id) == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Mandami un link e proverò a scaricare il contenuto e rimandartelo qui."
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
        raise ValueError("Il file è troppo grande per essere inviato da questo bot.")

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
                raise ValueError(
                    "Il file è troppo grande per essere inviato da questo bot."
                )

            file.write(chunk)

    return path


def download_media(url: str, folder: str) -> str:
    output = os.path.join(folder, "%(title).80s.%(ext)s")

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


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Prima bisogna configurare il nostro Telegram ID su Render.
    if not is_allowed(user_id):
        await update.message.reply_text(
            f"🔐 Il bot non è ancora configurato per il tuo account.\n\n"
            f"Il tuo Telegram ID è:\n{user_id}\n\n"
            f"Conservalo: ci servirà per configurare il bot."
        )
        return

    text = update.message.text.strip()

    if not text.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❌ Mandami un link che inizi con http:// o https://"
        )
        return

    status = await update.message.reply_text("⏳ Sto scaricando...")

    with tempfile.TemporaryDirectory() as folder:
        try:
            # Prima proviamo con yt-dlp per siti supportati.
            try:
                file_path = await asyncio.to_thread(
                    download_media, text, folder
                )
            except Exception:
                # Se non è un sito supportato, proviamo come file/link diretto.
                file_path = await asyncio.to_thread(
                    download_direct, text, folder
                )

            if not os.path.exists(file_path):
                raise FileNotFoundError("File non trovato.")

            file_size = os.path.getsize(file_path)

            if file_size > MAX_FILE_SIZE:
                raise ValueError("Il file supera il limite consentito.")

            await status.edit_text("📤 Download completato. Te lo invio...")

            with open(file_path, "rb") as document:
                await update.message.reply_document(
                    document=document,
                    caption="✅ Ecco il tuo file."
                )

            await status.delete()

        except Exception as error:
            await status.edit_text(
                "❌ Non sono riuscito a scaricare questo link.\n\n"
                "Potrebbe essere un link non supportato, protetto "
                "oppure un file troppo grande."
            )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN non configurato.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
