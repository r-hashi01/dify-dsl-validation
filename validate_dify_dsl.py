# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Dify DSL Validator

Difyソース (langgenius/dify) を参照し、以下の実スキーマ/実制約に準拠:

  - api/services/app_dsl_service.py
      * yaml.safe_load 結果が dict でないと FAILED
      * `app` キー必須 ("Missing app data in YAML content")
      * version / kind は欠落時に自動補完 (0.1.0 / "app")
      * dependencies は PluginDependency として検証される
  - api/core/workflow/workflow_entry.py
      * _NodeConfigDict: id/width/height/type/data
      * _EdgeConfigDict: source/target/sourceHandle/targetHandle (全必須)
  - api/core/workflow/node_factory.py
      * _START_NODE_TYPES = {start, datasource, trigger-webhook,
                             trigger-schedule, trigger-plugin}
      * get_default_root_node_id が start を見つけられないと import は通るが
        実行時 ValueError
      * top-level type == "custom-note" は root 判定でスキップされる
  - web/app/components/workflow/types.ts
      * BlockEnum で有効な data.type の全列挙
      * iteration/loop コンテナは内部に iteration-start / loop-start・loop-end
        を持つ

Dify の import 自体は edge の dangling target をチェックしない。このため
壊れたDSLでも import は成功し、編集画面で ReactFlow レンダリングが失敗して
「何も表示されない」状態になる。本ツールはその水準を埋める検証を行う。

2つのモード:
  1) Difyコードノード: `main(dsl_text: str) -> dict`
     出力: is_valid (boolean), errors (array[object]), report (string),
           graph (object), summary (object)
  2) CLI: `uv run validate_dify_dsl.py <file.yml>`
"""

from __future__ import annotations

import json
import re as _re
from collections import Counter, defaultdict

# Difyソース由来の定数
# api/core/trigger/constants.py + api/core/workflow/node_factory.py
START_NODE_TYPES = frozenset({
    "start",
    "datasource",
    "trigger-webhook",
    "trigger-schedule",
    "trigger-plugin",
})

# web/app/components/workflow/types.ts の BlockEnum
TERMINAL_NODE_TYPES = frozenset({"end", "answer"})

# コンテナ内部に自動生成される補助ノード (単独で存在しうる)
CONTAINER_CHILD_TYPES = frozenset({"iteration-start", "loop-start", "loop-end"})

VALID_NODE_TYPES = frozenset({
    "start", "end", "answer", "llm", "knowledge-retrieval",
    "question-classifier", "if-else", "code", "template-transform",
    "http-request", "variable-assigner", "variable-aggregator", "tool",
    "parameter-extractor", "iteration", "iteration-start", "assigner",
    "document-extractor", "list-operator", "agent", "loop", "loop-start",
    "loop-end", "human-input", "datasource", "datasource-empty",
    "knowledge-index", "trigger-schedule", "trigger-webhook", "trigger-plugin",
})

# top-level の `type` で無視すべきもの (node_factory.py)
SKIP_TOPLEVEL_TYPES = frozenset({"custom-note"})

# plugin_unique_identifier の正規表現
# dify-plugin-daemon/pkg/entities/plugin_entities/identity.go
# 形式: "[author/]plugin_id:version@checksum"
PLUGIN_UNIQUE_ID_RE = _re.compile(
    r"^(?:([a-z0-9_-]{1,64})/)?([a-z0-9_-]{1,255}):"
    r"([0-9]{1,4})(\.[0-9]{1,4}){1,3}(-\w{1,16})?@[a-f0-9]{32,64}$"
)

# PluginDependency.Type (api/core/plugin/entities/plugin.py)
VALID_DEPENDENCY_TYPES = frozenset({"github", "marketplace", "package"})


def _parse(dsl_text: str):
    """Dify と同じく yaml.safe_load を優先。sandbox で yaml が無ければ json。"""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(dsl_text)
    except ImportError:
        return json.loads(dsl_text)


def _err(code: str, message: str, severity: str = "error", **extra) -> dict:
    e = {
        "code": code,
        "severity": severity,  # "error" | "warning"
        "message": message,
        "node_id": None,
        "edge_id": None,
        "fix_hint": None,
    }
    e.update(extra)
    return e


def _is_note(node: dict) -> bool:
    """custom-note (キャンバス上のメモ) は実行対象外なので検証スキップ。"""
    return node.get("type") in SKIP_TOPLEVEL_TYPES


def _validate_dependencies(deps: list) -> list[dict]:
    """
    PluginDependency (api/core/plugin/entities/plugin.py) スキーマ準拠:
      type: "github" | "marketplace" | "package"
      value: type ごとに構造が違う discriminated union
    """
    errs: list[dict] = []
    for i, d in enumerate(deps):
        prefix = f"dependencies[{i}]"
        if not isinstance(d, dict):
            errs.append(_err("DEP_NOT_MAPPING", f"{prefix} が mapping でない"))
            continue

        dtype = d.get("type")
        value = d.get("value")
        if dtype not in VALID_DEPENDENCY_TYPES:
            errs.append(_err(
                "DEP_INVALID_TYPE",
                f"{prefix}.type が不正: {dtype!r} (許可: github/marketplace/package)",
                fix_hint="type を github/marketplace/package のいずれかに",
            ))
            continue
        if not isinstance(value, dict):
            errs.append(_err("DEP_MISSING_VALUE",
                             f"{prefix}.value が mapping でない"))
            continue

        uid = None
        if dtype == "marketplace":
            uid = value.get("marketplace_plugin_unique_identifier")
            if not uid:
                errs.append(_err(
                    "DEP_MARKETPLACE_MISSING_UID",
                    f"{prefix}: marketplace_plugin_unique_identifier が無い",
                    fix_hint="value.marketplace_plugin_unique_identifier を追加",
                ))
        elif dtype == "github":
            for key in ("repo", "version", "package",
                        "github_plugin_unique_identifier"):
                if not value.get(key):
                    errs.append(_err(
                        "DEP_GITHUB_MISSING_FIELD",
                        f"{prefix}: github dependency に {key} が無い",
                        fix_hint=f"value.{key} を追加",
                    ))
            uid = value.get("github_plugin_unique_identifier")
        elif dtype == "package":
            uid = value.get("plugin_unique_identifier")
            if not uid:
                errs.append(_err(
                    "DEP_PACKAGE_MISSING_UID",
                    f"{prefix}: plugin_unique_identifier が無い",
                ))

        if uid and not PLUGIN_UNIQUE_ID_RE.match(uid):
            errs.append(_err(
                "DEP_INVALID_UID_FORMAT",
                f"{prefix}: plugin_unique_identifier '{uid}' が "
                f"形式 '[author/]plugin_id:version@checksum' に合致しない",
                fix_hint="形式: langgenius/google:0.1.1@<32-64桁hex> (小文字)",
            ))
    return errs


def _collect_plugin_references(graph: dict) -> set[str]:
    """
    nodes から参照されている provider_id / plugin_id を収集。
    tool, llm, agent, datasource, trigger ノードを走査。
    """
    refs: set[str] = set()
    for n in (graph or {}).get("nodes") or []:
        if not isinstance(n, dict):
            continue
        data = n.get("data") or {}
        ntype = data.get("type")
        # tool node
        if ntype == "tool":
            pid = data.get("provider_id") or data.get("plugin_id")
            if pid:
                refs.add(str(pid))
        # llm / agent: provider under model config
        if ntype in ("llm", "parameter-extractor", "question-classifier",
                     "agent", "knowledge-retrieval"):
            model = data.get("model") or {}
            prov = model.get("provider")
            if prov:
                refs.add(str(prov))
        # datasource / trigger nodes
        if ntype in ("datasource", "trigger-webhook",
                     "trigger-schedule", "trigger-plugin"):
            pid = data.get("provider_id") or data.get("plugin_id")
            if pid:
                refs.add(str(pid))
    return refs


def _validate_plugin_references(graph: dict, deps: list) -> list[dict]:
    """ノードが参照する plugin が dependencies で宣言されているか。"""
    if not isinstance(deps, list):
        return []
    declared_ids: set[str] = set()
    for d in deps:
        if not isinstance(d, dict):
            continue
        value = d.get("value") or {}
        uid = (value.get("marketplace_plugin_unique_identifier")
               or value.get("github_plugin_unique_identifier")
               or value.get("plugin_unique_identifier") or "")
        if not uid:
            continue
        # "author/name:version@hash" から "author/name" を取り出す
        plugin_id = uid.split(":", 1)[0]
        declared_ids.add(plugin_id)
        # provider は "name" だけで参照されることも多いので、name単体でも登録
        if "/" in plugin_id:
            declared_ids.add(plugin_id.split("/", 1)[1])

    errs: list[dict] = []
    for ref in _collect_plugin_references(graph):
        # 参照は "author/name" または "name" または "name/tool_name" 形式
        head = ref.split("/", 1)[0] if "/" in ref else ref
        ref_base = ref.rsplit("/", 1)[-1] if "/" in ref else ref
        if ref in declared_ids or head in declared_ids or ref_base in declared_ids:
            continue
        errs.append(_err(
            "UNDECLARED_PLUGIN_REFERENCE",
            f"ノードが参照する plugin '{ref}' が dependencies に宣言されていない",
            severity="warning",
            fix_hint=f"dependencies に '{ref}' を marketplace or github で追加",
        ))
    return errs


def validate(dsl: dict) -> list[dict]:
    errors: list[dict] = []

    # --- app_dsl_service.py と同じトップレベル検証 ---
    if not isinstance(dsl, dict):
        return [_err("ROOT_NOT_MAPPING",
                     "YAML トップレベルが mapping ではない (Invalid YAML format)")]

    if "app" not in dsl or not dsl.get("app"):
        errors.append(_err(
            "MISSING_APP",
            "app キーが無い (import時に 'Missing app data in YAML content' で FAILED)",
            fix_hint="app: {name, mode, icon, ...} を追加",
        ))

    if "kind" not in dsl:
        errors.append(_err("MISSING_KIND", "kind キーが無い (import時に 'app' が自動補完される)",
                           severity="warning", fix_hint="kind: app を追加"))
    elif dsl.get("kind") != "app":
        errors.append(_err("INVALID_KIND", f"kind は 'app' 固定 (現在: {dsl.get('kind')!r})",
                           severity="warning", fix_hint="kind: app に変更"))

    if "version" not in dsl:
        errors.append(_err("MISSING_VERSION", "version キーが無い (0.1.0 が自動補完される)",
                           severity="warning"))
    elif not isinstance(dsl.get("version"), str):
        errors.append(_err("INVALID_VERSION_TYPE",
                           f"version は str 必須 (現在: {type(dsl.get('version')).__name__})"))

    # --- dependencies 検証 (PluginDependency スキーマ準拠) ---
    deps = dsl.get("dependencies")
    if "dependencies" in dsl:
        if not isinstance(deps, list):
            errors.append(_err("INVALID_DEPENDENCIES",
                               "dependencies は list でなければならない"))
        else:
            errors.extend(_validate_dependencies(deps))

    app_mode = (dsl.get("app") or {}).get("mode") if isinstance(dsl.get("app"), dict) else None
    needs_workflow = app_mode in ("workflow", "advanced-chat")

    # --- workflow / graph 検証 ---
    wf = dsl.get("workflow")
    if needs_workflow and not wf:
        errors.append(_err("MISSING_WORKFLOW",
                           f"app.mode={app_mode!r} は workflow 必須だが workflow キーが無い"))
        return errors
    if not wf:
        return errors  # chatbot など single-node モードは以降スキップ

    if not isinstance(wf, dict):
        errors.append(_err("INVALID_WORKFLOW", "workflow は mapping でなければならない"))
        return errors

    graph = wf.get("graph")
    if not isinstance(graph, dict):
        errors.append(_err("MISSING_GRAPH", "workflow.graph が無い / mapping でない"))
        return errors

    nodes = graph.get("nodes")
    edges = graph.get("edges")

    if not isinstance(nodes, list):
        # node_factory.py: "nodes in workflow graph must be a list"
        errors.append(_err("INVALID_GRAPH_NODES",
                           "workflow.graph.nodes が list でない "
                           "(Dify実行時: 'nodes in workflow graph must be a list')"))
        nodes = []
    if not isinstance(edges, list):
        errors.append(_err("INVALID_GRAPH_EDGES", "workflow.graph.edges が list でない"))
        edges = []

    # ノート以外の実行対象ノードだけを検証対象にする
    exec_nodes = [n for n in nodes if isinstance(n, dict) and not _is_note(n)]

    # --- ノード個別検証 (_NodeConfigDict 準拠) ---
    node_ids: list[str] = []
    nodes_by_id: dict[str, dict] = {}
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            errors.append(_err("NODE_NOT_MAPPING", f"nodes[{i}] が mapping でない"))
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append(_err("NODE_MISSING_ID",
                               f"nodes[{i}] に id が無い、または str でない"))
            continue
        node_ids.append(nid)
        nodes_by_id[nid] = n

        if _is_note(n):
            continue

        data = n.get("data")
        if not isinstance(data, dict):
            errors.append(_err("NODE_MISSING_DATA",
                               f"node {nid} に data が無い / mapping でない",
                               node_id=nid, fix_hint="data: {type, title, ...} を追加"))
            continue

        ntype = data.get("type")
        if not isinstance(ntype, str) or not ntype:
            errors.append(_err("NODE_MISSING_TYPE",
                               f"node {nid} に data.type が無い",
                               node_id=nid, fix_hint="data.type をBlockEnum値から設定"))
        elif ntype not in VALID_NODE_TYPES:
            errors.append(_err("UNKNOWN_NODE_TYPE",
                               f"node {nid}: 未知の data.type={ntype!r}",
                               severity="warning", node_id=nid,
                               fix_hint="BlockEnum の値を使用 (start/end/llm/code/if-else/...)"))

        # ReactFlow レンダリングに必要
        if "position" not in n or not isinstance(n.get("position"), dict):
            errors.append(_err("NODE_MISSING_POSITION",
                               f"node {nid} に position が無い (キャンバス描画不能)",
                               node_id=nid, fix_hint="position: {x: <num>, y: <num>} を追加"))

    # 重複ID
    for dup, cnt in Counter(node_ids).items():
        if cnt > 1:
            errors.append(_err("DUPLICATE_NODE_ID",
                               f"node id 重複: {dup} ({cnt}回)", node_id=dup,
                               fix_hint="片方の id をユニーク化"))

    # --- エッジ検証 (_EdgeConfigDict 準拠) ---
    edge_ids: list[str] = []
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    id_set = set(node_ids)

    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            errors.append(_err("EDGE_NOT_MAPPING", f"edges[{i}] が mapping でない"))
            continue
        eid = e.get("id") or f"(index {i})"
        edge_ids.append(eid)

        src = e.get("source")
        tgt = e.get("target")
        src_handle = e.get("sourceHandle")
        tgt_handle = e.get("targetHandle")

        if not src:
            errors.append(_err("EDGE_MISSING_SOURCE",
                               f"edge {eid}: source が無い", edge_id=eid))
        if not tgt:
            errors.append(_err("EDGE_MISSING_TARGET",
                               f"edge {eid}: target が無い", edge_id=eid))
        if src_handle is None:
            errors.append(_err("EDGE_MISSING_SOURCE_HANDLE",
                               f"edge {eid}: sourceHandle が無い",
                               severity="warning", edge_id=eid,
                               fix_hint="sourceHandle: 'source' (if-else なら 'true'/'false'/'elif-xxx')"))
        if tgt_handle is None:
            errors.append(_err("EDGE_MISSING_TARGET_HANDLE",
                               f"edge {eid}: targetHandle が無い",
                               severity="warning", edge_id=eid,
                               fix_hint="targetHandle: 'target' を追加"))

        if src and src not in id_set:
            errors.append(_err("EDGE_DANGLING_SOURCE",
                               f"edge {eid}: source ノード '{src}' が nodes に存在しない",
                               edge_id=eid,
                               fix_hint=f"'{src}' を nodes に追加、またはこの edge を削除"))
        if tgt and tgt not in id_set:
            errors.append(_err("EDGE_DANGLING_TARGET",
                               f"edge {eid}: target ノード '{tgt}' が nodes に存在しない",
                               edge_id=eid,
                               fix_hint=f"'{tgt}' を nodes に追加、またはこの edge を削除"))

        if src and tgt:
            outgoing[src].append((tgt, src_handle or ""))
            incoming[tgt].append(src)

    for dup, cnt in Counter(edge_ids).items():
        if cnt > 1:
            errors.append(_err("DUPLICATE_EDGE_ID",
                               f"edge id 重複: {dup} ({cnt}回)", edge_id=dup))

    # --- Start ノード (node_factory.get_default_root_node_id 準拠) ---
    starts = [
        n for n in exec_nodes
        if (n.get("data") or {}).get("type") in START_NODE_TYPES
    ]
    terminals = [
        n for n in exec_nodes
        if (n.get("data") or {}).get("type") in TERMINAL_NODE_TYPES
    ]

    if not starts:
        errors.append(_err(
            "NO_START_NODE",
            "開始ノードが無い (start/datasource/trigger-* のいずれも不在)。"
            "Dify実行時に 'Unable to determine default root node ID' で失敗",
            fix_hint="data.type が start/datasource/trigger-webhook/trigger-schedule/trigger-plugin のノードを追加",
        ))
    elif len(starts) > 1:
        errors.append(_err("MULTIPLE_START_NODES",
                           f"開始ノードが複数 ({len(starts)})。先頭1つだけがrootとして使われる",
                           severity="warning"))

    # end/answer は app.mode によって必須性が変わるが、無いとフロー終端が不明瞭
    if not terminals:
        errors.append(_err("NO_TERMINAL_NODE",
                           "end / answer ノードが無い",
                           severity="warning",
                           fix_hint="data.type=end (workflow) または answer (advanced-chat) を追加"))

    # --- 到達可能性 (start から BFS) ---
    if starts:
        start_id = starts[0]["id"]
        seen = {start_id}
        stack = [start_id]
        while stack:
            cur = stack.pop()
            for nxt, _h in outgoing.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        for n in exec_nodes:
            nid = n.get("id")
            ntype = (n.get("data") or {}).get("type")
            if ntype in CONTAINER_CHILD_TYPES:
                continue  # iteration/loop 内部の補助ノードは親経由で扱われる
            if nid and nid not in seen:
                errors.append(_err(
                    "UNREACHABLE_NODE",
                    f"node {nid} ({ntype}) は開始ノードから到達不能",
                    node_id=nid,
                    fix_hint="incoming edge を追加、または不要なら削除",
                ))

    # --- 出口なしノード ---
    for n in exec_nodes:
        nid = n.get("id")
        ntype = (n.get("data") or {}).get("type")
        if ntype in TERMINAL_NODE_TYPES or ntype in CONTAINER_CHILD_TYPES:
            continue
        if nid and not outgoing.get(nid):
            errors.append(_err(
                "NODE_NO_OUTGOING",
                f"node {nid} ({ntype}) に outgoing edge が無い (行き止まり)",
                node_id=nid,
                fix_hint="次ノードへの edge を追加、または end/answer へ接続",
            ))

    # --- 宣言されていない plugin 参照の検知 ---
    if isinstance(deps, list):
        errors.extend(_validate_plugin_references(graph, deps))

    # --- iteration / loop コンテナの内部整合性 ---
    for n in exec_nodes:
        nid = n.get("id")
        ntype = (n.get("data") or {}).get("type")
        if ntype == "iteration":
            children = [
                m for m in exec_nodes
                if (m.get("data") or {}).get("iteration_id") == nid
                or (m.get("data") or {}).get("isInIteration")
                and m.get("parentId") == nid
            ]
            if not any((c.get("data") or {}).get("type") == "iteration-start" for c in children):
                errors.append(_err(
                    "ITERATION_MISSING_START",
                    f"iteration node {nid} に iteration-start 子ノードが無い",
                    node_id=nid,
                    fix_hint="iteration-start ノードを子として追加",
                ))
        if ntype == "loop":
            children = [
                m for m in exec_nodes
                if (m.get("data") or {}).get("loop_id") == nid
                or (m.get("data") or {}).get("isInLoop")
                and m.get("parentId") == nid
            ]
            types = {(c.get("data") or {}).get("type") for c in children}
            if "loop-start" not in types:
                errors.append(_err("LOOP_MISSING_START",
                                   f"loop node {nid} に loop-start 子ノードが無い",
                                   node_id=nid))
            if "loop-end" not in types:
                errors.append(_err("LOOP_MISSING_END",
                                   f"loop node {nid} に loop-end 子ノードが無い",
                                   node_id=nid))

    return errors


def _build_report(errors: list[dict], dsl: dict) -> str:
    wf = (dsl or {}).get("workflow") or {}
    g = wf.get("graph") or {}
    n_cnt = len(g.get("nodes") or [])
    e_cnt = len(g.get("edges") or [])

    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")

    if not errors:
        return f"OK: DSL は妥当 (nodes={n_cnt}, edges={e_cnt})"

    lines = [
        f"{'NG' if err_cnt else 'WARN'}: error={err_cnt}, warning={warn_cnt} "
        f"(nodes={n_cnt}, edges={e_cnt})",
        "",
    ]
    by_code: dict[str, list[dict]] = defaultdict(list)
    for e in errors:
        by_code[e["code"]].append(e)
    for code, items in by_code.items():
        sev = items[0]["severity"].upper()
        lines.append(f"[{sev}][{code}] x{len(items)}")
        for e in items[:10]:
            loc = []
            if e.get("node_id"):
                loc.append(f"node={e['node_id']}")
            if e.get("edge_id"):
                loc.append(f"edge={e['edge_id']}")
            suffix = f"  ({', '.join(loc)})" if loc else ""
            lines.append(f"  - {e['message']}{suffix}")
            if e.get("fix_hint"):
                lines.append(f"    修復: {e['fix_hint']}")
        if len(items) > 10:
            lines.append(f"  ... 他 {len(items)-10} 件")
    return "\n".join(lines)


# ---- Dify コードノード エントリーポイント ----
def main(dsl_text: str) -> dict:
    try:
        dsl = _parse(dsl_text)
    except Exception as ex:
        return {
            "is_valid": False,
            "errors": [_err("PARSE_ERROR", f"パース失敗: {ex}",
                            fix_hint="YAML/JSON 構文を確認")],
            "report": f"NG: パース失敗\n{ex}",
            "graph": {},
            "summary": {"error_count": 1, "warning_count": 0,
                        "node_count": 0, "edge_count": 0},
        }

    if not isinstance(dsl, dict):
        dsl = {}

    errors = validate(dsl)
    graph = ((dsl or {}).get("workflow") or {}).get("graph") or {}
    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")
    return {
        "is_valid": err_cnt == 0,
        "errors": errors,
        "report": _build_report(errors, dsl),
        "graph": graph,
        "summary": {
            "error_count": err_cnt,
            "warning_count": warn_cnt,
            "node_count": len(graph.get("nodes") or []),
            "edge_count": len(graph.get("edges") or []),
        },
    }


# ---- CLI ----
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: uv run validate_dify_dsl.py <dsl.yml>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read()
    result = main(text)
    print(result["report"])
    sys.exit(0 if result["is_valid"] else 1)
