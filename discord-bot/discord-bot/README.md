# Discord Bot

A highly optimized, aesthetic Discord bot built with Python & discord.py 2.x.

## Features

- **Miscellaneous**: `/ping`, `/avatar`, `/banner`, `/userinfo`, `/serverinfo`, `/roleinfo`, `/help`
- **Moderation**: `/ban`, `/unban`, `/kick`, `/timeout`, `/untimeout`, `/warn`, `/warnings`, `/clearwarnings`, `/purge`
- **Anti-Nuke**: Mass-action detection with automatic punishment + whitelist system
- **Customization**: Welcome messages, auto-role, mod-log, custom embed builder, `/settings`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure the bot

Edit `config.json`:

```json
{
  "token": "YOUR_BOT_TOKEN_HERE"
}
```

Or set the environment variable (recommended):

```bash
export DISCORD_TOKEN="your-token-here"
```

### 3. Run the bot

```bash
python bot.py
```

## File Structure

```
discord-bot/
├── bot.py               # Main entry point
├── config.json          # Bot configuration
├── requirements.txt     # Python dependencies
├── cogs/
│   ├── misc.py          # Miscellaneous commands
│   ├── moderation.py    # Moderation commands
│   ├── antinuke.py      # Anti-nuke protection
│   └── customization.py # Welcome, auto-role, settings
└── utils/
    ├── database.py      # SQLite + async DB + cache layer
    ├── embeds.py        # Centralised embed factory
    └── helpers.py       # Shared utility functions
```

## Configuration

| Key | Description |
|-----|-------------|
| `token` | Bot token from Discord Developer Portal |
| `developer_ids` | List of user IDs with developer access |
| `colors` | Color palette (hex integers) |
| `emojis` | Custom emoji strings or unicode |
| `antinuke.thresholds` | Actions per window before punishment |
| `antinuke.window_seconds` | Rolling detection window |
| `antinuke.punishment` | `ban` or `kick` |

## Bot Permissions Required

- Read Messages / View Channels
- Send Messages
- Embed Links
- Manage Messages (purge)
- Kick Members
- Ban Members
- Moderate Members (timeout)
- Manage Roles (auto-role)
- View Audit Log (anti-nuke detection)

## Slash Command Sync

By default, commands sync globally on startup (may take up to 1 hour to propagate).
For instant updates during development, replace the sync line in `bot.py`:

```python
# Development (instant, guild-only):
await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))

# Production (global, up to 1 hour):
await self.tree.sync()
```
