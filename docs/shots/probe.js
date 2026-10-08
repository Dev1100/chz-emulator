// Разведка: что видно после клика по разделу (текст экрана в консоль, снимок в raw/_probe.png)
module.exports = async function (ctx) {
  const steps = (process.env.STEPS || 'НСИ').split('|');
  for (const s of steps) { await ctx.click(s); await ctx.wait(2500); }
  console.log(await ctx.evaluate("document.body.innerText.slice(0, 3000)"));
  await ctx.shot("_probe.png");
};
