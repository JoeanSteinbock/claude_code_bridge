---
name: expose
description: Publish a local dev server at https://<sub>.luc.wtf via the vps-luc-wtf Cloudflare tunnel.
---

Use `expose` when you've started a local dev server on this VPS and want it reachable from the outside internet (for the user to open in a browser, share a preview, run webhooks into, etc.).

## When to use

Reach for it whenever you `<run a dev server>` and the user likely wants to view it. Common triggers:

- `npm run dev`, `bun dev`, `next dev`, `vite`, `astro dev`, ...
- `python -m http.server`, `ruby -run -e httpd`, `caddy`, ...
- a webhook receiver you just started (stripe, github, telegram test bot, …)
- any long-running process listening on a TCP port that should be externally reachable

Do NOT use it for ports that are meant to stay local (databases, redis, background queues, internal-only APIs).

## Commands

```
expose <port> [subdomain]     # publish http://localhost:<port> at https://<sub>.luc.wtf
expose --list                 # show current routes
expose --remove <subdomain>   # take a route down
expose --url <subdomain>      # print the URL without changing anything
```

If you omit `<subdomain>`, it defaults to `$(basename $PWD)`. So from `~/projects/xbtoshi/game-310` the default is `game-310.luc.wtf`.

Subdomain rules: `[a-z0-9-]`, 1–63 chars, no leading/trailing dash.

## Port notes

- Dev servers must bind an **unprivileged port** (≥ 1024). Port 80/443 require root which no process on this VPS has. Pick something like 3000, 5173, 8080, 8910.
- Bind to `127.0.0.1` or `0.0.0.0` — both work; cloudflared connects over loopback.
- The tunnel automatically terminates TLS, so your dev server speaks plain HTTP and users hit HTTPS at the edge.

## What to tell the user

After exposing, share the URL in your reply:

```
Started next-dev on :3000.
→ https://game-310.luc.wtf
```

Don't describe the mechanism unless asked. Just hand them the URL.

## Cleanup

Routes survive across sessions (they live in `~/.cloudflared/ingress.d/`). If you're done with a dev server, `expose --remove <sub>` to avoid stale routes. When in doubt, `expose --list` to see what's live.

## Under the hood

- Tunnel: `vps-luc-wtf` (id `3636c660-de78-4fb0-9b68-465d669c19fb`)
- Wildcard DNS: `*.luc.wtf` → `<tunnel>.cfargotunnel.com` (proxied through Cloudflare)
- Config: `~/.cloudflared/config.yml` (regenerated from `ingress.d/*.json` fragments each call)
- Reload: `expose` restarts the systemd user service `cloudflared` (~1-3s blip; reload isn't supported by cloudflared 2026.x).
