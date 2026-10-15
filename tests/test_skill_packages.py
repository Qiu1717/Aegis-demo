"""外部技能导入契约：临时用户目录、真实注册/注入，网络只替换传输层。"""
import io
import json
import stat
import urllib.error
import urllib.request
import zipfile

import pytest
from flask import Flask

from apps.agent.prompts import build_skills_section
from apps.agent.sandbox import SandboxError, open_workspace
from apps.skills import services as S
from apps.skills.routes import skills_bp


DOC = "---\nname: sample-skill\ndescription: |\n  Read references before answering.\n  Keep examples precise.\n---\n\n# Instructions\nUse references/guide.md.\n" + "完整指令。" * 1000
SHA = "a" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path / "data"))
    app = Flask(__name__)
    app.register_blueprint(skills_bp)
    app.config["TESTING"] = True
    return app.test_client()


def archive(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def upload(client, content, name="package.zip", **fields):
    response = client.post("/api/skills/import/file", data={
        "file": (io.BytesIO(content), name), **fields,
    })
    result = response.get_json()
    assert result is not None, "缺少 multipart 技能导入接口"
    return result


def custom_skills():
    return [s for s in S.list_skills() if not s["built_in"]]


class Response(io.BytesIO):
    def __init__(self, content, url, status=200, headers=None):
        super().__init__(content)
        self.url = url
        self.status = status
        self.code = status
        self.msg = "Found" if status == 302 else "OK"
        from email.message import Message
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


def github_transport(monkeypatch, files, branch="trunk", refs=None, error=None, missing_ref_status=404):
    refs = refs or {branch: SHA}
    payload = archive({"repo-snapshot/" + k: v for k, v in files.items()})

    def open_request(self, req, *args, **kwargs):
        url = req.full_url
        assert not req.get_header("Authorization"), "公开下载不得携带凭据"
        if error:
            return error(url)
        if url == "https://api.github.com/repos/owner/repo":
            body = json.dumps({"default_branch": branch, "private": False}).encode()
        elif url.startswith("https://api.github.com/repos/owner/repo/commits/"):
            from urllib.parse import unquote
            ref = unquote(url.rsplit("/", 1)[1])
            if ref not in refs:
                body = json.dumps({"message": "No commit found for SHA: " + ref,
                                   "documentation_url": "https://docs.github.com/rest/commits/commits#get-a-commit",
                                   "status": str(missing_ref_status)}).encode()
                raise urllib.error.HTTPError(url, missing_ref_status, "Not Found", {}, io.BytesIO(body))
            body = json.dumps({"sha": refs[ref]}).encode()
        elif url == "https://codeload.github.com/owner/repo/zip/" + SHA:
            body = payload
        elif url.startswith("https://api.github.com/repos/owner/repo/contents/"):
            from urllib.parse import unquote, urlsplit
            requested = unquote(urlsplit(url).path.split("/contents/", 1)[1]).strip("/")
            if req.get_header("Accept") == "application/vnd.github.raw+json":
                content = files[requested]
                return Response(content.encode() if isinstance(content, str) else content, url)
            prefix = unquote(urlsplit(url).path.split("/contents/", 1)[1]).strip("/") + "/"
            entries = {}
            for name, content in files.items():
                if name.startswith(prefix):
                    relative = name[len(prefix):]
                    child, _, tail = relative.partition("/")
                    entries[child] = {"type": "dir" if tail else "file", "name": child,
                                      "path": prefix + child, "size": len(content.encode() if isinstance(content, str) else content)}
            body = json.dumps(list(entries.values())).encode()
        elif url.startswith("https://raw.githubusercontent.com/owner/repo/" + SHA + "/"):
            from urllib.parse import unquote
            content = files[unquote(url.split(SHA + "/", 1)[1])]
            body = content.encode() if isinstance(content, str) else content
        else:
            raise AssertionError("非预期 GitHub 请求：" + url)
        return Response(body, url)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)


def github(client, url="https://github.com/owner/repo", **fields):
    result = client.post("/api/skills/import/github", json={"url": url, **fields}).get_json()
    assert result is not None, "缺少 GitHub 技能导入接口"
    return result


def test_markdown_persists_full_content_disabled_then_injects(client, tmp_path):
    result = upload(client, DOC.encode(), "SKILL.md")
    assert result["code"] == 0, result
    skill = custom_skills()[0]
    assert skill["enabled"] is False
    assert S.get_enabled_skills() == []
    root = tmp_path / "data" / "skills" / skill["id"]
    assert (root / "SKILL.md").read_bytes() == DOC.encode()
    assert client.put("/api/skills/" + skill["id"], json={"enabled": True}).get_json()["code"] == 0
    prompt = build_skills_section(S.get_enabled_skills())
    assert DOC in prompt
    assert str(root.resolve()) in prompt
    assert "工作区" in prompt and "沙箱" in prompt


def test_zip_preserves_resources_without_running_scripts(client, tmp_path):
    files = {"sample/SKILL.md": DOC, "sample/references/guide.md": "REFERENCE",
             "sample/scripts/run.py": "raise RuntimeError('must never execute')",
             "sample/assets/icon.bin": b"\x00\xff\x01"}
    result = upload(client, archive(files))
    assert result["code"] == 0, result
    skill = custom_skills()[0]
    root = tmp_path / "data" / "skills" / skill["id"]
    assert (root / "references/guide.md").read_text() == "REFERENCE"
    assert (root / "assets/icon.bin").read_bytes() == b"\x00\xff\x01"
    assert (root / "scripts/run.py").is_file()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxError):
        open_workspace(str(workspace)).resolve(str(root / "references/guide.md"))


@pytest.mark.parametrize("payload", [
    {"id": "legacy", "name": "legacy", "instructions": "legacy prompt", "enabled": True},
    [{"id": "legacy", "name": "legacy", "instructions": "legacy prompt", "enabled": True}],
    {"skills": [{"id": "legacy", "name": "legacy", "instructions": "legacy prompt", "enabled": True}]},
])
def test_json_file_keeps_formats_but_requires_explicit_enable(client, payload):
    result = upload(client, json.dumps(payload).encode(), "skills.json")
    assert result["code"] == 0, result
    assert custom_skills()[0]["enabled"] is False
    assert client.put("/api/skills/legacy", json={"enabled": True}).get_json()["code"] == 0
    assert "legacy prompt" in build_skills_section(S.get_enabled_skills())


def test_legacy_json_endpoint_keeps_working(client):
    result = client.post("/api/skills/import", json={"payload": [
        {"id": "old-json", "name": "old json", "instructions": "old instruction"}
    ]}).get_json()
    assert result["code"] == 0
    assert custom_skills()[0]["id"] == "old-json"


def test_duplicate_rejected_and_explicit_overwrite_replaces_resources(client, tmp_path):
    assert upload(client, archive({"SKILL.md": DOC, "references/old.md": "OLD"}))["code"] == 0
    assert upload(client, archive({"SKILL.md": DOC + "NEW"}))["code"] != 0
    root = tmp_path / "data/skills/sample-skill"
    assert (root / "references/old.md").exists()
    result = upload(client, archive({"SKILL.md": DOC + "NEW"}), overwrite="true")
    assert result["code"] == 0, result
    assert not (root / "references/old.md").exists()
    assert (root / "SKILL.md").read_text(encoding="utf-8") == DOC + "NEW"
    assert custom_skills()[0]["enabled"] is False


@pytest.mark.parametrize("bad_path", ["../escaped.txt", "/escaped.txt", "C:/escaped.txt",
                                        "sample/../../escaped.txt", "sample\\..\\escaped.txt",
                                        "sample/assets/file:stream", "sample/CON", "sample/a. "])
def test_zip_rejects_traversal_and_windows_aliases(client, tmp_path, bad_path):
    result = upload(client, archive({"sample/SKILL.md": DOC, bad_path: "EVIL"}))
    assert result["code"] != 0
    assert custom_skills() == []
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_rejects_symlink(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", DOC)
        link = zipfile.ZipInfo("references/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "../../secret")
    assert upload(client, buf.getvalue())["code"] != 0
    assert custom_skills() == []


@pytest.mark.parametrize("name,content", [("bad.exe", b"MZ"), ("SKILL.md", b"\xff"),
                                           ("SKILL.md", b""), ("bad.json", b"{"),
                                           ("package.zip", b"badzip")])
def test_invalid_file_has_clear_failure_and_no_registration(client, name, content):
    result = upload(client, content, name)
    assert result["code"] != 0
    assert result["msg"]
    assert custom_skills() == []


def test_missing_skill_and_duplicate_zip_paths_rejected(client):
    assert upload(client, archive({"README.md": "NO SKILL"}))["code"] != 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", DOC)
        z.writestr("skill.MD", "alias")
    assert upload(client, buf.getvalue())["code"] != 0


def test_upload_and_expansion_limits(client, monkeypatch):
    from apps.skills import packages as P
    monkeypatch.setattr(P, "MAX_DOWNLOAD_BYTES", 128)
    assert upload(client, b"x" * 129, "SKILL.md")["code"] != 0
    monkeypatch.setattr(P, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(P, "MAX_EXPANDED_BYTES", 128)
    assert upload(client, archive({"SKILL.md": DOC}))["code"] != 0
    monkeypatch.setattr(P, "MAX_EXPANDED_BYTES", 1024 * 1024)
    monkeypatch.setattr(P, "MAX_ARCHIVE_ENTRIES", 1)
    assert upload(client, archive({"SKILL.md": DOC, "assets/a": "a"}))["code"] != 0


def test_failed_registry_save_rolls_back_files_and_never_reports_success(client, tmp_path, monkeypatch):
    S.list_skills()
    monkeypatch.setattr(S, "_save_skills", lambda skills: False)
    result = upload(client, DOC.encode(), "SKILL.md")
    assert result["code"] != 0
    assert custom_skills() == []
    assert not (tmp_path / "data/skills/sample-skill").exists()


def test_failed_overwrite_preserves_old_package(client, tmp_path, monkeypatch):
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0
    monkeypatch.setattr(S, "_save_skills", lambda skills: False)
    assert upload(client, (DOC + "CHANGED").encode(), "SKILL.md", overwrite="true")["code"] != 0
    assert (tmp_path / "data/skills/sample-skill/SKILL.md").read_bytes() == DOC.encode()


def test_enabling_missing_package_fails(client, tmp_path):
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0
    (tmp_path / "data/skills/sample-skill/SKILL.md").unlink()
    result = client.put("/api/skills/sample-skill", json={"enabled": True}).get_json()
    assert result["code"] != 0
    assert custom_skills()[0]["enabled"] is False


@pytest.mark.parametrize("url,branch,refs", [
    ("https://github.com/owner/repo", "trunk", None),
    ("https://github.com/owner/repo.git", "trunk", None),
    ("https://github.com/owner/repo/tree/trunk/skills/sample", "trunk", None),
    ("https://github.com/owner/repo/blob/trunk/skills/sample/SKILL.md", "trunk", None),
    ("https://github.com/owner/repo/tree/feature/skills/skills/sample", "feature/skills", {"feature/skills": SHA}),
])
def test_github_repo_tree_blob_resolve_real_ref_and_preserve_siblings(client, monkeypatch, tmp_path, url, branch, refs):
    github_transport(monkeypatch, {"skills/sample/SKILL.md": DOC,
                                 "skills/sample/references/guide.md": "GITHUB REF",
                                 "other.txt": "excluded"}, branch=branch, refs=refs)
    result = github(client, url)
    assert result["code"] == 0, result
    assert result["data"]["total"] == 1
    root = tmp_path / "data/skills/sample-skill"
    assert (root / "references/guide.md").read_text() == "GITHUB REF"
    assert not (root / "other.txt").exists()
    assert custom_skills()[0]["enabled"] is False
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    assert DOC in build_skills_section(S.get_enabled_skills())


@pytest.mark.parametrize("url", ["http://github.com/owner/repo", "https://evil.test/owner/repo",
                                 "https://github.com.evil.test/owner/repo",
                                 "https://github.com@127.0.0.1/owner/repo",
                                 "https://github.com:444/owner/repo", "file:///etc/passwd",
                                 "https://github.com/owner/repo/tree/main/../../private",
                                 "https://github.com/owner/repo/tree/main/%2e%2e/private",
                                 "https://github.com/owner/repo?url=http://localhost",
                                 "https://github.com/owner/repo/blob/main/README.md"])
def test_github_rejects_unsafe_urls_before_network(client, monkeypatch, url):
    def forbidden(*args, **kwargs):
        pytest.fail("不安全 URL 不应发起网络请求")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    result = github(client, url)
    assert result["code"] != 0
    assert custom_skills() == []


@pytest.mark.parametrize("status", [302, 403, 404, 500])
def test_github_http_failure_is_not_import_success(client, monkeypatch, status):
    def error(url):
        raise urllib.error.HTTPError(url, status, "failure", {"Location": "http://127.0.0.1"}, None)
    github_transport(monkeypatch, {}, error=error)
    result = github(client)
    assert result["code"] != 0
    assert str(status) in result["msg"]
    assert custom_skills() == []


def test_download_success_without_skill_is_application_failure(client, monkeypatch):
    github_transport(monkeypatch, {"README.md": "no skill"})
    result = github(client)
    assert result["code"] != 0
    assert custom_skills() == []


def test_multiple_packages_have_independent_resources(client, tmp_path):
    result = upload(client, archive({"one/SKILL.md": DOC,
                                    "two/SKILL.md": "---\nname: another-skill\n---\nSecond instructions",
                                    "one/assets/one": "ONE", "two/assets/two": "TWO"}))
    assert result["code"] == 0, result
    assert result["data"]["total"] == 2
    assert (tmp_path / "data/skills/sample-skill/assets/one").read_text() == "ONE"
    assert (tmp_path / "data/skills/another-skill/assets/two").read_text() == "TWO"


def test_read_skill_resource_requires_enabled_import_and_returns_real_text(client, tmp_path):
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "真实资源\n"}))["code"] == 0
    result = S.read_skill_resource("sample-skill", "references/guide.md")
    assert result["ok"] is False
    assert result["error_code"] == "SKILL_NOT_ENABLED"
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    result = S.read_skill_resource("sample-skill", "references/guide.md")
    assert result["ok"] is True
    assert result["content"] == "真实资源\n"
    assert result["bytes"] == 13
    assert result["skill_id"] == "sample-skill"
    assert result["path"] == "references/guide.md"
    assert result["resource_root"] == str((tmp_path / "data/skills/sample-skill").resolve())
    prompt = build_skills_section(S.get_enabled_skills())
    assert "read_skill_resource" in prompt
    assert "sample-skill" in prompt


@pytest.mark.parametrize("relative", ["../config.user.json", "/etc/passwd", "C:/secret",
                                      "references/../../secret", "references\\guide.md",
                                      "references/guide.md:stream", "references/CON", "references/guide.md. "])
def test_read_skill_resource_rejects_paths(client, relative):
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "SAFE"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    result = S.read_skill_resource("sample-skill", relative)
    assert result["ok"] is False
    assert "content" not in result


def test_resource_reader_denies_prompt_skills_unknown_and_unlisted_files(client, tmp_path):
    assert S.register_skill("prompt", "", "instructions", skill_id="plain", enabled=True)["ok"]
    assert S.read_skill_resource("plain", "SKILL.md")["ok"] is False
    assert S.read_skill_resource("missing", "SKILL.md")["ok"] is False
    assert S.read_skill_resource("../secret", "SKILL.md")["ok"] is False
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    root = tmp_path / "data/skills/sample-skill"
    (root / "not-imported.txt").write_text("PRIVATE")
    assert S.read_skill_resource("sample-skill", "not-imported.txt")["ok"] is False


def test_resource_reader_enforces_text_and_size_limits(client, monkeypatch):
    assert upload(client, archive({"SKILL.md": DOC, "assets/binary": b"\x00\xff",
                                  "references/large.md": b"a" * (128 * 1024 + 1),
                                  "scripts/run.py": "print('do not run')"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    assert S.read_skill_resource("sample-skill", "assets/binary")["error_code"] == "RESOURCE_NOT_TEXT"
    assert S.read_skill_resource("sample-skill", "references/large.md")["error_code"] == "RESOURCE_TOO_LARGE"
    assert S.read_skill_resource("sample-skill", "scripts/run.py")["content"] == "print('do not run')"


def test_resource_reader_rejects_symlink_at_any_level(client, tmp_path):
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "SAFE"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    root = tmp_path / "data/skills/sample-skill"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guide.md").write_text("PRIVATE")
    (root / "references/guide.md").unlink()
    (root / "references").rmdir()
    try:
        (root / "references").symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows 非管理员以 junction 重解析点执行相同边界用例。
        import subprocess
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "references"), str(outside)], capture_output=True)
        if completed.returncode:
            pytest.skip("环境不支持 symlink 或 junction")
    result = S.read_skill_resource("sample-skill", "references/guide.md")
    assert result["ok"] is False
    assert "content" not in result


def test_package_delete_allows_reimport(client, tmp_path):
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0
    assert client.delete("/api/skills/sample-skill").get_json()["code"] == 0
    assert not (tmp_path / "data/skills/sample-skill").exists()
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0


def test_legacy_endpoint_accepts_bare_array_and_no_implicit_string_overwrite(client):
    payload = [{"id": "legacy-array", "name": "array", "instructions": "original"}]
    assert client.post("/api/skills/import", json=payload).get_json()["code"] == 0
    result = client.post("/api/skills/import", json={"payload": payload, "overwrite": "false"}).get_json()
    assert result["code"] != 0
    assert custom_skills()[0]["instructions"] == "original"


def test_blob_missing_exact_document_is_not_nested_skill_success(client, monkeypatch):
    github_transport(monkeypatch, {"skills/sample/nested/SKILL.md": DOC})
    result = github(client, "https://github.com/owner/repo/blob/trunk/skills/sample/SKILL.md")
    assert result["code"] != 0
    assert custom_skills() == []


@pytest.mark.parametrize("mode", ["file", "github"])
def test_independent_page_sends_exact_api_and_keeps_success_or_failure_visible(mode):
    import subprocess
    from pathlib import Path
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor() { this.value = ''; this.files = []; this.events = {}; this.children = []; this.textContent = ''; this.hidden = true; this.disabled = false; this.classList = {add(){},remove(){}}; }
  addEventListener(name, fn) { this.events[name] = fn; }
  appendChild(node) { this.children.push(node); }
}
class Form { constructor() { this.fields = {}; } append(key,value) { this.fields[key] = value; } }
const ids = ['skList','skEmpty','skFile','skDrop','skFileName','btnImport','btnClearFile','skImportResult','skGithubUrl','btnImportGithub','skGithubResult'];
const nodes = Object.fromEntries(ids.map(id => [id, new Element()]));
const requests = [], toasts = [];
let succeed = true;
const context = {document:{getElementById:id=>nodes[id] || null,createElement:()=>new Element()},
  window:{AegisAuth:{get:()=>Promise.resolve({data:{skills:[]}}),getToken:()=> 'test-token'},showToast:(msg,type)=>toasts.push({msg,type})},
  FormData:Form, fetch:(url,opts)=>{ requests.push({url,opts}); return Promise.resolve({ok:true,json:()=>Promise.resolve(succeed ? {code:0,data:{ok:true,total:1,imported:[{id:'sample-skill',name:'sample'}],skipped:[],enabled:false,resource_boundary:'Boundary'}} : {code:400,msg:'application failed',data:{}})}); }};
vm.runInNewContext(fs.readFileSync('static/js/skills.js','utf8'),context);
async function flush() { for (let i=0;i<6;i++) await new Promise(resolve=>setImmediate(resolve)); }
async function run() {
  const mode = process.argv[1];
  if (mode === 'file') {
    const file = {name:'package.zip',size:128}; nodes.skFile.files=[file]; nodes.skFile.events.change();
    assert.equal(nodes.btnImport.disabled,false); nodes.btnImport.events.click(); await flush();
    assert.equal(requests[0].url,'/api/skills/import/file');
    assert.strictEqual(requests[0].opts.body.fields.file,file);
    assert(!requests[0].opts.headers['Content-Type']);
    assert.equal(requests[0].opts.headers['X-Aegis-Token'],'test-token');
    assert.equal(nodes.skImportResult.hidden,false);
    assert(nodes.skImportResult.children.some(n=>n.textContent.includes('启用')));
    succeed=false; nodes.skFile.events.change(); nodes.btnImport.events.click(); await flush();
    assert.equal(nodes.skImportResult.hidden,false); assert.equal(nodes.btnImport.disabled,false);
  } else {
    nodes.skGithubUrl.value='https://github.com/owner/repo/tree/trunk/skills/sample';
    assert.equal(typeof nodes.btnImportGithub.events.click,'function');
    nodes.btnImportGithub.events.click(); await flush();
    assert.equal(requests[0].url,'/api/skills/import/github');
    assert.equal(JSON.parse(requests[0].opts.body).url,nodes.skGithubUrl.value);
    assert.equal(nodes.skGithubResult.hidden,false);
    succeed=false; nodes.btnImportGithub.events.click(); await flush();
    assert.equal(nodes.skGithubResult.hidden,false); assert.equal(nodes.btnImportGithub.disabled,false);
  }
  assert.equal(toasts.at(-1).type,'error');
}
run().catch(err=>{console.error(err);process.exitCode=1;});
"""
    completed = subprocess.run(["node", "-e", script, mode], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr


def test_real_urllib_redirect_pipeline_never_connects_to_redirect_destination(client, monkeypatch):
    calls = []
    def https_open(self, request):
        calls.append(request.full_url)
        if len(calls) > 1:
            pytest.fail("重定向目标不应被请求")
        return Response(b"", request.full_url, 302, {"Location": "http://127.0.0.1/private"})
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    result = github(client)
    assert result["code"] != 0
    assert "302" in result["msg"]
    assert calls == ["https://api.github.com/repos/owner/repo"]


def test_download_enforces_stream_limit_even_without_content_length(client, monkeypatch):
    from apps.skills import packages as P
    monkeypatch.setattr(P, "MAX_DOWNLOAD_BYTES", 32)
    github_transport(monkeypatch, {"SKILL.md": DOC})
    result = github(client)
    assert result["code"] != 0
    assert custom_skills() == []


def test_api_download_limit_is_not_misreported_as_invalid_json(client, monkeypatch):
    from apps.skills import packages as P
    def open_request(self, req, *args, **kwargs):
        return Response(b"x" * (P.MAX_SKILL_BYTES + 1), req.full_url)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    result = github(client)
    assert result["code"] != 0
    assert "体积" in result["msg"]


def test_import_is_atomic_across_multiple_skills_and_builtin_never_overwritten(client, tmp_path):
    builtin = "---\nname: explain-code\n---\nMust not overwrite builtin"
    result = upload(client, archive({"valid/SKILL.md": DOC, "builtin/SKILL.md": builtin}), overwrite="true")
    assert result["code"] != 0
    assert custom_skills() == []
    assert not (tmp_path / "data/skills/sample-skill").exists()


def test_delete_config_failure_restores_original_resources(client, monkeypatch, tmp_path):
    assert upload(client, DOC.encode(), "SKILL.md")["code"] == 0
    monkeypatch.setattr(S, "_save_skills", lambda skills: False)
    assert client.delete("/api/skills/sample-skill").get_json()["code"] != 0
    assert (tmp_path / "data/skills/sample-skill/SKILL.md").read_bytes() == DOC.encode()
    assert len(custom_skills()) == 1


def test_disabling_package_revokes_resource_access(client):
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "RESOURCE"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    assert S.read_skill_resource("sample-skill", "references/guide.md")["ok"]
    assert S.update_skill("sample-skill", enabled=False)["ok"]
    assert S.read_skill_resource("sample-skill", "references/guide.md")["error_code"] == "SKILL_NOT_ENABLED"


def test_resource_reader_rejects_skill_root_and_leaf_junctions(client, monkeypatch, tmp_path):
    from apps.skills import packages as P
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "SAFE"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    root = tmp_path / "data/skills/sample-skill"
    original = P._is_link
    # 补充不能创建文件 symlink 的 Windows 环境：重解析属性在叶子和包根均须拒绝。
    for unsafe in (root, root / "references/guide.md"):
        monkeypatch.setattr(P, "_is_link", lambda path: path == unsafe or original(path))
        result = S.read_skill_resource("sample-skill", "references/guide.md")
        assert result["ok"] is False
        assert "content" not in result


def test_nested_documents_stay_in_parent_package_without_duplicating_expansion(client, tmp_path):
    result = upload(client, archive({"parent/SKILL.md": DOC,
                                    "parent/nested/SKILL.md": "---\nname: nested-skill\n---\nNested instruction",
                                    "parent/nested/assets/item": "NESTED"}))
    assert result["code"] == 0
    assert result["data"]["total"] == 1
    assert (tmp_path / "data/skills/sample-skill/nested/assets/item").read_text() == "NESTED"


def test_imported_instructions_and_resource_reach_real_runner_model(client, ws, fake_client):
    from apps.agent.runner import AgentRunner
    from conftest import native_tool_events
    assert upload(client, archive({"SKILL.md": DOC, "references/guide.md": "SKILL RESOURCE EVIDENCE"}))["code"] == 0
    assert S.update_skill("sample-skill", enabled=True)["ok"]
    model = fake_client(script=[native_tool_events("read_skill_resource", {
        "skill_id": "sample-skill", "relative_path": "references/guide.md",
    })])
    runner = AgentRunner(workspace=ws, user_task="使用技能资源分析项目", config={},
                         permission_mode="plan", skills=S.get_enabled_skills())
    events = list(runner.run())
    resource = next(event for event in events if event.get("type") == "tool_result")
    assert resource["ok"] is True, resource
    assert resource["data"]["content"] == "SKILL RESOURCE EVIDENCE"
    assert any(DOC in message["content"] for message in model.message_log[0] if message["role"] == "system")
    assert any("SKILL RESOURCE EVIDENCE" in message.get("content", "")
               for messages in model.message_log for message in messages if message["role"] == "tool")


@pytest.mark.parametrize("mode", ["tree", "blob"])
def test_github_422_no_commit_found_tries_shorter_ref_and_imports(client, monkeypatch, tmp_path, mode):
    github_transport(monkeypatch, {"skills/frontend-design/SKILL.md": DOC,
                                 "skills/frontend-design/references/guide.md": "REAL REF"},
                     branch="main", missing_ref_status=422)
    path = "https://github.com/owner/repo/%s/main/skills/frontend-design" % mode
    if mode == "blob":
        path += "/SKILL.md"
    result = github(client, path)
    assert result["code"] == 0, result
    assert (tmp_path / "data/skills/sample-skill/references/guide.md").read_text() == "REAL REF"


@pytest.mark.parametrize("status,message", [
    (403, "No commit found for SHA: main/skills/frontend-design"),
    (429, "No commit found for SHA: main/skills/frontend-design"),
    (422, "Validation Failed"),
    (422, "No commit found for SHA: a-different-ref"),
    (404, "Not Found"),
])
def test_github_non_ref_errors_never_fall_back_to_valid_short_ref(client, monkeypatch, status, message):
    def error(url):
        if url.endswith("/commits/main%2Fskills%2Ffrontend-design"):
            body = json.dumps({"message": message, "status": str(status)}).encode()
            raise urllib.error.HTTPError(url, status, "Error", {}, io.BytesIO(body))
        # 如果错误被错误地吞掉，提供真实可注册的短 ref 与技能包，测试会观察到误报成功。
        if "/commits/" in url:
            return Response(json.dumps({"sha": SHA}).encode(), url)
        return Response(archive({"repo-snapshot/SKILL.md": DOC}), url)
    github_transport(monkeypatch, {}, error=error)
    result = github(client, "https://github.com/owner/repo/tree/main/skills/frontend-design")
    assert result["code"] != 0, result
    assert str(status) in result["msg"]
    assert custom_skills() == []


def test_selected_github_skill_downloads_only_selected_directory(client, monkeypatch, tmp_path):
    github_transport(monkeypatch, {"skills/sample/SKILL.md": DOC, "skills/sample/references/guide.md": "REFERENCE",
                                 "unrelated/SKILL.md": "do not import"}, branch="main")
    transport = urllib.request.OpenerDirector.open
    calls = []
    def selected_only(self, req, *args, **kwargs):
        calls.append(req.full_url)
        if "codeload.github.com" in req.full_url:
            raise urllib.error.URLError("large repository archive must not be downloaded")
        return transport(self, req, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", selected_only)
    result = github(client, "https://github.com/owner/repo/tree/main/skills/sample")
    assert result["code"] == 0, result
    assert (tmp_path / "data/skills/sample-skill/references/guide.md").read_text() == "REFERENCE"
    assert all("unrelated" not in url for url in calls)


def test_selected_skill_uses_github_api_when_raw_host_dns_is_unavailable(client, monkeypatch):
    github_transport(monkeypatch, {"skills/sample/SKILL.md": DOC}, branch="main")
    transport = urllib.request.OpenerDirector.open
    def no_raw_host(self, req, *args, **kwargs):
        if "raw.githubusercontent.com" in req.full_url:
            raise urllib.error.URLError("raw host DNS unavailable")
        return transport(self, req, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_raw_host)
    result = github(client, "https://github.com/owner/repo/tree/main/skills/sample")
    assert result["code"] == 0, result


@pytest.mark.parametrize("aliases", [
    {"skills/sample/skill.md": "REPLACED INSTRUCTIONS"},
    {"skills/sample/references/guide.md": "first", "skills/sample/References/other.md": "second"},
])
def test_selected_directory_rejects_windows_case_aliases_before_install(client, monkeypatch, tmp_path, aliases):
    github_transport(monkeypatch, {"skills/sample/SKILL.md": DOC, **aliases}, branch="main")
    result = github(client, "https://github.com/owner/repo/tree/main/skills/sample")
    assert result["code"] != 0, result
    assert custom_skills() == []
    assert not (tmp_path / "data/skills/sample-skill").exists()


def test_contents_submodule_reported_as_file_is_rejected(client, monkeypatch):
    github_transport(monkeypatch, {"skills/sample/SKILL.md": DOC, "skills/sample/module": "gitlink"}, branch="main")
    transport = urllib.request.OpenerDirector.open
    def contents_with_submodule(self, req, *args, **kwargs):
        result = transport(self, req, *args, **kwargs)
        if "/contents/skills/sample?" in req.full_url and req.get_header("Accept") != "application/vnd.github.raw+json":
            entries = json.loads(result.getvalue())
            for entry in entries:
                if entry["name"] == "module": entry["submodule_git_url"] = "https://github.com/other/repo"
            return Response(json.dumps(entries).encode(), req.full_url)
        return result
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", contents_with_submodule)
    result = github(client, "https://github.com/owner/repo/tree/main/skills/sample")
    assert result["code"] != 0, result
    assert custom_skills() == []
