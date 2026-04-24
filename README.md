# dify-dsl-validation

Dify Workflow DSL (YAML) の構造バリデータ。壊れた DSL を import して
編集画面が真っ白になる事故を検出し、LLM 修復フローに渡せる形式で
エラーを返す。

## ツール

### `validate_dify_dsl.py` — Workflow DSL 構造検証

Dify 本体のソース (`langgenius/dify`) に準拠:

- `api/services/app_dsl_service.py` — import 時の必須キー
- `api/core/workflow/workflow_entry.py` — `_NodeConfigDict` / `_EdgeConfigDict`
- `api/core/workflow/node_factory.py` — `_START_NODE_TYPES`, `custom-note` 扱い
- `api/core/plugin/entities/plugin.py` — `PluginDependency`
- `web/app/components/workflow/types.ts` — `BlockEnum`

**検出できる壊れ方:**

| コード | 内容 |
|---|---|
| `EDGE_DANGLING_SOURCE/TARGET` | edge が nodes に無いIDを参照 (import は通るが編集画面真っ白の主因) |
| `NO_START_NODE` | start/datasource/trigger-* が存在しない (実行時 ValueError) |
| `UNREACHABLE_NODE` | start から到達不能 |
| `NODE_NO_OUTGOING` | 行き止まりノード |
| `DEP_INVALID_UID_FORMAT` | plugin_unique_identifier が `[author/]name:N.N.N@hex` 形式でない |
| `UNDECLARED_PLUGIN_REFERENCE` | ノードが参照する plugin が dependencies に無い |
| `ITERATION/LOOP_MISSING_START/END` | iteration/loop コンテナの内部ノード欠落 |
| ほか | node.id/data.type/position 欠落、重複 ID、end/answer 欠落 など |

### `validate_dsl_plugin_usage.py` — DSL 内の plugin 使い方検証

DSL の tool / llm ノードが参照する plugin の実定義と整合するかを検証する。
真実の源は 2 段フォールバック:

1. `langgenius/dify-official-plugins` (source yaml)
2. `langgenius/dify-plugins` (marketplace ミラー、`.difypkg` を zip 展開して yaml 抽出)

**検出できる不整合:**

| コード | severity | 内容 |
|---|---|---|
| `PLUGIN_NOT_FOUND` | error | plugin 自体がどこにも見つからない (typo or 未公開) |
| `PROVIDER_NOT_FOUND` | error | plugin は存在するが provider 名が無い |
| `TOOL_NOT_FOUND` | error | provider は存在するが tool_name が無い |
| `MODEL_NOT_FOUND` | error | model plugin に該当 model 名が無い |
| `TOOL_PARAM_UNKNOWN` | error | `tool_parameters` に未定義キー |
| `TOOL_PARAM_MISSING_REQUIRED` | error | required パラメータが渡されていない |
| `TOOL_PARAM_INVALID_OPTION` | error | select 型で options に無い値 |
| `PLUGIN_LOCAL_PACKAGE_UNVERIFIABLE` | warning | `dependencies.type: package` (ローカル .difypkg) は検証不可 |

キャッシュは `~/.cache/dify-dsl-validation/plugins/<author>/<name>/<version>/`。
同じ version は再ダウンロードしない。

`plugin_unique_identifier` に version pinning があれば、その version の
`.difypkg` をピンポイント取得して検証する。

## 使い方

```bash
# CLI (uv の PEP 723 inline script metadata で依存自動解決)
uv run validate_dify_dsl.py <dsl.yml>
uv run validate_dsl_plugin_usage.py <dsl.yml>
```

終了コード: OK=0 / エラーあり=1

### Dify コードノードとして

両ファイルは `main(...) -> dict` を公開し、そのまま Dify の code node
にコピペできる。出力変数:

- `is_valid` (boolean)
- `errors` (array[object]) — `{code, severity, message, node_id, fix_hint, ...}`
- `report` (string) — 人間可読サマリ
- `graph` (object) (`validate_dify_dsl.py` のみ)
- `summary` (object) — カウント

`validate_dify_dsl.py` は sandbox に `yaml` が無い環境で上流 YAML→JSON 変換
すれば動く (json フォールバック実装済み)。
`validate_dsl_plugin_usage.py` は外部 HTTP が使えない sandbox 用に
`main(dsl_text, plugin_cache)` を受け取れる形にしてある
(事前取得した plugin 定義 dict を渡す想定)。

## 想定ワークフロー (修復)

```
[Start: dsl_text]
  → [Code: validate_dify_dsl.main]         構造検証
  → [Code: validate_dsl_plugin_usage.main]  plugin 使い方検証
  → [Merge errors]
  → [IF is_valid == false]
      ├─ true  → [End: pass-through]
      └─ false → [LLM: errors を根拠に修復]
                 → [Code: re-validate]
                 → [End: fixed DSL]
```

LLM には `errors[].code` と `errors[].fix_hint` を渡せば構造修復できる設計。
