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

All templates are stored at a 1080p reference size and every frame is resized to
the same height before matching, so templates made on a 1920x1080 PC work on a
2880x1800 laptop and the other way around (Fortnite's HUD scales with screen
height). If you crop by hand in an editor instead of `--crop`, resize the crop by
1080 / (your screenshot's height) before saving. Changing the in-game HUD scale
still needs new templates.
