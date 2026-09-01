from __future__ import annotations
import argparse
from .metrics import start_metrics_server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=9108)
    start_metrics_server(parser.parse_args().port)
    import threading; threading.Event().wait()
