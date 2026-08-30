# Assets & Build Modes

Everything the game can load, whether it's required, and how the two build
modes work. All of this is driven by the `pack-config` block near the top of
`index.html`, then processed by `build_atlas.py`.

---

## 1. Quick answer: what do I actually need?

**Bare minimum to run:** the six spritesheets and `bg.png`. Everything else
degrades gracefully. If a button PNG is missing the game draws a button
instead; if a sound is missing that effect is silent; if a video is missing
that scene is skipped.

Put every file in the **same folder** as `index.html` and `build_atlas.py`,
then run:

```
python build_atlas.py
```

---

## 2. Spritesheets (required)

These are grid spritesheets. The `cols`, `rows` and `frames` values in
`pack-config` must match the actual layout of your image, or frames will be
sliced in the wrong places.

| File | Config key | Grid (current) | Required? |
|---|---|---|---|
| `arrow_sheet.png` | `ARROW_SRC` | 3 x 7, 20 frames | Yes |
| `spear_spreadsheet.png` | `SPEAR_SRC` | 4 x 6, 23 frames | Yes |
| `shield.png` | `SHIELD_SRC` | 4 x 4, 16 frames | Yes |
| `eagle.png` | `EAGLE_SRC` | 3 x 6, 18 frames | Yes |
| `coin.png` | `COIN_SRC` | 4 x 4, 14 frames | Yes |
| `hand.png` | `HAND_SRC` | 5 x 5, 22 frames | Yes |

`frames` is how many cells actually contain art. Leftover cells at the end
of the grid are ignored, so a 3x7 sheet with only 20 real frames is fine.

**Animation speed** is optional per sheet. Add `"fps": 32` to any of these
entries to override its default (arrow 10, spear 10, shield 12, eagle 12,
coin 16, hand 6).

---

## 3. Single images

| File | Config key | Used for | Required? |
|---|---|---|---|
| `bg.png` | `BG_SRC` | Scrolling background | Yes |
| `score.png` | `SCORE_SRC` | Score plate behind the HUD | Recommended |
| `pause.png` | `PAUSE_SRC` | Pause button | Recommended |
| `play.png` | `PLAY_BTN_SRC` | Menu play button | Optional |
| `leaderboard.png` | `BOARD_BTN_SRC` | Leaderboard icon (the one that glows) | Optional |
| `resume.png` | `RESUME_BTN_SRC` | Resume button in the pause popup | Optional |
| `menubtn.png` | `MENU_BTN_SRC` | Menu button (pause + game over popups) | Optional |
| `restart.png` | `RESTART_BTN_SRC` | Play-again button in the game over popup | Optional |
| `tutorial.png` | `TUTORIAL_SRC` | Blinking "tap" hint at the bottom | Optional |
| `loading.png` | `LOADING_SRC` | Logo on the loading screen | Optional |

Any missing button image falls back to a drawn button with a text label, so
the game stays usable either way.

`loading.png` is special: it is never packed into the atlas, because it has
to appear *before* the atlas finishes downloading. It's always handled as a
standalone file (`"standalone": true`).

---

## 4. Videos (all optional)

| File | Config key | Used for | Toggle |
|---|---|---|---|
| `intro.mp4` | `INTRO_SRC` | Plays the first time you start a run each session | `INTRO_ON` |
| `intro2.mp4` | `INTRO2_SRC` | Plays on every later run in the same session | `INTRO2_ON` |
| `menu.mp4` | `MENU_BG_SRC` | Looping menu background | `MENU_BG_ON` |

Intro videos play **with their own audio**, and the menu music stops while
they run. If a video is missing, that step is simply skipped and the game
goes straight through.

To disable one without deleting the file, set its toggle to `false` in
`index.html`.

---

## 5. Sounds (all optional)

| File | Config key | Used for |
|---|---|---|
| `menu_music.mp3` | `SND_MENU_SRC` | Looping music on the menu and leaderboard |
| `game_music.mp3` | `SND_GAME_SRC` | Looping music during a run |
| `coin.mp3` | `SND_COIN_SRC` | Coin pickup |
| `hit.mp3` | `SND_HIT_SRC` | Crashing, or being grabbed by the hand |
| `catch.mp3` | `SND_CATCH_SRC` | A spear locking on |
| `tap.mp3` | `SND_TAP_SRC` | Flapping, and mashing to break free |
| `shield.mp3` | `SND_SHIELD_SRC` | Shield pickup |

Every sound is marked `"optional": true`, so missing files are reported and
skipped and the build still succeeds. `.mp3`, `.ogg`, `.wav` and `.m4a` all
work.

Volume and master switch, in `index.html`:

```js
const SOUND_ON = true;      // false disables all audio
const MUSIC_VOLUME = 0.45;
const SFX_VOLUME = 0.6;
const VIDEO_VOLUME = 0.8;   // intro video audio
```

Sound uses plain HTML5 `Audio`, not the WebAudio graph, so decoding and
mixing stay off the main thread and cost no frame time. Short effects are
pooled so rapid repeats overlap instead of cutting each other off, and
everything pauses when the browser tab is backgrounded.

---

## 6. Fonts (optional)

Any `.ttf` / `.otf` / `.woff` / `.woff2` referenced from a CSS
`@font-face` rule in `index.html` is found and embedded automatically. You
don't need to list fonts in `pack-config`.

The game currently expects `AncientFont`. If its file is missing the browser
falls back to a system serif, which still works but loses the styling.

---

## 7. The two build modes

Controlled by one line in `pack-config`:

```json
"loading": false
```

### Embed mode (`"loading": false`) — the default

Everything is base64-embedded directly into a single `game.html`. One file,
no server needed, works when opened straight off disk.

- Larger HTML file (all assets inline)
- Nothing to upload except `game.html`
- The loading bar barely appears, because there's nothing to download

Use this for simple hosting, or handing someone a single file.

### Loading mode (`"loading": true`)

Heavy assets are **not** embedded. `build_atlas.py` copies them next to
`game.html` and the game downloads them at runtime, showing the loading
screen and progress bar while it does.

What gets externalized: the atlas page(s), all videos, all sounds, and
`loading.png`. What stays inline: all the game code, and the atlas frame
metadata (a few KB).

After building you'll have:

```
game.html
atlas0.png
loading.png
intro.mp4
intro2.mp4
menu.mp4
menu_music.mp3      (and any other sounds you provided)
```

Upload all of them together. **This mode needs a real web server** (http://
or https://). Opening it from `file://` will fail, because browsers block
loading local files that way.

---

## 8. Hosting assets on another domain or CDN

In loading mode you can serve assets from somewhere other than where
`game.html` lives. Set `baseUrl` in `pack-config`:

```json
{
  "loading": true,
  "baseUrl": "https://cdn.example.com/arash/",
  "assets": { ... }
}
```

Now the game requests `https://cdn.example.com/arash/atlas0.png` instead of
a file next to itself. Upload `game.html` wherever you like, and everything
else to that URL.

Notes:
- A trailing slash is optional, it's added if missing.
- Leave `baseUrl` empty (the default) to load assets from next to
  `game.html`.
- `baseUrl` is ignored in embed mode, since there's nothing to fetch.
- The host must allow cross-origin requests (CORS). Most CDNs do by default;
  a plain web server may need `Access-Control-Allow-Origin` configured.

---

## 9. The loading screen

Shown while assets download. Only really visible in loading mode.

```js
const LOADING_TEXT = "LOADING";
const LOAD_BAR_W = 0.46;     // bar width, fraction of screen width
const LOAD_BAR_H = 0.016;    // bar height
const LOAD_BAR_Y = 0.86;     // vertical position
const LOAD_LABEL_Y = 0.78;   // label position
const LOAD_LABEL_SIZE = 0.04;
```

The bar fills based on real download progress across every queued asset.
`loading.png`, if present, is shown above the label.

---

## 10. Config reference

Top-level keys in `pack-config`:

| Key | Default | Meaning |
|---|---|---|
| `max` | `1280` | Max pixel size for a spritesheet's longest side; larger sheets are scaled down before packing |
| `loading` | `false` | `false` embeds everything, `true` externalizes heavy assets |
| `baseUrl` | `""` | URL prefix for externalized assets (loading mode only) |

Per-asset keys:

| Key | Meaning |
|---|---|
| `file` | Filename on disk |
| `cols` / `rows` | Spritesheet grid dimensions |
| `frames` | How many cells actually contain art |
| `fps` | Optional animation speed override |
| `standalone` | Never pack into the atlas, keep as its own file |
| `optional` | Missing file is skipped instead of warned about |

---

## 11. Checklist before shipping

- [ ] All six spritesheets present, with `cols`/`rows`/`frames` matching the art
- [ ] `bg.png` present
- [ ] Decide on `"loading"`: `false` for a single portable file, `true` for a real server with a progress bar
- [ ] If using loading mode, upload **every** generated file, not just `game.html`
- [ ] If using `baseUrl`, confirm the host sends CORS headers
- [ ] Point `LEADERBOARD_API_URL` at your real server (while it's still the placeholder, all leaderboard calls are skipped)
- [ ] Set `SCORE_SIGN_KEY` to match `SIGN_KEY` in `api.php`
- [ ] Optionally set `LEADERBOARD_PAGE_URL` for the "view full leaderboard" link
