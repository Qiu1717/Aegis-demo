/**
 * Aegis — 本地鉴权与请求封装
 *
 * 后端 main.py 的 before_request 会对所有 /api/* 校验 X-Aegis-Token，
 * token 由 /token.js（仅 127.0.0.1 可访问）下发，不落任何配置文件。
 *
 * 对外暴露：
 *   window.AEGIS_TOKEN                 由 /token.js 注入的随机 token
 *   window.aegisFetch(url, opts)       自动带鉴权头的 fetch（返回 Response）
 *   window.aegisFetchJSON(url, opts)   fetch + 解析 {code,msg,data}，非 0 抛错
 *   window.AegisAuth                   统一命名空间（getToken / setToken / headers ...）
 *
 * 依赖：common.js（showToast）。若 common.js 未加载，401 提示退化为 console 输出。
 */
(function () {
    "use strict";

    /** /token.js 注入的本地 token（未注入时为空串，请求将收到 401） */
    var TOKEN = (typeof window.AEGIS_TOKEN === "string" && window.AEGIS_TOKEN)
        ? window.AEGIS_TOKEN
        : "";

    /** 401 回调集合：业务方可注册自己的处理逻辑（如弹「前往设置」） */
    var unauthorizedHandlers = [];

    function notify(msg) {
        if (typeof window.showToast === "function") {
            window.showToast(msg, "error", 4000);
        } else if (window.console && window.console.warn) {
            window.console.warn(msg);
        }
    }

    /**
     * 合并默认请求头：注入鉴权头与 Content-Type。
     * 调用方传入的 headers 优先级更高（不会被覆盖成错的 Content-Type 除外）。
     */
    function buildHeaders(opts) {
        var headers = {};
        var incoming = (opts && opts.headers) || {};

        // 支持 Headers 实例 / 二维数组 / 普通对象三种写法
        if (typeof Headers !== "undefined" && incoming instanceof Headers) {
            incoming.forEach(function (value, key) {
                headers[key] = value;
            });
        } else if (Array.isArray(incoming)) {
            incoming.forEach(function (pair) {
                headers[pair[0]] = pair[1];
            });
        } else {
            Object.keys(incoming).forEach(function (k) {
                headers[k] = incoming[k];
            });
        }

        if (TOKEN && !headers["X-Aegis-Token"] && !headers["x-aegis-token"]) {
            headers["X-Aegis-Token"] = TOKEN;
        }
        var isForm = (typeof FormData !== "undefined") && (opts && opts.body instanceof FormData);
        if (!isForm && !headers["Content-Type"] && !headers["content-type"]) {
            headers["Content-Type"] = "application/json";
        }
        return headers;
    }

    /**
     * 带鉴权的 fetch。
     * 与原生 fetch 行为一致（返回 Response，不主动抛 HTTP 错误），
     * 仅在 401 时统一提示，便于调用方保留既有错误处理逻辑。
     *
     * @param {string} url
     * @param {object} [opts] 原生 fetch 的 init 对象
     * @returns {Promise<Response>}
     */
    function aegisFetch(url, opts) {
        var init = Object.assign({}, opts || {});
        init.headers = buildHeaders(init);
        init.credentials = init.credentials || "same-origin";

        return fetch(url, init).then(function (resp) {
            if (resp && resp.status === 401) {
                notify("本地鉴权已失效，请重启 Aegis 后重试");
                unauthorizedHandlers.forEach(function (fn) {
                    try {
                        fn(resp, url);
                    } catch (e) {
                        /* 单个回调异常不影响其他回调 */
                    }
                });
            }
            return resp;
        });
    }

    /**
     * 带鉴权的 JSON 请求：自动解析后端统一响应 {code, msg, data}。
     * code !== 0 时抛出 Error(msg)，与 common.js 的 API.request 语义一致。
     *
     * @returns {Promise<*>} data 字段
     */
    function aegisFetchJSON(url, opts) {
        return aegisFetch(url, opts).then(function (resp) {
            return resp.json().then(function (data) {
                if (!data || Number(data.code) !== 0) {
                    throw new Error((data && data.msg) || "请求失败");
                }
                return data.data;
            });
        });
    }

    /**
     * 兼容层：让**未改造的旧页面**自动带上鉴权头。
     *
     * 背景：main.py 的 before_request 对 /api/* 强制校验 X-Aegis-Token，
     * 而 common.js 的 API.request 与部分页面的原生 fetch 并不知道 token。
     * 在 main.js 等旧脚本完成 aegisFetch 改造之前，这里统一兜底，
     * 保证「开启鉴权」不回归现有功能。
     *
     * 影响范围被严格限制：
     *   · 只给同源且以 /api/ 开头的请求注入；
     *   · 之前已带过该头的请求不再覆盖；
     *   · 任何异常都退回原生 fetch / 原实现。
     */
    function shouldInject(url) {
        if (!TOKEN) return false;
        if (typeof url !== "string") return false;
        if (url.indexOf("/api/") === 0) return true;
        try {
            var parsed = new URL(url, window.location.href);
            return parsed.origin === window.location.origin
                && parsed.pathname.indexOf("/api/") === 0;
        } catch (e) {
            return false;
        }
    }

    function patchLegacyApi() {
        var API = window.API;
        if (!API || typeof API.request !== "function" || API.__aegisPatched) return;
        API.request = function (url, options) {
            var opts = Object.assign({ headers: { "Content-Type": "application/json" } },
                options || {});
            if (opts.body && typeof opts.body === "object") {
                opts.body = JSON.stringify(opts.body);
            }
            if (shouldInject(url)) {
                opts.headers = buildHeaders(opts);
            }
            return fetch(url, opts).then(function (resp) {
                if (resp && resp.status === 401) {
                    notify("本地鉴权已失效，请重启 Aegis 后重试");
                }
                return resp.json();
            }).then(function (data) {
                if (!data || Number(data.code) !== 0) {
                    throw new Error((data && data.msg) || "请求失败");
                }
                return data.data;
            });
        };
        API.__aegisPatched = true;
    }

    function patchGlobalFetch() {
        if (window.__aegisFetchPatched) return;
        var nativeFetch = window.fetch;
        if (typeof nativeFetch !== "function") return;

        window.fetch = function (input, init) {
            var url = (typeof input === "string") ? input
                : (input && input.url) ? input.url : "";
            if (!shouldInject(url)) {
                return nativeFetch.apply(window, arguments);
            }
            try {
                if (typeof input === "string") {
                    var opts = Object.assign({}, init || {});
                    opts.headers = buildHeaders(opts);
                    return nativeFetch.call(window, input, opts);
                }
                // Request 对象：其 headers 不可变，重建一个等价的 Request
                var headers = {};
                if (input.headers && typeof input.headers.forEach === "function") {
                    input.headers.forEach(function (v, k) { headers[k] = v; });
                }
                var merged = buildHeaders({ headers: headers });
                var cloned = new Request(input, { headers: merged });
                return nativeFetch.call(window, cloned, init);
            } catch (e) {
                return nativeFetch.apply(window, arguments);
            }
        };
        window.__aegisFetchPatched = true;
    }

    function installCompatLayer() {
        try {
            patchLegacyApi();
            patchGlobalFetch();
        } catch (e) {
            /* 兼容层失败不影响页面其它逻辑 */
        }
    }

    var AegisAuth = {
        /** 读取当前 token（只读副本语义） */
        getToken: function () {
            return TOKEN;
        },
        /** 运行时替换 token（桌面壳通过其它通道下发时使用） */
        setToken: function (value) {
            TOKEN = (typeof value === "string") ? value : "";
            if (typeof window.AEGIS_TOKEN !== "undefined") {
                window.AEGIS_TOKEN = TOKEN;
            }
        },
        /** 构造带鉴权头的 headers 对象（供 XMLHttpRequest / EventSource 之外场景使用） */
        headers: function (extra) {
            return buildHeaders({ headers: extra || {} });
        },
        /** 注册 401 回调 */
        onUnauthorized: function (fn) {
            if (typeof fn === "function") {
                unauthorizedHandlers.push(fn);
            }
        },
        fetch: aegisFetch,
        fetchJSON: aegisFetchJSON,
        /** 便捷方法 */
        get: function (url) {
            return aegisFetchJSON(url, { method: "GET" });
        },
        post: function (url, body) {
            return aegisFetchJSON(url, {
                method: "POST",
                body: body === undefined ? undefined : JSON.stringify(body),
            });
        },
        put: function (url, body) {
            return aegisFetchJSON(url, {
                method: "PUT",
                body: body === undefined ? undefined : JSON.stringify(body),
            });
        },
        del: function (url, body) {
            return aegisFetchJSON(url, {
                method: "DELETE",
                body: body === undefined ? undefined : JSON.stringify(body),
            });
        },
    };

    window.AegisAuth = AegisAuth;
    window.aegisFetch = aegisFetch;
    window.aegisFetchJSON = aegisFetchJSON;

    // common.js（API.request）在部分页面里晚于 auth.js 加载，
    // 因此除了立即尝试一次，还要在 DOMContentLoaded 时再补一次。
    installCompatLayer();
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installCompatLayer);
    }
})();
