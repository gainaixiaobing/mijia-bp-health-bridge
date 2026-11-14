import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 让"本地时区"在测试里可预期（Unix 上 fromtimestamp 会读 TZ）
os.environ["TZ"] = "Asia/Shanghai"
if hasattr(time, "tzset"):
    time.tzset()
