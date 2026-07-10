#export BOT_TOKEN=""
import os
import re
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from yt_dlp import YoutubeDL

# -----------------------------
# Настройки
# -----------------------------
YOUTUBE_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+")
MAX_QUALITY_BUTTONS = 6

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = DOWNLOADS_DIR / "video"
AUDIO_DIR = DOWNLOADS_DIR / "audio"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Сессии в памяти: user_id -> {url,title,raw_info,type}
SESSIONS: Dict[int, Dict[str, Any]] = {}


# -----------------------------
# Вспомогательные функции
# -----------------------------
def is_youtube_url(text: str) -> bool:
    return bool(text and YOUTUBE_RE.search(text))


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:120] if name else "video")


def extract_info(url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def pick_video_qualities(info: Dict[str, Any]) -> List[Tuple[int, str]]:
    formats = info.get("formats") or []
    heights = set()

    for f in formats:
        if f.get("vcodec") and f.get("vcodec") != "none":
            h = f.get("height")
            if isinstance(h, int) and h > 0:
                heights.add(h)

    if not heights:
        return []

    preferred = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    ordered = [h for h in preferred if h in heights] + sorted(
        [h for h in heights if h not in preferred], reverse=True
    )
    ordered = ordered[:MAX_QUALITY_BUTTONS]
    return [(h, f"{h}p") for h in ordered]


def pick_audio_qualities(info: Dict[str, Any]) -> List[Tuple[int, str]]:
    formats = info.get("formats") or []
    abrs = set()

    for f in formats:
        # "чистое" аудио: есть acodec и нет видео
        if f.get("acodec") and f.get("acodec") != "none" and (f.get("vcodec") in (None, "none")):
            abr = f.get("abr")
            if isinstance(abr, (int, float)) and abr > 0:
                abrs.add(int(round(abr)))

    preferred = [320, 256, 192, 160, 128, 96, 64]
    ordered = [a for a in preferred if a in abrs] + sorted(
        [a for a in abrs if a not in preferred], reverse=True
    )

    if not ordered:
        ordered = [192, 128]

    ordered = ordered[:MAX_QUALITY_BUTTONS]
    return [(a, f"{a} kbps") for a in ordered]


def download_video(url: str, title: str, height: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = safe_name(title)
    outtmpl = str(VIDEO_DIR / f"{base}__{height}p__{stamp}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "format": (
            f"bestvideo[vcodec^=avc1][ext=mp4][height<={height}]+"
            f"bestaudio[acodec^=mp4a][ext=m4a]/"
            f"best[vcodec^=avc1][ext=mp4][height<={height}]/"
            f"best[ext=mp4][height<={height}]/best"
        ),
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4"
            }
        ],
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = info.get("requested_downloads")
        if requested and isinstance(requested, list):
            fp = requested[0].get("filepath")
            if fp:
                return Path(fp)

        # запасной вариант
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


def download_audio(url: str, title: str, abr: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = safe_name(title)
    outtmpl = str(AUDIO_DIR / f"{base}__audio__{abr}k__{stamp}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(abr)}
        ],
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        requested = info.get("requested_downloads")
        if requested and isinstance(requested, list):
            fp = requested[0].get("filepath")
            if fp:
                return Path(fp).with_suffix(".mp3")

        base_path = Path(ydl.prepare_filename(info)).with_suffix("")
        return base_path.with_suffix(".mp3")


def kb_root() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Видео", callback_data="pick:type:video")
    kb.button(text="🎵 Аудио", callback_data="pick:type:audio")
    kb.adjust(2)
    return kb


def kb_back() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="pick:back:root")
    kb.adjust(1)
    return kb


def kb_qualities(items: List[Tuple[int, str]]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for value, label in items:
        kb.button(text=label, callback_data=f"pick:q:{value}")
    kb.adjust(2)
    kb.row(*kb_back().buttons)
    return kb


# -----------------------------
# Хэндлеры
# -----------------------------
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Пришли ссылку на YouTube — я предложу выбрать (видео/аудио) и качество.\n"
        "Скачаю в папку downloads рядом с ботом и напишу, когда всё готово."
    )


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not is_youtube_url(text):
        return

    user_id = message.from_user.id
    await message.answer("Считываю информацию о видео…")

    try:
        info = await asyncio.to_thread(extract_info, text)
    except Exception as e:
        await message.answer(f"Не удалось обработать ссылку. Ошибка: {e}")
        return

    SESSIONS[user_id] = {
        "url": text,
        "title": info.get("title") or "video",
        "raw_info": info,
        "type": None,
    }

    await message.answer(
        f"Найдено: {SESSIONS[user_id]['title']}\nВыбери формат:",
        reply_markup=kb_root().as_markup(),
    )


@dp.callback_query(F.data.startswith("pick:"))
async def on_pick(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await call.answer("Сессия устарела. Пришли ссылку ещё раз.", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # pick:<kind>:<value>
    _, kind, value = (call.data or "").split(":", 2)

    if kind == "back" and value == "root":
        await call.answer()
        await call.message.edit_text("Выбери формат:", reply_markup=kb_root().as_markup())
        return

    if kind == "type":
        sess["type"] = value
        info = sess["raw_info"]
        await call.answer()

        if value == "video":
            qualities = pick_video_qualities(info)
            if not qualities:
                await call.message.edit_text("Не нашёл доступных вариантов качества видео.")
                return
            await call.message.edit_text("Выбери качество видео:", reply_markup=kb_qualities(qualities).as_markup())
            return

        if value == "audio":
            qualities = pick_audio_qualities(info)
            await call.message.edit_text("Выбери качество аудио:", reply_markup=kb_qualities(qualities).as_markup())
            return

        await call.message.edit_text("Неизвестный тип.")
        return

    if kind == "q":
        media_type = sess.get("type")
        if media_type not in ("video", "audio"):
            await call.answer("Сначала выбери формат (видео/аудио).", show_alert=True)
            return

        title = sess["title"]
        url = sess["url"]

        await call.answer()

        if media_type == "video":
            height = int(value)
            await call.message.edit_text(f"Ок, качаю видео {height}p в папку downloads…")
            try:
                path = await asyncio.to_thread(download_video, url, title, height)
                await call.message.answer(
                    "Готово!\n"
                    f"Скачано: {path.name}\n"
                    f"Путь: {path}"
                )
            except Exception as e:
                await call.message.answer(f"❌ Ошибка при скачивании: {e}")

        else:
            abr = int(value)
            await call.message.edit_text(f"Ок, качаю аудио {abr} kbps в папку downloads…")
            try:
                path = await asyncio.to_thread(download_audio, url, title, abr)
                await call.message.answer(
                    "Готово!\n"
                    f"Скачано: {path.name}\n"
                    f"Путь: {path}"
                )
            except Exception as e:
                await call.message.answer(f"❌ Ошибка при скачивании: {e}")

        SESSIONS.pop(user_id, None)
        return


# -----------------------------
# Запуск
# -----------------------------
async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажи токен: export BOT_TOKEN='123:ABC'")

    bot = Bot(token=token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
