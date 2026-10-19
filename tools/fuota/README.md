# FUOTA via ChirpStack

`fuotacs.py` sends a firmware update to a single Basic MAC device over
LoRaWAN, using the device queue of a ChirpStack v4 network server. It talks
to the device's `frag` (port 201) and `fwman` (port 203) services.

This is unicast only: fragments are ordinary class A downlinks. The device
answers every frag/fwman request with an uplink, and each uplink opens the
receive window for the next queued downlink. So after the first uplink the
whole queue drains by itself.

## Requirements

Device firmware:

- `frag` and `fwman` services enabled, and an update signing key configured
  (`SVC_FWMAN_PUBKEY`, `SVC_FWMAN_CURVE`; `SVC_FWMAN_TEST` selects the test
  key that matches `services/fuota/testkey.pem`).

Host:

- Python 3 with `chirpstack-api` and `grpcio`, plus the dependencies of
  `basicloader/tools/fwtool/zfwtool.py`.
- A ChirpStack API key. Watching the device's answers uses ChirpStack's event
  stream (`InternalService.StreamDeviceEvents`).

## Connection settings

Pass them as options or set environment variables:

| Option | Environment | Example |
|---|---|---|
| `--server` | `CHIRPSTACK_SERVER` | `192.168.1.100:8080` (gRPC; add `--tls` for TLS) |
| `--token` | `CHIRPSTACK_API_TOKEN` | API key |
| `--dev-eui` | `DEV_EUI` | `ffffffaaac56dff8` |

## Making an update file

Updates are built with `zfwtool.py` from the `.zfw` files the project build
produces. A delta update is much smaller, but it applies only on top of the
exact firmware it was made from. Keep the `.zfw` of every build you deploy.

```sh
ZFW=basicloader/tools/fwtool/zfwtool.py
KEY=services/fuota/testkey.pem

# full (compressed) update
$ZFW mkupdate -s $KEY new.zfw update.up

# delta update against the firmware the device runs now
$ZFW mkupdate -d current.zfw -s $KEY new.zfw update.up
```

## Sending an update

```sh
fuotacs.py check --expect-zfw current.zfw   # device runs the delta reference?
fuotacs.py send --reboot update.up          # transfer, verify, install
fuotacs.py cleanup                          # after rejoin: new CRC, free session
```

Nothing is delivered until the device sends an uplink, so trigger one if the
device is idle.

1. `check` asks for the firmware CRC. Before sending a delta, confirm the
   device reports the CRC of the reference `.zfw`.
2. `send` queues the session setup, the fragments (with spares), a progress
   request and the image check. The device should answer
   `UpgradeImage VALID new_crc=...`.

   With `--reboot`, a reboot follows the image check. The device registers the
   update only if the image is valid; otherwise it just reboots into the
   current firmware. On reboot the bootloader installs the update, and the
   device rejoins.
3. `cleanup` asks for the CRC again and deletes the finished fragmentation
   session, so the storage is free for the next update.

Without `--reboot`, run `fuotacs.py reboot` after a valid image check.

## Fragment size

`--csz` sets the fragment size in bytes. It must be a multiple of 4, and the
default is 40. Each downlink carries `csz + 3` bytes, which must fit the
maximum payload at the device's data rate. For EU868 that's 51 bytes at
DR0–2, 115 at DR3 and 222 at DR4–7.

Larger fragments mean fewer downlinks: a 1 KB delta at `--csz 200` is 6
fragments. While watching, the script warns if an uplink's data rate is too
low for the chosen size.

Run `fuotacs.py --csz 200 plan update.up` to see the fragment count offline.
The plan step also reassembles the fragments with the device's algorithm as a
self-test.

## Commands

| Command | What it does |
|---|---|
| `plan UP` | Show the fragmentation plan (offline). `-v` lists every payload. |
| `check` | Query the firmware CRC (`--expect-zfw` prints the expected one). |
| `send UP` | Queue the transfer and image check. `--reboot` adds a reboot; `--flush` / `--append` handle a non-empty queue; `--extra N` sets the number of spare fragments. |
| `reboot` | Queue a reboot. A valid update gets installed. |
| `cleanup` | Query the firmware CRC and delete the fragmentation session. |
| `watch` | Print the device's uplinks, decoded. |
| `queue` | Show the device queue length; `--flush` empties it. |
| `decode PORT PAYLOAD` | Decode a frag/fwman uplink payload (hex or base64). |

## Troubleshooting

| Symptom | Cause |
|---|---|
| Nothing happens after `send` | The device hasn't sent an uplink yet. Trigger one. |
| Transfer stops halfway | A downlink or uplink was lost, so the chain broke. Trigger another uplink; the rest of the queue continues. |
| `SessionSetup ... ERROR INDEX` | The session index isn't available on the device, or a session already exists (`send` deletes it first). |
| `UpgradeImage NONE` | No complete image is stored. The transfer was incomplete, or an earlier image check crashed partway through. |
| `UpgradeImage INVALID` | Wrong signing key, a corrupt image, or a delta built against different firmware. |
| Device crashes (LED blinks, then it resets) | The bootloader blinks out a crash code before resetting: a long flash, then the type, reason and address as hex digits, lowest digit first, with digit *n* shown as *n*+1 blinks. |
