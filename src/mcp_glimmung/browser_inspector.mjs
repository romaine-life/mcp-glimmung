import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const playwrightModule = pathToFileURL(
  process.env.PLAYWRIGHT_PACKAGE_PATH || require.resolve("playwright"),
).href;
const playwright = await import(playwrightModule);
const chromium = playwright.chromium || playwright.default?.chromium;
if (!chromium) {
  throw new Error(`Unable to load Playwright Chromium from ${playwrightModule}`);
}

const input = JSON.parse(await new Promise((resolve, reject) => {
  let data = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => { data += chunk; });
  process.stdin.on("end", () => resolve(data || "{}"));
  process.stdin.on("error", reject);
}));

if (!input.playwrightWsEndpoint || typeof input.playwrightWsEndpoint !== "string") {
  throw new Error(
    "playwrightWsEndpoint is required: inspect_browser_url runs in the leased "
    + "test slot's slot-playwright pod, not on the MCP host.",
  );
}

function truncate(value, limit) {
  const text = String(value ?? "");
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(limit - 3, 0)).trimEnd()}...`;
}

function interestingElements() {
  return (maxElements) => {
    const max = maxElements ?? 80;
    const selectorParts = (el) => {
      if (el.id) return `#${CSS.escape(el.id)}`;
      const testId = el.getAttribute("data-testid") || el.getAttribute("data-test");
      if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
      const aria = el.getAttribute("aria-label");
      if (aria) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
      return el.tagName.toLowerCase();
    };
    const selectorFor = (el) => {
      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
        let part = selectorParts(cur);
        if (!cur.id && cur.parentElement) {
          const siblings = Array.from(cur.parentElement.children)
            .filter((s) => s.tagName === cur.tagName);
          if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
        }
        parts.unshift(part);
        if (cur.id) break;
        cur = cur.parentElement;
      }
      return parts.join(" > ");
    };
    const roleFor = (el) => {
      const explicit = el.getAttribute("role");
      if (explicit) return explicit;
      const tag = el.tagName.toLowerCase();
      if (tag === "button") return "button";
      if (tag === "a" && el.hasAttribute("href")) return "link";
      if (tag === "input") return el.getAttribute("type") || "textbox";
      if (/^h[1-6]$/.test(tag)) return "heading";
      if (tag === "select") return "combobox";
      if (tag === "textarea") return "textbox";
      if (tag === "canvas") return "canvas";
      return null;
    };
    const candidates = Array.from(document.querySelectorAll(
      "main,header,nav,section,article,h1,h2,h3,button,a,input,select,textarea,[role],canvas,[data-testid],[data-test]",
    ));
    return candidates
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const visible = rect.width > 0 && rect.height > 0
          && style.visibility !== "hidden"
          && style.display !== "none"
          && Number(style.opacity || "1") > 0;
        const text = (el.innerText || el.getAttribute("aria-label") || el.getAttribute("alt") || "")
          .replace(/\s+/g, " ")
          .trim();
        return {
          selector: selectorFor(el),
          tag: el.tagName.toLowerCase(),
          role: roleFor(el),
          text,
          visible,
          bounds: {
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          },
          styles: {
            display: style.display,
            position: style.position,
            color: style.color,
            backgroundColor: style.backgroundColor,
            fontSize: style.fontSize,
            zIndex: style.zIndex,
          },
        };
      })
      .filter((el) => el.visible || el.text || el.role || el.tag === "canvas")
      .slice(0, max);
  };
}

function canvasStats() {
  return () => Array.from(document.querySelectorAll("canvas")).map((canvas, index) => {
    const rect = canvas.getBoundingClientRect();
    const out = {
      index,
      selector: canvas.id ? `#${CSS.escape(canvas.id)}` : `canvas:nth-of-type(${index + 1})`,
      width: canvas.width,
      height: canvas.height,
      bounds: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
      readable: false,
      nonblank_pixels: null,
      sampled_pixels: null,
    };
    try {
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      if (!ctx || canvas.width === 0 || canvas.height === 0) return out;
      const sampleW = Math.min(canvas.width, 128);
      const sampleH = Math.min(canvas.height, 128);
      const image = ctx.getImageData(0, 0, sampleW, sampleH).data;
      let nonblank = 0;
      for (let i = 0; i < image.length; i += 4) {
        if (image[i] !== 0 || image[i + 1] !== 0 || image[i + 2] !== 0 || image[i + 3] !== 0) {
          nonblank += 1;
        }
      }
      out.readable = true;
      out.nonblank_pixels = nonblank;
      out.sampled_pixels = sampleW * sampleH;
    } catch (e) {
      out.error = String(e?.message ?? e);
    }
    return out;
  });
}

const startedAt = new Date();
const consoleMessages = [];
const pageErrors = [];
const failedRequests = [];
const httpErrors = [];

const browser = await chromium.connect({ wsEndpoint: input.playwrightWsEndpoint });
try {
  const context = await browser.newContext({
    viewport: input.viewport || { width: 1440, height: 900 },
    ignoreHTTPSErrors: true,
  });

  // Auth-injection plumbing: cookies, extra HTTP headers, and per-origin
  // localStorage seeds. The slot-playwright pod has no credentials of its
  // own — every authenticated browse has to come from the caller, which
  // forwards a session cookie / bearer token / pre-populated storage. See
  // the inspect_url docstring in browser_inspector.py for the typical
  // tank-operator auth.romaine.life → /api/auth/exchange flow that
  // produces these values.
  if (Array.isArray(input.cookies) && input.cookies.length > 0) {
    await context.addCookies(input.cookies);
  }
  if (input.extraHttpHeaders && typeof input.extraHttpHeaders === "object") {
    await context.setExtraHTTPHeaders(input.extraHttpHeaders);
  }
  if (input.localStorage && typeof input.localStorage === "object") {
    // Init scripts run on every page before any page script. We seed
    // localStorage for any origin whose entry matches the current
    // document. Errors (e.g. opaque-origin sandboxed iframes that
    // refuse localStorage access) are swallowed: the goal is best-
    // effort seeding for the top-level navigation, not a guarantee for
    // every nested context.
    await context.addInitScript((items) => {
      try {
        const here = items && items[window.location.origin];
        if (!here) return;
        for (const [k, v] of Object.entries(here)) {
          try { window.localStorage.setItem(k, v); } catch (_) { /* ignore */ }
        }
      } catch (_) { /* ignore */ }
    }, input.localStorage);
  }

  const page = await context.newPage();
  if (input.captureConsole !== false) {
    page.on("console", (msg) => consoleMessages.push({
      type: msg.type(),
      text: truncate(msg.text(), 1000),
      location: msg.location(),
    }));
    page.on("pageerror", (err) => pageErrors.push(String(err?.message ?? err)));
  }
  if (input.captureNetwork !== false) {
    page.on("requestfailed", (req) => failedRequests.push({
      url: req.url(),
      method: req.method(),
      failure: req.failure(),
    }));
    page.on("response", (resp) => {
      if (resp.status() >= 400) {
        httpErrors.push({
          url: resp.url(),
          status: resp.status(),
          status_text: resp.statusText(),
        });
      }
    });
  }

  let status = null;
  let responseError = null;
  let networkIdleReached = false;
  try {
    const response = await page.goto(input.url, {
      waitUntil: "domcontentloaded",
      timeout: input.timeoutMs ?? 30000,
    });
    status = response ? response.status() : null;
  } catch (e) {
    // Navigation itself failed (DNS, refused connection, navigation timeout,
    // etc.). Real error — surface it through response_error.
    responseError = String(e?.message ?? e);
  }
  if (responseError === null) {
    // Navigation succeeded. Wait a bounded window for the page to settle to
    // network idle. Canvas-driven, WebSocket-heavy, polling, and animated
    // pages never reach network idle by design, so timing out here is the
    // expected case rather than an error. Surface it as a soft signal
    // (`network_idle_reached`) and keep response_error clean — callers
    // should not see a "page is broken" signal for a page that's working
    // exactly as intended.
    try {
      await page.waitForLoadState("networkidle", { timeout: Math.min(input.timeoutMs ?? 30000, 10000) });
      networkIdleReached = true;
    } catch (_) {
      networkIdleReached = false;
    }
  }
  if ((input.waitMs ?? 0) > 0) {
    await page.waitForTimeout(input.waitMs);
  }

  let bodyText = "";
  try {
    bodyText = await page.locator("body").innerText({ timeout: 2000 });
  } catch {
    bodyText = "";
  }

  let screenshotPath = null;
  let screenshotBytes = 0;
  if (typeof input.screenshotPath !== "string" || input.screenshotPath.length === 0) {
    throw new Error(
      "screenshotPath is required: the Python wrapper owns the tempfile lifecycle "
      + "and the screenshot is uploaded to glimmung as a durable artifact instead "
      + "of round-tripped through stdout as base64.",
    );
  }
  const fs = await import("node:fs/promises");
  const buf = await page.screenshot({
    path: input.screenshotPath,
    fullPage: input.fullPage !== false,
  });
  screenshotPath = input.screenshotPath;
  screenshotBytes = buf.length;
  // Belt-and-braces: confirm the tempfile got the bytes Chromium just emitted.
  // Playwright writes via stream when `path` is given, but the path side and
  // the buffer return should agree in size.
  const stat = await fs.stat(screenshotPath);
  if (stat.size !== screenshotBytes) {
    throw new Error(
      `screenshot tempfile size=${stat.size} disagrees with chromium buffer size=${screenshotBytes}`,
    );
  }

  let accessibility = null;
  if (input.captureAccessibility) {
    try {
      accessibility = await page.accessibility.snapshot({ interestingOnly: true });
    } catch (e) {
      accessibility = { error: String(e?.message ?? e) };
    }
  }

  const result = {
    schema_version: 2,
    url: input.url,
    final_url: page.url(),
    status,
    response_error: responseError,
    network_idle_reached: networkIdleReached,
    title: await page.title(),
    body_text: truncate(bodyText.replace(/\0/g, ""), input.bodyTextLimit ?? 4000),
    viewport: input.viewport || { width: 1440, height: 900 },
    elements: await page.evaluate(interestingElements(), input.maxElements ?? 80),
    console: consoleMessages,
    page_errors: pageErrors,
    failed_requests: failedRequests,
    http_errors: httpErrors,
    accessibility,
    screenshot_path: screenshotPath,
    screenshot_size_bytes: screenshotBytes,
    canvas: await page.evaluate(canvasStats()),
    inspected_at: startedAt.toISOString(),
  };
  await context.close();
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} finally {
  await browser.close();
}
