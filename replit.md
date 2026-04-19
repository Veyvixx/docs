# Nana Bot Documentation

## Overview

Documentation site for **Nana** — a Discord bot built with a soft aesthetic and powerful capabilities. The docs are built with Mintlify and cover all public-facing features of the bot.

## Project Structure

- `docs.json` — Core configuration (navigation, colors `#A38F8F`/`#726060`, logo, links)
- `index.mdx` — Introduction / landing page with feature card grid
- `quickstart.mdx` — Getting started guide
- `features/` — Feature documentation pages
  - `moderation.mdx` — Moderation commands (ban, kick, mute, jail, warn, purge, automod)
  - `antinuke.mdx` — Anti-nuke protection system
  - `autoresponders.mdx` — Trigger-based autoresponder system with flags
  - `buttons.mdx` — Custom link and functional buttons
  - `customization.mdx` — Greet/leave/boost, embed builder, autorole, palette, emojis
  - `event-logging.mdx` — Server event logging
- `premium/` — Premium tier information
  - `overview.mdx` — Tiers, features comparison, and pricing
  - `activation.mdx` — How to activate and switch premium server slots
- `reference/` — Technical reference
  - `commands.mdx` — Full command reference by category
  - `variables.mdx` — Template variable reference

## Color Palette

The docs use Nana's rose/blush color palette:
- Primary: `#A38F8F`
- Light: `#F7EFEF`
- Dark: `#726060`

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

## External Links

- Support Server: https://discord.gg/BQcm3ptaxX
- Patreon: https://patreon.com/NanaBotDis
