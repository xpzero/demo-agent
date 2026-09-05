from cli import chat
from logging_setup import setup_logging
from services import create_default_services
from sessions import SessionError, create_default_session_service


def main():
    setup_logging()
    try:
        session_service = create_default_session_service()
        try:
            # 换成 run_agent("...") 可以跑单次任务
            chat(create_default_services(), session_service)
        finally:
            session_service.close()
    except SessionError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
