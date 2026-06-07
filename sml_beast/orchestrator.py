"""
sml_beast.orchestrator — multithreaded entry point.

Launches the internal x402 proxy in one thread and one Worker per product
vertical in additional threads. Verticals scale concurrently; neither
blocks the other.

The proxy is the only path workers use for live SERP data — they speak
pure x402 to it (X-PAYMENT header), the proxy validates and fires the
operator's Serper.dev key upstream. Real data, clean protocol.
"""

import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sml_beast.adapters.x402_proxy import create_app
from sml_beast.workers.mastersheets import MasterSheetsWorker
from sml_beast.workers.xrpl_x402    import XrplX402Worker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sml-beast.orchestrator")

# Bind host: 0.0.0.0 in production (Render needs an externally-reachable port
# so the dashboard works); 127.0.0.1 locally for the same security as before.
# Render sets $PORT; we honor it. Workers always hit 127.0.0.1:$PORT internally.
PROXY_BIND_HOST = "0.0.0.0" if os.environ.get("PORT") else os.environ.get("X402_PROXY_HOST", "127.0.0.1")
PROXY_PORT      = int(os.environ.get("PORT") or os.environ.get("X402_PROXY_PORT", "4020"))
PROXY_URL       = f"http://127.0.0.1:{PROXY_PORT}"

OUTPUT_ROOT = os.environ.get(
    "BEAST_OUTPUT_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"),
)


def _start_proxy(stop: threading.Event) -> threading.Thread:
    """Run the x402 facilitator-proxy on its own thread. werkzeug threaded is
    fine — internal worker traffic is low-volume and the dashboard is
    read-only. On Render the same process binds to $PORT so the operator
    can hit /dashboard externally."""
    app = create_app(output_root=OUTPUT_ROOT)

    def _serve():
        app.run(host=PROXY_BIND_HOST, port=PROXY_PORT, threaded=True, use_reloader=False)

    t = threading.Thread(target=_serve, name="x402-proxy", daemon=True)
    t.start()
    # Wait until the proxy responds before letting workers start
    import requests
    for _ in range(50):
        try:
            if requests.get(f"{PROXY_URL}/health", timeout=1).ok:
                logger.info("x402 proxy live at %s", PROXY_URL)
                return t
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("x402 proxy failed to come up within 10s")


def main(verticals: tuple[str, ...] | None = None) -> int:
    if not os.environ.get("SERPER_API_KEY"):
        logger.error("SERPER_API_KEY missing. Live SERP data is the only data path. Aborting.")
        return 2
    if not os.environ.get("X402_PROXY_SECRET"):
        logger.error("X402_PROXY_SECRET missing. Cannot mint internal x402 tokens. Aborting.")
        return 2

    stop = threading.Event()

    def _shutdown(sig, frame):  # noqa: ARG001
        logger.info("signal %s — initiating clean shutdown", sig)
        stop.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    proxy_thread = _start_proxy(stop)

    requested = set(verticals or ("mastersheets", "xrpl_x402"))
    workers: list = []
    if "mastersheets" in requested:
        workers.append(MasterSheetsWorker(
            brief=MasterSheetsWorker.BRIEF,
            silos=MasterSheetsWorker.SILOS,
            proxy_url=PROXY_URL,
            output_dir=os.path.join(OUTPUT_ROOT, "mastersheets"),
            stop=stop,
        ))
    if "xrpl_x402" in requested:
        workers.append(XrplX402Worker(
            brief=XrplX402Worker.BRIEF,
            silos=XrplX402Worker.SILOS,
            proxy_url=PROXY_URL,
            output_dir=os.path.join(OUTPUT_ROOT, "xrpl"),
            stop=stop,
        ))

    logger.info("launching %d workers (verticals=%s)", len(workers), sorted(requested))
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(w.run) for w in workers]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.error("worker crashed: %s", e)
                stop.set()
                return 1

    logger.info("all verticals complete; proxy thread will exit with process")
    return 0


if __name__ == "__main__":
    sys.exit(main())
