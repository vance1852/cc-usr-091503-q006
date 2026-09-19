import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from washcare.api import create_app  # noqa: E402
from washcare.notifications import FailingNotificationSender, LogNotificationSender  # noqa: E402


@pytest.fixture
def client():
    app = create_app(":memory:", LogNotificationSender(), seed=True)
    app.testing = True
    return app.test_client()


@pytest.fixture
def failing_client():
    app = create_app(":memory:", FailingNotificationSender(), seed=True)
    app.testing = True
    return app.test_client()


# 演示数据中的固定标识
BABY = "BABY-0001"
ID_TAG = "手环 W-0001 / 床头卡 C-0001"
NURSE_OK = "NURSE-A07"
NURSE_EXPIRED = "NURSE-X99"
LOT_WASH = "LOT-WASH-2026-09"
LOT_LOTION = "LOT-LOTION-2026-09"
LOT_OIL_EXPIRED = "LOT-OIL-2025-03"
