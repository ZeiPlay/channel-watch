#!/usr/bin/env python3
"""Проверяет каналы, считает «дни тишины», пишет docs/data.js и шлёт алерты в Telegram.

Запуск:
    python check.py           # реальная проверка
    python check.py --demo    # демо-данные, без сети (для предпросмотра дашборда)

Переменные окружения для уведомлений: TG_BOT_TOKEN, TG_CHAT_ID.
Без них сообщения просто печатаются в консоль.
"""
import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.json"
STATE_FILE = ROOT / "state.json"
OUT_DIR = ROOT / "docs"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
UA = "Mozilla/5.0 (compatible; channel-watch/1.0)"
HISTORY_DAYS = 30


class Unsupported(Exception):
    """Платформа пока не подключена."""


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------- YouTube ----------

def resolve_channel_id(ch, state):
    if ch.get("channel_id"):
        return ch["channel_id"]
    handle = ch.get("handle", "").strip()
    source = ch.get("source_url", "").strip()
    ref = handle or source
    if not ref:
        raise ValueError("Укажите handle, channel_id или source_url")
    cache = state.setdefault("resolved", {})
    if ref in cache:
        return cache[ref]
    if handle:
        url = "https://www.youtube.com/" + urllib.parse.quote(handle if handle.startswith("@") else "@" + handle)
    else:
        host = (urllib.parse.urlparse(source).hostname or "").lower()
        if not (host == "youtube.com" or host.endswith(".youtube.com")):
            raise ValueError("source_url должен вести на youtube.com")
        url = source
    html = http_get(url)
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
         or re.search(r'"channelId":"(UC[\w-]{22})"', html)
         or re.search(r'channel_id=(UC[\w-]{22})', html))
    if not m:
        raise ValueError("Не удалось найти ID канала для " + ref)
    cache[ref] = m.group(1)
    return m.group(1)


def parse_youtube_feed(xml_text):
    root = ET.fromstring(xml_text)
    title = root.findtext("a:title", default="", namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        link = e.find("a:link", NS)
        videos.append({
            "id": e.findtext("yt:videoId", default="", namespaces=NS),
            "title": e.findtext("a:title", default="", namespaces=NS),
            "published": parse_iso(e.findtext("a:published", namespaces=NS)).isoformat(),
            "url": link.get("href") if link is not None else "",
        })
    videos.sort(key=lambda v: v["published"], reverse=True)
    return {"title": title, "videos": videos}


def fetch_youtube(ch, state):
    cid = resolve_channel_id(ch, state)
    xml_text = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
    feed = parse_youtube_feed(xml_text)
    feed["channel_url"] = "https://www.youtube.com/channel/" + cid
    return feed


# ---------- Telegram (публичные каналы, страница t.me/s/имя) ----------

TG_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def parse_telegram_page(page):
    """Достаёт название канала и последние посты из HTML страницы t.me/s/имя."""
    if "tgme_widget_message" not in page and "tgme_channel_info" not in page:
        raise ValueError("Это не публичный канал (личные профили, группы и закрытые каналы отслеживать нельзя)")
    m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = unescape(m.group(1)).strip() if m else ""
    posts = []
    for chunk in page.split("tgme_widget_message_wrap")[1:]:
        d = re.search(r'<a class="tgme_widget_message_date" href="([^"]+)"[^>]*>\s*<time[^>]*datetime="([^"]+)"', chunk)
        if not d:
            continue
        t = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        text = ""
        if t:
            text = re.sub(r"<br\s*/?>", " ", t.group(1))
            text = unescape(re.sub(r"<[^>]+>", "", text))
            text = re.sub(r"\s+", " ", text).strip()
        posts.append({
            "id": d.group(1).rsplit("/", 1)[-1],
            "title": (text[:80] + "…") if len(text) > 80 else (text or "Пост без текста"),
            "published": parse_iso(d.group(2)).isoformat(),
            "url": unescape(d.group(1)),
        })
    posts.sort(key=lambda v: v["published"], reverse=True)
    return {"title": title, "videos": posts}


def fetch_telegram(ch, state):
    name = (ch.get("handle") or "").strip().lstrip("@")
    if not TG_NAME.match(name):
        raise ValueError("Неверное имя Telegram-канала: " + (ch.get("handle") or ""))
    feed = parse_telegram_page(http_get("https://t.me/s/" + name))
    feed["channel_url"] = "https://t.me/" + name
    return feed


# ---------- TikTok (через Apify, нужен секрет APIFY_TOKEN) ----------
# У TikTok нет бесплатного API для чужих каналов, поэтому данные берёт сторонний сборщик.
# Чтобы не тратить кредиты, опрашиваем его не чаще раза в tiktok_interval_hours (по умолчанию 24),
# а между опросами используем сохранённый в state.json результат.

APIFY_ACTOR = "clockworks~tiktok-scraper"
TT_NAME = re.compile(r"^[A-Za-z0-9_.]{2,24}$")
TT_ERRORS = {}   # ошибки последнего опроса, только в памяти


def tt_name(ch):
    return (ch.get("handle") or "").strip().lstrip("@")


def apify_run(token, payload):
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?timeout=270"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=290) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        reasons = {
            401: "неверный APIFY_TOKEN",
            402: "закончились бесплатные кредиты Apify (обновятся в начале следующего месяца)",
            403: "Apify отказал в доступе (проверьте токен и тариф)",
            408: "Apify не успел ответить, попробуем в следующий раз",
            429: "Apify просит подождать (слишком много запросов)",
        }
        raise ValueError("Apify: " + reasons.get(ex.code, f"ошибка {ex.code}"))


def parse_tiktok_items(items, names):
    """Группирует видео из ответа Apify по каналам. Возвращает {имя_в_нижнем_регистре: feed}."""
    feeds = {n.lower(): {"title": "", "videos": []} for n in names}
    for it in items or []:
        meta = it.get("authorMeta") or {}
        name = str(meta.get("name") or "").lower()
        if name not in feeds:
            continue
        iso = it.get("createTimeISO")
        if iso:
            published = parse_iso(iso).isoformat()
        elif it.get("createTime"):
            published = datetime.fromtimestamp(int(it["createTime"]), timezone.utc).isoformat()
        else:
            continue
        text = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()
        f = feeds[name]
        f["title"] = f["title"] or meta.get("nickName") or meta.get("name") or ""
        f["videos"].append({
            "id": str(it.get("id") or ""),
            "title": (text[:80] + "…") if len(text) > 80 else (text or "Видео без подписи"),
            "published": published,
            "url": it.get("webVideoUrl") or "",
        })
    for f in feeds.values():
        f["videos"].sort(key=lambda v: v["published"], reverse=True)
    return feeds


def prefetch_tiktok(channels, state, settings, now):
    """Один запрос к Apify для всех TikTok-каналов, у которых сохранённые данные устарели."""
    names = []
    for ch in channels:
        if ch.get("platform") == "tiktok" and TT_NAME.match(tt_name(ch)):
            names.append(tt_name(ch))
    cache = state.setdefault("tiktok_cache", {})
    for k in [k for k in cache if k not in {n.lower() for n in names}]:
        del cache[k]                      # канал удалили из списка
    token = os.environ.get("APIFY_TOKEN")
    if not names or not token:
        return
    max_age = timedelta(hours=settings.get("tiktok_interval_hours", 24))
    due = []
    for n in names:
        c = cache.get(n.lower())
        if not c or now - parse_iso(c["fetched_at"]) >= max_age:
            due.append(n)
    if not due:
        return
    try:
        items = apify_run(token, {
            "profiles": due,
            "resultsPerPage": settings.get("tiktok_videos_per_check", 3),
            "profileScrapeSections": ["videos"],
            "profileSorting": "latest",
            "excludePinnedPosts": True,
            "maxProfilesPerQuery": max(10, len(due)),
            "shouldDownloadVideos": False, "shouldDownloadCovers": False, "shouldDownloadAvatars": False,
            "shouldDownloadMusicCovers": False, "shouldDownloadSlideshowImages": False, "shouldDownloadSubtitles": False,
        })
    except Exception as ex:
        for n in due:
            TT_ERRORS[n.lower()] = str(ex)
        print("TikTok: ", ex, file=sys.stderr)
        return
    feeds = parse_tiktok_items(items, due)
    for n in due:
        f = feeds[n.lower()]
        if f["videos"]:
            cache[n.lower()] = {"fetched_at": now.isoformat(), "feed": f}
        else:
            # запоминаем и «не найден», чтобы не платить за повторный опрос при каждом запуске
            msg = "Нет данных: профиль не найден, закрыт или без видео"
            cache[n.lower()] = {"fetched_at": now.isoformat(), "feed": None, "error": msg}


def fetch_tiktok(ch, state):
    name = tt_name(ch)
    if not TT_NAME.match(name):
        raise ValueError("Неверное имя TikTok-канала: " + (ch.get("handle") or ""))
    if not os.environ.get("APIFY_TOKEN"):
        raise Unsupported("TikTok: добавьте секрет APIFY_TOKEN (инструкция в README)")
    c = state.get("tiktok_cache", {}).get(name.lower())
    if not c:
        raise ValueError(TT_ERRORS.get(name.lower()) or "Данные ещё не получены")
    if c.get("feed") is None:
        raise ValueError(c.get("error") or "Нет данных")
    if name.lower() in TT_ERRORS:
        print(f"TikTok @{name}: используем сохранённые данные ({TT_ERRORS[name.lower()]})", file=sys.stderr)
    feed = dict(c["feed"])
    feed["channel_url"] = "https://www.tiktok.com/@" + name
    return feed


def fetch_instagram(ch, state):
    raise Unsupported("Instagram пока не подключён")


PROVIDERS = {"youtube": fetch_youtube, "telegram": fetch_telegram, "tiktok": fetch_tiktok, "instagram": fetch_instagram}


# ---------- Расчёт статуса ----------

def chan_key(ch):
    """Уникальный ключ канала. То же правило в docs/manage.js."""
    ref = ch.get("handle") or ch.get("channel_id") or ch.get("source_url") or ch.get("name") or ""
    return (ch["platform"] + ":" + re.sub(r"^@", "", ref.strip())).lower()


def build_entry(ch, feed, now, settings):
    silence = ch.get("silence_days", settings["silence_days"])
    dormant = settings["dormant_days"]
    videos = feed["videos"]
    entry = {
        "key": chan_key(ch),
        "platform": ch["platform"],
        "name": ch.get("name") or feed.get("title") or ch.get("handle", ""),
        "handle": ch.get("handle", ""),
        "url": feed.get("channel_url", ""),
        "silence_days": silence,
        "last_post": None, "last_title": "", "last_url": "",
        "days_since": None, "posts_7d": 0, "posts_30d": 0,
        "history": [0] * HISTORY_DAYS,
        "status": "unknown", "message": "",
    }
    if not videos:
        entry["message"] = "Нет публикаций в ленте"
        return entry

    last = videos[0]
    last_dt = parse_iso(last["published"])
    days = (now - last_dt).total_seconds() / 86400
    entry.update(last_post=last["published"], last_title=last["title"], last_url=last["url"],
                 days_since=round(days, 2))
    for v in videos:
        age = int((now - parse_iso(v["published"])).total_seconds() // 86400)
        if 0 <= age < HISTORY_DAYS:
            entry["history"][HISTORY_DAYS - 1 - age] += 1
            entry["posts_30d"] += 1
            if age < 7:
                entry["posts_7d"] += 1
    if days >= dormant:
        entry["status"] = "dormant"
    elif days >= silence:
        entry["status"] = "silent"
    else:
        entry["status"] = "ok"
    return entry


def error_entry(ch, status, message):
    return {
        "key": chan_key(ch), "platform": ch["platform"], "name": ch.get("name") or ch.get("handle", ""),
        "handle": ch.get("handle", ""), "url": "", "silence_days": ch.get("silence_days"),
        "last_post": None, "last_title": "", "last_url": "", "days_since": None,
        "posts_7d": 0, "posts_30d": 0, "history": [0] * HISTORY_DAYS,
        "status": status, "message": message,
    }


# ---------- Уведомления ----------

def ru_days(n):
    n = int(n)
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    return f"{n} " + {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def send_telegram(text):
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("[telegram не настроен] " + text.replace("\n", " | "))
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return True


def process_alerts(entries, state):
    """Одно сводное сообщение за запуск. О канале сообщаем один раз при выходе за порог
    и ещё раз при «глубокой» тишине; после нового видео счётчик сбрасывается.
    Если Telegram не настроен или отправка не удалась, повторим в следующий запуск."""
    alerted = state.setdefault("alerted", {})
    pending = []
    for e in entries:
        key = e["key"]
        if e["status"] == "ok":
            alerted.pop(key, None)
            continue
        if e["status"] not in ("silent", "dormant"):
            continue
        level = 2 if e["status"] == "dormant" else 1
        prev = alerted.get(key, {})
        if prev.get("last_post") == e["last_post"] and prev.get("level", 0) >= level:
            continue
        icon = "🔴" if level == 2 else "🟡"
        what = "постов" if e["platform"] in ("telegram", "instagram") else "видео"
        line = f'{icon} {e["name"]} ({e["platform"]}): {ru_days(e["days_since"])} без {what}\n{e["last_url"]}'.rstrip()
        pending.append((key, level, line, e["last_post"]))
    if not pending:
        return

    chunks, cur = [], "Каналы без новых публикаций:\n"
    for _, _, line, _ in pending:
        if len(cur) + len(line) + 2 > 3500:
            chunks.append(cur)
            cur = ""
        cur += "\n" + line + "\n"
    chunks.append(cur)

    try:
        sent = [send_telegram(c.strip()) for c in chunks]
    except Exception as ex:
        print("Не удалось отправить в Telegram:", ex, file=sys.stderr)
        return
    if not all(sent):
        return
    for key, level, _, last_post in pending:
        alerted[key] = {"last_post": last_post, "level": level}


# ---------- Демо ----------

def demo_entries(now, settings):
    rng = random.Random(7)
    spec = [
        ("youtube", "Кулинарный канал", "@cooking", 0.4, 5),
        ("youtube", "Обзоры техники", "@tech", 1.2, 3),
        ("youtube", "Влоги про путешествия", "@travel", 3.5, 2),
        ("youtube", "Игровой стрим", "@games", 9.0, 1),
        ("telegram", "Новости канала", "@newsline", 0.3, 5),
        ("tiktok", "Танцы и тренды", "@dance", 0.8, 6),
        ("tiktok", "Лайфхаки", "@hacks", 2.6, 4),
        ("instagram", "Фото-блог", "photoblog", 1.0, 3),
        ("instagram", "Магазин одежды", "shop", 5.1, 2),
    ]
    out = []
    for platform, name, handle, last_days, per_week in spec:
        hist = [0] * HISTORY_DAYS
        gap = int(last_days)
        for d in range(gap, HISTORY_DAYS):
            if rng.random() < per_week / 7:
                hist[HISTORY_DAYS - 1 - d] += 1
        last_dt = now - timedelta(days=last_days)
        silence = settings["silence_days"]
        status = "dormant" if last_days >= settings["dormant_days"] else "silent" if last_days >= silence else "ok"
        out.append({
            "key": chan_key({"platform": platform, "handle": handle}),
            "platform": platform, "name": name, "handle": handle, "url": "#",
            "silence_days": silence, "last_post": last_dt.isoformat(),
            "last_title": "Пример последнего видео", "last_url": "#",
            "days_since": round(last_days, 2), "posts_7d": sum(hist[-7:]), "posts_30d": sum(hist),
            "history": hist, "status": status, "message": "",
        })
    out.append(error_entry({"platform": "tiktok", "name": "Ещё один TikTok", "handle": "@soon"},
                           "unsupported", "TikTok пока не подключён"))
    return out


# ---------- main ----------

def write_output(entries, settings, now):
    OUT_DIR.mkdir(exist_ok=True)
    payload = {"generated_at": now.isoformat(), "settings": settings, "channels": entries}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    (OUT_DIR / "data.json").write_text(text, encoding="utf-8")
    # data.js позволяет открывать index.html двойным кликом, без сервера
    (OUT_DIR / "data.js").write_text("window.CHANNEL_DATA = " + text + ";\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    settings = {"silence_days": 2, "dormant_days": 7, **cfg.get("settings", {})}
    now = datetime.now(timezone.utc)

    if args.demo:
        write_output(demo_entries(now, settings), settings, now)
        print("Демо-данные записаны в docs/data.js")
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    entries = []
    prefetch_tiktok(cfg["channels"], state, settings, now)
    for ch in cfg["channels"]:
        provider = PROVIDERS.get(ch["platform"])
        try:
            if provider is None:
                raise Unsupported("Неизвестная платформа: " + ch["platform"])
            feed = provider(ch, state)
            entries.append(build_entry(ch, feed, now, settings))
        except Unsupported as ex:
            entries.append(error_entry(ch, "unsupported", str(ex)))
        except Exception as ex:
            print(f'Ошибка для {ch.get("name") or ch.get("handle")}: {ex}', file=sys.stderr)
            entries.append(error_entry(ch, "error", f"Ошибка загрузки: {ex}"))

    process_alerts(entries, state)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_output(entries, settings, now)
    print(f"Готово: {len(entries)} каналов")


if __name__ == "__main__":
    main()
