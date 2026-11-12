# Screenshot guide

Add two real PNG captures from a local PULSEBOT session:

| File | Suggested content |
| --- | --- |
| `telegram-bot.png` | `/start` or `/help`, followed by `/status` in a private bot chat |
| `monitoring-history.png` | `/history <id>` or `/check <id>`, showing real status, latency, and timestamps |

Use a public demonstration URL such as `https://example.com`. Keep screenshots
readable and show only enough conversation to demonstrate the feature. Do not
fabricate responses, monitoring results, or availability metrics.

Before saving, crop or permanently cover Telegram usernames, names, avatars, chat
IDs, bot tokens, private URLs, and any sensitive path or query parameters. Avoid
capturing `.env`, BotFather token messages, or terminal output containing secrets.
Use opaque redaction rather than blur, export a flattened PNG, remove identifying
metadata, and inspect the final file before sharing. If a token was exposed,
revoke and replace it through BotFather; redaction does not invalidate a credential.

The main README currently displays these paths as inline code to avoid broken
image embeds. After adding and reviewing each real image, replace its placeholder
with the corresponding Markdown image link (relative to the project root README):

```markdown
![Telegram bot commands and status](docs/screenshots/telegram-bot.png)
![Monitoring history or manual website check](docs/screenshots/monitoring-history.png)
```

Enable an embed only when that image exists. Keep any remaining missing image as
an inline-code path. Do not add synthetic screenshots or empty placeholder images.
