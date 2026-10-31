"""E-Mobile REST 客户端(`{服务器}/emp/**`,鉴权头 access_token + emaccesstk)。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

#: 兜底 UA:探测不到客户端版本时用它。会随 `session.rest` 创建时的
#: profile.user_agent 被覆盖(7.1.4 与 7.2.6 需分别报告自己的版本,
#: 冒用 7.2.6 在旧客户端上无必要地引人怀疑)。
USER_AGENT = "E-Mobile/unknown"


class EMError(RuntimeError):
    """REST 调用失败。"""


class EMRestClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0,
                 session: Optional[requests.Session] = None,
                 user_agent: Optional[str] = None) -> None:
        if not base_url:
            raise EMError("base_url 未配置")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._http = session or requests.Session()
        # UA 跟着真实客户端版本走,不冒用别的版本
        ua = (user_agent or USER_AGENT).strip()
        self.user_agent = ua if ua.lower().startswith("e-mobile/") \
            and ua != "E-Mobile/" else USER_AGENT

    # ---- 请求 ---- #
    def call(self, path: str, method: str = "GET",
             params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + "/emp" + path
        query = dict(params or {})
        query.setdefault("getTime", int(time.time() * 1000))
        query.setdefault("client_type", 1)
        headers = {
            "access_token": self.token,
            "emaccesstk": self.token,
            "User-Agent": self.user_agent,
            "Referer": self.base_url + "/client/main/",
        }
        try:
            resp = self._http.request(
                method.upper(), url, params=query, json=body,
                headers=headers, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise EMError(f"请求 {method} {url} 失败: {exc}") from exc

        if resp.status_code != 200:
            raise EMError(f"{method} {url} -> HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise EMError(f"{url} 返回非 JSON: {resp.text[:200]}") from exc

        errcode = data.get("errcode", 0)
        if errcode not in (0, "0", None):
            raise EMError(f"{url} -> errcode={errcode}, errmsg={data.get('errmsg')!r}")
        return data

    get = lambda self, path, **kw: self.call(path, "GET", **kw)   # noqa: E731
    post = lambda self, path, body=None, **kw: self.call(         # noqa: E731
        path, "POST", body=body, **kw)

    # ---- 业务封装 ---- #
    def get_im_params(self) -> Dict[str, Any]:
        """取 IM 连接参数(消息服务器/token/域/udid)。"""
        return self.call("/api/msg/gettoken")

    def passport_alive(self) -> Dict[str, Any]:
        return self.call("/api/passport/alive")

    def corpid(self) -> str:
        """取当前主企业 corpid(搜索人/部门时要用)。"""
        data = self.tenant_list()
        for tenant in data.get("tenantlist") or []:
            if str(tenant.get("id")) == str(data.get("main_tenant_id")):
                return str(tenant.get("corpid") or "")
        tenants = data.get("tenantlist") or []
        return str(tenants[0].get("corpid") or "") if tenants else ""

    def user_search(self, keyword: str, limit: int = 20,
                    corpid: Optional[str] = None) -> Dict[str, Any]:
        """搜人(组织/通讯录)。参数为前端同款:{keyword, corpid, search_dept}"""
        return self.get("/api/user/client/search",
                        params={"keyword": keyword,
                                "corpid": corpid or self.corpid(),
                                "search_dept": 1, "count": limit,
                                "pageNumber": 1})

    def baseuser_search(self, keyword: str, search_type: str = "",
                        limit: int = 20) -> Dict[str, Any]:
        """按姓名/拼音搜人和群(不需要 corpid)。"""
        return self.get("/api/baseuser/search",
                        params={"keyword": keyword, "search_type": search_type,
                                "count": limit, "pageNumber": 1})

    def user_infos(self, user_ids: List[str]) -> Dict[str, Any]:
        """批量取用户详情(姓名/部门/职务)。"""
        return self.post("/api/msg/userinfo/simplelist", {"userlist": user_ids})

    def group_list(self, limit: int = 200) -> Dict[str, Any]:
        return self.get("/api/msg/group/mylist",
                        params={"count": limit, "pageNumber": 1})

    def tenant_list(self) -> Dict[str, Any]:
        return self.get("/api/tenant/joinlist")
