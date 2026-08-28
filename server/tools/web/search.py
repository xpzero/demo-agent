from .client import MAX_RESULTS, client, truncate, wrap_untrusted

SCHEMA = {
    "type": "function",
    "name": "web_search",
    "description": "联网搜索，返回若干条结果的标题、链接与摘要。需要页面全文时再用 fetch_url",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
        },
        "required": ["query"],
    },
    "strict": False,
}


def run(args: dict) -> str:
    response = client().search(query=args["query"], max_results=MAX_RESULTS)
    results = response.get("results", [])

    if not results:
        return "没有找到相关结果"

    # 带序号返回，模型才好指明要 fetch_url 抓第几条
    listing = "\n\n".join(
        f"[{i}] {item.get('title', '')}\n{item.get('url', '')}\n{truncate(item.get('content', ''))}"
        for i, item in enumerate(results, 1)
    )
    return wrap_untrusted(listing)
