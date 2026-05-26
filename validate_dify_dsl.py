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
        # Difyのプラグイン参照は2層構造:
        #   dependencies に書くもの   = "<author>/<plugin>"  (例: "langgenius/gemini")
        #   ノードが参照するもの       = それに provider/tool/model を足した形
        #                               (例: "langgenius/gemini/google")
        # 参照 ref から「プラグイン名部分(最初の2セグメント)」を取り出して
        # dependencies と突き合わせる。
        parts = ref.split("/")
        # 可能な「プラグイン名」候補:
        #   - 完全一致: ref そのもの
        #   - "<author>/<plugin>" 形式 (2セグメントまで)
        #   - 1セグメント (author 省略形)
        candidates = {ref}
        if len(parts) >= 2:
            candidates.add("/".join(parts[:2]))  # 最初の2セグメント
            candidates.add(parts[1])              # name部分(author省略形)
        candidates.add(parts[0])                  # 先頭セグメント
        if candidates & declared_ids:
            continue
        plugin_part = "/".join(parts[:2]) if len(parts) >= 2 else ref
        errs.append(_err(
            "UNDECLARED_PLUGIN_REFERENCE",
            f"ノードが参照するプラグイン '{plugin_part}' (参照パス: '{ref}') が "
            f"dependencies に宣言されていない",
            severity="warning",
            fix_hint=f"dependencies に '{plugin_part}' を marketplace or github で追加",
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
    """技術者向けの構造化レポート(従来の出力)。"""
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


# ============================================================
# 🌸 やさしいレポート (非エンジニア向けの自然言語サマリ)
# ============================================================
#
# エラーコードを「何が起きているか / なぜまずいか / どう直すか」の
# 平易な日本語に翻訳して、Dify編集画面の運用者・教育担当者でも
# 読めるレポートを作る。
#
# 構造化された errors[] は LLM 修復フロー用にそのまま温存し、
# main() は両方 (`report_friendly` と `report_technical`) を返す。
# CLI はデフォルトで friendly を表示する。

_CODE_CATALOG: dict[str, dict] = {
    # === 🔴 重大: import後に編集画面が真っ白になる/実行できない ===
    "EDGE_DANGLING_SOURCE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "もう存在しないノードへの線が残っている",
        "why": "編集画面が真っ白になる原因。Difyが「あるはずのノードが見つからない」状態でクラッシュします。",
        "fix": "yml の `edges:` 配列から、該当する線(edge)を削除してください。",
    },
    "EDGE_DANGLING_TARGET": {
        "icon": "🔴", "severity_label": "重大",
        "title": "もう存在しないノードへの線が残っている",
        "why": "編集画面が真っ白になる原因。Difyが「あるはずのノードが見つからない」状態でクラッシュします。",
        "fix": "yml の `edges:` 配列から、該当する線(edge)を削除してください。",
    },
    "NO_START_NODE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "開始ノードがない",
        "why": "ワークフローの実行ができません(実行時にエラーになります)。",
        "fix": "`start` / `datasource` / `trigger-*` のいずれかのノードを追加してください。",
    },
    "MULTIPLE_START_NODES": {
        "icon": "🔴", "severity_label": "重大",
        "title": "開始ノードが複数ある",
        "why": "どこから始まるか不定になり、想定外の動作になります。",
        "fix": "開始ノードを1つだけにしてください。",
    },
    "NO_TERMINAL_NODE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "終了ノード(end / answer)がない",
        "why": "Workflowアプリは end、Chatflowアプリは answer ノードが必須です。",
        "fix": "`end` または `answer` ノードを追加してください。",
    },
    "DEP_INVALID_UID_FORMAT": {
        "icon": "🔴", "severity_label": "重大",
        "title": "プラグイン識別子の書き方が誤り",
        "why": "正しい形式 `[author/]name:N.N.N@hex` でないとプラグインが読み込まれません。",
        "fix": "例: `langgenius/google:0.1.1@<32-64桁hex>` の形式に修正してください。",
    },
    "DEP_INVALID_TYPE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "プラグイン依存の `type` が不正",
        "why": "type は `github` / `marketplace` / `package` のいずれかである必要があります。",
        "fix": "dependencies の `type` を正しい値に修正してください。",
    },
    "ITERATION_MISSING_START": {
        "icon": "🔴", "severity_label": "重大",
        "title": "イテレーションの開始ノードが欠落",
        "why": "iterationコンテナの中に `iteration-start` が無いとループが回りません。",
        "fix": "iterationの中に `iteration-start` ノードを追加してください。",
    },
    "LOOP_MISSING_START": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ループの開始ノードが欠落",
        "why": "loopコンテナの中に `loop-start` が無いと処理が始まりません。",
        "fix": "loopの中に `loop-start` ノードを追加してください。",
    },
    "LOOP_MISSING_END": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ループの終了ノードが欠落",
        "why": "loopコンテナの中に `loop-end` が無いと処理が終わりません。",
        "fix": "loopの中に `loop-end` ノードを追加してください。",
    },
    "DUPLICATE_NODE_ID": {
        "icon": "🔴", "severity_label": "重大",
        "title": "同じIDのノードが2つ以上ある",
        "why": "Difyがノードを区別できず、編集・実行で不具合が出ます。",
        "fix": "片方のノードのIDをユニークなものに変えてください。",
    },
    "DUPLICATE_EDGE_ID": {
        "icon": "🔴", "severity_label": "重大",
        "title": "同じIDの線(edge)が2つ以上ある",
        "why": "Difyが線を区別できず、編集・実行で不具合が出ます。",
        "fix": "片方の線のIDをユニークなものに変えてください。",
    },
    "NODE_MISSING_DATA": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ノード定義の `data` セクションが欠落",
        "why": "ノードのタイプ/タイトル/設定が読み込めずクラッシュします。",
        "fix": "ノードに `data: {type, title, ...}` を追加してください。",
    },
    "NODE_MISSING_TYPE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ノードに `data.type` がない",
        "why": "Difyがノードの種別を判別できません。",
        "fix": "BlockEnum の値 (start/end/llm/code/if-else など) を `data.type` に設定してください。",
    },
    "UNKNOWN_NODE_TYPE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "未知のノードタイプ",
        "why": "Difyに存在しないノードタイプが指定されています。",
        "fix": "BlockEnum の有効な値に修正してください。",
    },
    "NODE_MISSING_ID": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ノードに `id` がない",
        "why": "Difyが他のノードや線から参照できません。",
        "fix": "ノードに一意なIDを追加してください。",
    },
    "NODE_MISSING_POSITION": {
        "icon": "🔴", "severity_label": "重大",
        "title": "ノードの座標 `position` がない",
        "why": "編集画面の描画でクラッシュします。",
        "fix": "ノードに `position: {x: <数値>, y: <数値>}` を追加してください。",
    },
    "EDGE_MISSING_SOURCE": {
        "icon": "🔴", "severity_label": "重大",
        "title": "線(edge)に `source` がない",
        "why": "どのノードから出ている線か分かりません。",
        "fix": "edge に `source` を追加してください。",
    },
    "EDGE_MISSING_TARGET": {
        "icon": "🔴", "severity_label": "重大",
        "title": "線(edge)に `target` がない",
        "why": "どのノードへ向かう線か分かりません。",
        "fix": "edge に `target` を追加してください。",
    },
    "PARSE_ERROR": {
        "icon": "🔴", "severity_label": "重大",
        "title": "YAMLの構文エラー",
        "why": "そもそも読み込めません。",
        "fix": "YAML構文を見直してください(インデント・コロン・引用符など)。",
    },
    "MISSING_APP": {
        "icon": "🔴", "severity_label": "重大",
        "title": "`app` セクションが無い",
        "why": "Difyは `app` の存在を前提に動きます。",
        "fix": "yml 最上位に `app: {name, mode, icon, ...}` を追加してください。",
    },
    "MISSING_WORKFLOW": {
        "icon": "🔴", "severity_label": "重大",
        "title": "`workflow` セクションが無い",
        "why": "ワークフロー定義が読み込めません。",
        "fix": "yml 最上位に `workflow:` を追加してください。",
    },
    "MISSING_GRAPH": {
        "icon": "🔴", "severity_label": "重大",
        "title": "`workflow.graph` が無い",
        "why": "ノード・線の定義が読み込めません。",
        "fix": "`workflow.graph: {nodes:[...], edges:[...]}` を追加してください。",
    },
    # === 🟡 注意: 動くが、別環境や運用で問題化する可能性 ===
    "UNDECLARED_PLUGIN_REFERENCE": {
        "icon": "🟡", "severity_label": "注意",
        "title": "プラグイン宣言の漏れ",
        "why": "ノードが使っているプラグインが `dependencies:` に宣言されていません。"
               "Difyの参照は2層構造になっていて、`dependencies:` には**プラグイン名**を、"
               "ノードはその中の provider/tool/model 名を参照します。"
               "例: dependencies=`langgenius/gemini`、ノードは `google` provider を参照。"
               "現環境では動くが、DSLを別ワークスペース・別環境にimportすると動かなくなります。",
        "fix": "ノードが参照している plugin 部分(`<author>/<name>` 形式)を `dependencies:` に "
               "marketplace または github 経由で追加してください。",
    },
    "UNREACHABLE_NODE": {
        "icon": "🟡", "severity_label": "注意",
        "title": "開始ノードから繋がっていないノードがある",
        "why": "そのノードは実行されません。意図しないなら線(edge)を引いてください。",
        "fix": "開始ノードからの経路上に線(edge)を追加するか、不要なら該当ノードを削除してください。",
    },
    "NODE_NO_OUTGOING": {
        "icon": "🟡", "severity_label": "注意",
        "title": "行き止まりノード(次への線がない)",
        "why": "そこで処理が止まります。意図的でなければ次のノードへの線が必要です。",
        "fix": "次のノードへの線を追加、または end / answer ノードへ接続してください。",
    },
    "EDGE_MISSING_SOURCE_HANDLE": {
        "icon": "🟡", "severity_label": "注意",
        "title": "線の出口情報 `sourceHandle` がない",
        "why": "if-else など分岐があるノードで、どの分岐か判別できません。",
        "fix": "`sourceHandle: 'source'` (if-else なら 'true' / 'false' / 'elif-xxx') を追加してください。",
    },
    "EDGE_MISSING_TARGET_HANDLE": {
        "icon": "🟡", "severity_label": "注意",
        "title": "線の入口情報 `targetHandle` がない",
        "why": "Difyの描画・実行で予期しない動作になります。",
        "fix": "`targetHandle: 'target'` を追加してください。",
    },
}


def _parse_dangling_edge(edge_id: str) -> tuple[str, str]:
    """`<src>-source-<tgt>-target` 形式から (src, tgt) を取り出す。失敗時は (edge_id, '')。"""
    if not edge_id:
        return ("", "")
    if "-source-" in edge_id and edge_id.endswith("-target"):
        body = edge_id[: -len("-target")]
        src, _, tgt = body.partition("-source-")
        return (src, tgt)
    return (edge_id, "")


def _node_title_map(dsl: dict) -> dict[str, str]:
    """nodeId → 「ノードタイトル (タイプ)」の対応表を作る。"""
    out: dict[str, str] = {}
    g = ((dsl or {}).get("workflow") or {}).get("graph") or {}
    for n in (g.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        nid = n.get("id") or ""
        if not nid:
            continue
        data = n.get("data") or {}
        title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
        ntype = (data.get("type") or "").strip() if isinstance(data, dict) else ""
        if title and ntype:
            out[nid] = f"「{title}」({ntype})"
        elif title:
            out[nid] = f"「{title}」"
        elif ntype:
            out[nid] = f"({ntype}ノード)"
        else:
            out[nid] = nid
    return out


def _describe_node(nid: str, titles: dict[str, str]) -> str:
    """既知ならタイトル付き、未知なら「(削除済み)」を添えて返す。"""
    if not nid:
        return "(空)"
    if nid in titles:
        return f"{titles[nid]} `{nid}`"
    return f"`{nid}` (※ 存在しない／削除済み)"


def _build_friendly_report(errors: list[dict], dsl: dict) -> str:
    """非エンジニア向けの自然言語レポート。"""
    wf = (dsl or {}).get("workflow") or {}
    g = wf.get("graph") or {}
    n_cnt = len(g.get("nodes") or [])
    e_cnt = len(g.get("edges") or [])
    titles = _node_title_map(dsl)

    err_cnt = sum(1 for e in errors if e["severity"] == "error")
    warn_cnt = sum(1 for e in errors if e["severity"] == "warning")

    head = [
        "=" * 60,
        "  Dify DSL 検査結果",
        "=" * 60,
        f"  ノード数: {n_cnt}  ／  線(edge)数: {e_cnt}",
        "",
    ]

    if not errors:
        head += [
            "[判定] ✅ OK (問題は見つかりませんでした)",
            "",
        ]
        return "\n".join(head)

    head += [
        f"[判定] {'🔴 NG (要修復)' if err_cnt else '🟡 注意あり'}",
        f"   🔴 重大な問題: {err_cnt} 件",
        f"   🟡 注意事項  : {warn_cnt} 件",
        "",
    ]

    # 重大度ごとに分割
    sev_groups: dict[str, list[dict]] = {"error": [], "warning": []}
    for e in errors:
        sev_groups.setdefault(e["severity"], []).append(e)

    body: list[str] = []

    def render_group(severity: str, header_text: str) -> None:
        items = sev_groups.get(severity) or []
        if not items:
            return
        # コード別にまとめる
        by_code: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            by_code[it["code"]].append(it)

        # 同じタイトル(=ユーザーから見て同じ問題)のコードをマージする。
        # 例: EDGE_DANGLING_SOURCE と EDGE_DANGLING_TARGET は両方とも
        # 「もう存在しないノードへの線が残っている」が原因なので1件として扱う。
        # 構造: [(代表タイトル, [(code, [items]), ...]), ...]  (順序保持)
        merged_groups: list[tuple[str, list[tuple[str, list[dict]]]]] = []
        title_to_idx: dict[str, int] = {}
        for code, group in by_code.items():
            cat = _CODE_CATALOG.get(code, {})
            title = cat.get("title", code)
            if title in title_to_idx:
                merged_groups[title_to_idx[title]][1].append((code, group))
            else:
                title_to_idx[title] = len(merged_groups)
                merged_groups.append((title, [(code, group)]))

        body.append("=" * 60)
        body.append(f"  {header_text}")
        body.append("=" * 60)
        body.append("")

        # EDGE_DANGLING_SOURCE と TARGET があれば、まず「推測される状況」として
        # 「削除済みの中間ノード」を可視化する (最重要シグナル)
        dangling_src = by_code.get("EDGE_DANGLING_SOURCE") or []
        dangling_tgt = by_code.get("EDGE_DANGLING_TARGET") or []
        if dangling_src or dangling_tgt:
            missing_nodes: dict[str, dict[str, list[str]]] = defaultdict(
                lambda: {"upstream": [], "downstream": []}
            )
            for it in dangling_src:
                src, tgt = _parse_dangling_edge(it.get("edge_id") or "")
                missing_nodes[src]["downstream"].append(tgt)
            for it in dangling_tgt:
                src, tgt = _parse_dangling_edge(it.get("edge_id") or "")
                missing_nodes[tgt]["upstream"].append(src)

            if missing_nodes:
                body.append(
                    f"💡 [中間ノードが消えた痕跡]  {len(missing_nodes)} 箇所で、"
                    "ノードが削除されたまま線(edge)だけが残っています:"
                )
                body.append("")
                idx = 0
                for mid_id, ctx in missing_nodes.items():
                    idx += 1
                    ups = ", ".join(_describe_node(u, titles) for u in ctx["upstream"]) or "(なし)"
                    dns = ", ".join(_describe_node(d, titles) for d in ctx["downstream"]) or "(なし)"
                    body.append(f"   箇所{idx}: {ups}")
                    body.append(f"         │")
                    body.append(f"         ▼  ❌ `{mid_id}` (削除済み)")
                    body.append(f"         │")
                    body.append(f"         ▼")
                    body.append(f"      {dns}")
                    body.append("")
                body.append(
                    "   → 修復: 上記の関連する線(edge)を yml の `edges:` から削除。"
                    "中間ノードのロジックはバージョン履歴から復元してください。"
                )
                body.append("")

        problem_no = 0
        for title, code_group_pairs in merged_groups:
            problem_no += 1
            # 代表として最初のコードの catalog を使う(全コードが同じタイトル=同じ問題のはず)
            first_code = code_group_pairs[0][0]
            cat = _CODE_CATALOG.get(first_code, {})
            icon = cat.get("icon", "🔴" if severity == "error" else "🟡")
            why = cat.get("why", "")
            fix = cat.get("fix", "")
            # 全アイテムを1つにまとめる
            all_items: list[dict] = [it for _, g in code_group_pairs for it in g]
            count = len(all_items)
            # マージ元のコードが複数あれば末尾に出す(技術者向け参考情報)
            codes_used = [c for c, _ in code_group_pairs]
            codes_suffix = ""
            if len(codes_used) > 1:
                codes_suffix = "  [内訳: " + ", ".join(
                    f"{c}×{len(g)}" for c, g in code_group_pairs
                ) + "]"

            body.append(f"【{problem_no}】{icon} {title} (該当 {count} 件){codes_suffix}")
            if why:
                body.append(f"   📖 何が起きている?: {why}")

            # 全コードが EDGE_DANGLING 系なら簡潔な edge ID 一覧
            if all(c in ("EDGE_DANGLING_SOURCE", "EDGE_DANGLING_TARGET")
                   for c in codes_used):
                body.append("   📍 該当の線(edge) ID:")
                for it in all_items[:12]:
                    body.append(f"      - {it.get('edge_id', '(不明)')}")
                if len(all_items) > 12:
                    body.append(f"      ... 他 {len(all_items) - 12} 件")
            else:
                body.append("   📍 該当箇所:")
                for it in all_items[:10]:
                    parts: list[str] = []
                    if it.get("node_id"):
                        parts.append(_describe_node(it["node_id"], titles))
                    if it.get("edge_id"):
                        parts.append(f"線 `{it['edge_id']}`")
                    where = " ／ ".join(parts) if parts else ""
                    msg = it.get("message", "")
                    if where:
                        body.append(f"      - {where}")
                        body.append(f"        {msg}")
                    else:
                        body.append(f"      - {msg}")
                if len(all_items) > 10:
                    body.append(f"      ... 他 {len(all_items) - 10} 件")

            if fix:
                body.append(f"   ✅ 修復方法: {fix}")
            body.append("")

    render_group("error", "⚠️  重大な問題 (このままだと編集画面が開かない／実行できない)")
    render_group("warning", "💡 注意事項 (動くが、別環境への持ち出しや運用で問題化の可能性)")

    # まとめ
    body.append("=" * 60)
    body.append("  📋 まとめ (どこから手を付けるか)")
    body.append("=" * 60)
    body.append("")
    step = 0
    if err_cnt:
        step += 1
        body.append(f"   {step}. まず 🔴 重大な問題 {err_cnt} 件を修復(編集画面を開けるようにする)")
    if warn_cnt:
        step += 1
        verb = "続いて" if err_cnt else "まず"
        body.append(f"   {step}. {verb} 🟡 注意事項 {warn_cnt} 件を修復(別環境への持ち出しに備える)")
    step += 1
    body.append(f"   {step}. 修復後、再度このバリデータを実行して全部 OK になることを確認")
    body.append("")

    return "\n".join(head + body)


# ---- Dify コードノード エントリーポイント ----
# ---------------------------------------------------------------------------
# --fix モード: 決定的に直せるパターンだけ自動修復する
# ---------------------------------------------------------------------------
# 対象 (Tier 1):
#   - EDGE_DANGLING (中間ノード消失): A→X + X→B が揃っていれば A→B に直結
#   - EDGE_DANGLING (片側のみ): edge を削除
#   - EDGE_MISSING_SOURCE / EDGE_MISSING_TARGET: edge を削除
#   - DUPLICATE_EDGE_ID: 先頭1本だけ残す
#   - EDGE_MISSING_SOURCE_HANDLE / TARGET_HANDLE: 'source' / 'target' 補完
#     (if-else は条件分岐 handle を持つので skip)
# 対象外: NO_START_NODE / UNREACHABLE_NODE / プラグイン系 / コンテナ内部 など


def _fix(dsl: dict) -> tuple[dict, list[str]]:
    """壊れたDSLを Tier1 ルールで修復した dict と修復ログを返す。元は変更しない。"""
    import copy
    fixed = copy.deepcopy(dsl)
    log: list[str] = []

    graph = ((fixed or {}).get("workflow") or {}).get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = list(graph.get("edges") or [])

    # ノードIDセット (custom-note は edge 接続不可なので除外しない: id 比較のみ)
    id_set = {n.get("id") for n in nodes if n.get("id")}

    # ノードIDごとの type を引けるように
    node_type = {n.get("id"): (n.get("data") or {}).get("type")
                 for n in nodes if n.get("id")}

    # ---- 1. DUPLICATE_EDGE_ID: 先頭だけ残す ----
    seen_eids: set[str] = set()
    deduped: list[dict] = []
    for e in edges:
        eid = e.get("id")
        if eid and eid in seen_eids:
            log.append(f"重複 edge id を削除: {eid}")
            continue
        if eid:
            seen_eids.add(eid)
        deduped.append(e)
    edges = deduped

    # ---- 2. EDGE_MISSING_SOURCE/TARGET: source/target が無い edge は削除 ----
    cleaned: list[dict] = []
    for e in edges:
        if not e.get("source") or not e.get("target"):
            log.append(f"source/target が欠落した edge を削除: {e.get('id')}")
            continue
        cleaned.append(e)
    edges = cleaned

    # ---- 3. EDGE_DANGLING: 消失ノードごとに in/out を集めて rewire or delete ----
    # 消失ノード毎の (in_edges, out_edges) を集計
    dangling_in: dict[str, list[dict]] = defaultdict(list)   # X が target
    dangling_out: dict[str, list[dict]] = defaultdict(list)  # X が source
    for e in edges:
        if e.get("target") not in id_set:
            dangling_in[e["target"]].append(e)
        if e.get("source") not in id_set:
            dangling_out[e["source"]].append(e)

    edges_to_remove: set[int] = set()  # 削除対象 edge の id(オブジェクト) を保持
    edges_to_add: list[dict] = []
    # 全 dangling ノードを巡回 (in/out のキー和集合)
    for missing in set(list(dangling_in.keys()) + list(dangling_out.keys())):
        ins = dangling_in.get(missing, [])
        outs = dangling_out.get(missing, [])
        if ins and outs:
            # rewire: cartesian product
            for in_e in ins:
                for out_e in outs:
                    sh = in_e.get("sourceHandle") or "source"
                    new_edge = {
                        "data": {
                            "isInIteration": (in_e.get("data") or {}).get("isInIteration", False),
                            "isInLoop": (in_e.get("data") or {}).get("isInLoop", False),
                            "sourceType": (in_e.get("data") or {}).get("sourceType"),
                            "targetType": (out_e.get("data") or {}).get("targetType"),
                        },
                        "id": f"{in_e['source']}-{sh}-{out_e['target']}-target",
                        "selected": False,
                        "source": in_e["source"],
                        "sourceHandle": sh,
                        "target": out_e["target"],
                        "targetHandle": out_e.get("targetHandle") or "target",
                        "type": "custom",
                        "zIndex": 0,
                    }
                    edges_to_add.append(new_edge)
                    log.append(
                        f"消失ノード '{missing}' を迂回: "
                        f"{in_e['source']} → {out_e['target']} (新 edge 追加)"
                    )
            for e in ins + outs:
                edges_to_remove.add(id(e))
        else:
            # 片側のみ → 削除
            for e in ins + outs:
                edges_to_remove.add(id(e))
                log.append(
                    f"消失ノード '{missing}' を参照する edge を削除: {e.get('id')}"
                )

    edges = [e for e in edges if id(e) not in edges_to_remove] + edges_to_add

    # 新規 edge の id 重複を再除去 (rewire 後に偶発的に同一 id が生まれた場合)
    seen_eids = set()
    final: list[dict] = []
    for e in edges:
        eid = e.get("id")
        if eid and eid in seen_eids:
            log.append(f"修復後の edge id 重複を解消: {eid}")
            continue
        if eid:
            seen_eids.add(eid)
        final.append(e)
    edges = final

    # ---- 4. handle 補完 (if-else 以外) ----
    for e in edges:
        src = e.get("source")
        if e.get("sourceHandle") is None:
            if node_type.get(src) == "if-else":
                # if-else は 'true'/'false'/case_id を持つので自動補完は危険
                continue
            e["sourceHandle"] = "source"
            log.append(f"sourceHandle 補完: edge {e.get('id')}")
        if e.get("targetHandle") is None:
            e["targetHandle"] = "target"
            log.append(f"targetHandle 補完: edge {e.get('id')}")

    graph["edges"] = edges
    fixed.setdefault("workflow", {}).setdefault("graph", {})
    fixed["workflow"]["graph"]["edges"] = edges

    return fixed, log


def _dump_yaml(dsl: dict) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    except ImportError:
        return json.dumps(dsl, ensure_ascii=False, indent=2)


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
        "report": _build_friendly_report(errors, dsl),
        "report_technical": _build_report(errors, dsl),
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
    args = [a for a in sys.argv[1:] if a]
    show_technical = "--technical" in args
    do_fix = "--fix" in args
    dry_run = "--dry-run" in args
    output_path: str | None = None
    # -o / --output <path>
    for flag in ("-o", "--output"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(f"{flag} requires a path argument", file=sys.stderr)
                sys.exit(2)
            output_path = args[i + 1]
            del args[i:i + 2]
    args = [a for a in args
            if a not in ("--technical", "--fix", "--dry-run")]

    if len(args) != 1:
        print(
            "usage: uv run validate_dify_dsl.py <dsl.yml>\n"
            "       [--technical] [--fix [--dry-run] [-o <out.yml>]]",
            file=sys.stderr,
        )
        sys.exit(2)

    input_path = args[0]
    with open(input_path, encoding="utf-8") as f:
        text = f.read()
    result = main(text)

    if not do_fix:
        if show_technical:
            print(result.get("report_technical") or result["report"])
        else:
            print(result["report"])
        sys.exit(0 if result["is_valid"] else 1)

    # --- --fix モード ---
    if result["is_valid"]:
        print("修復不要: エラーは検出されませんでした。", file=sys.stderr)
        sys.exit(0)

    try:
        dsl = _parse(text)
    except Exception as ex:
        print(f"パース失敗 — 修復不可: {ex}", file=sys.stderr)
        sys.exit(1)

    fixed_dsl, fix_log = _fix(dsl if isinstance(dsl, dict) else {})
    fixed_text = _dump_yaml(fixed_dsl)

    # 修復後を再検証
    after = main(fixed_text)

    print("=" * 60, file=sys.stderr)
    print(f"  自動修復ログ ({len(fix_log)} 件)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    for line in fix_log:
        print(f"  ・{line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        f"修復前: error={result['summary']['error_count']} "
        f"warning={result['summary']['warning_count']}",
        file=sys.stderr,
    )
    print(
        f"修復後: error={after['summary']['error_count']} "
        f"warning={after['summary']['warning_count']}",
        file=sys.stderr,
    )
    if after["summary"]["error_count"] > 0:
        print(
            "\n⚠️  未修復のエラーが残っています (構造判断が必要なため自動修復対象外):",
            file=sys.stderr,
        )
        remaining_codes = sorted({e["code"] for e in after["errors"]
                                  if e["severity"] == "error"})
        for code in remaining_codes:
            print(f"  - {code}", file=sys.stderr)
        print("\n詳細は次のコマンドで確認:", file=sys.stderr)
        print(f"  uv run validate_dify_dsl.py <出力ファイル>",
              file=sys.stderr)

    if dry_run:
        print("\n[--dry-run] ファイルは書き出していません。", file=sys.stderr)
        sys.exit(0)

    # 出力先決定: 既定は <input>.fixed.yml
    if output_path is None:
        if input_path.endswith((".yml", ".yaml")):
            base = input_path.rsplit(".", 1)[0]
            ext = input_path.rsplit(".", 1)[1]
            output_path = f"{base}.fixed.{ext}"
        else:
            output_path = input_path + ".fixed"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(fixed_text)
    print(f"\n修復済み yml を書き出しました: {output_path}", file=sys.stderr)
    sys.exit(0)
