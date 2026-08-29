"""The dashboard: a query layer, an HTTP transport, and a page.

`api` holds every question and knows nothing about sockets; `server` is a thin transport
with no opinions of its own. The assets are three files stitched into one
self-contained response at startup.
"""
