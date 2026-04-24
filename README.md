# dify-dsl-validation

Dify DSL / Plugin manifest の構造バリデータ。壊れたYAMLを import して
エディタが真っ白になる事故を検出し、LLM 修復フローに渡せる形式で
エラーを返す。

## ツール

### `validate_dify_dsl.py` — Workflow DSL 検証

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

### `validate_dify_plugin.py` — Plugin manifest 検証

`langgenius/dify-plugin-daemon` の Go 実装に準拠:

- `pkg/entities/plugin_entities/plugin_declaration.go` — `PluginDeclaration`
- `pkg/entities/plugin_entities/identity.go` — `PluginUniqueIdentifier` 正規表現
- `pkg/entities/plugin_entities/tool_declaration.go` — `ToolProviderDeclaration`
- `pkg/entities/manifest_entities/version.go` — version regex
- `pkg/entities/manifest_entities/tags.go` — `PluginTag` 列挙
- `pkg/entities/constants/{arch,language}.go` — amd64/arm64, python 限定

manifest.yaml 単体でも、プラグインディレクトリ丸ごとでも検証可能。後者では:
- `_assets/<icon>` 存在確認
- `plugins.tools[]` が指す provider yaml と、その中の tool yaml 再帰検証
- I18n 必須フィールド (en_US)、parameter type/form の整合性

## 使い方

```bash
# CLI (uv の PEP 723 inline script metadata で依存自動解決)
uv run validate_dify_dsl.py <dsl.yml>
uv run validate_dify_plugin.py <plugin_dir | manifest.yaml>
```

終了コード: OK=0 / エラーあり=1

### Dify コードノードとして

両ファイルは `main(text: str) -> dict` を公開し、そのまま Dify の code node
にコピペできる。出力変数:

- `is_valid` (boolean)
- `errors` (array[object]) — `{code, severity, message, node_id, edge_id, fix_hint}`
- `report` (string) — 人間可読サマリ
- `graph` (object) (DSL版のみ)
- `summary` (object) — カウント

sandbox に `yaml` が無い環境では上流で YAML→JSON 変換し、JSON 文字列を
渡せば動く (json フォールバック実装済み)。

## 想定ワークフロー (修復)

```
[Start: dsl_text]
  → [Code: validate_dify_dsl.main]
  → [IF is_valid == false]
      ├─ true  → [End: pass-through]
      └─ false → [LLM: errors + graph を根拠に修復]
                 → [Code: re-validate]
                 → [End: fixed DSL]
```

LLM には `errors[].code` と `errors[].fix_hint` を渡せば構造修復できる設計。
