from dotenv import load_dotenv
from openai import OpenAI

# 自动读取.env文件
load_dotenv()

# 自动从环境变量中读取OPENAI_API_KEY和OPENAI_BASE_URL
client = OpenAI()

# 创建一次对话
response = client.chat.completions.create(
    model="gpt-5.6-luna", messages=[{"role": "user", "content": "Say Hi!"}]
)

print(f"{response.choices[0].message.role} reply: {response.choices[0].message.content}")
