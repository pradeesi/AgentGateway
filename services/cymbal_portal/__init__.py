"""
Purpose: Package initializer for the Cymbal Bank Operations Portal web console.
Architecture/Context: Exposes `app` and `create_portal_app` for Cloud Run Uvicorn
         serving, GKE deployment, and local pytest integration tests.
Dependencies/Side Effects: Imports `services.cymbal_portal.main`.
Author: Pradeep Singh
License: Apache-2.0
"""

from services.cymbal_portal.main import app, create_portal_app

__all__ = ["app", "create_portal_app"]
