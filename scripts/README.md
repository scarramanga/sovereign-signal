# Mac-side Scripts — sovereign-signal

These scripts run on Andy's Mac to scrape LinkedIn and forward comments to the sovereign-signal pod for processing.

## Prerequisites

```bash
pip3 install playwright httpx
playwright install chromium
```

## LinkedIn Session

The Mac scraper reads cookies from a local file. Export from the pod:

```bash
./scripts/export_session.sh
```

This creates `~/.sovereign-signal/linkedin_session.json` with format:

```json
{
  "cookies": [
    {"name": "li_at", "value": "...", "domain": ".linkedin.com", "path": "/", ...}
  ],
  "user_agent": "Mozilla/5.0 ..."
}
```

To recapture a session, run the Playwright capture script on your Mac, then save the output to this file.

## Setup

### 1. Port-forward (persistent)

Copy the plist and load it:

```bash
cp scripts/com.sovereignsignal.portforward.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sovereignsignal.portforward.plist
```

This keeps `kubectl port-forward` running at `localhost:8080`, auto-restarting if it drops.

### 2. Listener (every 15 minutes)

```bash
cp scripts/com.sovereignsignal.listener.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sovereignsignal.listener.plist
```

Runs `listener_mac.py` every 900 seconds (15 minutes). Also runs once immediately on load.

## Logs

```bash
# Listener output
tail -f ~/Library/Logs/sovereign-signal-listener.log

# Listener errors
tail -f ~/Library/Logs/sovereign-signal-listener-error.log

# Port-forward output
tail -f ~/Library/Logs/sovereign-signal-portforward.log
```

## Manual run

```bash
python3 scripts/listener_mac.py
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SS_API_URL` | `http://localhost:8080` | Pod API base URL (via port-forward) |
| `SS_LINKEDIN_PROFILE` | `https://www.linkedin.com/in/andy-boss-b89856/recent-activity/all/` | LinkedIn activity feed URL |

## Unloading

```bash
launchctl unload ~/Library/LaunchAgents/com.sovereignsignal.listener.plist
launchctl unload ~/Library/LaunchAgents/com.sovereignsignal.portforward.plist
```
