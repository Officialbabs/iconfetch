# IconFetch

Gives your installed apps their real icons, and applies them right away. The
dock, search and switcher redraw within a second, with no sign-out needed.

IconFetch lists every app with the icon it shows now and the icons it found
online. Apps that show a stand-in, nothing at all, or a blurry icon are ticked
for you. Tick any others you'd like to swap, choose among the options by
clicking the icon on the right, and press **Apply**. Every change can be undone.

## Where icons come from

Checked in this order, most official first:

| Source | What it is |
|---|---|
| Flathub | The icon an app's developers publish in its AppStream listing |
| Snap Store | The icon a snap's publisher uploaded |
| App folder | Icons shipped next to an unpacked download (e.g. `~/Downloads/zen/…/default128.png`) |
| Website | The app's homepage: its manifest icons, touch icon or a large favicon |
| Brand icons | [dashboard-icons](https://github.com/homarr-labs/dashboard-icons), brand icons by app name |
| Papirus theme | Full-colour theme icons, for system tools whose generic icon names (e.g. `preferences-system-network`) Adwaita no longer ships |

An app is matched by its ID, program, window class or icon name, and by its
name only when the name isn't an ordinary word. That way "Files" never picks up
an unrelated app called Files. Ada's own apps are left alone.

## How applying works

The desktop reads `~/.local/share/applications` before the system folders. For
a system app, IconFetch writes a copy of its entry there under the same desktop
ID, with `Icon=` pointing to a PNG in `~/.local/share/iconfetch/icons/`. Your
own entries are edited in place, after a backup in
`~/.local/share/iconfetch/backup/`. The PNG's name changes with the picture, so
nothing serves a cached copy, and GIO's app monitor tells the shell at once.

Undo removes IconFetch's copy, or restores the original `Icon=` line.

One thing to know: while IconFetch's copy of a system entry is in place, it
also hides later changes to that entry (a renamed action after an update, say).
Undo and apply again to pick them up.

## Use

```sh
./install.sh            # adds IconFetch to your apps and `iconfetch` to ~/.local/bin
iconfetch               # the window
iconfetch list          # what each app shows now and what was found (* = would apply)
iconfetch apply         # apply the ticked-by-default ones without the window
iconfetch apply vlc     # apply the best find for specific apps (desktop IDs)
iconfetch revert        # put every original icon back (or: iconfetch revert vlc)
./install.sh --uninstall  # reverts everything, then removes IconFetch
```

Needs Python 3 with PyGObject, GTK 4 and libadwaita.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests run in throwaway data folders with a fake network, so they never
touch your real launchers.

## License

GPL-3.0. See [LICENSE](LICENSE).
