# cloudflared

## Introduction

cloudflared is a command-line tool that allows you to run a Cloudflare tunnel locally, enabling you to expose your local development environment to the internet through Cloudflare's network.

## Usage

0. Create a Tunnel in the Cloudflare dashboard and download the credentials file.
1. Create `.env` and `config.yaml` files based on the provided templates.
2. In `.env`, set the tunnel name and point the credential and certificate paths to the downloaded credentials file.
3. In `config.yaml`, set the tunnel name and configure the desired routes and services.
4. Run `docker compose up -d` to deploy the container.

## Note

- After adding a new route or service in the `config.yaml`, you need to restart the cloudflared container for the changes to take effect.