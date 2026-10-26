#!/usr/bin/env python3
"""YouTube outliers about GTA → Telegram with a "🎬 В работу" button.

Outlier = a GTA video that got N× more views than the usual video of the same
channel in the same format (long vs Shorts). Same idea as vidIQ "Outliers".
Free: public channel RSS (views included) + YouTube search via yt-dlp (metadata only).

  python3 outliers.py                # daily run: find, send top new ones to Telegram, save state
  python3 outliers.py --dry          # same, print instead of sending
  RADAR_WORK=<videoId>:<msgId> python3 outliers.py --work   # button pressed → add to work queue

Files: data/yt_channels.json (channel pool), data/yt_outliers.json (sent + last run),
data/work/<videoId>.json (work queue, one file per video).
"""
import datetime as dt
import html
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import radar  # Telegram + GigaChat helpers

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CHANNELS = os.path.join(DATA, "yt_channels.json")
STATE = os.path.join(DATA, "yt_outliers.json")
WORK = os.path.join(DATA, "work")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"
COOKIE = "SOCS=CAI; CONSENT=PENDING+999"  # "reject all" consent, avoids the EU consent page
WEEK = "EgIIAw%3D%3D"                      # search filter: upload date = this week

DAYS = 7               # window
MIN_SCORE = 3          # outlier threshold for the report
MIN_VIEWS = {"long": 10000, "short": 30000}
SEND_MAX = 5           # Telegram: at most per day
SEND_PER_CHANNEL = 2
SEND_SCORE = {"RU": 3, "EN": 5}   # Telegram: lower bar for Russian videos
KEEP_DAYS = 30

QUERIES = [
    "GTA 6", "GTA 6 news", "GTA VI", "GTA 6 gameplay", "GTA 6 trailer", "GTA 6 release",
    "GTA 6 shorts", "Vice City GTA 6", "Rockstar GTA 6",
    "ГТА 6", "GTA 6 новости", "GTA 6 на русском", "ГТА 6 обзор", "GTA 6 слив",
]
GTA_RE = re.compile(r"gta|grand theft|гта|vice city|вайс.?сити|leonida|леонид|lucia|люсия|rockstar|рокстар", re.I)
OFFICIAL_RE = re.compile(r"^(xbox|playstation|rockstar games|ign|gamespot)$", re.I)  # trailers, not ideas
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}


def http(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if getattr(e, "code", None) == 404:
                return ""
            time.sleep(2 * (i + 1))
    return ""


def search(ch, log, n=60):
    """YouTube search 'this week' via yt-dlp. New GTA channels join the pool."""
    found = fails = 0
    for q in QUERIES:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q) + "&sp=" + WEEK
        try:
            r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", "--playlist-end", str(n), url],
                               capture_output=True, text=True, timeout=180)
            entries = json.loads(r.stdout).get("entries", [])
        except Exception:
            fails += 1
            continue
        for e in entries:
            cid = e.get("channel_id")
            if cid and GTA_RE.search(e.get("title") or "") and cid not in ch:
                ch[cid] = {"name": e.get("channel") or "", "handle": "", "subs": 0,
                           "source": "search: " + q, "added": dt.date.today().isoformat()}
                found += 1
    log.append("поиск: +%d каналов, ошибок %d/%d" % (found, fails, len(QUERIES)))
    return found


def feed(cid, kind):
    """kind: LF = long videos, SH = Shorts. Last 15 uploads with views."""
    xml = http("https://www.youtube.com/feeds/videos.xml?playlist_id=UU%s%s" % (kind, cid[2:]))
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", NS):
        st = e.find("m:group/m:community/m:statistics", NS)
        out.append({
            "id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS) or "",
            "published": e.findtext("a:published", namespaces=NS),
            "views": int(st.get("views")) if st is not None and st.get("views") else 0,
            "format": "short" if kind == "SH" else "long",
        })
    return out


def analyse(ch, days=DAYS, min_score=MIN_SCORE, min_views=MIN_VIEWS):
    now = dt.datetime.now(dt.timezone.utc)
    jobs = [(cid, k) for cid in ch for k in ("LF", "SH")]
    with ThreadPoolExecutor(10) as ex:
        feeds = list(ex.map(lambda j: feed(*j), jobs))
    res = []
    for (cid, kind), vids in zip(jobs, feeds):
        if len(vids) < 5:
            continue
        for v in vids:
            pub = dt.datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            v["age_h"] = round((now - pub).total_seconds() / 3600, 1)
        for v in vids:
            if v["age_h"] > days * 24 or not GTA_RE.search(v["title"]):
                continue
            others = [o["views"] for o in vids if o["id"] != v["id"] and o["age_h"] > 24]
            if len(others) < 4:
                continue
            base = max(statistics.median(others), 1)
            score = v["views"] / base
            if score >= min_score and v["views"] >= min_views[v["format"]]:
                res.append(dict(v, channel=ch[cid]["name"], channel_id=cid, baseline=int(base), score=round(score, 1),
                                lang="RU" if re.search(r"[а-яё]", v["title"], re.I) else "EN",
                                url=("https://www.youtube.com/shorts/" if v["format"] == "short"
                                     else "https://www.youtube.com/watch?v=") + v["id"]))
    res.sort(key=lambda r: -r["score"])
    return res, sum(1 for f in feeds if f)


def fmt_n(n):
    return "%.1f млн" % (n / 1e6) if n >= 1e6 else "%.0f тыс." % (n / 1e3) if n >= 1e3 else str(n)


def age_txt(h):
    return "%.0f ч" % h if h < 48 else "%.1f дн" % (h / 24)


# ---------- ideas (GigaChat) ----------

def idea_for(o):
    """Angle for DargaNGU: idea, hook, format, stars. None if GigaChat is silent."""
    try:
        voice = open(radar.VOICE, encoding="utf-8").read()
    except FileNotFoundError:
        voice = ""
    system = ("Ты сценарист русского YouTube-канала DargaNGU о GTA 6 (зрители — мужчины 25–34, юмор взрослый и ироничный). "
              "Правила канала:\n\n" + voice + "\n\nОтвечай ТОЛЬКО одним JSON-объектом, без markdown и пояснений.")
    user = ("Чужой ролик стал «выбросом» — набрал в %s раза больше обычного у своего канала.\n"
            "Название: %s\nКанал: %s\nЯзык: %s · Формат: %s · Просмотры: %d · Возраст: %s\n\n"
            "Предложи, как сделать СВОЙ ролик на эту тему по-русски (не копию). Верни JSON:\n"
            '"idea": угол и почему зайдёт нашему зрителю, 1–2 предложения;\n'
            '"hook": первая фраза ролика, до 12 слов;\n'
            '"format": "Short" или "Длинный";\n'
            '"stars": 1–5 — насколько хорошая тема для нас прямо сейчас (5 — горячая и понятная, на русском такого нет; '
            '3 — нормальная; 1 — не подходит: реакция на чужое видео, пиратство, чужой мем без смысла).') % (
        o["score"], o["title"], o["channel"], o["lang"], "Shorts" if o["format"] == "short" else "длинный",
        o["views"], age_txt(o["age_h"]))
    allowed = set(re.findall(r"\d+", user + system))
    try:
        text, model = radar.giga_chat(system, user, max_tokens=500)
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        ai = json.loads(m.group(0)) if m else {}
        if not all(k in ai for k in ("idea", "hook", "format", "stars")):
            return None
        if set(re.findall(r"\d+", ai["idea"] + " " + ai["hook"])) - allowed:  # invented numbers
            ai["idea"] = re.sub(r"\d+", "", ai["idea"]).replace("  ", " ")
        ai["stars"] = max(1, min(5, int(ai["stars"]))) if str(ai["stars"]).isdigit() else 3
        ai["model"] = model
        return ai
    except Exception as e:
        print("GigaChat error:", type(e).__name__, str(e)[:120])
        return None


def card(o, n, total):
    e = lambda x: html.escape(str(x))
    ai = o.get("ai")
    head = "📈 <b>Выброс %d/%d</b> · <b>%s×</b> от обычного · %s · %s" % (
        n, total, o["score"], o["lang"], "Short" if o["format"] == "short" else "длинный")
    body = '<a href="%s"><b>%s</b></a>\n%s · %s просм. (обычно %s) · вышло %s назад' % (
        e(o["url"]), e(o["title"]), e(o["channel"]), fmt_n(o["views"]), fmt_n(o["baseline"]), age_txt(o["age_h"]))
    if not ai:
        return head + "\n" + body
    return "%s\n%s\n\n💡 <b>Наш ролик</b> %s · %s: %s\n🎬 Хук: «%s»" % (
        head, body, "⭐" * ai["stars"] + "☆" * (5 - ai["stars"]), e(ai["format"]), e(ai["idea"]), e(ai["hook"]))


def work_button(vid):
    return {"inline_keyboard": [[{"text": "🎬 В работу", "callback_data": "w:" + vid}]]}


def pick(res, sent):
    out, per = [], {}
    # Russian topics count double: that is our audience and there is less competition
    for o in sorted(res, key=lambda r: -r["score"] * (2 if r["lang"] == "RU" else 1)):
        if o["id"] in sent or OFFICIAL_RE.match(o["channel"].strip()):
            continue
        if o["score"] < SEND_SCORE[o["lang"]] or per.get(o["channel_id"], 0) >= SEND_PER_CHANNEL:
            continue
        per[o["channel_id"]] = per.get(o["channel_id"], 0) + 1
        out.append(o)
        if len(out) >= SEND_MAX:
            break
    return out


# ---------- runs ----------

def daily(dry):
    log = []
    ch = radar.load_json(CHANNELS, {})
    state = radar.load_json(STATE, {"sent": {}, "meta": {}})
    if "--no-search" not in sys.argv:
        search(ch, log)
    radar.save_json(CHANNELS, ch)
    res, n_feeds = analyse(ch)
    log.append("каналов %d, лент %d, выбросов %d" % (len(ch), n_feeds, len(res)))
    top = pick(res, state["sent"])
    if top:
        radar.tg_send("📈 <b>YouTube-выбросы по GTA за %d дней</b>: %d всего, новых лучших — %d.\n"
                      "Под каждым — «🎬 В работу»: ролик попадёт в очередь FUMO." % (DAYS, len(res), len(top)), dry)
    for i, o in enumerate(top, 1):
        o["ai"] = idea_for(o)
        mid = radar.tg_send(card(o, i, len(top)), dry, markup=work_button(o["id"]))
        if mid:
            state["sent"][o["id"]] = dict(o, msg_id=mid if mid is not True else None, sent_at=radar.iso(radar.now()))
    cutoff = time.time() - KEEP_DAYS * 86400
    state["sent"] = {k: v for k, v in state["sent"].items()
                     if radar.parse_iso(v["sent_at"]).timestamp() > cutoff}
    state["meta"] = {"last_run": radar.iso(radar.now()), "log": log, "top_today": [o["id"] for o in top],
                     "all": [{k: o[k] for k in ("id", "score", "views", "lang", "format", "channel", "title")} for o in res]}
    if not dry:
        radar.save_json(STATE, state)
    print("outliers %s · %s · отправлено %d · GigaChat %d" % (radar.iso(radar.now()), " · ".join(log), len(top),
                                                                  radar._giga["calls"]))


def work(dry):
    m = re.fullmatch(r"([A-Za-z0-9_-]{11}):(\d+)", os.environ.get("RADAR_WORK", ""))
    if not m:
        sys.exit("RADAR_WORK: ждём videoId:message_id")
    vid, mid = m.group(1), int(m.group(2)) or None
    state = radar.load_json(STATE, {"sent": {}})
    o = state["sent"].get(vid)
    path = os.path.join(WORK, vid + ".json")
    if not o:
        radar.tg_send("Этот выброс уже удалён из базы (старше %d дней)." % KEEP_DAYS, dry, reply_to=mid)
        return
    if not os.path.exists(path):
        os.makedirs(WORK, exist_ok=True)
        item = {k: o.get(k) for k in ("id", "title", "url", "channel", "lang", "format", "views", "baseline", "score", "ai")}
        item.update(status="new", added=radar.iso(radar.now()))
        if not dry:
            radar.save_json(path, item)
    if not dry and mid and os.environ.get("TG_BOT_TOKEN"):
        try:
            radar.tg_api("editMessageReplyMarkup", {"chat_id": os.environ["TG_CHAT_ID"], "message_id": mid,
                         "reply_markup": {"inline_keyboard": [[{"text": "✅ В работе", "callback_data": "w:done"}]]}})
        except Exception as e:
            print("edit markup error:", type(e).__name__)
    radar.tg_send("✅ В очереди FUMO. Скажи Claude: «FUMO: очередь» — сделаем сценарий.", dry, reply_to=mid)
    print("work: %s added" % vid)


def main():
    dry = "--dry" in sys.argv
    if "--work" in sys.argv:
        return work(dry)
    return daily(dry)


if __name__ == "__main__":
    main()
