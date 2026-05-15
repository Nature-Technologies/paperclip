# Paperclip + OpenClaw Gateway Setup Guide

This guide walks through setting up Paperclip locally via Docker, connecting it to a remote OpenClaw instance, and exposing your local API to the cloud.

---

## Prerequisites

- Docker Desktop installed and running
- OpenClaw running on a cloud server
- Gateway token from your OpenClaw instance
- Administrator access to both Paperclip and OpenClaw

---

## Part 1: Run Paperclip with Docker

### Step 1: Create the env file

Create `docker/.env` in the project root:

```
BETTER_AUTH_SECRET=<run: openssl rand -base64 32>

# Optional: enable Claude / OpenAI adapters inside the container
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
```

Generate the secret with:

```bash
openssl rand -base64 32
```

### Step 2: Build and start

```bash
docker compose -f docker/docker-compose.quickstart.yml up --build
```

Open Paperclip at `http://localhost:3100`.

Data is persisted to `./data/docker-paperclip/` and survives restarts.

### Step 3: Restart after config changes

```bash
docker compose -f docker/docker-compose.quickstart.yml restart
```

---

## Part 2: Connect OpenClaw Agent to Paperclip

### Step 1: Create an Agent Invite in Paperclip

1. Go to `http://localhost:3100` in your browser
2. Navigate to **Settings → Agents**
3. Click **+ Add Agent** and select **OpenClaw Gateway**
4. Fill in the agent details and copy the **invite link**

### Step 2: Submit Join Request from OpenClaw

On your cloud OpenClaw instance:

1. Open the OpenClaw nodes UI
2. Paste the invite link or manually submit a join request with:
   - **Agent name**: Your agent name
   - **Gateway URL**: `wss://your-openclaw-gateway.example` (your OpenClaw WSS URL)
   - **Gateway token**: `x-openclaw-token` header value
   - **Paperclip API URL**: Will be set in Part 4

### Step 3: Approve Pairing Request

1. A pairing request appears in OpenClaw nodes
2. Approve it in the OpenClaw UI
3. OpenClaw automatically retries the join request

### Step 4: Approve Join Request in Paperclip

1. Go back to Paperclip UI
2. In **Settings → Join Requests**, find your agent's pending request
3. Click **Approve**
4. The agent is now created

---

## Part 3: Claim API Key

### Step 1: Get the Claim Secret

After approval, the join request shows a **claimSecret** (one-time, 7-day expiration).

### Step 2: Claim the API Key

Call the claim endpoint from your local machine:

```bash
curl -X POST http://localhost:3100/api/join-requests/{requestId}/claim-api-key \
  -H "Content-Type: application/json" \
  -d '{"claimSecret":"<claim-secret-from-join-request>"}'
```

Save the `token` value from the response.

### Step 3: Move API Key to Cloud Server

SSH into your cloud server and create the file:

```bash
mkdir -p ~/.openclaw/workspace

cat > ~/.openclaw/workspace/paperclip-claimed-api-key.json << 'EOF'
{"token":"pcp_YOUR_API_KEY_HERE","apiKey":"pcp_YOUR_API_KEY_HERE"}
EOF

chmod 644 ~/.openclaw/workspace/paperclip-claimed-api-key.json
```

Replace `pcp_YOUR_API_KEY_HERE` with the token from the claim response.

Verify:

```bash
cat ~/.openclaw/workspace/paperclip-claimed-api-key.json
```

---

## Part 4: Expose Local Paperclip to the Cloud

Your cloud server can't reach `http://localhost:3100` on your local machine. You need a tunnel.

### Step 1: Install cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o ~/cloudflared && chmod +x ~/cloudflared
```

Verify:

```bash
~/cloudflared --version
```

### Step 2: Start the tunnel

Run in a dedicated terminal and **keep it open**:

```bash
~/cloudflared tunnel --url http://localhost:3100
```

Wait ~10 seconds until you see a URL like:

```
https://your-tunnel-name.trycloudflare.com
```

**The tunnel dies if this terminal is closed. Each restart generates a new URL.**

### Step 3: Allow the hostname in Paperclip

Add to `docker/.env`:

```
PAPERCLIP_ALLOWED_HOSTNAMES=your-tunnel-name.trycloudflare.com
```

### Step 4: Restart Paperclip

```bash
docker compose -f docker/docker-compose.quickstart.yml restart
```

### Step 5: Verify the tunnel

```bash
curl https://your-tunnel-name.trycloudflare.com/api/health
```

Should return `200 OK`.

### Step 6: Update Agent Config

In Paperclip UI:

1. Go to **Agents → Your Agent**
2. Find **OpenClaw Gateway adapter settings**
3. Set **Paperclip API URL** to: `https://your-tunnel-name.trycloudflare.com`

---

## Part 5: Full Integration Test

### Step 1: Trigger a Wake Event

1. In Paperclip, create an issue or task
2. Add a comment mentioning your agent
3. The agent should wake up

### Step 2: Monitor the Flow

1. Check OpenClaw nodes for the wake event
2. Paperclip calls the agent via the gateway
3. The agent loads `PAPERCLIP_API_KEY` from `~/.openclaw/workspace/paperclip-claimed-api-key.json`
4. The agent calls back to Paperclip via the tunnel URL

---

## Troubleshooting

### `BETTER_AUTH_SECRET must be set`
- **Fix**: Ensure `docker/.env` contains `BETTER_AUTH_SECRET=...`

### `exec format error` on container start
- **Cause**: `docker-entrypoint.sh` has Windows CRLF line endings or a UTF-8 BOM
- **Fix**: The file must be saved with LF line endings and no BOM. This is already handled — just rebuild: `docker compose -f docker/docker-compose.quickstart.yml up --build`

### `EACCES: permission denied` on `/paperclip`
- **Cause**: Docker created the bind-mount directory as root
- **Fix**: Already handled in `docker-entrypoint.sh` — the container always chowns `/paperclip` at startup. Restart to apply: `docker compose -f docker/docker-compose.quickstart.yml restart`

### Hostname Not Allowed Error (403)
- **Cause**: The tunnel hostname is not in the allowlist
- **Fix**:
  1. Add `PAPERCLIP_ALLOWED_HOSTNAMES=your-tunnel-name.trycloudflare.com` to `docker/.env`
  2. Restart: `docker compose -f docker/docker-compose.quickstart.yml restart`

### `curl` exit code 7 — agent can't reach Paperclip
- **Cause**: Agent is using `http://localhost:3100` which resolves to the cloud server itself, not your local machine
- **Fix**: Set up the Cloudflare tunnel (Part 4) and update the agent's **Paperclip API URL** to the tunnel URL

### Tunnel URL Changes on Restart
- **Cause**: Temporary tunnels generate a new URL each time
- **Fix**: For a stable URL, create a named tunnel with a Cloudflare account — see [Cloudflare Docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps)

### Agent Won't Wake
- **Fix**:
  1. Verify agent is approved in Paperclip
  2. Check the claim secret hasn't expired (7-day limit)
  3. Verify **Paperclip API URL** in agent config is set to the tunnel URL
  4. Test: `curl https://your-tunnel-name.trycloudflare.com/api/health` returns `200`

---

## Summary of Commands

| Task | Command |
|------|---------|
| Generate secret | `openssl rand -base64 32` |
| Start Paperclip | `docker compose -f docker/docker-compose.quickstart.yml up --build` |
| Restart Paperclip | `docker compose -f docker/docker-compose.quickstart.yml restart` |
| Stop Paperclip | `docker compose -f docker/docker-compose.quickstart.yml down` |
| Install cloudflared | `curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/cloudflared && chmod +x ~/cloudflared` |
| Start tunnel | `~/cloudflared tunnel --url http://localhost:3100` |
| Verify tunnel | `curl https://your-tunnel-name.trycloudflare.com/api/health` |
| Create API key file | `mkdir -p ~/.openclaw/workspace && echo '{"token":"KEY","apiKey":"KEY"}' > ~/.openclaw/workspace/paperclip-claimed-api-key.json` |
