# Detection templates

The detector recognizes events by matching small images of on-screen messages.
Each event gets its own folder; put one or more `.png` crops in it:

```
templates/
  victory/       "VICTORY ROYALE" text
  eliminated/    "You placed #N" / eliminated-by message
  knocked/       the "knocked" / down-but-not-out message
  elim/          the "ELIMINATED <name>" message when you get a kill
  match_start/   optional, e.g. the "JUMP" prompt on the Battle Bus
  spectating/    optional: the "SPECTATING" label shown after you die in squads
```

Events without templates are simply skipped.

## Making templates

1. Run `python detector.py --save-every 2` and play a couple of matches. It saves a
   full screenshot every 2 seconds to `pc/recordings/` (gitignored).
2. Find screenshots that show each message and note a tight box around the
   **text only** (Paint shows pixel coordinates at the bottom). Avoid background
   scenery, because it changes every time. Save it with:

   ```
   python detector.py --crop recordings/frame_123.png X0 Y0 X1 Y1 --event elim --name my_banner
   ```

   `--crop` takes the box in the screenshot's own pixels and rescales the crop to
   the 1080p reference size, so screenshots from any resolution work.
3. Check it with `python detector.py --test pc/recordings`. Each screenshot gets a
   0-1 score per event. Screenshots with the message should score high (0.8+),
   others low (under 0.5). Adjust `THRESHOLDS` in `detector.py` if needed.
4. Add a second crop of the same message from another match if one template
   misses sometimes.

All templates are stored at a 1080p reference size and every frame is resized so
its HUD is the same size before matching, so templates made on a 1920x1080 PC
work on a 2880x1800 laptop and the other way around. Fortnite lays its HUD out in
the largest 16:9 area that fits, so the HUD size follows min(height, width x 9/16):
the height on a 16:9 screen, the width on a 16:10 laptop. If you crop by hand in
an editor instead of `--crop`, resize the crop by 1080 / that number first.

## Kill counting

Back-to-back kills are counted from what's on screen, so name kill templates by
what they show:

- `*enemy_eliminated*` - one "+300 ENEMY ELIMINATED" popup line. Each stacked
  popup counts as one kill, so crop a **single** line.
- `*double_elim*`, `*triple_elim*`, `*quad_elim*` - the banner for 2/3/4 kills in a
  row. If you ever get a triple, crop it as `elim/triple_elim.png` and it'll count 3.
- anything else in `elim/` counts as 1 kill.

## Spectating (squads)

After you die in squads you spectate a teammate, and their kill banners and
knocks show on your screen. The Pi treats a kill after your death as a reboot,
so a teammate's kill would wrongly bring your match back. Crop the
"SPECTATING" label (or the spectated player's name bar) into `spectating/` and
the detector stops sending kills and knocks while it's on screen. It is never
sent to the Pi itself.

## White text on the game world

The bus "TO JUMP" prompt has no dark box behind it, so `match_start/` templates
are matched on white pixels only (`WHITE_TEXT` in detector.py). Crop just the
letters; the background in the crop is ignored.

A message that always appears in the same spot can get its own small search box in
`TEMPLATE_REGIONS` (detector.py), matched by file name. The kill popup and kill
banner templates do this, which makes detection faster. Changing the in-game HUD scale
still needs new templates.
