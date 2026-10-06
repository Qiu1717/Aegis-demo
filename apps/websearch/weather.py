"""
weather 子模块：天气类问题的实时数据快捷通道

背景：搜索引擎摘要对天气查询往往只返回「网站介绍」而非实际天气数值，
模型拿到后仍无法回答。本模块在命中天气意图时，直接调用 Open-Meteo
（免费、免密钥）获取实时天气并生成可注入 system 的参考文本。

流程：问题文本 -> 提取城市名 -> Open-Meteo Geocoding -> Forecast API -> 中文天气简报
任何一步失败都返回 None（由上层继续走通用搜索，互不影响）。
"""
import re

import requests

from apps import logger

_WX_HINT_RE = re.compile(r"天气|气温|温度|降雨|下雨|下雪|降雪|湿度|风力|风向|weather", re.I)

# 从问题中剔除这些词后，剩余部分视作城市名
_CITY_STRIP_RE = re.compile(
    r"今天|今日|现在|当前|此刻|实时|最新|最近|"
    r"天气预报|天气|气温|温度|降雨|下雨|下雪|降雪|湿度|风力|风向|天气情况|情况|"
    r"怎么样|怎样|如何|多少|多少度|几度|预报|预测|查询|查一下|看看|请问|告诉|一下|"
    r"适宜|适合|穿什么|出行|"
    r"weather|forecast|today|now|current|in|the|"
    r"[的了吗呢吧呢么？?！!。，,\s]",
    re.I,
)

# 句末语气词：「武汉呀」「北京呢」这类写法会让地理编码查不到城市，必须剔除
_PARTICLE_RE = re.compile(r"[呀哦呢吧啊嘛哟喔啦唉哎喂么吗嘞]+$")

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WX_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code -> 中文描述
_WMO = {
    0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "冻毛毛雨", 57: "浓冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

_DIR8 = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


def _wmo_desc(code):
    if code is None:
        return ""
    return _WMO.get(int(code), "未知")


def _wind_dir(deg):
    try:
        return _DIR8[int((float(deg) + 22.5) % 360 // 45)]
    except (TypeError, ValueError):
        return ""


def _extract_city(query):
    """从问句中抽取城市名：先剥离意图词，再剥离句末语气词。"""
    city = _CITY_STRIP_RE.sub("", query or "").strip()
    city = _PARTICLE_RE.sub("", city).strip()
    return city[:20] if city else ""


def _geocode(city, timeout):
    """城市名 -> (lat, lon, 规范名)；查不到返回 None。"""
    resp = requests.get(
        _GEO_URL,
        params={"name": city, "count": 1, "language": "zh", "format": "json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    top = results[0]
    return top["latitude"], top["longitude"], (top.get("name") or city)


def fetch_weather_text(query, timeout=8):
    """命中天气意图时返回一段实时天气简报；否则/失败返回 None。"""
    if not query or not _WX_HINT_RE.search(query):
        return None
    city = _extract_city(query)
    if not city:
        return None
    try:
        # 城市名仍可能带残留尾缀（如「武汉呀」漏网），地理编码失败时逐字回退一次
        cands = [city]
        if len(city) >= 3:
            cands.append(city[:-1])
        geo_timeout = min(timeout, 5)
        geo = None
        for c in cands:
            geo = _geocode(c, geo_timeout)
            if geo:
                break
        if not geo:
            return None
        lat, lon, name = geo

        wx_resp = requests.get(
            _WX_URL,
            params={
                "latitude": lat, "longitude": lon,
                "current": ("temperature_2m,apparent_temperature,"
                            "relative_humidity_2m,weather_code,"
                            "wind_speed_10m,wind_direction_10m"),
                "daily": ("temperature_2m_max,temperature_2m_min,"
                          "weather_code,precipitation_probability_max"),
                "timezone": "auto", "forecast_days": 1,
            },
            timeout=timeout,
        )
        wx_resp.raise_for_status()
        wx = wx_resp.json()

        cur = wx.get("current") or {}
        daily = wx.get("daily") or {}
        parts = [f"{name}实时天气（数据时间 {cur.get('time', '')}，当地时间）："]
        if cur.get("temperature_2m") is not None:
            parts.append(f"当前气温 {cur['temperature_2m']}°C"
                         f"（体感 {cur.get('apparent_temperature')}°C）")
        desc = _wmo_desc(cur.get("weather_code"))
        if desc:
            parts.append(f"天气{desc}")
        if cur.get("relative_humidity_2m") is not None:
            parts.append(f"湿度 {cur['relative_humidity_2m']}%")
        if cur.get("wind_speed_10m") is not None:
            parts.append(f"风速 {cur['wind_speed_10m']} km/h"
                         + (f"（{_wind_dir(cur.get('wind_direction_10m'))}风）"
                            if cur.get("wind_direction_10m") is not None else ""))
        dmax = (daily.get("temperature_2m_max") or [None])[0]
        dmin = (daily.get("temperature_2m_min") or [None])[0]
        if dmax is not None and dmin is not None:
            parts.append(f"今日气温 {dmin}°C ~ {dmax}°C")
        dcode = (daily.get("weather_code") or [None])[0]
        if dcode is not None:
            dd = _wmo_desc(dcode)
            if dd:
                parts.append(f"今日天气{dd}")
        dprec = (daily.get("precipitation_probability_max") or [None])[0]
        if dprec is not None:
            parts.append(f"今日降水概率 {dprec}%")

        text = "，".join(parts) + "。以上为实时气象数据，请直接据此回答用户的天气问题。"
        return text
    except Exception as e:
        logger.warning("实时天气获取失败（已忽略，city=%s）: %s", city, e)
        return None
