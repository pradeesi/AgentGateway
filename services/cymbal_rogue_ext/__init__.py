"""
Purpose: Package initializer for the unregistered Rogue Egress test endpoint.
Architecture/Context: Deployed to Cloud Run (`cymbal-rogue-ext`) and intentionally
         omitted from Google Cloud Agent Registry to prove default-deny egress
         enforcement on `AGENT_TO_ANYWHERE` Agent Gateway.
Dependencies/Side Effects: Imports `services.cymbal_rogue_ext.app`.
Author: Pradeep Singh
License: Apache-2.0
"""

from services.cymbal_rogue_ext.app import app

__all__ = ["app"]
