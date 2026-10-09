"""The base layer (9 Oct 2026, the refactoring scan's phase 6): what enrolment, learning and operations all read.

The alert poster (notify.py), the heartbeats a run wrote and the operator stop (heartbeats.py), and what the kill
rules hold back (holds.py). They need only the clients, the store, the context and the settings model, so every
layer above settings may import them at module level; nothing here imports sources, scoring, enrol, learn or ops.
"""
