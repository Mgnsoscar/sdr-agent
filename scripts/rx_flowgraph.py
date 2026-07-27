#!/usr/bin/env python3
"""
Example GNU Radio flowgraph stub.

Replace the body with your actual gr.top_block subclass.
The agent captures all stdout/stderr to the task log file.
"""

import time
import sys

# ── GNU Radio imports ─────────────────────────────────────────────────────────
# Uncomment when running on the Pi with GNU Radio installed:
#
# from gnuradio import gr, uhd, blocks
# import osmosdr


def main():
    print("rx_flowgraph: starting", flush=True)

    # ── Build your flowgraph here ─────────────────────────────────────────────
    # tb = gr.top_block()
    # src = uhd.usrp_source(
    #     ",".join(("", "")),
    #     uhd.stream_args(cpu_format="fc32", args="", channels=list(range(1))),
    # )
    # sink = blocks.null_sink(gr.sizeof_gr_complex * 1)
    # tb.connect(src, sink)
    # tb.start()
    # ─────────────────────────────────────────────────────────────────────────

    # Stub: just run until interrupted
    try:
        while True:
            print("rx_flowgraph: running …", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        print("rx_flowgraph: interrupted", flush=True)
        sys.exit(0)

    # tb.stop()
    # tb.wait()


if __name__ == "__main__":
    main()
