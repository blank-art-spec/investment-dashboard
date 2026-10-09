# 配置示例

此处仅保存空配置，用于说明字段，程序不会自动读取这些示例。
推荐在网页右上角“设置”中配置接口地址、模型与 API Key，并在抓取窗口登录雪球。

| 示例 | 实际存放位置 | 字段 |
| --- | --- | --- |
| `settings.example.json` | `data/settings.json` | `llm_base_url` 为兼容接口的基础地址；程序会在后面追加 `/chat/completions`。`llm_model` 为服务商提供的模型名称，空值使用程序默认名称。 |
| `secrets.example.json` | `data/secrets.json` | `llm_api_key` 为接口密钥；`xueqiu_cookie` 为可选的登录 Cookie。 |

这些文件只列出部分字段，不应覆盖已有 `data/settings.json` 或 `data/secrets.json`。
界面会保留其他偏好并将敏感项分流至密钥文件。`DASH_LLM_API_KEY` 和 `DASH_XUEQIU_COOKIE`
可作为文件内相应字段为空时的环境变量兜底，已有文件值优先。

示例没有包含任何真实接口密钥、浏览器登录态或账户数据。
