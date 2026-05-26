#!/usr/bin/env node
import { chromium } from "playwright";

const url = process.argv[2];
if (!url) {
  console.error("usage: playwright-dom-snapshot.mjs <url>");
  process.exit(2);
}

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15000 });
  const snap = await page.evaluate(() => {
    const text = (document.body?.innerText || document.body?.textContent || "").slice(0, 24000);
    const els = Array.from(document.querySelectorAll("button,a,input,textarea,select,[role]"))
      .slice(0, 80)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const role =
          el.getAttribute("role") ||
          (el.tagName || "element").toLowerCase();
        const label =
          el.getAttribute("aria-label") ||
          el.getAttribute("title") ||
          el.textContent ||
          el.getAttribute("placeholder") ||
          el.getAttribute("value") ||
          "";
        return {
          role,
          label: String(label).trim().slice(0, 300),
          bounds: [rect.x, rect.y, rect.width, rect.height],
          source: "Playwright",
          confidence: 0.98,
        };
      })
      .filter((el) => el.label || el.role);
    return {
      app_name: "Browser",
      window_title: document.title || location.href,
      url: location.href,
      dom_text_sample: text,
      visible_elements: els,
      confidence: 0.96,
    };
  });
  console.log(JSON.stringify(snap));
} finally {
  await browser.close();
}
