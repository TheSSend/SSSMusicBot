import os
import logging
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ================= OWNER =================

_owner_id_str = os.getenv("OWNER_ID")
if not _owner_id_str:
    raise ValueError("OWNER_ID environment variable is not set")
OWNER_ID: int = int(_owner_id_str)

# ================= TIME =================

MOSCOW_TZ = timezone(timedelta(hours=3))
DATE_FORMAT = "%d.%m.%Y %H:%M"
