import http from "node:http";
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const MOCK_MEET_HTML = `
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Google Meet - Lobby</title>
  <style>
    body { font-family: Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #202124; color: #e8eaed; }
    .card { background: #303134; padding: 32px; border-radius: 8px; text-align: center; }
    input { width: 260px; padding: 8px; font-size: 16px; border: 1px solid #8ab4f8; border-radius: 4px; margin-bottom: 16px; background: #3c4043; color: #e8eaed; }
    button { padding: 10px 24px; font-size: 15px; background: #8ab4f8; border: none; border-radius: 4px; cursor: pointer; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Join meeting</h1>
    <input type="text" jsname="L9xHkb" placeholder="Meeting code" />
    <button>Ask to join</button>
  </div>
</body>
</html>
`;

const PORT = 39876;

function startServer(): Promise<http.Server> {
  return new Promise((resolve, reject) => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(MOCK_MEET_HTML);
    });
    server.listen(PORT, "127.0.0.1", () => resolve(server));
    server.on("error", reject);
  });
}

async function run(): Promise<void> {
  const server = await startServer();
  console.log(`Mock server listening on http://127.0.0.1:${PORT}`);

  let exitCode = 1;

  try {
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();

    await page.goto(`http://127.0.0.1:${PORT}`);

    // Verify the input field exists and fill it
    const input = await page.locator('input[jsname="L9xHkb"]');
    await input.waitFor({ state: "visible" });
    await input.fill("abcde-fghij");
    console.log("Input field found and filled successfully.");

    // Verify the "Ask to join" button and click it
    const button = await page.locator('button:has-text("Ask to join")');
    await button.waitFor({ state: "visible" });
    await button.click();
    console.log('"Ask to join" button found and clicked successfully.');

    // Take screenshot
    const screenshotPath = "/tmp/test-join-success.png";
    await page.screenshot({ path: screenshotPath, fullPage: true });
    console.log(`Screenshot saved to ${screenshotPath}`);

    await browser.close();
    exitCode = 0;
    console.log("Test passed.");
  } catch (err) {
    console.error("Test failed:", err);
  } finally {
    server.close();
  }

  process.exit(exitCode);
}

run();
