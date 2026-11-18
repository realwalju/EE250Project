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
2. Find screenshots that show each message. Open one in Paint (or any editor) and
   crop tightly around the **text only**. Avoid background scenery, because it
   changes every time. Save it as a `.png` in the event's folder.
3. Check it with `python detector.py --test pc/recordings`. Each screenshot gets a
   0-1 score per event. Screenshots with the message should score high (0.8+),
   others low (under 0.5). Adjust `THRESHOLDS` in `detector.py` if needed.
4. Add a second crop of the same message from another match if one template
   misses sometimes.

Templates must come from the same screen resolution and HUD scale you play at.
