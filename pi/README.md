# Offline Wi-Fi: the Pi as its own hotspot

At home the Pi joins home Wi-Fi as usual. Anywhere else (a gig, no internet), it starts its own
Wi-Fi network, `fxbox`, so the phone can join it and open the web app. Nothing here touches audio.

| | home Wi-Fi | hotspot |
|---|---|---|
| Phone joins | home network | Wi-Fi **fxbox** (WPA2, your password) |
| Web app | `http://fxbox.local:8080` | `http://fxbox.local:8080` or `http://10.42.0.1:8080` |
| SSH from the Mac | `ssh ian@fxbox.local` | join `fxbox` on the Mac first (the Mac then has no internet) |

The phone will say the `fxbox` network has no internet; that's expected. iOS stays joined. If a
page won't load, turn off mobile data for a moment so the phone doesn't try the cellular route.

## How it decides

- **At boot**, `fxbox-net-fallback.service` waits up to 40 s for NetworkManager to join home
  Wi-Fi (20 s more if a handshake is still in progress). If it hasn't, it starts the hotspot.
- **No automatic retry afterwards.** The Pi's Wi-Fi chip is either an access point or a client,
  not both, so looking for home Wi-Fi means switching the hotspot off for ~30 s, which would
  knock the phone off mid-gig. In practice the Pi boots where it's used: at home, home Wi-Fi is
  there at boot; at a gig, it stays a hotspot. If it ever comes up as a hotspot at home (router
  was down), run `sudo fxbox-net home` or reboot.
- **Manual switch**, any time:

```
fxbox-net status          # mode, address, hotspot clients (no sudo needed)
sudo fxbox-net hotspot    # start the hotspot (drops home Wi-Fi and any SSH over it)
sudo fxbox-net home       # rejoin home Wi-Fi; if that fails, it goes back to the hotspot
sudo fxbox-net trial 300  # self-test, below
```

`fxbox-net` is a plain command so the Pisound button can call it later. Logs:
`journalctl -t fxbox-net -u fxbox-net-fallback -u fxbox-net-trial`.

## Install / update

From the Mac, copy just this directory (not the whole repo):

```
ssh ian@fxbox.local mkdir -p fxbox-setup && scp pi/* ian@fxbox.local:fxbox-setup/
ssh -t ian@fxbox.local 'sudo bash ~/fxbox-setup/setup-hotspot.sh'
```

It asks for a hotspot password once (8-63 characters) and keeps it only on the Pi, in the
root-only NetworkManager keyfile `/etc/NetworkManager/system-connections/fxbox-hotspot.nmconnection`.
It is never in the repo. Re-running keeps the password; `--new-password` changes it;
`--uninstall` removes everything. Running the setup doesn't switch networks, so it's safe over SSH.

What it installs:

- **`fxbox-hotspot`** NetworkManager profile: SSID `fxbox`, WPA2-PSK with AES only, 2.4 GHz
  channel 6 (every phone supports it; 5 GHz AP mode on the Pi's chip is pickier), protected
  management frames off (some phones fail to join the Pi's AP with them), shared IPv4 on
  `10.42.0.1/24` (NetworkManager runs DHCP + DNS for clients), no IPv6, not autoconnect
  (the fallback service decides). `fxbox.local` works on the hotspot through avahi (mDNS).
- **`/usr/local/bin/fxbox-net`**, the switch above.
- **`fxbox-net-fallback.service`**, enabled for boot; niced, idle I/O, and boot doesn't wait on it.
- **`/etc/default/fxbox-net`**: `BOOT_WAIT`, `HOME_WAIT`, and `HOME_CONNECTIONS` (which
  profiles count as home; empty = every client Wi-Fi profile). Kept if it already exists.

The home Wi-Fi profile (`netplan-wlan0-…`, created by cloud-init) is left alone.

## Test it

`sudo fxbox-net trial 300` runs the same fallback the boot uses, as its own systemd unit so it
survives the SSH connection dropping: it disconnects home Wi-Fi, waits `BOOT_WAIT` seconds, starts
the hotspot, holds it for 300 s (logging the client count every 15 s), then rejoins home. During
the hold, join `fxbox` on the phone and open the web app. Afterwards:

```
journalctl -u fxbox-net-trial
```

If anything goes wrong and the Pi stays a hotspot, join `fxbox` from the Mac and
`ssh ian@10.42.0.1`, or power-cycle it with home Wi-Fi in range.

The web app has to be running for the phone test (it isn't a service yet; see the main README).
