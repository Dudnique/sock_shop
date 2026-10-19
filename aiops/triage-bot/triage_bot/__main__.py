"""Entry point: `python -m triage_bot`.

Wiring is explicit here and nowhere else: settings from the environment, the
journal on disk, the two read-only clients, the Telegram client, the cascade,
and the HTTP server. Nothing in this file can reach the cluster API, because no
code in this package can.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__
from .config import Settings
from .context import LokiClient, PrometheusClient
from .journal import Journal
from .metrics import Metrics
from .pipeline import TriagePipeline
from .server import make_server
from .telegram import TelegramClient

log = logging.getLogger("aiops")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    log.info("aiops-triage %s starting", __version__)

    metrics = Metrics()
    journal = Journal(settings.journal_dir)
    prometheus = PrometheusClient(settings.prometheus_url, settings.http_timeout_seconds)
    loki = LokiClient(settings.loki_url, settings.http_timeout_seconds)
    telegram = TelegramClient(settings, metrics=metrics)
    pipeline = TriagePipeline(settings, metrics, journal, prometheus, loki, telegram)

    if settings.llm_api_style != "mock" and pipeline.model_adapter is None:
        # Loud on purpose: a stand that silently loses its model tier is worse
        # than one that says so, and every card repeats the reason.
        log.error("model tier is NOT available: %s", pipeline.model_unavailable_reason)
    log.info("cascade: tier0=rules, tier1=%s, eval_mode=%s, telegram=%s",
             settings.model_adapter, settings.eval_mode, telegram.enabled)

    server = make_server(settings, metrics, pipeline, journal)
    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received - shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # not on the main thread / not supported
            pass

    state = journal.load_state()
    poller = threading.Thread(
        target=telegram.poll_loop,
        args=(pipeline.handle_callback, pipeline.handle_message, state, stop_event),
        kwargs={"save_state": lambda: journal.save_state(state)},
        name="telegram-poller", daemon=True)
    poller.start()

    log.info("listening on %s:%s", settings.listen_host, settings.listen_port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop_event.set()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
