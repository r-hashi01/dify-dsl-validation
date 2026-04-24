# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Dify Plugin Manifest Validator

dify-plugin-daemon の Go 実装 (pkg/entities/plugin_entities/) に準拠し、
プラグインの manifest.yaml ならびに tools/models/endpoints/... の
provider/tool YAML の構造を検証する。

参照した実装:
  - plugin_entities/plugin_declaration.go  (PluginDeclarationWithoutAdvancedFields)
  - plugin_entities/basic_type.go          (I18nObject)
  - plugin_entities/identity.go            (PluginUniqueIdentifier regex)
  - plugin_entities/tool_declaration.go    (ToolProviderDeclaration / ToolDeclaration)
  - plugin_entities/constant.go            (parameter type 定数)
  - manifest_entities/manifest.go          (DifyManifestType = plugin|bundle)
  - manifest_entities/version.go           (PluginDeclarationVersionRegex)
  - manifest_entities/tags.go              (PluginTag 列挙)
  - constants/arch.go                      (amd64|arm64)
  - constants/language.go                  (python)

使い方:
  # プラグインディレクトリ (manifest.yaml を含む) を検証
  uv run validate_dify_plugin.py /path/to/plugin_dir

  # manifest.yaml 単体を検証 (リファレンスファイルチェックはスキップ)
  uv run validate_dify_plugin.py /path/to/manifest.yaml

  # Dify コードノード用: main(manifest_text: str, provider_files: dict = None) -> dict
"""

from __future__ import annotations
import json
import os
import re
from collections import defaultdict
from pathlib import Path


# ---- dify-plugin-daemon 由来の定数/正規表現 ----

# manifest_entities/version.go
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,4}){2}(-\w{1,16})?$")

# plugin_entities/identity.go
PLUGIN_UNIQUE_ID_RE = re.compile(
    r"^(?:([a-z0-9_-]{1,64})/)?([a-z0-9_-]{1,255}):"
    r"([0-9]{1,4})(\.[0-9]{1,4}){1,3}(-\w{1,16})?@[a-f0-9]{32,64}$"
)

# constants/arch.go
VALID_ARCHS = frozenset({"amd64", "arm64"})

# constants/language.go
VALID_LANGUAGES = frozenset({"python"})

# manifest_entities/tags.go
VALID_PLUGIN_TAGS = frozenset({
    "search", "image", "videos", "weather", "finance", "design", "travel",
    "social", "news", "medical", "productivity", "education", "business",
    "entertainment", "utilities", "agent", "rag", "other", "trigger",
})

# plugin_declaration.go: PluginCategory (ただし自動判定なのでmanifestには書かない)
VALID_CATEGORIES = frozenset({
    "tool", "model", "extension", "agent-strategy", "datasource", "trigger",
})

# plugin_entities/constant.go: tool parameter type
VALID_PARAM_TYPES = frozenset({
    "secret-input", "text-input", "select", "string", "number", "file", "files",
    "boolean", "app-selector", "model-selector", "array[tools]", "any",
    "dynamic-select", "array", "object", "checkbox",
})

# tool_declaration.go: ToolParameterForm
VALID_PARAM_FORMS = frozenset({"schema", "form", "llm"})

# プラグインカテゴリごとに plugins.<key> に列挙されるべきファイルのキー
EXTENSION_KEYS = ("tools", "models", "endpoints", "agent_strategies",
                  "datasources", "triggers")


def _parse(text: str):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return json.loads(text)


def _err(code: str, message: str, severity: str = "error", **extra) -> dict:
    e = {
        "code": code,
        "severity": severity,
        "message": message,
        "path": None,       # manifest内のJSONPath風キー
        "file": None,       # 検証対象のファイルパス
        "fix_hint": None,
    }
    e.update(extra)
    return e


# ---- I18n / 基本型 ----

def _validate_i18n(obj, path: str, *, required: bool = True) -> list[dict]:
    errs: list[dict] = []
    if obj is None:
        if required:
            errs.append(_err("I18N_MISSING", f"{path} が無い (I18nObject 必須)",
                             path=path, fix_hint=f"{path}.en_US を最低限設定"))
        return errs
    if not isinstance(obj, dict):
        errs.append(_err("I18N_NOT_MAPPING", f"{path} が mapping でない", path=path))
        return errs
    en = obj.get("en_US")
    if required and not en:
        errs.append(_err("I18N_EN_US_REQUIRED",
                         f"{path}.en_US が必須 (required,gt=0,lt=1024)",
                         path=path, fix_hint=f"{path}.en_US を設定"))
    for k in ("en_US", "ja_JP", "zh_Hans", "pt_BR"):
        v = obj.get(k)
        if v is not None and isinstance(v, str) and len(v) >= 1024:
            errs.append(_err("I18N_TOO_LONG",
                             f"{path}.{k} は 1024 文字未満必須", path=path))
    return errs


# ---- manifest.yaml 検証 ----

def validate_manifest(manifest: dict) -> list[dict]:
    """PluginDeclarationWithoutAdvancedFields 相当の検証。"""
    errs: list[dict] = []

    if not isinstance(manifest, dict):
        return [_err("ROOT_NOT_MAPPING",
                     "manifest.yaml のトップレベルが mapping でない")]

    # type: required, eq=plugin
    mtype = manifest.get("type")
    if mtype is None:
        errs.append(_err("MANIFEST_MISSING_TYPE",
                         "type が無い (required, 'plugin' 固定)",
                         path="type", fix_hint='type: plugin を追加'))
    elif mtype != "plugin":
        errs.append(_err("MANIFEST_INVALID_TYPE",
                         f"type は 'plugin' 固定 (現在: {mtype!r})",
                         path="type"))

    # version: required, version pattern
    v = manifest.get("version")
    if not v:
        errs.append(_err("MANIFEST_MISSING_VERSION",
                         "version が無い (required, N.N.N[-suffix] 形式)",
                         path="version"))
    elif not isinstance(v, str) or not VERSION_RE.match(v):
        errs.append(_err("MANIFEST_INVALID_VERSION",
                         f"version '{v}' は N.N.N[-suffix] 形式必須 (例: 0.1.1)",
                         path="version",
                         fix_hint="0.1.0 のような 3 ドット区切り数字"))

    # name: required, max=128
    name = manifest.get("name")
    if not name:
        errs.append(_err("MANIFEST_MISSING_NAME", "name が無い (required)",
                         path="name"))
    elif not isinstance(name, str) or len(name) > 128:
        errs.append(_err("MANIFEST_INVALID_NAME",
                         "name は 128 文字以下の文字列", path="name"))

    # author: optional, max=64
    author = manifest.get("author")
    if author is not None and (not isinstance(author, str) or len(author) > 64):
        errs.append(_err("MANIFEST_INVALID_AUTHOR",
                         "author は 64 文字以下の文字列", path="author"))

    # label / description: required I18nObject
    errs.extend(_validate_i18n(manifest.get("label"), "label"))
    errs.extend(_validate_i18n(manifest.get("description"), "description"))

    # icon: required, max=128
    icon = manifest.get("icon")
    if not icon:
        errs.append(_err("MANIFEST_MISSING_ICON",
                         "icon が無い (required, max=128)", path="icon"))
    elif not isinstance(icon, str) or len(icon) > 128:
        errs.append(_err("MANIFEST_INVALID_ICON",
                         "icon は 128 文字以下のパス文字列", path="icon"))

    icon_dark = manifest.get("icon_dark")
    if icon_dark is not None and (not isinstance(icon_dark, str)
                                   or len(icon_dark) > 128):
        errs.append(_err("MANIFEST_INVALID_ICON_DARK",
                         "icon_dark は 128 文字以下", path="icon_dark"))

    # created_at: required
    if "created_at" not in manifest or not manifest.get("created_at"):
        errs.append(_err("MANIFEST_MISSING_CREATED_AT",
                         "created_at が無い (required, RFC3339)",
                         path="created_at"))

    # tags: optional list, 各要素は PluginTag
    tags = manifest.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            errs.append(_err("MANIFEST_INVALID_TAGS",
                             "tags は list でなければならない", path="tags"))
        else:
            for i, t in enumerate(tags):
                if t not in VALID_PLUGIN_TAGS:
                    errs.append(_err(
                        "MANIFEST_UNKNOWN_TAG",
                        f"tags[{i}]='{t}' は既知のタグでない",
                        severity="warning", path=f"tags[{i}]",
                        fix_hint=f"許可: {', '.join(sorted(VALID_PLUGIN_TAGS))}",
                    ))

    # repo: optional URL
    repo = manifest.get("repo")
    if repo is not None and isinstance(repo, str):
        if not re.match(r"^https?://", repo):
            errs.append(_err("MANIFEST_INVALID_REPO",
                             f"repo はURL必須 (現在: {repo!r})",
                             severity="warning", path="repo"))

    # resource: required, memory required
    errs.extend(_validate_resource(manifest.get("resource")))

    # meta: required
    errs.extend(_validate_meta(manifest.get("meta")))

    # plugins: required, extension 種類別にファイル列を列挙
    errs.extend(_validate_plugins_section(manifest.get("plugins")))

    # provider 定義 (tool/model/endpoint/...) は別ファイルに分離される設計だが
    # 直接埋め込む場合もあるので present なら最低限チェック
    for key in ("tool", "model", "endpoint", "agent_strategy",
                "datasource", "trigger"):
        v = manifest.get(key)
        if v is not None and not isinstance(v, dict):
            errs.append(_err(
                f"MANIFEST_INVALID_{key.upper()}",
                f"{key} が mapping でない", path=key,
            ))

    return errs


def _validate_resource(resource) -> list[dict]:
    errs: list[dict] = []
    if not resource:
        return [_err("MANIFEST_MISSING_RESOURCE",
                     "resource が無い (required)", path="resource",
                     fix_hint="resource: {memory: 1048576, permission: {...}}")]
    if not isinstance(resource, dict):
        return [_err("MANIFEST_INVALID_RESOURCE",
                     "resource が mapping でない", path="resource")]
    mem = resource.get("memory")
    if mem is None:
        errs.append(_err("MANIFEST_MISSING_MEMORY",
                         "resource.memory が無い (required)",
                         path="resource.memory",
                         fix_hint="bytes単位で指定 (例: 1048576 = 1MB)"))
    elif not isinstance(mem, int) or mem <= 0:
        errs.append(_err("MANIFEST_INVALID_MEMORY",
                         "resource.memory は正の整数 (bytes)",
                         path="resource.memory"))

    perm = resource.get("permission")
    if perm is not None:
        if not isinstance(perm, dict):
            errs.append(_err("MANIFEST_INVALID_PERMISSION",
                             "resource.permission が mapping でない",
                             path="resource.permission"))
        else:
            storage = perm.get("storage")
            if isinstance(storage, dict):
                size = storage.get("size")
                # validate:"min=1024,max=1073741824"
                if size is not None and isinstance(size, int):
                    if size < 1024 or size > 1073741824:
                        errs.append(_err(
                            "MANIFEST_INVALID_STORAGE_SIZE",
                            f"storage.size は 1024..1073741824 (現在: {size})",
                            path="resource.permission.storage.size",
                        ))
    return errs


def _validate_meta(meta) -> list[dict]:
    errs: list[dict] = []
    if not meta:
        return [_err("MANIFEST_MISSING_META", "meta が無い (required)",
                     path="meta",
                     fix_hint="meta.version / arch / runner が必須")]
    if not isinstance(meta, dict):
        return [_err("MANIFEST_INVALID_META", "meta が mapping でない",
                     path="meta")]

    v = meta.get("version")
    if not v:
        errs.append(_err("META_MISSING_VERSION",
                         "meta.version が無い (required, N.N.N 形式)",
                         path="meta.version"))
    elif not isinstance(v, str) or not VERSION_RE.match(v):
        errs.append(_err("META_INVALID_VERSION",
                         f"meta.version '{v}' は N.N.N[-suffix] 形式",
                         path="meta.version"))

    arch = meta.get("arch")
    if not arch:
        errs.append(_err("META_MISSING_ARCH",
                         "meta.arch が無い (required, [amd64|arm64])",
                         path="meta.arch",
                         fix_hint="arch: [amd64, arm64]"))
    elif not isinstance(arch, list) or not arch:
        errs.append(_err("META_INVALID_ARCH",
                         "meta.arch は非空 list", path="meta.arch"))
    else:
        for i, a in enumerate(arch):
            if a not in VALID_ARCHS:
                errs.append(_err(
                    "META_UNKNOWN_ARCH",
                    f"meta.arch[{i}]='{a}' は無効 (amd64|arm64 のみ)",
                    path=f"meta.arch[{i}]",
                ))

    runner = meta.get("runner")
    if not runner:
        errs.append(_err("META_MISSING_RUNNER",
                         "meta.runner が無い (required)", path="meta.runner"))
    elif not isinstance(runner, dict):
        errs.append(_err("META_INVALID_RUNNER",
                         "meta.runner が mapping でない", path="meta.runner"))
    else:
        lang = runner.get("language")
        if lang not in VALID_LANGUAGES:
            errs.append(_err(
                "META_INVALID_LANGUAGE",
                f"meta.runner.language='{lang}' は無効 (python のみサポート)",
                path="meta.runner.language",
            ))
        if not runner.get("version"):
            errs.append(_err("META_MISSING_RUNNER_VERSION",
                             "meta.runner.version が無い",
                             path="meta.runner.version"))
        if not runner.get("entrypoint"):
            errs.append(_err("META_MISSING_ENTRYPOINT",
                             "meta.runner.entrypoint が無い (required)",
                             path="meta.runner.entrypoint",
                             fix_hint="entrypoint: main (通常 main.py)"))

    mdv = meta.get("minimum_dify_version")
    if mdv is not None and isinstance(mdv, str) and not VERSION_RE.match(mdv):
        errs.append(_err(
            "META_INVALID_MIN_DIFY_VERSION",
            f"meta.minimum_dify_version '{mdv}' は N.N.N 形式",
            severity="warning", path="meta.minimum_dify_version",
        ))
    return errs


def _validate_plugins_section(plugins) -> list[dict]:
    errs: list[dict] = []
    if plugins is None:
        return [_err("MANIFEST_MISSING_PLUGINS",
                     "plugins セクションが無い (required)", path="plugins",
                     fix_hint="plugins: {tools: [...], models: [...], ...}")]
    if not isinstance(plugins, dict):
        return [_err("MANIFEST_INVALID_PLUGINS",
                     "plugins が mapping でない", path="plugins")]

    for key in plugins.keys():
        if key not in EXTENSION_KEYS:
            errs.append(_err(
                "MANIFEST_UNKNOWN_PLUGINS_KEY",
                f"plugins.{key} は未知のキー "
                f"(許可: {', '.join(EXTENSION_KEYS)})",
                severity="warning", path=f"plugins.{key}",
            ))

    # 少なくとも1つの extension が宣言されている必要 (空はありうるが警告)
    total = 0
    for key in EXTENSION_KEYS:
        arr = plugins.get(key)
        if arr is None:
            continue
        if not isinstance(arr, list):
            errs.append(_err("MANIFEST_PLUGINS_KEY_NOT_LIST",
                             f"plugins.{key} は list", path=f"plugins.{key}"))
            continue
        total += len(arr)
        for i, p in enumerate(arr):
            if not isinstance(p, str) or not p:
                errs.append(_err(
                    "MANIFEST_PLUGINS_ITEM_INVALID",
                    f"plugins.{key}[{i}] はファイルパス文字列必須",
                    path=f"plugins.{key}[{i}]",
                ))
            elif len(p) > 128:
                errs.append(_err(
                    "MANIFEST_PLUGINS_ITEM_TOO_LONG",
                    f"plugins.{key}[{i}] は 128 文字以下",
                    path=f"plugins.{key}[{i}]",
                ))
    if total == 0:
        errs.append(_err(
            "MANIFEST_NO_EXTENSIONS",
            "plugins にいずれの extension (tools/models/endpoints/...) も"
            "宣言されていない",
            severity="warning", path="plugins",
            fix_hint="少なくとも 1 つの provider yaml を指定",
        ))
    return errs


# ---- provider YAML (tool/model/etc) 検証 ----

def validate_tool_provider(provider: dict, source: str = "",
                            plugin_root: Path | None = None) -> list[dict]:
    """ToolProviderDeclaration + ToolDeclaration の簡易検証。

    provider.tools は dify-official-plugins の実例では **ファイルパス文字列**
    のリスト (例: "tools/google_search.yaml") になっている。
    plugin_root が与えられればファイルを読んで ToolDeclaration を検証する。
    """
    errs: list[dict] = []
    if not isinstance(provider, dict):
        return [_err("PROVIDER_NOT_MAPPING",
                     f"{source}: provider が mapping でない", file=source)]

    identity = provider.get("identity")
    if not identity:
        errs.append(_err("TOOL_PROVIDER_MISSING_IDENTITY",
                         "identity が無い", file=source))
    elif not isinstance(identity, dict):
        errs.append(_err("TOOL_PROVIDER_INVALID_IDENTITY",
                         "identity が mapping でない", file=source))
    else:
        if not identity.get("author"):
            errs.append(_err("TOOL_PROVIDER_MISSING_AUTHOR",
                             "identity.author が無い", file=source))
        if not identity.get("name"):
            errs.append(_err("TOOL_PROVIDER_MISSING_NAME",
                             "identity.name が無い", file=source))
        if not identity.get("icon"):
            errs.append(_err("TOOL_PROVIDER_MISSING_ICON",
                             "identity.icon が無い (tool_provider は required)",
                             file=source))
        errs.extend(_validate_i18n(identity.get("label"),
                                    "identity.label", required=True))

    tools = provider.get("tools")
    if tools is None:
        errs.append(_err("TOOL_PROVIDER_MISSING_TOOLS",
                         "tools が無い (required)", file=source))
    elif not isinstance(tools, list):
        errs.append(_err("TOOL_PROVIDER_INVALID_TOOLS",
                         "tools が list でない", file=source))
    else:
        for i, t in enumerate(tools):
            if isinstance(t, str):
                # ファイルパス参照
                if plugin_root is None:
                    # 単体検証時はファイル解決できないので警告のみ
                    errs.append(_err(
                        "TOOL_FILE_NOT_RESOLVED",
                        f"tools[{i}]='{t}' はファイル参照 "
                        f"(plugin ディレクトリ検証でのみ中身を検証可能)",
                        severity="warning", file=source,
                    ))
                    continue
                fpath = plugin_root / t
                if not fpath.exists():
                    errs.append(_err(
                        "TOOL_FILE_MISSING",
                        f"tools[{i}]='{t}' のファイルが存在しない",
                        file=str(fpath),
                    ))
                    continue
                try:
                    tool_decl = _parse(fpath.read_text(encoding="utf-8"))
                except Exception as e:
                    errs.append(_err(
                        "TOOL_FILE_PARSE_ERROR",
                        f"{t}: YAML パース失敗: {e}", file=str(fpath),
                    ))
                    continue
                errs.extend(_validate_tool(tool_decl, f"tools[{i}]", str(fpath)))
            elif isinstance(t, dict):
                errs.extend(_validate_tool(t, f"tools[{i}]", source))
            else:
                errs.append(_err(
                    "TOOL_ITEM_INVALID",
                    f"tools[{i}] は文字列(ファイルパス)またはmapping",
                    file=source,
                ))

    return errs


def _validate_tool(tool, path: str, source: str) -> list[dict]:
    errs: list[dict] = []
    if not isinstance(tool, dict):
        return [_err("TOOL_NOT_MAPPING", f"{path} が mapping でない",
                     path=path, file=source)]

    identity = tool.get("identity")
    if not isinstance(identity, dict):
        errs.append(_err("TOOL_MISSING_IDENTITY",
                         f"{path}.identity が無い/mapping でない",
                         path=path, file=source))
    else:
        if not identity.get("author"):
            errs.append(_err("TOOL_IDENTITY_MISSING_AUTHOR",
                             f"{path}.identity.author が無い",
                             path=path, file=source))
        if not identity.get("name"):
            errs.append(_err("TOOL_IDENTITY_MISSING_NAME",
                             f"{path}.identity.name が無い",
                             path=path, file=source))
        errs.extend(_validate_i18n(identity.get("label"),
                                    f"{path}.identity.label", required=True))

    desc = tool.get("description")
    if not isinstance(desc, dict):
        errs.append(_err("TOOL_MISSING_DESCRIPTION",
                         f"{path}.description が無い/mapping でない",
                         path=path, file=source))
    else:
        errs.extend(_validate_i18n(desc.get("human"),
                                    f"{path}.description.human", required=True))
        if not desc.get("llm"):
            errs.append(_err("TOOL_DESC_MISSING_LLM",
                             f"{path}.description.llm が無い (required)",
                             path=path, file=source))

    params = tool.get("parameters")
    if params is not None:
        if not isinstance(params, list):
            errs.append(_err("TOOL_PARAMETERS_NOT_LIST",
                             f"{path}.parameters が list でない",
                             path=path, file=source))
        else:
            for j, p in enumerate(params):
                errs.extend(_validate_tool_parameter(
                    p, f"{path}.parameters[{j}]", source))
    return errs


def _validate_tool_parameter(p, path: str, source: str) -> list[dict]:
    errs: list[dict] = []
    if not isinstance(p, dict):
        return [_err("PARAM_NOT_MAPPING", f"{path} が mapping でない",
                     path=path, file=source)]
    name = p.get("name")
    if not name:
        errs.append(_err("PARAM_MISSING_NAME", f"{path}.name が無い",
                         path=path, file=source))
    elif not isinstance(name, str) or not (0 < len(name) < 1024):
        errs.append(_err("PARAM_INVALID_NAME",
                         f"{path}.name は 0<len<1024", path=path, file=source))

    ptype = p.get("type")
    if not ptype:
        errs.append(_err("PARAM_MISSING_TYPE", f"{path}.type が無い",
                         path=path, file=source))
    elif ptype not in VALID_PARAM_TYPES:
        errs.append(_err(
            "PARAM_UNKNOWN_TYPE",
            f"{path}.type='{ptype}' は既知でない",
            severity="warning", path=path, file=source,
            fix_hint=f"許可: {', '.join(sorted(VALID_PARAM_TYPES))}",
        ))

    form = p.get("form")
    if not form:
        errs.append(_err("PARAM_MISSING_FORM", f"{path}.form が無い",
                         path=path, file=source))
    elif form not in VALID_PARAM_FORMS:
        errs.append(_err("PARAM_INVALID_FORM",
                         f"{path}.form='{form}' は schema/form/llm のみ",
                         path=path, file=source))

    errs.extend(_validate_i18n(p.get("label"), f"{path}.label", required=True))
    errs.extend(_validate_i18n(p.get("human_description"),
                                f"{path}.human_description", required=True))
    return errs


# ---- ファイル参照整合性 (plugin ディレクトリ検証) ----

def validate_plugin_dir(plugin_dir: str) -> list[dict]:
    """プラグインディレクトリを包括的に検証。manifest.yaml + 参照yaml + icon。"""
    errs: list[dict] = []
    root = Path(plugin_dir)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        return [_err("DIR_MANIFEST_MISSING",
                     f"{manifest_path} が存在しない")]

    try:
        manifest = _parse(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [_err("DIR_MANIFEST_PARSE_ERROR",
                     f"manifest.yaml パース失敗: {e}",
                     file=str(manifest_path))]

    errs.extend(validate_manifest(manifest if isinstance(manifest, dict) else {}))

    if not isinstance(manifest, dict):
        return errs

    # icon は _assets/ 配下にある想定
    icon = manifest.get("icon")
    if icon:
        icon_candidates = [root / "_assets" / icon, root / icon]
        if not any(p.exists() for p in icon_candidates):
            errs.append(_err(
                "ICON_FILE_MISSING",
                f"icon '{icon}' が {root/'_assets'} 配下に見つからない",
                file=str(manifest_path),
                fix_hint=f"_assets/{icon} を配置、または icon パスを修正",
            ))

    # plugins.<key> の各ファイルを検証
    plugins = manifest.get("plugins") or {}
    for key in EXTENSION_KEYS:
        for rel in (plugins.get(key) or []):
            if not isinstance(rel, str):
                continue
            fpath = root / rel
            if not fpath.exists():
                errs.append(_err(
                    "PROVIDER_FILE_MISSING",
                    f"plugins.{key}: '{rel}' が存在しない",
                    file=str(fpath),
                    fix_hint=f"ファイルを配置、または manifest から該当行を削除",
                ))
                continue
            try:
                prov = _parse(fpath.read_text(encoding="utf-8"))
            except Exception as e:
                errs.append(_err(
                    "PROVIDER_FILE_PARSE_ERROR",
                    f"{rel}: YAML パース失敗: {e}",
                    file=str(fpath),
                ))
                continue

            if key == "tools":
                errs.extend(validate_tool_provider(
                    prov, source=str(fpath), plugin_root=root))
            # model/endpoint/agent_strategy/datasource/trigger の詳細スキーマは
            # 膨大なのでここでは mapping チェックのみ行う
            elif not isinstance(prov, dict):
                errs.append(_err(
                    "PROVIDER_NOT_MAPPING",
                    f"{rel}: provider が mapping でない",
                    file=str(fpath),
                ))

    return errs


# ---- レポート/エントリーポイント ----

def _build_report(errors: list[dict]) -> str:
    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")
    if not errors:
        return "OK: plugin manifest は妥当"
    lines = [f"{'NG' if err_cnt else 'WARN'}: error={err_cnt}, warning={warn_cnt}", ""]
    by_code: dict[str, list[dict]] = defaultdict(list)
    for e in errors:
        by_code[e["code"]].append(e)
    for code, items in by_code.items():
        sev = items[0]["severity"].upper()
        lines.append(f"[{sev}][{code}] x{len(items)}")
        for e in items[:10]:
            loc = []
            if e.get("path"): loc.append(f"path={e['path']}")
            if e.get("file"): loc.append(f"file={e['file']}")
            suffix = f"  ({', '.join(loc)})" if loc else ""
            lines.append(f"  - {e['message']}{suffix}")
            if e.get("fix_hint"):
                lines.append(f"    修復: {e['fix_hint']}")
        if len(items) > 10:
            lines.append(f"  ... 他 {len(items)-10} 件")
    return "\n".join(lines)


# ---- Dify コードノード エントリーポイント ----
def main(manifest_text: str) -> dict:
    """
    Dify コードノード用: manifest.yaml 本文を受け取り検証結果を返す。
    出力変数: is_valid (boolean), errors (array[object]), report (string),
              summary (object)
    ※ ファイル参照整合性 (プラグインディレクトリ検証) は別途 CLI で。
    """
    try:
        manifest = _parse(manifest_text)
    except Exception as ex:
        return {
            "is_valid": False,
            "errors": [_err("PARSE_ERROR", f"パース失敗: {ex}",
                            fix_hint="YAML 構文を確認")],
            "report": f"NG: パース失敗\n{ex}",
            "summary": {"error_count": 1, "warning_count": 0},
        }
    if not isinstance(manifest, dict):
        manifest = {}
    errors = validate_manifest(manifest)
    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")
    return {
        "is_valid": err_cnt == 0,
        "errors": errors,
        "report": _build_report(errors),
        "summary": {"error_count": err_cnt, "warning_count": warn_cnt},
    }


# ---- CLI ----
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: uv run validate_dify_plugin.py <plugin_dir | manifest.yaml>",
              file=sys.stderr)
        sys.exit(2)
    target = sys.argv[1]
    if os.path.isdir(target):
        errors = validate_plugin_dir(target)
    else:
        with open(target, encoding="utf-8") as f:
            manifest = _parse(f.read())
        errors = validate_manifest(manifest if isinstance(manifest, dict) else {})
    print(_build_report(errors))
    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    sys.exit(0 if err_cnt == 0 else 1)
