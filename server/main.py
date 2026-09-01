from cli import chat
from logging_setup import setup_logging
from services import create_default_services


def main():
    setup_logging()
    # 换成 run_agent("...") 可以跑单次任务
    chat(create_default_services())


if __name__ == "__main__":
    main()
