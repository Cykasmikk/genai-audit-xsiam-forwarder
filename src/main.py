"""GCP Cloud Function (Gen 2) entrypoint.

Cloud Functions Gen 2 with the Python runtime expects `main.py` at the root
of the deployed source bundle. We re-export the handler from the `forwarder`
package so the actual logic stays cleanly organized.
"""

from forwarder.gcp_handler import handler

__all__ = ["handler"]
