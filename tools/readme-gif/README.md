# README GIFs

The two GIFs in the project README are **not mockups**. They are produced by
driving the production `format_comment` exactly as the goldens do, so what the
reader sees is what the service actually posts.

Regenerate them after any change to the comment format:

```bash
./tools/readme-gif/build_gifs.sh            # both
./tools/readme-gif/build_gifs.sh monthly    # just the hero
```

Needs `ffmpeg`, `gifsicle`, and `npm install` in this directory (puppeteer).

| File | What it is |
|---|---|
| `fleet_names.py` | The demo environment names, and why they are safe |
| `make_comment.py` | Hero scenario: a monthly customer bump across 25 apps |
| `make_danger.py` | Blocked scenario: a decommission that would orphan a VM |
| `render_html.py` | Renders the comment as a Bitbucket-style PR page |
| `capture.js` | Scrolls that page and captures 92 frames |
| `build_gifs.sh` | Runs the above and encodes the GIFs |

## On the environment names

The demo names follow the production convention (`pv-<customer>-<cell>`) so the
GIF reads like a real fleet, but **every one was screened against the 293
distinct customer short-names in `acme-config-{dev,stage,prod}` and none of them
is a real customer.**

That is deliberate and worth preserving. The README is a shareable artifact, and
the second GIF depicts a near-miss outage — an environment armed for deletion
with its VM config stripped. Neither should name a company we actually host.
`fleet_names.py` is the single place to change if these ever need updating; keep
the screening step when you do.
