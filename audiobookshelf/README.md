# Audiobookshelf

Copy `.env.example` to a local `.env`, create the state/library directories, and
run `docker compose up -d`. Attach your reverse proxy to the external network
named `traefik-net`, or change that network name for your deployment. Proxy HTTP
to `audiobookshelf:80` with WebSocket support. No host port is published.

Initialize the administrator privately before exposing the route. Use native
Audiobookshelf accounts so web and mobile clients can authenticate and synchronize
progress. Create an audiobook library pointing at `/audiobooks`; this mount is
read-only. Place each completed book in its own subdirectory and prefer embedded
audio tags over folder names in library metadata precedence.

Back up `/config` while the service is stopped, along with `/metadata` and your
library files. Pin/review upgrades and retain the prior image for rollback.
