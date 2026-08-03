# Screenshots

The README references the primary PNG files in this directory. They are captured from a local lab
using only service URLs and demo interfaces.

Keeping the list here rather than in the README means the capture instructions live next to the files
they produce, and the README stays about the project.

## What to capture

| File | Where | Should show |
| --- | --- | --- |
| `landing-page.png` | <https://lab.localhost> | The full service index with status dots settled green |
| `grafana-overview.png` | Grafana → Lab Overview | Populated panels — leave the lab running an hour first, an empty graph sells nothing |
| `keycloak-realm.png` | Keycloak administrator console | The administrator sign-in page |
| `open-webui.png` | <https://chat.lab.localhost> | The local first-run sign-in page |
| `traefik-dashboard.png` | <https://traefik.lab.localhost> | The Routers view, showing every discovered service |
| `vault-policies.png` | Vault → Policies | The four ACL policies (capture after authenticating with a local development token) |
| `first-boot.gif` | Terminal | `make up` through to `make health` reporting healthy (optional recording, not shown in the README) |

## Guidelines

**Resolution.** Capture at 2560×1440 or 1920×1080 and let GitHub scale. A screenshot taken at 1280 wide
looks soft on a modern display.

**Theme.** Dark mode throughout, for consistency and because it matches the landing page.

**Content.** Wait for real data. A dashboard with an hour of history is persuasive; one taken 30 seconds
after boot is a picture of empty axes.

**Redact before committing.** Screenshots are the easiest way to leak a credential into a public
repository, and they are not searchable afterwards. Check for:

- Anything on screen after `make creds`
- Vault tokens, visible in the UI header after login
- Session cookies or tokens in an open devtools panel
- Real hostnames, internal IP ranges, or your own email address
- Browser tabs, bookmarks and notifications from the rest of your life

**File size.** Compress before committing — [Squoosh](https://squoosh.app/) or `pngquant`. Aim for under
500 KB per PNG. Nobody enjoys cloning a 40 MB repository of screenshots.

## Recording the GIF

[`vhs`](https://github.com/charmbracelet/vhs) produces reproducible terminal recordings from a script,
which is better than a screen recorder because it can be re-run when the output changes:

```bash
vhs < screenshots/first-boot.tape
```

A starting point:

```tape
Output screenshots/first-boot.gif
Set FontSize 15
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"

Type "make up"     Enter    Sleep 45s
Type "make health" Enter    Sleep 12s
```

Alternatives: [asciinema](https://asciinema.org/) with `agg` to convert, or
[terminalizer](https://github.com/faressoft/terminalizer).

Keep it under 15 seconds of playback and under 5 MB. Cut the dead time during image pulls — nobody needs
to watch a progress bar in real time.
