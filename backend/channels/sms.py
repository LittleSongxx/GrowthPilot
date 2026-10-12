"""下载短信的教学模板；由会话服务记录模拟发送，不连接短信供应商。"""

APP_DOWNLOADS = {
    "meituan_rider": ("美团骑手 App", "https://download.example.com/meituan-rider"),
    "meituan_merchant": ("美团外卖商家 App", "https://download.example.com/meituan-merchant"),
    "didi_driver": ("滴滴司机 App", "https://download.example.com/didi-driver"),
    "financial_consultation": ("度小满 App", "https://download.example.com/duxiaoman"),
}


def download_message(scenario_id: str) -> dict:
    """按业务选择固定模板，示例链接只用于本地发送记录，不作为政策证据。"""
    app_name, download_url = APP_DOWNLOADS[scenario_id]
    return {
        "app_name": app_name,
        "download_url": download_url,
        "text": f"您咨询的{app_name}下载入口：{download_url}。请按页面提示下载和注册。",
    }
