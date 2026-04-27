# prep-app

A small interview-prep flashcard app. Decks are per company (currently `cherry`
and `temporal`). Questions are generated on demand by Claude — you press
**Generate** and it reads your existing prep notes plus your prior questions and
produces new ones, no static question bank up front.

Spaced repetition: get one wrong (or press "I don't know") and you'll see it again
in 10 minutes. Get it right and the interval steps up: 1d → 3d → 7d → 14d → 30d.

## Where it lives

- **Primary (Tailscale, anywhere):** `https://example-host.ts.net/prep/` — clean HTTPS via Tailscale Serve, works on any device joined to the tailnet on or off home Wi-Fi
- **Home LAN (mDNS):** `http://example-host.local:8000/prep/`
- **Home LAN (IP fallback):** `http://192.0.2.27:8000/prep/`
- **Source:** `~/Dropbox/workspace/macmini/prep-app/`
- **Data:** `data.sqlite` in the project dir (synced via Dropbox)

## Using it

1. Open the URL → you see your decks.
2. Click a deck → list of all questions in it, plus two buttons:
   - **Study** — start a session of the cards that are currently due.
   - **Generate** — ask Claude for N more questions (default 5, max 15). New questions show up in the "due" pile right away.
3. In a study session, you get one card at a time. Submit your answer (or hit **I don't know**) and you'll see:
   - whether you got it right
   - feedback on what you got/missed
   - the model answer + rubric
   - when you'll see this card next
4. **Suspend** removes a card from rotation if it's actually broken (typo, ambiguous, depends on something you haven't covered). It does not punish you in the SRS — different from "wrong."

## Adding a new deck

Edit `generator.py` → add an entry to `DECK_CONTEXT`:

```python
"newcompany": {
    "source": "newcompany",          # subdir name under ~/Dropbox/workspace/interviews/
    "topics": ["behavioral"],        # optional shared topic dirs
    "focus": "Short paragraph telling Claude what to bias toward.",
},
```

Then `pm2 restart prep-app` and the deck will appear in the index.

## Running it locally (without pm2/Caddy)

```bash
cd ~/Dropbox/workspace/macmini/prep-app
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081 --reload
```

Visit http://127.0.0.1:8081/ directly (no `/prep` prefix when bypassing Caddy —
or set `ROOT_PATH=` to empty).

## Generating questions from the CLI

```bash
.venv/bin/python generator.py cherry 5
.venv/bin/python generator.py temporal 5
```

This shells out to your Claude CLI (`~/.local/bin/claude -p ...`) and uses your
existing CC subscription — no separate API key needed.
