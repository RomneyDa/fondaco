# Notifications

Notifications are configured in `settings.json` at the project root. Each medium (desktop, ntfy) has its own section with an `enabled` flag and a list of event types to subscribe to.

## Event types

Events are hierarchical — subscribing to a parent catches all children.

| Event             | Description                            |
|-------------------|----------------------------------------|
| `trades`          | All trades (buys + sells)              |
| `trades:buys`     | Buy executions only                    |
| `trades:sells`    | Sell executions only                   |
| `session:end`     | Any session resolution (wins + losses) |
| `session:end:win` | Sessions with positive PnL             |
| `session:end:loss`| Sessions with negative PnL             |
| `error`           | Trade failures, process errors         |
| `process:start`   | Collector/worker started               |
| `process:stop`    | Collector/worker stopped               |
| `clob_down`       | CLOB flat line detected (midpoints stale) |
| `report:hourly`   | Periodic summary (coming soon)         |

### Sports

| Event                   | Description                                          |
|-------------------------|------------------------------------------------------|
| `sports`                | Everything sports-related                            |
| `sports:game`           | Game start + end                                     |
| `sports:game:start`     | Started tracking a game                              |
| `sports:game:end`       | Game resolved                                        |
| `sports:score`          | Score change on a tracked game                       |
| `sports:stale_book`     | Book has not repriced after a score — the layer-0 signal |
| `sports:review_pending` | Play or goal under official review; trading suspended |
| `risk`                  | All risk events                                      |
| `risk:exposure_limit`   | Allocator refused or throttled a position            |

`sports:stale_book` and `risk:exposure_limit` are sent at **high** priority.
The first is a fleeting opportunity worth interrupting for; the second means the
portfolio is at a limit and something you expected to happen did not.

## Desktop

Enabled by default. Uses native macOS Notification Center via `notify-rust`. No setup required.

If notifications aren't appearing, check **System Settings > Notifications** and make sure your terminal app (Terminal, iTerm, etc.) is allowed to send notifications.

## ntfy (mobile push, email, webhooks)

[ntfy](https://ntfy.sh) is a free pub/sub notification service. One integration gets you mobile push (iOS + Android), email forwarding, and webhooks — no API keys required.

### Setup

1. Pick a topic name (use something random for privacy, e.g. `fondaco-a8f2k9x`)
2. Install the [ntfy app](https://ntfy.sh) on your phone and subscribe to your topic
3. Enable in `settings.json`:

```json
{
  "notifications": {
    "ntfy": {
      "enabled": true,
      "topic": "fondaco-a8f2k9x",
      "server": "https://ntfy.sh",
      "events": ["session:end", "error", "process:stop"]
    }
  }
}
```

That's it. You'll get push notifications on your phone.

### Priority

Errors are sent as `urgent` (breaks Do Not Disturb), losses as `high`, everything else as `default`. Override per-event:

```json
{
  "ntfy": {
    "enabled": true,
    "topic": "fondaco-a8f2k9x",
    "events": ["trades", "session:end", "error"],
    "priority": {
      "trades": "low",
      "error": "urgent"
    }
  }
}
```

ntfy priority levels: `min`, `low`, `default`, `high`, `urgent`.

### Email forwarding

Add an email subscriber to your topic in the ntfy web UI or app — no SMTP config needed on your end.

### Self-hosted

Run your own ntfy server and point `server` to it:

```json
{
  "ntfy": {
    "server": "https://ntfy.yourserver.com",
    "topic": "trading"
  }
}
```

## Full example

```json
{
  "notifications": {
    "desktop": {
      "enabled": true,
      "events": ["trades", "session:end", "error", "process:start", "process:stop"]
    },
    "ntfy": {
      "enabled": true,
      "topic": "fondaco-a8f2k9x",
      "server": "https://ntfy.sh",
      "events": ["session:end:loss", "error", "process:stop"],
      "priority": {}
    }
  }
}
```

Desktop notifications for everything, phone push only for losses, errors, and process stops.
