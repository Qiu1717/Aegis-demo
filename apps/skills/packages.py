"""外部技能文件/GitHub 导入；只保存内容，不执行脚本、不扩大 Agent 沙箱。"""
import hashlib
import io
import json
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from apps import paths
from . import services

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 30 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_RESOURCE_BYTES = 128 * 1024
GITHUB_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com"}
IMPORT_LOCK = threading.RLock()
RESOURCE_BOUNDARY = (
    "完整技能指令已注入。references/scripts/assets 的相对路径以此根目录为准；"
    "通过 read_skill_resource(skill_id, relative_path) 读取已启用包内的 UTF-8 文本资源（最多 128 KiB）。"
    "普通 Agent 文件工具仍受当前工作区沙箱约束，根目录在工作区外时不能直接读取资源。"
    "不要绕过沙箱或自动运行包内脚本。"
)


class PackageError(ValueError):
    pass


def _safe_path(name):
    # 同时拒绝 Windows 别名、ADS、反斜线，避免跨平台展开后指向另一个文件。
    if not name or "\\" in name or any(ord(c) < 32 for c in name) or name.startswith("/"):
        raise PackageError("技能包包含不安全路径")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or any(c in part for c in ':<>"|?*')
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *["COM%d" % i for i in range(1, 10)], *["LPT%d" % i for i in range(1, 10)]}):
            raise PackageError("技能包包含越界或不安全路径")
    return PurePosixPath(*parts)


def _is_link(path):
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def skills_root():
    root = paths.data_dir().resolve() / "skills"
    if _is_link(root):
        raise PackageError("技能目录不能是符号链接或重解析点")
    return root


def package_root(skill_id):
    if not services.SKILL_ID_OK.fullmatch(skill_id):
        raise PackageError("无效技能标识")
    root = skills_root() / skill_id
    if _is_link(root):
        raise PackageError("技能目录不能是符号链接或重解析点")
    return root


def _decode_document(content):
    if not content or len(content) > MAX_SKILL_BYTES:
        raise PackageError("SKILL.md 必须非空且不超过 1 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PackageError("SKILL.md 必须使用 UTF-8 编码")
    if not text.strip() or "\x00" in text:
        raise PackageError("SKILL.md 不是有效的文本指令")
    return text


def package_instructions(skill):
    root = package_root(skill["id"])
    doc = root / "SKILL.md"
    if _is_link(doc):
        raise PackageError("SKILL.md 不能是符号链接")
    try:
        with doc.open("rb") as stream:
            text = _decode_document(stream.read(MAX_SKILL_BYTES + 1))
    except OSError:
        raise PackageError("技能文件缺失或不可读，请重新导入后启用")
    return "技能资源根路径：%s\n资源读取 skill_id：%s\n%s\n\n%s" % (root, skill["id"], RESOURCE_BOUNDARY, text)


def read_resource(skill, relative_path):
    """受限文本读取；只处理导入时登记的文件，逐级拒绝链接，不执行任何内容。"""
    try:
        if not isinstance(relative_path, str):
            raise PackageError("资源路径必须为包内相对路径")
        rel = _safe_path(relative_path)
        if str(rel) != relative_path or relative_path not in skill.get("package_files", []):
            return {"ok": False, "error_code": "RESOURCE_NOT_IMPORTED", "reason": "资源不在导入包文件清单内"}
        root = package_root(skill["id"])
        target = root
        for part in rel.parts:
            target = target / part
            if _is_link(target):
                raise PackageError("技能资源路径包含符号链接或重解析点")
        target.resolve().relative_to(root.resolve())
        if not target.is_file():
            return {"ok": False, "error_code": "RESOURCE_NOT_FOUND", "reason": "包内资源不存在或不是文件"}
        if target.stat().st_size > MAX_RESOURCE_BYTES:
            return {"ok": False, "error_code": "RESOURCE_TOO_LARGE", "reason": "文本资源超过 128 KiB 读取上限"}
        with target.open("rb") as stream:
            content = stream.read(MAX_RESOURCE_BYTES + 1)
        if len(content) > MAX_RESOURCE_BYTES:
            return {"ok": False, "error_code": "RESOURCE_TOO_LARGE", "reason": "文本资源超过 128 KiB 读取上限"}
        try:
            text = content.decode("utf-8-sig")
            if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
                raise UnicodeError()
        except UnicodeError:
            return {"ok": False, "error_code": "RESOURCE_NOT_TEXT", "reason": "仅支持 UTF-8 文本资源，不读取二进制资产"}
        return {"ok": True, "skill_id": skill["id"], "path": relative_path,
                "resource_root": str(root), "content": text, "bytes": len(content), "reason": ""}
    except (PackageError, ValueError):
        return {"ok": False, "error_code": "RESOURCE_PATH_INVALID", "reason": "技能资源路径越界、不安全或包含链接"}
    except OSError:
        return {"ok": False, "error_code": "RESOURCE_READ_FAILED", "reason": "技能资源不可读"}


def delete_package(skill, remaining):
    with IMPORT_LOCK:
        root = package_root(skill["id"])
        if not root.exists():
            return services._save_skills(remaining)
        with tempfile.TemporaryDirectory(prefix=".delete-", dir=skills_root()) as temp:
            backup = Path(temp) / skill["id"]
            root.rename(backup)
            try:
                saved = services._save_skills(remaining)
            except Exception:
                backup.rename(root)
                raise
            if not saved:
                backup.rename(root)
            return saved


def _metadata(text, fallback):
    """只提取显示用 name/description/id；所有原始 frontmatter 和正文完整保留。"""
    metadata = {}
    lines = text.splitlines()
    if lines and lines[0] == "---":
        end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        if end is None:
            raise PackageError("SKILL.md 的 frontmatter 缺少结束分隔符")
        key, block, folded = None, [], False
        for line in lines[1:end] + ["__end__: "]:
            if key and line.startswith((" ", "\t")):
                block.append(line.strip())
                continue
            if key:
                metadata[key] = (" " if folded else "\n").join(block)
                key, block = None, []
            match = re.match(r"^(name|description|id):\s*(.*?)\s*$", line)
            if not match:
                continue
            field, value = match.groups()
            if value in ("|", ">", "|-", ">-", "|+", ">+"):
                key, folded = field, value.startswith(">")
            else:
                if value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip('"')
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1].replace("''", "'")
                metadata[field] = value
    name = str(metadata.get("name") or fallback or "Imported skill").strip()
    if not name or len(name) > 128:
        raise PackageError("技能名称必填且不超过 128 字")
    sid = str(metadata.get("id") or "").strip().lower()
    if not sid:
        sid = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:32]
        if not services.SKILL_ID_OK.fullmatch(sid):
            sid = "skill-" + hashlib.sha256(name.encode()).hexdigest()[:16]
    if not services.SKILL_ID_OK.fullmatch(sid):
        raise PackageError("SKILL.md 的 id 不合规")
    return sid, name, str(metadata.get("description") or "")


def _zip_files(content):
    files, seen, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            entries = z.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise PackageError("技能包文件/目录数量超过上限 %d" % MAX_ARCHIVE_ENTRIES)
            for info in entries:
                name = str(_safe_path(info.filename))
                key = name.casefold()
                if key in seen:
                    raise PackageError("技能包包含重复路径或大小写别名")
                seen.add(key)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise PackageError("技能包不能包含符号链接或特殊文件")
                if info.flag_bits & 1:
                    raise PackageError("不支持加密技能包")
                total += info.file_size
                if info.file_size > MAX_FILE_BYTES or total > MAX_EXPANDED_BYTES:
                    raise PackageError("技能包展开体积超过限制（单文件 2 MiB / 总计 30 MiB）")
                if not info.is_dir():
                    with z.open(info) as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                        raise PackageError("技能包文件体积不正确")
                    files[name] = data
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise PackageError("无效或不支持的 zip 技能包") from exc
    if not files:
        raise PackageError("技能包没有文件")
    file_keys = {k.casefold() for k in files}
    for name in seen:
        if any(str(parent).casefold() in file_keys for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise PackageError("技能包文件与目录路径冲突")
    return files


def _packages(files, selected=""):
    if selected:
        prefix = selected.rstrip("/") + "/"
        files = {k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)}
    documents = sorted(k for k in files if PurePosixPath(k).name == "SKILL.md")
    if "SKILL.md" in files:
        documents = ["SKILL.md"]
    else:
        documents = [k for k in documents if not {"scripts", "references", "assets"}.intersection(PurePosixPath(k).parts[:-1])]
        # 嵌套 SKILL.md 属于上层包的完整资源，避免重复展开导致体积倍增。
        documents = [k for k in documents if not any(
            k.startswith(str(PurePosixPath(other).parent) + "/")
            for other in documents if other != k
        )]
    if not documents:
        raise PackageError("下载/文件解析完成，但未找到 SKILL.md，技能未注册")
    if len(documents) > services.MAX_SKILLS:
        raise PackageError("技能数量超过上限")
    result, ids = [], set()
    for document in documents:
        parent = PurePosixPath(document).parent
        prefix = "" if str(parent) == "." else str(parent) + "/"
        text = _decode_document(files[document])
        sid, name, description = _metadata(text, parent.name)
        if sid in ids:
            raise PackageError("技能包包含重复技能标识：%s" % sid)
        ids.add(sid)
        resources = {k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)}
        result.append(({"id": sid, "name": name, "description": description,
                        "instructions": text, "type": "prompt", "package": True,
                        "enabled": False, "built_in": False, "created_at": services._now()}, resources))
    return result


def _install(packages, overwrite, source):
    """预检全部包，临时目录准备，配置失败恢复旧目录；成功后才报告导入。"""
    skills = services._load_skills()
    existing = {s["id"]: s for s in skills}
    for entry, _ in packages:
        sid = entry["id"]
        old = existing.get(sid)
        if old and (old.get("built_in") or not overwrite):
            raise PackageError("技能标识已存在，未覆盖：%s" % sid)
        target = package_root(sid)
        if target.exists() and not (old and old.get("package") and overwrite):
            raise PackageError("本地技能目录已存在，未覆盖：%s" % sid)
    new_ids = {entry["id"] for entry, _ in packages}
    if len(skills) + len(new_ids - existing.keys()) > services.MAX_SKILLS:
        raise PackageError("技能数量已达上限")
    root = skills_root()
    root.mkdir(parents=True, exist_ok=True)
    moved, backups = [], []
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as temp:
        stage = Path(temp)
        try:
            for entry, files in packages:
                sid = entry["id"]
                entry["source"] = source
                entry["package_files"] = sorted(files)
                prepared = stage / sid
                prepared.mkdir()
                for name, data in files.items():
                    target = prepared.joinpath(*_safe_path(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                target = package_root(sid)
                if target.exists():
                    backup = stage / (sid + ".backup")
                    target.rename(backup)
                    backups.append((target, backup))
                prepared.rename(target)
                moved.append(target)
            updated = [s for s in skills if s["id"] not in new_ids] + [entry for entry, _ in packages]
            if not services._save_skills(updated):
                raise PackageError("技能已下载/解析，但写入注册配置失败，已回滚，未应用")
        except Exception:
            for target in reversed(moved):
                shutil.rmtree(target)
            for target, backup in reversed(backups):
                backup.rename(target)
            raise
    return {"ok": True, "imported": [{"id": e["id"], "name": e["name"],
             "resource_root": str(package_root(e["id"]))} for e, _ in packages],
            "total": len(packages), "skipped": [], "reason": "", "enabled": False,
            "resource_boundary": RESOURCE_BOUNDARY}


def import_file(stream, filename, overwrite=False):
    content = stream.read(MAX_DOWNLOAD_BYTES + 1)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise PackageError("技能文件超过下载/上传上限 10 MiB")
    suffix = Path(filename or "").suffix.lower()
    with IMPORT_LOCK:
        if suffix == ".json":
            try:
                data = json.loads(content.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                raise PackageError("文件不是合法的 UTF-8 JSON")
            items, error = services.normalize_import(data)
            if error:
                raise PackageError(error)
            result = services.import_skills([
                dict(item, enabled=False) if isinstance(item, dict) else item for item in items
            ], overwrite=overwrite)
            result["enabled"] = False
            return result
        if suffix == ".md" and Path(filename).name.upper() == "SKILL.MD":
            files = {"SKILL.md": content}
        elif suffix == ".zip":
            files = _zip_files(content)
        else:
            raise PackageError("仅支持 JSON、SKILL.md 和 zip 技能包")
        return _install(_packages(files), overwrite, "local")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PackageError("GitHub HTTP %d 重定向已拒绝" % code)


def _validated_url(url, api_ref=False):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in GITHUB_HOSTS
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or (parsed.query and not (api_ref and parsed.hostname == "api.github.com"
                    and re.fullmatch(r"ref=[0-9a-fA-F]{40}", parsed.query)))
                or parsed.fragment or any(ord(c) < 33 for c in url)
                or "\\" in url):
            raise ValueError()
    except (ValueError, TypeError):
        raise PackageError("仅允许无凭据、无查询参数的官方 GitHub HTTPS 链接")
    return parsed


def _download(url, limit, deadline, accept=None):
    _validated_url(url, api_ref=True)
    if time.monotonic() >= deadline:
        raise PackageError("GitHub 下载总时限超过 60 秒")
    req = urllib.request.Request(url, headers={"User-Agent": "Aegis-Skills-Importer",
                                             "Accept": accept or ("application/vnd.github+json" if "api.github.com/" in url else "application/zip")})
    # 禁止环境代理和任何重定向；不读取 netrc、用户 token 或其他凭据。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=min(15, max(1, deadline - time.monotonic()))) as resp:
        if resp.geturl() != url or getattr(resp, "status", 200) != 200:
            raise PackageError("GitHub 下载响应或重定向不符合要求")
        length = resp.headers.get("Content-Length")
        if length and int(length) > limit:
            raise PackageError("GitHub 下载体积超过限制")
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise PackageError("GitHub 下载总时限超过 60 秒")
            chunk = resp.read(min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise PackageError("GitHub 下载体积超过限制")
        return b"".join(chunks)


def _api(url, deadline):
    content = _download(url, MAX_SKILL_BYTES, deadline)
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise PackageError("GitHub API 返回了无效 JSON")
    if not isinstance(value, dict):
        raise PackageError("GitHub API 返回格式错误")
    return value


def _selected_files(owner, repo, sha, selected, deadline):
    """子目录链接只下载所选目录，不为几 KiB 的技能拉取整个仓库。"""
    files, total, requests_count, seen_paths = {}, 0, 0, set()
    pending = [selected]
    while pending:
        directory = pending.pop()
        requests_count += 1
        if requests_count > 100:
            raise PackageError("技能目录过大，最多读取 100 个目录/文件请求")
        url = "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (
            owner, repo, urllib.parse.quote(directory, safe="/"), sha)
        try:
            entries = json.loads(_download(url, MAX_SKILL_BYTES, deadline))
        except (ValueError, UnicodeDecodeError):
            raise PackageError("GitHub 目录响应不是有效 JSON")
        if not isinstance(entries, list):
            raise PackageError("GitHub 链接未指向有效技能目录")
        if len(entries) + len(files) + len(pending) > MAX_ARCHIVE_ENTRIES:
            raise PackageError("技能目录条目过多")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise PackageError("GitHub 目录条目格式无效")
            path = str(_safe_path(entry["path"]))
            expected = (directory + "/" if directory else "") + str(entry.get("name") or "")
            if path.casefold() in seen_paths:
                raise PackageError("技能目录包含重复路径或大小写别名")
            seen_paths.add(path.casefold())
            if path != expected or entry.get("target") or entry.get("submodule_git_url"):
                raise PackageError("GitHub 条目路径越界或包含链接")
            if entry.get("type") == "dir":
                pending.append(path)
            elif entry.get("type") == "file":
                size = entry.get("size")
                if type(size) is not int or size < 0 or size > MAX_FILE_BYTES:
                    raise PackageError("GitHub 技能资源超过单文件限制")
                requests_count += 1
                if requests_count > 100:
                    raise PackageError("技能目录过大，最多读取 100 个目录/文件请求")
                file_url = "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (
                    owner, repo, urllib.parse.quote(path, safe="/"), sha)
                content = _download(file_url, MAX_FILE_BYTES, deadline,
                                    accept="application/vnd.github.raw+json")
                total += len(content)
                if total > MAX_EXPANDED_BYTES:
                    raise PackageError("技能资源总体积超过限制")
                files[path] = content
            else:
                raise PackageError("技能目录包含符号链接或子模块，未导入")
    return files


def import_github(url, overwrite=False):
    parsed = _validated_url(url)
    if parsed.hostname != "github.com":
        raise PackageError("请提供 github.com 的仓库、tree 子目录或 blob SKILL.md 页面链接")
    parts = list(_safe_path(urllib.parse.unquote(parsed.path.lstrip("/").rstrip("/"))).parts)
    if len(parts) < 2 or len(parts) > 14:
        raise PackageError("GitHub 仓库链接无效或路径过深")
    owner, repo = parts[:2]
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not re.fullmatch(r"[A-Za-z0-9_-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise PackageError("GitHub 仓库标识无效")
    mode = parts[2] if len(parts) > 2 else "repo"
    tail = parts[3:]
    if mode not in ("repo", "tree", "blob") or (mode != "repo" and not tail):
        raise PackageError("仅支持 GitHub 仓库、tree 子目录和 blob SKILL.md 链接")
    if mode == "blob" and (len(tail) < 2 or tail[-1] != "SKILL.md"):
        raise PackageError("blob 链接必须指向 SKILL.md")
    deadline = time.monotonic() + 60
    api = "https://api.github.com/repos/%s/%s" % (owner, repo)
    selected, sha = "", ""
    if mode == "repo":
        metadata = _api(api, deadline)
        branch = metadata.get("default_branch")
        if metadata.get("private") or not isinstance(branch, str) or not branch:
            raise PackageError("仅支持有默认分支的公开 GitHub 仓库")
        sha = _api(api + "/commits/" + urllib.parse.quote(branch, safe=""), deadline).get("sha", "")
    else:
        # 分支可以含斜线；只有明确的“当前 ref 不存在”才尝试更短 ref。
        maximum = len(tail) - (1 if mode == "blob" else 0)
        for count in range(maximum, 0, -1):
            ref = "/".join(tail[:count])
            try:
                sha = _api(api + "/commits/" + urllib.parse.quote(ref, safe=""), deadline).get("sha", "")
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 422):
                    try:
                        error = json.loads(exc.read(4096))
                    except (ValueError, UnicodeDecodeError):
                        error = {}
                    if isinstance(error, dict) and error.get("message") == "No commit found for SHA: " + ref:
                        continue
                raise
            selected = "/".join(tail[count:-1] if mode == "blob" else tail[count:])
            break
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise PackageError("无法解析 GitHub 分支/提交")
    if mode != "repo":
        files = _selected_files(owner, repo, sha, selected, deadline)
        if mode == "blob" and ((selected + "/" if selected else "") + "SKILL.md") not in files:
            raise PackageError("blob 链接指向的 SKILL.md 不存在，技能未注册")
        with IMPORT_LOCK:
            return _install(_packages(files, selected), overwrite, url)
    content = _download("https://codeload.github.com/%s/%s/zip/%s" % (owner, repo, sha), MAX_DOWNLOAD_BYTES, deadline)
    files = _zip_files(content)
    tops = {PurePosixPath(k).parts[0] for k in files}
    if len(tops) != 1 or any(len(PurePosixPath(k).parts) < 2 for k in files):
        raise PackageError("GitHub 仓库压缩包结构无效")
    files = {k.split("/", 1)[1]: v for k, v in files.items()}
    if mode == "blob" and ((selected + "/" if selected else "") + "SKILL.md") not in files:
        raise PackageError("blob 链接指向的 SKILL.md 不存在，技能未注册")
    with IMPORT_LOCK:
        return _install(_packages(files, selected), overwrite, url)
