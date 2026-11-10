# CelesTrak コネクタ（Fivetran Connector SDK）

[English](README.md) | 日本語

[CelesTrak](https://celestrak.org) の人工衛星データを同期する Fivetran Connector SDK 用コネクタです。API キーは不要です。

次の 3 種類のデータセットを同期します。

| テーブル | 主キー | 取得元エンドポイント | 内容 |
|---|---|---|---|
| `gp_element_set` | `norad_cat_id`, `epoch` | `NORAD/elements/gp.php` | GP 軌道要素。衛星ごと・エポックごとに 1 行で、履歴が蓄積されます。 |
| `gp_group_membership` | `group_name`, `norad_cat_id` | `NORAD/elements/gp.php` | 設定した CelesTrak グループのうち、各衛星がどのグループに含まれていたか。 |
| `satcat` | `norad_cat_id` | `satcat/records.php` | 衛星カタログ（SATCAT）。所有者、打ち上げ日・消滅日、軌道タイプ、状態など。最新の値で上書きされます。 |
| `supgp_element_set` | `supgp_file`, `norad_cat_id`, `epoch` | `NORAD/elements/supplemental/sup-gp.php` | 運用者が提供する補足軌道要素（例: Starlink、OneWeb）。 |

スキーマで宣言しているのは主キーと `norad_cat_id`（`INT`）だけです。それ以外のカラムは自動推論されるため、CelesTrak 側でフィールドが追加されても同期は壊れません。フィールド名は小文字に変換されます（`NORAD_CAT_ID` は `norad_cat_id` になります）。

## 設定

`configuration.json` を編集します。値はすべて文字列です。

| キー | デフォルト | 説明 |
|---|---|---|
| `gp_groups` | `stations,active` | CelesTrak の GP グループ名（`GROUP` パラメータ）をカンマ区切りで指定します。空にすると GP データを同期しません。 |
| `satcat_groups` | `stations,active` | SATCAT 用のグループ名をカンマ区切りで指定します。空にすると SATCAT を同期しません。 |
| `supgp_files` | `starlink,oneweb` | SupGP のファイル名（`FILE` パラメータ）をカンマ区切りで指定します。空にすると SupGP を同期しません。 |
| `min_fetch_interval_hours` | `2` | 同じクエリを再取得するまでの最小間隔（時間）。 |

`gp_groups`、`satcat_groups`、`supgp_files` のうち少なくとも 1 つは設定してください。

## 動作の仕組み

- **クエリごとに全件スナップショットを取得します。** CelesTrak には「前回以降の変更分」を取得する機能がないため、各クエリは現在のスナップショットを毎回読み込みます。upsert は冪等なので、重複は発生しません。
- **リクエスト頻度の制限があります。** CelesTrak の更新は約 2 時間ごとで、同じデータを短い間隔で何度も要求するクライアントはブロックされます。state に各クエリ（例: `gp:stations`）の最終取得時刻を保存し、`min_fetch_interval_hours` 以内に取得済みのクエリはスキップします。
- **チェックポイント。** クエリが 1 つ終わるごとに state をチェックポイントします。途中で失敗しても、完了済みのクエリはやり直しになりません。
- **リトライ。** タイムアウト、429、5xx は指数バックオフ（5 秒、10 秒、20 秒）で最大 4 回までリトライします。403 は CelesTrak が過剰なリクエストをブロックする際に返すため、リトライせずメッセージを出して同期を停止します。
- **空の結果。** 存在しないグループやファイルを指定すると、CelesTrak は `No GP data found` のようなプレーンテキストを返します。コネクタは警告ログを出して処理を続行します。

## テスト

### 1. セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fivetran-connector-sdk
```

`requests` は Fivetran の実行環境にプリインストールされているため、`requirements.txt` は不要です。

### 2. ローカルでコネクタを実行する

プロジェクトのディレクトリで次を実行します。

```bash
fivetran debug
```

`configuration.json` を使って `connector.py` を実行し、結果をローカルの DuckDB ファイル `files/warehouse.db` に書き込みます。保存された state は `files/state.json` に出力されます。デフォルト設定での初回実行は 6 回のリクエスト（GP 2 グループ、SATCAT 2 グループ、SupGP 2 ファイル）になります。`active` グループはデータ量が多いため、数分かかることがあります。

成功すると、末尾に操作のサマリー（upsert 数、チェックポイント数）が表示され、`SEVERE` の行は出ません。

### 3. 出力を確認する

```bash
python - <<'EOF'
import duckdb
con = duckdb.connect("files/warehouse.db", read_only=True)
for t in ["gp_element_set", "gp_group_membership", "satcat", "supgp_element_set"]:
    print(t, con.execute(f"select count(*) from tester.{t}").fetchone()[0])
print(con.execute("select norad_cat_id, epoch, object_name from tester.gp_element_set where norad_cat_id = 25544").fetchall())
EOF
```

テーブルは `tester` スキーマに作成されます。stations のみの実行（19 衛星）は約 15 秒で完了します。次の点を確認してください。

- すべてのテーブルに行があること（テーブルは行を受け取って初めて作成されるため、SupGP を無効にした場合やファイル名が受け付けられない場合、`supgp_element_set` は存在せず、ログに `No ... data found` の警告が出ます）。
- `satcat` には、消滅済みの衛星が同期されるまで `decay_date` カラムがありません（カラムは null 以外の値から推論されるため）。
- `epoch` が `TIMESTAMP` として読み込まれていること。CelesTrak はタイムゾーンなしで値を返しますが、値は UTC です。
- 複数のグループに属する衛星（例: ISS、25544 は `stations` と `active` の両方に属します）が、`gp_element_set` には 1 行、`gp_group_membership` にはグループごとに 1 行ずつ入っていること。

### 4. 再同期の動作を確認する

すぐにもう一度 `fivetran debug` を実行します。すべてのクエリが `Skipping gp:stations: fetched less than 2.0h ago` のようにスキップされ、HTTP リクエストは発生しません。

テスト中に強制的に再取得したい場合は、次のいずれかを行います（2 時間の制限があるため、本物の API に対して繰り返し実行しないでください）。

- `configuration.json` で `"min_fetch_interval_hours": "0"` に設定する。
- `fivetran reset` を実行して保存済みの state を消去する。

上記の主キーで upsert されるため、再取得しても行が重複することはありません。

### 5. まず小さな設定で試す

初回の実行を短く済ませ、CelesTrak への負荷も抑えるため、最初は小さなグループ 1 つから始めることをおすすめします。

```json
{
  "gp_groups": "stations",
  "satcat_groups": "stations",
  "supgp_files": "",
  "min_fetch_interval_hours": "2"
}
```

### トラブルシューティング

| 症状 | 考えられる原因 |
|---|---|
| `CelesTrak returned 403 ...` | 同じデータへのリクエストが多すぎます。数時間待ち、`min_fetch_interval_hours` を 2 以上にしてください。 |
| `failed after 4 attempts: HTTP 503` | CelesTrak が一時的に利用できません。時間をおいて再実行してください。 |
| `CelesTrak returned no data for {...}` の警告 | グループ名またはファイル名が正しくありません。CelesTrak のサイトで名前を確認してください。 |
| `Nothing to sync: set at least one of ...` | `gp_groups`、`satcat_groups`、`supgp_files` がすべて空です。 |

## デプロイ

ローカルでのテストが通ったら、`fivetran deploy` でデプロイします。

```bash
export FIVETRAN_API_KEY=<Base64 エンコード済みの API キー>
fivetran deploy \
  --api-key "$FIVETRAN_API_KEY" \
  --destination <デスティネーションのグループ名> \
  --connection <コネクション名> \
  --configuration configuration.json \
  --python 3.12
```

- `--destination` はデスティネーションのグループ名です。ダッシュボードの表示名と異なる場合があります。
- `--connection` はデスティネーション側のスキーマ名になります。
- デプロイではコネクションが作成されるだけで、初回同期は開始されません。Fivetran のダッシュボードから開始してください。
- 同期は 2 時間より短い間隔でスケジュールしないでください。CelesTrak に 403 でブロックされる可能性があります。
- API キーは絶対にコミットしないでください。`.gitignore` で `.env` と `options.toml` を除外しています。

すべてのオプションは [Connector SDK のドキュメント](https://fivetran.com/docs/connector-sdk)を参照してください。
