# Changelog

## 1.2.1 (2026-11-19)

- Combine the GitHub release checker with scoped lifetime promotion entry and exact purchased-plan labels.

## 1.2.0 (2026-11-19)

- Update alerts: Earshot watches its GitHub releases and tells you when a new version is out, with the release
  notes. Copies from Depot update through Depot (Luma's own updater); copies downloaded from GitHub download
  the bundle for this device, check it against the release's checksums, and open it in the system installer,
  which asks you to confirm. Turn automatic checks off in the update panel or Settings.

## 1.1.1 (2026-11-19)

- Ready for promotion codes on Earshot Plus Lifetime: when the store turns them on, the upgrade panel says so and
  codes are entered on Stripe's checkout page. Codes are checked by Stripe; a completed order unlocks exactly like a paid one.

## 1.1.0 (2026-11-19)

- Earshot Plus, an optional purchase through Stripe: voice boost, skip intros and outros per show, automatic
  downloads, and listening stats. Nothing that was free before costs money now.
- Computer, tablet, phone and TV screenshots; on Luma's TV home.

## 1.0.0 (2026-11-19)

The first release.

- Discover: charts for 15 categories and search across the podcast directory; add any show by its feed or website.
- New episodes, Keep listening, Up next with automatic play-through, and Downloads.
- Pitch-correct speed from 0.8× to 3×, remembered per show.
- Chapters (Podcasting 2.0 and Podlove) and tappable times in show notes.
- Sleep timer with a gentle fade, or at the end of the episode or chapter.
- Media keys, headphone buttons and the lock screen through MPRIS; keeps playing with the window closed.
- OPML import and export.
