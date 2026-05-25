# Self-hosted Docker Apps

This repository contains Docker Compose files for various self-hosted applications. Each application has its own directory with a `docker-compose.yml` file and any necessary configuration files.

This project uses Cloudflare Tunnel and Traefik. With a domain name, you can easily access these applications from anywhere without exposing your home network for free.

## Pre-requisites

- Docker and Docker Compose installed on your server.
- A domain name
- Cloudflare account

## Usage

0. **Preparation**:

    a. Make Docker run in rootless mode to avoid permission issues. Refer to the [Docker documentation](https://docs.docker.com/engine/security/rootless/) for instructions.
    b. Use Cloudflare to host your domain name.

1. **Clone the repository**:

   ```bash
   git clone https://github.com/jin-li/selfhost.git
   ```

2. **Set up reverse proxy**:

   a. Cloudflare Tunnel
   b. Traefik
   
   We use a middleware to add the `X-Forwarded-Proto: https` header to ensure that applications behind Traefik can correctly identify the original request protocol.

