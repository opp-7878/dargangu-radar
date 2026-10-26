// Webhook бота радара: нажатие кнопки под карточкой → сразу запускаем GitHub Actions для одного поста.
// Секреты (только в настройках Vercel): TG_BOT_TOKEN, TG_WEBHOOK_SECRET, GH_TOKEN.
const REPO = "opp-7878/dargangu-radar";
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

export async function POST(request) {
  const secret = process.env.TG_WEBHOOK_SECRET;
  if (!secret || request.headers.get("x-telegram-bot-api-secret-token") !== secret) return json({ error: "unauthorized" }, 401);

  let update;
  try {
    update = await request.json();
  } catch {
    return json({ ok: true });
  }
  const q = update.callback_query;
  if (!q) return json({ ok: true });

  const tg = (method, body) =>
    fetch(`https://api.telegram.org/bot${process.env.TG_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  const msgId = (q.message && q.message.message_id) || 0;
  const dispatch = (workflow, inputs) =>
    fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${process.env.GH_TOKEN}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "dargangu-radar-hook",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    });

  let job = null;
  const p = /^p:(full|flash|list):([0-9a-f]{12})$/.exec(q.data || "");
  const w = /^w:([A-Za-z0-9_-]{11})$/.exec(q.data || "");
  if (p) job = { wf: "radar.yml", inputs: { post: `${p[1]}:${p[2]}:${msgId}` }, ok: "Готовлю пост, 20–40 секунд…", what: "создание поста" };
  else if (w) job = { wf: "outliers.yml", inputs: { work: `${w[1]}:${msgId}` }, ok: "Кладу в очередь FUMO…", what: "очередь FUMO" };
  if (!job) {
    await tg("answerCallbackQuery", { callback_query_id: q.id, text: q.data === "w:done" ? "Уже в работе ✅" : undefined });
    return json({ ok: true });
  }
  const [res] = await Promise.all([
    dispatch(job.wf, job.inputs),
    tg("answerCallbackQuery", { callback_query_id: q.id, text: job.ok }),
  ]);
  if (res.status !== 204 && q.message) {
    await tg("sendMessage", {
      chat_id: q.message.chat.id,
      text: `Не удалось запустить ${job.what} (GitHub ответил ${res.status}). Нажми кнопку ещё раз через минуту.`,
      reply_parameters: { message_id: msgId, allow_sending_without_reply: true },
    });
  }
  return json({ ok: true });
}

export async function GET() {
  return json({ ok: true, service: "dargangu-radar-hook" });
}
