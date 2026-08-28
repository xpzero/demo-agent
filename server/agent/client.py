import os

from dotenv import load_dotenv
from openai import OpenAI

# 自动读取 .env 文件，必须在创建客户端之前执行
load_dotenv()

# 自动从环境变量中读取 OPENAI_API_KEY 和 OPENAI_BASE_URL
client = OpenAI()

# 模型与系统提示词可在 .env 中覆盖；未设置或留空时使用学习项目的默认值
MODEL = os.getenv("MODEL") or "gpt-5.6-terra"

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or "你是一个有用的助手，可以调用工具来帮助用户。"
