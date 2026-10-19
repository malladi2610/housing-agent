from __future__ import annotations

import argparse
from pathlib import Path

from .db import Store
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Private Munich rental-search assistant")
    sub = parser.add_subparsers(dest="command",required=True)
    sub.add_parser("web",help="Run the private web interface")
    sub.add_parser("worker",help="Run the persistent background worker")
    sub.add_parser("once",help="Process one due job (useful for diagnosis)")
    backup = sub.add_parser("backup",help="Make a consistent SQLite tracker backup")
    backup.add_argument("destination",type=Path)
    args = parser.parse_args()
    settings = Settings()
    settings.prepare()
    if args.command == "web":
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(settings),host=settings.host,port=settings.port,log_level="info")
    elif args.command in {"worker","once"}:
        from .worker import Worker
        worker = Worker(settings)
        worker.run() if args.command=="worker" else worker.tick()
    elif args.command == "backup":
        destination = args.destination.resolve()
        if destination == settings.database_path:
            parser.error("Backup destination must differ from the live database")
        destination.parent.mkdir(parents=True,exist_ok=True)
        Store(settings.database_path).backup(destination)
        print(f"Consistent tracker backup written to {destination}")


if __name__ == "__main__":
    main()
