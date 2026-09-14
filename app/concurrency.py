"""Structured-concurrency helpers shared by adapters and the scrape service.

Two separate things are bounded here, and conflating them is how a fan-out inside a fan-out
quietly squares the configured limit:

* **Requests in flight** — the number that matters to a retailer. It is one budget per
  retailer, carried in a context variable so every nested fan-out in that retailer's task
  tree shares it, and acquired around each outbound request.
* **Tasks created** — `gather_bounded`'s own limit, which only stops a fan-out from
  materialising a task per product id.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class RequestBudget:
    """How many outbound requests one retailer may have in flight, and the slots for them."""

    limit: int
    slots: asyncio.Semaphore


_budget: ContextVar[RequestBudget | None] = ContextVar("request_budget", default=None)


@contextmanager
def request_budget(limit: int) -> Generator[RequestBudget]:
    """Give everything in this task tree one shared budget of `limit` requests in flight.

    A task created inside the block inherits the budget (asyncio copies the context at
    `create_task`), so an adapter fanning out inside a search draws on the same slots as the
    search itself rather than opening a second, independent allowance.
    """
    budget = RequestBudget(max(1, limit), asyncio.Semaphore(max(1, limit)))
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


@asynccontextmanager
async def request_slot() -> AsyncGenerator[None]:
    """Hold one of the current retailer's request slots. A no-op outside a budget."""
    budget = _budget.get()
    if budget is None:
        yield
        return
    async with budget.slots:
        yield


def fanout_limit() -> int:
    """How wide a nested fan-out should go: the ambient budget, else the configured default.

    Adapters use this instead of reading settings themselves, so there is one source for the
    policy whether the adapter runs inside a scrape or on its own from a script.
    """
    budget = _budget.get()
    if budget is not None:
        return budget.limit
    return get_settings().scrape_max_concurrent_requests_per_retailer


async def gather_bounded[T](limit: int, factories: Sequence[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run the awaitables concurrently, at most `limit` at a time, results in input order.

    A task group owns the tasks, so the first failure cancels the rest and the caller always
    leaves with nothing still running. Never launches more tasks than it was given, and never
    more than `limit` of them at once. This bounds *tasks*; outbound requests are bounded
    separately by the ambient `request_budget`.
    """
    if not factories:
        return []
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(run(factory)) for factory in factories]
    return [task.result() for task in tasks]


def describe_exception(exc: BaseException) -> str:
    """`Type: message`, unwrapping the task groups that structured concurrency introduces.

    A retailer failure must read the same as it did when scrapes were sequential, not as
    "ExceptionGroup: unhandled errors in a TaskGroup".
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"
