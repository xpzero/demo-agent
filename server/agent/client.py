import os

from dotenv import load_dotenv
from openai import OpenAI

# 自动读取 .env 文件，必须在创建客户端之前执行
load_dotenv()

# 默认走智谱的 OpenAI 兼容接口；设置 BASE_URL 可换成其他兼容网关
client = OpenAI(
    api_key=os.getenv("API_KEY"),
    base_url=os.getenv("BASE_URL") or "https://open.bigmodel.cn/api/paas/v4",
)

# 模型与系统提示词可在 .env 中覆盖；未设置或留空时使用学习项目的默认值
MODEL = os.getenv("MODEL") or "glm-4.6"

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or "你是一个有用的助手，可以调用工具来帮助用户。"
