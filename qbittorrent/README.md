# qBittorrent

This Compose stack runs qBittorrent with host networking so peer traffic can
use the host's native IPv6 connectivity. The Web UI and peer port are separate
listeners. Host networking does not use Docker port publishing or a bridge
network; bind the Web UI to a private host address and restrict it with the host
firewall before adding a reverse proxy route.

Copy `.env.example` to `.env`, set the bind-mount paths and ports, create the
directories, and run `docker compose up -d`. The image prints a temporary Web UI
password to its logs on first start. Set a persistent password immediately and
keep Web UI authentication, Host-header validation, and CSRF protection enabled.

To use IPv6 for peer traffic, select the host's Internet interface and all IPv6
addresses in qBittorrent's Advanced settings. Open the chosen peer TCP/UDP port
in the host and upstream IPv6 firewalls for inbound seeding. Verify tracker and
peer connections before relying on IPv6-only operation; host networking alone
does not enforce that policy.
