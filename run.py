"""开发入口：初始化数据库并启动 Flask 服务。

用法：python run.py [--init-db] [--host 0.0.0.0] [--port 5000]
"""
import argparse
import os

from carelog import create_app
from carelog.db import init_db

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true", help="建表后退出")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--db", default=os.path.join(os.getcwd(), "carelog.db"))
    args = parser.parse_args()

    app = create_app(db_path=args.db)
    with app.app_context():
        init_db()
    if args.init_db:
        print(f"database initialized at {args.db}")
    else:
        app.run(host=args.host, port=args.port)
