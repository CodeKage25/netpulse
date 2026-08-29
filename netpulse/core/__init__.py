"""The foundation: types, time, and the store.

Nothing here knows about routers, HTTP, or analysis. Everything else is built on it,
and it imports none of them back — which is what keeps the dependency graph a DAG
rather than a suggestion.
"""
