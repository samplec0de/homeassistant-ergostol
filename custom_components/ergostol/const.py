"""Constants for the Ergostol Desk integration."""
from __future__ import annotations

DOMAIN = "ergostol"

# Config / discovery
CONF_ADDRESS = "address"

# Coordinator behaviour
IDLE_POLL_INTERVAL = 5.0      # seconds between height polls while idle
                              # (also reflects handset-driven changes quickly)
MOVE_TIMEOUT = 60.0           # max seconds for one move
STOP_LEAD_HALL = 55           # brake this many hall units early (coast comp.)
TOLERANCE_CM = 0.4            # acceptable error after a move
