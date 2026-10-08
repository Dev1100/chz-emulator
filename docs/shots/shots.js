// Скриншоты для ИНСТРУКЦИЯ.md: настройка 1С (веб-клиент публикации ka99) и интерфейс эмулятора.
// Запуск: node cdp.js shots.js   →  raw/*.png + marks.json, затем python _annotate.py → img/*.png
const EMU = "http://127.0.0.1:3128/";

module.exports = async function (ctx) {
  // элемент, чей текст или data-content СОДЕРЖИТ подстроку (самый глубокий видимый)
  const near = (sub, idx = 0) => ctx.evaluate(`(() => {
    const want = ${JSON.stringify(sub)}.toLowerCase();
    const has = e => ((e.getAttribute && e.getAttribute('data-content')) || e.textContent || '').toLowerCase().includes(want);
    let els = [...document.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && has(e); });
    els = els.filter(e => !els.some(o => o !== e && e.contains(o)));
    const el = els[${idx}]; if (!el) return null;
    const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  })()`);
  const pad = (b, p = 4) => b && { x: b.x - p, y: b.y - p, w: b.w + 2 * p, h: b.h + 2 * p };

  const section = async () => { await ctx.click("НСИ"); await ctx.waitText("Интеграция с ИС МП (обувь, одежда, табак...)"); await ctx.wait(1500); };
  const clickBox = async b => { await ctx.clickAt(b.x + b.w / 2, b.y + b.h / 2); };
  const reveal = async sub => { await ctx.evaluate(`(() => { const want = ${JSON.stringify(sub)};
    let els = [...document.querySelectorAll('*')].filter(e => (e.textContent || '').trim() === want);
    els = els.filter(e => !els.some(o => o !== e && e.contains(o))); if (els[0]) els[0].scrollIntoView({ block: 'center' }); })()`); await ctx.wait(800); };

  // 0. Раздел «НСИ и администрирование»: где искать настройки
  await section();
  await reveal("Интеграция с ИС МП (обувь, одежда, табак...)");
  await ctx.shot("00_section.png", [{ label: "Интеграция с ИС МП (обувь, одежда, табак...)", num: 1 },
                                     { label: "Персональные настройки", num: 2 },
                                     { label: "Печатные формы, отчеты и обработки", num: 3 }]);

  // 1. Тестовый контур ИС МП
  await reveal("Интеграция с ИС МП (обувь, одежда, табак...)");
  await ctx.click("Интеграция с ИС МП (обувь, одежда, табак...)");
  await ctx.waitText("Электронные подписи и авторизация", 60000); await ctx.wait(1500);
  await ctx.click("Электронные подписи и авторизация");
  await ctx.waitText("Тестовый контур", 30000); await ctx.wait(1500);
  await ctx.shot("01_test_contour.png", [{ label: "Электронные подписи и авторизация", num: 1 },
                                         { box: pad(await near("Тестовый контур")), num: 2 }]);

  // 2. Общие настройки → параметры прокси-сервера
  await section();
  await reveal("Персональные настройки");
  await ctx.click("Персональные настройки");
  await ctx.waitText("Настройка доступа к Интернету", 60000); await ctx.wait(2500);
  const proxyLink = await near("Настройка доступа к Интернету");
  await ctx.shot("02_general_settings.png", [{ box: pad(proxyLink), num: 1 }]);

  // 4. Расширения: ЧЗ_БезПодписи
  await section();
  await reveal("Печатные формы, отчеты и обработки");
  await ctx.click("Печатные формы, отчеты и обработки");
  await ctx.waitText("Расширения", 60000); await ctx.wait(2000);
  await ctx.click("Расширения");
  await ctx.waitText("Расширение подключено", 60000); await ctx.wait(2500);
  await ctx.clickAt(1360, 131); await ctx.type("без подписи"); await ctx.key("Enter", "Enter", 13); await ctx.wait(3000);
  await ctx.shot("04_extensions.png", [{ label: "Добавить из файла", num: 1 }, { box: pad(await near("без подписи")), num: 2 }]);

  // 5–7. Эмулятор: журнал запросов 1С, действия, автосценарий
  await ctx.goto(EMU);
  await ctx.waitText("Эмулятор Честного Знака");
  await ctx.wait(1500);
  await ctx.shot("05_emu_log.png");
  await ctx.click("Нац. каталог");
  await ctx.wait(1200);
  await ctx.shot("05a_emu_nk.png", [{ box: pad(await near("Карточка товара")), num: 1 },
                                    { box: pad(await near("Импорт из CSV")), num: 2 }]);
  await ctx.click("Действия");
  await ctx.wait(800);
  await ctx.shot("06_emu_actions.png", [{ box: pad(await near("Сгенерировать коды")), num: 1 },
                                       { box: pad(await near("Входящая отгрузка")), num: 2 },
                                       { box: pad(await near("Скачать расширение")), num: 3 }]);
  await ctx.click("Автосценарий");
  await ctx.click("Запустить");
  await ctx.waitText("Все шаги прошли", 60000);
  await ctx.shot("07_emu_autotest.png");
  await ctx.goto(ctx.URL_BASE);   // вернуться в 1С, чтобы cdp.js закрыл сеанс (logout)
  await ctx.wait(2500);
};
