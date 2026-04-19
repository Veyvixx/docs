# Mintlify Starter Kit

## Overview

A documentation website built with [Mintlify](https://mintlify.com/). This is the Mint Starter Kit template for creating and deploying documentation using the Mintlify platform.

## Project Structure

- `docs.json` — Core configuration file (navigation, theme, colors, logo)
- `index.mdx` — Home/landing page
- `quickstart.mdx` — Quickstart guide
- `development.mdx` — Local development instructions
- `essentials/` — Content guides (markdown, code blocks, images, snippets, navigation, settings)
- `api-reference/` — API documentation pages and OpenAPI spec
- `ai-tools/` — Guides for AI coding tools (Cursor, Claude Code, Windsurf)
- `snippets/` — Reusable MDX components
- `images/` — Static images
- `logo/` — Light and dark mode SVG logos

## Tech Stack

- **Platform**: Mintlify
- **Content Format**: MDX (Markdown + JSX)
- **CLI**: `mint` (Mintlify CLI, installed globally via npm)
- **Runtime**: Node.js 20

## Running Locally

The app runs via the "Start application" workflow using:

```bash
mint dev --port 5000
```

The local preview is available at port 5000.

## Deployment

Configured as an autoscale deployment running `mint dev --port 5000`.
