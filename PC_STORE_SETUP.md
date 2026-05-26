# SongZip PC Store Bridge

Use this when you want the public Render site to keep account data on your own PC instead of
relying on Render's paid persistent disk.

## What it does

- Render keeps serving the public website.
- Your PC runs a small SongZip store bridge backed by the local SQLite database.
- Render sends account, membership, session, settings, snapshot, and PayPal record updates to your PC.

## Requirements

- Your PC must stay on and connected to the internet.
- The bridge must be exposed over HTTPS with a secure tunnel.
- Set a long random `SONGZIP_REMOTE_STORE_SHARED_SECRET`.

## Local PC setup

1. Open [`.spotdl.env`](C:/Users/autom/Documents/Songzip/.spotdl.env) and add:

```env
SONGZIP_REMOTE_STORE_SHARED_SECRET=replace_with_a_long_random_secret
```

2. Start the bridge:

```powershell
cd "C:\Users\autom\Documents\Songzip"
powershell -ExecutionPolicy Bypass -File .\run-songzip-store-bridge.ps1
```

3. Expose it with your tunnel of choice.

Examples:
- Cloudflare Tunnel: point the tunnel at `http://localhost:8820`
- Tailscale Funnel: point the funnel at `http://localhost:8820`

4. Confirm the public bridge URL responds on:

```text
GET /health
```

## Render setup

Add these environment variables to the `songzip` Render service:

```env
SONGZIP_REMOTE_STORE_URL=https://your-public-bridge-url
SONGZIP_REMOTE_STORE_SHARED_SECRET=replace_with_a_long_random_secret
SONGZIP_REMOTE_STORE_TIMEOUT_SECONDS=10
```

After saving them, redeploy Render.

## Important notes

- If your PC or tunnel goes offline, account saves will fail until it comes back.
- Use a strong secret because this bridge can update memberships and sessions.
- For best safety, do not expose the bridge without HTTPS.
