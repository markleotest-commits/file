# Misroute Checker — Vercel site with password login

This folder IS the website. Two files:

- `index.html` — the MC app (copy of `mc/MC_V2.html` from 2026-10-07).
- `middleware.js` — password gate (HTTP Basic Auth, dependency-free). The
  password is the `SITE_PASSWORD` env var. It is NEVER in this folder.

## First deploy (one time, ~10 min)

1. Install Node.js: in PowerShell, `winget install -e --id OpenJS.NodeJS.LTS`,
   then close and reopen the terminal. Check with `node --version`.
2. Log in to Vercel (free account is fine):
   ```
   cd C:\Users\phlspxuser\sheet-bot\mc-site
   npx vercel login
   ```
   (opens a browser SSO page — that login is yours; nothing is stored here).
3. Deploy:
   ```
   npx vercel --prod
   ```
   Answer: project name (e.g. `sheet-mc`), scope, defaults for the rest.
   You get `https://<name>.vercel.app` — it 401s until step 4 (fail closed).
4. Set the password (value typed at the prompt — never saved to disk):
   ```
   npx vercel env add SITE_PASSWORD production
   ```
   Use letters/numbers (avoid non-ASCII). Any username works at login —
   only this password is checked.
5. Redeploy so the env var takes effect:
   ```
   npx vercel --prod
   ```
6. Open the URL, log in with your password. Done.

## Point the bot at it

In `C:\Users\phlspxuser\sheet-bot\config.py`:

```python
MC_URL = "https://anyuser:YOUR_PASSWORD@<name>.vercel.app"
```

(username can be anything; the password is the same `SITE_PASSWORD`).
Empty `MC_URL` = old behavior (local `mc/MC_V2.html`). The bot finds the
Vercel tab by host, opens it when missing, and everything else (paste,
validate, packed OIDs, QR generator) works unchanged — same DOM.

## Updating the site later (MC_V2.html changes)

```powershell
Copy-Item ..\mc\MC_V2.html index.html
npx vercel --prod
```

The middleware stays. No env changes needed.

## Notes

- HTTPS enforced by Vercel; the basic-auth password is encrypted in transit.
- `localStorage` (custom hubs `spx_hubs`, LH remark, UI prefs) is per-origin:
  the site starts with a FRESH bucket — re-import custom hubs once via the
  in-app file picker (or export `custom-hubs-*.csv` from the local copy
  first, then import it on the site).
- Vercel free tier sleeps nothing for static + middleware; cold starts are
  ~100 ms at the edge. No server to keep awake.
- If you ever see 401 with the right password: env var not set on
  production, or you forgot the redeploy in step 5.
