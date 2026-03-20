# SDRTrunk Setup Guide

How to configure SDRTrunk to feed calls into sdrtrunk-pg.

---

## Overview

SDRTrunk connects to sdrtrunk-pg via its **Broadcastify Calls** broadcaster. Every time a call
completes on a monitored talkgroup, SDRTrunk POSTs the metadata and MP3 audio to your
sdrtrunk-pg server.

The wiring has two parts:
1. **Broadcaster** — tells SDRTrunk where to send calls (your server URL and API key)
2. **Alias broadcast channels** — tells SDRTrunk *which* talkgroups to send

Both must be configured or calls will either not arrive at all, or arrive with no talkgroup context.

---

## Step 1: Configure the Broadcaster

In SDRTrunk: **View → Preferences → Streaming**

Click **+** to add a new streaming configuration and select **Broadcastify Calls**.

| Field | Value |
|---|---|
| **Name** | `sdrtrunk-pg` (this name links to aliases — must match exactly) |
| **Host** | `http://your-server-ip:5010/api/call` |
| **API Key** | the `API_KEY` value from your sdrtrunk-pg `.env` |
| **System ID** | your P25 system identifier (e.g. `VA-HR-P25`) — optional but useful |
| **Enabled** | ✓ |

Click **Save**.

> **Local server:** If SDRTrunk runs on the same machine as sdrtrunk-pg, use `http://127.0.0.1:5010/api/call`.

Test the connection with the **Test** button. SDRTrunk will send a test POST — you should see a log entry in `/tmp/sdrtrunk-flask.log`.

---

## Step 2: Wire Talkgroups to the Broadcaster

This is the part people miss. SDRTrunk will only forward calls for talkgroups that have the
broadcaster explicitly added as a **broadcast channel** in their alias.

### Option A: Use the import script (recommended)

If you've already imported your talkgroups into the sdrtrunk-pg database, the script can
wire all of them at once by modifying the playlist XML:

```bash
python scripts/import_from_playlist.py /path/to/SDRTrunk/playlist/default.xml \
    --broadcast-channel sdrtrunk-pg
```

> ⚠️ **Do this while SDRTrunk is stopped.** Editing the playlist XML while SDRTrunk
> is actively decoding will freeze it. Start SDRTrunk again after the script completes.

### Option B: Manual via the SDRTrunk UI

For each talkgroup alias you want to capture:

1. Open **View → Aliases**
2. Find the alias for the talkgroup
3. Click the alias to edit it
4. Under **Streaming**, click **+** and select `sdrtrunk-pg`
5. Save

This is tedious for large alias lists — use Option A.

### What the XML looks like

A correctly wired alias in `default.xml`:

```xml
<alias group="Police" color="0" name="NPD 1st Main" list="NFK2" iconName="Police">
    <id type="talkgroup" value="608" protocol="APCO25"/>
    <id type="broadcastChannel" channel="sdrtrunk-pg"/>
</alias>
```

The `channel="sdrtrunk-pg"` must exactly match the **Name** you gave the broadcaster in Step 1.

---

## Step 3: Configure the P25 Channel Decoder

SDRTrunk needs a channel configuration pointing at your P25 control channel.

**View → Channels → +**

| Field | Value |
|---|---|
| **System** | `APCO-25` |
| **Protocol** | `P25 Phase 1` (or Phase 2 if your system uses it) |
| **Frequency** | Your system's control channel frequency (e.g. `857.5125 MHz` for Norfolk) |
| **Alias List** | The alias list containing your wired talkgroups |
| **Auto-start** | ✓ |

For trunked P25 systems, SDRTrunk will decode the control channel and automatically follow
traffic channels as calls are granted.

---

## Step 4: Tune for Performance

### Traffic channel limit

Set the maximum number of simultaneous traffic channels to match your hardware:

**Preferences → P25 → Max Traffic Channels**

With 4 RTL-SDR dongles at 2.4 MHz sample rate (96 × 25kHz channels each), you can handle
well over 100 simultaneous channels in practice.

### Audio recording

Make sure **Record Audio** is enabled in the channel configuration so calls are saved locally
by SDRTrunk before being forwarded. sdrtrunk-pg stores its own MP3 copy from the PUT upload,
but local SDRTrunk recording is a useful backup.

---

## Step 5: Verify It's Working

Start sdrtrunk-pg first, then SDRTrunk.

```bash
# Watch the flask ingest log
tail -f /tmp/sdrtrunk-flask.log

# Should see lines like:
# 2026-03-20T09:32:12 INFO [app.ingest] Registered call id=1 tg=608 duration=4.2s — awaiting audio upload
# 2026-03-20T09:32:13 INFO [app.ingest] Saved audio call_id=1 tg=608 size=6720 path=...
```

After a minute or two:
```bash
# Check that calls are flowing
psql sdrtrunk -c "SELECT count(*), max(received_at) FROM calls;"

# Check that workers are processing them
tail -f /tmp/sdrtrunk-workers.log
```

Open `http://localhost:5010/map` — calls should start appearing in the live feed within seconds.

---

## Common Issues

### No calls arriving
- Check that the broadcaster name in SDRTrunk exactly matches the `channel=` attribute in your aliases
- Check that sdrtrunk-pg is running and accessible: `curl http://localhost:5010/health`
- Check the SDRTrunk app log for connection errors: `~/SDRTrunk/logs/sdrtrunk_app.log`

### Calls arrive but no talkgroup name
- The talkgroup isn't in the `talkgroups` table. Run the import script:
  ```bash
  python scripts/import_talkgroups.py your-system.csv --system-id YOUR-SYSTEM-ID
  ```
  Or import directly from the playlist:
  ```bash
  python scripts/import_from_playlist.py ~/SDRTrunk/playlist/default.xml
  ```

### SDRTrunk froze after editing the playlist
- Hard stop SDRTrunk, restore the playlist backup (`default.xml.backup`), restart
- Always edit the playlist while SDRTrunk is stopped

### Test button shows "Connection Failed"
- Make sure sdrtrunk-pg is running
- Check firewall: `sudo firewall-cmd --list-ports` — port 5010 needs to be accessible
- If SDRTrunk and sdrtrunk-pg are on different machines, use the server's actual IP, not `localhost`

### Calls have audio but no transcript
- Workers aren't running: `ps aux | grep run_workers`
- Whisper failed to load (usually a torch/CUDA issue): `tail -50 /tmp/sdrtrunk-workers.log`
- Call duration too short: calls under ~1 second are skipped

---

## Talkgroup CSV Format

RadioReference exports talkgroup CSVs with headers like:

```
Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category
608,260,NPD 1st Main,D,NPD 1st Main Channel,Police,Police
```

The import script handles this format directly. Download from RadioReference → your system → Export.

---

## SDRTrunk Broadcastify Calls Protocol

For reference, the two-step protocol:

**Step 1 — POST `/api/call`** with multipart form data:
```
apiKey      = your API key
systemId    = system identifier
callDuration = seconds (float)
ts           = Unix timestamp
tg           = talkgroup decimal
src          = source radio ID
freq         = frequency in MHz
enc          = encoding type ("mp3")
lat          = GPS latitude (only if LRRP enabled — see patches/)
lon          = GPS longitude (only if LRRP enabled)
```

Response must be exactly: `0 http://your-server/api/call/upload/{id}`

**Step 2 — PUT `/api/call/upload/{id}`** with the raw MP3 binary in the request body.

If step 1 returns anything other than `"0 <url>"`, SDRTrunk silently drops the call.
