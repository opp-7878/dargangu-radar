#!/usr/bin/env python3
"""Один раз на Mac: включает мгновенные посты по кнопкам (webhook на Vercel).

Спросит скрыто (в чат и файлы не попадает):
  1) токен бота от @BotFather,
  2) ключ GitHub (тот же, что в cron-job.org: Actions → Read and write).
Затем: проверит ключи → запишет их в настройки Vercel-проекта dargangu-radar-hook → передеплоит →
скажет Telegram слать нажатия кнопок на https://dargangu-radar-hook.vercel.app/api/tg.
"""
import getpass
import json
import os
import secrets
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
HOOK_DIR = os.path.join(ROOT, "webhook")
URL = "https://dargangu-radar-hook.vercel.app/api/tg"
REPO = "opp-7878/dargangu-radar"


def call(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers=dict({"Content-Type": "application/json", "User-Agent": "radar-setup"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return r.status, (json.loads(body) if body else {})


def main():
    bot = getpass.getpass("1/2. Токен бота от @BotFather (не отображается), Enter: ").strip()
    gh = getpass.getpass("2/2. Ключ GitHub github_pat_… (не отображается), Enter: ").strip()
    try:
        me = call("https://api.telegram.org/bot%s/getMe" % bot)[1]["result"]["username"]
    except Exception as e:
        sys.exit("Токен бота не подошёл (%s)." % type(e).__name__)
    try:
        call("https://api.github.com/repos/%s/actions/workflows/radar.yml" % REPO,
             headers={"Authorization": "Bearer " + gh, "Accept": "application/vnd.github+json"})
    except Exception as e:
        sys.exit("Ключ GitHub не подошёл (%s). Нужен доступ Actions: Read and write к %s." % (type(e).__name__, REPO))
    print("Бот @%s и ключ GitHub работают." % me)

    hook_secret = secrets.token_urlsafe(32)
    for name, value in (("TG_BOT_TOKEN", bot), ("GH_TOKEN", gh), ("TG_WEBHOOK_SECRET", hook_secret)):
        r = subprocess.run(["npx", "--yes", "vercel@latest", "env", "add", name, "production", "--force"],
                           input=value.encode(), cwd=HOOK_DIR, capture_output=True)
        if r.returncode:
            sys.exit("Vercel не принял %s:\n%s" % (name, r.stderr.decode()[-400:]))
        print("Vercel: %s записан." % name)
    r = subprocess.run(["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes"], cwd=HOOK_DIR, capture_output=True)
    if r.returncode:
        sys.exit("Деплой не удался:\n%s" % r.stderr.decode()[-600:])
    print("Vercel: функция обновлена.")

    st, res = call("https://api.telegram.org/bot%s/setWebhook" % bot,
                   {"url": URL, "secret_token": hook_secret, "allowed_updates": ["callback_query"], "max_connections": 4})
    info = call("https://api.telegram.org/bot%s/getWebhookInfo" % bot)[1]["result"]
    print("Webhook: ok=%s, адрес=%s, ошибок=%s" % (res.get("ok"), info.get("url"), info.get("last_error_message", "нет")))
    print("\nГотово. Нажми кнопку под любой карточкой — пост придёт за 20–40 секунд.")


if __name__ == "__main__":
    main()
