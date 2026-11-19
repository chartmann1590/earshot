# Submitting Earshot to Depot

Everything in this repository is ready for review. A person submits, not an agent, so these are the steps
for the publisher (Hartmann Studios).

## What is already done

| Check | Result |
| --- | --- |
| `luma lint` | passed |
| `luma test` (360, 500, 1024, 1280 px × pointer, touch, keyboard) | passed |
| `appstreamcli validate --no-net --pedantic` (AppStream 1.1.3) | passed (one pedantic note: capital letters in the reverse-DNS ID, which is normal) |
| `desktop-file-validate` | passed |
| `flatpak-builder-lint manifest` (real manifest, GitHub URL, pinned commit) | passed |
| Build with `org.flatpak.Builder` 1.4.12 on `org.projectluma.Platform//44`, installed and run in its sandbox | passed |
| Unit tests (feeds, library, OPML, show notes) and UI tests driving the real app in the Luma runtime (real playback through Up next, resume, speed, sleep timer, show-note times, unfollow) | 6 + 6 passed |
| Installed Flatpak run in its own sandbox: live charts and artwork load, MPRIS answers on the session bus | passed |
| `luma release-check` | **fails until real conformance evidence exists**: it wants evidence from Luma's SDK test-image lanes on `x86_64` *and* `aarch64`, desktop and mobile. The tests above ran on both architectures, but not in those lanes, so the evidence file is left honest (all false); never fill it in by hand. |

Permissions the app asks for: showing a window (`--share=ipc`, `--socket=wayland`, `--socket=fallback-x11`,
`--device=dri`), **the internet** (`--share=network`, to fetch feeds and episodes), **audio**
(`--socket=pulseaudio`, the only way a Flatpak plays sound; Luma's list shows it as "Records audio", but Earshot
never opens a microphone) and its own media-controls name (`--own-name=org.mpris.MediaPlayer2.Earshot`, which
Luma counts as nothing extra). Subscriptions files come and go through the file chooser portal; there is no
filesystem access. If review asks about the audio line, that is the answer.

## Packages

Depot takes a package per architecture. Both are built with the official Flatpak builder from the
commit the manifest pins, installed and tested on each architecture:

- `com.charleshartmann.Earshot-1.1.0-x86_64.flatpak`: computers
- `com.charleshartmann.Earshot-1.1.0-aarch64.flatpak`: phones, tablets and ARM computers

They are attached, with `SHA256SUMS`, to the GitHub release
[v1.1.0](https://github.com/chartmann1590/earshot/releases/tag/v1.1.0) and kept in `~/luma-apps/bundles/`. Upload both in Connect.

## Steps

Done: the public repository, the pinned manifest commit (`8b049545…`), both 1.1.0 packages on the GitHub
release, and the live Earshot Plus catalog. There is no Earshot draft in Connect yet; create one only when
Charles asks, and only one:

1. **New app** `com.charleshartmann.Earshot`, name `Earshot`, in the `com.charleshartmann` namespace (the same one as Hartmann Office).
2. **Packages:** `com.charleshartmann.Earshot-1.1.0-x86_64.flatpak` and `com.charleshartmann.Earshot-1.1.0-aarch64.flatpak` (SHA-256 above), version `1.1.0`.
3. **Listing** from `data/com.charleshartmann.Earshot.metainfo.xml` (it says the app is free with an optional Earshot Plus purchase);
   privacy policy link: `PRIVACY.md` in the repository; in-app purchases: yes; sign-in: `none`; TV: supported.
4. **Permissions** Depot will show: uses the internet, plays audio (listed as "Records audio": the only way a
   Flatpak plays sound; no microphone is used), media controls name.
5. **Screenshots:** `screenshots/` (computer, tablet, phone, TV).
6. **Review.** Only Charles sends it for review.

## 1.1.0 release check (2026-11-19)

**Packages** (GitHub release [v1.1.0](https://github.com/chartmann1590/earshot/releases/tag/v1.1.0); built with
`org.flatpak.Builder` from source commit `8b0495456eba80beb8eda5d739edc6ef562a6271`, the commit the manifest pins; runtime
`org.projectluma.Platform//44`):

| Architecture | File | SHA-256 |
| --- | --- | --- |
| x86_64 (computers, TVs) | `com.charleshartmann.Earshot-1.1.0-x86_64.flatpak` | `d1c4d4d4a3aa82957a6ff977d01ea187a52d0614f7a33ae44cc2939175c15e22` |
| aarch64 (phones, tablets, ARM computers) | `com.charleshartmann.Earshot-1.1.0-aarch64.flatpak` | `704ec6175eb770e8d6ec8c8d7fa87cd1ced6303be0a5a264408c916c2947f7cc` |

One package per architecture serves computer, tablet, phone and TV (LumaUI adapts by width, input and posture).
Permissions: network, audio (`--socket=pulseaudio`), display, `org.mpris.MediaPlayer2.Earshot`. The same paid-feature, network and privacy disclosure applies on every device: free app,
optional Earshot Plus through Stripe, email to the license server and Stripe only when buying or restoring.

**What was verified, and where.** None of this ran on a real Luma phone, tablet or TV.

| Check | Where it ran | Result |
| --- | --- | --- |
| UI tests (7), driving the real app | Luma SDK runtime, x86_64 (Proxmox build container) and aarch64 (Docker on Apple Silicon), virtual display | passed |
| Bundle builds, installs, reports version 1.1.0 | same two environments | passed |
| Packaged billing code reads the live catalog | both installed packages, both architectures, live server, read-only | exactly the three products: $1.99 a month; $14.99 a year, first 7 days free; $29.99 once |
| Upgrade panel shows live prices; Buy creates a live Stripe Checkout session (`cs_live_…`); app waits; Cancel returns to the plans; nothing unlocks | x86_64, virtual display, live server; reserved test address, no browser, no card, no payment | passed |
| Restore for an address that never bought | both packages, live server | "no purchase", nothing unlocked |
| Purchase, restore and expiry logic against a server speaking the same `/v2` API | unit tests with a local fake server | passed |
| Computer, tablet, phone and TV layouts | screenshots in the Luma runtime with Luma's device switches (TV posture forced, no Luma shell) | reviewed |
| Real Luma device (phone, tablet, TV), sound, lock-screen controls, portals | not run | **not verified** |
| Completed payment, trial enrollment, cancellation in Stripe's portal, restore of a real purchase | not run (no live charges; no test-mode server exists) | **not verified** |
| `luma release-check` conformance evidence | not run in Luma's SDK test-image lanes | fails as expected; evidence file left honest |

## Billing (Earshot Plus)

The app is free; **Earshot Plus** is an optional purchase through the shared Hartmann Studios license server (the
Cloudflare Worker `livecaptionn-premium`, `/v2` API) and Stripe Checkout, the same system as the studio's direct
Android builds. No Stripe key is in the app.

**For the listing (Luma review 3.6):** the description already says the app is free with an optional purchase,
and names the extras. In Connect, mark the app as offering in-app purchases if the form asks. Prices and trial
lengths are read live from the license server, never hard-coded.

**Sign-in declaration (2.4):** `none`. Buying asks for an email for the receipt and license; it does not create
an account, and nothing in the app needs one.

**Live since 2026-11-19:** the license server's catalog lists `earshot` with the three approved Stripe prices
(registered by the studio's billing maintainer; nothing in this repo creates prices). Public readback of
`/v2/catalog?app=earshot`:

| Product key | Label | Stripe mode | Price |
| --- | --- | --- | --- |
| `earshot_plus:monthly` | Earshot Plus (Monthly) | subscription | $1.99 / month |
| `earshot_plus:yearly` | Earshot Plus (Yearly) | subscription | $14.99 / year, 7-day free trial |
| `earshot_lifetime` | Earshot Plus (Lifetime) | payment | $29.99 once |

The public catalog it serves:

```json
{
  "app": "earshot",
  "name": "Earshot",
  "products": {
    "earshot_plus:monthly": {
      "mode": "subscription",
      "trial_days": 0,
      "implies": [],
      "display": {
        "label": "Earshot Plus (Monthly)",
        "amount": 199,
        "interval": "month",
        "consumable": false
      }
    },
    "earshot_plus:yearly": {
      "mode": "subscription",
      "trial_days": 7,
      "implies": [],
      "display": {
        "label": "Earshot Plus (Yearly)",
        "amount": 1499,
        "interval": "year",
        "consumable": false
      }
    },
    "earshot_lifetime": {
      "mode": "payment",
      "trial_days": 0,
      "implies": [],
      "display": {
        "label": "Earshot Plus (Lifetime)",
        "amount": 2999,
        "interval": null,
        "consumable": false
      }
    }
  }
}
```

Checkout returns to the server's own `/v2/return` page; the app doesn't need a redirect. It reads the Checkout
session id from the checkout address and asks `/v2/entitlement` until Stripe confirms the payment.

## Listing text (also in the metainfo)

- **Name:** Earshot
- **Summary:** Find, follow and listen to podcasts
- **Developer:** Hartmann Studios
- **Category:** Media (Audio, Player)
- **Licence:** GPL-3.0-or-later
- **Content rating:** OARS 1.1, no objectionable content
- **Screenshots:** `screenshots/new-episodes.png`, `now-playing.png`, `discover.png` (computer), `tablet.png`, `phone.png`, `tv.png`
- **Third-party service:** search and charts use Apple's public podcast directory (said in the listing and `PRIVACY.md`)
