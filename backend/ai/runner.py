"""Shared concurrency guard for AI queue runs (PRD §18: one claiming queue at a time).

Two paths drain claimable media through :func:`backend.ai.queue.run_ai_queue`:

* the scrape pipeline's AI stage (``backend.api.routes_scraper._run_ai_stage``), and
* the background :class:`backend.ai.supervisor.AISupervisor`.

Claiming is status-based, so exactly one of them may run at a time; both
therefore race through :class:`AIQueueGate` — a compare-and-set over the single
``app.state.ai_queue_running`` boolean created in the app lifespan. Acquisition
is check-and-set with no ``await`` in between, which makes it atomic on the
event loop that runs it, and tests (or legacy code) that poke
``app.state.ai_queue_running`` directly keep working, because the gate reads
and writes that very attribute.
"""

from __future__ import annotations


class AIQueueGate:
    """Compare-and-set guard over ``app.state.ai_queue_running``."""

    def __init__(self, state: object) -> None:
        """``state`` is the object holding the flag — typically ``app.state``."""
        self._state = state

    @property
    def running(self) -> bool:
        """True while any AI queue run owns the slot."""
        return bool(getattr(self._state, "ai_queue_running", False))

    def try_acquire(self) -> bool:
        """Claim the queue slot; ``False`` when another run already owns it."""
        if self.running:
            return False
        setattr(self._state, "ai_queue_running", True)
        return True

    def release(self) -> None:
        """Free the slot (unconditionally — every acquirer releases in ``finally``)."""
        setattr(self._state, "ai_queue_running", False)
