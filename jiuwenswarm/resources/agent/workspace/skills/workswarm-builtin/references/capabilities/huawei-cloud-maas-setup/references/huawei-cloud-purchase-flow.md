# 华为云 MaaS 参考文档

## 官方文档

- 快速入门: https://support.huaweicloud.com/qs-maas/qs-maas-0001.html
- MaaS 访问授权: https://support.huaweicloud.com/permission-maas/maas-modelarts-0016.html
- API 调用规范: https://support.huaweicloud.com/model-call-maas/model-call-017.html
- 模型服务价格: https://support.huaweicloud.com/price-maas/price-maas-0001.html

## 关键页面 URL

| 页面 | URL |
|------|-----|
| MaaS 控制台首页 | https://console.huaweicloud.com/modelarts/?region=cn-southwest-2#/model-studio/homepage |
| API Key 管理页面 | https://console.huaweicloud.com/modelarts/?region=cn-southwest-2#/model-studio/authmanage |
| 在线推理（预置服务） | https://console.huaweicloud.com/modelarts/?region=cn-southwest-2#/model-studio/deployment |
| 实名认证（个人支付宝） | https://account.huaweicloud.com/usercenter/?locale=zh-cn#/accountindex/realNameAuth |
| 费用中心首页（登录落地页） | https://account.huaweicloud.com/usercenter/?region=cn-southwest-2#/userindex/allview |
| 账户充值 | https://account.huaweicloud.com/usercenter/#/accountindex/balance |

> 以上 URL 均指定 `cn-southwest-2`（西南-贵阳一）区域，MaaS 预置服务仅支持该区域。

## 模型列表

模型列表定义在 `<skill_dir>/models.json` 中，修改该文件即可调整开通和写入的模型。
可用模型及对应 API 参数值请参考控制台「模型推理 > 在线推理」页面或
[API 调用规范文档](https://support.huaweicloud.com/model-call-maas/model-call-017.html)。

## 计费说明

- 开通后调用以实际用量进行扣费
- 未使用时不会产生费用
- 不同模型计费方式有所区别，详见: https://support.huaweicloud.com/price-maas/price-maas-0001.html

## API 调用示例

### OpenAI 兼容方式（推荐 jiuwenswarm 使用）

```python
from openai import OpenAI

base_url = "https://api.modelarts-maas.com/openai/v1"
api_key = "MAAS_API_KEY"

client = OpenAI(api_key=api_key, base_url=base_url)
response = client.chat.completions.create(
    model="qwen3-30b-a3b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "介绍下你自己"},
    ]
)
print(response.choices[0].message.content)
```

### 直接 API 方式

```
POST https://api.modelarts-maas.com/v2/chat/completions
Headers:
  Content-Type: application/json
  Authorization: Bearer <api_key>
Body:
  {"model": "qwen3-30b-a3b", "messages": [...]}
```

## 常见问题

### API Key 创建后需要等待多久才能生效？

API Key 在创建后不会立即生效，通常需要等待几分钟才能生效。

### 如何查看更多模型？

前往控制台「模型推理 > 在线推理」页面查看所有预置服务，或参考 API 调用规范文档。
