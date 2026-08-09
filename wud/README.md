# wud

## Introduction

What's Up Docker (wud) is a simple and lightweight web-based dashboard to watch the version of your docker containers. It provides a quick overview of the current version of each container and allows you to easily check for updates.

## Usage

0. Create `.env` file based on the provided template.
1. In `.env`, set the Admin username and password for authentication.
2. Run `docker compose up -d` to deploy the container.
4. The version watch is through adding labels to your docker containers. In the `docker-compose.yml` file of your containers, add the following labels to enable version watch:

    ```yaml
        labels:
        - wud.tag.include=^\d+\.\d+\.\d+$$
        - wud.tag.transform=^(\d+\.\d+)\.\d+$$ => $$1
    ```