"""Constants for the Ergostol Desk integration."""

from __future__ import annotations

DOMAIN = "ergostol"

# Config / discovery
CONF_ADDRESS = "address"

# Options: pause background polling during a daily quiet window (so the desk's
# LED panel doesn't light up overnight). HH:MM[:SS] local time; may wrap midnight.
CONF_QUIET_START = "quiet_start"
CONF_QUIET_END = "quiet_end"

# Options: sit/stand preset heights (cm). The preset buttons drive to these.
CONF_SIT_HEIGHT = "sit_height"
CONF_STAND_HEIGHT = "stand_height"
DEFAULT_SIT_HEIGHT = 73.0
DEFAULT_STAND_HEIGHT = 115.0

# Entry data: cached init-walk calibration (static per desk). Lets reconnects
# skip the 11-step walk — burst writes right after the module wakes are the
# prime suspect for the E04 "communication fault" on the handset bus.
CONF_CALIBRATION = "calibration"

# Coordinator behaviour
CONNECT_SETTLE_DELAY = 1.0  # seconds after connect before the first write
INIT_STEP_TIMEOUT = 0.5  # seconds to wait for each init-walk reply
INIT_STEP_RETRIES = 3  # attempts per init-walk step before giving up
IDLE_POLL_INTERVAL = 5.0  # seconds between height polls while idle
# (also reflects handset-driven changes quickly)
MOVE_TIMEOUT = 60.0  # max seconds for one move
STOP_LEAD_HALL = 55  # brake this many hall units early (coast comp.)
TOLERANCE_CM = 0.12  # acceptable error after a move (~5 hall @ g.u 44)
