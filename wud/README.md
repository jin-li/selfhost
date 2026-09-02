# wud

## Introduction

What's Up Docker (wud) is a simple and lightweight web-based dashboard to watch the version of your docker containers. It provides a quick overview of the current version of each container and allows you to easily check for updates.

## Usage

0. Create `.env` file based on the provided template.
1. In `.env`, set the admin username and an Apache APR1 password hash for
   authentication. WUD's `pass` module does not accept bcrypt hashes. Generate
   a compatible value with `htpasswd -nb USER PASSWORD` and use only the hash
   after the first colon. With Compose interpolation, escape literal dollar
   signs as `$$`; `containerctl`-rendered `compose.env` files retain the raw
   single-dollar hash.
2. Run `docker compose up -d` to deploy the container.
4. The version watch is through adding labels to your docker containers. In the `docker-compose.yml` file of your containers, add the following labels to enable version watch:

    ```yaml
        labels:
        - wud.tag.include=^\d+\.\d+\.\d+$$
        - wud.tag.transform=^(\d+\.\d+)\.\d+$$ => $$1
    ```
