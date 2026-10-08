// Драйвер снимков веб-клиента 1С через CDP (без зависимостей, Node 22+).
// Запуск: node cdp.js <сценарий.js>
// Сценарий экспортирует async function (ctx) — ctx: {shot, click, clickAt, type, key, evaluate, wait, waitText, box, marks}
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9334;
const URL_BASE = process.env.URL_BASE || "http://localhost:8081/ka99/ru/";
const OUT = path.join(__dirname, "raw");
const W = 1600, H = 1000;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function fetchJson(url) {
  const res = await fetch(url);
  return res.json();
}

async function launch() {
  const profile = path.join(os.tmpdir(), "cdp-1c-profile");
  const proc = spawn(CHROME, [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    `--window-size=${W},${H}`,
    "--hide-scrollbars",
    "--force-device-scale-factor=1",
    "--no-first-run",
    "--disable-gpu",
    "about:blank",
  ], { stdio: "ignore" });
  for (let i = 0; i < 60; i++) {
    try { await fetchJson(`http://127.0.0.1:${PORT}/json/version`); return proc; } catch { await sleep(500); }
  }
  throw new Error("Chrome не поднялся");
}

class Session {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async open(wsUrl) {
    const ws = new WebSocket(wsUrl);
    const s = new Session(ws);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && s.pending.has(msg.id)) {
        const { res, rej } = s.pending.get(msg.id);
        s.pending.delete(msg.id);
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
      }
    };
    return s;
  }
  send(method, params = {}, timeoutMs = 60000) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      const timer = setTimeout(() => { this.pending.delete(id); rej(new Error("CDP timeout: " + method)); }, timeoutMs);
      this.pending.set(id, { res: v => { clearTimeout(timer); res(v); }, rej: e => { clearTimeout(timer); rej(e); } });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function main() {
  const scenarioPath = process.argv[2];
  if (!scenarioPath) throw new Error("укажите файл сценария");
  fs.mkdirSync(OUT, { recursive: true });

  const proc = await launch();
  const targets = await fetchJson(`http://127.0.0.1:${PORT}/json/list`);
  const page = targets.find(t => t.type === "page");
  const s = await Session.open(page.webSocketDebuggerUrl);

  await s.send("Page.enable");
  await s.send("Runtime.enable");
  await s.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  const evaluate = async expr => {
    const r = await s.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "js error");
    return r.result.value;
  };

  const marks = {};
  const ctx = {
    evaluate,
    wait: sleep,
    async goto(url) {
      await s.send("Page.navigate", { url });
      await sleep(1500);
    },
    async waitText(txt, timeout = 30000) {
      const t0 = Date.now();
      while (Date.now() - t0 < timeout) {
        const ok = await evaluate(`document.body.innerText.includes(${JSON.stringify(txt)}) || !!document.querySelector('[data-content*=${JSON.stringify(txt)}]')`);
        if (ok) { await sleep(400); return true; }
        await sleep(400);
      }
      const { data } = await s.send("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(path.join(OUT, "_debug.png"), Buffer.from(data, "base64"));
      const txtNow = await evaluate("document.body.innerText.slice(0,600)");
      console.log("--- на экране сейчас ---\n" + txtNow + "\n---");
      throw new Error("не дождался текста: " + txt);
    },
    // Прямоугольник элемента по видимой подписи (учитывает data-content 1С)
    async box(label, idx = 0) {
      return evaluate(`(() => {
        const norm = t => (t || '').replace(/[.…\s]+$/g, '').replace(/\s+/g, ' ').trim();
        const want = norm(${JSON.stringify(label)});
        const hit = e => {
          const d = e.getAttribute && e.getAttribute('data-content');
          if (d && norm(d) === want) return true;
          return norm(e.textContent) === want;
        };
        let els = [...document.querySelectorAll('*')].filter(e => {
          const r = e.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && hit(e);
        });
        // оставляем самые глубокие совпадения: родителей с тем же текстом отбрасываем
        els = els.filter(e => !els.some(o => o !== e && e.contains(o)));
        const el = els[${idx}]; if (!el) return null;
        let node = el;
        // у подписи-«ярлыка» рамку рисуем по родителю: он включает саму кнопку/поле
        const r0 = el.getBoundingClientRect();
        if (r0.width < 8 || r0.height < 8) node = el.parentElement || el;
        const r = node.getBoundingClientRect();
        return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
      })()`);
    },
    async clickAt(x, y, dbl = false) {
      const common = { x, y, button: "left", clickCount: dbl ? 2 : 1 };
      await s.send("Input.dispatchMouseEvent", { type: "mousePressed", ...common });
      await s.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...common });
      await sleep(700);
    },
    async click(label, { idx = 0, dbl = false, dx = 0, dy = 0 } = {}) {
      // элемент может появиться не сразу: панель перерисовывается после выбора строки
      let b = null;
      for (let i = 0; i < 6 && !b; i++) {
        b = await ctx.box(label, idx);
        if (!b) await sleep(800);
      }
      if (!b) throw new Error("не нашёл элемент: " + label);
      await ctx.clickAt(b.x + b.w / 2 + dx, b.y + b.h / 2 + dy, dbl);
      return b;
    },
    async scroll(x, y, dy) {
      await s.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: dy });
      await sleep(600);
    },
    async type(text) {
      for (const ch of text) await s.send("Input.dispatchKeyEvent", { type: "char", text: ch });
      await sleep(300);
    },
    async key(key, code, keyCode) {
      await s.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
      await s.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
      await sleep(500);
    },
    // Снимок + обводки: labels = [{label, idx, num, dx, dy, dw, dh}] или готовые box
    async shot(name, labels = []) {
      const items = [];
      for (const m of labels) {
        let b = m.box || await ctx.box(m.label, m.idx || 0);
        if (!b) { await sleep(800); b = await ctx.box(m.label, m.idx || 0); }
        if (!b) { console.log("  ! нет элемента для обводки:", m.label); continue; }
        items.push({
          box: [b.x + (m.dx || 0), b.y + (m.dy || 0), b.w + (m.dw || 0), b.h + (m.dh || 0)],
          label: m.num ? String(m.num) : undefined,
        });
      }
      const { data } = await s.send("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(path.join(OUT, name), Buffer.from(data, "base64"));
      if (items.length) {
        marks[name] = items;
        // пишем сразу: прогон могут прервать, обводки терять нельзя
        const mp = path.join(__dirname, "marks.json");
        const prev = fs.existsSync(mp) ? JSON.parse(fs.readFileSync(mp, "utf8")) : {};
        fs.writeFileSync(mp, JSON.stringify({ ...prev, [name]: items }, null, 1), "utf8");
      }
      console.log("  снят", name, items.length ? `(обводок: ${items.length})` : "");
    },
    URL_BASE,
  };

  // Вход
  await ctx.goto(URL_BASE);
  await sleep(2500);
  const needLogin = await evaluate(`!!document.querySelector('input[placeholder="Пользователь"]')`);
  if (needLogin) {
    const box = await evaluate(`(() => { const i = document.querySelector('input[placeholder="Пользователь"]');
      const r = i.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
    await ctx.clickAt(box.x, box.y);
    await ctx.type("Администратор");
    await sleep(500);
    const btn = await evaluate(`(() => { const b = [...document.querySelectorAll('button')].find(b => (b.textContent||'').trim().startsWith('Войти'));
      const r = b.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
    await ctx.clickAt(btn.x, btn.y);
    await sleep(4000);
  }
  await ctx.waitText("Начальная страница", 60000);
  await sleep(2000);

  const scenario = require(path.resolve(scenarioPath));
  try {
    await scenario(ctx);
  } finally {
    // сеанс обязательно закрыть: иначе следующий запуск упрётся в «вход невозможен»
    await ctx.goto(URL_BASE + "e1cib/logout").catch(() => {});
    await sleep(1500);
  }

  const marksPath = path.join(__dirname, "marks.json");
  const prev = fs.existsSync(marksPath) ? JSON.parse(fs.readFileSync(marksPath, "utf8")) : {};
  fs.writeFileSync(marksPath, JSON.stringify({ ...prev, ...marks }, null, 1), "utf8");
  console.log("marks.json обновлён:", Object.keys(marks).length, "картинок");

  await s.send("Browser.close").catch(() => {});
  proc.kill();
  process.exit(0);
}

main().catch(e => { console.error("ОШИБКА:", e.message); process.exit(1); });
