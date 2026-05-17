# Render Deployment

This repository now includes a Render Blueprint in `render.yaml` and a Docker-based deployment path in `Dockerfile`.

## What the Render setup does

- deploys SongZip as a Render web service
- installs `ffmpeg` in the image so audio conversion works
- mounts a persistent disk at `/var/data/songzip`
- stores SongZip account data, provider links, downloads, bundles, and temp state under that persistent path
- starts the hosted ASGI app with `uvicorn spotdl.render_app:app`
- health-checks the service on `/api/version`

## Recommended Render flow

1. Push this repo to GitHub.
2. In Render, create a new Blueprint from the repo.
3. Let Render read `render.yaml`.
4. Fill in the `sync: false` secrets during setup, especially:
   - `SPOTDL_CLIENT_ID`
   - `SPOTDL_CLIENT_SECRET`
   - `SPOTDL_SPOTIFY_OAUTH_CLIENT_ID`
   - `SPOTDL_SPOTIFY_OAUTH_CLIENT_SECRET`
   - `SPOTDL_SPOTIFY_OAUTH_REDIRECT_URI`
   - `SPOTDL_GOOGLE_OAUTH_CLIENT_ID`
   - `SPOTDL_GOOGLE_OAUTH_CLIENT_SECRET`
   - `SPOTDL_GOOGLE_OAUTH_REDIRECT_URI`
   - `SONGZIP_GOOGLE_LOGIN_CLIENT_ID`
   - `SONGZIP_GOOGLE_LOGIN_CLIENT_SECRET`
   - `SONGZIP_GOOGLE_LOGIN_REDIRECT_URI`
   - `SONGZIP_ADMIN_EMAIL`
   - `SONGZIP_PAYPAL_CLIENT_ID`
   - `SONGZIP_PAYPAL_CLIENT_SECRET`
   - `SONGZIP_PAYPAL_WEBHOOK_ID`
5. After the first deploy, update the Spotify and Google OAuth redirect URIs in their dashboards so they match the actual Render hostname for your service.

## Notes

- Render web services need to listen on `0.0.0.0` and the Render-provided `PORT`.
- Render filesystems are ephemeral unless you attach a persistent disk, so the disk is important for SongZip state.
- Attached disks require a paid web service plan on Render.
- Google sign-in for SongZip accounts uses `openid email profile` and needs an authorized redirect URI that exactly matches your deployed callback URL.
