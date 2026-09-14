"""Request dependencies shared by the routers."""

from fastapi import Request

from app.retailers.clients import RetailerClients


def optional_http_clients(request: Request) -> RetailerClients | None:
    """The pooled HTTP clients, or None when the application lifespan did not run.

    For work that is allowed to carry on without them -- a search still answers from the
    database, it just cannot revalidate behind its answer.
    """
    clients = getattr(request.app.state, "http_clients", None)
    return clients if isinstance(clients, RetailerClients) else None


def get_http_clients(request: Request) -> RetailerClients:
    """The pooled HTTP clients opened by the application lifespan.

    For work that cannot proceed without them. One accessor, so a rename or a lazier
    lifespan cannot fail loudly in one router and silently in the other.
    """
    clients = optional_http_clients(request)
    if clients is None:  # pragma: no cover - only reachable without the lifespan
        raise RuntimeError("HTTP clients are not open; the application lifespan did not run")
    return clients
