"""Entry point for the standalone .exe build (PyInstaller) and for running
locally with `python run.py` instead of `uvicorn helper:app`. Kept as a
plain script (not just the uvicorn CLI) since PyInstaller needs an actual
Python entry point to bundle against.
"""
import uvicorn

from helper import app

if __name__ == "__main__":
    print("Instalocker helper starting on http://127.0.0.1:13337 -- leave this window open.")
    uvicorn.run(app, host="127.0.0.1", port=13337)
