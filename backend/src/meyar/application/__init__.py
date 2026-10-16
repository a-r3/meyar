"""Shared, client-neutral application operations (issue #45).

Adapters (Jinja UI, machine REST, human JSON) authenticate, validate transport
shape and present results. Every operation in this package receives an already
trusted principal and never reads tenant/user/session identity from a client."""
