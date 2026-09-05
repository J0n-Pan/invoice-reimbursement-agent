"""财务系统适配层占位。

后期接入财务系统时，只需要在这里实现 register()，不需要改动 Agent 主流程。
"""


def register(invoice: dict) -> dict:
    """将已通过的发票写入财务系统。

    当前不连接外部系统，仅返回待接入状态，避免误写真实财务数据。
    """
    return {
        "success": False,
        "status": "待接入",
        "message": "财务系统适配器尚未配置",
        "invoice_id": invoice.get("id"),
    }

