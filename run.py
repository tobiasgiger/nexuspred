#!/usr/bin/env python3
"""Entry point for the Tradovate Webhook Bridge.

Usage:
    python run.py                # serve on 0.0.0.0:8000
    HOST=127.0.0.1 PORT=9000 python run.py

The auto-updater re-execs this same command, so keep it self-contained.
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
