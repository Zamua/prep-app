"""The migration tool: exports a pre-cutover snapshot, imports it into
the running app, and verifies the two agree.

It stands alone. The schema it reads and the scheduler it compares
against are frozen copies (`legacy_schema`, `legacy_srs`), not live
code, so a snapshot stays readable after the app that wrote it is gone.
"""
