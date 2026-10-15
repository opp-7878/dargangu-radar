#!/usr/bin/env python3
"""DargaNGU News Radar — сборщик новостей GTA 6 + уведомления в Telegram.

Запуск:
  python3 radar.py            # один проход (GitHub Actions, каждые 10 мин круглосуточно)
  python3 radar.py --dry      # без отправки в Telegram, сообщения печатаются
  python3 radar.py --digest   # принудительно отправить дайджест за 24 ч
  python3 radar.py --post       # (RADAR_POST=full:<id>:<msg>) один пост по нажатию кнопки — вызывает webhook
  python3 radar.py --test-card  # прислать в Telegram карточку с кнопками (проверка кнопок)
  python3 radar.py --test-post  # три формата поста в лог (RADAR_TEST_Q=текст — выбрать историю), без Telegram
  python3 radar.py --setup    # один раз: спросить токен бота (скрыто), найти chat id, записать секреты в GitHub
  python3 radar.py --setup-giga  # один раз: ключ GigaChat (скрыто) → проверка → секрет в GitHub → тестовый пост

Без внешних библиотек: только стандартный Python 3.9+.
Секреты — только из переменных окружения TG_BOT_TOKEN, TG_CHAT_ID, GIGACHAT_AUTH_KEY; в файлы и логи не пишутся.
Этап 2: перед отправкой карточки GigaChat пишет русский заголовок, суть и идею ролика (правила — voice.md).
Кнопки под карточкой: «📝 Пост», «⚡ Срочно», «📋 По пунктам» — следующий запуск готовит пост и присылает ответом.
Срочные новости (официальное, дата, ПК, трейлер, цена) — короткий пост приходит сразу, без кнопки.
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(ROOT, "sources.json")
NEWS = os.path.join(ROOT, "data", "news.json")
UA = "Mozilla/5.0 (compatible; DargaNGU-radar/1.0; +https://github.com)"
MSK = dt.timezone(dt.timedelta(hours=3))

KEEP_DAYS = 7          # сколько дней хранить истории
MAX_AGE_H = 48         # записи старше — не берём
CLUSTER_SIM = 0.34     # похожесть заголовков, чтобы считать одной историей
ALERT_SCORE = 70       # порог жёлтой карточки
QUIET = None          # тихие часы МСК, например (0, 8): жёлтые карточки ждут утра. None = присылать и ночью
DIGEST_HOUR = 21       # дайджест в 21:00 МСК
URGENT_TAGS = {"дата/перенос", "ПК", "трейлер", "цена/издания", "размер/preload"}  # с ними важная новость = срочная
URGENT_SCORE = 85      # порог срочной новости (официальное — срочно всегда)

GTA_RE = re.compile(r"\bgta\s?(6|vi)\b|grand theft auto\s?(6|vi)\b|\bгта\s?(6|vi)\b|\brockstar\b|lucia|jason duval|vice city|leonida", re.I)
HOT = {
    "трейлер": r"trailer|трейлер",
    "дата/перенос": r"release date|delay|postpon|перенос|дата выхода",
    "ПК": r"\bpc\b|\bпк\b|steam",
    "цена/издания": r"price|\$\d|preorder|pre-order|edition|цена|предзаказ|издани",
    "геймплей": r"gameplay|геймпле",
    "слив": r"leak|слив|утечк|cyberleek",
    "Online": r"\bonline\b",
    "Take-Two": r"take-two|take two|earnings|zelnick",
    "размер/preload": r"preload|file size|\bgb\b|предзагруз|гигабайт",
    "анонс": r"confirm|announc|reveal|подтверд|анонс|объяв",
}
STOP = set("the a an of to in on for and or is are was be with at by from as it its this that will can new gta grand theft auto vi 6 rockstar games game report reportedly says could may just more about after over how what why when you your has have not but than into out up all one".split())
STOP |= set("в на и с по о что как для это из к не но за от до у же ли бы уже все ещё еще новый новая новые гта игра игры rockstar".split())


# ---------- утилиты ----------
def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        t = email.utils.parsedate_to_datetime(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def uid(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


SYN = {"пк": "pc", "компьютер": "pc", "компьютеров": "pc", "компьютерную": "pc", "трейлер": "trailer", "слив": "leak",
       "лусия": "lucia", "люсия": "lucia", "люсию": "lucia", "лусию": "lucia", "перенос": "delay", "цена": "price"}


SHORT = {"pc", "q1", "q2", "q3", "q4", "gb", "tv"}


def tokens(title):
    words = re.findall(r"[a-zа-яё0-9]+", title.lower())
    words = [SYN.get(w, w) for w in words]
    return {w for w in words if (len(w) > 2 or w.isdigit() or w in SHORT) and w not in STOP}


def similar(a_title, b_title):
    """Похожесть заголовков: Jaccard или «2+ общих слова и общая горячая тема» (ловит EN↔RU пересказы)."""
    a, b = tokens(a_title), tokens(b_title)
    sim = jaccard(a, b)
    if len(a & b) >= 2 and set(hot_tags(a_title)) & set(hot_tags(b_title)):
        sim = max(sim, 0.4)
    return sim


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


# ---------- парсеры ----------
def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(text, src):
    """RSS 2.0 и Atom. Возвращает [{title, url, ts, summary, outlet}]."""
    out = []
    root = ET.fromstring(text.encode("utf-8"))
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {local(c.tag): c for c in el}
        title = clean(f["title"].text if "title" in f else "")
        link = ""
        if "link" in f:
            link = (f["link"].text or "").strip() or f["link"].get("href", "")
        summary = ""
        for k in ("description", "summary", "content", "encoded"):  # не `or`: элемент без детей ложен
            if k in f and f[k].text:
                summary = clean(f[k].text)
                break
        when = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in f:
                when = parse_date(f[k].text)
                if when:
                    break
        outlet = ""
        if src["kind"] == "gnews":
            outlet = clean(f["source"].text) if "source" in f else ""
            if outlet and title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3].strip()
            summary = ""  # в Google News описание = тот же заголовок
        if title and link:
            out.append({"title": title, "url": link, "ts": when, "summary": summary[:600], "outlet": outlet})
    return out


TG_POST = re.compile(r'data-post="([^"]+)"(.*?)<time[^>]*datetime="([^"]+)"', re.S)
TG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)


def parse_telegram(text, src):
    out = []
    for post, body, when in TG_POST.findall(text):
        m = TG_TEXT.search(body)
        if not m:
            continue
        full = clean(m.group(1).replace("<br/>", "\n").replace("<br>", "\n"))
        first = re.split(r"(?<=[.!?…])\s|•|\n", full, 1)[0].strip(" ⚡️❗️🔥•")
        title = (first or full)[:160]
        out.append({"title": title, "url": "https://t.me/" + post, "ts": parse_date(when), "summary": full[:1500], "outlet": ""})
    return out


# ---------- ядро ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def hot_tags(text):
    return [k for k, rx in HOT.items() if re.search(rx, text, re.I)]


def level_of(story):
    groups = {i["group"] for i in story["items"]}
    strong = {i["outlet"] or i["src"] for i in story["items"] if i["group"] in ("media", "insider") and i["weight"] >= 60}
    media = {i["outlet"] or i["src"] for i in story["items"] if i["group"] in ("media", "media_ru", "insider", "fansite")}
    if "official" in groups:
        return "CONFIRMED"
    leak = sum(1 for i in story["items"] if re.search(HOT["слив"], i["title"], re.I))
    if leak * 2 >= len(story["items"]):  # история именно про слив, а не упоминание
        return "SPECULATION"
    if len(strong) >= 3:
        return "HIGH CONFIDENCE"
    if media:
        return "PROBABLE"
    return "SPECULATION"


def score_of(story):
    top = max(i["weight"] for i in story["items"])
    outlets = {i["outlet"] or i["src"] for i in story["items"]}
    bonus = min(25, 8 * (len(outlets) - 1)) + (15 if story["tags"] else 0)
    return min(100, top + bonus)


def collect(cfg, log):
    items, mw = [], {k: v for k, v in cfg.get("media_weights", {}).items() if not k.startswith("_")}
    for src in cfg["sources"]:
        if src.get("enabled") is False:
            continue
        try:
            text = fetch(src["url"])
            got = parse_telegram(text, src) if src["kind"] == "telegram" else parse_feed(text, src)
        except Exception as e:  # один упавший источник не ломает остальные
            log.append("⚠️ %s: %s" % (src["id"], str(e)[:120]))
            continue
        kept = 0
        for it in got:
            if src.get("filter") and not GTA_RE.search(it["title"] + " " + it["summary"]):
                continue
            if src.get("must") and not re.search(src["must"], it["title"], re.I):
                continue
            if len(tokens(it["title"])) < 2:  # пустые и служебные заголовки
                continue
            ts = it["ts"] or now()
            if now() - ts > dt.timedelta(hours=MAX_AGE_H):
                continue
            w = src["weight"]
            group = src["group"]
            if it["outlet"]:
                w = mw.get(it["outlet"], w)
                if w <= 0:  # издание выключено (например, сам Rockstar через Google News — там старые страницы)
                    continue
            items.append({
                "id": uid(src["id"], it["url"]), "src": src["id"], "src_name": src["name"], "outlet": it["outlet"],
                "group": group, "weight": w, "alert": src.get("alert", True),
                "title": it["title"], "url": it["url"], "ts": iso(ts), "summary": it["summary"],
            })
            kept += 1
        log.append("%s: %d/%d" % (src["id"], kept, len(got)))
    return items


def merge(db, items):
    """Новые записи → в истории (кластеры). Возвращает список изменённых историй."""
    seen = {i["id"] for s in db["stories"] for i in s["items"]}
    touched = {}
    for it in sorted(items, key=lambda x: x["ts"]):
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        best, best_sim = None, 0.0
        for s in db["stories"]:
            if now() - parse_iso(s["updated"]) > dt.timedelta(hours=MAX_AGE_H):
                continue
            sim = max(similar(it["title"], x["title"]) for x in s["items"])
            if sim > best_sim:
                best, best_sim = s, sim
        if best is None or best_sim < CLUSTER_SIM:
            best = {"id": it["id"], "title": it["title"], "first_seen": iso(now()), "updated": iso(now()),
                    "items": [], "tags": [], "score": 0, "level": "", "alerted": "", "status": "new"}
            db["stories"].append(best)
        best["items"].append(it)
        best["updated"] = iso(now())
        best["tags"] = sorted(set(best["tags"]) | set(hot_tags(it["title"] + " " + it["summary"])))
        if it["weight"] > max([x["weight"] for x in best["items"][:-1]] or [0]):
            best["title"] = it["title"]  # заголовок истории — от самого весомого источника
        touched[best["id"]] = best
    for s in touched.values():
        s["score"], s["level"] = score_of(s), level_of(s)
    return list(touched.values())


# ---------- GigaChat (этап 2) ----------
GIGA_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGA_API = "https://api.giga.chat/v1"
GIGA_CA = os.path.join(ROOT, "certs", "russian_trusted_root_ca.pem")  # корень Минцифры, только для запросов к GigaChat
GIGA_MODELS = ("GigaChat-3-Ultra", "GigaChat-2-Max", "GigaChat-2-Pro")
AI_PER_RUN = 8  # больше за один запуск не обрабатываем (1 поток, лимит времени GitHub)
VOICE = os.path.join(ROOT, "voice.md")
_giga = {"token": None, "exp": 0.0, "calls": 0, "model": None}


def giga_open(req, timeout=60):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context(cafile=GIGA_CA))


def giga_token():
    key = os.environ.get("GIGACHAT_AUTH_KEY", "").strip()
    if not key:
        return None
    if _giga["token"] and time.time() < _giga["exp"] - 60:
        return _giga["token"]
    req = urllib.request.Request(GIGA_OAUTH, data=urllib.parse.urlencode({"scope": "GIGACHAT_API_PERS"}).encode(), headers={
        "Authorization": "Basic " + key, "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with giga_open(req, 30) as r:
        d = json.load(r)
    _giga["token"], _giga["exp"] = d["access_token"], d.get("expires_at", 0) / 1000 or time.time() + 1500
    return _giga["token"]


def giga_chat(system, user, max_tokens=1200):
    token = giga_token()
    if not token:
        return None, None
    models = ([_giga["model"]] if _giga["model"] else []) + [m for m in GIGA_MODELS if m != _giga["model"]]
    last = None
    for m in models:
        body = json.dumps({"model": m, "temperature": 0.3, "max_tokens": max_tokens, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]}).encode("utf-8")
        req = urllib.request.Request(GIGA_API + "/chat/completions", data=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with giga_open(req, 90) as r:
                d = json.load(r)
            _giga["model"], _giga["calls"] = m, _giga["calls"] + 1
            return d["choices"][0]["message"]["content"], m
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (400, 402, 403, 404, 422, 429):  # модель недоступна / лимит — пробуем следующую
                continue
            raise
    raise RuntimeError("GigaChat: все модели отказали (%s)" % last)


AI_FIELDS = ("title_ru", "summary", "idea", "hook", "stars")


def enrich(s):
    """GigaChat: русский заголовок, суть, идея ролика, хук, оценка. Ошибка не мешает отправке. Пост — по кнопке (make_post)."""
    if s.get("ai") or s.get("ai_fail", 0) >= 2 or _giga["calls"] >= AI_PER_RUN:
        return s.get("ai")
    try:
        voice = open(VOICE, encoding="utf-8").read()
    except FileNotFoundError:
        voice = ""
    system = ("Ты редактор новостей канала DargaNGU о GTA 6. Правила канала:\n\n" + voice +
              "\n\nОтвечай ТОЛЬКО одним JSON-объектом, без markdown и пояснений.")
    src = []
    for i in sorted(s["items"], key=lambda x: -x["weight"])[:8]:
        src.append("- [%s] %s%s" % (i["outlet"] or i["src_name"], i["title"], (" — " + i["summary"][:500]) if i["summary"] else ""))
    user = ("Сегодня %s.\n" % now().astimezone(MSK).strftime("%d.%m.%Y")) + ("Уровень достоверности (посчитан программой, не повышай его): %s\nТемы: %s\nИсточники (%d):\n%s\n\n"
            "Верни JSON с ключами:\n"
            '"title_ru": заголовок по-русски до 90 знаков;\n'
            '"summary": суть в 2–3 предложениях по-русски, только факты из источников, кто сообщил;\n'
            '"idea": идея для Shorts на 30–40 секунд: угол и почему зрителю интересно, 1–2 предложения;\n'
            '"hook": первая фраза ролика, до 12 слов;\n'
            '"stars": 1, 2 или 3 — насколько это хорошая тема для ролика прямо сейчас.') % (
        s["level"], ", ".join(s["tags"]) or "—", len(s["items"]), "\n".join(src))
    allowed = set(re.findall(r"\d+", user + system))
    try:
        for attempt in range(2):
            text, model = giga_chat(system, user)
            if text is None:
                return None
            m = re.search(r"\{.*\}", text, re.S)
            ai = json.loads(m.group(0)) if m else {}
            if not all(k in ai for k in AI_FIELDS):
                raise ValueError("нет полей: " + ",".join(k for k in AI_FIELDS if k not in ai))
            written = " ".join(str(ai[k]) for k in ("title_ru", "summary", "idea", "hook"))
            extra = sorted(set(re.findall(r"\d+", written)) - allowed)
            if not extra:
                break
            # защита от выдуманных цифр: модель берёт даты из старых знаний
            user += ("\n\nВ прошлом ответе были числа, которых нет в источниках и фактах: %s. "
                     "Убери их или замени словами из источников. Верни JSON заново." % ", ".join(extra))
        else:
            raise ValueError("выдуманные числа: " + ", ".join(extra))
        ai = {k: ai[k] for k in AI_FIELDS}
        ai["stars"] = max(1, min(3, int(ai["stars"]))) if str(ai["stars"]).isdigit() else 1
        ai.update(model=model, at=iso(now()))
        s["ai"] = ai
        return ai
    except Exception as e:
        s["ai_fail"] = s.get("ai_fail", 0) + 1
        s["ai_error"] = ("%s: %s" % (type(e).__name__, e))[:200]
        print("GigaChat error:", s["ai_error"])
        return None


# ---------- посты по кнопке ----------
POST_FORMATS = {
    "flash": ("⚡ Срочно", 500,
              "СРОЧНЫЙ короткий пост, как у новостных каналов в момент выхода новости.\n"
              "1) Первая строка — хук-заголовок: эмодзи ⚡️ или ❗️ + суть в 5–9 словах, выдели **жирным**.\n"
              "2) Пустая строка. 1–2 предложения: что случилось и кто сообщил («по данным …», «пишет …»).\n"
              "3) Если не официально — строка «⚠️ Rockstar это не подтверждала.»\n"
              "4) Последняя строка — 2–3 хэштега (#GTA6 #DargaNGU …). Без вопроса к подписчикам.\n"
              "Длина 200–400 знаков."),
    "full": ("📝 Пост", 1400,
             "ПОЛНОЦЕННЫЙ пост, чуть длиннее обычного.\n"
             "1) Первая строка — хук: эмодзи (🔥 / 👀 / ❗️) + цепляющая суть в 5–10 словах, **жирным**.\n"
             "2) Пустая строка. 3–4 коротких абзаца по 1–3 предложения, между ними пустые строки: что случилось, "
             "кто сообщил, подробности из источников, что это значит для игроков (контекст — только из «Базовых фактов»).\n"
             "3) Если есть цифры, сроки или сравнение — 2–5 пунктов с «▪️», ключевые слова **жирным**.\n"
             "4) Если не официально — отдельная строка «⚠️ …не подтверждено.»\n"
             "5) Вопрос к подписчикам + «👇». 6) Последняя строка — 2–3 хэштега, всегда #GTA6 и #DargaNGU.\n"
             "Можно 1 лёгкую шутку. Длина 800–1300 знаков."),
    "list": ("📋 По пунктам", 2000,
             "ПОДРОБНЫЙ пост-список, когда в новости много деталей.\n"
             "1) Первая строка — эмодзи 🔥 + заголовок вида «**Много новых деталей о …**» (подставь тему).\n"
             "2) Пустая строка. Одно предложение: откуда детали («рассказали в интервью …», «пишет …»).\n"
             "3) Пустая строка. 5–10 пунктов, КАЖДЫЙ с новой строки и с ПУСТОЙ строкой между пунктами. "
             "Каждый пункт начинается с подходящего эмодзи (💰 🎮 🗺 👥 🛒 ⏳ 🔒 🚀 и т.п.), затем **2–4 ключевых слова жирным** — и 1–2 коротких предложения. "
             "Пункты — только из источников, каждый — отдельный факт, без повторов.\n"
             "4) Если не официально — строка «⚠️ …не подтверждено.» 5) Вопрос к подписчикам + «👇». 6) 2–3 хэштега, всегда #GTA6 и #DargaNGU.\n"
             "Длина 900–1800 знаков."),
}
ARTICLE_MAX = 3500


def article_text(url):
    """Текст статьи (абзацы <p>) для подробного поста. Google News — ссылки-обёртки, их пропускаем."""
    if "news.google.com" in url or "reddit.com" in url or "t.me/" in url:
        return ""
    try:
        page = fetch(url, 20)
    except Exception:
        return ""
    page = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", page)
    paras = [clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
    paras = [p for p in paras if len(p) > 60 and not re.search(r"cookie|subscribe|newsletter|©|all rights", p, re.I)]
    return "\n".join(paras)[:ARTICLE_MAX]


def post_html(text):
    """Ответ GigaChat → HTML для Telegram: экранируем, **жирное** → <b>, убираем markdown-заголовки."""
    t = html.escape(text.strip().strip("`"))
    t = re.sub(r"(?m)^#{1,4}\s+", "", t)  # markdown-заголовки, но не #хэштеги
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"__(.+?)__", r"<i>\1</i>", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def make_post(s, fmt):
    """Готовый пост для TG/VK в формате fmt (flash/full/list). Кэш — s["posts"][fmt]."""
    s.setdefault("posts", {})
    if s["posts"].get(fmt):
        return s["posts"][fmt]
    label, max_chars, rules = POST_FORMATS[fmt]
    try:
        voice = open(VOICE, encoding="utf-8").read()
    except FileNotFoundError:
        voice = ""
    system = ("Ты редактор новостей канала DargaNGU о GTA 6. Правила канала:\n\n" + voice +
              "\n\nСейчас нужен пост в особом формате — его правила важнее раздела «Формат поста» выше.\n"
              "Ответ — ТОЛЬКО текст поста, без пояснений, без кавычек вокруг. Жирный — **так**. Без ссылок.")
    items = sorted(s["items"], key=lambda x: -x["weight"])[:8]
    src = ["- [%s] %s%s" % (i["outlet"] or i["src_name"], i["title"], (" — " + i["summary"]) if i["summary"] else "") for i in items]
    texts, got = [], 0
    for i in items:  # полный текст 1–2 статей: подробнее и без выдумок
        if got >= (2 if fmt != "flash" else 1):
            break
        t = article_text(i["url"])
        if t:
            texts.append("Текст статьи [%s]:\n%s" % (i["outlet"] or i["src_name"], t))
            got += 1
    user = ("Сегодня %s.\nУровень достоверности (посчитан программой, не повышай его): %s\nТемы: %s\n"
            "Источники (%d):\n%s\n\n%s\n\nФОРМАТ:\n%s") % (
        now().astimezone(MSK).strftime("%d.%m.%Y"), s["level"], ", ".join(s["tags"]) or "—",
        len(s["items"]), "\n".join(src), "\n\n".join(texts) or "(полного текста статей нет — пиши только по заголовкам и описаниям)", rules)
    allowed = set(re.findall(r"\d+", user + system))
    text = None
    for attempt in range(2):
        text, _ = giga_chat(system, user, max_tokens=1600)
        if text is None:
            return None
        extra = sorted(set(re.findall(r"\d+", text)) - allowed)
        if not extra:
            break
        user += ("\n\nВ прошлом ответе были числа, которых нет в источниках: %s. Убери их. Напиши пост заново." % ", ".join(extra))
    else:
        text += "\n\n⚠️ Проверь числа: " + ", ".join(extra)
    s["posts"][fmt] = post_html(text)[:4000]
    return s["posts"][fmt]


# ---------- Telegram ----------
LEVEL_RU = {"CONFIRMED": "✅ ОФИЦИАЛЬНО", "HIGH CONFIDENCE": "🟢 много СМИ", "PROBABLE": "🟡 одно-два СМИ", "SPECULATION": "⚪️ слух / не подтверждено"}


def ago(ts):
    m = int((now() - parse_iso(ts)).total_seconds() // 60)
    return "%d мин назад" % m if m < 60 else "%d ч назад" % (m // 60)


def card(s, head):
    ai = s.get("ai")
    outlets = []
    for i in s["items"]:
        name = i["outlet"] or i["src_name"]
        if name not in [o[0] for o in outlets]:
            outlets.append((name, i["url"]))
    links = " · ".join('<a href="%s">%s</a>' % (html.escape(u, True), html.escape(n)) for n, u in outlets[:5])
    first = min(i["ts"] for i in s["items"])
    tags = (" · #" + " #".join(t.replace("/", "_").replace(" ", "_").replace("-", "_") for t in s["tags"])) if s["tags"] else ""
    meta = "%s · важность %d · %d ист. · вышло %s (%s МСК)%s\n%s" % (
        LEVEL_RU.get(s["level"], s["level"]), s["score"], len(outlets), ago(first),
        parse_iso(first).astimezone(MSK).strftime("%H:%M"), tags, links)
    if not ai:
        return "%s\n<b>%s</b>\n\n%s" % (head, html.escape(s["title"]), meta)
    e = lambda x: html.escape(str(x))
    return "%s\n<b>%s</b>\n\n%s\n\n💡 <b>Ролик</b> %s: %s\n🎬 Хук: «%s»\n\n%s" % (
        head, e(ai["title_ru"]), e(ai["summary"]), "⭐" * ai["stars"], e(ai["idea"]), e(ai["hook"]), meta)


def buttons(s):
    return {"inline_keyboard": [[{"text": POST_FORMATS[f][0], "callback_data": "p:%s:%s" % (f, s["id"])} for f in ("full", "flash", "list")]]}


def urgent(s):
    return s["level"] == "CONFIRMED" or (s["score"] >= URGENT_SCORE and bool(set(s["tags"]) & URGENT_TAGS))


def send_story(s, head, dry):
    """Карточка с кнопками постов. Срочная новость — сразу и короткий пост ответом на карточку."""
    enrich(s)
    msg = tg_send(card(s, head), dry, markup=buttons(s))
    if not msg:
        return False
    s["msg_id"] = msg
    if urgent(s):
        try:
            post = make_post(s, "flash")
            if post:
                tg_send(post, dry, reply_to=msg)
        except Exception as e:
            print("flash post error:", type(e).__name__, str(e)[:120])
    return True


def tg_api(method, params, timeout=20):
    url = "https://api.telegram.org/bot%s/%s" % (os.environ["TG_BOT_TOKEN"], method)
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tg_send(text, dry, markup=None, reply_to=None):
    """Возвращает message_id (или True в dry-режиме), False при ошибке."""
    if dry or not os.environ.get("TG_BOT_TOKEN"):
        print("\n--- TG (dry) ---\n" + re.sub(r"<[^>]+>", "", text) + ("\n[кнопки]" if markup else ""))
        return True
    params = {"chat_id": os.environ["TG_CHAT_ID"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        params["reply_markup"] = markup
    if reply_to and reply_to is not True:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    try:
        d = tg_api("sendMessage", params)
        return d["result"]["message_id"] if d.get("ok") else False
    except urllib.error.HTTPError as e:
        if e.code == 400 and "parse_mode" in params:  # битый HTML от модели — шлём без разметки
            params.pop("parse_mode")
            params["text"] = re.sub(r"<[^>]+>", "", text)
            try:
                d = tg_api("sendMessage", params)
                return d["result"]["message_id"] if d.get("ok") else False
            except Exception:
                pass
        print("TG error: HTTP", e.code)  # без текста ошибки: в URL токен
        return False
    except Exception as e:
        print("TG error:", type(e).__name__)
        return False


def handle_buttons(db, dry):
    """Нажатия «📝 Пост / ⚡ Срочно / 📋 По пунктам» с прошлого запуска → готовим пост и шлём ответом на карточку."""
    if dry or not os.environ.get("TG_BOT_TOKEN") or os.environ.get("RADAR_POLL_BUTTONS") != "1":
        return 0  # кнопки обрабатывает webhook на Vercel (webhook/), опрос — только как запасной режим
    try:
        d = tg_api("getUpdates", {"offset": db["meta"].get("tg_offset", 0), "timeout": 0,
                                  "allowed_updates": ["callback_query"]})
    except Exception as e:
        print("getUpdates error:", type(e).__name__)
        return 0
    by_id, done = {s["id"]: s for s in db["stories"]}, 0
    print("buttons: updates=%d offset=%s" % (len(d.get("result", [])), db["meta"].get("tg_offset")))
    for u in d.get("result", []):
        db["meta"]["tg_offset"] = u["update_id"] + 1
        q = u.get("callback_query")
        if not q or str(q.get("message", {}).get("chat", {}).get("id")) != str(os.environ.get("TG_CHAT_ID")):
            continue  # чужие чаты игнорируем
        try:
            tg_api("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Готовлю пост…"})
        except Exception:
            pass  # кнопку нажали давно — Telegram уже не принимает ответ, это не страшно
        parts = (q.get("data") or "").split(":")
        print("button:", q.get("data"))
        reply_to = q.get("message", {}).get("message_id")
        if len(parts) != 3 or parts[0] != "p" or parts[1] not in POST_FORMATS:
            continue
        s = by_id.get(parts[2])
        if not s:
            tg_send("Эта новость уже удалена из базы (старше %d дней)." % KEEP_DAYS, dry, reply_to=reply_to)
            continue
        try:
            post = make_post(s, parts[1])
        except Exception as e:
            post = None
            print("post error:", type(e).__name__, str(e)[:120])
        if post:
            tg_send(post, dry, reply_to=reply_to)
            done += 1
        else:
            tg_send("Не получилось сделать пост (GigaChat не ответил). Нажми кнопку ещё раз через 10 минут.", dry, reply_to=reply_to)
    return done


def alerts(db, touched, dry, bootstrap):
    msk = now().astimezone(MSK)
    quiet = bool(QUIET) and QUIET[0] <= msk.hour < QUIET[1]
    sent = 0
    if bootstrap:
        for s in db["stories"]:
            s["alerted"] = s["alerted"] or "bootstrap"
        tg_send("📡 <b>Радар DargaNGU запущен.</b>\nВ базе %d историй за последние 48 ч. Дальше пришлю только новое: 🔴 официальное и ⚡ срочное — сразу с коротким постом, 🟡 важное — с кнопками постов, дайджест — в %d:00 МСК." % (len(db["stories"]), DIGEST_HOUR), dry)
        return 0
    recent = [s for s in db["stories"] if s.get("alerted") in ("red", "yellow")
              and now() - parse_iso(s["updated"]) < dt.timedelta(hours=24)]

    def dup(s):  # та же тема уже присылалась за сутки (эвристика до этапа 2, где склеивает нейросеть)
        mine = tokens(s["title"])
        return any(r["id"] != s["id"] and mine & tokens(r["title"]) and set(r["tags"]) & set(s["tags"]) for r in recent)

    queue = touched + [s for s in db["stories"] if s.get("alerted") == "queued"]
    for s in sorted({x["id"]: x for x in queue}.values(), key=lambda x: -x["score"]):
        alertable = any(i["alert"] for i in s["items"])
        if not alertable:
            continue
        if s["level"] == "CONFIRMED" and s["alerted"] in ("", "queued", "yellow"):
            if send_story(s, "🔴 <b>ОФИЦИАЛЬНО</b>", dry):
                s["alerted"], sent = "red", sent + 1
        elif (s["score"] >= ALERT_SCORE + 10 or (s["score"] >= ALERT_SCORE and s["tags"])) and s["alerted"] in ("", "queued"):
            if dup(s):
                s["alerted"] = "dup"
            elif quiet and not urgent(s):
                s["alerted"] = "queued"
            elif send_story(s, "⚡ <b>СРОЧНО</b>" if urgent(s) else "🟡 <b>Новость</b>", dry):
                s["alerted"], sent = "yellow", sent + 1
                recent.append(s)
        elif s["alerted"] == "yellow" and s["level"] == "HIGH CONFIDENCE" and not s.get("upgraded"):
            if tg_send(card(s, "⬆️ <b>Подтверждают уже 3+ СМИ</b>"), dry, markup=buttons(s)):
                s["upgraded"], sent = True, sent + 1
    return sent


def digest(db, dry):
    since = now() - dt.timedelta(hours=24)
    day = [s for s in db["stories"] if parse_iso(s["updated"]) >= since]
    top = sorted(day, key=lambda s: -s["score"])[:7]
    if not top:
        return tg_send("🗞 За сутки по GTA 6 ничего важного.", dry)
    lines = ["🗞 <b>Дайджест GTA 6 за сутки</b> (%d историй, топ-%d)\n" % (len(day), len(top))]
    for n, s in enumerate(top, 1):
        u = max(s["items"], key=lambda i: i["weight"])["url"]
        lines.append('%d. <a href="%s">%s</a>\n   %s · %d' % (n, html.escape(u, True), html.escape((s.get("ai") or {}).get("title_ru") or s["title"]), LEVEL_RU.get(s["level"], ""), s["score"]))
    lines.append("\nВзять в работу: напиши Claude «радар: &lt;номер или тема&gt;».")
    return tg_send("\n".join(lines), dry)


def prune(db):
    limit = now() - dt.timedelta(days=KEEP_DAYS)
    db["stories"] = [s for s in db["stories"] if parse_iso(s["updated"]) >= limit]


def setup():
    """Один раз на Mac: токен вводится скрыто, в чат и файлы не попадает."""
    import getpass
    import subprocess
    token = getpass.getpass("Вставь токен бота от @BotFather (не отображается) и нажми Enter: ").strip()
    print("Теперь открой своего бота в Telegram и отправь ему /start. Жду 60 секунд…")
    chat = None
    for _ in range(12):
        with urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % token, timeout=20) as r:
            res = json.load(r).get("result", [])
        chats = [u["message"]["chat"]["id"] for u in res if "message" in u]
        if chats:
            chat = str(chats[-1])
            break
        time.sleep(5)
    if not chat:
        sys.exit("Не вижу сообщения боту. Отправь /start и запусти --setup ещё раз.")
    repo = os.environ.get("RADAR_REPO", "")
    for name, value in (("TG_BOT_TOKEN", token), ("TG_CHAT_ID", chat)):
        cmd = ["gh", "secret", "set", name] + (["-R", repo] if repo else [])
        subprocess.run(cmd, input=value.encode(), check=True, cwd=ROOT)
    os.environ.update(TG_BOT_TOKEN=token, TG_CHAT_ID=chat)
    tg_send("✅ Бот радара DargaNGU подключён. Секреты записаны в GitHub.", False)
    print("Готово: секреты TG_BOT_TOKEN и TG_CHAT_ID записаны в GitHub, тестовое сообщение отправлено.")


def setup_giga():
    """Один раз на Mac: ключ авторизации GigaChat вводится скрыто → проверка → секрет в GitHub → тестовый пост."""
    import getpass
    import subprocess
    key = getpass.getpass("Вставь Authorization Key из личного кабинета GigaChat (не отображается) и нажми Enter: ").strip()
    os.environ["GIGACHAT_AUTH_KEY"] = key
    try:
        giga_token()
    except Exception as e:
        sys.exit("Ключ не подошёл: %s. Проверь, что скопирован Authorization Key целиком." % type(e).__name__)
    req = urllib.request.Request(GIGA_API + "/models", headers={"Authorization": "Bearer " + _giga["token"], "Accept": "application/json"})
    with giga_open(req, 30) as r:
        ids = [m["id"] for m in json.load(r).get("data", [])]
    print("Ключ работает. Доступные модели:", ", ".join(ids))
    repo = os.environ.get("RADAR_REPO", "")
    subprocess.run(["gh", "secret", "set", "GIGACHAT_AUTH_KEY"] + (["-R", repo] if repo else []), input=key.encode(), check=True, cwd=ROOT)
    db = load_json(NEWS, {"stories": []})
    top = sorted(db["stories"], key=lambda x: -x["score"])
    if top:
        s = dict(top[0], ai=None, ai_fail=0)
        ai = enrich(s)
        print("\nТест на истории «%s» (модель %s):\n" % (top[0]["title"][:70], _giga["model"]))
        print(json.dumps(ai, ensure_ascii=False, indent=1) if ai else "ошибка: " + s.get("ai_error", "?"))
    print("\nГотово: секрет GIGACHAT_AUTH_KEY записан в GitHub.")


def main():
    args = set(sys.argv[1:])
    if "--setup" in args:
        return setup()
    if "--setup-giga" in args:
        return setup_giga()
    if "--test-ai" in args:  # проверка текстов GigaChat на 2 главных историях, без Telegram
        db = load_json(NEWS, {"stories": []})
        for s0 in sorted(db["stories"], key=lambda x: -x["score"])[:2]:
            s = dict(s0, ai=None, ai_fail=0)
            print(json.dumps(enrich(s) or s.get("ai_error"), ensure_ascii=False, indent=1))
        return None
    if "--post" in args:  # один пост по нажатию кнопки: RADAR_POST=формат:id_истории:id_сообщения
        m = re.fullmatch(r"(full|flash|list):([0-9a-f]{12}):(\d+)", os.environ.get("RADAR_POST", ""))
        if not m:
            sys.exit("RADAR_POST: ждём формат:id:message_id")
        fmt, sid, mid = m.group(1), m.group(2), int(m.group(3)) or None
        db = load_json(NEWS, {"stories": []})
        s = next((x for x in db["stories"] if x["id"] == sid), None)
        if not s:
            tg_send("Эта новость уже удалена из базы (старше %d дней)." % KEEP_DAYS, False, reply_to=mid)
            return None
        post = None
        for attempt in range(4):  # GigaChat: 1 поток, при занятости ждём
            try:
                post = make_post(s, fmt)
            except Exception as e:
                print("post error:", type(e).__name__, str(e)[:150])
            if post:
                break
            time.sleep(8)
        if post:
            print("post sent:", tg_send(post, False, reply_to=mid))
        else:
            tg_send("Не получилось сделать пост (GigaChat не ответил). Нажми кнопку ещё раз через минуту.", False, reply_to=mid)
        return None
    if "--tg-info" in args:  # диагностика: вебхук и очередь обновлений бота (токен не печатается)
        print(json.dumps(tg_api("getWebhookInfo", {}), ensure_ascii=False))
        print(json.dumps(tg_api("getUpdates", {"timeout": 0, "limit": 20}), ensure_ascii=False)[:1500])
        return None
    if "--test-card" in args:  # прислать карточку с кнопками (история по RADAR_TEST_Q), чтобы проверить кнопки
        db = load_json(NEWS, {"stories": [], "meta": {}})
        q = os.environ.get("RADAR_TEST_Q", "").lower()
        pool = [x for x in db["stories"] if q in (x["title"] + json.dumps(x.get("ai") or {}, ensure_ascii=False)).lower()] or db["stories"]
        s = max(pool, key=lambda x: x["score"])
        enrich(s)
        mid = tg_send(card(s, "🧪 <b>Тест кнопок</b> — нажми любую, пост придёт за 10 минут"), dry=False, markup=buttons(s))
        print("test-card: message_id =", mid, "story =", s["id"], "ai =", bool(s.get("ai")))
        save_json(NEWS, db)
        return None
    if "--test-post" in args:  # три формата поста для главной истории (или с текстом из RADAR_TEST_Q), без Telegram
        db = load_json(NEWS, {"stories": []})
        q = os.environ.get("RADAR_TEST_Q", "").lower()
        pool = [x for x in db["stories"] if q in (x["title"] + json.dumps(x.get("ai") or {}, ensure_ascii=False)).lower()] or db["stories"]
        s = dict(max(pool, key=lambda x: x["score"]), posts={})
        print("История:", s["title"])
        for f in POST_FORMATS:
            print("\n===== %s =====\n%s" % (POST_FORMATS[f][0], make_post(s, f)))
        return None
    dry = "--dry" in args
    cfg = load_json(SOURCES, {})
    db = load_json(NEWS, {"stories": [], "meta": {}})
    bootstrap = not db["stories"] and not db["meta"].get("started")
    log = []
    posts = handle_buttons(db, dry)  # сначала — посты по кнопкам: их ждут
    touched = merge(db, collect(cfg, log))
    sent = alerts(db, touched, dry, bootstrap)
    msk = now().astimezone(MSK)
    today = msk.strftime("%Y-%m-%d")
    if "--digest" in args or (msk.hour == DIGEST_HOUR and db["meta"].get("digest") != today and not bootstrap):
        if digest(db, dry):
            db["meta"]["digest"] = today
    prune(db)
    db["meta"].update(started=db["meta"].get("started") or iso(now()), last_run=iso(now()), last_log=log)
    db["stories"].sort(key=lambda s: s["updated"], reverse=True)
    save_json(NEWS, db)
    print("run %s · новых/изменённых историй: %d · отправлено: %d · постов по кнопке: %d · всего: %d · GigaChat: %d (%s)" % (
        iso(now()), len(touched), sent, posts, len(db["stories"]), _giga["calls"], _giga["model"] or "—"))
    print(" | ".join(log))


if __name__ == "__main__":
    main()
