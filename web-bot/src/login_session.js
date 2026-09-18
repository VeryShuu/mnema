// Otkryvaet brauzer (headful na DISPLAY) dlya vruchnogo logina Zoom.
// Zhivoye okno cherez noVNC: pol'zovatel' vvodit dannye; my zhdem 20 min,
// periodicheski proveryaya i pishiya v konsol' chto na ekrane.
import { chromium } from "playwright";

const url = process.argv[2] || "https://app.zoom.us/wc/home";
const ctx = await chromium.launchPersistentContext(
  process.env.ZOOM_PROFILE_DIR || "/data/zoom-profile",
  { headless: false,
    args: ["--use-fake-device-for-media-stream", "--window-size=1400,860"] });
const page = ctx.pages()[0] || await ctx.newPage();
await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(e => console.log("goto:", e.message));
console.log("[login] brauzer otkryt na", url);
for (let i = 0; i < 120; i++) {
  await new Promise(r => setTimeout(r, 10000));
  const txt = await page.evaluate(() =>
    (document.body?.innerText || "").replace(/\s+/g, " ").slice(0, 200)).catch(() => "");
  const urlNow = page.url();
  console.log(`[login] ${Math.round(i * 10 / 60 * 10) / 10} min | ${urlNow.slice(0, 90)} | ${txt.slice(0, 120)}`);
  const home = /app\.zoom\.us\/wc\/home|profile/i.test(urlNow) &&
               !/signin|sign-in|login/i.test(urlNow);
  const prejoin = /Join|Войти/i.test(txt);
  if (home && !/Automated bots aren't allowed/i.test(txt)) {
    console.log("[login] VOZMOZHNO SESSIYA AKTIVNA (dom bez steny). Znachenii: sobscheniya ostal'nogo proveryayutsya avtomaticheski.");
  }
  if (i > 20 && prejoin && home) { console.log("[login] vykhod: sessiya + prejoin dostupny"); break; }
}
await ctx.close();
console.log("[login] zaversheno. Sobranie protsessa uberyot fony.");
