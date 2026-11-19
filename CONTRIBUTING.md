# Contributing

Thanks for helping make Earshot better.

## Reporting a problem

[Open an issue](https://github.com/chartmann1590/earshot/issues/new/choose) and say what you did and what happened.
If it's about one show, include its name or feed address.

## Sending a change

1. Fork the repository and make a branch.
2. Keep each pull request to one change.
3. Run the checks:
   ```sh
   python3 -m pytest tests/test_core.py
   xvfb-run flatpak run --command=python3 org.projectluma.Sdk//44 tests/test_ui.py
   luma lint && luma test
   ```
4. For anything you can see, try it at phone width (360 px) and on a computer, in light and dark.
5. Say in the pull request what you tested.

## Style

- Use LumaUI parts (`luma_appkit`) before writing your own widget, and Luma's colour tokens, never fixed colours.
- Words in the interface are short and plain, in sentence case.
- Feeds come from strangers: treat everything in them as untrusted, bound every read, and never let one bad
  episode break a whole show.
- `feeds.py`, `store.py`, `notes.py` and `opml.py` don't import GTK, so they can be tested anywhere. Keep it that way.

By contributing you agree your work is licensed under GPL-3.0-or-later.
