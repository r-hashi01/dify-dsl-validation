# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0", "requests>=2.31"]
# ///
"""
Dify DSL Plugin Usage Validator

DSL 内で参照している plugin (tool/llm) の使い方が実定義と整合するかを検証。
真実の源:
  - 1st: langgenius/dify-official-plugins (source yaml, langgenius 作のみ)
  - 2nd: langgenius/dify-plugins (marketplace ミラー, .difypkg を unzip して抽出)
  - 見つからなければ error (PLUGIN_NOT_FOUND)

検証対象:
  - tool ノード: tool_name の存在、tool_parameters / tool_configurations の整合
  - llm ノード : provider の存在、model 名の存在

使い方:
  uv run validate_dsl_plugin_usage.py <dsl.yml>
  # キャッシュ: ~/.cache/dify-dsl-validation/

Dify コードノード利用時は、外部 HTTP が使えない sandbox を想定して
`main(dsl_text, plugin_cache)` を公開。plugin_cache は上流 HTTP ノードで
事前取得した dict を渡す。
"""

from __future__ import annotations
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_BLOB = "https://github.com"
OFFICIAL_REPO = "langgenius/dify-official-plugins"
MARKETPLACE_REPO = "langgenius/dify-plugins"

# dify-official-plugins のカテゴリフォルダ (総当たり検索用)
OFFICIAL_CATEGORIES = ("tools", "models", "datasources", "triggers",
                       "extensions", "agent-strategies")

CACHE_DIR = Path(os.path.expanduser("~/.cache/dify-dsl-validation"))


def _parse_yaml(text):
    import yaml  # type: ignore
    return yaml.safe_load(text)


def _err(code, message, severity="error", **extra):
    e = {
        "code": code, "severity": severity, "message": message,
        "node_id": None, "plugin_id": None, "tool_name": None,
        "model_name": None, "fix_hint": None,
    }
    e.update(extra)
    return e


# ====== Plugin 定義取得 ======

class PluginDefinition:
    """解決済み plugin 定義。"""
    def __init__(self, plugin_id: str, version: str | None,
                 manifest: dict, providers: dict[str, dict],
                 tools: dict[str, dict], models: dict[str, list[str]],
                 source: str):
        self.plugin_id = plugin_id   # "author/name"
        self.version = version
        self.manifest = manifest
        # provider_name -> provider yaml dict (identity + tools[] + ...)
        self.providers = providers
        # provider_name + "/" + tool_name -> ToolDeclaration dict
        self.tools = tools
        # provider_name -> list of model names (filename stems under models/<type>/)
        self.models = models
        self.source = source

    def list_tools(self, provider_name: str) -> list[str]:
        prefix = f"{provider_name}/"
        return [t[len(prefix):] for t in self.tools if t.startswith(prefix)]

    def get_tool(self, provider_name: str, tool_name: str) -> dict | None:
        return self.tools.get(f"{provider_name}/{tool_name}")

    def list_providers(self) -> list[str]:
        return list(self.providers.keys())

    def list_models(self, provider_name: str) -> list[str]:
        return self.models.get(provider_name, [])


def _cache_path(plugin_id: str, version: str | None) -> Path:
    author, name = plugin_id.split("/", 1)
    v = version or "latest"
    return CACHE_DIR / "plugins" / author / name / v


def _http_get(url: str, binary: bool = False):
    import requests  # type: ignore
    r = requests.get(url, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content if binary else r.text


def _fetch_tree(repo: str, path: str) -> list[dict] | None:
    """GitHub contents API. 公開 raw を使えない一覧取得のため。"""
    import requests  # type: ignore
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _resolve_official(plugin_id: str) -> dict | None:
    """dify-official-plugins 内のカテゴリ/パスを見つけて files を返す。
    戻り値: {"category": str, "files": {relpath: bytes/text}}
    """
    author, name = plugin_id.split("/", 1)
    if author != "langgenius":
        return None

    for cat in OFFICIAL_CATEGORIES:
        manifest_url = f"{GITHUB_RAW}/{OFFICIAL_REPO}/main/{cat}/{name}/manifest.yaml"
        txt = _http_get(manifest_url)
        if not txt:
            continue
        manifest = _parse_yaml(txt) or {}
        # フォルダ名と manifest.name が異なるケースあり (tools/openai → openai_tool)
        if manifest.get("name") != name or manifest.get("author") != author:
            continue

        files = {"manifest.yaml": txt}
        plugins = manifest.get("plugins") or {}

        # provider yaml 群を取得
        for key in ("tools", "models", "endpoints", "agent_strategies",
                    "datasources", "triggers"):
            for rel in (plugins.get(key) or []):
                purl = f"{GITHUB_RAW}/{OFFICIAL_REPO}/main/{cat}/{name}/{rel}"
                ptxt = _http_get(purl)
                if ptxt is not None:
                    files[rel] = ptxt
                    # provider yaml 内で参照されている tool yaml を再帰取得
                    if key == "tools":
                        prov = _parse_yaml(ptxt) or {}
                        tools_list = prov.get("tools") or []
                        for t in tools_list:
                            if isinstance(t, str):
                                turl = f"{GITHUB_RAW}/{OFFICIAL_REPO}/main/{cat}/{name}/{t}"
                                ttxt = _http_get(turl)
                                if ttxt is not None:
                                    files[t] = ttxt

        # model plugin の場合、models/ 配下を列挙
        if cat == "models":
            tree = _fetch_tree(OFFICIAL_REPO, f"{cat}/{name}/models")
            if isinstance(tree, list):
                for typ_entry in tree:
                    if typ_entry.get("type") != "dir":
                        continue
                    typ = typ_entry["name"]
                    sub = _fetch_tree(OFFICIAL_REPO, f"{cat}/{name}/models/{typ}")
                    if not isinstance(sub, list):
                        continue
                    for m in sub:
                        if m.get("type") == "file" and m["name"].endswith(".yaml"):
                            rel = f"models/{typ}/{m['name']}"
                            mtxt = _http_get(
                                f"{GITHUB_RAW}/{OFFICIAL_REPO}/main/{cat}/{name}/{rel}")
                            if mtxt is not None:
                                files[rel] = mtxt
        return {"category": cat, "files": files}
    return None


def _resolve_marketplace(plugin_id: str, version: str | None) -> dict | None:
    """dify-plugins ミラーから .difypkg を取得して展開。"""
    author, name = plugin_id.split("/", 1)
    # フォルダ内のパッケージ一覧
    tree = _fetch_tree(MARKETPLACE_REPO, f"{author}/{name}")
    if not isinstance(tree, list):
        return None
    pkgs = [e for e in tree if e.get("name", "").endswith(".difypkg")]
    if not pkgs:
        return None

    target = None
    if version:
        needle = f"-{version}.difypkg"
        for p in pkgs:
            if p["name"].endswith(needle):
                target = p
                break
    if target is None:
        # 最新 (sort by name で末尾)
        target = sorted(pkgs, key=lambda x: x["name"])[-1]

    url = f"{GITHUB_BLOB}/{MARKETPLACE_REPO}/raw/main/{author}/{name}/{target['name']}"
    blob = _http_get(url, binary=True)
    if not blob:
        return None

    files: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name_in_zip in z.namelist():
            if not (name_in_zip.endswith(".yaml")
                    or name_in_zip.endswith(".yml")):
                continue
            try:
                files[name_in_zip] = z.read(name_in_zip).decode("utf-8")
            except Exception:
                pass
    return {"category": None, "files": files}


def _save_cache(cache: Path, files: dict[str, str], category: str | None):
    cache.mkdir(parents=True, exist_ok=True)
    (cache / ".category").write_text(category or "", encoding="utf-8")
    for rel, content in files.items():
        p = cache / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _load_cache(cache: Path) -> dict | None:
    if not cache.exists():
        return None
    cat_file = cache / ".category"
    category = cat_file.read_text(encoding="utf-8") if cat_file.exists() else None
    files: dict[str, str] = {}
    for p in cache.rglob("*"):
        if p.is_file() and p.suffix in (".yaml", ".yml") and p.name != ".category":
            files[str(p.relative_to(cache))] = p.read_text(encoding="utf-8")
    if not files:
        return None
    return {"category": category or None, "files": files}


def _build_definition(plugin_id: str, version: str | None,
                       bundle: dict) -> PluginDefinition:
    files = bundle["files"]
    manifest = _parse_yaml(files.get("manifest.yaml", "")) or {}

    providers: dict[str, dict] = {}
    tools: dict[str, dict] = {}
    models: dict[str, list[str]] = defaultdict(list)

    plugins = manifest.get("plugins") or {}

    for key in ("tools", "models", "endpoints", "agent_strategies",
                "datasources", "triggers"):
        for rel in (plugins.get(key) or []):
            if rel not in files:
                continue
            prov = _parse_yaml(files[rel]) or {}
            if not isinstance(prov, dict):
                continue
            identity = prov.get("identity") or {}
            provider_name = identity.get("name") or Path(rel).stem
            providers[provider_name] = prov
            if key == "tools":
                for t in (prov.get("tools") or []):
                    if isinstance(t, str) and t in files:
                        tool_decl = _parse_yaml(files[t]) or {}
                        if isinstance(tool_decl, dict):
                            tname = ((tool_decl.get("identity") or {}).get("name")
                                     or Path(t).stem)
                            tools[f"{provider_name}/{tname}"] = tool_decl
                    elif isinstance(t, dict):
                        tname = ((t.get("identity") or {}).get("name") or "")
                        if tname:
                            tools[f"{provider_name}/{tname}"] = t

    # model plugin の model 名列挙 (.difypkg 内 or official tree から)
    for rel in files:
        m = re.match(r"^models/([^/]+)/([^/]+)\.ya?ml$", rel)
        if m:
            # rel は provider yaml の場合も含むので、中身を見て model フィールドがあるか確認
            decl = _parse_yaml(files[rel]) or {}
            if isinstance(decl, dict) and (decl.get("model")
                                             or decl.get("model_type")):
                model_name = decl.get("model") or Path(rel).stem
                # providerごと: モデル定義は全 providers にひも付ける (1対1前提)
                for pname in providers:
                    models[pname].append(str(model_name))

    return PluginDefinition(
        plugin_id=plugin_id, version=version, manifest=manifest,
        providers=providers, tools=tools, models=dict(models),
        source=bundle.get("category") or "marketplace",
    )


def resolve_plugin(plugin_id: str, version: str | None = None,
                   use_cache: bool = True) -> PluginDefinition | None:
    """plugin_id = 'author/name'。version=None なら latest。"""
    cache = _cache_path(plugin_id, version)
    if use_cache:
        loaded = _load_cache(cache)
        if loaded:
            return _build_definition(plugin_id, version, loaded)

    # official-plugins 優先
    try:
        bundle = _resolve_official(plugin_id)
    except Exception:
        bundle = None
    if bundle is None:
        try:
            bundle = _resolve_marketplace(plugin_id, version)
        except Exception:
            bundle = None
    if bundle is None:
        return None

    _save_cache(cache, bundle["files"], bundle.get("category"))
    return _build_definition(plugin_id, version, bundle)


# ====== DSL からの参照抽出 ======

def _split_provider_ref(provider_ref: str) -> tuple[str, str]:
    """'langgenius/openai/openai' -> ('langgenius/openai', 'openai')
    'author/name' のみの場合は provider_name=name を仮定。
    """
    parts = provider_ref.split("/")
    if len(parts) >= 3:
        return "/".join(parts[:2]), parts[2]
    if len(parts) == 2:
        return provider_ref, parts[1]
    return provider_ref, provider_ref


def _extract_version_from_uid(uid: str | None) -> str | None:
    if not uid or ":" not in uid or "@" not in uid:
        return None
    try:
        return uid.split(":", 1)[1].split("@", 1)[0]
    except Exception:
        return None


def collect_usages(graph: dict) -> list[dict]:
    """graph から tool / llm ノードの plugin 使用状況を列挙。"""
    usages: list[dict] = []
    for n in (graph or {}).get("nodes") or []:
        if not isinstance(n, dict):
            continue
        data = n.get("data") or {}
        ntype = data.get("type")
        nid = n.get("id")

        if ntype == "tool":
            provider_id = data.get("provider_id") or ""
            plugin_id, provider_name = _split_provider_ref(provider_id)
            # data.plugin_id が入ってれば優先
            if data.get("plugin_id"):
                plugin_id = data["plugin_id"]
            version = _extract_version_from_uid(
                data.get("plugin_unique_identifier"))
            usages.append({
                "kind": "tool", "node_id": nid,
                "plugin_id": plugin_id, "version": version,
                "provider_name": provider_name,
                "tool_name": data.get("tool_name"),
                "tool_parameters": data.get("tool_parameters") or {},
                "tool_configurations": data.get("tool_configurations") or {},
            })

        elif ntype in ("llm", "parameter-extractor", "question-classifier",
                       "agent", "knowledge-retrieval"):
            model = data.get("model") or {}
            provider_ref = model.get("provider") or ""
            if not provider_ref:
                continue
            plugin_id, provider_name = _split_provider_ref(provider_ref)
            usages.append({
                "kind": "llm", "node_id": nid,
                "plugin_id": plugin_id, "version": None,
                "provider_name": provider_name,
                "model_name": model.get("name"),
                "model_mode": model.get("mode"),
                "completion_params": model.get("completion_params") or {},
            })
    return usages


# ====== 検証 ======

def _dependencies_types(deps: list) -> dict[str, str]:
    """plugin_id -> dependency type (github/marketplace/package)"""
    out: dict[str, str] = {}
    if not isinstance(deps, list):
        return out
    for d in deps:
        if not isinstance(d, dict):
            continue
        dtype = d.get("type")
        value = d.get("value") or {}
        uid = (value.get("marketplace_plugin_unique_identifier")
               or value.get("github_plugin_unique_identifier")
               or value.get("plugin_unique_identifier") or "")
        if uid and ":" in uid:
            pid = uid.split(":", 1)[0]
            out[pid] = dtype or ""
    return out


def validate_usage(usage: dict, plugin: PluginDefinition | None,
                    dep_type: str | None) -> list[dict]:
    errs: list[dict] = []
    loc = {"node_id": usage["node_id"], "plugin_id": usage["plugin_id"]}

    if plugin is None:
        if dep_type == "package":
            errs.append(_err(
                "PLUGIN_LOCAL_PACKAGE_UNVERIFIABLE",
                f"plugin '{usage['plugin_id']}' は local package (検証不可)",
                severity="warning",
                fix_hint="marketplace 公開版の使用を推奨",
                **loc,
            ))
        else:
            errs.append(_err(
                "PLUGIN_NOT_FOUND",
                f"plugin '{usage['plugin_id']}' が "
                f"dify-official-plugins / dify-plugins のいずれにも見つからない",
                fix_hint="plugin_id の綴り確認、または dependencies の追加",
                **loc,
            ))
        return errs

    if usage["kind"] == "tool":
        errs.extend(_validate_tool_usage(usage, plugin))
    elif usage["kind"] == "llm":
        errs.extend(_validate_llm_usage(usage, plugin))
    return errs


def _validate_tool_usage(usage: dict, plugin: PluginDefinition) -> list[dict]:
    errs: list[dict] = []
    pname = usage["provider_name"]
    tname = usage["tool_name"]
    loc = {"node_id": usage["node_id"], "plugin_id": usage["plugin_id"],
           "tool_name": tname}

    if pname not in plugin.providers:
        errs.append(_err(
            "PROVIDER_NOT_FOUND",
            f"provider '{pname}' が plugin '{usage['plugin_id']}' に存在しない "
            f"(利用可能: {', '.join(plugin.list_providers()) or '(無)'})",
            **loc,
        ))
        return errs

    tool = plugin.get_tool(pname, tname) if tname else None
    if not tool:
        available = plugin.list_tools(pname)
        errs.append(_err(
            "TOOL_NOT_FOUND",
            f"tool '{tname}' が provider '{pname}' に存在しない "
            f"(利用可能: {', '.join(available) or '(無)'})",
            fix_hint="tool_name の綴り確認",
            **loc,
        ))
        return errs

    # パラメータ照合
    declared = {p.get("name"): p for p in (tool.get("parameters") or [])
                if isinstance(p, dict) and p.get("name")}

    passed_params = usage["tool_parameters"] or {}
    passed_configs = usage["tool_configurations"] or {}
    passed_all = {**passed_params, **passed_configs}

    for key in passed_all:
        if key not in declared:
            errs.append(_err(
                "TOOL_PARAM_UNKNOWN",
                f"tool '{tname}' に未定義パラメータ '{key}' が渡されている "
                f"(定義済: {', '.join(declared) or '(無)'})",
                fix_hint=f"'{key}' を削除、または正しい名前に修正",
                **loc,
            ))

    for pname_, pdef in declared.items():
        if not pdef.get("required"):
            continue
        if pname_ in passed_all:
            val = passed_all[pname_]
            # value-selector 形式 {type: "mixed"/"variable", value: ...}
            # 中身が空なら欠落扱い
            if isinstance(val, dict):
                inner = val.get("value")
                if inner is None or (isinstance(inner, str) and not inner.strip()
                                      and not val.get("value_selector")):
                    errs.append(_err(
                        "TOOL_PARAM_MISSING_REQUIRED",
                        f"required パラメータ '{pname_}' の値が空",
                        **loc,
                    ))
            continue
        errs.append(_err(
            "TOOL_PARAM_MISSING_REQUIRED",
            f"required パラメータ '{pname_}' が渡されていない",
            fix_hint=f"tool_parameters に '{pname_}' を追加",
            **loc,
        ))

    # select 型の値チェック (静的な値のみ)
    for key, pdef in declared.items():
        if pdef.get("type") not in ("select", "dynamic-select"):
            continue
        if key not in passed_all:
            continue
        val = passed_all[key]
        # 変数参照ならスキップ
        if isinstance(val, dict) and val.get("type") in ("variable", "mixed"):
            continue
        static = val.get("value") if isinstance(val, dict) else val
        if static is None:
            continue
        options = [o.get("value") for o in (pdef.get("options") or [])
                   if isinstance(o, dict)]
        if options and static not in options:
            errs.append(_err(
                "TOOL_PARAM_INVALID_OPTION",
                f"パラメータ '{key}'='{static}' は options に無い "
                f"(許可: {', '.join(map(str, options))})",
                **loc,
            ))
    return errs


def _validate_llm_usage(usage: dict, plugin: PluginDefinition) -> list[dict]:
    errs: list[dict] = []
    pname = usage["provider_name"]
    mname = usage["model_name"]
    loc = {"node_id": usage["node_id"], "plugin_id": usage["plugin_id"],
           "model_name": mname}

    if pname not in plugin.providers:
        errs.append(_err(
            "PROVIDER_NOT_FOUND",
            f"provider '{pname}' が plugin '{usage['plugin_id']}' に存在しない "
            f"(利用可能: {', '.join(plugin.list_providers()) or '(無)'})",
            **loc,
        ))
        return errs

    if not mname:
        return errs

    available = plugin.list_models(pname)
    # predefined ではなく customizable な provider (OpenAI 互換など) は models 空
    # その場合は検証スキップ (warning にしない)
    if available and mname not in available:
        errs.append(_err(
            "MODEL_NOT_FOUND",
            f"model '{mname}' が provider '{pname}' に存在しない "
            f"(利用可能は計 {len(available)} 件)",
            fix_hint="model 名の綴り、または対応 plugin version を確認",
            **loc,
        ))
    return errs


# ====== エントリーポイント ======

def _build_report(errors: list[dict], usages: list[dict]) -> str:
    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")
    head = f"scanned={len(usages)} usages, error={err_cnt}, warning={warn_cnt}"
    if not errors:
        return f"OK: plugin 使い方は妥当 ({head})"
    lines = [f"{'NG' if err_cnt else 'WARN'}: {head}", ""]
    by_code: dict[str, list[dict]] = defaultdict(list)
    for e in errors:
        by_code[e["code"]].append(e)
    for code, items in by_code.items():
        sev = items[0]["severity"].upper()
        lines.append(f"[{sev}][{code}] x{len(items)}")
        for e in items[:10]:
            loc = []
            for k in ("node_id", "plugin_id", "tool_name", "model_name"):
                if e.get(k):
                    loc.append(f"{k}={e[k]}")
            suffix = f"  ({', '.join(loc)})" if loc else ""
            lines.append(f"  - {e['message']}{suffix}")
            if e.get("fix_hint"):
                lines.append(f"    修復: {e['fix_hint']}")
        if len(items) > 10:
            lines.append(f"  ... 他 {len(items)-10} 件")
    return "\n".join(lines)


def main(dsl_text: str, plugin_cache: dict | None = None) -> dict:
    """
    Dify コードノード用。外部 HTTP が使えない sandbox では plugin_cache を渡す:
      plugin_cache = {
        "langgenius/openai": { "manifest.yaml": "...", "provider/openai.yaml": "...", ... },
        ...
      }
    """
    try:
        dsl = _parse_yaml(dsl_text)
    except Exception as e:
        return {
            "is_valid": False,
            "errors": [_err("PARSE_ERROR", f"YAML パース失敗: {e}")],
            "report": f"NG: パース失敗\n{e}",
            "summary": {"error_count": 1, "warning_count": 0},
        }
    if not isinstance(dsl, dict):
        dsl = {}

    graph = ((dsl.get("workflow") or {}).get("graph")) or {}
    deps = dsl.get("dependencies") or []
    dep_types = _dependencies_types(deps)

    usages = collect_usages(graph)

    # plugin 定義の解決
    definitions: dict[str, PluginDefinition | None] = {}
    for u in usages:
        pid = u["plugin_id"]
        if pid in definitions:
            continue
        if plugin_cache and pid in plugin_cache:
            bundle = {"category": None, "files": plugin_cache[pid]}
            definitions[pid] = _build_definition(pid, u["version"], bundle)
        else:
            definitions[pid] = resolve_plugin(pid, u["version"])

    errors: list[dict] = []
    for u in usages:
        errors.extend(validate_usage(u, definitions[u["plugin_id"]],
                                      dep_types.get(u["plugin_id"])))

    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")
    return {
        "is_valid": err_cnt == 0,
        "errors": errors,
        "report": _build_report(errors, usages),
        "summary": {
            "error_count": err_cnt,
            "warning_count": warn_cnt,
            "usage_count": len(usages),
            "plugin_count": len(definitions),
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: uv run validate_dsl_plugin_usage.py <dsl.yml>",
              file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read()
    result = main(text)
    print(result["report"])
    sys.exit(0 if result["is_valid"] else 1)
