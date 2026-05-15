#!/usr/bin/env -S node --import tsx
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rootEnvPath = path.join(repoRoot, ".env");
const dockerEnvPath = path.join(repoRoot, "docker", ".env");
// The server reads THIS file at startup (server CWD is server/, not repo root)
const instanceEnvPath = path.join(homedir(), ".paperclip", "instances", "default", ".env");
const pnpmBin = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
const cloudflaredBin = process.platform === "win32" ? "cloudflared.exe" : "cloudflared";

const TUNNEL_BLOCK_START = "# === TUNNEL (managed by pnpm dev:tunnel — do not edit) ===";
const TUNNEL_BLOCK_END = "# === END TUNNEL ===";

// Parse --no-dev flag
const noDevServer = process.argv.includes("--no-dev");

function parseEnvFile(filePath: string): Record<string, string> {
  if (!existsSync(filePath)) return {};
  const result: Record<string, string> = {};
  for (const line of readFileSync(filePath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    result[key] = value;
  }
  return result;
}

function readEnvFile(filePath: string): string {
  return existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
}

function upsertManagedBlock(filePath: string, block: string) {
  let content = readEnvFile(filePath);
  const startIdx = content.indexOf(TUNNEL_BLOCK_START);
  const endIdx = content.indexOf(TUNNEL_BLOCK_END);
  if (startIdx !== -1 && endIdx !== -1) {
    content = content.slice(0, startIdx).trimEnd() + "\n\n" + block + content.slice(endIdx + TUNNEL_BLOCK_END.length);
  } else {
    content = content.trimEnd() + "\n\n" + block + "\n";
  }
  writeFileSync(filePath, content, "utf8");
}

function deleteManagedBlock(filePath: string) {
  if (!existsSync(filePath)) return;
  let content = readEnvFile(filePath);
  const startIdx = content.indexOf(TUNNEL_BLOCK_START);
  const endIdx = content.indexOf(TUNNEL_BLOCK_END);
  if (startIdx === -1 || endIdx === -1) return;
  content = content.slice(0, startIdx).trimEnd() + content.slice(endIdx + TUNNEL_BLOCK_END.length);
  writeFileSync(filePath, content.trimEnd() + "\n", "utf8");
}

function writeManagedBlock(tunnelUrl: string) {
  const hostname = tunnelUrl.replace(/^https?:\/\//, "");
  const rootBlock = [
    TUNNEL_BLOCK_START,
    `PAPERCLIP_PUBLIC_URL=${tunnelUrl}`,
    `PAPERCLIP_ALLOWED_HOSTNAMES=${hostname}`,
    TUNNEL_BLOCK_END,
  ].join("\n");
  // Instance block — this is the file the server's dotenv actually loads at startup
  const instanceBlock = [
    TUNNEL_BLOCK_START,
    `PAPERCLIP_ALLOWED_HOSTNAMES=${hostname}`,
    `PAPERCLIP_PUBLIC_URL=${tunnelUrl}`,
    TUNNEL_BLOCK_END,
  ].join("\n");
  upsertManagedBlock(rootEnvPath, rootBlock);
  upsertManagedBlock(instanceEnvPath, instanceBlock);
}

function removeManagedBlock() {
  deleteManagedBlock(rootEnvPath);
  deleteManagedBlock(instanceEnvPath);
}

function log(msg: string) {
  process.stdout.write(`[tunnel] ${msg}\n`);
}

// Read config from both env files (root takes precedence)
const dockerEnv = parseEnvFile(dockerEnvPath);
const rootEnv = parseEnvFile(rootEnvPath);
const mergedEnv = { ...dockerEnv, ...rootEnv };

const port = Number.parseInt(process.env.PORT ?? mergedEnv.PORT ?? "3100", 10) || 3100;
const tunnelToken = process.env.CLOUDFLARE_TUNNEL_TOKEN ?? mergedEnv.CLOUDFLARE_TUNNEL_TOKEN ?? "";

// Check cloudflared is available
try {
  const check = spawn(cloudflaredBin, ["--version"], { stdio: "ignore", shell: process.platform === "win32" });
  await new Promise<void>((resolve, reject) => {
    check.on("error", reject);
    check.on("exit", (code: number | null) => {
      if (code === 0 || code === null) resolve();
      else reject(new Error(`exit ${code}`));
    });
  });
} catch {
  process.stderr.write(
    `[tunnel] cloudflared not found in PATH.\n` +
    `[tunnel] Install it:\n` +
    `[tunnel]   Windows : download cloudflared-windows-amd64.exe from\n` +
    `[tunnel]             https://github.com/cloudflare/cloudflared/releases\n` +
    `[tunnel]             rename to cloudflared.exe and add to PATH\n` +
    `[tunnel]   Linux   : curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared\n` +
    `[tunnel]             chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/\n` +
    `[tunnel]   macOS   : brew install cloudflare/cloudflare/cloudflared\n`,
  );
  process.exit(1);
}

let cloudflaredChild: ReturnType<typeof spawn> | null = null;
let devChild: ReturnType<typeof spawn> | null = null;
let cleanedUp = false;

function cleanup() {
  if (cleanedUp) return;
  cleanedUp = true;
  log("Stopping and cleaning up...");
  cloudflaredChild?.kill("SIGTERM");
  devChild?.kill("SIGTERM");
  removeManagedBlock();
  log("Done.");
}

process.on("SIGINT", () => { cleanup(); process.exit(130); });
process.on("SIGTERM", () => { cleanup(); process.exit(143); });

function runPreflightCleanup() {
  log("Killing stale Postgres processes...");
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/F", "/IM", "postgres.exe"], { stdio: "ignore", shell: true });
    spawnSync("taskkill", ["/F", "/IM", "pg_ctl.exe"], { stdio: "ignore", shell: true });
  } else {
    spawnSync("pkill", ["-f", "postgres"], { stdio: "ignore" });
  }
  const pidFile = path.join(homedir(), ".paperclip", "instances", "default", "db", "postmaster.pid");
  rmSync(pidFile, { force: true });
}

function registerAllowedHostname(tunnelUrl: string) {
  const hostname = tunnelUrl.replace(/^https?:\/\//, "").split("/")[0];
  log(`Registering allowed hostname: ${hostname}`);
  spawnSync(pnpmBin, ["paperclipai", "allowed-hostname", hostname], {
    stdio: "inherit",
    cwd: repoRoot,
    shell: process.platform === "win32",
  });
}

function startDevServer(tunnelUrl: string) {
  if (noDevServer) {
    log(`Tunnel active. Start dev server manually with: pnpm dev`);
    log(`Press Ctrl+C to stop the tunnel and clean up .env`);
    return;
  }

  registerAllowedHostname(tunnelUrl);
  runPreflightCleanup();
  log("Starting dev server...");
  const hostname = tunnelUrl.replace(/^https?:\/\//, "").split("/")[0];
  devChild = spawn(pnpmBin, ["dev"], {
    stdio: "inherit",
    cwd: repoRoot,
    shell: process.platform === "win32",
    env: {
      ...process.env,
      PAPERCLIP_ALLOWED_HOSTNAMES: hostname,
      PAPERCLIP_PUBLIC_URL: tunnelUrl,
    },
  });

  devChild.on("error", (err: Error) => {
    process.stderr.write(`[tunnel] dev server error: ${err.message}\n`);
    cleanup();
    process.exit(1);
  });

  devChild.on("exit", (code: number | null, signal: NodeJS.Signals | null) => {
    if (cleanedUp) return;
    cleanup();
    if (signal) process.exit(1);
    process.exit(code ?? 0);
  });
}

if (tunnelToken) {
  // Named tunnel — stable URL, no URL parsing needed
  log("Named Cloudflare tunnel (CLOUDFLARE_TUNNEL_TOKEN is set)");
  const existingPublicUrl = mergedEnv.PAPERCLIP_PUBLIC_URL;
  if (existingPublicUrl && existingPublicUrl !== "http://localhost:3100") {
    writeManagedBlock(existingPublicUrl);
    log(`Using stable tunnel URL: ${existingPublicUrl}`);
    log(`Set this as paperclipApiUrl in your OpenClaw agent config`);
  } else {
    log("Warning: CLOUDFLARE_TUNNEL_TOKEN is set but PAPERCLIP_PUBLIC_URL is not.");
    log("Set PAPERCLIP_PUBLIC_URL in .env to the stable tunnel URL (e.g. https://my-app.example.com).");
  }

  cloudflaredChild = spawn(cloudflaredBin, ["tunnel", "run", "--token", tunnelToken], {
    stdio: "inherit",
    shell: process.platform === "win32",
  });

  startDevServer(existingPublicUrl ?? tunnelToken);
} else {
  // Quick tunnel — parse URL from cloudflared output
  log(`Starting cloudflared quick tunnel on http://localhost:${port}...`);
  log(`Waiting for tunnel URL before starting the dev server...`);

  cloudflaredChild = spawn(
    cloudflaredBin,
    ["tunnel", "--url", `http://localhost:${port}`],
    { stdio: ["ignore", "pipe", "pipe"], shell: process.platform === "win32" },
  );

  const urlPattern = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/;
  let tunnelUrlFound = false;

  function processCloudflaredOutput(chunk: Buffer) {
    const text = chunk.toString();
    process.stdout.write(text);
    if (tunnelUrlFound) return;
    const match = urlPattern.exec(text);
    if (!match) return;

    tunnelUrlFound = true;
    const tunnelUrl = match[0];
    writeManagedBlock(tunnelUrl);
    log(`Tunnel ready: ${tunnelUrl}`);
    log(`Set this as paperclipApiUrl in your OpenClaw agent config`);
    startDevServer(tunnelUrl);
  }

  cloudflaredChild.stdout?.on("data", processCloudflaredOutput);
  cloudflaredChild.stderr?.on("data", processCloudflaredOutput);
}

cloudflaredChild.on("error", (err: Error) => {
  process.stderr.write(`[tunnel] cloudflared error: ${err.message}\n`);
  cleanup();
  process.exit(1);
});

cloudflaredChild.on("exit", (code: number | null, signal: NodeJS.Signals | null) => {
  if (cleanedUp) return;
  if (signal) { cleanup(); process.exit(1); }
  if (code !== 0) {
    process.stderr.write(`[tunnel] cloudflared exited with code ${code}\n`);
    cleanup();
    process.exit(code ?? 1);
  }
  cleanup();
  process.exit(0);
});
