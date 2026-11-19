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
| `luma release-check` | **fails, as Luma says it will for every app today**: it needs real evidence on x86_64 *and* aarch64, and Luma isn't built for aarch64 yet. The evidence file is left honest (all false); never fill it in by hand. |

Permissions the app asks for: showing a window (`--share=ipc`, `--socket=wayland`, `--socket=fallback-x11`,
`--device=dri`), **the internet** (`--share=network`, to fetch feeds and episodes), **audio**
(`--socket=pulseaudio`, the only way a Flatpak plays sound; Luma's list shows it as "Records audio", but Earshot
never opens a microphone) and its own media-controls name (`--own-name=org.mpris.MediaPlayer2.Earshot`, which
Luma counts as nothing extra). Subscriptions files come and go through the file chooser portal; there is no
filesystem access. If review asks about the audio line, that is the answer.

## Steps

1. **Publish the source.** Create the public repository `github.com/chartmann1590/earshot` and push
   `main` (the metainfo's links and screenshot URLs point there).
2. **Pin the commit.** In `flatpak/com.charleshartmann.Earshot.json`, replace `COMMIT_PLACEHOLDER` with the full
   40-character commit you want Luma to build (`git rev-parse HEAD`), commit that, and push. Luma rejects a git
   source without a full commit.
3. **Join the developer program** at https://hub.simplyluma.com/connect/developer.
4. **Prove the namespace.** The app ID is `com.charleshartmann.Earshot`, so you prove you control
   `charleshartmann.com` (Connect shows how). If you'd rather prove a GitHub account instead, the ID would
   become `io.github.chartmann1590.Earshot` in every file; choose before the first submission,
   because an app ID is permanent.
5. **Submit the release** in Connect with the name `Earshot` and the ID `com.charleshartmann.Earshot`.
   The listing (summary, description, screenshots, categories, licence, links, release notes, OARS rating) is
   read from `data/com.charleshartmann.Earshot.metainfo.xml`. For the privacy policy, give the link to
   `PRIVACY.md` in the repository.
6. **Review.** Luma builds the manifest, validates the metainfo and desktop file, runs
   `flatpak-builder-lint`, computes permissions, then a person reviews the first release.

## Listing text (also in the metainfo)

- **Name:** Earshot
- **Summary:** Find, follow and listen to podcasts
- **Developer:** Hartmann Studios
- **Category:** Media (Audio, Player)
- **Licence:** GPL-3.0-or-later
- **Content rating:** OARS 1.1, no objectionable content
- **Screenshots:** `screenshots/new-episodes.png`, `now-playing.png`, `discover.png`, `phone.png`
- **Third-party service:** search and charts use Apple's public podcast directory (said in the listing and `PRIVACY.md`)
