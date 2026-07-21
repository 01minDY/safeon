"""내장 MQTT 브로커 (amqtt) — mosquitto가 없을 때 사용.

실행: python broker.py
대회장에서는 mosquitto 권장 (더 안정적), 이건 개발/데모용 백업.
"""
import asyncio
import logging

from amqtt.broker import Broker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

CONFIG = {
    "listeners": {
        "default": {"type": "tcp", "bind": "0.0.0.0:1883"},
    },
    "sys_interval": 0,
    "auth": {"allow-anonymous": True},
}


async def main():
    broker = Broker(CONFIG)
    await broker.start()
    print("SafeON MQTT 브로커 기동 완료 (포트 1883). Ctrl+C로 종료.")
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n브로커 종료")
