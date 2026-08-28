from .client import client, truncate, wrap_untrusted

SCHEMA = {
    "type": "function",
    "name": "fetch_url",
    "description": "抓取指定网页的正文内容，通常用于读取 web_search 返回的某个链接",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "完整的网页地址，例如 https://example.com/post",
            },
        },
        "required": ["url"],
    },
    "strict": False,
}


def run(args: dict) -> str:
    url = args["url"]
    response = client().extract(urls=[url], format="markdown")

    results = response.get("results", [])
    if not results:
        raise ValueError(f"抓取失败：{url}")

    return wrap_untrusted(truncate(results[0].get("raw_content", "")))
