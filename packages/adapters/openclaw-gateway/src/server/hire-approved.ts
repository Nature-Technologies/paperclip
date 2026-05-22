import { randomUUID } from "node:crypto";
import { WebSocket } from "ws";
import type {
  HireApprovedPayload,
  HireApprovedHookResult,
} from "@paperclipai/adapter-utils";

const PROTOCOL_VERSION = 4;
const CONNECT_TIMEOUT_MS = 30_000;

type ReqFrame = { type: "req"; id: string; method: string; params?: unknown };
type ResFrame = {
  type: "res";
  id: string;
  ok: boolean;
  payload?: unknown;
  error?: { code?: unknown; message?: unknown };
};
type EventFrame = { type: "event"; event: string; payload?: unknown };

function asRecord(v: unknown): Record<string, unknown> | null {
  return typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

function nonEmpty(v: unknown): string | null {
  return typeof v === "string" && v.trim().length > 0 ? v.trim() : null;
}

function toStringRecord(v: unknown): Record<string, string> {
  const rec = asRecord(v);
  const out: Record<string, string> = {};
  if (!rec) return out;
  for (const [k, val] of Object.entries(rec)) {
    if (typeof val === "string") out[k] = val;
  }
  return out;
}

function rawBytes(data: unknown): string {
  if (typeof data === "string") return data;
  if (Buffer.isBuffer(data)) return data.toString("utf8");
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  return String(data);
}

function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  label: string,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`timeout: ${label}`)), ms);
    promise.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

/**
 * Sends a one-shot "claim your API key" wake to OpenClaw over WebSocket.
 * Uses disableDeviceAuth so no pairing flow is required for this administrative task.
 */
async function sendClaimWake(opts: {
  url: string;
  headers: Record<string, string>;
  gatewayToken: string;
  agentId: string;
  paperclipApiUrl: string;
  joinRequestId: string;
  freshClaimSecret: string;
  claimedApiKeyPath: string;
}): Promise<void> {
  const ws = new WebSocket(opts.url, {
    headers: opts.headers,
    maxPayload: 4 * 1024 * 1024,
  });

  const pending = new Map<
    string,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();
  let resolveChallenge!: (nonce: string) => void;
  let rejectChallenge!: (e: Error) => void;
  const challengePromise = new Promise<string>((res, rej) => {
    resolveChallenge = res;
    rejectChallenge = rej;
  });
  challengePromise.catch(() => {});

  function send(method: string, params: unknown): Promise<unknown> {
    const id = randomUUID();
    ws.send(
      JSON.stringify({ type: "req", id, method, params } satisfies ReqFrame),
    );
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
    });
  }

  function failAll(err: Error) {
    for (const { reject } of pending.values()) reject(err);
    pending.clear();
    rejectChallenge(err);
  }

  ws.on("message", (data) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawBytes(data));
    } catch {
      return;
    }

    const rec = asRecord(parsed);
    if (!rec) return;

    if (rec.type === "event") {
      const frame = parsed as EventFrame;
      if (frame.event === "connect.challenge") {
        const nonce = nonEmpty(asRecord(frame.payload)?.nonce);
        if (nonce) resolveChallenge(nonce);
      }
      return;
    }

    if (rec.type === "res") {
      const frame = parsed as ResFrame;
      const entry = pending.get(frame.id);
      if (!entry) return;
      pending.delete(frame.id);
      if (frame.ok) {
        entry.resolve(frame.payload ?? null);
      } else {
        const errRec = asRecord(frame.error);
        entry.reject(
          new Error(
            nonEmpty(errRec?.message) ??
              nonEmpty(errRec?.code) ??
              "gateway request failed",
          ),
        );
      }
    }
  });

  ws.on("close", (code, reason) => {
    failAll(new Error(`gateway closed (${code}): ${rawBytes(reason)}`));
  });

  ws.on("error", (err) => {
    failAll(err instanceof Error ? err : new Error(String(err)));
  });

  try {
    // Wait for socket open
    await withTimeout(
      new Promise<void>((resolve, reject) => {
        ws.once("open", resolve);
        ws.once("error", reject);
        ws.once("close", (code) =>
          reject(new Error(`closed before open (${code})`)),
        );
      }),
      CONNECT_TIMEOUT_MS,
      "websocket open",
    );

    // Wait for challenge then connect
    const nonce = await withTimeout(
      challengePromise,
      CONNECT_TIMEOUT_MS,
      "connect challenge",
    );
    await withTimeout(
      send("connect", {
        minProtocol: PROTOCOL_VERSION,
        maxProtocol: PROTOCOL_VERSION,
        client: {
          id: "gateway-client",
          version: "paperclip",
          platform: process.platform,
          mode: "backend",
        },
        role: "operator",
        scopes: ["operator.admin"],
        auth: { token: opts.gatewayToken },
        nonce,
      }),
      CONNECT_TIMEOUT_MS,
      "connect",
    );

    const message = buildClaimMessage(opts);
    await withTimeout(
      send("agent", {
        message,
        idempotencyKey: `paperclip-claim-${opts.agentId}`,
        sessionKey: `paperclip-claim-${opts.agentId.slice(0, 8)}`,
      }),
      CONNECT_TIMEOUT_MS,
      "agent send",
    );
  } finally {
    ws.close(1000, "paperclip-claim-finished");
  }
}

function buildClaimMessage(opts: {
  paperclipApiUrl: string;
  joinRequestId: string;
  freshClaimSecret: string;
  claimedApiKeyPath: string;
}): string {
  return [
    "Your Paperclip board membership has been approved. You must now claim your API key.",
    "",
    "Perform the following steps exactly, in order. Do not skip any step.",
    "",
    "## Step 1: Claim your API key",
    "",
    `POST ${opts.paperclipApiUrl}/api/join-requests/${opts.joinRequestId}/claim-api-key`,
    "Headers: Content-Type: application/json",
    `Body: {"claimSecret":"${opts.freshClaimSecret}"}`,
    "",
    'Expected response: HTTP 201 with JSON body containing { "keyId", "token", "agentId", "createdAt" }',
    "",
    "## Step 2: Save the API key",
    "",
    `Save the full JSON response to: ${opts.claimedApiKeyPath}`,
    `chmod 600 ${opts.claimedApiKeyPath}`,
    "",
    "## Step 3: Configure your environment",
    "",
    "Set these in your OpenClaw environment configuration (https://docs.openclaw.ai/help/environment):",
    `PAPERCLIP_API_KEY=<token field from the saved JSON>`,
    `PAPERCLIP_API_URL=${opts.paperclipApiUrl}`,
    "",
    "Important:",
    "- The claim secret is single-use and expires in 7 days.",
    "- Complete steps 1-3 now in this session.",
    "- Do not attempt to claim more than once — a second attempt will fail.",
  ].join("\n");
}

export async function onHireApproved(
  payload: HireApprovedPayload,
  adapterConfig: Record<string, unknown>,
): Promise<HireApprovedHookResult> {
  if (!payload.freshClaimSecret || !payload.joinRequestId) {
    // Key already claimed or not a join_request source — nothing to do.
    return { ok: true };
  }

  const url = nonEmpty(adapterConfig.url);
  if (!url) {
    return { ok: false, error: "adapter config missing gateway url" };
  }

  const headers = toStringRecord(adapterConfig.headers);
  const gatewayToken =
    headers["x-openclaw-token"] ??
    headers["x-openclaw-auth"] ??
    Object.entries(headers).find(
      ([k]) => k.toLowerCase() === "x-openclaw-token",
    )?.[1] ??
    Object.entries(headers).find(
      ([k]) => k.toLowerCase() === "x-openclaw-auth",
    )?.[1] ??
    null;

  if (!gatewayToken) {
    return {
      ok: false,
      error: "adapter config missing x-openclaw-token header",
    };
  }

  const paperclipApiUrl = nonEmpty(adapterConfig.paperclipApiUrl);
  if (!paperclipApiUrl) {
    return {
      ok: false,
      error:
        "adapter config missing paperclipApiUrl — cannot instruct agent where to call claim endpoint",
    };
  }

  const claimedApiKeyPath =
    nonEmpty(adapterConfig.claimedApiKeyPath) ??
    "~/.openclaw/workspace/paperclip-claimed-api-key.json";

  try {
    await sendClaimWake({
      url,
      headers,
      gatewayToken,
      agentId: payload.agentId,
      paperclipApiUrl,
      joinRequestId: payload.joinRequestId,
      freshClaimSecret: payload.freshClaimSecret,
      claimedApiKeyPath,
    });
    return { ok: true };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, error: `claim wake failed: ${message}` };
  }
}
