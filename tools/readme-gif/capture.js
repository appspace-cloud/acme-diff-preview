// Scroll the rendered PR comment and capture frames for the README GIF.
// Uses Chrome headless via CDP through Playwright's bundled chromium if present,
// otherwise falls back to the system Chrome.
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const OUT = path.join(__dirname, "frames");
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  let puppeteer;
  try {
    puppeteer = require("puppeteer");
  } catch (e) {
    console.error("NEED_PUPPETEER");
    process.exit(2);
  }
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--force-device-scale-factor=2", "--hide-scrollbars"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1180, height: 720, deviceScaleFactor: 2 });
  // Loaded straight off disk. The page is self-contained (inline CSS, no
  // external requests), so there is nothing for a server to add -- and an
  // earlier version pointed at a fixed localhost port, which silently
  // captured whatever OTHER directory happened to be served there.
  await page.goto(`file://${path.join(__dirname, "comment.html")}`, {
    waitUntil: "networkidle0",
  });

  const total = await page.evaluate(
    () => document.body.scrollHeight - window.innerHeight
  );
  console.log("scrollable height:", total);

  // Hold at the top so the verdict is readable, then ease down, then hold.
  const HOLD_TOP = 14, HOLD_END = 16, STEPS = 62;
  let n = 0;
  const shot = async () =>
    page.screenshot({
      path: path.join(OUT, `f${String(n++).padStart(4, "0")}.png`),
    });

  for (let i = 0; i < HOLD_TOP; i++) await shot();
  for (let i = 1; i <= STEPS; i++) {
    // ease-in-out so the scroll feels deliberate rather than mechanical
    const t = i / STEPS;
    const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    await page.evaluate((y) => window.scrollTo(0, y), Math.round(total * e));
    await shot();
  }
  for (let i = 0; i < HOLD_END; i++) await shot();

  await browser.close();
  console.log("frames:", n);
})();
