from cli import chat
from logging_setup import setup_logging


def main():
    setup_logging()
    # 换成 run_agent("...") 可以跑单次任务
    chat()


if __name__ == "__main__":
    main()
