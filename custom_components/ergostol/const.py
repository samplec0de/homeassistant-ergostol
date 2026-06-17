"""Constants for the Ergostol Desk integration."""
from __future__ import annotations

DOMAIN = "ergostol"

# Config / discovery
CONF_ADDRESS = "address"

# Options: pause background polling during a daily quiet window (so the desk's
# LED panel doesn't light up overnight). HH:MM[:SS] local time; may wrap midnight.
CONF_QUIET_START = "quiet_start"
CONF_QUIET_END = "quiet_end"

# Coordinator behaviour
IDLE_POLL_INTERVAL = 5.0      # seconds between height polls while idle
                              # (also reflects handset-driven changes quickly)
MOVE_TIMEOUT = 60.0           # max seconds for one move
STOP_LEAD_HALL = 55           # brake this many hall units early (coast comp.)
TOLERANCE_CM = 0.12           # acceptable error after a move (~5 hall @ g.u 44)
