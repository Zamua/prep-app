"""Trivia bounded context.

A trivia deck is a notification-driven deck mode. Every
`notification_interval_minutes` the scheduler picks the next card from
the deck's queue, fires a web push carrying the question text, and the
user taps the push to answer. There's no SRS ladder — cards rotate
forever in a per-deck queue, and the generator drops fresh batches in
when the deck runs out of unanswered cards.

This context owns:
- the `trivia_queue` row state (queue position + last answer)
- the queue traversal rules (pick next, rotate on answer)
- the batch generation trigger logic (when to ask claude for more)

It depends on:
- `prep.decks.Question` for the underlying card content
- `prep.study` for free-text grading (reused as-is)
- `prep.notify` for the web-push delivery primitive
"""
