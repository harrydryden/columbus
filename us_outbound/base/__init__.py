"""The base layer (9 Oct 2026, the refactoring scan's phase 6): what enrolment, learning and operations all read.

The alert poster (notify.py), the plain words of the alerts (say.py), and what the kill rules hold back (holds.py).
The heartbeats a run wrote and the operator stop moved to ops/health.py with the rest of job health (W2-4, 20 Oct
2026); it imports only this layer at module level, so enrolment and the registry read the operator stop from it. They need only the clients, the store, the context and the settings model, so every
layer above settings may import them at module level; nothing here imports sources, scoring, enrol, learn or ops.
"""
