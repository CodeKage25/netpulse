"""Deciding what is worth interrupting someone for, and where to send it.

Detection fires immediately — a delay would leave the dashboard saying "fine" while the
alert list said otherwise — and every guard against flapping lives in the notifier's
throttle instead.
"""
