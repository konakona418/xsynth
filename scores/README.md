# Scores

Text scores for `xsynth play`, and where each one came from.

```bash
uv run xsynth play scores/twinkle.txt
uv run xsynth listen 30 --source <handle>     # record it and play it back
```

The format is one note to a line -- start time in seconds, pitch, duration --
with the patch in a header of `key value` lines above them. `;` starts a
comment. `xsynth/host/score.py` is the definition and `xsynth/host/player.py`
streams it into the board's schedule; the README has the syntax.

| score | what it is | status |
| --- | --- | --- |
| `twinkle.txt` | *Twinkle, Twinkle, Little Star*; the melody is "Ah! vous dirai-je, maman", published 1761 | public domain |
| `frere-jacques.txt` | *Frère Jacques*, traditional French | public domain |
| `ode-to-joy.txt` | Beethoven, 9th Symphony, 1824, in D major. Bars 1-8 are the theme, bars 9-16 a reconstruction | public domain |
| `tong-poo.txt` | Ryuichi Sakamoto, *Tong Poo*, 1978. Track 2 of a piano-solo MIDI, bars 0-15, reduced mechanically | **not public domain** |

Only the tunes are written down -- no lyrics anywhere, which keeps the
traditional ones clear of anything a translator or a publisher might hold.

`tong-poo.txt` is different in kind: the composition is in copyright, so what
is here is a derivative of it, included as a listening example. It was produced
without any editing by hand -- note-ons paired with their note-offs, ticks
converted at the MIDI's own 84 bpm -- so what it says is what the file said.

None of these are good tests of the engine. They are melodies, and a melody
repeats pitches, which is what found the voice-releasing bug; for the engine's
own acceptance gates see `tests/` instead.
