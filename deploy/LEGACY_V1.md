# Legacy V1 setup (for reference)

How the **old** Saint Antoine script was configured on the Pi, and how it was
retired when V2 was deployed. Kept here so nobody re-enables it by accident or
wonders where it went.

## How V1 launched at boot

V1 was **not** a systemd service. It was started by an `@reboot` entry in the
`pi` user's crontab:

```
@reboot <command launching the old script>
```

Consequences of that setup (and why V2 moved to systemd):

- No supervision: if the script crashed, nothing restarted it until the next
  reboot.
- No logs: output went nowhere useful (no `journalctl`).
- No clean stop/start: the only controls were `kill` and a reboot.

## How it was disabled (June 2026)

The `@reboot` line was commented out rather than deleted, so the original
command is still visible in the crontab as a record:

```bash
crontab -e        # the @reboot line is prefixed with '#'
```

Verified after a reboot: no V1 process running (`ps aux` shows only stock
Raspberry Pi OS processes, e.g. `wayvnc-control.py`, which belongs to the
built-in VNC service — leave it alone).

## Where boot-time launches live on this Pi (checked)

If something ever needs to launch at boot again, these are the places that
were audited when retiring V1:

| Mechanism | Location | V1 used it? |
|---|---|---|
| systemd units | `systemctl list-unit-files --state=enabled` | no — all stock services |
| user crontab | `crontab -l` (user `pi`) | **yes — `@reboot` line, now commented out** |
| root crontab | `sudo crontab -l` | no |
| rc.local | `/etc/rc.local` | no |
| desktop autostart | `~/.config/autostart/`, lxsession/wayfire/labwc configs | no |

## V2 replacement

V2 runs as the `saintantoine` systemd unit
([saintantoine.service](saintantoine.service)) — supervised, auto-restarting,
logged via `journalctl -u saintantoine`. Deployment steps are in the
[README](../README.md#raspberry-pi-deployment).
